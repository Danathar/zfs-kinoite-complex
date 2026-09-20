"""
Script: tests/test_reflections_docs.py
What: Joins docs/reflections/ to the tree -- its README's format contract to the entries that
are supposed to obey it, and the one entry filed so far to the guard, the workflow comment and
the CONTRIBUTING bullet it makes claims about.
Doing: Derives the rules from the README's own prose rather than restating them, applies them to
every tracked entry, and recomputes each claim the 2026-09-04 entry makes about the machine.
Why: This directory exists to be trusted about what was believed and when, and nothing opened
it. Its README states a filename form, a three-heading shape and the four-part shape of the file
it routes readers to instead; all three were unenforced. The entry itself is worse placed: it is
a story about a security guard that passed for the wrong reason, and it only stays true while
`packages:` is still present in `.github/workflows/ai-fix.yml` as a comment and absent as a
permission. Delete that header comment and the entry becomes fiction -- and, more to the point,
tests/test_workflow_build_container.py's comment-stripping stops being distinguishable from the
naive substring check it replaced.
Goal: Make the directory fail here when an entry drifts from the form the README promises, and
make the guard-that-guarded-nothing lesson fail here the day the thing it is a lesson about
stops being true.

Two deliberate omissions, named so they do not read as oversights.

The README also says entries are **append-only**. That is a claim about history, and a test can
only settle it with `git log --diff-filter=M`, which would fail on a commit that is already
merged and cannot be fixed forward without rewriting history. It is a review rule, not a gate.

Nothing here re-checks that the README's links resolve; tests/test_docs_consistency.py already
resolves every relative link in every tracked markdown file. What is checked instead is what
those links say *about* their targets -- the body, not the path.

No PyYAML, for the reason tests/test_docs_consistency.py gives: CI installs pytest, pytest-cov
and ruff, so a third-party import here would skip in exactly the place these assertions are
supposed to run. The workflow is read with a local comment stripper instead.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
DOCS_DIR = REPO_ROOT / "docs"
REFLECTIONS_DIR = DOCS_DIR / "reflections"

README = REFLECTIONS_DIR / "README.md"
GUARD_ENTRY = REFLECTIONS_DIR / "2026-09-04-a-guard-that-guarded-nothing.md"

CORRECTIONS = REPO_ROOT / ".claude" / "memory" / "corrections.md"
UPSTREAM_RESPONSE = DOCS_DIR / "upstream-change-response.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

GUARD_MODULE = TESTS_DIR / "test_workflow_build_container.py"
AI_FIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ai-fix.yml"

# The filename form the README's Format section states. Asserted to be the literal the README
# still prints before it is used, so this pattern cannot outlive the sentence it encodes.
ENTRY_NAME_FORM = "`YYYY-MM-DD-short-slug.md`"
ENTRY_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


def _tracked(directory: Path) -> list[Path]:
    """Files under `directory` that git tracks.

    Tracked rather than globbed: an entry that is not committed is not part of the record this
    directory claims to be, and a stray untracked scratch file must not fail the suite.
    """

    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", str(directory.relative_to(REPO_ROOT))],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / name for name in listing]


def entries() -> list[Path]:
    """Every reflection entry: the tracked markdown in the directory, minus its README."""

    return [path for path in _tracked(REFLECTIONS_DIR) if path.name != "README.md"]


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading at the same level or higher."""

    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != heading:
            continue
        body = []
        for following in lines[index + 1 :]:
            if following.startswith("#") and len(following) - len(following.lstrip("#")) <= level:
                break
            body.append(following)
        return "\n".join(body)
    raise AssertionError(f"no {heading!r} section")


def _read(path: Path) -> str:
    """Read `path`, naming it if it is gone.

    A reflection entry that disappears is the README's append-only rule being broken, and the
    class below reads one by name. Without this the whole class raises FileNotFoundError eight
    times and a reader has to work out which file, and why it mattered, from a traceback.
    """

    if not path.is_file():
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} is gone; this record is append-only")
    return path.read_text(encoding="utf-8")


def _flow(text: str) -> str:
    """Markdown prose as one line, so a rule split across a wrap is still one sentence."""

    return " ".join(text.split())


def _headings(text: str) -> list[str]:
    return [line.lstrip("#").strip() for line in text.splitlines() if line.startswith("## ")]


