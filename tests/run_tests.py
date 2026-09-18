"""
Script: tests/run_tests.py
What: Runs this repository's test suite, refusing any selection that would import Python from outside tests/.
Doing: Rejects the pytest options that move collection off this tree, resolves every positional selection inside tests/, requires every .py file it will import to be tracked by git, then execs pytest.
Why: `.claude/settings.json` has to allow *some* test command unattended, and an unrestricted one is unbounded local code execution -- pytest imports every module it collects, and an import is not a tool call, so nothing in the deny list is consulted.
Goal: Make the one command an agent may run without a prompt able to import only code that is already in the diff.

The problem this exists for, stated plainly. `.claude/settings.json` denies
`cosign sign`, `skopeo copy`, `podman push`, `gh release`, `gh pr merge` and
the rest because `docs/SECURITY-AI.md` says those operations are outward-facing
and irreversible. Those rules decide *commands*. A Python module that runs

    subprocess.run(["cosign", "sign", ...])

at import time is not a command the agent asked to run, so no rule sees it. A
test runner imports every module it collects. So an allow-listed
`python3 -m pytest <anything>` is a way to reach every denied operation with
one approved command, and the deny list is only as strong as the set of files
that runner is willing to import.

What this closes and what it does not, because the difference is the whole
point:

  * **Closed:** code from outside this repository. `pytest /tmp/x.py`,
    `--pyargs some.installed.module`, `-p some_plugin` and
    `--confcutdir=/` each import something that was never in a diff and that
    no reviewer will ever see. Those are refused outright, in every spelling
    pytest accepts -- including `-o addopts=...`, which splices whatever it
    is given into the command line after this script has looked at it.
  * **Closed:** a file dropped into `tests/` and not committed. Every `*.py`
    under a selection must be tracked by git, so an untracked module cannot be
    collected. Committing it is a separate step that shows up in
    `git status` and in the pull request.
  * **Not closed:** a *tracked* file under `tests/`. Agents are invited to add
    tests here -- `docs/SECURITY-AI.md` lists editing tests as something an
    agent may do unattended -- so a committed test module runs by design. The
    control for that one is review, not this script, and
    `docs/SECURITY-AI.md` says so rather than implying otherwise.

A committed manifest of test files was the alternative, matching
`build_files/brew-payload.manifest`. It was not taken: it would be a second
hand-maintained copy of the `tests/` file list, churned by every pull request
that adds a test, and it would gate a file the same agent may edit in the same
commit. Asking git which files are tracked gets the drop-in property for free
and cannot drift from the tree.

Standard library only, like everything else that runs before pytest is
installed. When pytest is absent this falls back to
`python3 -m unittest discover`, which CONTRIBUTING documents as the
no-network path -- with the caveat that `unittest discover` does not recurse
into `tests/e2e/` the way pytest does, so the fallback runs fewer tests and
says so.

Run it the way the permission rule spells it, from the repository root:

    python3 tests/run_tests.py                 # the whole suite
    python3 tests/run_tests.py tests/test_cli.py -v
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# Options that move collection, conftest discovery or plugin loading off this
# tree. Each is refused rather than sanitised: an option that can name an
# arbitrary importable thing has no safe value that is worth the code to
# recognise, and refusing is legible in a way a rewritten argv is not.
#
# `--pyargs` turns positional arguments into module names, which would bypass
# the path check below entirely. `-p` loads a plugin module by name from
# sys.path. `--confcutdir` is the upward bound on conftest.py collection, so a
# value above the repository root pulls in a conftest nobody here wrote.
# `--rootdir` and `-c`/`--config-file` relocate what pytest considers the
# project, which moves both of those in turn. `-o`/`--override-ini` sets any
# ini option for the run, and one of those is `addopts`, whose value pytest
# splices into the command line before it parses options -- so
# `-o addopts=--pyargs x` is `--pyargs x` with an allowed option wrapped
# around it. Refused whole: there is no ini key worth telling apart.
REFUSED_OPTIONS = (
    "-p",
    "--pyargs",
    "--rootdir",
    "--confcutdir",
    "-c",
    "--config-file",
    "--import-mode",
    "-o",
    "--override-ini",
)

# pytest's short options that take no value, from `pytest -h` (9.1.1). In a
# single-dash argument these may precede the option that matters: `-vo
# addopts=x` is `-v -o addopts=x` and `-xp name` is `-x -p name`. Any other
# letter ends the cluster, either as the option itself or as one whose
# value is the rest of the argument (`-kfoo`, `-Werror`, `-rp`). A letter a
# plugin adds is not here and is read as value-taking -- test.yml installs
# no plugin, and a wrong guess there costs one cluster spelling that can be
# written as separate arguments, each of which is checked on its own.
SHORT_FLAGS = frozenset("hlqsvVx")


def refused_option(argument: str) -> str | None:
    """Return the refused option `argument` spells, or None.

    Every spelling pytest accepts is checked, not only the bare one:
    `--rootdir x` and `--rootdir=x`; a short option with its value attached
    (`-poutside.evil`, `-oaddopts=...`); and a short option closing a cluster
    of flags (`-vo addopts=...`). Matching the bare and `=` forms alone let
    the attached and clustered spellings reach pytest.
    """
    for option in REFUSED_OPTIONS:
        if argument == option or argument.startswith(f"{option}="):
            return option
    if argument.startswith("-") and not argument.startswith("--"):
        for letter in argument[1:]:
            if letter in SHORT_FLAGS:
                continue
            if f"-{letter}" in REFUSED_OPTIONS:
                return f"-{letter}"
            break
    return None


def tracked_python_files() -> set[Path]:
    """Every tracked `*.py` path in the repository, resolved.

    `git ls-files` rather than a `.git` read: it is the same answer the
    reviewer gets, and it reports the index, so a file staged in this commit
    counts and a file merely written into the working tree does not.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        (REPO_ROOT / name).resolve()
        for name in result.stdout.split("\0")
        if name
    }


