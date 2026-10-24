"""
Script: tests/test_cli.py
What: Tests for the shared `ci_tools` command dispatcher.
Doing: Checks command-map entries, parser behavior, command-run paths, and that options after the
command name reach only the commands that take them.
Why: Makes sure workflow command names still point to the right modules.
Goal: Protect the main command entry surface used by workflow steps.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from ci_tools import cli
from ci_tools.cli import TAKES_ARGUMENTS, build_parser, command_map, run_command


class CliTests(unittest.TestCase):
    def test_command_map_contains_expected_entries(self) -> None:
        commands = command_map()
        expected = {
            "resolve-build-inputs",
            "write-build-inputs-manifest",
            "check-akmods-cache",
            "check-stable-signal",
            "compute-candidate-tag",
            "export-repo-defaults",
            "export-registry-context",
            "prepare-validation-build",
            "pin-akmods-cache",
            "promote-stable",
            "akmods-clone-pinned",
            "akmods-configure-zfs-target",
            "akmods-build-and-publish",
        }
        self.assertTrue(expected.issubset(set(commands.keys())))

    def test_parser_accepts_known_command(self) -> None:
        parser = build_parser({"demo-command": lambda: None})
        args = parser.parse_args(["demo-command"])
        self.assertEqual(args.command, "demo-command")

    def test_run_command_calls_target_function(self) -> None:
        called = {"value": False}

        def _target() -> None:
            called["value"] = True

        run_command("demo", {"demo": _target})
        self.assertTrue(called["value"])

    def test_run_command_hands_arguments_to_the_target(self) -> None:
        received: list[list[str]] = []
        run_command("demo", {"demo": received.append}, ("--fleet",))
        self.assertEqual(received, [["--fleet"]])

    def test_main_passes_fleet_through_to_check_ruleset_drift(self) -> None:
        received: list[list[str]] = []
        with mock.patch.object(
            cli, "command_map", return_value={"check-ruleset-drift": received.append}
        ):
            cli.main(["check-ruleset-drift", "--fleet"])
        self.assertEqual(received, [["--fleet"]])
        self.assertIn("check-ruleset-drift", TAKES_ARGUMENTS)
        self.assertTrue(TAKES_ARGUMENTS.issubset(command_map()))

    def test_main_runs_a_command_without_options_with_no_arguments(self) -> None:
        called: list[str] = []
        with mock.patch.object(
            cli, "command_map", return_value={"check-ruleset-drift": lambda: called.append("x")}
        ):
            cli.main(["check-ruleset-drift"])
        self.assertEqual(called, ["x"])

    def test_main_refuses_options_for_a_command_that_takes_none(self) -> None:
        called: list[str] = []
        commands = {"demo": lambda: called.append("x")}
        with (
            mock.patch.object(cli, "command_map", return_value=commands),
            contextlib.redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(["demo", "--fleet"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("demo takes no options: --fleet", err.getvalue())
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
