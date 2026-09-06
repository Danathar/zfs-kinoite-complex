"""
Script: tests/test_akmods_build_and_publish.py
What: Tests helper functions used by `ci_tools/akmods_build_and_publish.py`.
Doing: Checks cache-document generation and the primary-kernel-only publish flow.
Why: Catches behavior changes that could break the shared akmods cache build.
Goal: Keep the simplified akmods publish path explicit and reviewable.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ci_tools import akmods_build_and_publish as script
from ci_tools.akmods_build_and_publish import (
    build_kernel_cache_document,
    kernel_major_minor_patch,
    kernel_name_for_flavor,
)
from ci_tools.common import CiToolError


class AkmodsBuildAndPublishTests(unittest.TestCase):
    def test_kernel_name_for_longterm_flavor(self) -> None:
        self.assertEqual(kernel_name_for_flavor("longterm"), "kernel-longterm")
        self.assertEqual(kernel_name_for_flavor("longterm-lts"), "kernel-longterm")

    def test_kernel_name_for_standard_flavor(self) -> None:
        self.assertEqual(kernel_name_for_flavor("main"), "kernel")

    def test_kernel_major_minor_patch(self) -> None:
        value = kernel_major_minor_patch("6.18.12-200.fc43.x86_64")
        self.assertEqual(value, "6.18.12-200")

    def test_build_kernel_cache_document_default_path(self) -> None:
        payload, cache_path, upstream_build_root = build_kernel_cache_document(
            kernel_release="6.18.12-200.fc43.x86_64",
            kernel_flavor="main",
            akmods_version="43",
            build_root=Path("/tmp/akmods/build"),
            kcpath_override="",
        )

        self.assertEqual(payload["kernel_name"], "kernel")
        self.assertEqual(payload["kernel_major_minor_patch"], "6.18.12-200")
        self.assertEqual(str(upstream_build_root), "/tmp/akmods/build")
        self.assertTrue(payload["KCWD"].endswith("/main-43/KCWD"))
        self.assertTrue(payload["KCPATH"].endswith("/main-43/KCWD/rpms"))
        self.assertTrue(str(cache_path).endswith("/main-43/KCWD/rpms/cache.json"))

    def test_build_kernel_cache_document_with_kcpath_override(self) -> None:
        payload, cache_path, upstream_build_root = build_kernel_cache_document(
            kernel_release="6.18.12-200.fc43.x86_64",
            kernel_flavor="main",
            akmods_version="43",
            build_root=Path("/tmp/akmods/build"),
            kcpath_override="/custom/rpms",
        )

        self.assertEqual(str(upstream_build_root), "/tmp/akmods/build")
        self.assertEqual(payload["KCPATH"], "/custom/rpms")
        self.assertEqual(str(cache_path), "/custom/rpms/cache.json")

    def test_write_kernel_cache_file_exports_upstream_build_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir)),
            patch.dict(
                script.os.environ,
                {
                    "AKMODS_KERNEL": "main",
                    "AKMODS_VERSION": "43",
                },
                clear=True,
            ),
        ):
            script.write_kernel_cache_file(
                kernel_release="6.18.16-200.fc43.x86_64",
            )

            self.assertEqual(
                script.os.environ["AKMODS_BUILDDIR"],
                f"{tempdir}/build",
            )
            self.assertFalse("KCPATH" in script.os.environ)

    def test_write_kernel_cache_file_preserves_preexisting_kcpath_without_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir)),
            patch.dict(
                script.os.environ,
                {
                    "AKMODS_KERNEL": "main",
                    "AKMODS_VERSION": "43",
                    "KCPATH": "/preexisting",
                },
                clear=True,
            ),
        ):
            with patch.object(Path, "mkdir"), patch.object(Path, "write_text"):
                script.write_kernel_cache_file(
                    kernel_release="6.18.16-200.fc43.x86_64",
                )

            self.assertEqual(script.os.environ["KCPATH"], "/preexisting")

    def test_build_and_push_seeds_the_cache_file_before_upstream_builds(self) -> None:
        # main() patches this function out in the tests below, so the body is
        # asserted here: the cache.json upstream reads has to be on disk with
        # the pinned release in it *before* `just build` runs, and `just push`
        # has to follow the build rather than race it.
        observed: list[tuple[str, bool]] = []

        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir)),
            patch.dict(
                script.os.environ,
                {"AKMODS_KERNEL": "main", "AKMODS_VERSION": "43"},
                clear=True,
            ),
        ):
            cache_json = (
                Path(tempdir) / "build" / "main-43" / "KCWD" / "rpms" / "cache.json"
            )

            def record(args: list[str], **_kwargs: object) -> None:
                observed.append((args[-1], cache_json.exists()))

            with (
                patch.object(script, "run_cmd", side_effect=record) as run_cmd,
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                script.build_and_push_kernel_release("6.18.16-200.fc43.x86_64")

            self.assertEqual(observed, [("build", True), ("push", True)])
            self.assertEqual(
                run_cmd.call_args_list,
                [
                    call(["just", "build"], cwd=str(Path(tempdir)), capture_output=False),
                    call(["just", "push"], cwd=str(Path(tempdir)), capture_output=False),
                ],
            )
            self.assertIn(
                "Building akmods for kernel release: 6.18.16-200.fc43.x86_64",
                printed.getvalue(),
            )

            payload = json.loads(cache_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["kernel_release"], "6.18.16-200.fc43.x86_64")
            self.assertEqual(payload["kernel_flavor"], "main")
            self.assertEqual(payload["kernel_name"], "kernel")
            self.assertEqual(payload["kernel_major_minor_patch"], "6.18.16-200")

    def test_main_without_the_akmods_checkout_publishes_nothing(self) -> None:
        # The clone step puts /tmp/akmods in place. If it did not, every `just`
        # command below would run against whatever the working directory
        # happens to be, so this guard has to stop the run before the first one.
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir) / "absent"),
            patch.object(script, "run_cmd") as run_cmd,
            patch.object(script, "build_and_push_kernel_release") as build_release,
            patch.dict(
                script.os.environ,
                {"KERNEL_RELEASE": "6.18.16-200.fc43.x86_64"},
                clear=True,
            ),
            self.assertRaisesRegex(CiToolError, "Expected akmods checkout at"),
        ):
            script.main()

        run_cmd.assert_not_called()
        build_release.assert_not_called()

    def test_main_primary_kernel_runs_upstream_manifest_flow(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir)),
            patch.object(script, "build_and_push_kernel_release") as build_release,
            patch.object(script, "run_cmd") as run_cmd,
            patch.dict(
                script.os.environ,
                {
                    "KERNEL_RELEASE": "6.18.16-200.fc43.x86_64",
                    "GITHUB_REPOSITORY_OWNER": "Danathar",
                    "AKMODS_REPO": "zfs-kinoite-complex-akmods",
                    "AKMODS_KERNEL": "main",
                    "AKMODS_VERSION": "43",
                },
                clear=False,
            ),
        ):
            script.main()

        build_release.assert_called_once_with("6.18.16-200.fc43.x86_64")
        self.assertEqual(
            run_cmd.call_args_list,
            [
                call(["just", "login"], cwd=str(Path(tempdir)), capture_output=False),
                call(["just", "manifest"], cwd=str(Path(tempdir)), capture_output=False),
            ],
        )

    def test_main_without_kernel_release_keeps_upstream_default_behavior(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.object(script, "AKMODS_WORKTREE", Path(tempdir)),
            patch.object(script, "run_cmd") as run_cmd,
            patch.dict(script.os.environ, {}, clear=True),
        ):
            script.main()

        self.assertEqual(
            run_cmd.call_args_list,
            [
                call(["just", "build"], cwd=str(Path(tempdir)), capture_output=False),
                call(["just", "login"], cwd=str(Path(tempdir)), capture_output=False),
                call(["just", "push"], cwd=str(Path(tempdir)), capture_output=False),
                call(["just", "manifest"], cwd=str(Path(tempdir)), capture_output=False),
            ],
        )


if __name__ == "__main__":
    unittest.main()