def selection_path(argument: str) -> Path:
    """Resolve one positional selection to the file or directory it names.

    pytest node IDs carry `::` separators (`tests/test_x.py::TestY::test_z`).
    Only the part before the first `::` is a path.
    """
    return (REPO_ROOT / argument.split("::", 1)[0]).resolve()


def python_files_under(path: Path) -> list[Path]:
    """Every `*.py` a selection of `path` could cause to be imported.

    A directory selection is every `*.py` beneath it, not only `test_*.py`:
    package `__init__.py` files and `conftest.py` are imported too, and a
    module that is imported is a module that runs.
    """
    if path.is_dir():
        return sorted(p.resolve() for p in path.rglob("*.py"))
    return [path]


def is_selection(argument: str) -> bool:
    """Is `argument` something pytest could collect?

    An option and its value are not distinguished here on purpose. pytest has
    too many value-taking options (`-k expr`, `-m mark`, `--deselect id`) for a
    hand-kept list of them to stay right, and getting that list wrong in the
    generous direction would skip a real path. So the test is the one that
    matters for collection: a bare argument naming a path that exists is
    checked, and one that does not is passed through -- pytest cannot import
    what is not there, and an option value is not a path.
    """
    return not argument.startswith("-") and selection_path(argument).exists()


def check_selection(argument: str, tracked: set[Path]) -> str | None:
    """Return why `argument` is refused, or None if it is acceptable."""
    path = selection_path(argument)
    try:
        path.relative_to(TESTS_DIR)
    except ValueError:
        return (
            f"{argument!r} resolves to {path}, which is outside {TESTS_DIR}. "
            "This runner imports what it collects, so it collects only tests/."
        )
    untracked = [p for p in python_files_under(path) if p not in tracked]
    if untracked:
        listed = ", ".join(str(p.relative_to(REPO_ROOT)) for p in untracked)
        return (
            f"{argument!r} would import untracked Python: {listed}. "
            "Commit it first -- a module that runs unattended belongs in the diff."
        )
    return None


def check_repo_root_conftest(tracked: set[Path]) -> str | None:
    """Refuse an untracked `conftest.py` at the repository root.

    With no ini file, pytest's rootdir is the common ancestor of the
    arguments, and it collects `conftest.py` from there down. A repository-root
    conftest is therefore imported by a `tests/` selection without ever being
    named on the command line.
    """
    conftest = REPO_ROOT / "conftest.py"
    if conftest.exists() and conftest.resolve() not in tracked:
        return (
            f"{conftest} is untracked and pytest imports it before any test. "
            "Commit it first."
        )
    return None


def run_pytest(arguments: list[str]) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *arguments], cwd=REPO_ROOT, check=False
    ).returncode


def run_unittest(arguments: list[str], selections: list[str]) -> int:
    """The no-network fallback CONTRIBUTING documents, for directory selections.

    Deliberately not a silent substitute, in two ways. `unittest discover` does
    not recurse into `tests/e2e/`, which pytest collects, so this runs strictly
    fewer tests and says so. And it understands none of pytest's options, so an
    invocation carrying any of them is refused rather than run with them
    dropped -- a `-k` that silently selected nothing would report a pass over
    tests that never ran.
    """
    directories = [s for s in selections if selection_path(s).is_dir()]
    if len(directories) != len(selections) or arguments != selections:
        print(
            "pytest is not installed, and the unittest fallback understands no "
            "options and only whole directories. Install pytest (see "
            "CONTRIBUTING.md) to use this invocation.",
            file=sys.stderr,
        )
        return 2
    print(
        "pytest is not installed; falling back to unittest discover. This does "
        "not recurse into tests/e2e/, so fewer tests run than CI runs.",
        file=sys.stderr,
    )
    status = 0
    for directory in directories:
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", directory],
            cwd=REPO_ROOT,
            check=False,
        )
        status = status or completed.returncode
    return status


def pytest_is_available() -> bool:
    return (
        subprocess.run(
            [sys.executable, "-c", "import pytest"], capture_output=True, check=False
        ).returncode
        == 0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 tests/run_tests.py",
        description=(
            "Run the test suite with collection confined to tests/. "
            "Unrecognised arguments are passed through to pytest."
        ),
    )
    arguments = list(argv if argv is not None else sys.argv[1:])
    if "-h" in arguments or "--help" in arguments:
        parser.print_help()
        return 0

    for argument in arguments:
        option = refused_option(argument)
        if option is not None:
            print(
                f"{option} is refused here: it lets pytest import code from "
                "outside this repository, which is the thing this runner "
                "exists to prevent. Run pytest directly if you mean it -- "
                "that command is not on the unattended allow list.",
                file=sys.stderr,
            )
            return 2

    selections = [a for a in arguments if is_selection(a)]
    if not selections:
        selections = ["tests"]
        arguments = ["tests", *arguments]

    tracked = tracked_python_files()
    problems = [check_repo_root_conftest(tracked)]
    problems.extend(check_selection(s, tracked) for s in selections)
    refusals = [p for p in problems if p is not None]
    if refusals:
        for refusal in refusals:
            print(f"Refusing to run: {refusal}", file=sys.stderr)
        return 2

    if pytest_is_available():
        return run_pytest(arguments)
    return run_unittest(arguments, selections)


if __name__ == "__main__":
    raise SystemExit(main())
