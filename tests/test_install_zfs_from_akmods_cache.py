"""
Script: tests/test_install_zfs_from_akmods_cache.py
What: Tests the helper that installs cached ZFS RPMs into the build root.
Doing: Exercises the primary-kernel planning rules without invoking `dnf5` or mutating the host.
Why: The old inline Containerfile shell block was hard to reason about and almost impossible to unit test.
Goal: Keep the simplified primary-kernel contract explicit and reviewable.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_helper_module():
    helper_path = (
        Path(__file__).resolve().parents[1]
        / "containerfiles"
        / "zfs-akmods"
        / "install_zfs_from_akmods_cache.py"
    )
    spec = importlib.util.spec_from_file_location(
        "install_zfs_from_akmods_cache",
        helper_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper_module()


class InstallZfsFromAkmodsCacheTests(unittest.TestCase):
    def test_resolve_akmods_image_prefers_explicit_override(self) -> None:
        image_ref = helper.resolve_akmods_image(
            environ={"AKMODS_IMAGE": "ghcr.io/example/zfs-kinoite-complex-akmods:manual"},
            run_cmd=lambda _args: "43\n",
        )

        self.assertEqual(image_ref, "ghcr.io/example/zfs-kinoite-complex-akmods:manual")

    def test_resolve_akmods_image_renders_template_with_detected_fedora(self) -> None:
        image_ref = helper.resolve_akmods_image(
            environ={
                "AKMODS_IMAGE_TEMPLATE": "ghcr.io/example/zfs-kinoite-complex-akmods:main-{fedora}"
            },
            run_cmd=lambda _args: "43\n",
        )

        self.assertEqual(image_ref, "ghcr.io/example/zfs-kinoite-complex-akmods:main-43")

    def test_resolve_akmods_image_uses_default_template_when_unset(self) -> None:
        image_ref = helper.resolve_akmods_image(
            environ={},
            run_cmd=lambda _args: "43\n",
        )

        self.assertEqual(image_ref, "ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43")

    def test_run_cmd_redacts_secret_args_in_failure_message(self) -> None:
        args = [
            "skopeo",
            "inspect",
            "--creds",
            "actor:token-secret",
            "--src-creds=actor:src-secret",
        ]
        result = subprocess.CompletedProcess(args, 1, stdout="", stderr="failed")
        with (
            patch.object(helper.subprocess, "run", return_value=result),
            self.assertRaises(RuntimeError) as context,
        ):
            helper._run_cmd(args)

        message = str(context.exception)
        self.assertNotIn("token-secret", message)
        self.assertNotIn("src-secret", message)
        self.assertIn("--creds ***REDACTED***", message)
        self.assertIn("--src-creds=***REDACTED***", message)

    def test_image_kernels_from_modules_root_uses_natural_sort(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            modules_root = Path(temp_dir)
            (modules_root / "6.10.0-200.fc43.x86_64").mkdir()
            (modules_root / "6.9.0-200.fc43.x86_64").mkdir()

            kernels = helper.image_kernels_from_modules_root(modules_root)

        self.assertEqual(
            kernels,
            [
                "6.9.0-200.fc43.x86_64",
                "6.10.0-200.fc43.x86_64",
            ],
        )

    def test_load_layer_files_from_oci_layout_reads_manifest_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            layout_dir = Path(temp_dir)
            manifest_path = layout_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "layers": [
                            {"digest": "sha256:first-layer"},
                            {"digest": "sha256:second-layer"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            layer_files = helper.load_layer_files_from_oci_layout(layout_dir)

            self.assertEqual(
                layer_files,
                [
                    layout_dir / "first-layer",
                    layout_dir / "second-layer",
                ],
            )

    def test_unpack_layer_tarballs_rejects_parent_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad_layer = root / "layer.tar"
            destination = root / "extract"
            destination.mkdir()

            with tarfile.open(bad_layer, "w") as tar_handle:
                info = tarfile.TarInfo("../escape")
                info.size = 0
                tar_handle.addfile(info)

            with self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
                helper.unpack_layer_tarballs([bad_layer], destination)

    def test_unpack_layer_tarballs_rejects_symlinks_pointing_outside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad_layer = root / "layer.tar"
            destination = root / "extract"
            destination.mkdir()

            with tarfile.open(bad_layer, "w") as tar_handle:
                info = tarfile.TarInfo("safe_dir/link")
                info.size = 0
                info.type = tarfile.SYMTYPE
                info.linkname = "../../../etc/passwd"
                tar_handle.addfile(info)

            with self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
                helper.unpack_layer_tarballs([bad_layer], destination)

    def test_unpack_layer_tarballs_rejects_hardlinks_pointing_outside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad_layer = root / "layer.tar"
            destination = root / "extract"
            destination.mkdir()

            with tarfile.open(bad_layer, "w") as tar_handle:
                info = tarfile.TarInfo("safe_dir/link")
                info.size = 0
                info.type = tarfile.LNKTYPE
                info.linkname = "../../../etc/passwd"
                tar_handle.addfile(info)

            with self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
                helper.unpack_layer_tarballs([bad_layer], destination)

    def test_discover_zfs_rpms_filters_non_installable_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rpm_root = Path(temp_dir)
            keep = rpm_root / "zfs-2.4.0-1.fc43.x86_64.rpm"
            skip_src = rpm_root / "zfs-2.4.0-1.fc43.src.rpm"
            skip_debug = rpm_root / "zfs-debug-2.4.0-1.fc43.x86_64.rpm"

            keep.touch()
            skip_src.touch()
            skip_debug.touch()

            zfs_rpms = helper.discover_zfs_rpms(rpm_root)

            self.assertEqual(zfs_rpms, [keep])

    def test_build_install_plan_selects_primary_kernel_and_splits_rpms(self) -> None:
        shared_rpm = Path("/tmp/zfs-2.4.0.rpm")
        first_kmod = Path("/tmp/kmod-zfs-6.18.13.rpm")
        second_kmod = Path("/tmp/kmod-zfs-6.18.16.rpm")

        name_by_path = {
            shared_rpm: "zfs",
            first_kmod: "kmod-zfs",
            second_kmod: "kmod-zfs",
        }
        kernel_by_path = {
            first_kmod: "6.18.13-200.fc43.x86_64",
            second_kmod: "6.18.16-200.fc43.x86_64",
        }

        plan = helper.build_install_plan(
            [
                "6.18.13-200.fc43.x86_64",
                "6.18.16-200.fc43.x86_64",
            ],
            [shared_rpm, first_kmod, second_kmod],
            rpm_name_lookup=name_by_path.__getitem__,
            kernel_release_lookup=kernel_by_path.__getitem__,
        )

        self.assertEqual(plan.managed_rpms, [shared_rpm])
        self.assertEqual(plan.supported_kernel_release, "6.18.16-200.fc43.x86_64")
        self.assertEqual(plan.supported_kmod_rpm, second_kmod)
        self.assertEqual(
            plan.detected_kernel_releases,
            ["6.18.13-200.fc43.x86_64", "6.18.16-200.fc43.x86_64"],
        )

    def test_build_install_plan_rejects_missing_primary_kernel_payload(self) -> None:
        first_kmod = Path("/tmp/kmod-zfs-6.18.13.rpm")

        with self.assertRaisesRegex(RuntimeError, "do not cover the supported kernel"):
            helper.build_install_plan(
                [
                    "6.18.13-200.fc43.x86_64",
                    "6.18.16-200.fc43.x86_64",
                ],
                [first_kmod],
                rpm_name_lookup=lambda _path: "kmod-zfs",
                kernel_release_lookup=lambda _path: "6.18.13-200.fc43.x86_64",
            )

    def test_build_install_plan_rejects_duplicate_kernel_payloads(self) -> None:
        first_kmod = Path("/tmp/kmod-zfs-a.rpm")
        second_kmod = Path("/tmp/kmod-zfs-b.rpm")

        with self.assertRaisesRegex(RuntimeError, "Multiple kmod-zfs RPMs"):
            helper.build_install_plan(
                ["6.18.16-200.fc43.x86_64"],
                [first_kmod, second_kmod],
                rpm_name_lookup=lambda _path: "kmod-zfs",
                kernel_release_lookup=lambda _path: "6.18.16-200.fc43.x86_64",
            )

    def test_validate_installed_modules_checks_only_supported_primary_kernel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            modules_root = Path(temp_dir)
            supported_kernel = modules_root / "6.18.16-200.fc43.x86_64" / "extra" / "zfs"
            supported_kernel.mkdir(parents=True, exist_ok=True)
            (supported_kernel / "zfs.ko").touch()

            depmod_calls: list[list[str]] = []

            helper.validate_installed_modules(
                "6.18.16-200.fc43.x86_64",
                modules_root=modules_root,
                run_cmd=lambda args, **_kwargs: depmod_calls.append(args) or "",
            )

        self.assertEqual(depmod_calls, [["depmod", "-a", "6.18.16-200.fc43.x86_64"]])

    def test_validate_installed_modules_accepts_compressed_module(self) -> None:
        for suffix in ("zfs.ko.xz", "zfs.ko.zst"):
            with self.subTest(module=suffix), tempfile.TemporaryDirectory() as temp_dir:
                modules_root = Path(temp_dir)
                module_dir = modules_root / "6.18.16-200.fc43.x86_64" / "extra" / "zfs"
                module_dir.mkdir(parents=True, exist_ok=True)
                (module_dir / suffix).touch()

                depmod_calls: list[list[str]] = []

                helper.validate_installed_modules(
                    "6.18.16-200.fc43.x86_64",
                    modules_root=modules_root,
                    run_cmd=lambda args, depmod_calls=depmod_calls, **_kwargs: (
                        depmod_calls.append(args) or ""
                    ),
                )

                self.assertEqual(
                    depmod_calls, [["depmod", "-a", "6.18.16-200.fc43.x86_64"]]
                )

    def test_validate_installed_modules_rejects_when_no_module_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            modules_root = Path(temp_dir)
            module_dir = modules_root / "6.18.16-200.fc43.x86_64" / "extra" / "zfs"
            module_dir.mkdir(parents=True, exist_ok=True)

            with self.assertRaisesRegex(RuntimeError, "do not cover the supported kernel"):
                helper.validate_installed_modules(
                    "6.18.16-200.fc43.x86_64",
                    modules_root=modules_root,
                    run_cmd=lambda _args, **_kwargs: "",
                )

    def test_kmod_kernel_release_accepts_compressed_payload(self) -> None:
        for payload_name in ("zfs.ko", "zfs.ko.xz", "zfs.ko.zst"):
            with self.subTest(payload=payload_name):
                payload = (
                    "/usr/share/doc/kmod-zfs-README\n"
                    f"/lib/modules/6.18.16-200.fc43.x86_64/extra/zfs/{payload_name}\n"
                )
                original_run_cmd = helper._run_cmd
                helper._run_cmd = lambda _args, payload=payload, **_kwargs: payload
                try:
                    kernel_release = helper.kmod_kernel_release(Path("/tmp/kmod-zfs.rpm"))
                finally:
                    helper._run_cmd = original_run_cmd

                self.assertEqual(kernel_release, "6.18.16-200.fc43.x86_64")

    def test_run_cmd_raises_runtime_error_on_timeout(self) -> None:
        # The registry pull runs inside the image build, where a hang would
        # otherwise stall the whole build job. A timeout must surface as the
        # same RuntimeError a nonzero exit raises, not a bare
        # subprocess.TimeoutExpired traceback.
        args = ["skopeo", "copy", "--src-creds", "actor:build-secret", "docker://x", "dir:/tmp/x"]
        with (
            patch.object(
                helper.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(args, 1800.0),
            ),
            self.assertRaises(RuntimeError) as context,
        ):
            helper._run_cmd(args, timeout=1800.0)

        message = str(context.exception)
        self.assertIn("timed out", message)
        self.assertNotIn("build-secret", message)
        self.assertIn("--src-creds ***REDACTED***", message)

    def test_run_cmd_defaults_to_no_timeout(self) -> None:
        # The local rpm/dnf5/depmod callers keep waiting; only the registry
        # pull opts in to a ceiling.
        with patch.object(helper.subprocess, "run") as subprocess_run:
            subprocess_run.return_value = subprocess.CompletedProcess([], 0, stdout="43\n")
            helper._run_cmd(["rpm", "-E", "%fedora"])

        self.assertIsNone(subprocess_run.call_args.kwargs["timeout"])

    def test_registry_transfer_timeout_matches_ci_tools(self) -> None:
        # This script cannot import ci_tools (it runs inside the image build
        # with only shared/ on sys.path), so the ceiling is duplicated the way
        # SECRET_ARG_FLAGS already is. Pin the two together here so they cannot
        # silently drift apart.
        from ci_tools.common import REGISTRY_TRANSFER_TIMEOUT

        self.assertEqual(helper.REGISTRY_TRANSFER_TIMEOUT, REGISTRY_TRANSFER_TIMEOUT)

    def test_registry_pull_is_bounded(self) -> None:
        with patch.object(helper, "_run_cmd") as run_cmd_mock:
            helper.copy_oci_layout_from_registry(
                "ghcr.io/example/akmods:main-43",
                layout_dir=Path(tempfile.gettempdir()) / "does-not-exist-layout",
            )

        self.assertEqual(
            run_cmd_mock.call_args.kwargs.get("timeout"),
            helper.REGISTRY_TRANSFER_TIMEOUT,
        )

    def test_registry_pull_discards_a_stale_layout_directory(self) -> None:
        # A layout left behind by an earlier attempt would let `skopeo copy`
        # land next to blobs from a different akmods image, so the RPMs
        # unpacked afterwards would not all come from the pinned digest.
        with tempfile.TemporaryDirectory() as temp_dir:
            layout_dir = Path(temp_dir) / "akmods-zfs"
            layout_dir.mkdir()
            stale_blob = layout_dir / "stale-layer"
            stale_blob.write_text("from a previous pull", encoding="utf-8")

            with patch.object(helper, "_run_cmd") as run_cmd_mock:
                helper.copy_oci_layout_from_registry(
                    "ghcr.io/example/akmods:main-43",
                    layout_dir=layout_dir,
                )

            self.assertFalse(stale_blob.exists())
            self.assertFalse(layout_dir.exists())
            self.assertEqual(
                run_cmd_mock.call_args.args[0],
                [
                    "skopeo",
                    "copy",
                    "--retry-times",
                    "3",
                    "docker://ghcr.io/example/akmods:main-43",
                    f"dir:{layout_dir}",
                ],
            )

    def test_image_kernels_from_modules_root_rejects_an_empty_modules_root(self) -> None:
        # Without this guard the build would carry on and select a supported
        # kernel from an empty list, so the failure would surface later as an
        # opaque max() error instead of naming the missing modules root.
        with tempfile.TemporaryDirectory() as temp_dir:
            modules_root = Path(temp_dir)
            (modules_root / "not-a-kernel-dir").touch()

            with self.assertRaisesRegex(
                RuntimeError, r"No kernel directories found in .*"
            ) as context:
                helper.image_kernels_from_modules_root(modules_root)

        self.assertIn(str(modules_root), str(context.exception))

    def test_fedora_major_version_rejects_empty_rpm_output(self) -> None:
        # An empty `rpm -E %fedora` would otherwise render the akmods image
        # template as `...:main-`, which resolves to a tag that does not exist
        # or, worse, to some unrelated tag.
        with self.assertRaisesRegex(
            RuntimeError, "Could not determine Fedora major version from rpm -E %fedora"
        ):
            helper.fedora_major_version(run_cmd=lambda _args: "   \n")

    def test_discover_zfs_rpms_rejects_a_cache_with_no_installable_rpms(self) -> None:
        # A cache tree holding only source and debug RPMs means the akmods
        # build did not publish what this image needs; installing nothing must
        # not read as success.
        with tempfile.TemporaryDirectory() as temp_dir:
            rpm_root = Path(temp_dir)
            (rpm_root / "zfs-2.4.0-1.fc43.src.rpm").touch()
            (rpm_root / "zfs-debug-2.4.0-1.fc43.x86_64.rpm").touch()

            with self.assertRaisesRegex(RuntimeError, r"No ZFS RPMs found in .*") as context:
                helper.discover_zfs_rpms(rpm_root)

            self.assertIn(str(rpm_root), str(context.exception))

    def test_kmod_kernel_release_rejects_a_payload_without_a_zfs_module(self) -> None:
        # The kernel release is read from the payload path. An RPM whose
        # listing has no `/lib/modules/<release>/extra/zfs/zfs.ko*` entry
        # cannot be mapped to a kernel, and guessing from the file name is
        # exactly what this function exists to avoid.
        payload_listing = (
            "/usr/share/doc/kmod-zfs-README\n"
            "/lib/modules/6.18.16-200.fc43.x86_64/extra/zfs/zunicode.ko\n"
        )
        rpm_path = Path("/tmp/kmod-zfs-broken.rpm")

        with (
            patch.object(helper, "_run_cmd", return_value=payload_listing),
            self.assertRaisesRegex(
                RuntimeError, "Could not determine kernel release for"
            ) as context,
        ):
            helper.kmod_kernel_release(rpm_path)

        self.assertIn(str(rpm_path), str(context.exception))

    def test_build_install_plan_rejects_a_cache_with_no_kmod_rpms(self) -> None:
        # Userspace ZFS RPMs without any kmod-zfs would install a ZFS
        # toolchain into an image that has no module to load.
        with self.assertRaisesRegex(RuntimeError, "No kmod-zfs RPMs found in cache image"):
            helper.build_install_plan(
                ["6.18.16-200.fc43.x86_64"],
                [Path("/tmp/zfs-2.4.0.rpm"), Path("/tmp/libzfs-2.4.0.rpm")],
                rpm_name_lookup=lambda path: path.name.split("-")[0],
                kernel_release_lookup=lambda _path: "6.18.16-200.fc43.x86_64",
            )

    def test_require_command_names_the_missing_command(self) -> None:
        # main() checks the five host tools up front so a missing one fails
        # before the registry pull, and the message has to say which tool.
        with (
            patch.object(helper.shutil, "which", return_value=None),
            self.assertRaisesRegex(
                RuntimeError, "Required command is not available: dnf5"
            ),
        ):
            helper._require_command("dnf5")

    def test_require_command_accepts_a_present_command(self) -> None:
        with patch.object(helper.shutil, "which", return_value="/usr/bin/dnf5"):
            helper._require_command("dnf5")


if __name__ == "__main__":
    unittest.main()
