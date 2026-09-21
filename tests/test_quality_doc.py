"""
Script: tests/test_quality_doc.py
What: Joins docs/quality.md to the badges, workflows, CLI commands, coverage thresholds and
guard messages whose facts it restates by hand.
Doing: Parses the page's five tables and its quoted YAML, then recomputes each claim from the
tree -- the badge set from README.md's badge row, the `concurrency` block from build.yml with
comments stripped, the gate rows from workflow job names and `ci_tools/cli.py`'s command map,
the `build.yml:<N>` pin by reading that line, the unmeasured count from
`.coverage-thresholds.json`, the four refusal messages by reassembling the f-strings
`ci_tools/` raises, and the two badge-behaviour bullets by importing `build_badge_payload`
and running it.
Why: This is the page a reader is sent to when a badge is red, a run is cancelled or a gate
fired, and almost every line of it is a hand-copy of something that lives in the machine: a
job name, a step name, a cron expression, a CLI subcommand, a key count, a line number. None
of those copies was checked anywhere.
Goal: Make a renamed job, a moved step, a new `unmeasured` entry, a reworded guard or an added
badge fail here, instead of leaving an operator reading a page that describes a pipeline this
repository no longer has.

Before this file, `docs/quality.md` was opened by no test as a subject. The two occurrences of
the path under `tests/` read it as a *source*: tests/test_docs_consistency.py asserts it is in
the documentation map and resolves its links, and tests/test_session_summary.py reads two facts
back out of it (the August 2026 failure split, and the `unexpected EOF` triage row) to validate
`.claude/session-summary.md`. Those two claims are pinned as a side effect. The rest were not,
and three had already drifted when this file was written:

  * the page said `Containerfile` and `build_files/build-image.sh` "are the two entries" in
    `.coverage-thresholds.json`'s `unmeasured` section. There are five; the other three were
    added by later security work. A page about where the signal comes from drifting in the
    direction of *understating* the unmeasured surface is the bad direction.
  * the page said the dashboard is three badges. `README.md` carries five CI-status badges --
    `tests` and `nightly compliance` were added later, both pointing at workflows this same
    page discusses at length.
  * the gate row pinned the stable-signal skip at `build.yml:97`, which is
    `- name: Install skopeo and cosign`. The skip is the job-level `if:` at `build.yml:120`.

What this file does not restate: link and anchor resolution (owned by
tests/test_docs_consistency.py), the two claims tests/test_session_summary.py reads back out,
and the coverage gate's own behaviour (owned by tests/test_check_coverage.py). The thresholds
file is read here only for the size of its `unmeasured` section, which is a number the prose
states.

No PyYAML, for the reason tests/test_docs_consistency.py gives: the CI job installs pytest,
pytest-cov and ruff by name, so an optional parser import would start skipping silently the day
that changed. Workflow facts are therefore read with the indentation-aware helpers below, which
carry their own fixture cases in `ParserTests` -- a hand-rolled parser that is never wrong about
a fixture is the only thing keeping the assertions built on it honest.

Each corrected claim was re-falsified after the fix (five -> four badges, `build.yml:120` ->
`build.yml:119`, "five entries" -> "four entries", the `Evaluate Stable Signal Gate` job
renamed, `00 05` -> `00 04`, the `Promoted digest mismatch` wording changed) and the matching
test failed every time.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from ci_tools.cli import command_map
from ci_tools.write_akmods_badge import build_badge_payload

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "quality.md"
README = REPO_ROOT / "README.md"
AGENTS = REPO_ROOT / "AGENTS.md"
THRESHOLDS = REPO_ROOT / ".coverage-thresholds.json"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
CI_TOOLS = REPO_ROOT / "ci_tools"
INSTALL_AND_VERIFY = REPO_ROOT / "docs" / "install-and-verify.md"

BUILD_YML = WORKFLOW_DIR / "build.yml"
BUILD_PR_YML = WORKFLOW_DIR / "build-pr.yml"
BUILD_BRANCH_YML = WORKFLOW_DIR / "build-branch.yml"
TEST_YML = WORKFLOW_DIR / "test.yml"
NIGHTLY_YML = WORKFLOW_DIR / "nightly-compliance.yml"

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

# The badge row in README.md carries more than CI status: DeepWiki, the hive maintenance
# badge, the ACMM level, "AI assisted" and the licence are all static. A CI-status badge is
# one of exactly two shapes -- an Actions `badge.svg` for a workflow in this repository, or a
# shields.io endpoint reading a payload off the `status` branch -- and those two shapes are
# what the page means by "the dashboard".
WORKFLOW_BADGE_RE = re.compile(
    r"!\[([^\]]+)\]\(https://github\.com/Danathar/zfs-kinoite-complex"
    r"/actions/workflows/([^/]+)/badge\.svg"
)
STATUS_BADGE_RE = re.compile(
    r"!\[([^\]]+)\]\(https://img\.shields\.io/endpoint\?url=[^)]*%2Fstatus%2F([^)]+?)\.json"
)

BACKTICKED_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def squash(text: str) -> str:
    """
    Collapse a Markdown paragraph onto one line.

    Every prose assertion here matches a phrase that the page wraps at 80 columns, so the
    phrase is split across a newline in the file and would match nothing as written.
    """

    return re.sub(r"\s+", " ", text)


def section(text: str, heading: str) -> str:
    """
    Return the body of the `##`/`###` section whose title is `heading`, up to the next
    heading of the same or a shallower depth.
    """

    lines = text.splitlines()
    start = None
    depth = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{2,6})\s+(.*)$", line)
        if match is None:
            continue
        if start is None:
            if match.group(2).strip() == heading:
                start = index + 1
                depth = len(match.group(1))
            continue
        if len(match.group(1)) <= depth:
            return "\n".join(lines[start:index])
    if start is None:
        raise AssertionError(f"docs/quality.md has no section titled {heading!r}")
    return "\n".join(lines[start:])


def tables(text: str) -> list[list[list[str]]]:
    """
    Return every Markdown table in `text` as a list of rows of cells, header and separator
    dropped. Rows are matched after `strip()` because one table on this page sits inside a
    numbered list and is therefore indented.
    """

    found: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            current.append(cells)
            continue
        if current:
            found.append(current)
            current = []
    if current:
        found.append(current)
    return [rows[2:] for rows in found if len(rows) > 2]


def fenced(text: str, language: str) -> list[str]:
    """Return the bodies of every ``` fence opened with `language`."""

    blocks: list[str] = []
    body: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if body is None:
            if stripped == f"```{language}":
                body = []
            continue
        if stripped == "```":
            blocks.append("\n".join(body))
            body = None
            continue
        body.append(line)
    return blocks


