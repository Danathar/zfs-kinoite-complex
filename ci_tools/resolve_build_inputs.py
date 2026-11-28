"""
Script: ci_tools/resolve_build_inputs.py
What: Resolves and validates input references for one `main` workflow run.
Doing: Uses lock file or defaults, resolves digests/tags, and writes outputs.
Why: Keeps all jobs in the run on the same inputs.
Goal: Provide trusted base/build/kernel values for downstream steps.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ci_tools.classify_akmods_failure import kernel_major_minor
from ci_tools.common import (
    REGISTRY_METADATA_TIMEOUT,
    REGISTRY_RETRY_ATTEMPTS,
    REGISTRY_TRANSFER_TIMEOUT,
    CiToolError,
    extract_fedora_version,
    git_ls_remote_resolve,
    is_missing_image_error,
    is_registry_permission_error,
    load_repo_defaults,
    normalize_owner,
    optional_env,
    require_env,
    require_env_or_default,
    run_cmd,
    run_cmd_with_retries,
    run_json_cmd,
    skopeo_inspect_digest,
    skopeo_inspect_json,
    sort_kernel_releases,
    write_github_outputs,
)
from ci_tools.zfs_release import resolve_latest_zfs_version, resolve_linux_maximum

TAG_FROM_REF_RE = re.compile(r"^[^@]+:([^/:@]+)$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_STAMPED_TAG_RE = re.compile(r"-[0-9]{8}(\.[0-9]+)?$")
PLAIN_VERSION_LABEL_RE = re.compile(r"^(?P<fedora>[0-9]+)\.(?P<stamp>[0-9]{8}(?:\.[0-9]+)?)$")
PREFIXED_VERSION_LABEL_RE = re.compile(
    r"^(?P<prefix>[^-]+)-(?P<fedora>[0-9]+)\.(?P<stamp>[0-9]{8}(?:\.[0-9]+)?)$"
)
# A Kinoite daily tag such as `44.20261110.0`. The arch-suffixed copies
# (`44.20261110.0-x86_64`) deliberately do not match: the plain tag is the one
# the rest of this module pins and records.
DATED_BASE_TAG_RE = re.compile(r"^(?P<fedora>[0-9]+)\.(?P<day>[0-9]{8})\.(?P<respin>[0-9]+)$")
# How old, by its own date, a base a held build stands on may be. Past this the
# run fails as it did before the hold existed: a held base is all of Fedora
# frozen on that day, security updates included, not only the kernel. The owner
# set 90 days (#628), because OpenZFS can take a couple of months to release
# support for a new kernel line. Input resolution and the scheduled gate
# (`ci_tools/check_stable_signal.py`) both read this one constant, so the gate
# can never keep skipping after the real build would already fail.
#
# Kinoite labels its images `quay.expires-after=4w`: quay.io deletes a dated
# tag after four weeks. A hold longer than that stands on this repository's own
# copy of the held base (`HELD_BASE_PACKAGE`), which the main build keeps with
# `ci_tools/held_base_copy.py`.
KERNEL_HOLD_MAX_DAYS = 90
# The GHCR package, under this repository's owner, that holds the copies of
# held bases. Each copy is the same digest as the base it was copied from and
# is tagged with that base's `org.opencontainers.image.version` exactly
# (`44.20261110.0`), the tag `choose_base_image_tag` tries first.
HELD_BASE_PACKAGE = "zfs-kinoite-complex-held-base"
# Inherited from the Kinoite base, so it names the dated base an image was built on.
BASE_VERSION_LABEL = "org.opencontainers.image.version"


class HeldBaseCopiesNotPublic(CiToolError):
    """
    The held-base copy package refused an anonymous read.

    GHCR gives an anonymous caller the same bearer-token 403 for a private
    package as for one that does not exist yet, so this never means "there
    are no copies". It means "not readable without a credential".
    """


@dataclass(frozen=True)
class ResolvedBuildInputs:
    """
    Resolved values that downstream workflow jobs need for one build run.

    We keep this as a dataclass so multiple commands can share the same resolved
    values without each command re-implementing the environment and registry
    lookup flow.

    Important policy note:
    - `detected_kernel_releases` records every detected kernel directory in the base image
    - `kernel_release` is the supported primary kernel this repo builds and validates against
    - `zfs_minor_version` is the configured line (for example `2.4`); `zfs_version` is the
      exact patch resolved on that line right now (for example `2.4.3`). The cache-reuse
      check and the scheduled-build gate compare on `zfs_version`, not the line, so a new
      OpenZFS patch on an unchanged line still forces a rebuild instead of being masked by
      a cache that only matches the line.
    """

    version: str
    kernel_release: str
    detected_kernel_releases: tuple[str, ...]
    base_image_ref: str
    base_image_name: str
    base_image_tag: str
    base_image_pinned: str
    base_image_digest: str
    build_container_ref: str
    build_container_pinned: str
    build_container_digest: str
    brew_image_ref: str
    brew_image_pinned: str
    brew_image_digest: str
    zfs_minor_version: str
    zfs_version: str
    akmods_upstream_ref: str
    use_input_lock: bool
    lock_file_path: str
    # The newest base image's kernel when this run held the base back because
    # OpenZFS does not support that kernel yet; "" when it did not.
    kernel_held_from: str = ""


@dataclass(frozen=True)
class ConfiguredBuildInputs:
    """Top-level workflow inputs resolved before any registry lookups."""

    use_input_lock: bool
    lock_file_path: str
    build_container_ref: str
    base_image_ref: str
    brew_image_ref: str
    zfs_minor_version: str
    # Empty unless a lock file pinned an exact patch version. Replay mode must
    # not re-resolve this from the live OpenZFS release list, or the "replay"
    # would build a different ZFS version than the run it claims to reproduce.
    locked_zfs_version: str
    akmods_upstream_ref: str


@dataclass(frozen=True)
class BuildInputResolution:
    """
    Full resolution result, including debug-only metadata used in logs.

    `label_kernel_release` and `candidate_tags` help explain why a particular
    immutable base tag was selected, but only `inputs` needs to travel to later
    workflow steps.
    """

    inputs: ResolvedBuildInputs
    label_kernel_release: str
    candidate_tags: tuple[str, ...]


def extract_source_tag(image_ref: str) -> str:
    """Return the tag from an image ref like `name:tag`, or empty string."""
    match = TAG_FROM_REF_RE.match(image_ref)
    return match.group(1) if match else ""


def _resolve_akmods_ref_value(value: str, repo_url: str) -> str:
    if GIT_SHA_RE.match(value):
        return value
    if not repo_url:
        raise CiToolError("AKMODS_UPSTREAM_REPO is required to resolve non-SHA AKMODS_UPSTREAM_REF")
    return git_ls_remote_resolve(repo_url, value)


def choose_base_image_tag(
    *,
    source_tag: str,
    version_label: str,
    fedora_version: str,
    expected_digest: str,
    digest_lookup: Callable[[str], str],
) -> tuple[str, list[str]]:
    """
    Pick a stable base tag for this run.

    Rules:
    - If the source tag is already date-stamped, keep it.
    - Otherwise derive candidate tags from version label and choose the one
      that resolves to the expected digest.
    """
    # If we already got a date-stamped tag, treat it as stable for this run,
    # but still confirm it resolves to the expected digest so a moved tag
    # cannot slip through the early-return.
    if source_tag and DATE_STAMPED_TAG_RE.search(source_tag):
        if digest_lookup(source_tag) != expected_digest:
            raise CiToolError(
                f"Date-stamped source tag {source_tag} no longer resolves to digest "
                f"{expected_digest}"
            )
        return source_tag, [source_tag]

    candidate_tags: list[str] = []

    def append_candidate(tag: str) -> None:
        if tag and tag not in candidate_tags:
            candidate_tags.append(tag)

    prefixed_match = PREFIXED_VERSION_LABEL_RE.match(version_label)
    plain_match = PLAIN_VERSION_LABEL_RE.match(version_label)

    if prefixed_match:
        version_suffix = prefixed_match.group("stamp")
        label_prefix = prefixed_match.group("prefix")
        label_fedora_version = prefixed_match.group("fedora")
        normalized_suffix = version_suffix.split(".", 1)[0]
        append_candidate(version_label)
        append_candidate(f"{source_tag or label_prefix}-{version_suffix}")
        append_candidate(f"{fedora_version}-{version_suffix}")
        append_candidate(f"{fedora_version}-{label_fedora_version}.{version_suffix}")
        if normalized_suffix != version_suffix:
            append_candidate(f"{source_tag or label_prefix}-{label_fedora_version}.{normalized_suffix}")
            append_candidate(f"{source_tag or label_prefix}-{normalized_suffix}")
            append_candidate(f"{fedora_version}-{label_fedora_version}.{normalized_suffix}")
            append_candidate(f"{fedora_version}-{normalized_suffix}")
            append_candidate(f"{label_fedora_version}.{normalized_suffix}")
    elif plain_match:
        # Example label: 43.20260227.1
        # We only need the suffix part (20260227.1) to build candidate tags.
        version_suffix = plain_match.group("stamp")
        # ublue images commonly publish a tag equal to this label verbatim.
        append_candidate(version_label)
        append_candidate(f"{source_tag}-{version_suffix}" if source_tag else "")
        append_candidate(f"latest-{version_suffix}")
        append_candidate(f"{fedora_version}-{version_suffix}")
    else:
        raise CiToolError(
            "Failed to derive immutable base tag from "
            f"org.opencontainers.image.version={version_label}"
        )

    # Try each candidate tag and keep the first one that resolves to the same digest.
    # Digest match is the key safety check: tag text can move, digest does not.
    for candidate_tag in candidate_tags:
        candidate_digest = digest_lookup(candidate_tag)
        if candidate_digest == expected_digest:
            return candidate_tag, candidate_tags

    raise CiToolError(
        f"Failed to map digest {expected_digest} to an immutable tag. "
        f"Tried candidate tags: {' '.join(candidate_tags)}"
    )


def kernel_exceeds_maximum(kernel_release: str, linux_maximum: str) -> bool:
    """
    True when a kernel release's major.minor is newer than an OpenZFS `Linux-Maximum`.

    Either value failing to parse raises: the answer decides which base image
    the run builds on, so it is never guessed.
    """
    kernel = kernel_major_minor(kernel_release)
    maximum = kernel_major_minor(linux_maximum)
    if kernel is None:
        raise CiToolError(f"Cannot read a kernel version from {kernel_release!r}")
    if maximum is None:
        raise CiToolError(f"Cannot read a kernel version from OpenZFS Linux-Maximum {linux_maximum!r}")
    return kernel > maximum


def dated_base_version_key(version: str) -> tuple[int, date, int] | None:
    """
    Return a sortable (fedora, day, respin) key for `44.20261110.0`, or None.

    The same string is a Kinoite tag and the `org.opencontainers.image.version`
    label every image built on that base inherits.
    """
    match = DATED_BASE_TAG_RE.match(version)
    if not match:
        return None
    try:
        day = datetime.strptime(match.group("day"), "%Y%m%d").date()
    except ValueError:
        return None
    return int(match.group("fedora")), day, int(match.group("respin"))


def dated_base_tags_newest_first(
    tags: list[str], *, fedora_version: str, today: date, lookback_days: int
) -> list[str]:
    """
    Return the plain dated base tags for one Fedora release, newest first.

    Only tags dated within `lookback_days` of `today` are kept, so a hold can
    never reach for an arbitrarily old base.
    """
    earliest = today - timedelta(days=lookback_days)
    dated: list[tuple[tuple[int, date, int], str]] = []
    for tag in tags:
        key = dated_base_version_key(tag)
        if key is None or str(key[0]) != fedora_version or key[1] < earliest:
            continue
        dated.append((key, tag))
    return [tag for _, tag in sorted(dated, reverse=True)]


def _utc_today() -> date:
    """Today's date in UTC, the calendar the dated base tags are stamped in."""
    return datetime.now(UTC).date()


