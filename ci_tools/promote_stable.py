"""
Script: ci_tools/promote_stable.py
What: Promotes the tested candidate tag to stable tags in the same image repository.
Doing: Refuses when a newer run already promoted, when a held candidate is on an older base,
or when the candidate is on an older OpenZFS line than `latest`, then copies the candidate
digest to one immutable audit tag, then to `latest`.
Why: Candidate-first promotion keeps broken builds from advancing the normal user-facing tag.
Goal: Update stable tags without rebuilding the image a second time.
"""

from __future__ import annotations

import os

from ci_tools.check_stable_signal import (
    BASE_VERSION_LABEL,
    KERNEL_HELD_FROM_LABEL,
    ZFS_VERSION_LABEL,
)
from ci_tools.common import (
    COSIGN_TIMEOUT,
    REGISTRY_METADATA_TIMEOUT,
    REPO_ROOT,
    CiToolError,
    normalize_owner,
    optional_env,
    registry_auth_dir,
    registry_auth_file,
    registry_creds_from_env,
    require_env,
    require_env_or_default,
    run_cmd,
    run_json_cmd,
    skopeo_copy,
    skopeo_inspect_digest,
    skopeo_inspect_json,
    skopeo_inspect_json_optional,
)
from ci_tools.prune_registry import STABLE_RE
from ci_tools.resolve_build_inputs import dated_base_version_key
from ci_tools.tagging_context import build_candidate_tag
from ci_tools.zfs_release import zfs_line_key, zfs_line_of


def list_repository_tags(repository_ref: str, *, creds: str | None) -> list[str]:
    """
    Return every tag in one image repository (`docker://host/org/name`).

    `creds` reaches `skopeo` as an `--authfile`, never as `--creds`; see
    `registry_auth_dir` for why.
    """
    with registry_auth_dir(creds, repository_ref) as auth_dir:
        command = ["skopeo", "list-tags"]
        if auth_dir:
            command.extend(["--authfile", registry_auth_file(auth_dir)])
        command.append(repository_ref)
        listing = run_json_cmd(command, timeout=REGISTRY_METADATA_TIMEOUT)
    tags = listing.get("Tags")
    if not isinstance(tags, list):
        raise CiToolError(f"Missing tag list in skopeo list-tags output for {repository_ref}")
    return [str(tag) for tag in tags]


def refuse_older_than_published(*, run_number: str, tags: list[str]) -> None:
    """
    Refuse to promote when a newer run has already promoted.

    Every promotion writes `stable-<run>-<sha7>` before it moves `latest`, so
    the highest run number among those tags is the newest build that reached
    users. Re-running an old, finished run keeps that run's
    `GITHUB_RUN_NUMBER`, and the in-progress `concurrency` cancel in
    `build.yml` does not stop it. Without this check such a re-run would move
    `latest` back to an older image, and an older image may be unable to
    import pools whose features a newer one enabled (`docs/SECURITY-AI.md`).

    An equal run number is allowed: that is a re-run of the same build, which
    resolves the same candidate. A deliberate rollback is a new dispatch,
    which gets a new, higher run number.

    `GITHUB_RUN_NUMBER` counts per workflow. If promotion ever moves to a
    workflow whose count is lower than the existing `stable-*` tags, this check
    refuses every promotion until those tags are removed. That is the
    fail-closed direction.
    """
    if not run_number.isdigit():
        raise CiToolError(f"GITHUB_RUN_NUMBER is not a run number: {run_number!r}")
    published = [int(tag.split("-")[1]) for tag in tags if STABLE_RE.match(tag)]
    if published and max(published) > int(run_number):
        raise CiToolError(
            f"Refusing to promote run {run_number}: run {max(published)} has already "
            "promoted a newer image to latest. Re-running an older build's promotion "
            "would move latest backwards. To roll back on purpose, start a new run."
        )


