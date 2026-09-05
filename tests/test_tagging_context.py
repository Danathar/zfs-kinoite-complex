"""
Script: tests/test_tagging_context.py
What: Tests for the shared lightweight tag and registry-context helpers.
Doing: Verifies candidate-tag naming, branch-tag cleanup/composition, bot
detection, registry-context exports, and the four command entrypoints that
read the environment and write step outputs.
Why: These rules are small, but several workflows depend on them.
Goal: Keep the reduced helper surface explicit and safe.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ci_tools.common import CiToolError
from ci_tools.tagging_context import (
    actor_is_bot,
    build_branch_image_tag,
    build_branch_metadata,
    build_candidate_tag,
    export_registry_context_values,
    main_compose_branch_image_tag,
    main_compute_branch_metadata,
    main_compute_candidate_tag,
    main_export_registry_context,
    sanitize_branch_name,
)


def parse_github_file(path: Path) -> dict[str, str]:
    """
    Parse a `GITHUB_OUTPUT` file the way GitHub Actions does.

    The helpers write heredoc form -- `name<<DELIM`, the value, then `DELIM` --
    so an `assertIn("branch_image_tag<<", text)` check passes even when the
    value written under that name is wrong. Reading the name back out is what
    makes these tests assert the value a later workflow step would receive.
    """

    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        name, _, delimiter = lines[index].partition("<<")
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index] != delimiter:
            body.append(lines[index])
            index += 1
        index += 1  # the closing delimiter line
        values[name] = "\n".join(body)
    return values


class TaggingContextTests(unittest.TestCase):
    def test_builds_candidate_tag_from_sha_and_fedora_version(self) -> None:
        self.assertEqual(
            build_candidate_tag(
                github_sha="deadbeefcafebabefeedface1234567890abcdef",
                fedora_version="43",
            ),
            "candidate-deadbee-43",
        )

    def test_builds_branch_image_tag(self) -> None:
        self.assertEqual(
            build_branch_image_tag(
                branch_tag_prefix="br-my-branch",
                fedora_version="43",
            ),
            "br-my-branch-43",
        )

    def test_sanitizes_branch_name(self) -> None:
        self.assertEqual(sanitize_branch_name("Feature/My Branch!"), "feature-my-branch")

    def test_uses_fallback_when_branch_sanitizes_to_empty(self) -> None:
        self.assertEqual(sanitize_branch_name("!!!"), "branch")

    def test_clamps_long_names(self) -> None:
        long_branch = "a" * 300
        branch_tag = build_branch_metadata(long_branch)
        self.assertLessEqual(len(branch_tag), 120)
        self.assertTrue(branch_tag.startswith("br-"))

    def test_actor_is_bot_matches_github_bot_suffix(self) -> None:
        self.assertTrue(actor_is_bot("renovate[bot]"))
        self.assertTrue(actor_is_bot("dependabot[bot]"))
        self.assertFalse(actor_is_bot("dbaggett"))

    def test_export_registry_context_values(self) -> None:
        self.assertEqual(
            export_registry_context_values(
                repository_owner="Danathar",
                actor_name="renovate[bot]",
            ),
            {
                "image_org": "danathar",
                "image_registry": "ghcr.io/danathar",
                "actor_is_bot": "true",
            },
        )

    def test_main_export_registry_context_writes_outputs_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output.txt"
            env_path = Path(temp_dir) / "github-env.txt"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY_OWNER": "Danathar",
                    "GITHUB_ACTOR": "renovate[bot]",
                    "GITHUB_OUTPUT": str(output_path),
                    "GITHUB_ENV": str(env_path),
                },
                clear=False,
            ):
                main_export_registry_context()

            output_text = output_path.read_text(encoding="utf-8")
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("image_org<<", output_text)
            self.assertIn("danathar", output_text)
            self.assertIn("actor_is_bot<<", output_text)
            self.assertIn("true", output_text)
            self.assertIn("IMAGE_REGISTRY<<", env_text)
            self.assertIn("ghcr.io/danathar", env_text)
            self.assertIn("ACTOR_IS_BOT<<", env_text)


class TaggingEntrypointTests(unittest.TestCase):
    """
    The three command entrypoints `ci_tools.cli` dispatches for tag naming.

    The pure helpers above are covered, but a workflow step never calls them.
    It runs a command that has to read the right environment variable names,
    write the right output name, and put the helper's result in it. Each of
    those three wirings is a separate way to be wrong while every helper test
    still passes.
    """

    def run_entrypoint(self, entrypoint, env: dict[str, str]) -> tuple[dict[str, str], str]:
        """
        Run one entrypoint with exactly `env` plus a GITHUB_OUTPUT path.

        The environment is cleared rather than extended: a CI runner already
        exports GITHUB_SHA and GITHUB_REF_NAME, and inheriting them would let a
        test pass on a value it never set.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github-output.txt"
            stdout = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {**env, "GITHUB_OUTPUT": str(output_path)},
                    clear=True,
                ),
                redirect_stdout(stdout),
            ):
                entrypoint()
            return parse_github_file(output_path), stdout.getvalue()

    def test_compute_candidate_tag_writes_the_short_sha_tag_as_an_output(self) -> None:
        outputs, printed = self.run_entrypoint(
            main_compute_candidate_tag,
            {"GITHUB_SHA": "deadbeefcafebabefeedface1234567890abcdef", "FEDORA_VERSION": "43"},
        )

        self.assertEqual(outputs["candidate_tag"], "candidate-deadbee-43")
        self.assertIn("candidate-deadbee-43", printed)

    def test_compose_branch_image_tag_joins_the_prefix_and_the_fedora_version(self) -> None:
        # build-branch.yml is the only caller, and it is the one tagging
        # command no other test in either tier runs.
        outputs, printed = self.run_entrypoint(
            main_compose_branch_image_tag,
            {"BRANCH_TAG_PREFIX": "br-my-branch", "FEDORA_VERSION": "43"},
        )

        self.assertEqual(outputs["branch_image_tag"], "br-my-branch-43")
        self.assertIn("br-my-branch-43", printed)

    def test_compose_branch_image_tag_refuses_a_missing_fedora_version(self) -> None:
        # Without the guard the tag composes to `br-my-branch-`, which is a
        # legal registry reference. The branch build would publish under it
        # rather than stop.
        with self.assertRaises(CiToolError) as raised:
            self.run_entrypoint(
                main_compose_branch_image_tag,
                {"BRANCH_TAG_PREFIX": "br-my-branch"},
            )

        self.assertIn("FEDORA_VERSION", str(raised.exception))

    def test_compute_branch_metadata_writes_the_sanitized_prefix_as_an_output(self) -> None:
        outputs, printed = self.run_entrypoint(
            main_compute_branch_metadata,
            {"GITHUB_REF_NAME": "Feature/Fix ZFS_2.4!"},
        )

        self.assertEqual(outputs["branch_tag"], "br-feature-fix-zfs_2.4")
        self.assertIn("br-feature-fix-zfs_2.4", printed)

    def test_the_branch_tag_prefix_one_command_writes_is_what_the_next_consumes(self) -> None:
        """
        build-branch.yml runs `compute-branch-metadata` in one job and feeds its
        `branch_tag` output to `compose-branch-image-tag` in another as
        `BRANCH_TAG_PREFIX`. Nothing else asserts that hand-off, so a rename of
        either name would leave both commands passing their own tests.
        """

        metadata_outputs, _ = self.run_entrypoint(
            main_compute_branch_metadata,
            {"GITHUB_REF_NAME": "Feature/Fix ZFS_2.4!"},
        )
        image_outputs, _ = self.run_entrypoint(
            main_compose_branch_image_tag,
            {"BRANCH_TAG_PREFIX": metadata_outputs["branch_tag"], "FEDORA_VERSION": "43"},
        )

        self.assertEqual(image_outputs["branch_image_tag"], "br-feature-fix-zfs_2.4-43")
        # The composed value is about to become an image tag, so assert the
        # property rather than only the string.
        self.assertRegex(image_outputs["branch_image_tag"], r"^[a-z0-9._-]+$")


if __name__ == "__main__":
    unittest.main()
