"""
Script: tests/test_run_tests.py
What: Holds the refusals in tests/run_tests.py, the command `.claude/settings.json` allows unattended.
Doing: Builds a throwaway git repository, points the runner at it, and asserts what it refuses to import and what it hands to pytest.
Why: That script is the only thing standing between an allow-listed test command and arbitrary local code execution, and a refusal nobody exercises is a refusal that silently stops working.
Goal: Make a weakened check fail here, rather than the first time a module nobody read is imported by an approved command.

The claims being held are the ones `docs/SECURITY-AI.md` makes in its "run a
test suite unattended" row, in the order they matter:

  * a selection outside `tests/` is refused -- that is code from outside this
    repository, which no reviewer will ever see;
  * the options that relocate collection or load a plugin are refused, in both
    the `--opt value` and `--opt=value` spellings, because matching one and not
    the other would be a check that a space defeats;
  * an untracked `.py` anywhere under a selection is refused, so a module
    dropped into `tests/` cannot be collected until it is committed;
  * a tracked selection *is* run, with the arguments passed through unchanged.
    A runner that refused everything would pass the three assertions above and
    be useless, so this one is asserted too.

The fixture is a real `git init` rather than a stubbed `git ls-files`. The
whole point of asking git is that it reports the index, and a stub would agree
with whatever this file assumed instead of with git.

Standard library only, like the rest of the suite: no pytest is invoked here,
only `run_pytest` is observed being called.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_tests

# A real TestCase, because the unittest fallback below runs what it is given
# and reports exit code 5 when nothing was collected.
CASE = """import unittest


class %sTests(unittest.TestCase):
    def test_it(self) -> None:
        self.assertTrue(True)