def list_base_image_tags(base_image_name: str) -> list[str]:
    """Return every tag in the base image repository."""
    listing = run_json_cmd(
        ["skopeo", "list-tags", f"docker://{base_image_name}"], timeout=REGISTRY_METADATA_TIMEOUT
    )
    tags = listing.get("Tags")
    if not isinstance(tags, list):
        raise CiToolError(f"Missing tag list in skopeo list-tags output for {base_image_name}")
    return [str(tag) for tag in tags]


def held_base_repository(image_org: str) -> str:
    """Return this repository's held-base copy package, `ghcr.io/<owner>/<package>`."""
    return f"ghcr.io/{image_org}/{HELD_BASE_PACKAGE}"


def list_held_base_copy_tags(repository: str) -> list[str]:
    """
    Return every tag in the held-base copy package, read anonymously.

    Anonymous like every other base read here: pull request validation calls
    this too and has no package token. A package that does not exist yet has
    no copies. A refusal raises `HeldBaseCopiesNotPublic` instead of reading
    as "no copies", and the caller decides whether it matters.
    """
    try:
        return list_base_image_tags(repository)
    except CiToolError as exc:
        message = str(exc)
        if is_missing_image_error(message):
            return []
        if is_registry_permission_error(message):
            raise HeldBaseCopiesNotPublic(
                f"{repository} refused an anonymous read, so it either does not exist yet or "
                f"is not public ({message.strip()})"
            ) from exc
        raise