def _without_comments(text: str) -> str:
    """Return `text` with whole-line comments removed.

    A local copy of the helper tests/test_workflow_build_container.py uses, deliberately not
    imported from it: this module's job is to check that that module still reads the workflow
    this way, and a shared implementation would make both sides move together. Only whole-line
    comments, for the same reason the original gives -- stripping a trailing `#` correctly means
    knowing whether it is inside a string.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


class FormatContractTests(unittest.TestCase):
    """The README's Format section, applied to the entries it governs."""

    def setUp(self) -> None:
        self.readme = _read(README)
        self.format_rules = _flow(_section(self.readme, "## Format"))
        self.entries = entries()

    def test_the_directory_holds_an_entry_to_check(self) -> None:
        # Every other assertion in this class iterates the entries. An empty directory would
        # pass all of them while proving nothing -- which is the exact failure the one entry
        # filed here is about.
        self.assertGreaterEqual(len(self.entries), 1, "docs/reflections/ has no tracked entry")

    def test_the_readme_still_states_the_filename_form_this_module_enforces(self) -> None:
        self.assertIn(ENTRY_NAME_FORM, self.format_rules)

    def test_every_entry_is_named_for_the_date_the_lesson_was_learned_and_a_slug(self) -> None:
        for entry in self.entries:
            with self.subTest(entry=entry.name):
                self.assertRegex(entry.name, ENTRY_NAME_RE)

    def test_every_entry_carries_the_headings_the_readme_requires_in_order(self) -> None:
        # Read out of the README rather than hardcoded: if the required shape changes there,
        # this follows it, and if the sentence stating it disappears the match below fails.
        stated = re.search(r"Three headings: ([^.]+)\.", self.format_rules)
        self.assertIsNotNone(stated, "the Format section no longer states the three headings")
        required = [part.strip().lower() for part in stated.group(1).split(",")]
        self.assertEqual(len(required), 3, f"expected three headings, README lists {required}")
        for entry in self.entries:
            with self.subTest(entry=entry.name):
                found = [heading.lower() for heading in _headings(entry.read_text("utf-8"))]
                self.assertEqual(found, required)

    def test_no_entry_leaves_one_of_those_headings_empty(self) -> None:
        # A heading with nothing under it satisfies a heading check and records no lesson.
        for entry in self.entries:
            text = entry.read_text(encoding="utf-8")
            for heading in _headings(text):
                with self.subTest(entry=entry.name, heading=heading):
                    self.assertTrue(_flow(_section(text, f"## {heading}")))


class RoutingTests(unittest.TestCase):
    """The README's table sends a reader to two other places and says what is in them."""

    def setUp(self) -> None:
        self.readme = _read(README)
        self.table = _flow(_section(self.readme, "## How this differs from the other two"))

    def test_the_corrections_row_describes_a_correction_that_is_actually_filed(self) -> None:
        # The row's example is not decoration: it is the README's claim about what kind of thing
        # lives in corrections.md, and it names a specific one.
        self.assertIn("E402", self.table)
        corrections = CORRECTIONS.read_text(encoding="utf-8")
        self.assertIn("E402", corrections)
        self.assertIn("ruff 0.16", corrections.lower())

    def test_the_upstream_row_describes_a_situation_that_page_answers(self) -> None:
        self.assertIn("akmods cache no longer matches", self.table)
        upstream = UPSTREAM_RESPONSE.read_text(encoding="utf-8").lower()
        self.assertIn("akmods cache", upstream)
        self.assertIn("base image", upstream)

    def test_every_correction_has_the_four_parts_this_readme_says_they_have(self) -> None:
        # "believed, true, established by, avoid by" -- taken from the README's own sentence, so
        # the two files cannot describe different shapes without failing here.
        stated = re.search(r"entries are \*\*short and citable\*\* [^a-z]*([^.]+)\.", self.table)
        self.assertIsNotNone(stated, "the README no longer states the four parts")
        parts = [part.strip() for part in stated.group(1).split(",")]
        self.assertEqual(len(parts), 4, f"expected four parts, README lists {parts}")
        labels = [f"**{part[0].upper()}{part[1:]}:**" for part in parts]

        corrections = CORRECTIONS.read_text(encoding="utf-8")
        filed = _headings(corrections)
        self.assertGreaterEqual(len(filed), 1, "corrections.md records no correction")
        for heading in filed:
            with self.subTest(correction=heading):
                body = _section(corrections, f"## {heading}")
                positions = [body.find(label) for label in labels]
                for label, position in zip(labels, positions):
                    self.assertNotEqual(position, -1, f"{heading!r} has no {label} part")
                self.assertEqual(positions, sorted(positions), f"{heading!r} orders them wrongly")


