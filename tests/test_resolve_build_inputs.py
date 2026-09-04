"""
Script: tests/test_resolve_build_inputs.py
What: Tests for input-resolution tag selection.
Doing: Checks immutable-tag reuse, candidate-tag derivation, and failure paths.
Why: Protects the logic that pins run inputs and avoids moving-tag drift.
Goal: Keep input resolution predictable and explainable.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import CiToolError, sort_kernel_releases
from ci_tools.resolve_build_inputs import (
    _load_lock_file,
    _resolve_default_akmods_ref,
    choose_base_image_tag,
    detect_base_image_kernel_releases,
    extract_source_tag,
    resolve_build_inputs,
    resolve_configured_inputs,
)


class ChooseBaseImageTagTests(unittest.TestCase):
    def test_keeps_existing_date_stamped_source_tag(self) -> None:
        tag, checked = choose_base_image_tag(
            source_tag="latest-20260227",
            version_label="43.20260227.1",
            fedora_version="43",
            expected_digest="sha256:abc",
            digest_lookup=lambda _tag: "sha256:abc",
        )
        self.assertEqual(tag, "latest-20260227")
        self.assertEqual(checked, ["latest-20260227"])

    def test_rejects_date_stamped_source_tag_when_digest_moved(self) -> None:
        with self.assertRaises(CiToolError):
            choose_base_image_tag(
                source_tag="latest-20260227",
                version_label="43.20260227.1",
                fedora_version="43",
                expected_digest="sha256:abc",
                digest_lookup=lambda _tag: "sha256:moved",
            )

    def test_derives_tag_from_version_label_and_digest_match(self) -> None:
        digests = {
            "latest-20260227.1": "sha256:match",
            "43-20260227.1": "sha256:other",
        }

        tag, checked = choose_base_image_tag(
            source_tag="latest",
            version_label="43.20260227.1",
            fedora_version="43",
            expected_digest="sha256:match",
            digest_lookup=lambda t: digests.get(t, ""),
        )
        self.assertEqual(tag, "latest-20260227.1")
        self.assertEqual(checked, ["43.20260227.1", "latest-20260227.1", "43-20260227.1"])

    def test_derives_tag_from_bare_version_label_when_only_it_matches(self) -> None:
        # Some ublue images publish a tag equal to org.opencontainers.image.version
        # verbatim, with none of the other derived candidate forms present.
        digests = {"43.20260610.3": "sha256:match"}

        tag, checked = choose_base_image_tag(
            source_tag="latest",
            version_label="43.20260610.3",
            fedora_version="43",
            expected_digest="sha256:match",
            digest_lookup=lambda t: digests.get(t, ""),
        )
        self.assertEqual(tag, "43.20260610.3")
        self.assertEqual(checked[0], "43.20260610.3")

    def test_derives_tag_from_prefixed_version_label_and_digest_match(self) -> None:
        digests = {
            "latest-43.20260324": "sha256:match",
            "latest-20260324.1": "sha256:other",
            "43-20260324.1": "sha256:other",
            "43-43.20260324": "sha256:other",
        }

        tag, checked = choose_base_image_tag(
            source_tag="latest",
            version_label="latest-43.20260324.1",
            fedora_version="43",
            expected_digest="sha256:match",
            digest_lookup=lambda t: digests.get(t, ""),
        )
        self.assertEqual(tag, "latest-43.20260324")
        self.assertIn("latest-43.20260324", checked)
        self.assertIn("latest-20260324", checked)
        self.assertIn("43-43.20260324", checked)

    def test_rejects_unexpected_version_label(self) -> None:
        with self.assertRaises(CiToolError):
            choose_base_image_tag(
                source_tag="latest",
                version_label="bad-version",
                fedora_version="43",
                expected_digest="sha256:abc",
                digest_lookup=lambda _tag: "",
            )

    def test_rejects_when_no_candidate_tag_matches_expected_digest(self) -> None:
        # Every derived candidate resolves to some other digest -- none of them
        # is the pinned base image, so selection must fail closed instead of
        # silently returning an unverified tag.
        with self.assertRaises(CiToolError):
            choose_base_image_tag(
                source_tag="latest",
                version_label="43.20260227.1",
                fedora_version="43",
                expected_digest="sha256:expected",
                digest_lookup=lambda _tag: "sha256:other",
            )


class SortKernelReleasesTests(unittest.TestCase):
    def test_sorts_kernel_releases_naturally(self) -> None:
        releases = sort_kernel_releases(
            [
                "6.18.10-200.fc43.x86_64",
                "6.18.9-200.fc43.x86_64",
                "6.18.12-200.fc43.x86_64",
            ]
        )
        self.assertEqual(
            releases,
            [
                "6.18.9-200.fc43.x86_64",
                "6.18.10-200.fc43.x86_64",
                "6.18.12-200.fc43.x86_64",
            ],
        )

    def test_deduplicates_kernel_releases_while_preserving_order(self) -> None:
        releases = sort_kernel_releases(
            [
                "6.18.12-200.fc43.x86_64",
                "6.18.10-200.fc43.x86_64",
                "6.18.12-200.fc43.x86_64",
            ]
        )
        self.assertEqual(
            releases,
            [
                "6.18.10-200.fc43.x86_64",
                "6.18.12-200.fc43.x86_64",
            ],
        )


class ExtractSourceTagTests(unittest.TestCase):
    def test_extract_source_tag_from_standard_tagged_ref(self) -> None:
        self.assertEqual(extract_source_tag("ghcr.io/x/y:latest"), "latest")

    def test_extract_source_tag_returns_empty_for_untagged_ref(self) -> None:
        self.assertEqual(extract_source_tag("ghcr.io/x/y"), "")

    def test_extract_source_tag_rejects_host_port_only_ref(self) -> None:
        self.assertEqual(extract_source_tag("localhost:5000/x/y"), "")

    def test_extract_source_tag_accepts_tag_after_host_port(self) -> None:
        self.assertEqual(extract_source_tag("localhost:5000/x/y:latest"), "latest")

    def test_extract_source_tag_rejects_digest_ref(self) -> None:
        self.assertEqual(extract_source_tag("ghcr.io/x/y@sha256:abc"), "")


class ResolveDefaultAkmodsRefTests(unittest.TestCase):
    """Cascade: explicit env > defaults-file pin > git ls-remote against tracking ref."""

    def _env(self, **overrides: str) -> dict:
        wipe = {
            "DEFAULT_AKMODS_REF": "",
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "",
            "AKMODS_UPSTREAM_REPO": "",
        }
        wipe.update(overrides)
        return wipe

    def test_env_sha_ref_wins_over_everything(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "AKMODS_UPSTREAM_TRACK": "main",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(AKMODS_UPSTREAM_REF="cafef00d" * 5), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "cafef00d" * 5)
        ls_remote.assert_not_called()

    def test_env_branch_ref_resolves_with_ls_remote(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "main",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(AKMODS_UPSTREAM_REF="main"), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                return_value="b" * 40,
            ) as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "b" * 40)
        ls_remote.assert_called_once_with("https://example.invalid/akmods.git", "main")

    def test_env_tag_ref_resolves_with_ls_remote(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "main",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(AKMODS_UPSTREAM_REF="v2.4.0"), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                return_value="c" * 40,
            ) as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "c" * 40)
        ls_remote.assert_called_once_with("https://example.invalid/akmods.git", "v2.4.0")

    def test_defaults_file_sha_pin_used_when_env_empty(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "0e06cd70879aa5063c4193710d8c7e37bbc2ab57",
            "AKMODS_UPSTREAM_TRACK": "main",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "0e06cd70879aa5063c4193710d8c7e37bbc2ab57")
        ls_remote.assert_not_called()

    def test_defaults_file_branch_pin_resolves_with_ls_remote(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "main",
            "AKMODS_UPSTREAM_TRACK": "stable",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                return_value="d" * 40,
            ) as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "d" * 40)
        ls_remote.assert_called_once_with("https://example.invalid/akmods.git", "main")

    def test_floats_to_tracking_ref_when_nothing_pinned(self) -> None:
        defaults = {
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "main",
            "AKMODS_UPSTREAM_REPO": "https://example.invalid/akmods.git",
        }
        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                return_value="a" * 40,
            ) as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, "a" * 40)
        ls_remote.assert_called_once_with("https://example.invalid/akmods.git", "main")

    def test_raises_when_nothing_is_configured(self) -> None:
        defaults = {"AKMODS_UPSTREAM_REF": "", "AKMODS_UPSTREAM_TRACK": "", "AKMODS_UPSTREAM_REPO": ""}
        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            self.assertRaises(CiToolError),
        ):
            _resolve_default_akmods_ref()


class LockFileAkmodsRefInvariantTests(unittest.TestCase):
    """
    The checked-in ci/inputs.lock.json must not carry its own akmods_upstream_ref.
    ci/defaults.json is the one source of truth for the pinned akmods commit, and
    a divergent value in the lock file would silently win during replay runs.
    """

    def test_repo_lock_file_does_not_pin_akmods_upstream_ref(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        lock_path = repo_root / "ci" / "inputs.lock.json"
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertNotIn(
            "akmods_upstream_ref",
            data,
            "ci/inputs.lock.json must not pin akmods_upstream_ref; it comes from ci/defaults.json",
        )

    def test_lock_replay_without_akmods_ref_falls_back_to_defaults(self) -> None:
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "zfs_minor_version": "2.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with patch.dict(os.environ, env, clear=False):
                configured = resolve_configured_inputs()

        self.assertTrue(configured.use_input_lock)
        self.assertEqual(configured.base_image_ref, "ghcr.io/example/base@sha256:deadbeef")
        self.assertEqual(configured.zfs_minor_version, "2.4")
        self.assertEqual(configured.akmods_upstream_ref, "a" * 40)

    def test_lock_replay_pins_the_exact_zfs_patch_version(self) -> None:
        # Replay must reuse the locked patch version. Re-resolving it live
        # would make a replay build a different ZFS than the run it reproduces,
        # because the cache check now requires an exact version match.
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "zfs_minor_version": "2.4",
            "zfs_version": "2.4.1",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with patch.dict(os.environ, env, clear=False):
                configured = resolve_configured_inputs()

        self.assertEqual(configured.locked_zfs_version, "2.4.1")

    def test_non_lock_runs_do_not_pin_a_zfs_patch_version(self) -> None:
        env = {
            "USE_INPUT_LOCK": "false",
            "LOCK_FILE": "ci/inputs.lock.json",
            "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
            "DEFAULT_AKMODS_REF": "a" * 40,
            "DEFAULT_ZFS_MINOR_VERSION": "2.4",
        }
        with patch.dict(os.environ, env, clear=False):
            configured = resolve_configured_inputs()

        # Empty means "resolve the newest patch live", which is what a normal
        # (non-replay) build should do.
        self.assertEqual(configured.locked_zfs_version, "")


class LockFileReplayValidationTests(unittest.TestCase):
    """
    Replay mode (`workflow_dispatch` with `use_input_lock=true`) is not part of
    the normal schedule/push run path, so a stale or malformed
    `ci/inputs.lock.json` is only ever caught by these guards on the rare run
    that actually replays it. Each one fails closed instead of silently
    proceeding with an unverified or mismatched input.
    """

    def test_load_lock_file_missing_path_raises(self) -> None:
        with self.assertRaises(CiToolError):
            _load_lock_file("/nonexistent/inputs.lock.json")

    def test_lock_replay_missing_base_image_raises(self) -> None:
        lock_payload = {
            "version": 1,
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "zfs_minor_version": "2.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()

    def test_lock_replay_base_image_placeholder_raises(self) -> None:
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@REPLACE_ME",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "zfs_minor_version": "2.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()

    def test_lock_replay_build_container_placeholder_raises(self) -> None:
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@REPLACE_ME",
            "zfs_minor_version": "2.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()

    def test_lock_replay_build_container_mismatch_raises(self) -> None:
        # The build container is no longer settable per run (it selects the
        # image for a privileged job), so a lock file pinned to a different
        # one than the current BUILD_CONTAINER_REF must fail the replay
        # instead of silently building with today's container.
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:other",
            "zfs_minor_version": "2.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock_payload), encoding="utf-8")
            env = {
                "USE_INPUT_LOCK": "true",
                "LOCK_FILE": str(lock_path),
                "BUILD_CONTAINER_REF": "ghcr.io/example/build@sha256:cafef00d",
                "DEFAULT_AKMODS_REF": "a" * 40,
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()


class DetectBaseImageKernelReleasesTests(unittest.TestCase):
    """
    The `/lib/modules` probe runs on every scheduled build, but only its happy
    path does. The empty-result guard fails closed so a base image that carries
    no kernel directory can never be pinned as "the supported primary kernel",
    and nothing else in the pipeline re-checks that.
    """

    def test_returns_kernel_releases_in_natural_sort_order(self) -> None:
        with patch(
            "ci_tools.resolve_build_inputs.run_cmd",
            return_value="6.16.4-200.fc43.x86_64\n6.16.10-200.fc43.x86_64\n",
        ) as run_cmd_mock:
            detected = detect_base_image_kernel_releases("ghcr.io/example/base@sha256:deadbeef")

        self.assertEqual(
            detected,
            ["6.16.4-200.fc43.x86_64", "6.16.10-200.fc43.x86_64"],
        )
        argv = run_cmd_mock.call_args.args[0]
        # The probe must read the image's own filesystem, not a metadata label:
        # installonly kernels can leave more than one kernel in the merged root.
        self.assertIn("ghcr.io/example/base@sha256:deadbeef", argv)
        self.assertIn("/lib/modules", argv[-1])

    def test_empty_module_directory_listing_raises_with_image_ref(self) -> None:
        with (
            # `find ... -printf '%f\n'` prints nothing when no directory matches.
            patch("ci_tools.resolve_build_inputs.run_cmd", return_value=""),
            self.assertRaises(CiToolError) as caught,
        ):
            detect_base_image_kernel_releases("ghcr.io/example/base@sha256:deadbeef")

        self.assertEqual(
            str(caught.exception),
            "No installed kernel directories found in ghcr.io/example/base@sha256:deadbeef",
        )


class ResolveBuildInputsRegistryGuardTests(unittest.TestCase):
    """
    `resolve_build_inputs()` runs on every scheduled build, so its happy path is
    exercised in production daily. Its four registry guards are not: they only
    fire when skopeo returns a manifest missing a name, digest, or the
    `ostree.linux` label, which a green build never produces. Each one refuses
    to continue rather than pin an image by a value it could not read.
    """

    BASE_REF = "ghcr.io/example/kinoite:43"
    BUILD_REF = "ghcr.io/example/build:latest"
    BASE_DIGEST = "sha256:deadbeef"
    VERSION_LABEL = "43.20260901.1"

    def _env(self, **overrides: str) -> dict:
        env = {
            "USE_INPUT_LOCK": "false",
            "LOCK_FILE": "ci/inputs.lock.json",
            "BUILD_CONTAINER_REF": self.BUILD_REF,
            "DEFAULT_BASE_IMAGE": self.BASE_REF,
            "DEFAULT_ZFS_MINOR_VERSION": "2.4",
            "DEFAULT_AKMODS_REF": "a" * 40,
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "",
            "AKMODS_UPSTREAM_REPO": "",
        }
        env.update(overrides)
        return env

    def _base_inspect(self, **overrides) -> dict:
        payload = {
            "Name": "ghcr.io/example/kinoite",
            "Digest": self.BASE_DIGEST,
            "Labels": {
                "ostree.linux": "6.16.10-200.fc43.x86_64",
                "org.opencontainers.image.version": self.VERSION_LABEL,
            },
        }
        payload.update(overrides)
        return payload

    def _resolve(self, *, base_inspect: dict, build_inspect: dict):
        def inspect_json(ref: str) -> dict:
            return base_inspect if "kinoite" in ref else build_inspect

        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.skopeo_inspect_json", side_effect=inspect_json),
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_digest",
                side_effect=lambda ref: (
                    self.BASE_DIGEST if ref.endswith(f":{self.VERSION_LABEL}") else "sha256:other"
                ),
            ),
            patch(
                "ci_tools.resolve_build_inputs.run_cmd",
                return_value="6.16.4-200.fc43.x86_64\n6.16.10-200.fc43.x86_64\n",
            ),
            patch(
                "ci_tools.resolve_build_inputs.resolve_latest_zfs_version",
                return_value="2.4.1",
            ),
        ):
            return resolve_build_inputs()

    def _build_inspect(self) -> dict:
        return {"Name": "ghcr.io/example/build", "Digest": "sha256:cafef00d"}

    def test_resolves_pinned_refs_and_newest_kernel_on_the_happy_path(self) -> None:
        resolution = self._resolve(
            base_inspect=self._base_inspect(),
            build_inspect=self._build_inspect(),
        )
        inputs = resolution.inputs

        self.assertEqual(inputs.base_image_pinned, f"ghcr.io/example/kinoite@{self.BASE_DIGEST}")
        self.assertEqual(inputs.base_image_tag, self.VERSION_LABEL)
        self.assertEqual(inputs.build_container_pinned, "ghcr.io/example/build@sha256:cafef00d")
        # The newest installed kernel wins, not the label, and not list order.
        self.assertEqual(inputs.kernel_release, "6.16.10-200.fc43.x86_64")
        self.assertEqual(
            inputs.detected_kernel_releases,
            ("6.16.4-200.fc43.x86_64", "6.16.10-200.fc43.x86_64"),
        )
        self.assertEqual(inputs.version, "43")
        self.assertEqual(inputs.zfs_version, "2.4.1")
        self.assertEqual(resolution.label_kernel_release, "6.16.10-200.fc43.x86_64")

    def test_base_image_without_digest_raises_before_any_pinning(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                base_inspect=self._base_inspect(Digest=""),
                build_inspect=self._build_inspect(),
            )

        self.assertEqual(
            str(caught.exception),
            f"Failed to resolve base image digest for {self.BASE_REF}",
        )

    def test_base_image_without_name_raises_before_any_pinning(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                base_inspect=self._base_inspect(Name=""),
                build_inspect=self._build_inspect(),
            )

        self.assertEqual(
            str(caught.exception),
            f"Failed to resolve base image digest for {self.BASE_REF}",
        )

    def test_base_image_without_ostree_linux_label_raises(self) -> None:
        # Without this label there is no declared kernel to compare the
        # detected ones against, so the run cannot report a label/directory
        # mismatch at all.
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                base_inspect=self._base_inspect(
                    Labels={"org.opencontainers.image.version": self.VERSION_LABEL}
                ),
                build_inspect=self._build_inspect(),
            )

        self.assertEqual(
            str(caught.exception),
            f"Failed to read ostree.linux label from {self.BASE_REF}",
        )

    def test_build_container_without_digest_raises(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                base_inspect=self._base_inspect(),
                build_inspect={"Name": "ghcr.io/example/build", "Digest": ""},
            )

        self.assertEqual(
            str(caught.exception),
            f"Failed to resolve build container digest for {self.BUILD_REF}",
        )


class AkmodsRepoUrlRequiredTests(unittest.TestCase):
    """
    A non-SHA akmods ref has to be resolved through `git ls-remote`, which needs
    a repository URL. Both guards keep an unresolvable ref from reaching the
    build as if it were a commit.
    """

    def _env(self, **overrides: str) -> dict:
        wipe = {
            "DEFAULT_AKMODS_REF": "",
            "AKMODS_UPSTREAM_REF": "",
            "AKMODS_UPSTREAM_TRACK": "",
            "AKMODS_UPSTREAM_REPO": "",
        }
        wipe.update(overrides)
        return wipe

    def test_non_sha_ref_without_repo_url_raises(self) -> None:
        defaults = {"AKMODS_UPSTREAM_REF": "", "AKMODS_UPSTREAM_TRACK": "", "AKMODS_UPSTREAM_REPO": ""}
        with (
            patch.dict(os.environ, self._env(AKMODS_UPSTREAM_REF="main"), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
            self.assertRaises(CiToolError) as caught,
        ):
            _resolve_default_akmods_ref()

        self.assertEqual(
            str(caught.exception),
            "AKMODS_UPSTREAM_REPO is required to resolve non-SHA AKMODS_UPSTREAM_REF",
        )
        ls_remote.assert_not_called()

    def test_tracking_ref_without_repo_url_raises(self) -> None:
        defaults = {"AKMODS_UPSTREAM_REF": "", "AKMODS_UPSTREAM_TRACK": "main", "AKMODS_UPSTREAM_REPO": ""}
        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
            self.assertRaises(CiToolError) as caught,
        ):
            _resolve_default_akmods_ref()

        self.assertEqual(
            str(caught.exception),
            "AKMODS_UPSTREAM_REPO is required to resolve AKMODS_UPSTREAM_TRACK",
        )
        ls_remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