def find_held_base(
    *,
    base_image_name: str,
    copy_repository: str,
    fedora_version: str,
    linux_maximum: str,
    today: date,
    list_tags: Callable[[str], list[str]] = list_base_image_tags,
    list_copy_tags: Callable[[str], list[str]] = list_held_base_copy_tags,
    inspect: Callable[[str], dict] = skopeo_inspect_json,
) -> tuple[str, str]:
    """
    Return `(repository, tag)` of the newest dated base whose kernel label OpenZFS supports.

    The candidates are the upstream repository's dated tags and this
    repository's copies of earlier held bases, both of one Fedora release and
    both no older than `KERNEL_HOLD_MAX_DAYS` by their own date. They are
    tried newest first by that date, whichever repository holds them.
    Preferring upstream would pick an older upstream tag over a newer copy,
    and promotion's `refuse_held_base_older_than_latest` would then refuse
    the candidate. A tag in both is read from upstream; it is the same image.

    A copy package that refuses an anonymous read is not "no copies". It only
    matters when no upstream tag qualifies: quay.io keeps every dated tag for
    four weeks, so every upstream tag is newer than any copy whose upstream tag
    is gone, and a qualifying upstream tag would win anyway. With none, the
    run fails and names the refusal.

    The label only chooses the tag. The caller still pins the tag's digest and
    reads the real kernel from `/lib/modules` before building on it.
    """
    window = {
        "fedora_version": fedora_version,
        "today": today,
        "lookback_days": KERNEL_HOLD_MAX_DAYS,
    }
    upstream_tags = dated_base_tags_newest_first(list_tags(base_image_name), **window)
    copies_refused: HeldBaseCopiesNotPublic | None = None
    try:
        copy_tags = dated_base_tags_newest_first(list_copy_tags(copy_repository), **window)
    except HeldBaseCopiesNotPublic as exc:
        copies_refused = exc
        copy_tags = []
    candidates = [(base_image_name, tag) for tag in upstream_tags]
    candidates += [(copy_repository, tag) for tag in copy_tags if tag not in upstream_tags]
    candidates.sort(key=lambda candidate: dated_base_version_key(candidate[1]), reverse=True)
    for repository, tag in candidates:
        labels = inspect(f"docker://{repository}:{tag}").get("Labels") or {}
        kernel_label = str(labels.get("ostree.linux") or "")
        if kernel_major_minor(kernel_label) is None:
            continue
        if not kernel_exceeds_maximum(kernel_label, linux_maximum):
            return repository, tag
    if copies_refused is not None:
        raise CiToolError(
            f"No {base_image_name} Fedora {fedora_version} base image from the last "
            f"{KERNEL_HOLD_MAX_DAYS} days has a kernel OpenZFS supports (Linux-Maximum "
            f"{linux_maximum}), and this repository's copies of held bases could not be read: "
            f"{copies_refused}. Make the {HELD_BASE_PACKAGE} package public (see \"Kernel Hold\" "
            "in docs/architecture-overview.md); until then there is no base to hold the build on."
        )
    raise CiToolError(
        f"No Fedora {fedora_version} base image from the last {KERNEL_HOLD_MAX_DAYS} days, in "
        f"{base_image_name} or in this repository's copies at {copy_repository}, has a kernel "
        f"OpenZFS supports (Linux-Maximum {linux_maximum}), so there is no base to hold the "
        "build on."
    )


