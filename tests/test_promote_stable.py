"""
Script: tests/test_promote_stable.py
What: Tests for candidate-to-stable promotion in the single-repository flow.
Doing: Verifies the candidate tag naming rule, copy order, digest-preserving flags, and the
refusal to move `latest` backwards.
Why: Promotion is the safety gate that advances `latest`, so it should be covered directly.
Goal: Keep the promotion contract explicit while the workflow evolves.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import COSIGN_TIMEOUT, CiToolError
from ci_tools.promote_stable import (
    list_repository_tags,
    main,
    refuse_held_base_older_than_latest,
    refuse_older_than_published,
    refuse_zfs_line_older_than_latest,
    report_zfs_line_move,
)

ZFS = "org.zfs-kinoite-complex.zfs-version"


def _env() -> dict[str, str]:
    return {
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "REGISTRY_ACTOR": "actor",
        "REGISTRY_TOKEN": "token",
        "FEDORA_VERSION": "43",
        "IMAGE_NAME": "zfs-kinoite-complex",
        "GITHUB_RUN_NUMBER": "12",
        "GITHUB_SHA": "deadbeefcafefeed",
    }


class PromoteStableTests(unittest.TestCase):
    def setUp(self) -> None:
        # Run 12 is promoting; the newest published audit tag is from run 11.
        tags = patch(
            "ci_tools.promote_stable.list_repository_tags",
            return_value=["latest", "stable-11-c0ffee1", "candidate-deadbee-43"],
        )
        self.list_tags = tags.start()
        self.addCleanup(tags.stop)
        # The candidate is not held unless a test says so. There is no `latest` unless a test
        # says so.
        candidate = patch(
            "ci_tools.promote_stable.skopeo_inspect_json",
            return_value={
                "Labels": {
                    "org.opencontainers.image.version": "43.20261110.0",
                    ZFS: "2.4.4",
                }
            },
        )
        self.inspect_candidate = candidate.start()
        self.addCleanup(candidate.stop)
        latest = patch("ci_tools.promote_stable.skopeo_inspect_json_optional", return_value=None)
        self.inspect_latest = latest.start()
        self.addCleanup(latest.stop)

    def test_promotes_audit_tag_before_latest_with_digest_preserving_flags(self) -> None:
        with patch.dict(os.environ, _env(), clear=True):
            with patch(
                "ci_tools.promote_stable.skopeo_inspect_digest",
                return_value="sha256:abc",
            ) as digest_lookup, patch(
                "ci_tools.promote_stable.skopeo_copy"
            ) as skopeo_copy, patch("ci_tools.promote_stable.run_cmd"):
                main()

            digest_lookup.assert_any_call(
                "docker://ghcr.io/danathar/zfs-kinoite-complex:candidate-deadbee-43",
                creds="actor:token",
            )
            self.assertEqual(skopeo_copy.call_count, 2)

            # Audit tag is copied first.
            self.assertEqual(
                skopeo_copy.call_args_list[0].args[:2],
                (
                    "docker://ghcr.io/danathar/zfs-kinoite-complex@sha256:abc",
                    "docker://ghcr.io/danathar/zfs-kinoite-complex:stable-12-deadbee",
                ),
            )
            # `latest` is copied second.
            self.assertEqual(
                skopeo_copy.call_args_list[1].args[:2],
                (
                    "docker://ghcr.io/danathar/zfs-kinoite-complex@sha256:abc",
                    "docker://ghcr.io/danathar/zfs-kinoite-complex:latest",
                ),
            )
            for call in skopeo_copy.call_args_list:
                self.assertEqual(call.kwargs.get("creds"), "actor:token")
                self.assertTrue(call.kwargs.get("preserve_digests"))
                self.assertEqual(call.kwargs.get("multi_arch"), "all")

    def test_fails_before_copy_when_candidate_digest_lookup_fails(self) -> None:
        def fail_digest_lookup(image_ref: str, *, creds: str) -> str:
            del creds
            raise CiToolError(f"Missing digest in skopeo inspect output for {image_ref}")

        with (
            patch.dict(os.environ, _env(), clear=True),
            patch(
                "ci_tools.promote_stable.skopeo_inspect_digest",
                side_effect=fail_digest_lookup,
            ),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("candidate-deadbee-43", str(context.exception))
        skopeo_copy.assert_not_called()

    def test_fails_when_audit_copy_fails_before_latest_copy(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.run_cmd"),
            patch(
                "ci_tools.promote_stable.skopeo_copy",
                side_effect=CiToolError("copy audit failed"),
            ) as skopeo_copy,
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("copy audit failed", str(context.exception))
        self.assertEqual(skopeo_copy.call_count, 1)
        self.assertEqual(
            skopeo_copy.call_args.args[:2],
            (
                "docker://ghcr.io/danathar/zfs-kinoite-complex@sha256:abc",
                "docker://ghcr.io/danathar/zfs-kinoite-complex:stable-12-deadbee",
            ),
        )

    def test_fails_when_latest_copy_fails_after_audit_copy(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.run_cmd"),
            patch(
                "ci_tools.promote_stable.skopeo_copy",
                side_effect=[None, CiToolError("copy latest failed")],
            ) as skopeo_copy,
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("copy latest failed", str(context.exception))
        self.assertEqual(skopeo_copy.call_count, 2)
        self.assertEqual(
            skopeo_copy.call_args_list[1].args[:2],
            (
                "docker://ghcr.io/danathar/zfs-kinoite-complex@sha256:abc",
                "docker://ghcr.io/danathar/zfs-kinoite-complex:latest",
            ),
        )

    def test_fails_when_destination_digest_does_not_match_candidate(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch(
                "ci_tools.promote_stable.skopeo_inspect_digest",
                # First call resolves the candidate digest; second call (the
                # post-audit-copy verification) returns a different digest.
                side_effect=["sha256:abc", "sha256:different"],
            ),
            patch("ci_tools.promote_stable.run_cmd"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("Promoted digest mismatch", str(context.exception))
        self.assertIn("sha256:different", str(context.exception))
        self.assertIn("sha256:abc", str(context.exception))
        skopeo_copy.assert_called_once()

    def test_verifies_candidate_signature_by_digest_before_any_tag_moves(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd") as run_cmd,
        ):
            main()

        run_cmd.assert_called_once()
        verify_args = run_cmd.call_args.args[0]
        self.assertEqual(verify_args[0], "cosign")
        self.assertEqual(verify_args[1], "verify")
        # Verification must name the immutable digest, never a movable tag.
        self.assertEqual(
            verify_args[-1], "ghcr.io/danathar/zfs-kinoite-complex@sha256:abc"
        )
        self.assertIn("--new-bundle-format=false", verify_args)
        self.assertEqual(run_cmd.call_args.kwargs.get("timeout"), COSIGN_TIMEOUT)
        self.assertEqual(skopeo_copy.call_count, 2)

    def test_unsigned_candidate_is_never_promoted(self) -> None:
        # If the candidate digest cannot be verified against the committed
        # public key, `latest` and the audit tag must both stay where they are.
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch(
                "ci_tools.promote_stable.run_cmd",
                side_effect=CiToolError("no matching signatures"),
            ),
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("no matching signatures", str(context.exception))
        skopeo_copy.assert_not_called()

    def test_lists_tags_of_the_image_repository_with_credentials(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy"),
            patch("ci_tools.promote_stable.run_cmd"),
        ):
            main()

        self.list_tags.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex", creds="actor:token"
        )

    def test_rerun_of_older_build_never_moves_any_tag(self) -> None:
        # Run 12 re-run after run 13 promoted: `latest` would move backwards.
        self.list_tags.return_value = ["latest", "stable-11-c0ffee1", "stable-13-feedfac"]
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("run 13", str(context.exception))
        skopeo_copy.assert_not_called()

    def test_tag_listing_failure_never_moves_any_tag(self) -> None:
        self.list_tags.side_effect = CiToolError("toomanyrequests")
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
            self.assertRaises(CiToolError),
        ):
            main()

        skopeo_copy.assert_not_called()

    def test_the_candidate_and_latest_are_each_read_once(self) -> None:
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
        ):
            main()

        self.inspect_candidate.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex@sha256:abc", creds="actor:token"
        )
        self.inspect_latest.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex:latest", creds="actor:token"
        )
        self.assertEqual(skopeo_copy.call_count, 2)

    def test_a_held_candidate_on_an_older_base_never_moves_any_tag(self) -> None:
        self.inspect_candidate.return_value = {
            "Labels": {
                "org.zfs-kinoite-complex.kernel-held-from": "7.3.4-200.fc43.x86_64",
                "org.opencontainers.image.version": "43.20261110.0",
                ZFS: "2.4.4",
            }
        }
        self.inspect_latest.return_value = {
            "Labels": {"org.opencontainers.image.version": "43.20261120.0"}
        }
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.inspect_latest.assert_called_once_with(
            "docker://ghcr.io/danathar/zfs-kinoite-complex:latest", creds="actor:token"
        )
        self.assertIn("43.20261120.0", str(context.exception))
        skopeo_copy.assert_not_called()

    def test_a_held_candidate_on_the_same_base_as_latest_promotes(self) -> None:
        self.inspect_candidate.return_value = {
            "Labels": {
                "org.zfs-kinoite-complex.kernel-held-from": "7.3.4-200.fc43.x86_64",
                "org.opencontainers.image.version": "43.20261110.0",
                ZFS: "2.4.4",
            }
        }
        self.inspect_latest.return_value = {
            "Labels": {"org.opencontainers.image.version": "43.20261110.0"}
        }
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
        ):
            main()

        self.assertEqual(skopeo_copy.call_count, 2)

    def test_a_candidate_on_an_older_zfs_line_than_latest_never_moves_any_tag(self) -> None:
        # A lock-file replay of an old 2.4 lock, promoted by default, while latest runs 2.5.
        self.inspect_latest.return_value = {
            "Labels": {"org.opencontainers.image.version": "43.20261110.0", ZFS: "2.5.1"}
        }
        with (
            patch.dict(os.environ, _env(), clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("2.5.1", str(context.exception))
        skopeo_copy.assert_not_called()

    def test_a_move_to_a_newer_line_promotes_and_writes_the_notice(self) -> None:
        self.inspect_candidate.return_value = {"Labels": {ZFS: "2.5.1"}}
        self.inspect_latest.return_value = {"Labels": {ZFS: "2.4.4"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.md"
            with (
                patch.dict(os.environ, _env() | {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True),
                patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
                patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
                patch("ci_tools.promote_stable.run_cmd"),
                redirect_stdout(io.StringIO()),
            ):
                main()
            text = summary.read_text(encoding="utf-8")

        self.assertEqual(skopeo_copy.call_count, 2)
        self.assertIn("OpenZFS moved from 2.4 to 2.5", text)
        self.assertIn("zpool upgrade", text)

    def test_non_numeric_run_number_is_refused(self) -> None:
        env = _env() | {"GITHUB_RUN_NUMBER": "12a"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            patch("ci_tools.promote_stable.run_cmd"),
            self.assertRaises(CiToolError) as context,
        ):
            main()

        self.assertIn("not a run number", str(context.exception))
        skopeo_copy.assert_not_called()


# The two newest audit tags on ghcr.io when #360 was filed.
PUBLISHED = ["stable-220-e6f14f7", "stable-221-c6d1dee"]


class RefuseZfsLineOlderThanLatestTests(unittest.TestCase):
    def refuse(self, candidate: str, latest: dict | None, configured: str = "2.4") -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            refuse_zfs_line_older_than_latest(
                candidate_labels={ZFS: candidate} if candidate else {},
                latest_labels=latest,
                configured_line=configured,
            )
        return out.getvalue()

    def test_promotes_when_there_is_no_latest(self) -> None:
        self.refuse("2.4.4", None)

    def test_promotes_the_same_or_a_newer_line(self) -> None:
        for candidate in ("2.5.1", "2.5.2", "2.6.1"):
            with self.subTest(candidate=candidate):
                self.refuse(candidate, {ZFS: "2.5.1"})

    def test_refuses_an_older_line_than_latest(self) -> None:
        with self.assertRaises(CiToolError) as context:
            self.refuse("2.4.9", {ZFS: "2.5.1"})
        self.assertIn("older OpenZFS line", str(context.exception))

    def test_lines_compare_numerically(self) -> None:
        with self.assertRaises(CiToolError):
            self.refuse("2.9.1", {ZFS: "2.10.1"})

    def test_a_latest_without_a_readable_label_is_judged_against_the_configured_line(self) -> None:
        for latest in ({}, {ZFS: ""}, {ZFS: "2.5"}, {ZFS: "latest"}):
            with self.subTest(latest=latest):
                said = self.refuse("2.4.4", latest, configured="2.4")
                self.assertIn("configured line 2.4", said)
                with self.assertRaises(CiToolError):
                    self.refuse("2.4.4", latest, configured="2.5")

    def test_a_candidate_without_a_readable_label_is_refused(self) -> None:
        for candidate in ("", "2.4", "zfs-2.4.4"):
            with self.subTest(candidate=candidate), self.assertRaises(CiToolError):
                self.refuse(candidate, None)


class ReportZfsLineMoveTests(unittest.TestCase):
    def report(self, candidate: str, latest: dict | None) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.md"
            out = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
                redirect_stdout(out),
            ):
                report_zfs_line_move(candidate_labels={ZFS: candidate}, latest_labels=latest)
            written = summary.read_text(encoding="utf-8") if summary.exists() else ""
        return out.getvalue(), written

    def test_a_newer_line_is_reported_with_the_rollback_warnings(self) -> None:
        said, written = self.report("2.5.1", {ZFS: "2.4.4"})
        self.assertEqual(said.strip(), written.strip())
        self.assertIn("OpenZFS moved from 2.4 to 2.5", written)
        self.assertIn("Existing pools are untouched.", written)
        self.assertIn("zpool upgrade", written)
        self.assertIn("will not import on the previous image", written)

    def test_nothing_is_reported_without_a_line_move(self) -> None:
        for candidate, latest in (
            ("2.4.5", {ZFS: "2.4.4"}),
            ("2.5.1", None),
            ("2.5.1", {}),
            ("2.4.4", {ZFS: "2.5.1"}),
        ):
            with self.subTest(candidate=candidate, latest=latest):
                self.assertEqual(self.report(candidate, latest), ("", ""))


class RefuseOlderThanPublishedTests(unittest.TestCase):
    def test_allows_first_promotion_with_no_audit_tags(self) -> None:
        refuse_older_than_published(run_number="1", tags=["latest", "candidate-deadbee-43"])

    def test_allows_newer_run(self) -> None:
        refuse_older_than_published(run_number="222", tags=PUBLISHED)

    def test_allows_rerun_of_the_newest_promoted_run(self) -> None:
        # A promotion that failed between the audit copy and the `latest` copy,
        # re-run before any newer build promoted, must still finish.
        refuse_older_than_published(run_number="221", tags=PUBLISHED)

    def test_refuses_older_run(self) -> None:
        with self.assertRaises(CiToolError) as context:
            refuse_older_than_published(run_number="220", tags=PUBLISHED)
        self.assertIn("Refusing to promote run 220", str(context.exception))

    def test_compares_run_numbers_numerically_not_as_text(self) -> None:
        with self.assertRaises(CiToolError):
            refuse_older_than_published(run_number="99", tags=["stable-100-e6f14f7"])

    def test_ignores_tags_outside_the_audit_family(self) -> None:
        refuse_older_than_published(
            run_number="5", tags=["stable-999", "stable-999-NOTHEX", "br-stable-999-abc", "999"]
        )


HELD = {"org.zfs-kinoite-complex.kernel-held-from": "7.3.4-200.fc44.x86_64"}


def _base(version: str) -> dict[str, str]:
    return {"org.opencontainers.image.version": version}


class RefuseHeldBaseOlderThanLatestTests(unittest.TestCase):
    def test_an_unheld_candidate_is_not_checked(self) -> None:
        # A lock-file rollback onto an older base is not held, and still promotes.
        refuse_held_base_older_than_latest(
            candidate_labels=_base("44.20261101.0"), latest_labels=_base("44.20261118.0")
        )

    def test_an_unheld_candidate_with_an_empty_held_label_is_not_checked(self) -> None:
        refuse_held_base_older_than_latest(
            candidate_labels={"org.zfs-kinoite-complex.kernel-held-from": ""} | _base("44.20261101.0"),
            latest_labels=_base("44.20261118.0"),
        )

    def test_allows_a_held_candidate_when_there_is_no_latest(self) -> None:
        refuse_held_base_older_than_latest(
            candidate_labels=HELD | _base("44.20261110.0"), latest_labels=None
        )

    def test_allows_a_held_candidate_on_the_same_or_a_newer_base(self) -> None:
        for latest in ("44.20261110.0", "44.20261109.0", "44.20261110.0", "43.20261201.0"):
            with self.subTest(latest=latest):
                refuse_held_base_older_than_latest(
                    candidate_labels=HELD | _base("44.20261110.0"), latest_labels=_base(latest)
                )

    def test_refuses_a_held_candidate_on_an_older_base(self) -> None:
        for latest in ("44.20261111.0", "44.20261110.1", "45.20261001.0"):
            with self.subTest(latest=latest), self.assertRaises(CiToolError) as context:
                refuse_held_base_older_than_latest(
                    candidate_labels=HELD | _base("44.20261110.0"), latest_labels=_base(latest)
                )
            self.assertIn("Refusing to promote a held candidate", str(context.exception))

    def test_refuses_a_held_candidate_whose_base_versions_cannot_be_read(self) -> None:
        for candidate, latest in (("", "44.20261110.0"), ("44.20261110.0", ""), ("44.2026111.0", "44.20261110.0")):
            with self.subTest(candidate=candidate, latest=latest), self.assertRaises(CiToolError):
                refuse_held_base_older_than_latest(
                    candidate_labels=HELD | _base(candidate), latest_labels=_base(latest)
                )


class ListRepositoryTagsTests(unittest.TestCase):
    def test_passes_credentials_as_authfile_and_returns_tags(self) -> None:
        with patch(
            "ci_tools.promote_stable.run_json_cmd",
            return_value={"Repository": "ghcr.io/o/i", "Tags": ["latest", "stable-1-abc"]},
        ) as run_json_cmd:
            tags = list_repository_tags("docker://ghcr.io/o/i", creds="actor:token")

        self.assertEqual(tags, ["latest", "stable-1-abc"])
        command = run_json_cmd.call_args.args[0]
        self.assertEqual(command[:2], ["skopeo", "list-tags"])
        self.assertIn("--authfile", command)
        self.assertEqual(command[-1], "docker://ghcr.io/o/i")
        self.assertNotIn("actor:token", " ".join(command))

    def test_missing_tag_list_fails_closed(self) -> None:
        with (
            patch("ci_tools.promote_stable.run_json_cmd", return_value={"Repository": "x"}),
            self.assertRaises(CiToolError),
        ):
            list_repository_tags("docker://ghcr.io/o/i", creds=None)


if __name__ == "__main__":
    unittest.main()
