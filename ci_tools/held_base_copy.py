"""
Script: ci_tools/held_base_copy.py
What: Keeps this repository's own copy of the Fedora base a held build stands on.
Doing: Copies the held base by digest into the private `zfs-kinoite-complex-held-base` GHCR
package, tagged with its version label, checks the copy's digest, and answers one question for
everyone who needs it: can the job's token read this copy?
Why: quay.io deletes a dated Kinoite tag after four weeks (`quay.expires-after=4w`), while a
kernel hold may last up to `KERNEL_HOLD_MAX_DAYS`. Without a copy the held base disappears while
the hold still needs it, and every build after that fails.
Goal: A held build always has a base it can pull for the whole hold window, with no manual step:
the package stays private and every read of it uses the job's own token.

One existence function, `held_base_copy_exists`, serves the scheduled gate and the main build's
post-copy check, each with its job's token. They cannot disagree about whether a copy exists,
so a copy that one of them cannot see fails the run instead of rebuilding every night.
"""

from __future__ import annotations

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
)
from ci_tools.resolve_build_inputs import (
    BASE_VERSION_LABEL,
    dated_base_version_key,
    held_base_repository,
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


def held_base_copy_exists(copy_ref: str, *, creds: str) -> bool:
    """
    Return True when `creds` can read the copy, False when it is missing or refused.

    The package is private, so every read uses the job's token. GHCR may answer even an
    authenticated read of a package that was never created with `denied`, and an anonymous read
    cannot tell a missing package from a private one, so "missing" and "refused" are one answer
    here: the copy is not confirmed readable. Any other registry error (rate limit, network)
    raises, so "could not tell" is never read as "missing".
    """
    try:
        return skopeo_inspect_json_optional(copy_ref, creds=creds) is not None
    except CiToolError as exc:
        if is_registry_permission_error(str(exc)):
            return False
        raise


def copy_held_base(
    *, base_image_name: str, base_image_digest: str, image_org: str, creds: str
) -> str:
    """
    Make sure this repository's copy of one held base exists, and return its ref.

    The quay.io source is read anonymously and only the GHCR destination is given the token
    (`authenticate_source=False`), so the GitHub token never reaches quay.io. The copy is made
    with the same options promotion uses (`--preserve-digests`, `--multi-arch=all`), because a
    Kinoite base is a multi-arch index and the copy must be the same digest. It is tagged with
    the base's version label exactly, the tag resolution looks up. A tag that already holds a
    different digest, or a copy that landed at a different digest, fails the run: nothing here
    overwrites or guesses.

    Last, `held_base_copy_exists` is asked about the copy by digest with the same token kind the
    scheduled gate uses. If it cannot see the copy, the run fails here, loudly, instead of the
    gate seeing "missing" every night and rebuilding while this job looks green.
    """
    repository = held_base_repository(image_org)
    copy_ref = held_base_copy_ref(image_org, f"{base_image_name}@{base_image_digest}")
    if not copy_ref:
        raise CiToolError(
            f"Cannot keep a copy of {base_image_name}: its digest {base_image_digest!r} is not "
            "a sha256 digest."
        )

    if base_image_name == repository:
        # Resolution already built this run on the copy, so it is verified, never copied onto
        # itself: an inspect by digest returns that digest or fails.
        verified = skopeo_inspect_json_optional(copy_ref, creds=creds)
        if verified is None or str(verified.get("Digest") or "") != base_image_digest:
            raise CiToolError(
                f"This run was resolved on {copy_ref}, but reading it with the job's token "
                f"returned {'nothing' if verified is None else verified.get('Digest')!r}."
            )
        print(f"Held base is already the copy {copy_ref}; verified, not copied.")
    else:
        source_ref = f"docker://{base_image_name}@{base_image_digest}"
        labels = skopeo_inspect_json(source_ref).get("Labels") or {}
        version = str(labels.get(BASE_VERSION_LABEL) or "")
        if dated_base_version_key(version) is None:
            raise CiToolError(
                f"Cannot keep a copy of {source_ref}: its {BASE_VERSION_LABEL} label "
                f"{version!r} is not a dated base version such as 44.20261110.0, and resolution "
                "finds copies by that tag."
            )

        tag_ref = f"docker://{repository}:{version}"
        existing = skopeo_inspect_json_optional(tag_ref, creds=creds)
        if existing is None:
            skopeo_copy(
                source_ref,
                tag_ref,
                creds=creds,
                preserve_digests=True,
                multi_arch="all",
                authenticate_source=False,
            )
            copied_digest = skopeo_inspect_digest(tag_ref, creds=creds)
            print(f"Copied held base {source_ref} -> {tag_ref}")
        else:
            copied_digest = str(existing.get("Digest") or "")
        if copied_digest != base_image_digest:
            raise CiToolError(
                f"Held base copy digest mismatch: {tag_ref} resolves to "
                f"{copied_digest or 'no digest'}, expected {base_image_digest} from {source_ref}."
            )

    if not held_base_copy_exists(copy_ref, creds=creds):
        raise CiToolError(
            f"The held base copy {copy_ref} was just checked, but the job's token cannot read "
            "it back. The scheduled gate reads it the same way and would see no copy and "
            "rebuild every night. Check that the package is linked to this repository and "
            "readable by this repository's workflows."
        )
    print(f"Held base copy {copy_ref}: readable with the job's token.")
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


if __name__ == "__main__":
    main()