def _load_lock_file(lock_file_path: str) -> dict:
    # Lock file is a plain JSON object saved from a previous run.
    lock_path = Path(lock_file_path)
    if not lock_path.exists():
        raise CiToolError(f"Replay lock file not found: {lock_file_path}")
    with lock_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def detect_base_image_kernel_releases(image_ref: str) -> list[str]:
    """
    Inspect the base image filesystem and return every installed kernel release.

    We intentionally inspect `/lib/modules` from a real container view instead
    of trusting a single metadata label, because installonly kernel packages
    can leave more than one kernel in the final merged root filesystem.

    The image is pulled explicitly first. `podman run` would pull it implicitly,
    but that implicit pull was the only registry transfer in this repository
    with nothing retrying it: `skopeo copy` passes `--retry-times`, and `podman
    pull` takes `--retry`, while `podman run` exposes no such flag. This is the
    first registry read of the build, so one truncated blob from quay.io's CDN
    ended the whole run before a single akmod was compiled, with the resolve
    step reporting `unexpected EOF` (run 34266369977). Both belts are worn: the
    `--retry` podman applies inside one invocation, and `run_cmd_with_retries`
    around the invocation itself. Each wrapper attempt gets one-third of the
    transfer timeout, so exhausting all three still leaves the 90-minute job
    time to report the failure instead of being killed during the last pull.
    """
    run_cmd_with_retries(
        [
            "podman",
            "pull",
            "--retry",
            str(REGISTRY_RETRY_ATTEMPTS),
            image_ref,
        ],
        capture_output=False,
        timeout=REGISTRY_TRANSFER_TIMEOUT / REGISTRY_RETRY_ATTEMPTS,
    )
    # Now a local image; `podman run` transfers nothing and keeps its unbounded
    # runtime, which is a `find` over /lib/modules.
    output = run_cmd(
        [
            "podman",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            image_ref,
            "-lc",
            "find /lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'",
        ]
    )
    detected_kernel_releases = sort_kernel_releases(output.splitlines())
    if not detected_kernel_releases:
        raise CiToolError(f"No installed kernel directories found in {image_ref}")
    return detected_kernel_releases