def strip_yaml_comments(text: str) -> str:
    """
    Drop whole-line `#` comments and blank lines.

    Only whole-line comments: a `#` inside a `${{ }}` expression or a quoted cron string is
    not a comment, and this is used to compare a block the document quotes without comments
    against the block build.yml carries with them.
    """

    kept = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return "\n".join(kept)


def top_level_block(path: Path, key: str) -> str:
    """
    Return the lines of the top-level mapping `key` in a workflow file, from `key:` itself
    up to the next line that starts in column zero.
    """

    lines = path.read_text(encoding="utf-8").splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == f"{key}:":
            start = index
            continue
        if start is not None and line and not line[0].isspace():
            return "\n".join(lines[start:index])
    if start is None:
        raise AssertionError(f"{path.name} has no top-level {key!r}")
    return "\n".join(lines[start:])


def sequence(block: str, key: str) -> list[str]:
    """
    Return the `- ` items of the list under `key:` inside `block`, stopping at the first line
    indented no further than `key:` itself.
    """

    lines = block.splitlines()
    indent = None
    items: list[str] = []
    for line in lines:
        if indent is None:
            match = re.match(rf"^(\s*){re.escape(key)}:\s*$", line)
            if match is not None:
                indent = len(match.group(1))
            continue
        if not line.strip():
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent:
            break
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().strip("'\""))
    if indent is None:
        raise AssertionError(f"no {key!r} list in block")
    return items


def job_names(path: Path) -> dict[str, str]:
    """Map job id to its declared `name:` (falling back to the id) for one workflow file."""

    lines = path.read_text(encoding="utf-8").splitlines()
    in_jobs = False
    names: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line and not line[0].isspace():
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match is not None:
            current = match.group(1)
            names[current] = current
            continue
        name = re.match(r"^    name:\s*(.+?)\s*$", line)
        if name is not None and current is not None:
            names[current] = name.group(1).strip("'\"")
    return names