def refuse_held_base_older_than_latest(*, candidate_labels: dict, latest_labels: dict | None) -> None:
    """
    Refuse to promote a held candidate built on an older base than `:latest`.

    A held build sits on the newest Kinoite base whose kernel OpenZFS supports
    (`resolve_build_inputs.find_held_base`). If `:latest` is already on a
    newer base, promoting the held candidate would move users back to an older
    Fedora, kernel included. Candidates that were not held are left to the
    existing checks, so a deliberate rollback replayed from a lock file still
    promotes as before.

    Both images inherit `org.opencontainers.image.version` (`44.20261110.0`)
    from their base. A held candidate whose base version, or whose `:latest`
    base version, cannot be read is refused: that is the fail-closed direction.
    """
    if not str(candidate_labels.get(KERNEL_HELD_FROM_LABEL) or ""):
        return
    if latest_labels is None:
        return
    candidate_version = str(candidate_labels.get(BASE_VERSION_LABEL) or "")
    latest_version = str(latest_labels.get(BASE_VERSION_LABEL) or "")
    candidate_key = dated_base_version_key(candidate_version)
    latest_key = dated_base_version_key(latest_version)
    if candidate_key is None or latest_key is None:
        raise CiToolError(
            "Refusing to promote a held candidate: cannot compare its base version "
            f"{candidate_version!r} with the base version {latest_version!r} of latest."
        )
    if candidate_key < latest_key:
        raise CiToolError(
            f"Refusing to promote a held candidate on base {candidate_version}: latest is "
            f"already on the newer base {latest_version}. Promoting it would move users "
            "back to an older Fedora and kernel."
        )


def read_promotion_labels(
    *, candidate_ref: str, latest_ref: str, creds: str
) -> tuple[dict, dict | None]:
    """
    Return the candidate's labels and `latest`'s, or None for `latest` when it does not exist.

    Any other registry failure raises, so a check below never reads "could not tell" as
    "there is no `latest`".
    """
    candidate_labels = skopeo_inspect_json(candidate_ref, creds=creds).get("Labels") or {}
    latest_inspect = skopeo_inspect_json_optional(latest_ref, creds=creds)
    latest_labels = None if latest_inspect is None else latest_inspect.get("Labels") or {}
    return candidate_labels, latest_labels


def refuse_zfs_line_older_than_latest(
    *, candidate_labels: dict, latest_labels: dict | None, configured_line: str
) -> None:
    """
    Refuse to promote a candidate on an older OpenZFS line than `latest` runs.

    Input resolution never selects a line older than `latest`'s, but a
    lock-file replay keeps its locked version, and a manual dispatch promotes
    by default. Without this a replay of an old 2.4 lock would move users back
    from 2.5, and an older OpenZFS cannot import pools whose newer features
    were enabled.

    - No `latest`: promote.
    - `latest` without a readable `zfs-version` label (no image this pipeline
      builds lacks one): promote only when the candidate's line is not older
      than the configured line, and say so in the run log.
    - Otherwise refuse a candidate on an older line than `latest`'s.

    A candidate without a readable `zfs-version` label is refused: this
    pipeline always writes it, so its absence means the candidate is not one
    of this pipeline's images.
    """
    candidate_version = str(candidate_labels.get(ZFS_VERSION_LABEL) or "")
    candidate_line = zfs_line_of(candidate_version)
    if not candidate_line:
        raise CiToolError(
            f"Refusing to promote: the candidate's {ZFS_VERSION_LABEL} label "
            f"{candidate_version!r} is not an OpenZFS release, so its OpenZFS line "
            "cannot be compared with latest's."
        )
    if latest_labels is None:
        return
    latest_version = str(latest_labels.get(ZFS_VERSION_LABEL) or "")
    latest_line = zfs_line_of(latest_version)
    if not latest_line:
        configured_key = zfs_line_key(configured_line)
        if configured_key is None:
            raise CiToolError(f"Invalid ZFS minor version: {configured_line!r}")
        if zfs_line_key(candidate_line) < configured_key:
            raise CiToolError(
                f"Refusing to promote OpenZFS {candidate_version}: latest has no readable "
                f"{ZFS_VERSION_LABEL} label ({latest_version!r}), and the candidate's line "
                f"{candidate_line} is older than the configured line {configured_line}."
            )
        print(
            f"latest has no readable {ZFS_VERSION_LABEL} label ({latest_version!r}); "
            f"promoting OpenZFS {candidate_version} because its line {candidate_line} is not "
            f"older than the configured line {configured_line}."
        )
        return
    if zfs_line_key(candidate_line) < zfs_line_key(latest_line):
        raise CiToolError(
            f"Refusing to promote OpenZFS {candidate_version}: latest already runs OpenZFS "
            f"{latest_version}, on the newer {latest_line} line. Promoting it would move users "
            "back to an older OpenZFS line, which may not import pools whose newer features "
            "were enabled."
        )


