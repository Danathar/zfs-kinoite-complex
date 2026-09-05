"""
Script: tests/test_check_akmods_cache.py
What: Tests for shared akmods cache validation helpers.
Doing: Creates temporary RPM trees and checks primary-kernel cache detection,
plus the cosign signature check that gates reuse.
Why: Protects the simplified cache check that now follows only the supported primary kernel
and only trusts a cache signed by this repo's own key.
Goal: Keep rebuild decisions fail-closed when the required primary-kernel RPM is absent, or
when the cache cannot be verified as this repo's own signed output.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import ANY, patch

from ci_tools.check_akmods_cache import (
    AkmodsCacheStatus,
    _has_kernel_matching_rpm,
    inspect_akmods_cache,
    main,
)
from ci_tools.common import CiToolError


class CheckAkmodsCacheTests(unittest.TestCase):
    def test_reports_missing_primary_kernel_rpm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_dir = root / "rpms" / "kmods" / "zfs"
            rpm_dir.mkdir(parents=True, exist_ok=True)
            (rpm_dir / "kmod-zfs-6.18.13-200.fc43.x86_64-2.4.1-1.fc43.x86_64.rpm").touch()

            self.assertFalse(
                _has_kernel_matching_rpm(root, "6.18.16-200.fc43.x86_64", "2.4.1")
            )

    def test_rejects_cache_built_against_a_different_zfs_minor_line(self) -> None:
        # The cache holds the right kernel but the wrong ZFS line. Reusing it
        # would publish an image whose ZFS version silently disagrees with the
        # resolved ZFS version for the run.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_dir = root / "rpms" / "kmods" / "zfs"
            rpm_dir.mkdir(parents=True, exist_ok=True)
            (
                rpm_dir / "kmod-zfs-6.18.16-200.fc43.x86_64-2.3.8-1.fc43.x86_64.rpm"
            ).touch()

            self.assertFalse(
                _has_kernel_matching_rpm(root, "6.18.16-200.fc43.x86_64", "2.4.1")
            )
            self.assertTrue(
                _has_kernel_matching_rpm(root, "6.18.16-200.fc43.x86_64", "2.3.8")
            )

    def test_rejects_cache_built_against_an_older_patch_on_the_same_line(self) -> None:
        # This is the bug the exact-version match fixes: a cache holding 2.4.3
        # must MISS when the run resolved 2.4.4, even though both are on the
        # 2.4 line, so a new OpenZFS patch actually triggers a rebuild.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_dir = root / "rpms" / "kmods" / "zfs"
            rpm_dir.mkdir(parents=True, exist_ok=True)
            (
                rpm_dir / "kmod-zfs-7.0.12-201.fc44.x86_64-2.4.3-1.fc44.x86_64.rpm"
            ).touch()

            self.assertFalse(
                _has_kernel_matching_rpm(root, "7.0.12-201.fc44.x86_64", "2.4.4")
            )
            self.assertTrue(
                _has_kernel_matching_rpm(root, "7.0.12-201.fc44.x86_64", "2.4.3")
            )

    def test_exact_version_match_does_not_match_a_longer_numeric_patch(self) -> None:
        # `2.4.3` must not be satisfied by a hypothetical `2.4.30` release.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_dir = root / "rpms" / "kmods" / "zfs"
            rpm_dir.mkdir(parents=True, exist_ok=True)
            (
                rpm_dir / "kmod-zfs-6.18.16-200.fc43.x86_64-2.4.30-1.fc43.x86_64.rpm"
            ).touch()

            self.assertFalse(
                _has_kernel_matching_rpm(root, "6.18.16-200.fc43.x86_64", "2.4.3")
            )

    def test_inspect_akmods_cache_reads_shared_cache_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def fake_copy(_source: str, destination: str) -> None:
                image_dir = Path(destination.removeprefix("dir:"))
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / "manifest.json").write_text(
                    "{\"layers\": [{\"digest\": \"sha256:layer\"}]}",
                    encoding="utf-8",
                )
                (image_dir / "layer").write_text("", encoding="utf-8")

            def fake_load_layers(_image_dir: Path) -> list[Path]:
                return [root / "layer.tar"]

            def fake_unpack(_layer_files: list[Path], destination: Path) -> None:
                rpm_dir = destination / "rpms" / "kmods" / "zfs"
                rpm_dir.mkdir(parents=True, exist_ok=True)
                (
                    rpm_dir / "kmod-zfs-6.18.16-200.fc43.x86_64-2.4.1-1.fc43.x86_64.rpm"
                ).touch()

            with patch(
                "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
                return_value={"Digest": "sha256:abc123"},
            ) as inspect_json_optional, patch(
                "ci_tools.check_akmods_cache.skopeo_copy",
                side_effect=fake_copy,
            ) as skopeo_copy, patch(
                "ci_tools.check_akmods_cache.load_layer_files_from_oci_layout",
                side_effect=fake_load_layers,
            ), patch(
                "ci_tools.check_akmods_cache.unpack_layer_tarballs",
                side_effect=fake_unpack,
            ), patch(
                "ci_tools.check_akmods_cache.cosign_verify"
            ) as cosign_verify:
                status = inspect_akmods_cache(
                    image_org="danathar",
                    source_repo="zfs-kinoite-complex-akmods",
                    fedora_version="43",
                    kernel_release="6.18.16-200.fc43.x86_64",
                    zfs_version="2.4.1",
                )

        self.assertTrue(status.reusable)
        self.assertTrue(status.signature_verified)
        self.assertEqual(
            status.source_image_pinned,
            "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc123",
        )
        self.assertEqual(status.inspection_method, "unpacked-image")
        inspect_json_optional.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43"
        )
        skopeo_copy.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc123",
            ANY,
        )
        cosign_verify.assert_called_once_with(
            "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc123",
            key_path=ANY,
        )

    def test_inspect_akmods_cache_rejects_reuse_when_signature_verification_fails(self) -> None:
        # The cache has the right kmod-zfs RPM but is not signed by this
        # repo's key (or is not signed at all) -- reuse must be refused even
        # though the RPM content looks correct. This is the actual fix: a
        # matching filename alone is not proof of who produced the content.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def fake_copy(_source: str, destination: str) -> None:
                image_dir = Path(destination.removeprefix("dir:"))
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / "manifest.json").write_text(
                    "{\"layers\": [{\"digest\": \"sha256:layer\"}]}",
                    encoding="utf-8",
                )
                (image_dir / "layer").write_text("", encoding="utf-8")

            def fake_load_layers(_image_dir: Path) -> list[Path]:
                return [root / "layer.tar"]

            def fake_unpack(_layer_files: list[Path], destination: Path) -> None:
                rpm_dir = destination / "rpms" / "kmods" / "zfs"
                rpm_dir.mkdir(parents=True, exist_ok=True)
                (
                    rpm_dir / "kmod-zfs-6.18.16-200.fc43.x86_64-2.4.1-1.fc43.x86_64.rpm"
                ).touch()

            with patch(
                "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
                return_value={"Digest": "sha256:abc123"},
            ), patch(
                "ci_tools.check_akmods_cache.skopeo_copy",
                side_effect=fake_copy,
            ), patch(
                "ci_tools.check_akmods_cache.load_layer_files_from_oci_layout",
                side_effect=fake_load_layers,
            ), patch(
                "ci_tools.check_akmods_cache.unpack_layer_tarballs",
                side_effect=fake_unpack,
            ), patch(
                "ci_tools.check_akmods_cache.cosign_verify",
                side_effect=CiToolError("no signatures found"),
            ):
                status = inspect_akmods_cache(
                    image_org="danathar",
                    source_repo="zfs-kinoite-complex-akmods",
                    fedora_version="43",
                    kernel_release="6.18.16-200.fc43.x86_64",
                    zfs_version="2.4.1",
                )

        self.assertFalse(status.reusable)
        self.assertFalse(status.signature_verified)
        # The RPM content itself was fine; only the signature check failed.
        self.assertEqual(status.missing_release, "")

    def test_inspect_akmods_cache_misses_when_cache_holds_an_older_patch(self) -> None:
        # End-to-end version of the same bug: a real cache image whose only
        # kmod-zfs is 2.4.3 must be rejected when the run resolved 2.4.4.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def fake_copy(_source: str, destination: str) -> None:
                image_dir = Path(destination.removeprefix("dir:"))
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / "manifest.json").write_text(
                    "{\"layers\": [{\"digest\": \"sha256:layer\"}]}",
                    encoding="utf-8",
                )
                (image_dir / "layer").write_text("", encoding="utf-8")

            def fake_load_layers(_image_dir: Path) -> list[Path]:
                return [root / "layer.tar"]

            def fake_unpack(_layer_files: list[Path], destination: Path) -> None:
                rpm_dir = destination / "rpms" / "kmods" / "zfs"
                rpm_dir.mkdir(parents=True, exist_ok=True)
                (
                    rpm_dir / "kmod-zfs-7.0.12-201.fc44.x86_64-2.4.3-1.fc44.x86_64.rpm"
                ).touch()

            with patch(
                "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
                return_value={"Digest": "sha256:abc123"},
            ), patch(
                "ci_tools.check_akmods_cache.skopeo_copy",
                side_effect=fake_copy,
            ), patch(
                "ci_tools.check_akmods_cache.load_layer_files_from_oci_layout",
                side_effect=fake_load_layers,
            ), patch(
                "ci_tools.check_akmods_cache.unpack_layer_tarballs",
                side_effect=fake_unpack,
            ):
                status = inspect_akmods_cache(
                    image_org="danathar",
                    source_repo="zfs-kinoite-complex-akmods",
                    fedora_version="44",
                    kernel_release="7.0.12-201.fc44.x86_64",
                    zfs_version="2.4.4",
                )

        self.assertFalse(status.reusable)
        self.assertEqual(status.missing_release, "7.0.12-201.fc44.x86_64")
        self.assertEqual(status.required_zfs_version, "2.4.4")

    def test_inspect_akmods_cache_reports_missing_image_when_tag_does_not_exist(self) -> None:
        with patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            return_value=None,
        ):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertFalse(status.reusable)
        self.assertFalse(status.image_exists)
        self.assertEqual(
            status.source_image,
            "ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
        )
        self.assertEqual(status.source_image_pinned, "")
        self.assertEqual(status.missing_release, "6.18.16-200.fc43.x86_64")
        self.assertEqual(status.inspection_method, "missing-image")

    def test_inspect_akmods_cache_passes_registry_credentials_when_available(self) -> None:
        with patch.dict(
            os.environ,
            {"REGISTRY_ACTOR": "Danathar", "REGISTRY_TOKEN": "token"},
            clear=True,
        ), patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            return_value=None,
        ) as inspect_json_optional:
            inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        inspect_json_optional.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
            creds="Danathar:token",
        )

    def test_inspect_akmods_cache_raises_on_non_missing_registry_error(self) -> None:
        # skopeo_inspect_json_optional already re-raises everything except a
        # missing-image error; inspect_akmods_cache must not swallow it into a
        # false "image_exists=False" the way it used to.
        with patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            side_effect=CiToolError("unauthorized: authentication required"),
        ), self.assertRaises(CiToolError) as context:
            inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertIn("unauthorized", str(context.exception))

    def test_inspect_akmods_cache_raises_ci_error_when_layer_unpacking_fails(self) -> None:
        with patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            return_value={"Digest": "sha256:abc123"},
        ), patch("ci_tools.check_akmods_cache.skopeo_copy"), patch(
            "ci_tools.check_akmods_cache.load_layer_files_from_oci_layout",
            side_effect=RuntimeError("No layers found in OCI layout"),
        ), self.assertRaises(CiToolError) as context:
            inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertIn("No layers found in OCI layout", str(context.exception))

    def test_inspect_akmods_cache_raises_when_the_registry_returns_no_digest(self) -> None:
        # `skopeo_inspect_json_optional` returning a payload without a Digest
        # means the tag exists but we could not pin it. Continuing would copy
        # `...@` and verify a signature against an empty digest, so this has to
        # stop rather than fall through to a reuse decision made from an
        # unpinned reference.
        with patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            return_value={"Name": "ghcr.io/danathar/zfs-kinoite-complex-akmods"},
        ), patch("ci_tools.check_akmods_cache.skopeo_copy") as skopeo_copy, self.assertRaises(
            CiToolError
        ) as context:
            inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertIn("Missing digest in skopeo inspect output", str(context.exception))
        self.assertIn(
            "docker://ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
            str(context.exception),
        )
        skopeo_copy.assert_not_called()

    def test_has_kernel_matching_rpm_is_false_when_the_cache_has_no_kmods_directory(
        self,
    ) -> None:
        # A layer that unpacked but carries no rpms/kmods/zfs directory at all
        # is a cache miss, not a crash: `Path.glob` on a missing directory
        # would still return an empty iterator, so nothing here fails loudly
        # and the explicit `exists()` guard is what keeps the answer False.
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertFalse(
                _has_kernel_matching_rpm(
                    Path(temp_dir), "6.18.16-200.fc43.x86_64", "2.4.1"
                )
            )


class RegistryCredentialsTests(unittest.TestCase):
    """
    Covers the authenticated registry path, which is the one production takes.

    The first `check-akmods-cache` step in
    `.github/actions/prepare-main-akmods/action.yml` passes `REGISTRY_ACTOR`
    and `REGISTRY_TOKEN`, so every scheduled build reaches the credentialed
    branch of the pull and of the signature check. The rest of this file runs
    with those variables unset and therefore only exercises the anonymous
    fallback, which is the branch production never takes.

    Both halves of the credential contract matter. Dropping `creds=` from the
    copy makes the pull anonymous, which fails outright against a private
    cache. Dropping `registry_username`/`registry_password` from
    `cosign_verify` makes the verification fail instead of the pull, and that
    failure is swallowed into `signature_verified=False` -- the run then
    reports "its cosign signature could not be verified" and rebuilds, so a
    broken credential path looks like an unsigned cache rather than an error.
    """

    _CREDS: ClassVar[dict[str, str]] = {
        "REGISTRY_ACTOR": "Danathar",
        "REGISTRY_TOKEN": "registry-token-value",
    }

    @staticmethod
    def _fake_copy(*_args: object, **_kwargs: object) -> None:
        return None

    @staticmethod
    def _fake_unpack(_layer_files: list[Path], destination: Path) -> None:
        rpm_dir = destination / "rpms" / "kmods" / "zfs"
        rpm_dir.mkdir(parents=True, exist_ok=True)
        (rpm_dir / "kmod-zfs-6.18.16-200.fc43.x86_64-2.4.1-1.fc43.x86_64.rpm").touch()

    @contextlib.contextmanager
    def _cache_image(self, *, cosign_side_effect: object = None):
        """Patch the whole pull/unpack/verify chain around one cached image."""

        with patch.dict(os.environ, self._CREDS, clear=False), patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional",
            return_value={"Digest": "sha256:abc123"},
        ), patch(
            "ci_tools.check_akmods_cache.skopeo_copy",
            side_effect=self._fake_copy,
        ) as skopeo_copy, patch(
            "ci_tools.check_akmods_cache.load_layer_files_from_oci_layout",
            return_value=[],
        ), patch(
            "ci_tools.check_akmods_cache.unpack_layer_tarballs",
            side_effect=self._fake_unpack,
        ), patch(
            "ci_tools.check_akmods_cache.cosign_verify",
            side_effect=cosign_side_effect,
        ) as cosign_verify:
            yield skopeo_copy, cosign_verify

    def test_cache_pull_is_authenticated_when_registry_credentials_are_set(self) -> None:
        with self._cache_image() as (skopeo_copy, _cosign_verify):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertTrue(status.reusable)
        skopeo_copy.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc123",
            ANY,
            creds="Danathar:registry-token-value",
        )
        # The token must travel as the `creds` keyword and nowhere else:
        # `skopeo_copy` puts that value behind `--src-creds`/`--dest-creds`,
        # which `redact_command_args` knows to strip from a failure message. A
        # token smuggled into a positional argument would be printed verbatim
        # into the job log the first time the copy failed.
        for positional in skopeo_copy.call_args.args:
            self.assertNotIn("registry-token-value", str(positional))

    def test_signature_check_is_authenticated_when_registry_credentials_are_set(
        self,
    ) -> None:
        with self._cache_image() as (_skopeo_copy, cosign_verify):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        self.assertTrue(status.signature_verified)
        cosign_verify.assert_called_once_with(
            "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc123",
            key_path=ANY,
            registry_username="Danathar",
            registry_password="registry-token-value",
        )
        for positional in cosign_verify.call_args.args:
            self.assertNotIn("registry-token-value", str(positional))

    def test_authenticated_signature_failure_still_refuses_reuse(self) -> None:
        # The credentialed cosign call fails exactly like the anonymous one:
        # swallowed into signature_verified=False so the caller rebuilds
        # instead of trusting a cache it could not attribute to this repo.
        with self._cache_image(
            cosign_side_effect=CiToolError("no matching signatures")
        ) as (_skopeo_copy, cosign_verify):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
            )

        cosign_verify.assert_called_once()
        self.assertFalse(status.signature_verified)
        self.assertFalse(status.reusable)
        # Content was fine; only attribution failed.
        self.assertEqual(status.missing_release, "")

    def test_skipping_the_signature_check_makes_no_cosign_call_at_all(self) -> None:
        # `verify_signature=False` is the post-rebuild path: signing runs in a
        # later job, so the image is legitimately unsigned and a cosign call
        # would be a guaranteed failure. Skipping must mean *not calling*, not
        # calling and ignoring the result -- an ignored failure would still
        # cost a registry round trip and log an error next to a healthy build.
        with self._cache_image() as (_skopeo_copy, cosign_verify):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="43",
                kernel_release="6.18.16-200.fc43.x86_64",
                zfs_version="2.4.1",
                verify_signature=False,
            )

        cosign_verify.assert_not_called()
        self.assertFalse(status.signature_verified)
        self.assertTrue(status.content_matches)
        self.assertFalse(status.reusable)


class RequireMatchModeTests(unittest.TestCase):
    """
    Covers the strict mode used to verify a cache this run just rebuilt.

    The akmods fork resolves its own OpenZFS patch version independently of
    this repo, so a rebuild can publish a cache that does not contain the
    version this run resolved and is about to record as an image label. In
    normal mode that is just "rebuild required"; after a rebuild it is a
    failure, because there is nothing left to retry and the label would lie.
    """

    _ENV: ClassVar[dict[str, str]] = {
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "FEDORA_VERSION": "43",
        "KERNEL_RELEASE": "6.18.16-200.fc43.x86_64",
        "AKMODS_REPO": "zfs-kinoite-complex-akmods",
        "ZFS_VERSION": "2.4.4",
    }

    def test_require_match_raises_when_the_rebuilt_cache_does_not_match(self) -> None:
        mismatched = AkmodsCacheStatus(
            source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
            image_exists=True,
            source_image_pinned="ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc",
            missing_release="6.18.16-200.fc43.x86_64",
            required_zfs_version="2.4.4",
        )
        env = {**self._ENV, "REQUIRE_MATCH": "true"}
        with patch.dict(os.environ, env, clear=False), patch(
            "ci_tools.check_akmods_cache.inspect_akmods_cache", return_value=mismatched
        ), self.assertRaises(CiToolError) as context:
            main()

        self.assertIn("2.4.4", str(context.exception))
        self.assertIn("even after a rebuild", str(context.exception))

    def test_require_match_is_silent_when_the_rebuilt_cache_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            matched = AkmodsCacheStatus(
                source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                image_exists=True,
                source_image_pinned="ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc",
                missing_release="",
                required_zfs_version="2.4.4",
            )
            env = {**self._ENV, "REQUIRE_MATCH": "true", "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache", return_value=matched
            ):
                main()

    def test_require_match_does_not_demand_a_signature(self) -> None:
        # Regression guard for a bug that existed in neither change alone.
        # Exact-version verification runs inside the akmods job right after a
        # rebuild; cache signing runs in a *later, separate* job. So the cache
        # is always unsigned at verification time. If this check asked for
        # `reusable` (which requires a signature) instead of `content_matches`,
        # every single rebuild would fail.
        unsigned_but_correct = AkmodsCacheStatus(
            source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
            image_exists=True,
            source_image_pinned="ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc",
            missing_release="",
            required_zfs_version="2.4.4",
            signature_verified=False,
        )
        self.assertTrue(unsigned_but_correct.content_matches)
        self.assertFalse(unsigned_but_correct.reusable)

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            env = {**self._ENV, "REQUIRE_MATCH": "true", "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache",
                return_value=unsigned_but_correct,
            ) as inspect_cache:
                main()

            # It must also skip the cosign call entirely, rather than making a
            # request that is guaranteed to fail against an unsigned image.
            self.assertFalse(inspect_cache.call_args.kwargs["verify_signature"])

    def test_require_match_success_does_not_claim_a_rebuild_is_required(self) -> None:
        # Strict mode skips the signature check, which leaves `reusable` false.
        # Sharing the reuse-decision output path made a *successful* post-rebuild
        # verification print "signature could not be verified ... akmods rebuild
        # is required" and write exists=false -- seen for real in run 30318665416.
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            env = {**self._ENV, "REQUIRE_MATCH": "true", "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache",
                return_value=AkmodsCacheStatus(
                    source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                    image_exists=True,
                    source_image_pinned=(
                        "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc"
                    ),
                    missing_release="",
                    required_zfs_version="2.4.4",
                    signature_verified=False,
                ),
            ), contextlib.redirect_stdout(io.StringIO()) as out:
                main()

            printed = out.getvalue()
            self.assertIn("Verified the rebuilt", printed)
            self.assertNotIn("rebuild is required", printed)
            self.assertNotIn("could not be verified", printed)
            # Must not stamp a reuse decision the caller might act on. Strict
            # mode returns before writing any output at all, so the file is
            # never even created.
            self.assertFalse(output_path.exists())

    def test_reuse_path_still_verifies_the_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            env = {**self._ENV, "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache",
                return_value=AkmodsCacheStatus(
                    source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                    image_exists=True,
                    source_image_pinned=(
                        "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc"
                    ),
                    missing_release="",
                    required_zfs_version="2.4.4",
                    signature_verified=True,
                ),
            ) as inspect_cache:
                main()

            self.assertTrue(inspect_cache.call_args.kwargs["verify_signature"])

    def test_default_mode_still_reports_a_mismatch_without_raising(self) -> None:
        # The pre-rebuild check must keep treating "no usable cache" as a
        # normal answer that triggers a rebuild, not as a failure.
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            mismatched = AkmodsCacheStatus(
                source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                image_exists=True,
                source_image_pinned="ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc",
                missing_release="6.18.16-200.fc43.x86_64",
                required_zfs_version="2.4.4",
            )
            env = {**self._ENV, "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache", return_value=mismatched
            ):
                main()

            self.assertIn("exists<<", output_path.read_text(encoding="utf-8"))


class ReuseDecisionOutputTests(unittest.TestCase):
    """
    Covers the two reuse-decision outcomes `RequireMatchModeTests` leaves out.

    `main()` has three non-strict endings and they are not interchangeable to a
    reader of the job log: no cache image at all, a cache that is missing the
    RPM, and a cache that has the RPM but could not be attributed to this
    repo's key. Only the middle one was asserted. The other two both write
    `exists=false`, so the output alone cannot tell them apart -- the printed
    reason is the whole difference, and a wrong one sends a human looking at
    the akmods build when the real problem is signing, or the reverse.
    """

    _ENV: ClassVar[dict[str, str]] = {
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "FEDORA_VERSION": "43",
        "KERNEL_RELEASE": "6.18.16-200.fc43.x86_64",
        "AKMODS_REPO": "zfs-kinoite-complex-akmods",
        "ZFS_VERSION": "2.4.4",
    }

    def _run_main(self, status: AkmodsCacheStatus) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output"
            env = {**self._ENV, "GITHUB_OUTPUT": str(output_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "ci_tools.check_akmods_cache.inspect_akmods_cache", return_value=status
            ), contextlib.redirect_stdout(io.StringIO()) as out:
                # patch.dict restores the whole mapping on exit, so removing a
                # leaked REQUIRE_MATCH here cannot affect another test.
                os.environ.pop("REQUIRE_MATCH", None)
                main()
            return out.getvalue(), output_path.read_text(encoding="utf-8")

    def test_absent_cache_image_writes_exists_false_and_says_so(self) -> None:
        printed, output = self._run_main(
            AkmodsCacheStatus(
                source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                image_exists=False,
                missing_release="6.18.16-200.fc43.x86_64",
                required_zfs_version="2.4.4",
                inspection_method="missing-image",
            )
        )

        self.assertIn("exists<<", output)
        self.assertIn("\nfalse\n", output)
        # No pinned digest exists yet, so nothing may be published as one.
        self.assertNotIn("akmods_image", output)
        self.assertIn("No existing shared akmods cache image for Fedora 43", printed)
        self.assertIn("rebuild is required", printed)

    def test_unverifiable_signature_is_reported_as_a_signature_problem(self) -> None:
        # image_exists and the RPM both check out; only the signature did not.
        # This is the branch that must not be described as a missing kmod-zfs.
        printed, output = self._run_main(
            AkmodsCacheStatus(
                source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
                image_exists=True,
                source_image_pinned=(
                    "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:abc"
                ),
                missing_release="",
                required_zfs_version="2.4.4",
                signature_verified=False,
            )
        )

        self.assertIn("exists<<", output)
        self.assertIn("\nfalse\n", output)
        self.assertIn("cosign signature could not be verified", printed)
        self.assertIn("akmods rebuild is required", printed)
        # The RPM was present. Saying otherwise would point a reader at the
        # akmods build instead of at signing.
        self.assertNotIn("has no kmod-zfs", printed)


if __name__ == "__main__":
    unittest.main()
