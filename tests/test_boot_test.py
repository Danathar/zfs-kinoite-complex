"""
Script: tests/test_boot_test.py
What: Tests for the nightly boot test's guest script, its result parsing, its verdict and its retry rule.
Doing: Runs the generated guest script under bash with stub `rpm`, `zpool`, `zfs` and `systemctl`
    commands, feeds its output and hand-written SSH sessions through the parser and judge, and drives
    main() with the registry, pull and VM calls replaced.
Why: The verdict is read back from marked lines of an SSH session, so a quoting slip in the script, a
    marker the parser misses, or a judge that passes a check it never saw would turn the only boot of
    the published image into a green run that checked nothing.
Goal: Keep "green" meaning the image booted, was logged into, and had ZFS installed and loaded.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ci_tools.boot_test as script
from ci_tools.boot_test import (
    GUEST_CHECKS,
    MARKER,
    GuestReport,
    guest_script,
    judge,
    parse_guest_output,
    pinned_reference,
)
from ci_tools.common import CiToolError

ALL_PASS = [{"name": name, "status": "pass", "detail": ""} for name, _ in GUEST_CHECKS]


def passing_session() -> str:
    lines = [f"{MARKER}STATE degraded", f"{MARKER}INFO kernel 7.2.11-200.fc44.x86_64"]
    lines += [f"{MARKER}CHECK {name} pass detail" for name, _ in GUEST_CHECKS]
    lines += [f"{MARKER}FAILED-UNIT bootloader-update.service", f"{MARKER}DONE"]
    return "\n".join(lines) + "\n"


STUB = """#!/bin/bash
echo "$(basename "$0") SYSTEMD_PAGER=${SYSTEMD_PAGER-unset} $*" >> "$STUB_LOG"
case "$(basename "$0") $1" in
  "rpm -q") [ "$2" = zfs ] && echo "zfs-2.4.4-1.fc44.x86_64" && exit 0; exit 1 ;;
  "zpool list") echo "no pools available"; exit 0 ;;
  "zfs version") echo "zfs-2.4.4-1"; exit 0 ;;
  "systemctl is-system-running") echo running; exit 0 ;;
  "systemctl --failed") echo "mcelog.service loaded failed failed Machine Check"; exit 0 ;;