def write_resolved_build_outputs(inputs: ResolvedBuildInputs) -> None:
    """
    Export resolved build values to GitHub step outputs.

    Keeping this in one helper ensures every workflow path writes the same
    output names, which keeps downstream jobs honest across main, branch, and
    PR validation.
    """

    write_github_outputs(
        {
            "version": inputs.version,
            "kernel_release": inputs.kernel_release,
            "detected_kernel_releases": " ".join(inputs.detected_kernel_releases),
            "base_image_ref": inputs.base_image_ref,
            "base_image_name": inputs.base_image_name,
            "base_image_tag": inputs.base_image_tag,
            "base_image_pinned": inputs.base_image_pinned,
            "base_image_digest": inputs.base_image_digest,
            "build_container_ref": inputs.build_container_ref,
            "build_container_pinned": inputs.build_container_pinned,
            "build_container_digest": inputs.build_container_digest,
            "brew_image_ref": inputs.brew_image_ref,
            "brew_image_pinned": inputs.brew_image_pinned,
            "brew_image_digest": inputs.brew_image_digest,
            "zfs_minor_version": inputs.zfs_minor_version,
            "zfs_version": inputs.zfs_version,
            "akmods_upstream_ref": inputs.akmods_upstream_ref,
            "use_input_lock": "true" if inputs.use_input_lock else "false",
            "lock_file_path": inputs.lock_file_path,
            "kernel_held_from": inputs.kernel_held_from,
        }
    )


def _resolve_default_akmods_ref() -> str:
    """
    Pick the akmods source commit using a cascade that prefers explicit overrides.

    Order:
    1. `DEFAULT_AKMODS_REF` env (process-level override)
    2. `AKMODS_UPSTREAM_REF` env (explicit pin from caller)
    3. `AKMODS_UPSTREAM_REF` in `ci/defaults.json` (non-empty → explicit pin)
    4. `AKMODS_UPSTREAM_TRACK` (env or defaults) resolved via `git ls-remote` against `AKMODS_UPSTREAM_REPO`

    The point of (4) is self-healing: when no explicit pin is set, the build floats
    to whatever commit the tracking ref currently points at, so a red build caused
    by a transient upstream mismatch clears itself once upstream catches up.
    """
    defaults = load_repo_defaults()
    repo_url = optional_env("AKMODS_UPSTREAM_REPO") or defaults.get("AKMODS_UPSTREAM_REPO", "").strip()
    explicit = optional_env("DEFAULT_AKMODS_REF") or optional_env("AKMODS_UPSTREAM_REF")
    if explicit:
        return _resolve_akmods_ref_value(explicit, repo_url)

    pinned = defaults.get("AKMODS_UPSTREAM_REF", "").strip()
    if pinned:
        return _resolve_akmods_ref_value(pinned, repo_url)

    track = optional_env("AKMODS_UPSTREAM_TRACK") or defaults.get("AKMODS_UPSTREAM_TRACK", "").strip()
    if not track:
        raise CiToolError(
            "No akmods source commit is configured. Set AKMODS_UPSTREAM_REF (env or "
            "ci/defaults.json) or AKMODS_UPSTREAM_TRACK for floating resolution."
        )

    if not repo_url:
        raise CiToolError("AKMODS_UPSTREAM_REPO is required to resolve AKMODS_UPSTREAM_TRACK")

    return git_ls_remote_resolve(repo_url, track)


