"""
Script: ci_tools/held_base_copy.py
What: Keeps this repository's own copy of the Fedora base a held build stands on.
Doing: Copies the held base by digest into the `zfs-kinoite-complex-held-base` GHCR package,
tagged with its version label, checks the copy's digest, and reports whether a copy exists and
whether anyone can read it without a credential.
Why: quay.io deletes a dated Kinoite tag after four weeks (`quay.expires-after=4w`), while a
kernel hold may last up to `KERNEL_HOLD_MAX_DAYS`. Without a copy the held base disappears while
the hold still needs it, and every build after that fails.
Goal: A held build always has a base it can pull for the whole hold window, and the owner is
told, in the build log and in the "Kernel held" issue, while the copy is not public yet.

Three states, never two. Anonymous GHCR reads cannot tell a missing package from a private one:
both get a bearer-token 403. So whether a copy exists is always asked with the job's token, and
the anonymous read is asked only after that, to learn whether it is public.
"""

from __future__ import annotations

import json
from pathlib import Path

from ci_tools.common import (
    CiToolError,
    is_registry_permission_error,
    normalize_owner,
    optional_env,
    registry_creds_from_env,
    require_env,
    skopeo_copy,
    skopeo_inspect_digest,
    skopeo_inspect_json,
    skopeo_inspect_json_optional,
    write_github_outputs,
)
from ci_tools.resolve_build_inputs import (
    BASE_VERSION_LABEL,
    HELD_BASE_PACKAGE,
    dated_base_version_key,
    held_base_repository,
)

COPY_PUBLIC = "public"
COPY_NOT_PUBLIC = "not-public"
COPY_MISSING = "missing"

# What the owner does once, after the first held main build creates the package. A new GHCR
# package is private, and resolution reads copies anonymously, like every base.
MAKE_PUBLIC_STEPS = (
    f"On GitHub, open your profile, then Packages, then {HELD_BASE_PACKAGE}, then Package "
    "settings, then Change visibility, and choose Public."
)


def held_base_copy_ref(image_org: str, base_image_pinned: str) -> str:
    """
    Return the copy's `docker://` ref by digest for a pinned base ref, or "" when it has none.

    `base_image_pinned` is what the `org.zfs-kinoite-complex.base-image` label records,
    `quay.io/fedora-ostree-desktops/kinoite@sha256:...`. The copy keeps that digest.
    """
    _, separator, digest = base_image_pinned.rpartition("@")
    if not separator or not digest.startswith("sha256:"):
        return ""
    return f"docker://{held_base_repository(image_org)}@{digest}"


def held_base_copy_state(copy_ref: str, *, creds: str) -> str:
    """
    Return `COPY_MISSING`, `COPY_NOT_PUBLIC` or `COPY_PUBLIC` for one copy ref.

    Existence is asked with `creds`; any registry error other than a real absence raises, so
    "could not tell" is never read as "missing". Only then is an anonymous read tried.
    """
    if skopeo_inspect_json_optional(copy_ref, creds=creds) is None:
        return COPY_MISSING
    try:
        anonymous = skopeo_inspect_json_optional(copy_ref)
    except CiToolError as exc:
        if is_registry_permission_error(str(exc)):
            return COPY_NOT_PUBLIC
        raise
    return COPY_NOT_PUBLIC if anonymous is None else COPY_PUBLIC


def report_copy_state(copy_ref: str, state: str) -> None:
    """Print the state to the job log; a copy that is not public is a workflow warning."""
    if state == COPY_NOT_PUBLIC:
        print(
            f"::warning title=Held base copy is not public::{copy_ref} exists but cannot be read "
            "without a credential, so a held build cannot use it once Fedora's registry deletes "
            f"the original. {MAKE_PUBLIC_STEPS}"
        )
        return
    print(f"Held base copy {copy_ref}: {state}")


