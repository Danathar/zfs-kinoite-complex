"""
Script: tests/test_check_stable_signal.py
What: Tests for the scheduled stable-signal build gate helper.
Doing: Mocks registry inspection results and checks the gate outputs without network access.
Why: The gate must skip only unchanged schedule runs and fail closed on unknown upstream state.
Goal: Keep the schedule-only cadence signal explicit and testable.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from ci_tools import zfs_release
from ci_tools.check_stable_signal import (
    AUDIT_TAG_WALK_DEADLINE_SECONDS,
    COMPARE_FILES_CAP,
    MAX_AUDIT_TAGS_INSPECTED,
    PUSH_PATHS_IGNORE,
    STABLE_SIGNAL_DIGEST_LABEL,
    STABLE_SIGNAL_IMAGE_LABEL,
    ZFS_VERSION_LABEL,
    StableSignalDecision,
    _bypass_decision,
    _github_glob_regex,
    evaluate_stable_signal_gate,
    fetch_commit_comparison,
    find_shipped_commit,
    is_push_ignored_path,
    main,
    unshipped_commits_reason,
)
from ci_tools.common import REGISTRY_METADATA_TIMEOUT, CiToolError
from tests.test_common import parse_github_file

REPO_ROOT = Path(__file__).resolve().parent.parent
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
SHIPPED_SHA7 = "fedcba9"


@contextlib.contextmanager
def _patched_unshipped_check(*, shipped_commit: str, comparison: dict | None = None):
    """
    Stub the two lookups the unshipped-commit check makes.

    `find_shipped_commit` reads the registry and `fetch_commit_comparison`
    calls the GitHub API. Every gate test that reaches the end of the gate
    must stub both, or the check would find no shipped commit and build.
    `comparison=None` makes any compare call a test failure.
    """

    def compare(**kwargs):
        if comparison is None:
            raise AssertionError(f"unexpected compare call: {kwargs}")
        return comparison

    with (
        patch(
            "ci_tools.check_stable_signal.find_shipped_commit", return_value=shipped_commit
        ) as find,
        patch(
            "ci_tools.check_stable_signal.fetch_commit_comparison", side_effect=compare
        ) as fetch,
    ):
        yield find, fetch


@contextlib.contextmanager
def _patched_registry_inspect(side_effect):
    """
    Patch every path `evaluate_stable_signal_gate` uses to reach `skopeo_inspect_json`.

    The stable-signal-image call goes through the name imported directly into
    `ci_tools.check_stable_signal`. The current-`:latest` call goes through the
    real (unmocked) `skopeo_inspect_json_optional`, which is defined in
    `ci_tools.common` and looks up `skopeo_inspect_json` in that module's own
    namespace. One side effect needs to be installed in both places so a
    single dispatcher function can answer both calls.
    """
    with (
        patch("ci_tools.check_stable_signal.skopeo_inspect_json", side_effect=side_effect),
        patch("ci_tools.common.skopeo_inspect_json", side_effect=side_effect),
    ):
        yield


def _stable_signal_inspect(digest: str = "sha256:stable") -> dict:
    return {
        "Name": "quay.io/fedora-ostree-desktops/kinoite",
        "Digest": digest,
        "Labels": {},
    }


def _current_latest_inspect(
    *, signal_image: str, signal_digest: str, zfs_version: str = "2.4.3"
) -> dict:
    labels = {
        STABLE_SIGNAL_IMAGE_LABEL: signal_image,
        STABLE_SIGNAL_DIGEST_LABEL: signal_digest,
    }
    if zfs_version:
        labels[ZFS_VERSION_LABEL] = zfs_version
    return {
        "Name": "ghcr.io/danathar/zfs-kinoite-complex",
        "Digest": "sha256:repo-latest",
        "Labels": labels,
    }


class EvaluateStableSignalGateTests(unittest.TestCase):
    def test_unchanged_signal_skips_schedule_build(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                self.assertIsNone(creds)
                return _stable_signal_inspect("sha256:same")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                self.assertEqual(creds, "actor:token")
                return _current_latest_inspect(
                    signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                    signal_digest="sha256:same",
                    zfs_version="2.4.3",
                )
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), _patched_unshipped_check(shipped_commit=HEAD_SHA[:7]) as (find, _):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        find.assert_called_once_with(
            image_org="danathar",
            image_name="zfs-kinoite-complex",
            latest_digest="sha256:repo-latest",
            creds="actor:token",
        )
        self.assertFalse(decision.should_build)
        self.assertEqual(decision.reason, "stable-signal-unchanged")
        self.assertEqual(
            decision.stable_signal_ref,
            "quay.io/fedora-ostree-desktops/kinoite:44",
        )
        self.assertEqual(decision.stable_signal_digest, "sha256:same")
        self.assertEqual(decision.zfs_version, "2.4.3")

    def test_changed_signal_builds(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:new")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                return _current_latest_inspect(
                    signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                    signal_digest="sha256:old",
                    zfs_version="2.4.3",
                )
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "stable-signal-advanced")

    def test_zfs_version_advanced_builds_even_when_stable_signal_unchanged(self) -> None:
        # This is the fix: a new OpenZFS patch on the configured line must
        # force a build even when Kinoite stable has not moved at all.
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:same")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                return _current_latest_inspect(
                    signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                    signal_digest="sha256:same",
                    zfs_version="2.4.3",
                )
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.4"}],
        ), _patched_unshipped_check(shipped_commit="") as (find, _):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "zfs-version-advanced")
        self.assertEqual(decision.zfs_version, "2.4.4")
        # A ZFS patch (possibly a security fix) is decided before the
        # unshipped-commit check, so nothing that check sees can block it.
        find.assert_not_called()

    def test_missing_zfs_version_label_builds(self) -> None:
        # :latest predates this label (or was built by a run before this
        # feature existed). Treat that the same as "we don't know", not "no
        # change".
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:same")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                return _current_latest_inspect(
                    signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                    signal_digest="sha256:same",
                    zfs_version="",
                )
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "current-latest-missing-zfs-version-label")
        self.assertEqual(decision.zfs_version, "2.4.3")

    def test_missing_previous_image_builds(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:new")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                raise CiToolError("Command failed: skopeo inspect\nmanifest unknown")
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "current-latest-missing")

    def test_missing_previous_labels_builds(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:new")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                return {
                    "Name": "ghcr.io/danathar/zfs-kinoite-complex",
                    "Digest": "sha256:repo-latest",
                    "Labels": {},
                }
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "current-latest-missing-stable-signal-labels")

    def test_current_latest_registry_error_raises_instead_of_building(self) -> None:
        # An auth/rate-limit/network failure on the current-`:latest` lookup is
        # not the same as "no previous image yet" and must not be swallowed
        # into a build decision from unknown state.
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:new")
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                raise CiToolError("unauthorized: authentication required")
            raise AssertionError(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), self.assertRaises(CiToolError) as context:
            evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertIn("unauthorized", str(context.exception))

    def test_upstream_stable_signal_inspect_failure_raises(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del image_ref, creds
            raise CiToolError("upstream inspect failed")

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), self.assertRaises(CiToolError) as context:
            evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

        self.assertIn("upstream inspect failed", str(context.exception))


def _same_signal_inspect(image_ref: str, *, creds: str | None = None) -> dict:
    del creds
    if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
        return _stable_signal_inspect("sha256:same")
    if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
        return _current_latest_inspect(
            signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
            signal_digest="sha256:same",
            zfs_version="2.4.3",
        )
    raise AssertionError(image_ref)


def _ahead(*files: dict) -> dict:
    return {"status": "ahead", "files": list(files)}


class UnshippedCommitsGateTests(unittest.TestCase):
    """The scheduled gate, with every upstream signal unchanged."""

    def evaluate(self, *, shipped_commit: str, comparison: dict | None = None):
        with _patched_registry_inspect(_same_signal_inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), _patched_unshipped_check(shipped_commit=shipped_commit, comparison=comparison) as (
            _,
            fetch,
        ):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )
        return decision, fetch

    def test_unshipped_change_outside_ignored_paths_builds(self) -> None:
        decision, fetch = self.evaluate(
            shipped_commit=SHIPPED_SHA7,
            comparison=_ahead({"filename": "docs/notes.md"}, {"filename": "build_files/x.sh"}),
        )
        fetch.assert_called_once_with(
            repository="Danathar/zfs-kinoite-complex", base=SHIPPED_SHA7, head=HEAD_SHA
        )
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "unshipped-commits")
        self.assertEqual(decision.stable_signal_digest, "sha256:same")
        self.assertEqual(decision.zfs_version, "2.4.3")

    def test_change_only_inside_ignored_paths_skips(self) -> None:
        cases = {
            "root markdown": [{"filename": "CHANGELOG.md"}],
            "root README": [{"filename": "README.md"}],
            "nested markdown": [{"filename": "ci_tools/notes/design.md"}],
            "file under docs": [{"filename": "docs/metrics/data.json"}],
            "agent settings": [{"filename": ".claude/settings.json"}],
            "rename within ignored paths": [
                {
                    "filename": "docs/new.txt",
                    "status": "renamed",
                    "previous_filename": "docs/old.txt",
                }
            ],
        }
        for name, files in cases.items():
            with self.subTest(case=name):
                decision, _ = self.evaluate(shipped_commit=SHIPPED_SHA7, comparison=_ahead(*files))
                self.assertFalse(decision.should_build)
                self.assertEqual(decision.reason, "stable-signal-unchanged")

    def test_rename_across_an_ignored_path_boundary_builds(self) -> None:
        # Either side of a rename outside the ignored paths changes the image:
        # moving a file into docs/ removes it from the build as surely as
        # moving one out adds it.
        cases = {
            "out of docs": ("files/x.sh", "docs/x.sh"),
            "into docs": ("docs/x.sh", "files/x.sh"),
        }
        for name, (filename, previous) in cases.items():
            with self.subTest(case=name):
                decision, _ = self.evaluate(
                    shipped_commit=SHIPPED_SHA7,
                    comparison=_ahead(
                        {"filename": filename, "status": "renamed", "previous_filename": previous}
                    ),
                )
                self.assertTrue(decision.should_build)
                self.assertEqual(decision.reason, "unshipped-commits")

    def test_head_equal_to_shipped_commit_falls_through_without_comparing(self) -> None:
        decision, fetch = self.evaluate(shipped_commit=HEAD_SHA[:7], comparison=None)
        fetch.assert_not_called()
        self.assertFalse(decision.should_build)
        self.assertEqual(decision.reason, "stable-signal-unchanged")

    def test_identical_comparison_falls_through(self) -> None:
        decision, _ = self.evaluate(
            shipped_commit=SHIPPED_SHA7, comparison={"status": "identical", "files": []}
        )
        self.assertFalse(decision.should_build)
        self.assertEqual(decision.reason, "stable-signal-unchanged")

    def test_unknown_shipped_commit_builds(self) -> None:
        # No audit tag shares `:latest`'s digest. This keeps building every
        # night until one build promotes and writes a matching audit tag.
        decision, fetch = self.evaluate(shipped_commit="", comparison=None)
        fetch.assert_not_called()
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "unshipped-commits-source-unknown")

    def test_behind_or_diverged_history_builds(self) -> None:
        for status in ("behind", "diverged"):
            with self.subTest(status=status):
                decision, _ = self.evaluate(
                    shipped_commit=SHIPPED_SHA7,
                    comparison={"status": status, "files": [{"filename": "README.md"}]},
                )
                self.assertTrue(decision.should_build)
                self.assertEqual(decision.reason, "unshipped-commits-history-diverged")

    def test_unreadable_comparison_builds(self) -> None:
        cases = {
            "no file list": {"status": "ahead"},
            "file without a name": {"status": "ahead", "files": [{"status": "added"}]},
            "file that is not an object": {"status": "ahead", "files": ["README.md"]},
        }
        for name, comparison in cases.items():
            with self.subTest(case=name):
                decision, _ = self.evaluate(shipped_commit=SHIPPED_SHA7, comparison=comparison)
                self.assertTrue(decision.should_build)
                self.assertEqual(decision.reason, "unshipped-commits-compare-failed")

    def test_compare_failure_builds_instead_of_raising(self) -> None:
        with _patched_registry_inspect(_same_signal_inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), patch(
            "ci_tools.check_stable_signal.find_shipped_commit", return_value=SHIPPED_SHA7
        ), patch(
            "ci_tools.check_stable_signal.fetch_commit_comparison",
            side_effect=CiToolError("HTTP Error 404: Not Found"),
        ), contextlib.redirect_stdout(io.StringIO()):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "unshipped-commits-compare-failed")

    def test_a_dropped_connection_mid_compare_builds_instead_of_raising(self) -> None:
        # `IncompleteRead` is an `http.client.HTTPException`, not an `OSError`.
        handle = MagicMock()
        handle.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"{")
        with _patched_registry_inspect(_same_signal_inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), patch(
            "ci_tools.check_stable_signal.find_shipped_commit", return_value=SHIPPED_SHA7
        ), patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen", return_value=handle
        ), contextlib.redirect_stdout(io.StringIO()):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "unshipped-commits-compare-failed")

    def test_file_list_at_the_api_cap_builds_when_nothing_relevant_is_listed(self) -> None:
        files = [{"filename": f"docs/page-{n}.md"} for n in range(COMPARE_FILES_CAP)]
        decision, _ = self.evaluate(shipped_commit=SHIPPED_SHA7, comparison=_ahead(*files))
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "unshipped-commits-file-list-truncated")

    def test_file_list_just_under_the_cap_is_trusted(self) -> None:
        files = [{"filename": f"docs/page-{n}.md"} for n in range(COMPARE_FILES_CAP - 1)]
        decision, _ = self.evaluate(shipped_commit=SHIPPED_SHA7, comparison=_ahead(*files))
        self.assertFalse(decision.should_build)
        self.assertEqual(decision.reason, "stable-signal-unchanged")

    def test_changed_signal_builds_before_the_unshipped_check_runs(self) -> None:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                return _stable_signal_inspect("sha256:new")
            return _same_signal_inspect(image_ref)

        with _patched_registry_inspect(inspect), patch(
            "ci_tools.zfs_release.fetch_openzfs_releases",
            return_value=[{"tag_name": "zfs-2.4.3"}],
        ), _patched_unshipped_check(shipped_commit="") as (find, _):
            decision = evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )
        find.assert_not_called()
        self.assertEqual(decision.reason, "stable-signal-advanced")


class UnshippedCommitsReasonTests(unittest.TestCase):
    def test_short_head_prefix_is_not_mistaken_for_a_different_commit(self) -> None:
        with patch(
            "ci_tools.check_stable_signal.fetch_commit_comparison",
            return_value={"status": "ahead", "files": [{"filename": "Containerfile"}]},
        ) as fetch:
            reason = unshipped_commits_reason(
                repository="o/r", shipped_commit="0123457", head_sha=HEAD_SHA
            )
        fetch.assert_called_once()
        self.assertEqual(reason, "unshipped-commits")


class FindShippedCommitTests(unittest.TestCase):
    REPO = "docker://ghcr.io/danathar/zfs-kinoite-complex"

    def run_find(
        self, *, tags, digests, latest_digest="sha256:latest", list_error=None, clock=None
    ):
        inspected: list[str] = []
        self.timeouts: list[float | None] = []

        def inspect(
            image_ref: str, *, creds: str | None = None, timeout: float | None = None
        ) -> dict:
            self.assertEqual(creds, "actor:token")
            inspected.append(image_ref)
            self.timeouts.append(timeout)
            digest = digests[image_ref.rsplit(":", 1)[1]]
            if isinstance(digest, Exception):
                raise digest
            return {"Digest": digest}

        def list_tags(repository_ref: str, *, creds: str | None) -> list[str]:
            self.assertEqual(repository_ref, self.REPO)
            self.assertEqual(creds, "actor:token")
            if list_error is not None:
                raise list_error
            return tags

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("ci_tools.check_stable_signal._list_repository_tags", side_effect=list_tags)
            )
            stack.enter_context(
                patch("ci_tools.check_stable_signal.skopeo_inspect_json", side_effect=inspect)
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if clock is not None:
                stack.enter_context(
                    patch("ci_tools.check_stable_signal.monotonic", side_effect=clock)
                )
            found = find_shipped_commit(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                latest_digest=latest_digest,
                creds="actor:token",
            )
        return found, inspected

    def test_newest_audit_tag_with_latest_digest_wins(self) -> None:
        # Run numbers sort numerically: run 10 is newer than run 9.
        found, inspected = self.run_find(
            tags=["latest", "stable-9-aaaaaaa", "stable-10-bbbbbbb", "stable-8-ccccccc", "x"],
            digests={"stable-10-bbbbbbb": "sha256:latest", "stable-9-aaaaaaa": "sha256:latest"},
        )
        self.assertEqual(found, "bbbbbbb")
        self.assertEqual(inspected, [f"{self.REPO}:stable-10-bbbbbbb"])

    def test_walks_down_past_tags_with_another_digest(self) -> None:
        found, inspected = self.run_find(
            tags=["stable-9-aaaaaaa", "stable-10-bbbbbbb"],
            digests={"stable-10-bbbbbbb": "sha256:other", "stable-9-aaaaaaa": "sha256:latest"},
        )
        self.assertEqual(found, "aaaaaaa")
        self.assertEqual(len(inspected), 2)

    def test_no_matching_audit_tag_is_unknown(self) -> None:
        found, _ = self.run_find(
            tags=["stable-9-aaaaaaa"], digests={"stable-9-aaaaaaa": "sha256:other"}
        )
        self.assertEqual(found, "")

    def test_no_audit_tags_is_unknown(self) -> None:
        found, inspected = self.run_find(tags=["latest"], digests={})
        self.assertEqual(found, "")
        self.assertEqual(inspected, [])

    def test_inspection_stops_at_the_cap(self) -> None:
        count = MAX_AUDIT_TAGS_INSPECTED + 1
        tags = [f"stable-{n}-aaaaaaa" for n in range(1, count + 1)]
        digests = {tag: "sha256:other" for tag in tags}
        digests["stable-1-aaaaaaa"] = "sha256:latest"
        found, inspected = self.run_find(tags=tags, digests=digests)
        self.assertEqual(found, "")
        self.assertEqual(len(inspected), MAX_AUDIT_TAGS_INSPECTED)

    def test_the_walk_stops_at_its_deadline_and_is_unknown(self) -> None:
        # A fake clock: the walk starts at 0, the first inspect happens at 1s,
        # and the next check reads the deadline exactly, so the walk stops
        # before reaching the tag that would have matched.
        times = iter([0.0, 1.0, AUDIT_TAG_WALK_DEADLINE_SECONDS])
        found, inspected = self.run_find(
            tags=["stable-10-bbbbbbb", "stable-9-aaaaaaa"],
            digests={"stable-10-bbbbbbb": "sha256:other", "stable-9-aaaaaaa": "sha256:latest"},
            clock=lambda: next(times),
        )
        self.assertEqual(found, "")
        self.assertEqual(inspected, [f"{self.REPO}:stable-10-bbbbbbb"])

    def test_the_walk_still_matches_just_before_its_deadline(self) -> None:
        times = iter([0.0, 1.0, AUDIT_TAG_WALK_DEADLINE_SECONDS - 0.5])
        found, _ = self.run_find(
            tags=["stable-10-bbbbbbb", "stable-9-aaaaaaa"],
            digests={"stable-10-bbbbbbb": "sha256:other", "stable-9-aaaaaaa": "sha256:latest"},
            clock=lambda: next(times),
        )
        self.assertEqual(found, "aaaaaaa")

    def test_each_inspect_gets_only_the_time_left_before_the_deadline(self) -> None:
        # The first inspect starts at 1s, the second 10s before the deadline: neither may
        # run past it, and neither gets more than the usual registry ceiling.
        times = iter([0.0, 1.0, AUDIT_TAG_WALK_DEADLINE_SECONDS - 10.0])
        self.run_find(
            tags=["stable-10-bbbbbbb", "stable-9-aaaaaaa"],
            digests={"stable-10-bbbbbbb": "sha256:other", "stable-9-aaaaaaa": "sha256:latest"},
            clock=lambda: next(times),
        )
        self.assertEqual(
            self.timeouts,
            [min(REGISTRY_METADATA_TIMEOUT, AUDIT_TAG_WALK_DEADLINE_SECONDS - 1.0), 10.0],
        )

    def test_the_deadline_is_well_under_the_preflight_job_limit(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        preflight = text.split("\n  preflight:\n", 1)[1]
        minutes = int(re.search(r"^    timeout-minutes: (\d+)$", preflight, re.MULTILINE).group(1))
        self.assertLessEqual(AUDIT_TAG_WALK_DEADLINE_SECONDS * 2, minutes * 60)

    def test_empty_latest_digest_is_unknown_without_registry_calls(self) -> None:
        with patch("ci_tools.check_stable_signal._list_repository_tags") as list_tags:
            found = find_shipped_commit(
                image_org="danathar", image_name="zfs-kinoite-complex", latest_digest="", creds="c"
            )
        list_tags.assert_not_called()
        self.assertEqual(found, "")

    def test_registry_errors_are_unknown_not_raised(self) -> None:
        found, _ = self.run_find(
            tags=[], digests={}, list_error=CiToolError("unauthorized: authentication required")
        )
        self.assertEqual(found, "")
        found, _ = self.run_find(
            tags=["stable-9-aaaaaaa"],
            digests={"stable-9-aaaaaaa": CiToolError("manifest unknown")},
        )
        self.assertEqual(found, "")

    def test_the_tag_lister_is_promotions_own(self) -> None:
        from ci_tools import check_stable_signal

        with patch(
            "ci_tools.promote_stable.list_repository_tags", return_value=["latest"]
        ) as list_tags:
            tags = check_stable_signal._list_repository_tags(self.REPO, creds="actor:token")
        list_tags.assert_called_once_with(self.REPO, creds="actor:token")
        self.assertEqual(tags, ["latest"])


class FetchCommitComparisonTests(unittest.TestCase):
    @staticmethod
    def response(body: bytes) -> MagicMock:
        handle = MagicMock()
        handle.__enter__.return_value.read.return_value = body
        return handle

    def test_sends_the_token_to_the_compare_endpoint(self) -> None:
        payload = {"status": "ahead", "files": []}
        with patch.dict(os.environ, {"GITHUB_TOKEN": "gh-test-value"}), patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen",
            return_value=self.response(json.dumps(payload).encode()),
        ) as urlopen:
            result = fetch_commit_comparison(repository="o/r", base="abc1234", head=HEAD_SHA)
        self.assertEqual(result, payload)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, f"https://api.github.com/repos/o/r/compare/abc1234...{HEAD_SHA}"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer gh-test-value")

    def test_omits_authorization_without_a_token(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
        with patch.dict(os.environ, env, clear=True), patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen",
            return_value=self.response(b"{}"),
        ) as urlopen:
            fetch_commit_comparison(repository="o/r", base="a", head="b")
        self.assertIsNone(urlopen.call_args.args[0].get_header("Authorization"))

    def test_http_404_raises_a_tool_error(self) -> None:
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b""))
        with patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen", side_effect=error
        ), self.assertRaisesRegex(CiToolError, "Failed to compare a...b in o/r: HTTP Error 404"):
            fetch_commit_comparison(repository="o/r", base="a", head="b")

    def test_incomplete_read_raises_a_tool_error(self) -> None:
        handle = MagicMock()
        handle.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b"{")
        self.assertFalse(issubclass(http.client.IncompleteRead, OSError))
        with patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen", return_value=handle
        ), self.assertRaisesRegex(CiToolError, "Failed to compare a...b in o/r"):
            fetch_commit_comparison(repository="o/r", base="a", head="b")

    def test_non_json_and_non_object_payloads_raise(self) -> None:
        with patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen",
            return_value=self.response(b"not json"),
        ), self.assertRaisesRegex(CiToolError, "Failed to compare"):
            fetch_commit_comparison(repository="o/r", base="a", head="b")
        with patch(
            "ci_tools.check_stable_signal.urllib.request.urlopen",
            return_value=self.response(b"[]"),
        ), self.assertRaisesRegex(CiToolError, "Unexpected compare payload"):
            fetch_commit_comparison(repository="o/r", base="a", head="b")


class PushPathsIgnoreTests(unittest.TestCase):
    @staticmethod
    def workflow_paths_ignore() -> list[str]:
        """`on.push.paths-ignore` from build.yml, read as plain text."""
        lines = (REPO_ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        ).splitlines()
        push = lines.index("  push:")
        start = next(i for i in range(push, len(lines)) if lines[i] == "    paths-ignore:")
        patterns: list[str] = []
        for line in lines[start + 1 :]:
            item = re.match(r"^      - (.+)$", line)
            if not item:
                break
            patterns.append(item.group(1).strip().strip('"').strip("'"))
        return patterns

    def test_gate_copy_equals_the_workflow_list(self) -> None:
        workflow = self.workflow_paths_ignore()
        self.assertEqual(len(workflow), 13, workflow)
        self.assertEqual(list(PUSH_PATHS_IGNORE), workflow)

    def test_github_glob_semantics(self) -> None:
        ignored = (
            "README.md",
            "files/README.md",
            "a/b/README.md",
            "docs/x",
            "docs/a/b/c.json",
            "CHANGELOG.md",
            "ci_tools/notes.md",
            ".editorconfig",
            "LICENSE",
            ".claude/hooks/gate-git-diff.sh",
            ".github/ISSUE_TEMPLATE/bug.yml",
            ".github/labeler.yml",
        )
        built = (
            "Containerfile",
            "README.md.bak",
            "READMEXmd",
            "xdocs/a",
            "docs",
            "LICENSE.APACHE-2.0",
            "cosign.pub",
            "tests/test_check_stable_signal.py",
            "ci_tools/check_stable_signal.py",
            "files/docs.mdx",
            ".github/workflows/build.yml",
            "files/.editorconfig",
        )
        for path in ignored:
            with self.subTest(path=path):
                self.assertTrue(is_push_ignored_path(path))
        for path in built:
            with self.subTest(path=path):
                self.assertFalse(is_push_ignored_path(path))

    def test_single_star_and_question_mark_stop_at_a_slash(self) -> None:
        # fnmatch would match both of these; GitHub does not.
        self.assertIsNone(_github_glob_regex("docs/*").match("docs/a/b"))
        self.assertIsNotNone(_github_glob_regex("docs/*").match("docs/a"))
        self.assertIsNone(_github_glob_regex("a?b").match("a/b"))
        self.assertIsNotNone(_github_glob_regex("a?b").match("axb"))

    def test_unsupported_filter_syntax_is_refused(self) -> None:
        for pattern in ("!docs/**", "docs/[ab].md", "a+.md"):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                CiToolError, "Unsupported path filter syntax"
            ):
                _github_glob_regex(pattern)


class CheckStableSignalMainTests(unittest.TestCase):
    def test_main_writes_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output.txt"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_REPOSITORY_OWNER": "Danathar",
                    "GITHUB_EVENT_NAME": "schedule",
                    "REGISTRY_ACTOR": "actor",
                    "REGISTRY_TOKEN": "token",
                    "IMAGE_NAME": "zfs-kinoite-complex",
                    "STABLE_SIGNAL_IMAGE": "quay.io/fedora-ostree-desktops/kinoite:44",
                    "DEFAULT_ZFS_MINOR_VERSION": "2.4",
                    "GITHUB_REPOSITORY": "Danathar/zfs-kinoite-complex",
                    "GITHUB_SHA": HEAD_SHA,
                },
                clear=False,
            ), patch(
                "ci_tools.check_stable_signal.evaluate_stable_signal_gate",
                return_value=StableSignalDecision(
                    should_build=False,
                    reason="stable-signal-unchanged",
                    stable_signal_ref="quay.io/fedora-ostree-desktops/kinoite:44",
                    stable_signal_digest="sha256:same",
                    zfs_version="2.4.3",
                ),
            ) as evaluate:
                main()

            evaluate.assert_called_once_with(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )
            self.assertEqual(
                parse_github_file(output_path),
                {
                    "should_build": "false",
                    "reason": "stable-signal-unchanged",
                    "stable_signal_ref": "quay.io/fedora-ostree-desktops/kinoite:44",
                    "stable_signal_digest": "sha256:same",
                    "zfs_version": "2.4.3",
                },
            )

    def test_main_bypasses_gate_for_non_schedule_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output.txt"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "IMAGE_NAME": "zfs-kinoite-complex",
                    "STABLE_SIGNAL_IMAGE": "quay.io/fedora-ostree-desktops/kinoite:44",
                },
                clear=False,
            ), patch("ci_tools.check_stable_signal.evaluate_stable_signal_gate") as evaluate, patch(
                "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
                return_value={"Digest": "sha256:push-time"},
            ):
                main()

            evaluate.assert_not_called()
            self.assertEqual(
                parse_github_file(output_path),
                {
                    "should_build": "true",
                    "reason": "not-schedule-event",
                    "stable_signal_ref": "quay.io/fedora-ostree-desktops/kinoite:44",
                    "stable_signal_digest": "sha256:push-time",
                    "zfs_version": "",
                },
            )


# META ranges for the line-move scenarios: OpenZFS 2.4 stops at 7.2, 2.5.1 and later
# 2.4 patches reach 7.3, 2.6.1 needs at least 7.4.
LINE_MOVE_METAS = {
    "2.4.4": "Linux-Maximum: 7.2\nLinux-Minimum: 4.18\n",
    "2.4.5": "Linux-Maximum: 7.3\nLinux-Minimum: 4.18\n",
    "2.5.1": "Linux-Maximum: 7.3\nLinux-Minimum: 5.10\n",
}
UPSTREAM_KERNEL = "7.3.5-200.fc44.x86_64"


def _releases(*versions: str) -> list[dict]:
    return [
        {"tag_name": f"zfs-{version}", "draft": False, "prerelease": False}
        for version in versions
    ]


def _line_move_meta(version: str) -> str:
    """The META fetcher, failing as the real one does for a release it cannot read."""
    if version not in LINE_MOVE_METAS:
        raise CiToolError(f"Failed to fetch OpenZFS META for {version}: HTTP Error 404")
    return LINE_MOVE_METAS[version]


class ZfsLineSelectionGateTests(unittest.TestCase):
    """
    The gate with the real `select_zfs_version`, fed a release list and META files.

    The upstream signal image carries `UPSTREAM_KERNEL`. `:latest` carries the kernel of the
    base it was built on, which a held image keeps at 7.2; the gate must never select for it.
    """

    def evaluate(
        self,
        *,
        latest_zfs_version: str,
        releases: list[dict],
        signal_digest: str = "sha256:same",
        held: bool = False,
    ) -> StableSignalDecision:
        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            del creds
            if image_ref == "docker://quay.io/fedora-ostree-desktops/kinoite:44":
                signal = _stable_signal_inspect(signal_digest)
                signal["Labels"] = {"ostree.linux": UPSTREAM_KERNEL}
                return signal
            if image_ref == "docker://ghcr.io/danathar/zfs-kinoite-complex:latest":
                latest = _current_latest_inspect(
                    signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                    signal_digest="sha256:same",
                    zfs_version=latest_zfs_version,
                )
                latest["Labels"]["ostree.linux"] = "7.2.15-200.fc44.x86_64"
                if held:
                    latest["Labels"]["org.zfs-kinoite-complex.kernel-held-from"] = UPSTREAM_KERNEL
                return latest
            raise AssertionError(image_ref)

        with (
            _patched_registry_inspect(inspect),
            patch("ci_tools.zfs_release.fetch_openzfs_releases", return_value=releases),
            patch("ci_tools.zfs_release.fetch_openzfs_meta", side_effect=_line_move_meta),
            _patched_unshipped_check(shipped_commit=HEAD_SHA[:7]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image="quay.io/fedora-ostree-desktops/kinoite:44",
                zfs_minor_version="2.4",
                creds="actor:token",
                repository="Danathar/zfs-kinoite-complex",
                head_sha=HEAD_SHA,
            )

    def test_a_newly_eligible_line_builds(self) -> None:
        decision = self.evaluate(
            latest_zfs_version="2.4.4", releases=_releases("2.4.4", "2.5.0", "2.5.1")
        )
        self.assertEqual(decision.reason, "zfs-version-advanced")
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.zfs_version, "2.5.1")

    def test_after_the_move_there_is_no_rebuild(self) -> None:
        # `:latest` now runs 2.5.1. A later 2.4 patch that supports the kernel too does not
        # pull the selection back to 2.4, so the label equals the selection and nothing builds.
        decision = self.evaluate(
            latest_zfs_version="2.5.1", releases=_releases("2.4.4", "2.4.5", "2.5.0", "2.5.1")
        )
        self.assertEqual(decision.reason, "stable-signal-unchanged")
        self.assertFalse(decision.should_build)
        self.assertEqual(decision.zfs_version, "2.5.1")

    def test_a_patch_on_the_current_line_still_builds_first(self) -> None:
        # 2.4.5 supports the kernel, so the floor line keeps it and 2.5.1 is never chosen.
        decision = self.evaluate(
            latest_zfs_version="2.4.4", releases=_releases("2.4.4", "2.4.5", "2.5.1")
        )
        self.assertEqual(decision.reason, "zfs-version-advanced")
        self.assertEqual(decision.zfs_version, "2.4.5")

    def test_a_newer_line_x_y_0_alone_does_not_build(self) -> None:
        decision = self.evaluate(latest_zfs_version="2.4.4", releases=_releases("2.4.4", "2.5.0"))
        self.assertEqual(decision.reason, "stable-signal-unchanged")
        self.assertEqual(decision.zfs_version, "2.4.4")

    def test_the_selection_uses_the_upstream_kernel_not_latests(self) -> None:
        # `:latest`'s own 7.2 kernel is supported by 2.4.4; selecting for it would build nothing.
        with patch(
            "ci_tools.check_stable_signal.select_zfs_version",
            wraps=zfs_release.select_zfs_version,
        ) as select:
            decision = self.evaluate(
                latest_zfs_version="2.4.4", releases=_releases("2.4.4", "2.5.1")
            )
        self.assertEqual(select.call_args.kwargs["kernel_release"], UPSTREAM_KERNEL)
        self.assertEqual(select.call_args.kwargs["latest_zfs_version"], "2.4.4")
        self.assertEqual(decision.zfs_version, "2.5.1")

    def test_while_held_a_newly_eligible_line_lifts_the_hold(self) -> None:
        decision = self.evaluate(
            latest_zfs_version="2.4.4",
            releases=_releases("2.4.4", "2.5.1"),
            signal_digest="sha256:moved",
            held=True,
        )
        self.assertEqual(decision.reason, "zfs-version-advanced")
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.zfs_version, "2.5.1")

    def test_unreadable_meta_fails_the_gate(self) -> None:
        with self.assertRaises(CiToolError):
            self.evaluate(latest_zfs_version="2.4.4", releases=_releases("2.4.4", "2.6.1"))


class BypassDecisionTests(unittest.TestCase):
    def test_fills_stable_signal_digest_from_registry(self) -> None:
        with patch(
            "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
            return_value={"Digest": "sha256:push-time"},
        ) as inspect_optional:
            decision = _bypass_decision("quay.io/fedora-ostree-desktops/kinoite:44")

        inspect_optional.assert_called_once_with("docker://quay.io/fedora-ostree-desktops/kinoite:44")
        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "not-schedule-event")
        self.assertEqual(decision.stable_signal_ref, "quay.io/fedora-ostree-desktops/kinoite:44")
        self.assertEqual(decision.stable_signal_digest, "sha256:push-time")
        self.assertEqual(decision.zfs_version, "")

    def test_leaves_digest_empty_when_signal_image_missing(self) -> None:
        with patch(
            "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
            return_value=None,
        ):
            decision = _bypass_decision("quay.io/fedora-ostree-desktops/kinoite:44")

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.stable_signal_digest, "")

    def test_swallows_registry_error_and_leaves_digest_empty(self) -> None:
        with patch(
            "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
            side_effect=CiToolError("unauthorized: authentication required"),
        ):
            decision = _bypass_decision("quay.io/fedora-ostree-desktops/kinoite:44")

        self.assertTrue(decision.should_build)
        self.assertEqual(decision.reason, "not-schedule-event")
        self.assertEqual(decision.stable_signal_digest, "")

    def test_never_resolves_zfs_version_for_non_schedule_events(self) -> None:
        # The actual `zfs-version` image label for push/manual builds is
        # sourced from the real build's own resolution in `build-zfs-akmods`,
        # not from this gate, so the bypass path must not make a redundant
        # network call to the OpenZFS releases API for a value nothing reads.
        with patch(
            "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
            return_value={"Digest": "sha256:push-time"},
        ), patch(
            "ci_tools.check_stable_signal.select_zfs_version"
        ) as resolve_zfs:
            _bypass_decision("quay.io/fedora-ostree-desktops/kinoite:44")

        resolve_zfs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