def resolve_configured_inputs() -> ConfiguredBuildInputs:
    """Resolve top-level workflow inputs before any registry lookups happen."""

    use_input_lock = optional_env("USE_INPUT_LOCK", "false").lower() == "true"
    lock_file_path = require_env("LOCK_FILE")
    build_container_ref = require_env("BUILD_CONTAINER_REF")
    default_akmods_ref = _resolve_default_akmods_ref()

    if use_input_lock:
        lock_data = _load_lock_file(lock_file_path)
        base_image_ref = str(lock_data.get("base_image") or "")
        lock_build_container_ref = str(lock_data.get("build_container") or "")
        # Optional in the lock file: empty falls back to the checked-in default,
        # like zfs_version. A replay that wants to reproduce the original brew
        # payload must set this to the pinned ref from the run's build-inputs
        # artifact.
        brew_image_ref = str(lock_data.get("brew_image") or "")
        zfs_minor_version = str(lock_data.get("zfs_minor_version") or "")
        locked_zfs_version = str(lock_data.get("zfs_version") or "")
        akmods_upstream_ref = str(lock_data.get("akmods_upstream_ref") or "")

        if not base_image_ref:
            raise CiToolError("Lock file missing required field: base_image")
        if "REPLACE_ME" in base_image_ref:
            raise CiToolError("Lock file base_image still contains placeholder value")
        if lock_build_container_ref and "REPLACE_ME" in lock_build_container_ref:
            raise CiToolError("Lock file build_container still contains placeholder value")
        if brew_image_ref and "REPLACE_ME" in brew_image_ref:
            raise CiToolError("Lock file brew_image still contains placeholder value")
        if lock_build_container_ref and build_container_ref != lock_build_container_ref:
            raise CiToolError(
                "Replay mismatch: build container "
                f"({build_container_ref}) does not match lock file "
                f"({lock_build_container_ref}). The build container is no longer "
                "settable per run -- the workflow_dispatch override was removed "
                "because it selected the image for a privileged job. To replay "
                "this lock file, set DEFAULT_BUILD_CONTAINER_IMAGE in "
                f"ci/defaults.json to {lock_build_container_ref} (and the matching "
                "literal in each workflow's `container:` block) via a reviewed pull "
                "request, or update the lock file to the current build container."
            )

        if not brew_image_ref:
            brew_image_ref = require_env_or_default("DEFAULT_BREW_IMAGE")
        if not zfs_minor_version:
            zfs_minor_version = require_env_or_default("DEFAULT_ZFS_MINOR_VERSION")
        if not akmods_upstream_ref:
            akmods_upstream_ref = default_akmods_ref
    else:
        base_image_ref = require_env_or_default("DEFAULT_BASE_IMAGE")
        brew_image_ref = require_env_or_default("DEFAULT_BREW_IMAGE")
        zfs_minor_version = require_env_or_default("DEFAULT_ZFS_MINOR_VERSION")
        locked_zfs_version = ""
        akmods_upstream_ref = default_akmods_ref

    return ConfiguredBuildInputs(
        use_input_lock=use_input_lock,
        lock_file_path=lock_file_path,
        build_container_ref=build_container_ref,
        base_image_ref=base_image_ref,
        brew_image_ref=brew_image_ref,
        zfs_minor_version=zfs_minor_version,
        locked_zfs_version=locked_zfs_version,
        akmods_upstream_ref=akmods_upstream_ref,
    )