def copy_held_base(
    *, base_image_name: str, base_image_digest: str, image_org: str, creds: str
) -> str:
    """
    Make sure this repository's copy of one held base exists, and return its ref.

    The copy is made with the same options promotion uses (`--preserve-digests`,
    `--multi-arch=all`), because a Kinoite base is a multi-arch index and the copy must be the
    same digest. It is tagged with the base's version label exactly, the tag resolution looks
    up. A tag that already holds a different digest, or a copy that landed at a different
    digest, fails the run: nothing here overwrites or guesses.
    """
    repository = held_base_repository(image_org)
    if base_image_name == repository:
        # Resolution already built this run on the copy, so it exists; say whether it is public.
        copy_ref = f"docker://{repository}@{base_image_digest}"
        state = held_base_copy_state(copy_ref, creds=creds)
        if state == COPY_MISSING:
            raise CiToolError(
                f"This run was resolved on {copy_ref}, which the registry says does not exist."
            )
        report_copy_state(copy_ref, state)
        return copy_ref

    source_ref = f"docker://{base_image_name}@{base_image_digest}"
    labels = skopeo_inspect_json(source_ref).get("Labels") or {}
    version = str(labels.get(BASE_VERSION_LABEL) or "")
    if dated_base_version_key(version) is None:
        raise CiToolError(
            f"Cannot keep a copy of {source_ref}: its {BASE_VERSION_LABEL} label {version!r} is "
            "not a dated base version such as 44.20261110.0, and resolution finds copies by "
            "that tag."
        )

    copy_ref = f"docker://{repository}:{version}"
    existing = skopeo_inspect_json_optional(copy_ref, creds=creds)
    if existing is None:
        skopeo_copy(
            source_ref,
            copy_ref,
            creds=creds,
            preserve_digests=True,
            multi_arch="all",
            authenticate_source=False,
        )
        copied_digest = skopeo_inspect_digest(copy_ref, creds=creds)
        print(f"Copied held base {source_ref} -> {copy_ref}")
    else:
        copied_digest = str(existing.get("Digest") or "")
    if copied_digest != base_image_digest:
        raise CiToolError(
            f"Held base copy digest mismatch: {copy_ref} resolves to "
            f"{copied_digest or 'no digest'}, expected {base_image_digest} from {source_ref}."
        )
    report_copy_state(copy_ref, held_base_copy_state(copy_ref, creds=creds))
    return copy_ref


def main() -> None:
    """`copy-held-base`: run by the main build after input resolution, only when it held."""
    if not optional_env("KERNEL_HELD_FROM"):
        print("This run is not held; there is no held base to copy.")
        return
    copy_held_base(
        base_image_name=require_env("BASE_IMAGE_NAME"),
        base_image_digest=require_env("BASE_IMAGE_DIGEST"),
        image_org=normalize_owner(require_env("GITHUB_REPOSITORY_OWNER")),
        creds=registry_creds_from_env(required=True),
    )


def main_status() -> None:
    """
    `held-base-copy-status`: write the copy's state for the "Kernel held" issue as `state`.

    Reads the build-inputs manifest the triage workflow downloaded. `state` is "" when there is
    no manifest or the build was not held, so the issue step adds nothing.
    """
    manifest_path = Path(require_env("BUILD_INPUTS_MANIFEST"))
    image_org = normalize_owner(require_env("GITHUB_REPOSITORY_OWNER"))
    state = ""
    if manifest_path.is_file():
        inputs = json.loads(manifest_path.read_text(encoding="utf-8")).get("inputs") or {}
        if inputs.get("kernel_held_from") and inputs.get("use_input_lock") is not True:
            digest = str(inputs.get("base_image_digest") or "")
            copy_ref = held_base_copy_ref(image_org, f"{inputs.get('base_image_name')}@{digest}")
            if not copy_ref:
                raise CiToolError(f"The build inputs manifest names no base digest: {digest!r}")
            state = held_base_copy_state(copy_ref, creds=registry_creds_from_env(required=True))
            report_copy_state(copy_ref, state)
    write_github_outputs({"state": state})


if __name__ == "__main__":
    main()