def report_zfs_line_move(*, candidate_labels: dict, latest_labels: dict | None) -> None:
    """
    Say in the run log and the step summary when this promotion moved to a newer OpenZFS line.

    The informational issue is opened by the triage workflow; this is the same
    notice in the run itself. Nothing here changes a pool.
    """
    if latest_labels is None:
        return
    candidate_version = str(candidate_labels.get(ZFS_VERSION_LABEL) or "")
    latest_version = str(latest_labels.get(ZFS_VERSION_LABEL) or "")
    candidate_line = zfs_line_of(candidate_version)
    latest_line = zfs_line_of(latest_version)
    if not candidate_line or not latest_line:
        return
    if zfs_line_key(candidate_line) <= zfs_line_key(latest_line):
        return
    notice = "\n".join(
        [
            f"## OpenZFS moved from {latest_line} to {candidate_line}",
            "",
            f"`latest` now runs OpenZFS {candidate_version} (it ran {latest_version}).",
            "",
            "- Existing pools are untouched.",
            "- Do not run `zpool upgrade` while you want to be able to roll back.",
            "- A pool created from now on gets the new line's features and will not import "
            "on the previous image.",
            "",
        ]
    )
    print(notice)
    summary_path = optional_env("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(notice + "\n")


def verify_candidate_signature(*, image_org: str, image_name: str, candidate_digest: str) -> None:
    """
    Re-verify the candidate signature in this job before promoting it.

    The candidate was already signed and verified when it was published, but
    that happened in a different job on a different runner. Promotion is the
    step that actually points `latest` at a digest, so it should be able to
    prove locally -- not infer from an earlier job -- that the digest it is
    about to promote carries a valid signature from this repo's committed key.

    Verification uses the same committed `cosign.pub` that is baked into the
    image and that booted systems enforce, so a key mismatch fails here rather
    than on a user's machine at `bootc upgrade` time.
    """

    verification_key = os.environ.get("COSIGN_PUBLIC_KEY_PATH", "").strip() or str(
        REPO_ROOT / "cosign.pub"
    )
    if not os.path.exists(verification_key):
        raise CiToolError(f"Missing required verification key file: {verification_key}")

    digest_ref = f"ghcr.io/{image_org}/{image_name}@{candidate_digest}"
    run_cmd(
        [
            "cosign",
            "verify",
            "--new-bundle-format=false",
            "--key",
            verification_key,
            digest_ref,
        ],
        timeout=COSIGN_TIMEOUT,
    )
    print(f"Verified candidate signature before promotion: {digest_ref}")


def _copy_and_verify_digest(*, source_digest: str, source_ref: str, destination_ref: str, creds: str) -> None:
    """
    Copy `source_ref` to `destination_ref`, then confirm the copy landed at the
    exact source digest.

    `--preserve-digests` and `--multi-arch=all` should already keep the digest
    unchanged, but this check keeps the promotion fail-closed if skopeo's
    conversion behavior ever changes: `latest` must never silently point at
    something other than what was signed.
    """
    skopeo_copy(source_ref, destination_ref, creds=creds, preserve_digests=True, multi_arch="all")
    destination_digest = skopeo_inspect_digest(destination_ref, creds=creds)
    if destination_digest != source_digest:
        raise CiToolError(
            f"Promoted digest mismatch: copying {source_ref} to {destination_ref} "
            f"produced {destination_digest}, expected {source_digest}"
        )


def main() -> None:
    # Inputs from workflow context and job env.
    image_org = normalize_owner(require_env("GITHUB_REPOSITORY_OWNER"))
    creds = registry_creds_from_env(required=True)
    fedora_version = require_env("FEDORA_VERSION")
    image_name = require_env("IMAGE_NAME")
    run_number = require_env("GITHUB_RUN_NUMBER")
    github_sha = require_env("GITHUB_SHA")
    sha_short = github_sha[:7]

    # Candidate tags are built and pushed earlier in the workflow under the same
    # repository path. Promotion only moves stable-facing tags after that build passes.
    candidate_tag = build_candidate_tag(github_sha=github_sha, fedora_version=fedora_version)
    candidate_by_tag = f"docker://ghcr.io/{image_org}/{image_name}:{candidate_tag}"
    candidate_digest = skopeo_inspect_digest(candidate_by_tag, creds=creds)
    candidate_ref = f"docker://ghcr.io/{image_org}/{image_name}@{candidate_digest}"

    # Fail closed before any tag moves: never promote a digest we cannot prove
    # is signed by this repo's committed key.
    verify_candidate_signature(
        image_org=image_org, image_name=image_name, candidate_digest=candidate_digest
    )

    # Fail closed before any tag moves: never move `latest` backwards to an
    # image older than the one a newer run already promoted.
    refuse_older_than_published(
        run_number=run_number,
        tags=list_repository_tags(f"docker://ghcr.io/{image_org}/{image_name}", creds=creds),
    )

    stable_ref = f"docker://ghcr.io/{image_org}/{image_name}:latest"
    candidate_labels, latest_labels = read_promotion_labels(
        candidate_ref=candidate_ref, latest_ref=stable_ref, creds=creds
    )

    # Fail closed before any tag moves: a held candidate never takes `latest`
    # back to an older base.
    refuse_held_base_older_than_latest(
        candidate_labels=candidate_labels, latest_labels=latest_labels
    )

    # Fail closed before any tag moves: no candidate takes `latest` back to an
    # older OpenZFS line, a lock-file replay included.
    refuse_zfs_line_older_than_latest(
        candidate_labels=candidate_labels,
        latest_labels=latest_labels,
        configured_line=require_env_or_default("DEFAULT_ZFS_MINOR_VERSION"),
    )
    audit_ref = f"docker://ghcr.io/{image_org}/{image_name}:stable-{run_number}-{sha_short}"

    # Publish the immutable audit tag before moving the user-facing `latest`
    # tag. `build.yml` cancels in-progress runs on a newer push, so if this job
    # is cancelled between the two copies, it is safer to have an audit record
    # with no `latest` move yet than a moved `latest` with no audit record.
    _copy_and_verify_digest(
        source_digest=candidate_digest, source_ref=candidate_ref, destination_ref=audit_ref, creds=creds
    )
    _copy_and_verify_digest(
        source_digest=candidate_digest, source_ref=candidate_ref, destination_ref=stable_ref, creds=creds
    )

    report_zfs_line_move(candidate_labels=candidate_labels, latest_labels=latest_labels)

    print(f"Resolved candidate source {candidate_by_tag} -> {candidate_ref}")
    print(f"Published audit tag {audit_ref}")
    print(f"Promoted candidate image {candidate_ref} -> stable {stable_ref}")


if __name__ == "__main__":
    main()