def resolve_build_inputs() -> BuildInputResolution:
    """
    Resolve one complete set of build inputs from env and registry state.

    This is the core logic behind the main workflow and the non-main validation
    workflows. Sharing it here means every path pins the same base image, build
    container, Fedora version, and supported primary kernel.
    """

    configured = resolve_configured_inputs()
    use_input_lock = configured.use_input_lock
    lock_file_path = configured.lock_file_path
    build_container_ref = configured.build_container_ref
    base_image_ref = configured.base_image_ref
    zfs_minor_version = configured.zfs_minor_version
    # A lock file replay must reuse the exact patch version the locked run
    # built. Re-resolving it live would make replay non-reproducible: the cache
    # check requires an exact match, so a replay of an older run would reject
    # the cache, rebuild, and ship a newer ZFS than the run being reproduced.
    zfs_version = configured.locked_zfs_version or resolve_latest_zfs_version(zfs_minor_version)
    akmods_upstream_ref = configured.akmods_upstream_ref

    # Read base image metadata from registry.
    # Labels carry kernel information and stream version information.
    base_inspect_json = skopeo_inspect_json(f"docker://{base_image_ref}")
    base_image_name = str(base_inspect_json.get("Name") or "")
    base_image_digest = str(base_inspect_json.get("Digest") or "")
    labels = base_inspect_json.get("Labels") or {}
    label_kernel_release = str(labels.get("ostree.linux") or "")
    base_image_version_label = str(labels.get(BASE_VERSION_LABEL) or "")

    if not base_image_name or not base_image_digest:
        raise CiToolError(f"Failed to resolve base image digest for {base_image_ref}")
    if not label_kernel_release:
        raise CiToolError(f"Failed to read ostree.linux label from {base_image_ref}")

    # Hold the base on the newest image whose kernel this OpenZFS release
    # supports, instead of building on one it cannot (the ZFS build would fail
    # there anyway). A lock-file replay is never held: it must rebuild exactly
    # the base it names. The held base may come from this repository's own
    # copy package; `base_image_name` then names that package, so everything
    # below pins, pulls and tags the copy.
    kernel_held_from = ""
    copy_repository = ""
    linux_maximum = "" if use_input_lock else resolve_linux_maximum(zfs_version)
    if not use_input_lock and kernel_exceeds_maximum(label_kernel_release, linux_maximum):
        kernel_held_from = label_kernel_release
        image_org = normalize_owner(require_env("GITHUB_REPOSITORY_OWNER"))
        copy_repository = held_base_repository(image_org)
        held_repository, held_tag = find_held_base(
            base_image_name=base_image_name,
            copy_repository=copy_repository,
            fedora_version=extract_fedora_version(label_kernel_release),
            linux_maximum=linux_maximum,
            today=_utc_today(),
            list_tags=list_base_image_tags,
            list_copy_tags=list_held_base_copy_tags,
            inspect=skopeo_inspect_json,
        )
        base_image_name = held_repository
        held_inspect_json = skopeo_inspect_json(f"docker://{base_image_name}:{held_tag}")
        base_image_digest = str(held_inspect_json.get("Digest") or "")
        held_labels = held_inspect_json.get("Labels") or {}
        label_kernel_release = str(held_labels.get("ostree.linux") or "")
        base_image_version_label = str(held_labels.get(BASE_VERSION_LABEL) or "")
        if not base_image_digest:
            raise CiToolError(f"Failed to resolve base image digest for {base_image_name}:{held_tag}")

    base_image_pinned = f"{base_image_name}@{base_image_digest}"
    detected_kernel_releases = detect_base_image_kernel_releases(base_image_pinned)
    kernel_release = detected_kernel_releases[-1]
    fedora_version = extract_fedora_version(kernel_release)
    source_tag = extract_source_tag(base_image_ref)

    # Helper function:
    # input tag -> lookup digest in registry.
    # Return empty string when an upstream lookup fails so tag selection can
    # continue. A copy is tagged with its version label exactly, the first
    # candidate, so an error reading the copy is the answer rather than a
    # miss: report it, instead of the vague "Failed to map digest" it would
    # otherwise end in.
    def lookup_digest(candidate_tag: str) -> str:
        candidate_ref = f"docker://{base_image_name}:{candidate_tag}"
        try:
            return skopeo_inspect_digest(candidate_ref)
        except CiToolError as exc:
            if base_image_name != copy_repository:
                return ""
            reason = (
                f"the package is not public; make the {HELD_BASE_PACKAGE} package public"
                if is_registry_permission_error(str(exc))
                else "the registry read failed"
            )
            raise CiToolError(
                f"Could not read this repository's held base copy {candidate_ref}: {reason} ({exc})"
            ) from exc

    base_image_tag, candidate_tags = choose_base_image_tag(
        source_tag=source_tag,
        version_label=base_image_version_label,
        fedora_version=fedora_version,
        expected_digest=base_image_digest,
        digest_lookup=lookup_digest,
    )

    build_container_inspect = skopeo_inspect_json(f"docker://{build_container_ref}")
    build_container_name = str(build_container_inspect.get("Name") or "")
    build_container_digest = str(build_container_inspect.get("Digest") or "")

    if not build_container_name or not build_container_digest:
        raise CiToolError(f"Failed to resolve build container digest for {build_container_ref}")

    build_container_pinned = f"{build_container_name}@{build_container_digest}"

    # The brew payload is copied wholesale into the final image's root and its
    # services are enabled at build time, so it is a real supply-chain input.
    # Pin it to a digest here like the base image, so the build consumes an
    # exact payload and the run records which one.
    brew_image_ref = configured.brew_image_ref
    brew_inspect = skopeo_inspect_json(f"docker://{brew_image_ref}")
    brew_image_name = str(brew_inspect.get("Name") or "")
    brew_image_digest = str(brew_inspect.get("Digest") or "")

    if not brew_image_name or not brew_image_digest:
        raise CiToolError(f"Failed to resolve brew image digest for {brew_image_ref}")

    brew_image_pinned = f"{brew_image_name}@{brew_image_digest}"

    return BuildInputResolution(
        inputs=ResolvedBuildInputs(
            version=fedora_version,
            kernel_release=kernel_release,
            detected_kernel_releases=tuple(detected_kernel_releases),
            base_image_ref=base_image_ref,
            base_image_name=base_image_name,
            base_image_tag=base_image_tag,
            base_image_pinned=base_image_pinned,
            base_image_digest=base_image_digest,
            build_container_ref=build_container_ref,
            build_container_pinned=build_container_pinned,
            build_container_digest=build_container_digest,
            brew_image_ref=brew_image_ref,
            brew_image_pinned=brew_image_pinned,
            brew_image_digest=brew_image_digest,
            zfs_minor_version=zfs_minor_version,
            zfs_version=zfs_version,
            akmods_upstream_ref=akmods_upstream_ref,
            use_input_lock=use_input_lock,
            lock_file_path=lock_file_path,
            kernel_held_from=kernel_held_from,
        ),
        label_kernel_release=label_kernel_release,
        candidate_tags=tuple(candidate_tags),
    )