esac
exit 0
"""


@unittest.skipIf(shutil.which("bash") is None, "bash is not installed")
class GuestScriptTests(unittest.TestCase):
    """The generated script, run for real, against stub commands."""

    def run_script(self) -> tuple[GuestReport, str]:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            for name in ("rpm", "zpool", "zfs", "systemctl"):
                stub = bin_dir / name
                stub.write_text(STUB, encoding="utf-8")
                stub.chmod(0o755)
            log = Path(tmp) / "calls.log"
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log)}
            proc = subprocess.run(
                ["bash", "-c", guest_script()],
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=60,
            )
            return parse_guest_output(proc.stdout), log.read_text(encoding="utf-8")

    def test_every_check_reports_and_the_script_finishes(self) -> None:
        report, _ = self.run_script()
        self.assertTrue(report.done)
        self.assertEqual(report.state, "running")
        self.assertEqual([c["name"] for c in report.checks], [name for name, _ in GUEST_CHECKS])
        self.assertEqual(report.failed_units, ["mcelog.service"])
        self.assertEqual(report.zfs_version, ["zfs-2.4.4-1"])

    def test_a_check_reports_its_own_outcome_and_first_line(self) -> None:
        # The stub has zfs but not kmod-zfs; /sys/module/zfs depends on the host.
        report, _ = self.run_script()
        checks = {c["name"]: c for c in report.checks}
        self.assertEqual(checks["package:zfs"], {"name": "package:zfs", "status": "pass", "detail": "zfs-2.4.4-1.fc44.x86_64"})
        self.assertEqual(checks["package:kmod-zfs"]["status"], "fail")
        self.assertEqual(checks["zpool:list"]["detail"], "no pools available")
        expected_module = "pass" if Path("/sys/module/zfs").is_dir() else "fail"
        self.assertEqual(checks["module:zfs"]["status"], expected_module)

    def test_systemctl_never_gets_a_pager(self) -> None:
        # bcvk gives the session a terminal; a paged `systemctl --failed` waited
        # for a key until the boot timed out.
        _, calls = self.run_script()
        systemctl = [line for line in calls.splitlines() if line.startswith("systemctl ")]
        self.assertTrue(systemctl)
        for line in systemctl:
            self.assertIn("SYSTEMD_PAGER= ", line)


class ModuleCheckOrderTests(unittest.TestCase):
    """module:zfs has to see the module as the boot left it."""

    def test_nothing_runs_zfs_or_zpool_before_the_module_check(self) -> None:
        # OpenZFS's libzfs_init runs `modprobe zfs` when the module is absent, so
        # a zfs or zpool command ahead of module:zfs would load the module and
        # pass a boot whose modules-load.d entry never loaded it.
        lines = guest_script().splitlines()
        module_check = next(i for i, line in enumerate(lines) if "/sys/module/zfs" in line)
        callers = [i for i, line in enumerate(lines) if re.search(r"(?<![\w/:-])(zfs|zpool) ", line)]
        self.assertTrue(callers, "the script no longer runs zfs or zpool at all")
        self.assertGreater(min(callers), module_check, [lines[i] for i in callers])


class BootTimeoutTests(unittest.TestCase):
    """A boot that hits the timeout after the guest printed its results."""

    def test_the_checks_printed_before_the_timeout_are_kept(self) -> None:
        # subprocess.TimeoutExpired carries bytes even with text=True. Read as a
        # bytes repr, a failed check would look like checks that never ran, and
        # the retry could turn the run green.
        failing = passing_session().replace("CHECK module:zfs pass", "CHECK module:zfs fail")

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "bcvk":
                raise subprocess.TimeoutExpired(command, 1, output=failing.encode(), stderr=b"")
            return subprocess.CompletedProcess(command, 0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(script.subprocess, "run", side_effect=fake_run),
            contextlib.redirect_stdout(open(os.devnull, "w")),
        ):
            report, output = script.boot("ghcr.io/danathar/zfs-kinoite-complex@sha256:" + "c" * 64, Path(tmp), 1)
        self.assertTrue(report.done)
        statuses = {check["name"]: check["status"] for check in report.checks}
        self.assertEqual(statuses["module:zfs"], "fail")
        self.assertNotIn("b'", output)
        self.assertIn("bcvk timed out", output)


class ParseTests(unittest.TestCase):
    def test_markers_are_found_after_terminal_noise(self) -> None:
        output = f"\x1b]11;?\x1b\\\x1b[6n{MARKER}STATE running\nmotd line\n{MARKER}DONE\n"
        report = parse_guest_output(output)
        self.assertEqual(report.state, "running")
        self.assertTrue(report.done)

    def test_a_detail_keeps_its_spaces(self) -> None:
        report = parse_guest_output(f"{MARKER}CHECK zpool:list pass no pools available\n")
        self.assertEqual(report.checks, [{"name": "zpool:list", "status": "pass", "detail": "no pools available"}])


class JudgeTests(unittest.TestCase):
    def test_every_check_passing_passes_whatever_units_failed(self) -> None:
        report = GuestReport(done=True, state="degraded", checks=ALL_PASS, failed_units=["mcelog.service"])
        self.assertTrue(judge(report)[0])

    def test_checks_that_never_finished_fail(self) -> None:
        passed, summary = judge(GuestReport(done=False, checks=ALL_PASS))
        self.assertFalse(passed)
        self.assertIn("never reached", summary)

    def test_a_failed_check_fails_and_is_named(self) -> None:
        checks = [dict(c) for c in ALL_PASS]
        checks[2]["status"] = "fail"
        passed, summary = judge(GuestReport(done=True, checks=checks))
        self.assertFalse(passed)
        self.assertIn(GUEST_CHECKS[2][0], summary)

    def test_a_check_with_no_result_fails(self) -> None:
        passed, summary = judge(GuestReport(done=True, checks=ALL_PASS[:-1]))
        self.assertFalse(passed)
        self.assertIn(GUEST_CHECKS[-1][0], summary)


class PinnedReferenceTests(unittest.TestCase):
    def test_cases(self) -> None:
        digest = "sha256:" + "a" * 64
        cases = {
            "ghcr.io/danathar/zfs-kinoite-complex:latest": "ghcr.io/danathar/zfs-kinoite-complex",
            "ghcr.io/danathar/zfs-kinoite-complex": "ghcr.io/danathar/zfs-kinoite-complex",
            "localhost:5000/kinoite:candidate": "localhost:5000/kinoite",
            "localhost:5000/kinoite": "localhost:5000/kinoite",
            f"ghcr.io/danathar/zfs-kinoite-complex@sha256:{'b' * 64}": "ghcr.io/danathar/zfs-kinoite-complex",
        }
        for image, repository in cases.items():
            with self.subTest(image=image):
                self.assertEqual(pinned_reference(image, digest), f"{repository}@{digest}")


class MainTests(unittest.TestCase):
    """main(): which digest is booted, when a boot is retried, and the exit."""

    DIGEST = "sha256:" + "c" * 64

    def run_main(self, sessions: list[str]) -> tuple[list[str], dict, str, Exception | None]:
        booted: list[str] = []

        def fake_boot(image: str, work: Path, attempt: int) -> tuple[GuestReport, str]:
            booted.append(image)
            output = sessions[attempt - 1]
            return parse_guest_output(output), output

        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            summary_path = Path(tmp) / "summary.md"
            env = {
                "BOOT_TEST_IMAGE": "ghcr.io/danathar/zfs-kinoite-complex:latest",
                "BOOT_TEST_WORK": tmp,
                "GITHUB_STEP_SUMMARY": str(summary_path),
            }
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch.object(script, "skopeo_inspect_digest", return_value=self.DIGEST))
            stack.enter_context(patch.object(script, "run_cmd_with_retries", return_value=""))
            stack.enter_context(patch.object(script, "boot", side_effect=fake_boot))
            stack.enter_context(contextlib.redirect_stdout(open(os.devnull, "w")))  # noqa: SIM115
            error = None
            try:
                script.main()
            except CiToolError as exc:
                error = exc
            result = json.loads((Path(tmp) / "result.json").read_text(encoding="utf-8"))
            return booted, result, summary_path.read_text(encoding="utf-8"), error

    def test_a_passing_boot_boots_the_digest_once_and_succeeds(self) -> None:
        booted, result, summary, error = self.run_main([passing_session()])
        self.assertIsNone(error)
        self.assertEqual(booted, [f"ghcr.io/danathar/zfs-kinoite-complex@{self.DIGEST}"])
        self.assertTrue(result["passed"])
        self.assertIn(self.DIGEST, summary)

    def test_a_boot_that_never_ran_the_checks_is_retried_once(self) -> None:
        booted, result, _, error = self.run_main(["Timeout waiting for readiness", passing_session()])
        self.assertIsNone(error)
        self.assertEqual(len(booted), 2)
        self.assertEqual(result["attempts"], 2)

    def test_a_failed_check_is_not_retried_and_fails_the_run(self) -> None:
        failing = passing_session().replace("CHECK module:zfs pass", "CHECK module:zfs fail")
        booted, result, summary, error = self.run_main([failing, passing_session()])
        self.assertEqual(len(booted), 1)
        self.assertFalse(result["passed"])
        self.assertIsInstance(error, CiToolError)
        self.assertIn("module:zfs", summary)

    def test_two_boots_that_never_ran_the_checks_fail_the_run(self) -> None:
        booted, result, _, error = self.run_main(["no ssh", "no ssh"])
        self.assertEqual(len(booted), 2)
        self.assertFalse(result["passed"])
        self.assertIsInstance(error, CiToolError)


if __name__ == "__main__":
    unittest.main()
