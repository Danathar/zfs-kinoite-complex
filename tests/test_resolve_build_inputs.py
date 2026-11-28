"""
Script: tests/test_resolve_build_inputs.py
What: Tests for input-resolution tag selection.
Doing: Checks immutable-tag reuse, candidate-tag derivation, and failure paths.
Why: Protects the logic that pins run inputs and avoids moving-tag drift.
Goal: Keep input resolution predictable and explainable.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import (
    REGISTRY_RETRY_ATTEMPTS,
    REGISTRY_TRANSFER_TIMEOUT,
    CiToolError,
    sort_kernel_releases,
)
from ci_tools.resolve_build_inputs import (
    HELD_BASE_PACKAGE,
    KERNEL_HOLD_MAX_DAYS,
    BuildInputResolution,
    HeldBaseCopiesUnreadable,
    ResolvedBuildInputs,
    _load_lock_file,
    _resolve_default_akmods_ref,
    _utc_today,
    choose_base_image_tag,
    dated_base_tags_newest_first,
    dated_base_version_key,
    detect_base_image_kernel_releases,
    extract_source_tag,
    find_held_base,
    held_base_repository,
    kernel_exceeds_maximum,
    latest_zfs_version_label,
    list_base_image_tags,
    main,
    resolve_build_inputs,
    resolve_configured_inputs,
    trusted_held_copy_digest,
)
from ci_tools.zfs_release import ZfsSelection


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
    The resolver uses a lock file's akmods_upstream_ref ahead of every other
    source, so a value left in the template would silently override
    ci/defaults.json on every later replay.
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
            "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()

    def test_lock_replay_brew_image_placeholder_raises(self) -> None:
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "brew_image": "ghcr.io/example/brew@REPLACE_ME",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                self.assertRaises(CiToolError),
            ):
                resolve_configured_inputs()

    def test_lock_replay_uses_the_locked_brew_image(self) -> None:
        # A replay that pins brew_image must build with that exact payload, not
        # today's DEFAULT_BREW_IMAGE, or the "replay" ships different content
        # in / than the run it claims to reproduce.
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "brew_image": "ghcr.io/example/brew@sha256:locked",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
            }
            with patch.dict(os.environ, env, clear=False):
                configured = resolve_configured_inputs()

        self.assertEqual(configured.brew_image_ref, "ghcr.io/example/brew@sha256:locked")

    def test_lock_replay_empty_brew_image_falls_back_to_default(self) -> None:
        lock_payload = {
            "version": 1,
            "base_image": "ghcr.io/example/base@sha256:deadbeef",
            "build_container": "ghcr.io/example/build@sha256:cafef00d",
            "brew_image": "",
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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
            }
            with patch.dict(os.environ, env, clear=False):
                configured = resolve_configured_inputs()

        self.assertEqual(configured.brew_image_ref, "ghcr.io/example/brew@sha256:beefcafe")

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
                "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
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
        with (
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries"),
            patch(
                "ci_tools.resolve_build_inputs.run_cmd",
                return_value="6.16.4-200.fc43.x86_64\n6.16.10-200.fc43.x86_64\n",
            ) as run_cmd_mock,
        ):
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

    def test_a_private_copy_is_pulled_with_an_auth_file_never_the_token_in_argv(self) -> None:
        seen: dict = {}

        def pull(argv, **_kwargs):
            seen["argv"] = list(argv)
            seen["auths"] = json.loads(
                Path(argv[argv.index("--authfile") + 1]).read_text(encoding="utf-8")
            )["auths"]

        with (
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries", side_effect=pull),
            patch(
                "ci_tools.resolve_build_inputs.run_cmd", return_value="7.2.15-200.fc44.x86_64\n"
            ),
        ):
            detect_base_image_kernel_releases(
                "ghcr.io/example/held-base@sha256:deadbeef", creds="actor:secret-token"
            )
        self.assertEqual(list(seen["auths"]), ["ghcr.io"])
        self.assertNotIn("secret-token", " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][-1], "ghcr.io/example/held-base@sha256:deadbeef")

    def test_an_upstream_base_is_pulled_without_any_auth_file(self) -> None:
        with (
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries") as pull,
            patch(
                "ci_tools.resolve_build_inputs.run_cmd", return_value="7.2.15-200.fc44.x86_64\n"
            ),
        ):
            detect_base_image_kernel_releases("quay.io/example/base@sha256:deadbeef")
        self.assertNotIn("--authfile", pull.call_args.args[0])

    def test_empty_module_directory_listing_raises_with_image_ref(self) -> None:
        with (
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries"),
            # `find ... -printf '%f\n'` prints nothing when no directory matches.
            patch("ci_tools.resolve_build_inputs.run_cmd", return_value=""),
            self.assertRaises(CiToolError) as caught,
        ):
            detect_base_image_kernel_releases("ghcr.io/example/base@sha256:deadbeef")

        self.assertEqual(
            str(caught.exception),
            "No installed kernel directories found in ghcr.io/example/base@sha256:deadbeef",
        )

    def test_the_image_is_pulled_through_the_retrying_wrapper_before_the_probe(self) -> None:
        # The regression this guards: `podman run` pulling the image implicitly,
        # with nothing retrying the transfer. One truncated blob from quay.io's
        # CDN then ends the build at its first step (run 34266369977).
        with (
            patch(
                "ci_tools.resolve_build_inputs.run_cmd_with_retries",
                return_value="",
            ) as pull_mock,
            patch(
                "ci_tools.resolve_build_inputs.run_cmd",
                return_value="6.16.4-200.fc43.x86_64\n",
            ) as run_cmd_mock,
        ):
            detect_base_image_kernel_releases("ghcr.io/example/base@sha256:deadbeef")

        pull_argv = pull_mock.call_args.args[0]
        self.assertEqual(pull_argv[:2], ["podman", "pull"])
        # Pinned by digest, and the same reference the probe then runs against,
        # so the retried transfer cannot fetch one image and the probe read
        # another.
        self.assertEqual(pull_argv[-1], "ghcr.io/example/base@sha256:deadbeef")
        self.assertIn("ghcr.io/example/base@sha256:deadbeef", run_cmd_mock.call_args.args[0])
        self.assertEqual(
            pull_mock.call_args.kwargs,
            {
                "capture_output": False,
                "timeout": REGISTRY_TRANSFER_TIMEOUT / REGISTRY_RETRY_ATTEMPTS,
            },
        )
        # Belt and braces: podman retries within the invocation as well.
        self.assertEqual(
            pull_argv[pull_argv.index("--retry") + 1],
            str(REGISTRY_RETRY_ATTEMPTS),
        )

    def test_a_pull_that_exhausts_its_retries_raises_without_probing(self) -> None:
        # A failed transfer must not fall through to `podman run` against an
        # image that is not there: that turns a clear transfer error into an
        # obscure one, which is what the log of run 34266369977 shows.
        with (
            patch(
                "ci_tools.resolve_build_inputs.run_cmd_with_retries",
                side_effect=CiToolError("Command failed after 3 attempts: unexpected EOF"),
            ),
            patch("ci_tools.resolve_build_inputs.run_cmd") as run_cmd_mock,
            self.assertRaises(CiToolError) as caught,
        ):
            detect_base_image_kernel_releases("ghcr.io/example/base@sha256:deadbeef")

        self.assertIn("unexpected EOF", str(caught.exception))
        run_cmd_mock.assert_not_called()


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
    BREW_REF = "ghcr.io/example/brew@sha256:beefcafe"
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
            "DEFAULT_BREW_IMAGE": self.BREW_REF,
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

    def _resolve(self, *, base_inspect: dict, build_inspect: dict, brew_inspect: dict | None = None):
        if brew_inspect is None:
            brew_inspect = self._brew_inspect()

        def inspect_json(ref: str) -> dict:
            if "kinoite" in ref:
                return base_inspect
            if "brew" in ref:
                return brew_inspect
            return build_inspect

        with (
            patch.dict(os.environ, self._env(), clear=False),
            patch("ci_tools.resolve_build_inputs.skopeo_inspect_json", side_effect=inspect_json),
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_digest",
                side_effect=lambda ref: (
                    self.BASE_DIGEST if ref.endswith(f":{self.VERSION_LABEL}") else "sha256:other"
                ),
            ),
            # The base-image pull is a real registry transfer, stubbed here for
            # the same reason skopeo is: these cases are about the guards, not
            # about the network.
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries", return_value=""),
            patch(
                "ci_tools.resolve_build_inputs.run_cmd",
                return_value="6.16.4-200.fc43.x86_64\n6.16.10-200.fc43.x86_64\n",
            ),
            patch(
                "ci_tools.resolve_build_inputs.select_zfs_version",
                return_value=ZfsSelection(
                    version="2.4.1", line="2.4", floor_line="2.4", latest_line=""
                ),
            ),
            patch("ci_tools.resolve_build_inputs.latest_zfs_version_label", return_value=""),
            # 6.16 is inside this limit, so these cases never hold the base.
            patch("ci_tools.resolve_build_inputs.resolve_linux_maximum", return_value="6.17"),
        ):
            return resolve_build_inputs()

    def _build_inspect(self) -> dict:
        return {"Name": "ghcr.io/example/build", "Digest": "sha256:cafef00d"}

    def _brew_inspect(self) -> dict:
        return {"Name": "ghcr.io/example/brew", "Digest": "sha256:beefcafe"}

    def test_resolves_pinned_refs_and_newest_kernel_on_the_happy_path(self) -> None:
        resolution = self._resolve(
            base_inspect=self._base_inspect(),
            build_inspect=self._build_inspect(),
        )
        inputs = resolution.inputs

        self.assertEqual(inputs.base_image_pinned, f"ghcr.io/example/kinoite@{self.BASE_DIGEST}")
        self.assertEqual(inputs.base_image_tag, self.VERSION_LABEL)
        self.assertEqual(inputs.build_container_pinned, "ghcr.io/example/build@sha256:cafef00d")
        self.assertEqual(inputs.brew_image_pinned, "ghcr.io/example/brew@sha256:beefcafe")
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

    def test_brew_image_without_digest_raises(self) -> None:
        # The brew payload lands in the published image's root, so a ref this
        # run cannot pin to a digest must stop the run, not fall through to
        # whatever the Containerfile's floating default points at.
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                base_inspect=self._base_inspect(),
                build_inspect=self._build_inspect(),
                brew_inspect={"Name": "ghcr.io/example/brew", "Digest": ""},
            )

        self.assertEqual(
            str(caught.exception),
            f"Failed to resolve brew image digest for {self.BREW_REF}",
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


class ResolveBuildInputsMainTests(unittest.TestCase):
    """
    Cover `main()`, the command `.github/workflows/build.yml` actually runs.

    The tests above call `resolve_build_inputs()` and inspect the returned
    dataclass. Nothing in them reaches `main()`, and `tests/e2e/` never invokes
    `resolve-build-inputs` (it needs a registry), so the step body that hands
    the resolution to `write_resolved_build_outputs` and reports it was covered
    by neither tier. Everything downstream in that workflow reads those step
    outputs, so a resolution that is computed correctly and then exported wrong
    is indistinguishable from one that was resolved wrong.
    """

    @staticmethod
    def _resolution(
        *,
        label_kernel_release: str = "6.17.4-200.fc43.x86_64",
        candidate_tags: tuple[str, ...] = ("latest-20260906", "latest"),
    ) -> BuildInputResolution:
        # Every field gets a value distinguishable from every other field, so a
        # print or an export that reads the neighbouring attribute is visible
        # rather than matching by coincidence.
        inputs = ResolvedBuildInputs(
            version="43.20260906.1",
            kernel_release="6.17.4-200.fc43.x86_64",
            detected_kernel_releases=("6.17.3-200.fc43.x86_64", "6.17.4-200.fc43.x86_64"),
            base_image_ref="ghcr.io/ublue-os/kinoite-main:latest-20260906",
            base_image_name="kinoite-main",
            base_image_tag="latest-20260906",
            base_image_pinned="ghcr.io/ublue-os/kinoite-main@sha256:base",
            base_image_digest="sha256:base",
            build_container_ref="quay.io/fedora/fedora:43",
            build_container_pinned="quay.io/fedora/fedora@sha256:builder",
            build_container_digest="sha256:builder",
            brew_image_ref="ghcr.io/ublue-os/brew@sha256:brewref",
            brew_image_pinned="ghcr.io/ublue-os/brew@sha256:brewpin",
            brew_image_digest="sha256:brewpin",
            zfs_minor_version="2.4",
            zfs_version="2.4.1",
            akmods_upstream_ref="0123456789abcdef0123456789abcdef01234567",
            use_input_lock=True,
            lock_file_path="ci/input-lock.json",
        )
        return BuildInputResolution(
            inputs=inputs,
            label_kernel_release=label_kernel_release,
            candidate_tags=candidate_tags,
        )

    def _run_main(self, resolution: BuildInputResolution) -> tuple[str, list]:
        """Run `main()` against `resolution`; return its stdout and export calls."""

        exported: list = []

        with (
            patch(
                "ci_tools.resolve_build_inputs.resolve_build_inputs",
                return_value=resolution,
            ),
            patch(
                "ci_tools.resolve_build_inputs.write_resolved_build_outputs",
                side_effect=lambda inputs: exported.append(inputs),
            ),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            main()

        return stdout.getvalue(), exported

    def test_the_resolved_inputs_are_the_object_handed_to_the_exporter(self) -> None:
        resolution = self._resolution()

        _stdout, exported = self._run_main(resolution)

        # Identity, not equality: this pins that `resolution.inputs` is what
        # gets exported, rather than some re-derived or default-constructed
        # object that happens to compare equal today.
        self.assertEqual(len(exported), 1)
        self.assertIs(exported[0], resolution.inputs)

    def test_the_log_names_the_pinned_refs_the_build_will_actually_use(self) -> None:
        stdout, _exported = self._run_main(self._resolution())

        # The pinned (digest) refs, not the floating ones: a reviewer reading
        # the job log has to be able to tell which image content was built
        # against, and `base_image_ref` sitting next to `base_image_pinned` in
        # the dataclass is an easy line to print by mistake.
        self.assertIn(
            "Resolved base image: ghcr.io/ublue-os/kinoite-main@sha256:base\n", stdout
        )
        self.assertIn("Resolved base image tag: kinoite-main:latest-20260906\n", stdout)
        self.assertIn(
            "Resolved build container: quay.io/fedora/fedora@sha256:builder\n", stdout
        )
        self.assertIn(
            "Resolved brew image: ghcr.io/ublue-os/brew@sha256:brewpin\n", stdout
        )
        self.assertIn("Supported primary kernel release: 6.17.4-200.fc43.x86_64\n", stdout)
        self.assertIn(
            "Detected kernel releases in base image: "
            "6.17.3-200.fc43.x86_64 6.17.4-200.fc43.x86_64\n",
            stdout,
        )
        self.assertIn("Fedora version: 43.20260906.1\n", stdout)
        self.assertIn("ZFS minor version: 2.4\n", stdout)
        self.assertIn("Resolved ZFS version: 2.4.1\n", stdout)

    def test_a_label_that_disagrees_with_the_newest_module_directory_is_reported(
        self,
    ) -> None:
        # The base image's own kernel label and the newest directory under
        # /usr/lib/modules can disagree after an upstream rebuild. The build
        # proceeds on the directory, so the mismatch has to be stated or the
        # akmods are silently built against a kernel the label does not name.
        stdout, _exported = self._run_main(
            self._resolution(label_kernel_release="6.17.3-200.fc43.x86_64")
        )

        self.assertIn(
            "Base image label/kernel directory mismatch: "
            "label=6.17.3-200.fc43.x86_64 newest_dir=6.17.4-200.fc43.x86_64\n",
            stdout,
        )

    def test_no_mismatch_is_reported_when_the_label_and_the_directory_agree(self) -> None:
        # The other half of the branch. Without this a mismatch warning printed
        # unconditionally would still pass the test above, and the warning would
        # stop meaning anything.
        stdout, _exported = self._run_main(self._resolution())

        self.assertNotIn("mismatch", stdout)

    def test_the_candidate_tags_that_were_checked_are_listed(self) -> None:
        stdout, _exported = self._run_main(self._resolution())

        self.assertIn("Base-tag candidates checked: latest-20260906 latest\n", stdout)

    def test_no_candidate_line_is_printed_when_no_tag_had_to_be_searched(self) -> None:
        # An explicitly pinned base image resolves without a search. Printing an
        # empty "candidates checked:" line there reads as "nothing matched".
        stdout, _exported = self._run_main(self._resolution(candidate_tags=()))

        self.assertNotIn("Base-tag candidates checked", stdout)


class KernelExceedsMaximumTests(unittest.TestCase):
    def test_compares_major_minor_only(self) -> None:
        cases = [
            ("7.2.15-200.fc44.x86_64", "7.2", False),
            ("7.2.0-100.fc44.x86_64", "7.2", False),
            ("7.3.4-200.fc44.x86_64", "7.2", True),
            ("6.19.3-200.fc44.x86_64", "7.2", False),
            ("8.0.1-200.fc45.x86_64", "7.9", True),
            # Numeric, not text: 7.10 is newer than 7.9.
            ("7.10.1-200.fc44.x86_64", "7.9", True),
        ]
        for kernel, maximum, expected in cases:
            with self.subTest(kernel=kernel, maximum=maximum):
                self.assertEqual(kernel_exceeds_maximum(kernel, maximum), expected)

    def test_an_unreadable_value_raises_instead_of_guessing(self) -> None:
        for kernel, maximum in (("", "7.2"), ("x7.3", "7.2"), ("7.3.4-200.fc44.x86_64", "")):
            with self.subTest(kernel=kernel, maximum=maximum), self.assertRaises(CiToolError):
                kernel_exceeds_maximum(kernel, maximum)


class DatedBaseTagTests(unittest.TestCase):
    TODAY = date(2026, 11, 18)

    def test_version_key_reads_the_dated_form_only(self) -> None:
        self.assertEqual(dated_base_version_key("44.20261110.0"), (44, date(2026, 11, 10), 0))
        self.assertEqual(dated_base_version_key("44.20261110.12"), (44, date(2026, 11, 10), 12))
        for value in ("44", "latest", "44.20261110.0-x86_64", "44.20261340.0", "44.2026111.0", ""):
            with self.subTest(value=value):
                self.assertIsNone(dated_base_version_key(value))

    def test_keeps_plain_tags_of_one_fedora_release_newest_first(self) -> None:
        tags = [
            "44",
            "latest",
            "44.20261110.0",
            "44.20261110.0-x86_64",
            "44.20261110.0-aarch64",
            "44.20261118.0",
            "44.20261110.1",
            "43.20261117.0",
            "45.20261117.0",
            "44.20261340.0",
        ]
        self.assertEqual(
            dated_base_tags_newest_first(
                tags, fedora_version="44", today=self.TODAY, lookback_days=30
            ),
            ["44.20261118.0", "44.20261110.1", "44.20261110.0"],
        )

    def test_a_tag_dated_after_today_is_never_kept(self) -> None:
        # No daily build can be dated in the future, so such a tag is planted or wrong, and
        # would otherwise outrank every real one. Today's own tag is kept.
        tags = ["44.20261119.0", "44.20271231.0", "44.20261118.0", "44.20261110.0"]
        self.assertEqual(
            dated_base_tags_newest_first(
                tags, fedora_version="44", today=self.TODAY, lookback_days=30
            ),
            ["44.20261118.0", "44.20261110.0"],
        )

    def test_the_lookback_window_includes_its_first_day_only(self) -> None:
        tags = ["44.20261019.0", "44.20261018.0"]
        self.assertEqual(
            dated_base_tags_newest_first(
                tags, fedora_version="44", today=self.TODAY, lookback_days=30
            ),
            ["44.20261019.0"],
        )


# The real kernels of these Kinoite 44 tags on 2026-11-18.
KINOITE_44_KERNELS = {
    "44.20261118.0": "7.3.4-200.fc44.x86_64",
    "44.20261117.0": "7.3.3-200.fc44.x86_64",
    "44.20261111.0": "7.3.3-200.fc44.x86_64",
    "44.20261110.0": "7.2.15-200.fc44.x86_64",
    "44.20261109.0": "7.2.15-200.fc44.x86_64",
}


class UtcTodayTests(unittest.TestCase):
    def test_is_the_utc_calendar_date(self) -> None:
        # The dated tags are stamped in UTC; near midnight a local date would be off by one.
        before = datetime.now(UTC).date()
        today = _utc_today()
        after = datetime.now(UTC).date()
        self.assertIn(today, {before, after})


def _copy_digest(tag: str) -> str:
    """A stable fake digest for the copy of one version tag."""
    return "sha256:" + hashlib.sha256(f"copy {tag}".encode()).hexdigest()


class FindHeldBaseTagTests(unittest.TestCase):
    TODAY = date(2026, 11, 18)
    NAME = "quay.io/fedora-ostree-desktops/kinoite"
    COPIES = "ghcr.io/danathar/zfs-kinoite-complex-held-base"

    def _find_in(
        self,
        kernels: dict[str, str],
        copies: dict[str, str] | None = None,
        linux_maximum: str = "7.2",
        today: date | None = None,
        trusted: str | Exception | None = None,
        copy_error: Exception | None = None,
    ) -> tuple[tuple[str, str, str], list[str]]:
        """
        `copies` maps a version tag to its kernel; each copy sits at `_copy_digest(tag)` and
        carries that tag as its version label. `trusted` is the version whose digest the signed
        `:latest` records, or the error that verifying it raises.
        """
        inspected: list[str] = []
        self.trusted_asked = 0
        copies = copies or {}

        def inspect(ref: str) -> dict:
            inspected.append(ref)
            repository, tag = ref.removeprefix("docker://").rsplit(":", 1)
            self.assertEqual(repository, self.NAME)
            kernel = kernels[tag]
            return {"Labels": {"ostree.linux": kernel} if kernel else {}}

        def inspect_copy(ref: str) -> dict:
            inspected.append(ref)
            repository, digest = ref.removeprefix("docker://").rsplit("@", 1)
            self.assertEqual(repository, self.COPIES)
            if copy_error is not None:
                raise copy_error
            for version, kernel in copies.items():
                if _copy_digest(version) == digest:
                    return {
                        "Labels": {
                            "ostree.linux": kernel,
                            "org.opencontainers.image.version": version,
                        }
                    }
            raise CiToolError(f"manifest unknown: {ref}")

        def trusted_copy_digest() -> str:
            self.trusted_asked += 1
            if isinstance(trusted, Exception):
                raise trusted
            if trusted is None:
                raise CiToolError("Cannot trust a held base copy: no signed :latest")
            return _copy_digest(trusted)

        found = find_held_base(
            base_image_name=self.NAME,
            copy_repository=self.COPIES,
            fedora_version="44",
            linux_maximum=linux_maximum,
            today=today or self.TODAY,
            list_tags=lambda name: ["44", *kernels],
            trusted_copy_digest=trusted_copy_digest,
            inspect=inspect,
            inspect_copy=inspect_copy,
        )
        return found, inspected

    def _find(self, kernels: dict[str, str], linux_maximum: str = "7.2") -> tuple[str, list[str]]:
        (repository, tag, trusted_digest), inspected = self._find_in(
            kernels, linux_maximum=linux_maximum
        )
        self.assertEqual(repository, self.NAME)
        self.assertEqual(trusted_digest, "")
        return tag, inspected

    def test_returns_the_newest_tag_whose_kernel_openzfs_supports(self) -> None:
        tag, inspected = self._find(KINOITE_44_KERNELS)
        self.assertEqual(tag, "44.20261110.0")
        # Newest first, and it stops at the first supported one.
        self.assertEqual(
            inspected,
            [
                f"docker://{self.NAME}:44.20261118.0",
                f"docker://{self.NAME}:44.20261117.0",
                f"docker://{self.NAME}:44.20261111.0",
                f"docker://{self.NAME}:44.20261110.0",
            ],
        )

    def test_a_tag_without_a_readable_kernel_label_is_skipped(self) -> None:
        kernels = dict(KINOITE_44_KERNELS) | {"44.20261110.0": ""}
        tag, _ = self._find(kernels)
        self.assertEqual(tag, "44.20261109.0")

    def test_a_future_dated_upstream_tag_is_ignored(self) -> None:
        kernels = dict(KINOITE_44_KERNELS) | {"44.20261201.0": "7.2.99-200.fc44.x86_64"}
        tag, inspected = self._find(kernels)
        self.assertEqual(tag, "44.20261110.0")
        self.assertNotIn(f"docker://{self.NAME}:44.20261201.0", inspected)

    def test_no_supported_base_in_the_window_raises(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._find_in(KINOITE_44_KERNELS, linux_maximum="7.1", trusted=None)
        self.assertIn("Cannot trust a held base copy", str(caught.exception))

    def test_the_hold_limit_is_ninety_days(self) -> None:
        # The owner's decision (#628). The gate reads the same constant; see
        # tests/test_check_stable_signal.py.
        self.assertEqual(KERNEL_HOLD_MAX_DAYS, 90)

    def test_a_supported_upstream_tag_wins_and_the_copy_is_never_consulted(self) -> None:
        # While any supported upstream tag remains it is used, so the first run, before the
        # copy package exists, never reads it and never needs a signed `:latest`.
        copies = {"44.20261110.0": "7.2.15-200.fc44.x86_64"}
        found, inspected = self._find_in(KINOITE_44_KERNELS, copies, trusted=None)
        self.assertEqual(found, (self.NAME, "44.20261110.0", ""))
        self.assertEqual(self.trusted_asked, 0)
        self.assertTrue(all(self.NAME in ref for ref in inspected))

    def test_the_copy_the_signed_latest_was_built_on_is_used_once_no_upstream_tag_qualifies(
        self,
    ) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        copies = {"44.20261110.0": "7.2.15-200.fc44.x86_64"}
        found, inspected = self._find_in(upstream, copies, trusted="44.20261110.0")
        self.assertEqual(
            found, (self.COPIES, "44.20261110.0", _copy_digest("44.20261110.0"))
        )
        # Looked up by the trusted digest, never by listing the package.
        self.assertEqual(
            inspected,
            [
                f"docker://{self.NAME}:44.20261118.0",
                f"docker://{self.COPIES}@{_copy_digest('44.20261110.0')}",
            ],
        )

    def test_a_planted_newer_tag_in_the_copy_package_is_refused(self) -> None:
        # Anything that can write the package can add a newer-looking supported tag. Only the
        # copy at the digest the signed `:latest` was built on is ever considered.
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        copies = {
            "44.20261110.0": "7.2.15-200.fc44.x86_64",
            "44.20261117.0": "7.2.99-200.fc44.x86_64",
        }
        found, inspected = self._find_in(upstream, copies, trusted="44.20261110.0")
        self.assertEqual(found[1], "44.20261110.0")
        self.assertNotIn(_copy_digest("44.20261117.0"), " ".join(inspected))

    def test_a_copy_that_the_signed_latest_was_not_built_on_is_not_used(self) -> None:
        # A copy exists, but `:latest` records another base (or none that was ever copied).
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        with self.assertRaises(CiToolError) as caught:
            self._find_in(
                upstream,
                {"44.20261112.0": "7.2.15-200.fc44.x86_64"},
                trusted="44.20261110.0",
            )
        message = str(caught.exception)
        self.assertIn("has no copy in", message)
        self.assertIn("never became :latest", message)
        self.assertNotIsInstance(caught.exception, HeldBaseCopiesUnreadable)

    def test_an_unverifiable_latest_fails_loudly_once_the_copy_is_needed(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        copies = {"44.20261110.0": "7.2.15-200.fc44.x86_64"}
        failure = CiToolError("Cannot trust a held base copy: the signature on :latest")
        with self.assertRaises(CiToolError) as caught:
            self._find_in(upstream, copies, trusted=failure)
        self.assertIn("Cannot trust a held base copy", str(caught.exception))

    def test_a_trusted_copy_older_than_the_hold_limit_is_never_used(self) -> None:
        # 2026-08-19 is 91 days before 2026-11-18; 2026-08-20 is exactly 90.
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        with self.assertRaises(CiToolError) as caught:
            self._find_in(
                upstream, {"44.20260819.0": "7.2.1-200.fc44.x86_64"}, trusted="44.20260819.0"
            )
        self.assertIn("outside the hold window", str(caught.exception))
        found, _ = self._find_in(
            upstream, {"44.20260820.0": "7.2.1-200.fc44.x86_64"}, trusted="44.20260820.0"
        )
        self.assertEqual(found[:2], (self.COPIES, "44.20260820.0"))

    def test_a_trusted_copy_dated_today_is_used(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        found, _ = self._find_in(
            upstream, {"44.20261118.0": "7.2.1-200.fc44.x86_64"}, trusted="44.20261118.0"
        )
        self.assertEqual(found[:2], (self.COPIES, "44.20261118.0"))

    def test_a_trusted_copy_dated_after_today_is_never_used(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        with self.assertRaises(CiToolError) as caught:
            self._find_in(
                upstream, {"44.20261119.0": "7.2.1-200.fc44.x86_64"}, trusted="44.20261119.0"
            )
        self.assertIn("outside the hold window", str(caught.exception))

    def test_a_trusted_copy_of_another_fedora_release_or_kernel_is_not_used(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        for version, kernel in (
            ("43.20261110.0", "7.2.15-200.fc43.x86_64"),
            ("44.20261110.0", "7.3.1-200.fc44.x86_64"),
            ("44.20261110.0", ""),
        ):
            with self.subTest(version=version, kernel=kernel), self.assertRaises(CiToolError):
                self._find_in(upstream, {version: kernel}, trusted=version)

    def test_a_copy_package_that_refuses_the_read_fails_naming_the_package_and_token(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        for message in ("denied: permission_denied", "403 Forbidden"):
            with self.subTest(message=message), self.assertRaises(HeldBaseCopiesUnreadable) as c:
                self._find_in(
                    upstream, {}, trusted="44.20261110.0", copy_error=CiToolError(message)
                )
            self.assertIn(self.COPIES, str(c.exception))
            self.assertIn("REGISTRY_TOKEN", str(c.exception))

    def test_another_registry_error_reading_the_copy_is_not_read_as_missing(self) -> None:
        upstream = {"44.20261118.0": "7.3.4-200.fc44.x86_64"}
        with self.assertRaises(CiToolError) as caught:
            self._find_in(
                upstream,
                {},
                trusted="44.20261110.0",
                copy_error=CiToolError("connection reset by peer"),
            )
        self.assertNotIsInstance(caught.exception, HeldBaseCopiesUnreadable)
        self.assertIn("connection reset", str(caught.exception))

    def test_walking_a_long_hold_forward_never_trips_the_promotion_check(self) -> None:
        # Kinoite builds a dated tag every day, and quay.io deletes it after 28 days. A hold
        # starts when 7.3 arrives on 2026-11-11. Each day the build picks a base, makes a copy
        # of it, and promotes against the previous day's `:latest`. The pick must never be older
        # than what `:latest` is already on, or `refuse_held_base_older_than_latest` would refuse
        # the promotion and the hold would stall. Once upstream loses the held base, the pick is
        # the copy the signed `:latest` was built on, which is that same base.
        from ci_tools.promote_stable import (
            KERNEL_HELD_FROM_LABEL,
            refuse_held_base_older_than_latest,
        )

        def tag_on(day: date) -> str:
            return f"44.{day:%Y%m%d}.0"

        def kernel_on(day: date) -> str:
            return "7.3.4-200.fc44.x86_64" if day >= date(2026, 11, 11) else "7.2.15-200.fc44.x86_64"

        first = date(2026, 9, 1)
        copies: dict[str, str] = {}
        latest_labels: dict | None = None
        picked_from_copy = 0
        for offset in range(75):
            today = date(2026, 11, 11) + timedelta(days=offset)
            upstream = {
                tag_on(day): kernel_on(day)
                for day in (first + timedelta(days=n) for n in range((today - first).days + 1))
                if (today - day).days <= 28
            }
            trusted = latest_labels["org.opencontainers.image.version"] if latest_labels else None
            (repository, tag, _), _ = self._find_in(upstream, copies, today=today, trusted=trusted)
            if repository == self.COPIES:
                picked_from_copy += 1
            copies.setdefault(tag, "7.2.15-200.fc44.x86_64")
            candidate_labels = {
                KERNEL_HELD_FROM_LABEL: "7.3.4-200.fc44.x86_64",
                "org.opencontainers.image.version": tag,
            }
            refuse_held_base_older_than_latest(
                candidate_labels=candidate_labels, latest_labels=latest_labels
            )
            latest_labels = candidate_labels
        # The walk really did run past the point where upstream lost the held base.
        self.assertGreater(picked_from_copy, 0)
        self.assertEqual(latest_labels["org.opencontainers.image.version"], "44.20261110.0")

    def test_the_copy_repository_is_the_owners_held_base_package(self) -> None:
        self.assertEqual(held_base_repository("danathar"), self.COPIES)

    # -- the trusted record a copy is checked against ----------------------

    LATEST_DIGEST = "sha256:" + "a" * 64
    BASE_DIGEST = "sha256:" + "2" * 64

    def _trust(self, *, latest="default", cosign_error=None, creds="actor:secret-token"):
        if latest == "default":
            latest = {
                "Digest": self.LATEST_DIGEST,
                "Labels": {
                    "org.zfs-kinoite-complex.base-image": (
                        f"quay.io/fedora-ostree-desktops/kinoite@{self.BASE_DIGEST}"
                    )
                },
            }
        with (
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_json_optional", return_value=latest
            ) as inspect,
            patch(
                "ci_tools.resolve_build_inputs.cosign_verify", side_effect=cosign_error
            ) as cosign,
        ):
            try:
                return (
                    trusted_held_copy_digest(
                        image_org="danathar",
                        image_name="zfs-kinoite-complex",
                        creds=creds,
                        key_path="/keys/cosign.pub",
                    ),
                    inspect,
                    cosign,
                )
            except CiToolError as exc:
                self.failure = exc
                self.inspect, self.cosign = inspect, cosign
                raise

    def test_the_trusted_digest_is_the_base_the_verified_latest_was_built_on(self) -> None:
        digest, inspect, cosign = self._trust()
        self.assertEqual(digest, self.BASE_DIGEST)
        inspect.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex:latest", creds="actor:secret-token"
        )
        # Verified by the digest that was read, so the labels are those of the signed image.
        cosign.assert_called_once_with(
            f"ghcr.io/danathar/zfs-kinoite-complex@{self.LATEST_DIGEST}",
            key_path="/keys/cosign.pub",
            creds="actor:secret-token",
        )

    def test_the_committed_public_key_is_the_default_key(self) -> None:
        with (
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_json_optional",
                return_value={
                    "Digest": self.LATEST_DIGEST,
                    "Labels": {
                        "org.zfs-kinoite-complex.base-image": f"x/y@{self.BASE_DIGEST}"
                    },
                },
            ),
            patch("ci_tools.resolve_build_inputs.cosign_verify") as cosign,
        ):
            trusted_held_copy_digest(image_org="danathar", image_name="n", creds="a:t")
        self.assertTrue(cosign.call_args.kwargs["key_path"].endswith("/cosign.pub"))

    def test_an_unsigned_or_unverifiable_latest_is_not_trusted(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._trust(cosign_error=CiToolError("no signatures found"))
        message = str(caught.exception)
        self.assertIn("did not verify against cosign.pub", message)
        self.assertIn("no signatures found", message)

    def test_a_missing_latest_is_not_trusted(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._trust(latest=None)
        self.assertIn("does not exist", str(caught.exception))
        self.cosign.assert_not_called()

    def test_a_latest_that_records_no_base_digest_is_not_trusted(self) -> None:
        for recorded in ("", "quay.io/fedora-ostree-desktops/kinoite:44", "x@sha256:short"):
            with self.subTest(recorded=recorded), self.assertRaises(CiToolError) as caught:
                self._trust(
                    latest={
                        "Digest": self.LATEST_DIGEST,
                        "Labels": {"org.zfs-kinoite-complex.base-image": recorded},
                    }
                )
            self.assertIn("no usable", str(caught.exception))

    def test_an_unreadable_latest_is_reported_not_read_as_missing(self) -> None:
        with (
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_json_optional",
                side_effect=CiToolError("connection reset by peer"),
            ),
            self.assertRaises(CiToolError) as caught,
        ):
            trusted_held_copy_digest(image_org="danathar", image_name="n", creds="a:t")
        self.assertIn("reading ghcr.io/danathar/n:latest failed", str(caught.exception))

    def test_trust_without_a_token_fails_naming_it(self) -> None:
        with self.assertRaises(HeldBaseCopiesUnreadable) as caught:
            self._trust(creds=None)
        self.assertIn("REGISTRY_TOKEN", str(caught.exception))
        self.assertIn("fork", str(caught.exception))
        self.inspect.assert_not_called()

    def test_a_copy_tag_maps_through_choose_base_image_tag(self) -> None:
        # The copy is tagged with the version label exactly, which is the first candidate the
        # held path tries in the copy repository.
        digest = "sha256:" + "2" * 64
        tag, checked = choose_base_image_tag(
            source_tag="44",
            version_label="44.20261110.0",
            fedora_version="44",
            expected_digest=digest,
            digest_lookup=lambda candidate: digest if candidate == "44.20261110.0" else "",
        )
        self.assertEqual(tag, "44.20261110.0")
        self.assertEqual(checked[0], "44.20261110.0")

    def test_lists_the_base_repository_tags_with_skopeo_anonymously(self) -> None:
        with patch(
            "ci_tools.resolve_build_inputs.run_json_cmd",
            return_value={"Repository": self.NAME, "Tags": ["44", "44.20261110.0"]},
        ) as run_json_cmd:
            tags = list_base_image_tags(self.NAME)
        self.assertEqual(tags, ["44", "44.20261110.0"])
        self.assertEqual(
            run_json_cmd.call_args.args[0], ["skopeo", "list-tags", f"docker://{self.NAME}"]
        )

    def test_a_listing_without_tags_fails_closed(self) -> None:
        with (
            patch("ci_tools.resolve_build_inputs.run_json_cmd", return_value={"Repository": "x"}),
            self.assertRaises(CiToolError),
        ):
            list_base_image_tags(self.NAME)


HOLD_NEWEST_DIGEST = "sha256:" + "1" * 64
HOLD_HELD_DIGEST = "sha256:" + "2" * 64
HOLD_LATEST_DIGEST = "sha256:" + "a" * 64
# tag -> (ostree.linux, org.opencontainers.image.version, digest)
HOLD_TAGS = {
    "44": ("7.3.4-200.fc44.x86_64", "44.20261118.0", HOLD_NEWEST_DIGEST),
    "44.20261118.0": ("7.3.4-200.fc44.x86_64", "44.20261118.0", HOLD_NEWEST_DIGEST),
    "44.20261117.0": ("7.3.3-200.fc44.x86_64", "44.20261117.0", "sha256:" + "3" * 64),
    "44.20261110.0": ("7.2.15-200.fc44.x86_64", "44.20261110.0", HOLD_HELD_DIGEST),
}
# quay.io has deleted the held tag, and this repository's copy of it is all that is left.
HOLD_GONE = {tag: value for tag, value in HOLD_TAGS.items() if tag != "44.20261110.0"}
HOLD_HELD_COPY = {"44.20261110.0": HOLD_TAGS["44.20261110.0"]}


class ResolveBuildInputsKernelHoldTests(unittest.TestCase):
    """
    `resolve_build_inputs()` with the newest base on a kernel OpenZFS does not support.

    The registry below is Kinoite 44 as it was on 2026-11-18: the `:44` tag and its newest
    dated tag carry 7.3.4, the last dated tag with a 7.2 kernel is 44.20261110.0, and OpenZFS
    2.4.4 declares `Linux-Maximum: 7.2`.
    """

    NAME = "quay.io/fedora-ostree-desktops/kinoite"
    BASE_REF = f"{NAME}:44"
    NEWEST_DIGEST = HOLD_NEWEST_DIGEST
    HELD_DIGEST = HOLD_HELD_DIGEST

    COPIES = "ghcr.io/danathar/zfs-kinoite-complex-held-base"

    def _resolve(
        self,
        *,
        tags: dict | None = None,
        copies: dict | None = None,
        trusted: str | None = "44.20261110.0",
        cosign_error: Exception | None = None,
        copy_inspect_error: Exception | None = None,
        copy_digest_error: Exception | None = None,
        linux_maximum: str = "7.2",
        installed: str = "7.2.15-200.fc44.x86_64\n",
        env: dict | None = None,
        token: bool = True,
        selected: str = "2.4.4",
        latest_zfs_version: str = "2.4.4",
    ):
        """
        `copies` is the private package, tag -> (kernel, version, digest). `trusted` is the
        version the signed `:latest` was built on; None means `:latest` does not exist.
        """
        tags = HOLD_TAGS if tags is None else tags
        copies = {} if copies is None else copies
        self.pulled: list[str] = []
        self.pull_auth: list[dict | None] = []
        self.listed = 0
        # Every registry read of the copy package, as (call, credentialed).
        self.copy_reads: list[tuple[str, bool]] = []
        self.upstream_reads_with_token: list[str] = []
        self.latest_reads = 0
        self.cosign_calls: list[str] = []

        def inspect_json(ref: str, *, creds: str | None = None) -> dict:
            if "build" in ref:
                return {"Name": "ghcr.io/example/build", "Digest": "sha256:cafef00d"}
            if "brew" in ref:
                return {"Name": "ghcr.io/example/brew", "Digest": "sha256:beefcafe"}
            if "@" in ref:
                # The copy, looked up by the digest the signed :latest records.
                repository, digest = ref.removeprefix("docker://").rsplit("@", 1)
                self.assertEqual(repository, self.COPIES)
                self.copy_reads.append((f"inspect @{digest[:14]}", creds is not None))
                if copy_inspect_error is not None:
                    raise copy_inspect_error
                for kernel, version, copy_digest in copies.values():
                    if copy_digest == digest:
                        return {
                            "Name": repository,
                            "Digest": digest,
                            "Labels": {
                                "ostree.linux": kernel,
                                "org.opencontainers.image.version": version,
                            },
                        }
                raise CiToolError(f"manifest unknown: {ref}")
            repository, tag = ref.removeprefix("docker://").rsplit(":", 1)
            if repository == self.COPIES:
                self.copy_reads.append((f"inspect {tag}", creds is not None))
            elif creds is not None:
                self.upstream_reads_with_token.append(ref)
            kernel, version, digest = (copies if repository == self.COPIES else tags)[tag]
            return {
                "Name": repository,
                "Digest": digest,
                "Labels": {"ostree.linux": kernel, "org.opencontainers.image.version": version},
            }

        def inspect_optional(ref: str, *, creds: str | None = None) -> dict | None:
            self.latest_reads += 1
            self.assertEqual(ref, "docker://ghcr.io/danathar/zfs-kinoite-complex:latest")
            self.assertIsNotNone(creds, ":latest is read with the job's token")
            if trusted is None:
                return None
            return {
                "Digest": HOLD_LATEST_DIGEST,
                "Labels": {
                    "org.zfs-kinoite-complex.base-image": (
                        f"{self.NAME}@{HOLD_TAGS[trusted][2]}"
                    )
                },
            }

        def cosign(ref: str, **kwargs) -> None:
            self.cosign_calls.append(ref)
            self.assertTrue(kwargs["key_path"].endswith("cosign.pub"))
            if cosign_error is not None:
                raise cosign_error

        def inspect_digest(ref: str, *, creds: str | None = None) -> str:
            repository, tag = ref.removeprefix("docker://").rsplit(":", 1)
            if repository == self.COPIES:
                self.copy_reads.append((f"digest {tag}", creds is not None))
                if copy_digest_error is not None:
                    raise copy_digest_error
                return copies[tag][2]
            if creds is not None:
                self.upstream_reads_with_token.append(ref)
            if tag not in tags:
                raise CiToolError(f"manifest unknown: {ref}")
            return tags[tag][2]

        def select(**kwargs) -> ZfsSelection:
            # The selection is asked about the newest base's kernel, never a held one.
            self.assertEqual(kwargs["kernel_release"], tags[self.BASE_REF.rsplit(":", 1)[1]][0])
            self.assertEqual(kwargs["latest_zfs_version"], latest_zfs_version)
            return ZfsSelection(
                version=selected,
                line=selected.rsplit(".", 1)[0],
                floor_line="2.4",
                latest_line=latest_zfs_version.rsplit(".", 1)[0],
            )

        def list_tags(name: str) -> list[str]:
            self.assertNotEqual(name, self.COPIES, "the copy package is never listed")
            self.listed += 1
            return list(tags)

        def pull(args, **kwargs) -> str:
            self.pulled.append(args[-1])
            auth = None
            if "--authfile" in args:
                auth = json.loads(
                    Path(args[args.index("--authfile") + 1]).read_text(encoding="utf-8")
                )["auths"]
                self.assertNotIn("secret-token", " ".join(args))
            self.pull_auth.append(auth)
            return ""

        base_env = {
            "USE_INPUT_LOCK": "false",
            "LOCK_FILE": "ci/inputs.lock.json",
            "BUILD_CONTAINER_REF": "ghcr.io/example/build:latest",
            "DEFAULT_BASE_IMAGE": self.BASE_REF,
            "DEFAULT_ZFS_MINOR_VERSION": "2.4",
            "DEFAULT_AKMODS_REF": "a" * 40,
            "DEFAULT_BREW_IMAGE": "ghcr.io/example/brew@sha256:beefcafe",
            "GITHUB_REPOSITORY_OWNER": "Danathar",
            "IMAGE_NAME": "zfs-kinoite-complex",
            "REGISTRY_ACTOR": "actor" if token else "",
            "REGISTRY_TOKEN": "secret-token" if token else "",
        }
        with (
            patch.dict(os.environ, base_env | (env or {}), clear=False),
            patch("ci_tools.resolve_build_inputs.skopeo_inspect_json", side_effect=inspect_json),
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_json_optional",
                side_effect=inspect_optional,
            ),
            patch("ci_tools.resolve_build_inputs.cosign_verify", side_effect=cosign),
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_digest", side_effect=inspect_digest
            ),
            patch("ci_tools.resolve_build_inputs.list_base_image_tags", side_effect=list_tags),
            patch("ci_tools.resolve_build_inputs.run_cmd_with_retries", side_effect=pull),
            patch("ci_tools.resolve_build_inputs.run_cmd", return_value=installed),
            patch(
                "ci_tools.resolve_build_inputs.select_zfs_version", side_effect=select
            ) as self.select,
            patch(
                "ci_tools.resolve_build_inputs.latest_zfs_version_label",
                return_value=latest_zfs_version,
            ),
            patch(
                "ci_tools.resolve_build_inputs.resolve_latest_zfs_version", return_value="2.4.4"
            ),
            patch(
                "ci_tools.resolve_build_inputs.resolve_linux_maximum", return_value=linux_maximum
            ) as self.linux_maximum,
            patch(
                "ci_tools.resolve_build_inputs._utc_today", return_value=date(2026, 11, 18)
            ),
        ):
            return resolve_build_inputs()

    def test_an_unsupported_newest_kernel_holds_the_base_on_the_last_supported_one(self) -> None:
        resolution = self._resolve()
        inputs = resolution.inputs
        self.assertEqual(inputs.kernel_held_from, "7.3.4-200.fc44.x86_64")
        self.assertEqual(inputs.base_image_pinned, f"{self.NAME}@{self.HELD_DIGEST}")
        self.assertEqual(inputs.base_image_digest, self.HELD_DIGEST)
        self.assertEqual(inputs.base_image_tag, "44.20261110.0")
        self.assertEqual(inputs.kernel_release, "7.2.15-200.fc44.x86_64")
        self.assertEqual(inputs.version, "44")
        self.assertEqual(resolution.label_kernel_release, "7.2.15-200.fc44.x86_64")
        # The floating ref is still recorded as what was asked for.
        self.assertEqual(inputs.base_image_ref, self.BASE_REF)
        self.linux_maximum.assert_called_once_with("2.4.4")
        # Only the held base is pulled: the newest one is never downloaded.
        self.assertEqual(self.pulled, [f"{self.NAME}@{self.HELD_DIGEST}"])

    def test_a_supported_newest_kernel_is_not_held(self) -> None:
        resolution = self._resolve(linux_maximum="7.3", installed="7.3.4-200.fc44.x86_64\n")
        inputs = resolution.inputs
        self.assertEqual(inputs.kernel_held_from, "")
        self.assertEqual(inputs.base_image_pinned, f"{self.NAME}@{self.NEWEST_DIGEST}")
        self.assertEqual(inputs.base_image_tag, "44.20261118.0")
        self.assertEqual(self.listed, 0)

    def test_no_supported_base_in_the_window_fails_the_run(self) -> None:
        tags = {tag: value for tag, value in HOLD_TAGS.items() if tag != "44.20261110.0"}
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=tags)
        self.assertIn("no base to hold the build on", str(caught.exception))
        self.assertEqual(self.pulled, [])

    def test_a_held_tag_without_a_digest_fails_before_any_pull(self) -> None:
        tags = dict(HOLD_TAGS) | {"44.20261110.0": ("7.2.15-200.fc44.x86_64", "44.20261110.0", "")}
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=tags)
        self.assertEqual(
            str(caught.exception),
            f"Failed to resolve base image digest for {self.NAME}:44.20261110.0",
        )
        self.assertEqual(self.pulled, [])

    def test_the_first_run_never_reads_the_copy_package_or_latest(self) -> None:
        # A supported upstream base remains, so the package, which may not exist yet and may
        # answer `denied`, and the signed `:latest` are not touched at all, and upstream is
        # read anonymously.
        inputs = self._resolve(
            copy_inspect_error=CiToolError("denied"), trusted=None, cosign_error=CiToolError("x")
        ).inputs
        self.assertEqual(inputs.base_image_name, self.NAME)
        self.assertEqual(self.copy_reads, [])
        self.assertEqual(self.latest_reads, 0)
        self.assertEqual(self.cosign_calls, [])
        self.assertEqual(self.upstream_reads_with_token, [])
        self.assertEqual(self.pull_auth, [None])

    def test_a_held_base_only_this_repository_still_has_is_built_from_the_copy(self) -> None:
        # quay.io deleted 44.20261110.0; the copy keeps its digest and its version-label tag, and
        # the signed `:latest` was built on that digest.
        inputs = self._resolve(tags=HOLD_GONE, copies=HOLD_HELD_COPY).inputs
        self.assertEqual(inputs.kernel_held_from, "7.3.4-200.fc44.x86_64")
        self.assertEqual(inputs.base_image_name, self.COPIES)
        self.assertEqual(inputs.base_image_pinned, f"{self.COPIES}@{self.HELD_DIGEST}")
        self.assertEqual(inputs.base_image_tag, "44.20261110.0")
        self.assertEqual(inputs.kernel_release, "7.2.15-200.fc44.x86_64")
        self.assertEqual(self.pulled, [f"{self.COPIES}@{self.HELD_DIGEST}"])
        # `:latest` was verified by its own digest before its label was believed.
        self.assertEqual(
            self.cosign_calls, [f"ghcr.io/danathar/zfs-kinoite-complex@{HOLD_LATEST_DIGEST}"]
        )

    def test_every_read_of_the_copy_uses_the_token_and_upstream_reads_stay_anonymous(self) -> None:
        self._resolve(tags=HOLD_GONE, copies=HOLD_HELD_COPY)
        # By digest (choosing), by tag (pinning), then the digest lookup that maps the tag.
        self.assertEqual(
            self.copy_reads,
            [
                (f"inspect @{HOLD_HELD_DIGEST[:14]}", True),
                ("inspect 44.20261110.0", True),
                ("digest 44.20261110.0", True),
            ],
        )
        self.assertEqual(self.upstream_reads_with_token, [])
        # `podman pull` of the copy gets an auth file for ghcr.io alone, never the token in argv.
        self.assertEqual(list(self.pull_auth[0]), ["ghcr.io"])

    def test_a_planted_newer_copy_is_not_built_on(self) -> None:
        # Someone with `packages: write` adds a newer, supported-looking tag. The copy package is
        # never listed, so only the digest the signed `:latest` records is ever read.
        planted = ("7.2.99-200.fc44.x86_64", "44.20261117.0", "sha256:" + "9" * 64)
        copies = HOLD_HELD_COPY | {"44.20261117.0": planted}
        inputs = self._resolve(tags=HOLD_GONE, copies=copies).inputs
        self.assertEqual(inputs.base_image_pinned, f"{self.COPIES}@{self.HELD_DIGEST}")
        self.assertEqual(inputs.base_image_tag, "44.20261110.0")
        self.assertNotIn("44.20261117.0", " ".join(read for read, _ in self.copy_reads))

    def test_a_copy_tag_moved_to_another_image_is_refused(self) -> None:
        moved = ("7.2.15-200.fc44.x86_64", "44.20261110.0", "sha256:" + "8" * 64)
        copies = {
            "44.20261110.0": moved,
            # The copy at the trusted digest, under another name.
            "other": HOLD_TAGS["44.20261110.0"],
        }
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=HOLD_GONE, copies=copies)
        self.assertIn("not " + HOLD_HELD_DIGEST, str(caught.exception))
        self.assertEqual(self.pulled, [])

    def test_a_copy_tag_that_resolves_to_no_digest_is_refused(self) -> None:
        empty = ("7.2.15-200.fc44.x86_64", "44.20261110.0", "")
        copies = {"44.20261110.0": empty, "other": HOLD_TAGS["44.20261110.0"]}
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=HOLD_GONE, copies=copies)
        self.assertIn("resolves to no digest, not " + HOLD_HELD_DIGEST, str(caught.exception))

    def test_a_run_on_an_upstream_base_needs_no_token(self) -> None:
        # Only a base that is the private copy needs the job's token; everything else is
        # anonymous, so a pull request from a fork still resolves.
        inputs = self._resolve(token=False).inputs
        self.assertEqual(inputs.base_image_name, self.NAME)
        self.assertEqual(self.pull_auth, [None])

    def test_an_unsigned_latest_fails_loudly_and_builds_nothing(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._resolve(
                tags=HOLD_GONE,
                copies=HOLD_HELD_COPY,
                cosign_error=CiToolError("no signatures found"),
            )
        self.assertIn("Cannot trust a held base copy", str(caught.exception))
        self.assertEqual(self.pulled, [])

    def test_a_missing_latest_fails_loudly_and_builds_nothing(self) -> None:
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=HOLD_GONE, copies=HOLD_HELD_COPY, trusted=None)
        self.assertIn("Cannot trust a held base copy", str(caught.exception))
        self.assertEqual(self.pulled, [])

    def test_a_copy_that_the_signed_latest_was_never_built_on_is_not_built_on(self) -> None:
        # `:latest` is signed and records the held base, but that base was never copied; a copy
        # of another base exists. Failing is the answer: nothing newer-looking is accepted.
        other = ("7.2.15-200.fc44.x86_64", "44.20261112.0", "sha256:" + "7" * 64)
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=HOLD_GONE, copies={"44.20261112.0": other})
        self.assertIn("has no copy in", str(caught.exception))
        self.assertEqual(self.pulled, [])

    def test_a_copy_that_cannot_be_read_fails_naming_the_package_and_the_token(self) -> None:
        for error in ("denied: permission_denied", "403 Forbidden"):
            with self.subTest(error=error), self.assertRaises(CiToolError) as caught:
                self._resolve(
                    tags=HOLD_GONE, copies=HOLD_HELD_COPY, copy_inspect_error=CiToolError(error)
                )
            message = str(caught.exception)
            self.assertIn(self.COPIES, message)
            self.assertIn("REGISTRY_TOKEN", message)
            self.assertNotIn("Failed to map digest", message)
            self.assertEqual(self.pulled, [])

    def test_a_copy_needed_without_a_token_fails_naming_the_token(self) -> None:
        # A pull request from a fork has no package token; that matters only once no supported
        # upstream base is left.
        with self.assertRaises(CiToolError) as caught:
            self._resolve(tags=HOLD_GONE, copies=HOLD_HELD_COPY, token=False)
        self.assertIn("REGISTRY_TOKEN", str(caught.exception))
        self.assertEqual(self.copy_reads, [])
        self.assertEqual(self.latest_reads, 0)

    def test_a_copy_that_cannot_be_read_says_why_instead_of_failing_to_map(self) -> None:
        cases = {
            "unauthorized: authentication required": "cannot read the "
            f"{HELD_BASE_PACKAGE} package",
            "connection reset by peer": "registry read failed",
        }
        for error, expected in cases.items():
            with self.subTest(error=error), self.assertRaises(CiToolError) as caught:
                self._resolve(
                    tags=HOLD_GONE, copies=HOLD_HELD_COPY, copy_digest_error=CiToolError(error)
                )
            message = str(caught.exception)
            self.assertIn(f"held base copy docker://{self.COPIES}:44.20261110.0", message)
            self.assertIn(expected, message)
            self.assertIn(error, message)
            self.assertNotIn("Failed to map digest", message)

    def test_an_upstream_candidate_tag_that_cannot_be_read_is_only_a_miss(self) -> None:
        # Outside the copy, a candidate shape the registry does not have is skipped and the
        # next shape is tried, as before copies existed.
        tags = {tag: value for tag, value in HOLD_TAGS.items() if tag != "44.20261118.0"}
        tags["latest-20261118.0"] = HOLD_TAGS["44.20261118.0"]
        inputs = self._resolve(
            tags=tags, linux_maximum="7.3", installed="7.3.4-200.fc44.x86_64\n"
        ).inputs
        self.assertEqual(inputs.base_image_tag, "latest-20261118.0")

    def test_an_unreadable_openzfs_limit_fails_the_run(self) -> None:
        with self.assertRaises(CiToolError):
            self._resolve(linux_maximum="")

    def test_a_lock_file_replay_is_never_held(self) -> None:
        lock = {
            "version": 1,
            "base_image": self.BASE_REF,
            "build_container": "ghcr.io/example/build:latest",
            "zfs_minor_version": "2.4",
            "zfs_version": "2.4.4",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            resolution = self._resolve(
                env={"USE_INPUT_LOCK": "true", "LOCK_FILE": str(lock_path)},
                installed="7.3.4-200.fc44.x86_64\n",
            )
        self.assertEqual(resolution.inputs.kernel_held_from, "")
        self.assertEqual(resolution.inputs.base_image_digest, self.NEWEST_DIGEST)
        self.linux_maximum.assert_not_called()
        self.assertEqual(self.listed, 0)

    def _replay_lock(self, base_image: str) -> dict:
        return {
            "version": 1,
            "base_image": base_image,
            "build_container": "ghcr.io/example/build:latest",
            "zfs_minor_version": "2.4",
            "zfs_version": "2.4.4",
        }

    def test_a_lock_file_replay_of_a_run_built_on_the_copy_reads_it_with_the_token(self) -> None:
        copy_ref = f"{self.COPIES}:44.20261110.0"
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(self._replay_lock(copy_ref)), encoding="utf-8")
            resolution = self._resolve(
                tags=HOLD_GONE,
                copies=HOLD_HELD_COPY,
                env={"USE_INPUT_LOCK": "true", "LOCK_FILE": str(lock_path)},
            )
        self.assertEqual(resolution.inputs.kernel_held_from, "")
        self.assertEqual(resolution.inputs.base_image_pinned, f"{self.COPIES}@{self.HELD_DIGEST}")
        self.assertEqual(self.copy_reads[0], ("inspect 44.20261110.0", True))
        self.assertTrue(all(credentialed for _, credentialed in self.copy_reads))
        self.assertEqual(list(self.pull_auth[0]), ["ghcr.io"])
        # A replay is never held, so it asks nothing of the signed `:latest`.
        self.assertEqual(self.latest_reads, 0)

    def test_a_lock_file_replay_of_a_copy_without_a_token_fails_naming_it(self) -> None:
        copy_ref = f"{self.COPIES}:44.20261110.0"
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(self._replay_lock(copy_ref)), encoding="utf-8")
            with self.assertRaises(CiToolError) as caught:
                self._resolve(
                    tags=HOLD_GONE,
                    copies=HOLD_HELD_COPY,
                    env={"USE_INPUT_LOCK": "true", "LOCK_FILE": str(lock_path)},
                    token=False,
                )
        self.assertIn("REGISTRY_TOKEN", str(caught.exception))
        self.assertIn("held-base copy", str(caught.exception))

    def test_a_held_run_says_so_and_exports_the_held_kernel(self) -> None:
        resolution = self._resolve()
        with (
            patch("ci_tools.resolve_build_inputs.resolve_build_inputs", return_value=resolution),
            patch("ci_tools.resolve_build_inputs.write_github_outputs") as write_outputs,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            main()
        self.assertEqual(
            write_outputs.call_args.args[0]["kernel_held_from"], "7.3.4-200.fc44.x86_64"
        )
        self.assertIn("Kernel held: the newest base image has kernel 7.3.4", stdout.getvalue())

    def test_a_newer_line_that_supports_the_newest_kernel_lifts_the_hold(self) -> None:
        # OpenZFS 2.4 stops at 7.2, 2.5.1 supports 7.3: the selection moves, so the newest base
        # is built and nothing is held. The hold's limit is read for the selected release.
        resolution = self._resolve(
            selected="2.5.1", linux_maximum="7.3", installed="7.3.4-200.fc44.x86_64\n"
        )
        inputs = resolution.inputs
        self.assertEqual(inputs.kernel_held_from, "")
        self.assertEqual(inputs.base_image_pinned, f"{self.NAME}@{self.NEWEST_DIGEST}")
        self.assertEqual(inputs.zfs_version, "2.5.1")
        self.assertEqual(inputs.zfs_minor_version, "2.5")
        self.assertEqual(inputs.latest_zfs_minor_version, "2.4")
        self.linux_maximum.assert_called_once_with("2.5.1")
        self.select.assert_called_once()
        self.assertEqual(self.select.call_args.kwargs["configured_line"], "2.4")

    def test_a_lock_file_replay_keeps_its_locked_version_and_never_selects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            lock_path.write_text(json.dumps(self._replay_lock(self.BASE_REF)), encoding="utf-8")
            resolution = self._resolve(
                env={"USE_INPUT_LOCK": "true", "LOCK_FILE": str(lock_path)},
                installed="7.3.4-200.fc44.x86_64\n",
                selected="2.5.1",
                latest_zfs_version="2.5.1",
            )
        self.select.assert_not_called()
        self.assertEqual(resolution.inputs.zfs_version, "2.4.4")
        self.assertEqual(resolution.inputs.zfs_minor_version, "2.4")
        # The manifest still records which line `:latest` ran, for the line-move notice.
        self.assertEqual(resolution.inputs.latest_zfs_minor_version, "2.5")


class LatestZfsVersionLabelTests(unittest.TestCase):
    """`:latest`'s `zfs-version` label, the floor `select_zfs_version` never goes below."""

    ENV = {
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "REGISTRY_ACTOR": "actor",
        "REGISTRY_TOKEN": "secret-token",
        "IMAGE_NAME": "zfs-kinoite-complex",
    }
    LATEST = "docker://ghcr.io/danathar/zfs-kinoite-complex:latest"

    def _read(self, *, env: dict, latest) -> tuple[str, object]:
        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "ci_tools.resolve_build_inputs.skopeo_inspect_json_optional",
                side_effect=latest if isinstance(latest, Exception) else None,
                return_value=latest,
            ) as inspect,
            redirect_stdout(io.StringIO()),
        ):
            return latest_zfs_version_label(), inspect

    def test_reads_the_label_of_latest_with_the_token(self) -> None:
        label, inspect = self._read(
            env=self.ENV, latest={"Labels": {"org.zfs-kinoite-complex.zfs-version": "2.5.1"}}
        )
        self.assertEqual(label, "2.5.1")
        inspect.assert_called_once_with(self.LATEST, creds="actor:secret-token")

    def test_a_missing_latest_or_label_is_empty(self) -> None:
        for latest in (None, {"Labels": {}}, {"Labels": None}, {}):
            with self.subTest(latest=latest):
                self.assertEqual(self._read(env=self.ENV, latest=latest)[0], "")

    def test_without_a_token_nothing_is_read(self) -> None:
        label, inspect = self._read(
            env=self.ENV | {"REGISTRY_TOKEN": ""}, latest=AssertionError("read without a token")
        )
        self.assertEqual(label, "")
        inspect.assert_not_called()

    def test_any_other_registry_failure_raises(self) -> None:
        with self.assertRaises(CiToolError):
            self._read(env=self.ENV, latest=CiToolError("unauthorized: authentication required"))


if __name__ == "__main__":
    unittest.main()