def step_names(path: Path) -> list[str]:
    """Every `- name:` step label in a workflow file, in file order."""

    return [
        match.group(1).strip("'\"")
        for match in re.finditer(r"^\s*- name:\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]


def ci_tool_error_messages() -> dict[str, list[list[str]]]:
    """
    Map each `ci_tools/*.py` path to the literal segments of every `CiToolError` it raises.

    A message is a list of the fixed pieces of one f-string, in order; the interpolations
    between them are dropped. Reassembling with `ast` is what makes a message split across
    five source lines comparable to the single line the document quotes.
    """

    found: dict[str, list[list[str]]] = {}
    for path in sorted(CI_TOOLS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        messages: list[list[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "CiToolError"):
                continue
            if not node.args:
                continue
            messages.append(literal_segments(node.args[0]))
        if messages:
            found[f"ci_tools/{path.name}"] = messages
    return found


def literal_segments(node: ast.AST) -> list[str]:
    """Flatten an f-string, a plain string or an implicit/`+` concatenation to its literals."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        segments: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                segments.append(value.value)
            else:
                segments.append("\x00")
        return segments
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_segments(node.left) + literal_segments(node.right)
    return ["\x00"]


def contains_in_order(segments: list[str], parts: list[str]) -> bool:
    """
    True when every part appears, in order, inside the run of literal segments.

    Segments are joined with the placeholder marker between them so a part can never be
    matched across an interpolation the document wrote as `…`.
    """

    haystack = "\x00".join(segments)
    position = 0
    for part in parts:
        index = haystack.find(part, position)
        if index < 0:
            return False
        position = index + len(part)
    return True


def documented_parts(message: str) -> list[str]:
    """Split a message the document quotes with `…` elisions into its fixed parts."""

    return [part.strip() for part in message.split("…") if part.strip()]


class Badges(unittest.TestCase):
    """The badge table is the README badge row, retyped."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()
        cls.rows = tables(section(cls.text, "The badges"))[0]
        readme = README.read_text(encoding="utf-8")
        cls.readme_badges = {
            match.group(1): match.group(2) for match in WORKFLOW_BADGE_RE.finditer(readme)
        }
        cls.readme_status = {
            match.group(1): match.group(2) for match in STATUS_BADGE_RE.finditer(readme)
        }

    def test_the_readme_still_carries_both_badge_shapes(self):
        """Guard against a regex that silently matches nothing and passes every join below."""

        self.assertTrue(self.readme_badges, "no Actions badge.svg found in README.md")
        self.assertTrue(self.readme_status, "no status-branch shields endpoint found in README.md")

    def test_the_table_names_every_ci_status_badge_and_no_others(self):
        """
        Both directions. A badge added to README.md and not to the table is a signal on the
        front page that this document never explains; a row with no badge behind it sends a
        reader looking for something that is not there.
        """

        documented = {BOLD_RE.search(row[0]).group(1) for row in self.rows}
        actual = set(self.readme_badges) | set(self.readme_status)
        self.assertEqual(documented, actual)

    def test_the_prose_count_matches_the_table(self):
        claim = re.search(r"The dashboard is (\w+) badges in", squash(self.text))
        self.assertIsNotNone(claim, "the page no longer counts the badges")
        self.assertEqual(NUMBER_WORDS[claim.group(1)], len(self.rows))

    def test_each_workflow_badge_row_names_the_workflow_behind_it(self):
        """The Source cell for an Actions badge names a workflow file; it must be that badge's."""

        for label, workflow in sorted(self.readme_badges.items()):
            with self.subTest(badge=label):
                row = next(r for r in self.rows if BOLD_RE.search(r[0]).group(1) == label)
                named = BACKTICKED_RE.findall(row[1])
                self.assertIn(workflow, named)
                self.assertTrue((WORKFLOW_DIR / workflow).is_file())

    def test_each_status_branch_row_says_status_branch(self):
        for label in sorted(self.readme_status):
            with self.subTest(badge=label):
                row = next(r for r in self.rows if BOLD_RE.search(r[0]).group(1) == label)
                self.assertIn("status", BACKTICKED_RE.findall(row[1]))
                self.assertIn("branch payload", row[1])


class CancelledRunsReadAsFailed(unittest.TestCase):
    """The section that explains why merging quickly turns the build badge red."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()

    def test_the_quoted_concurrency_block_is_build_yml_s_own(self):
        """
        The page quotes the block to show that `build.yml` cancels itself on purpose. A changed
        `group:` expression, or a `cancel-in-progress` turned off, makes the whole explanation
        wrong -- so compare the text, with build.yml's comments stripped.
        """

        quoted = [block for block in fenced(self.text, "yaml") if block.startswith("concurrency:")]
        self.assertEqual(len(quoted), 1, "the page no longer quotes exactly one concurrency block")
        actual = strip_yaml_comments(top_level_block(BUILD_YML, "concurrency"))
        self.assertEqual(quoted[0].strip(), actual.strip())

    def test_the_paths_ignore_patterns_the_prose_names_are_still_ignored(self):
        """
        "merge the documentation-only pull requests last" is advice that only works while these
        patterns are in the list. Containment, not equality: the real list is longer, and
        tests/test_licensing_doc.py already owns the rest of it.
        """

        claim = re.search(r"`paths-ignore`\s*\(([^)]+)\)", squash(self.text))
        self.assertIsNotNone(claim, "the page no longer names build.yml's paths-ignore patterns")
        ignored = sequence(top_level_block(BUILD_YML, "on"), "paths-ignore")
        for pattern in BACKTICKED_RE.findall(claim.group(1)):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)

    def test_a_cancelled_run_cannot_move_this_repo_s_own_badge(self):
        """
        "It refuses to guess": any conclusion the module cannot interpret leaves the badge
        alone. Executed rather than read, because the bullet is a behavioural claim.
        """

        for conclusion in ("cancelled", "skipped", "timed_out", ""):
            with self.subTest(conclusion=conclusion):
                payload = build_badge_payload(
                    conclusion=conclusion, failure_payload=None, build_ran=True
                )
                self.assertIsNone(payload)

    def test_a_gate_skipped_success_cannot_turn_a_red_badge_green(self):
        """
        "A skipped or cancelled run cannot turn a red badge green", and the page says that rule
        lives inside the module rather than only in the workflow that calls it. Calling the
        module directly is what proves the second half.
        """

        self.assertIsNone(
            build_badge_payload(conclusion="success", failure_payload=None, build_ran=False)
        )
        rendered = build_badge_payload(conclusion="success", failure_payload=None, build_ran=True)
        self.assertEqual(rendered["message"], "in sync")

    def test_the_module_the_prose_names_is_the_one_imported_here(self):
        """
        Matched against the raw text, not a backtick scan of the whole page: the ```yaml
        fence above makes whole-document backtick pairing meaningless.
        """

        self.assertIn("`ci_tools/write_akmods_badge.py`", self.text)
        self.assertTrue((REPO_ROOT / "ci_tools" / "write_akmods_badge.py").is_file())
        self.assertIn("write_akmods_badge.py", squash(section(self.text, "The badges")))


class Gates(unittest.TestCase):
    """The gates table, and the paragraphs about what each gate cannot reach."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()
        cls.rows = tables(section(cls.text, "The gates"))[0]
        cls.thresholds = json.loads(THRESHOLDS.read_text(encoding="utf-8"))

    def test_python_unit_tests_is_still_the_job_name_in_test_yml(self):
        row = next(r for r in self.rows if "Python Unit Tests" in r[0])
        self.assertIn("test.yml", BACKTICKED_RE.findall(row[0]))
        self.assertIn("Python Unit Tests", job_names(TEST_YML).values())

    def test_the_lint_and_floor_rows_are_steps_of_that_same_job(self):
        """
        `ruff check` and tests/check_coverage.py are both listed as running in "same job".

        Read with comments stripped: test.yml explains the floor gate in a comment that names
        the same path, so a `grep` for it stays green after the step itself is gone.
        """

        body = strip_yaml_comments(TEST_YML.read_text(encoding="utf-8"))
        for row in self.rows:
            label = BACKTICKED_RE.findall(row[0])
            if not label:
                continue
            if label[0] == "ruff check":
                self.assertRegex(body, r"run:\s*ruff check ")
            if label[0] == "tests/check_coverage.py":
                self.assertTrue((REPO_ROOT / "tests" / "check_coverage.py").is_file())
                self.assertRegex(body, r"run:\s*python3 tests/check_coverage\.py")

    def test_the_stable_signal_row_names_the_job_build_yml_declares(self):
        row = next(r for r in self.rows if "Evaluate Stable Signal Gate" in r[0])
        self.assertIn("build.yml", BACKTICKED_RE.findall(row[0]))
        self.assertIn("Evaluate Stable Signal Gate", job_names(BUILD_YML).values())

    def test_the_line_pin_in_that_row_still_resolves_to_the_skip(self):
        """
        The one citation on this page precise enough to take a reader straight to the
        mechanism, and the one nothing re-resolves when build.yml grows a step. It pointed at
        `- name: Install skopeo and cosign` when this test was written.
        """

        row = next(r for r in self.rows if "Evaluate Stable Signal Gate" in r[0])
        pin = re.search(r"build\.yml:(\d+)", row[2])
        self.assertIsNotNone(pin, "the stable-signal row no longer pins a line in build.yml")
        lines = BUILD_YML.read_text(encoding="utf-8").splitlines()
        number = int(pin.group(1))
        self.assertLessEqual(number, len(lines), "the pinned line is past the end of build.yml")
        pinned = lines[number - 1]
        self.assertNotEqual(pinned.strip(), "", "the pinned line is blank")
        self.assertFalse(pinned.strip().startswith("#"), "the pinned line is a comment")
        self.assertIn("should_build", pinned)
        self.assertIn("!= 'schedule'", pinned)

    def test_the_three_publish_gates_are_cli_commands(self):
        """
        `check-akmods-cache`, `sign-image` and `promote-stable` are rows in the table and
        subcommands of ci_tools/cli.py. A renamed command leaves the row naming nothing.
        """

        commands = set(command_map())
        for name in ("check-akmods-cache", "sign-image", "promote-stable"):
            with self.subTest(command=name):
                self.assertTrue(
                    any(name in BACKTICKED_RE.findall(row[0]) for row in self.rows),
                    f"no gate row names {name!r}",
                )
                self.assertIn(name, commands)

    def test_strict_mode_is_a_real_mode_of_the_cache_check(self):
        """
        The row says "strict mode", which is `REQUIRE_MATCH` in the module -- the branch that
        refuses rather than reporting, and the branch that raises the message the refusal
        table quotes.
        """

        source = (CI_TOOLS / "check_akmods_cache.py").read_text(encoding="utf-8")
        self.assertIn("REQUIRE_MATCH", source)
        self.assertIn("Strict mode", source)

    def test_the_nightly_row_names_the_workflow_that_exists(self):
        row = next(r for r in self.rows if "nightly-compliance.yml" in r[0])
        self.assertTrue(NIGHTLY_YML.is_file())
        self.assertIn("on dispatch", row[1])
        self.assertIn("workflow_dispatch:", top_level_block(NIGHTLY_YML, "on"))

    def test_the_unmeasured_count_matches_the_thresholds_file(self):
        """
        The drift that prompted this file. Two of five is the reading a person doing an audit
        of the unmeasured surface would stop at.
        """

        claim = re.search(
            r"they are two of the (\w+)\s+entries in\s+`\.coverage-thresholds\.json`'s "
            r"`unmeasured` section",
            squash(self.text),
        )
        self.assertIsNotNone(claim, "the page no longer counts the unmeasured entries")
        self.assertEqual(NUMBER_WORDS[claim.group(1)], len(self.thresholds["unmeasured"]))

    def test_every_path_the_page_calls_unmeasured_is_an_unmeasured_key(self):
        """Both directions: the five keys are named, and nothing else is called unmeasured."""

        paragraphs = squash(section(self.text, "What each one does not cover"))
        named = {
            token
            for token in BACKTICKED_RE.findall(paragraphs)
            if token in self.thresholds["unmeasured"] or token.endswith((".sh", ".py"))
        }
        keys = set(self.thresholds["unmeasured"])
        self.assertTrue(keys.issubset(named | {"Containerfile"}), f"unnamed keys: {keys - named}")
        for key in keys:
            with self.subTest(path=key):
                self.assertIn(key, paragraphs)
                self.assertNotIn(key, self.thresholds["floors"])

    def test_the_image_side_python_still_carries_the_floors_the_page_credits_it_with(self):
        """
        "both carry coverage floors" is the sentence that stops the page from conflating the
        image-side Python with the shell it sits next to.
        """

        claim = squash(section(self.text, "What each one does not cover"))
        self.assertIn("both carry coverage floors", claim)
        for module in (
            "containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py",
            "files/scripts/configure_signing_policy.py",
        ):
            with self.subTest(module=module):
                self.assertIn(Path(module).name, claim)
                self.assertIn(module, self.thresholds["floors"])
                self.assertGreater(self.thresholds["floors"][module], 0)

    def test_the_page_does_not_send_a_reader_to_the_closed_workflow_coverage_issue(self):
        """
        The gap is real; the tracker cited for it was not. Issue #10 was "CI test workflow
        (test.yml) collects no code coverage at all", and it closed when the coverage job
        landed -- which test.yml proves by running both.
        """

        body = TEST_YML.read_text(encoding="utf-8")
        self.assertIn("pytest-cov", body)
        self.assertIn("tests/check_coverage.py", body)
        self.assertNotIn("issues/10", squash(self.text))


class NightlyCompliance(unittest.TestCase):
    """The job that asks whether the artifact a user would pull still verifies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()
        cls.claim = squash(section(cls.text, "The nightly job answers a question the others cannot"))

    def test_it_runs_the_hour_before_the_production_build(self):
        """
        Both crons, and the gap between them, are the whole justification for the schedule the
        paragraph gives. Read as minute and hour rather than matched as a string, so a
        reformatted cron with the same meaning still passes.
        """

        gap = re.search(r"runs an (\w+) before \[?`?build\.yml`?\]?[^ ]* (\d+):(\d+)", self.claim)
        self.assertIsNotNone(gap, "the paragraph no longer states the schedule offset")
        self.assertEqual(NUMBER_WORDS.get(gap.group(1), 1), 1)
        nightly = sequence(top_level_block(NIGHTLY_YML, "on"), "schedule")
        production = sequence(top_level_block(BUILD_YML, "on"), "schedule")
        nightly_hour = int(nightly[0].removeprefix("cron: ").strip("'\"").split()[1])
        production_hour = int(production[0].removeprefix("cron: ").strip("'\"").split()[1])
        self.assertEqual(production_hour, int(gap.group(2)))
        self.assertEqual(production_hour - nightly_hour, 1)

    def test_it_verifies_with_the_exact_flag_the_user_documentation_gives(self):
        """
        "uses the exact command install-and-verify.md gives users, `--new-bundle-format=false`
        included" is only true while both files carry the flag in the command itself. The
        nightly job also explains the flag in a comment directly above it, so the workflow
        is read with comments stripped: a `grep` would stay green on the comment alone.
        """

        flag = "--new-bundle-format=false"
        self.assertIn(flag, self.claim)
        self.assertIn(flag, strip_yaml_comments(NIGHTLY_YML.read_text(encoding="utf-8")))
        self.assertIn(flag, INSTALL_AND_VERIFY.read_text(encoding="utf-8"))

    def test_it_verifies_against_the_committed_key(self):
        self.assertIn("cosign.pub", self.claim)
        self.assertTrue((REPO_ROOT / "cosign.pub").is_file())
        self.assertIn("cosign.pub", strip_yaml_comments(NIGHTLY_YML.read_text(encoding="utf-8")))

    def test_it_re_runs_the_unit_suite_and_the_coverage_gate(self):
        """"It also re-runs the unit suite and the coverage gate." Deliberate duplication."""

        self.assertIn("re-runs the unit suite and the coverage gate", self.claim)
        body = NIGHTLY_YML.read_text(encoding="utf-8")
        self.assertIn("tests/check_coverage.py", body)
        self.assertRegex(body, r"pytest\b")


class Refusals(unittest.TestCase):
    """The four messages the page promises a reader will see in a job log."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()
        cls.rows = tables(section(cls.text, "The checks that actually protect a booted machine"))[0]
        cls.messages = ci_tool_error_messages()
        cls.build = BUILD_YML.read_text(encoding="utf-8")

    def test_the_table_still_carries_four_refusals(self):
        self.assertEqual(len(self.rows), 4)

    def test_each_quoted_refusal_is_raised_where_the_page_implies(self):
        """
        Reassembled from source rather than grepped: three of these are f-strings split across
        several lines, so the single line the table quotes exists nowhere in the file.
        """

        expected = {
            "Refusing to publish an unsigned production image": None,
            "does not provide a kmod-zfs for": "ci_tools/check_akmods_cache.py",
            "Promoted digest mismatch": "ci_tools/promote_stable.py",
            "Missing required verification key file": "ci_tools/sign_image.py",
        }
        for row in self.rows:
            quoted = BACKTICKED_RE.findall(row[0])[0]
            parts = documented_parts(quoted.rstrip("…").strip())
            key = next(k for k in expected if k in quoted)
            with self.subTest(message=key):
                source = expected[key]
                if source is None:
                    self.assertTrue(
                        any(contains_in_order([line], parts) for line in self.build.splitlines()),
                        f"build.yml no longer prints {key!r}",
                    )
                    continue
                self.assertTrue(
                    any(contains_in_order(m, parts) for m in self.messages[source]),
                    f"{source} no longer raises {key!r}",
                )

    def test_the_verification_key_refusal_is_raised_on_both_paths(self):
        """
        The row says it prevents "signing or promotion" proceeding without the key. Two
        modules, and the claim is wrong if either stops checking.
        """

        parts = ["Missing required verification key file"]
        for source in ("ci_tools/sign_image.py", "ci_tools/promote_stable.py"):
            with self.subTest(module=source):
                self.assertTrue(any(contains_in_order(m, parts) for m in self.messages[source]))

    def test_the_first_rule_citation_resolves_to_the_rule_it_describes(self):
        """
        "AGENTS.md section 0 rule 1" is a numbered citation into another file. A renumbering
        that keeps the count would leave this pointing at the wrong rule, so the rule's text
        is checked, not its existence.
        """

        claim = squash(self.text)
        self.assertIn("AGENTS.md section 0 rule 1", claim)
        agents = AGENTS.read_text(encoding="utf-8")
        rule = re.search(r"^1\. \*\*(.+?)\*\*", agents, re.MULTILINE)
        self.assertIsNotNone(rule, "AGENTS.md no longer numbers its section 0 rules")
        self.assertIn("fail-closed check", rule.group(1))
        self.assertIn("never relaxing the check", claim)


class ReadingARedBuild(unittest.TestCase):
    """The workflow table and the third-party failure table."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()
        body = section(cls.text, "Reading a red build")
        cls.workflows, cls.third_party = tables(body)[0], tables(body)[1]

    def test_the_table_covers_every_workflow_a_reader_could_be_looking_at(self):
        """
        The four workflows a person watches. `ai-fix.yml`, `labeler.yml` and
        `akmods-failure-triage.yml` are deliberately absent: none of them builds anything.
        """

        named = {BACKTICKED_RE.findall(row[0])[0] for row in self.workflows}
        self.assertEqual(
            named, {"build.yml", "build-pr.yml", "build-branch.yml", "test.yml"}
        )
        for workflow in sorted(named):
            with self.subTest(workflow=workflow):
                self.assertTrue((WORKFLOW_DIR / workflow).is_file())

    def test_the_runs_on_column_matches_each_workflow_s_triggers(self):
        cells = {BACKTICKED_RE.findall(row[0])[0]: row[1] for row in self.workflows}

        build_on = top_level_block(BUILD_YML, "on")
        self.assertIn("push", cells["build.yml"])
        self.assertIn("main", sequence(build_on, "branches"))
        self.assertIn("06:00", cells["build.yml"])
        self.assertEqual(sequence(build_on, "schedule")[0].split()[2], "06")

        self.assertIn("pull request", cells["build-pr.yml"])
        self.assertIn("pull_request:", top_level_block(BUILD_PR_YML, "on"))

        self.assertIn("pull request", cells["test.yml"])
        test_on = top_level_block(TEST_YML, "on")
        self.assertIn("pull_request:", test_on)
        self.assertIn("main", sequence(test_on, "branches"))

    def test_the_branch_row_names_the_two_patterns_build_branch_excludes(self):
        """
        "every branch except `main` and `ai-fix/**`" is `branches-ignore`, retyped.

        Set equality, not containment: a pattern dropped from `branches-ignore` leaves the
        row describing an exclusion the workflow no longer makes, and containment in the
        direction of the workflow cannot see that.
        """

        cells = {BACKTICKED_RE.findall(row[0])[0]: row[1] for row in self.workflows}
        excluded = set(sequence(top_level_block(BUILD_BRANCH_YML, "on"), "branches-ignore"))
        self.assertEqual(set(BACKTICKED_RE.findall(cells["build-branch.yml"])), excluded)

    def test_the_branch_row_is_right_that_those_images_are_unsigned(self):
        row = next(r for r in self.workflows if "build-branch.yml" in r[0])
        self.assertIn("unsigned", squash(row[2]))
        self.assertNotIn("sign-image", BUILD_BRANCH_YML.read_text(encoding="utf-8"))

    def test_every_place_the_third_party_table_points_at_is_a_real_job_or_step(self):
        """
        The "Where it surfaces" column is the only thing that makes the third row usable: its
        message reads like a signing-policy problem and is not, and the tell is which step
        failed. A name that no longer exists costs the reader exactly that.
        """

        known = set(job_names(BUILD_YML).values()) | set(job_names(BUILD_BRANCH_YML).values())
        known |= set(step_names(BUILD_YML)) | set(step_names(BUILD_BRANCH_YML))
        for row in self.third_party:
            for name in BACKTICKED_RE.findall(row[1]):
                with self.subTest(name=name):
                    self.assertIn(name, known)

    def test_the_sigstore_row_points_at_the_tool_installation_step(self):
        """The trap the paragraph below the table explains: it fails before this repo's code."""

        row = next(r for r in self.third_party if "trusted root" in r[0])
        self.assertIn("Install skopeo and cosign", BACKTICKED_RE.findall(row[1]))
        self.assertIn("Install skopeo and cosign", step_names(BUILD_YML))


class ParserTests(unittest.TestCase):
    """The hand-rolled helpers, against fixtures. Nothing above is trustworthy without these."""

    def test_section_returns_one_section_and_stops_at_the_next_heading(self):
        text = "# T\nintro\n\n## A\nbody a\n\n### A1\ndeep\n\n## B\nbody b\n"
        self.assertEqual(section(text, "A").strip(), "body a\n\n### A1\ndeep")
        self.assertEqual(section(text, "A1").strip(), "deep")
        self.assertEqual(section(text, "B").strip(), "body b")
        with self.assertRaises(AssertionError):
            section(text, "C")

    def test_tables_drops_the_header_and_reads_indented_rows(self):
        text = "| H | I |\n| --- | --- |\n| a | b |\n\ntext\n\n   | X |\n   | --- |\n   | y |\n"
        self.assertEqual(tables(text), [[["a", "b"]], [["y"]]])

    def test_tables_ignores_a_table_with_no_body_rows(self):
        self.assertEqual(tables("| H |\n| --- |\n"), [])

    def test_fenced_returns_only_blocks_of_the_requested_language(self):
        text = "```yaml\na: 1\n```\n\n```bash\nls\n```\n\n```yaml\nb: 2\n```\n"
        self.assertEqual(fenced(text, "yaml"), ["a: 1", "b: 2"])
        self.assertEqual(fenced(text, "bash"), ["ls"])

    def test_strip_yaml_comments_keeps_a_hash_inside_a_value(self):
        text = "key:\n  # a comment\n  a: '0 # 6 * * *'\n\n  b: ${{ x }}\n"
        self.assertEqual(strip_yaml_comments(text), "key:\n  a: '0 # 6 * * *'\n  b: ${{ x }}")

    def test_top_level_block_stops_at_the_next_column_zero_line(self):
        text = "on:\n  push:\nconcurrency:\n  group: g\n  cancel-in-progress: true\njobs:\n  a:\n"
        path = self._tmp(text)
        self.assertEqual(
            top_level_block(path, "concurrency"),
            "concurrency:\n  group: g\n  cancel-in-progress: true",
        )
        with self.assertRaises(AssertionError):
            top_level_block(path, "permissions")

    def test_sequence_reads_items_and_stops_at_a_sibling_key(self):
        block = "on:\n  push:\n    branches:\n      - main\n      # note\n      - 'x/**'\n    p: 1\n"
        self.assertEqual(sequence(block, "branches"), ["main", "x/**"])
        with self.assertRaises(AssertionError):
            sequence(block, "schedule")

    def test_job_names_falls_back_to_the_id_and_stops_after_the_jobs_block(self):
        text = "name: W\njobs:\n  a:\n    name: Alpha\n    runs-on: x\n  b:\n    runs-on: y\n"
        self.assertEqual(job_names(self._tmp(text)), {"a": "Alpha", "b": "b"})

    def test_job_names_does_not_read_a_step_name_as_a_job_name(self):
        text = "jobs:\n  a:\n    name: Alpha\n    steps:\n      - name: Step One\n"
        self.assertEqual(job_names(self._tmp(text)), {"a": "Alpha"})

    def test_step_names_reads_steps_at_any_depth(self):
        text = "jobs:\n  a:\n    steps:\n      - name: One\n      - name: 'Two'\n"
        self.assertEqual(step_names(self._tmp(text)), ["One", "Two"])

    def test_literal_segments_flattens_the_three_shapes_that_occur(self):
        plain = ast.parse('CiToolError("a b")', mode="eval").body.args[0]
        self.assertEqual(literal_segments(plain), ["a b"])
        fstring = ast.parse('CiToolError(f"a {x} b")', mode="eval").body.args[0]
        self.assertEqual(literal_segments(fstring), ["a ", "\x00", " b"])
        # Implicit adjacency is folded into one JoinedStr by the parser, so the seam is gone
        # before this function sees it; an explicit `+` is the shape that still arrives as two.
        adjacent = ast.parse('CiToolError(f"a {x} " "b")', mode="eval").body.args[0]
        self.assertEqual(literal_segments(adjacent), ["a ", "\x00", " b"])
        added = ast.parse('CiToolError(f"a {x}" + "b")', mode="eval").body.args[0]
        self.assertEqual(literal_segments(added), ["a ", "\x00", "b"])
        other = ast.parse("CiToolError(msg)", mode="eval").body.args[0]
        self.assertEqual(literal_segments(other), ["\x00"])

    def test_contains_in_order_will_not_match_across_an_interpolation(self):
        segments = ["copying ", "\x00", " produced ", "\x00", ", expected "]
        self.assertTrue(contains_in_order(segments, ["copying", "produced", ", expected"]))
        self.assertFalse(contains_in_order(segments, ["copying to produced"]))
        self.assertFalse(contains_in_order(segments, ["produced", "copying"]))

    def test_documented_parts_splits_on_the_elision_the_page_uses(self):
        self.assertEqual(
            documented_parts("Shared akmods cache … does not provide a kmod-zfs for …"),
            ["Shared akmods cache", "does not provide a kmod-zfs for"],
        )

    def test_squash_joins_a_wrapped_sentence(self):
        self.assertEqual(
            squash("The dashboard is\nfive badges in\n`README.md`"),
            "The dashboard is five badges in `README.md`",
        )

    def _tmp(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "workflow.yml"
        path.write_text(text, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