class GuardThatGuardedNothingTests(unittest.TestCase):
    """The 2026-09-04 entry's claims about the guard, the workflow and CONTRIBUTING."""

    def setUp(self) -> None:
        self.entry = _read(GUARD_ENTRY)
        self.guard = _read(GUARD_MODULE)

    def test_the_entry_names_the_module_it_is_about_and_that_module_exists(self) -> None:
        self.assertIn("tests/test_workflow_build_container.py", self.entry)
        self.assertTrue(GUARD_MODULE.is_file())

    def test_the_two_properties_the_entry_describes_are_still_asserted(self) -> None:
        # "that its job holds no `packages:` permission, and that it never references the
        # signing secret" -- both still held, and still held over ai-fix.yml.
        self.assertIn('assertNotIn("packages:"', self.guard)
        self.assertIn('assertNotIn("secrets.SIGNING_SECRET"', self.guard)
        self.assertIn("ai-fix.yml", self.guard)

    def test_the_permission_assertion_still_reads_comment_stripped_source(self) -> None:
        # The fix the entry records. A bare assertNotIn over the raw file is the defect.
        self.assertIn(
            'assertNotIn("packages:", _without_comments(self._agent_workflow()))', self.guard
        )

    def test_the_comment_the_entry_is_about_is_still_in_the_workflow(self) -> None:
        # This is what makes the entry a lesson rather than a hypothetical, and what makes the
        # stripping above load-bearing: the literal is in the file, in a comment, and the
        # permission is not granted. Lose the comment and the guard silently becomes the naive
        # check it replaced -- green either way, and nothing would say so.
        workflow = AI_FIX_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("packages:", workflow)
        self.assertNotIn("packages:", _without_comments(workflow))

    def test_the_guard_still_uses_no_parser(self) -> None:
        # "No parser, so no undeclared dependency" -- read through ast so a mention of yaml in
        # the module's own prose cannot satisfy or break this.
        imported = set()
        for node in ast.walk(ast.parse(self.guard)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - set(sys.stdlib_module_names), set())

    def test_the_guard_has_no_skip_path(self) -> None:
        # "and no skip path": a skip is a green that ran nothing, and a security guard must not
        # be able to opt out of running. Read through ast, not grep, because that module's
        # comments discuss a workflow step that must not silently skip a rebuild -- prose about
        # skipping is not a skip, and this file exists to keep that distinction.
        tree = ast.parse(self.guard)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [ast.unparse(decorator) for decorator in node.decorator_list]
            elif isinstance(node, ast.Call):
                names = [ast.unparse(node.func)]
            elif isinstance(node, ast.Raise) and node.exc is not None:
                names = [ast.unparse(node.exc)]
            for name in names:
                self.assertNotIn("skip", name.lower(), f"{name} can leave an assertion unrun")

    def test_the_guard_runs_and_skips_nothing_with_nothing_installed(self) -> None:
        # The entry's claim stated as the command it states it in. `-I` drops PYTHONPATH and the
        # user site directory, so this is the module standing on the standard library.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(TESTS_DIR),
                "-p",
                GUARD_MODULE.name,
                "-v",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            # Not check=True: a non-zero exit is this assertion's finding, reported with the
            # output, rather than a CalledProcessError that says only that something failed.
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-2000:])
        self.assertNotIn("skipped", output.lower())

    def test_contributing_still_warns_about_the_failure_the_entry_compares_this_to(self) -> None:
        # The entry's closing paragraph quotes CONTRIBUTING.md. The quote runs two sentences of
        # one bullet together, so both halves are checked, and checked to be the same bullet.
        contributing = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("A test that asserts only that something raises.", self.entry)
        bullets = re.split(r"\n- ", contributing)
        matching = [
            _flow(bullet)
            for bullet in bullets
            if "A test that asserts only that something raises." in bullet
        ]
        self.assertEqual(len(matching), 1, "CONTRIBUTING.md no longer carries that warning once")
        self.assertIn("passes for any reason at all", matching[0])


if __name__ == "__main__":
    unittest.main()