def main() -> None:
    resolution = resolve_build_inputs()
    inputs = resolution.inputs

    # Export resolved values for downstream workflow steps.
    write_resolved_build_outputs(inputs)

    print(f"Resolved base image: {inputs.base_image_pinned}")
    print(f"Resolved base image tag: {inputs.base_image_name}:{inputs.base_image_tag}")
    print(f"Resolved build container: {inputs.build_container_pinned}")
    print(f"Resolved brew image: {inputs.brew_image_pinned}")
    if resolution.label_kernel_release != inputs.kernel_release:
        print(
            "Base image label/kernel directory mismatch: "
            f"label={resolution.label_kernel_release} newest_dir={inputs.kernel_release}"
        )
    print(f"Supported primary kernel release: {inputs.kernel_release}")
    print(
        "Detected kernel releases in base image: "
        f"{' '.join(inputs.detected_kernel_releases)}"
    )
    print(f"Fedora version: {inputs.version}")
    print(f"ZFS minor version: {inputs.zfs_minor_version}")
    print(f"Resolved ZFS version: {inputs.zfs_version}")
    if inputs.kernel_held_from:
        print(
            f"Kernel held: the newest base image has kernel {inputs.kernel_held_from}, which "
            f"OpenZFS {inputs.zfs_version} does not support yet; building on "
            f"{inputs.base_image_name}:{inputs.base_image_tag} ({inputs.kernel_release}) instead."
        )

    # Helpful for debugging: shows exactly which tags were considered.
    if resolution.candidate_tags:
        print(f"Base-tag candidates checked: {' '.join(resolution.candidate_tags)}")


if __name__ == "__main__":
    main()