"""


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


class RunnerRefusalTests(unittest.TestCase):
    """Every refusal, exercised against a real repository."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_ok.py").write_text(CASE % "Ok")
        self.outside = Path(self.tmp.name).resolve() / "outside"
        self.outside.mkdir()
        (self.outside / "test_elsewhere.py").write_text(CASE % "Elsewhere")

        git(self.repo, "init", "-q")
        git(self.repo, "add", "tests/test_ok.py")

        self.addCleanup(patch.stopall)
        patch.object(run_tests, "REPO_ROOT", self.repo).start()
        patch.object(run_tests, "TESTS_DIR", self.repo / "tests").start()
        # pytest's presence is a property of the machine running this suite, so
        # it is pinned rather than discovered. The fallback has its own test.
        patch.object(run_tests, "pytest_is_available", return_value=True).start()
        self.run_pytest = patch.object(run_tests, "run_pytest", return_value=0).start()

    def test_a_tracked_selection_runs_with_its_arguments_unchanged(self) -> None:
        # Guard the guard: without this, a runner that refused everything would
        # satisfy every other assertion in this class.
        self.assertEqual(run_tests.main(["tests", "-v"]), 0)
        self.run_pytest.assert_called_once_with(["tests", "-v"])

    def test_no_selection_means_the_tests_directory(self) -> None:
        self.assertEqual(run_tests.main([]), 0)
        self.run_pytest.assert_called_once_with(["tests"])

    def test_a_node_id_is_resolved_by_its_path_half(self) -> None:
        self.assertEqual(run_tests.main(["tests/test_ok.py::test_ok"]), 0)
        self.run_pytest.assert_called_once_with(["tests/test_ok.py::test_ok"])

    def test_an_option_value_that_is_not_a_path_is_passed_through(self) -> None:
        # `-k expr` puts a bare word in argv. It is not a path, so it is not a
        # selection, and the default selection is used instead of refusing.
        self.assertEqual(run_tests.main(["-k", "test_ok"]), 0)
        self.run_pytest.assert_called_once_with(["tests", "-k", "test_ok"])

    def test_a_selection_outside_the_tests_directory_is_refused(self) -> None:
        self.assertEqual(run_tests.main([str(self.outside / "test_elsewhere.py")]), 2)
        self.run_pytest.assert_not_called()

    def test_a_path_that_escapes_upward_is_refused(self) -> None:
        self.assertEqual(run_tests.main(["tests/../../outside"]), 2)
        self.run_pytest.assert_not_called()

    def test_an_untracked_module_under_the_selection_is_refused(self) -> None:
        (self.repo / "tests" / "test_dropped_in.py").write_text("import os\n")
        self.assertEqual(run_tests.main(["tests"]), 2)
        self.run_pytest.assert_not_called()

    def test_staging_the_module_is_what_makes_it_runnable(self) -> None:
        # The refusal above is about the index, not about the commit: a file
        # staged in the change under review is in the diff, which is the
        # property being required.
        (self.repo / "tests" / "test_dropped_in.py").write_text(CASE % "DroppedIn")
        self.assertEqual(run_tests.main(["tests"]), 2)
        git(self.repo, "add", "tests/test_dropped_in.py")
        self.assertEqual(run_tests.main(["tests"]), 0)

    def test_an_untracked_module_beside_a_named_file_is_not_reached(self) -> None:
        # Naming one file collects one file, so an untracked sibling is not
        # something that selection imports and is not a reason to refuse it.
        (self.repo / "tests" / "test_dropped_in.py").write_text("import os\n")
        self.assertEqual(run_tests.main(["tests/test_ok.py"]), 0)

    def test_an_untracked_repository_root_conftest_is_refused(self) -> None:
        # Never named on the command line, imported before any test: with no
        # ini file pytest collects conftest.py from the rootdir down.
        (self.repo / "conftest.py").write_text("import os\n")
        self.assertEqual(run_tests.main(["tests"]), 2)
        self.run_pytest.assert_not_called()

    def test_every_collection_relocating_option_is_refused_in_both_spellings(self) -> None:
        for option in run_tests.REFUSED_OPTIONS:
            for argument in (option, f"{option}=x"):
                with self.subTest(argument=argument):
                    self.run_pytest.reset_mock()
                    self.assertEqual(run_tests.main([argument, "tests"]), 2)
                    self.run_pytest.assert_not_called()

    def test_an_ini_override_cannot_smuggle_a_refused_option(self) -> None:
        # `-o addopts=...` is spliced into the command line by pytest after
        # this runner has looked at it, so an allowed option carrying a
        # refused one inside its value is the refused one (#194). Every
        # spelling pytest accepts for `-o` is exercised, not only the bare
        # form: the `=` form, the value attached to the short option, and the
        # short option closing a cluster of flags.
        for arguments in (
            ["-o", "addopts=--pyargs outside.evil", "tests"],
            ["--override-ini", "addopts=--pyargs outside.evil", "tests"],
            ["--override-ini=addopts=-p outside.evil", "tests"],
            ["-oaddopts=--pyargs outside.evil", "tests"],
            ["-vo", "addopts=--pyargs outside.evil", "tests"],
        ):
            with self.subTest(arguments=arguments):
                self.run_pytest.reset_mock()
                self.assertEqual(run_tests.main(arguments), 2)
                self.run_pytest.assert_not_called()

    def test_a_short_option_is_refused_with_its_value_attached_or_in_a_cluster(self) -> None:
        # argparse accepts `-pname` for `-p name` and `-xp name` for
        # `-x -p name`. A refusal that matched only the bare `-p` was one a
        # missing space defeated.
        for arguments in (
            ["-poutside.evil", "tests"],
            ["-xp", "outside.evil", "tests"],
            ["-c/elsewhere/pytest.ini", "tests"],
            ["-svc", "/elsewhere/pytest.ini", "tests"],
        ):
            with self.subTest(arguments=arguments):
                self.run_pytest.reset_mock()
                self.assertEqual(run_tests.main(arguments), 2)
                self.run_pytest.assert_not_called()

    def test_a_cluster_of_flags_or_an_attached_value_of_another_option_passes(self) -> None:
        # The cluster walk must stop at the first value-taking option, or a
        # `-k`, `-W` or `-r` value that happens to contain a refused letter
        # would be refused for spelling an option it does not: `-rp` reports
        # passed tests, `-Werror` is a warnings filter, and `-vv` is two
        # flags and no option at all.
        for arguments in (["-vv", "tests"], ["-rp", "tests"], ["-Werror", "tests"], ["-kfoo", "tests"]):
            with self.subTest(arguments=arguments):
                self.run_pytest.reset_mock()
                self.assertEqual(run_tests.main(arguments), 0)
                self.run_pytest.assert_called_once_with(arguments)

    def test_the_refused_list_covers_the_options_that_import_from_elsewhere(self) -> None:
        # Pinned by name so that dropping one is a failure rather than a
        # quietly shorter tuple. `--pyargs` turns positionals into module
        # names, `-p` loads a plugin, `-o` can set `addopts` to either, and
        # the other three move what pytest treats as the project, and with
        # it conftest collection.
        self.assertEqual(
            set(run_tests.REFUSED_OPTIONS),
            {
                "-p",
                "--pyargs",
                "--rootdir",
                "--confcutdir",
                "-c",
                "--config-file",
                "--import-mode",
                "-o",
                "--override-ini",
            },
        )

    def test_the_flag_table_matches_pytest_when_it_is_installed(self) -> None:
        # SHORT_FLAGS is what lets the cluster walk tell `-vo` (a flag, then
        # `-o`) from `-rp` (`-r` with value `p`). A flag pytest adds that is
        # missing here reads as value-taking, which would let `-<flag>o
        # addopts=...` through, so hold the table against the installed
        # pytest's own help text rather than against memory.
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-h"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("pytest is not installed; the table is held against 9.1.1's help")
        help_text = completed.stdout
        # A value-taking option is documented with a metavar right after the
        # option (`-k EXPRESSION`, `-r, --report-chars chars`); a flag has
        # nothing before the help column (`-s`, `-v, --verbose`).
        option_line = re.compile(r"^-(\w)(?:,\s--[\w-]+)?(?:\s(\S+))?(?:\s{2,}.*)?$")
        flags = set()
        for line in help_text.splitlines():
            match = option_line.match(line.strip())
            if match and match.group(2) is None:
                flags.add(match.group(1))
        self.assertEqual(flags, set(run_tests.SHORT_FLAGS))


class UnittestFallbackTests(unittest.TestCase):
    """The no-network path, which must not quietly run something else."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_ok.py").write_text(CASE % "Ok")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "tests/test_ok.py")

        self.addCleanup(patch.stopall)
        patch.object(run_tests, "REPO_ROOT", self.repo).start()
        patch.object(run_tests, "TESTS_DIR", self.repo / "tests").start()
        patch.object(run_tests, "pytest_is_available", return_value=False).start()

    def test_it_refuses_an_invocation_carrying_options(self) -> None:
        # `unittest` understands none of pytest's options. Running with them
        # dropped would report a pass over a selection that never happened --
        # a `-k` naming one test would silently run all of them.
        self.assertEqual(run_tests.main(["-k", "test_ok"]), 2)

    def test_it_refuses_a_file_selection(self) -> None:
        self.assertEqual(run_tests.main(["tests/test_ok.py"]), 2)

    def test_a_whole_directory_falls_back(self) -> None:
        self.assertEqual(run_tests.main(["tests"]), 0)


if __name__ == "__main__":
    unittest.main()
