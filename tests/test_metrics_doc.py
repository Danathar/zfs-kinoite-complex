"""
Script: tests/test_metrics_doc.py
What: Holds docs/metrics.md, and the dated snapshots under docs/metrics/, to the commands, workflows and guard messages they tell an operator to run and read.
Doing: Tokenizes the document's bash blocks, joins every `gh` invocation's `--json` field list to the jq filter that reads it, compares the quoted coverage command against the three workflows that run it, executes tests/check_coverage.py to prove the message the document quotes is the message the gate prints, and runs the documented failure classifier against the real guard strings.
Why: Almost every line of this page is either a command someone will run or a literal copied by hand out of the machine, and each one fails silently when the machine moves -- a dropped `--json` field prints `null`, a reworded guard makes the triage grep match nothing, which reads as "no failures of that class".
Goal: Make a change that invalidates the documented measurement recipe fail in CI, instead of leaving an operator holding a command that quietly reports the wrong thing.

Before this file, `docs/metrics.md` was opened by no test at any tier. The only
occurrence of the word anywhere under `tests/` was a prose comment in
tests/test_docs_consistency.py, whose directory-wide scans check the document's
links, its anchors and its presence in the documentation-guide tree -- never a
claim inside it.

What this file does not restate: link and anchor resolution (owned by
tests/test_docs_consistency.py) and the coverage gate's own behaviour (owned by
tests/test_check_coverage.py). The gate is executed here for one narrow reason:
the document quotes the shape of one of its output lines, and that quotation is
only worth anything if the line is produced.

Standard library only, and no PyYAML, for the reason tests/test_docs_consistency.py
gives: the CI job installs pytest, pytest-cov and ruff, so a third-party parser
would depend on the runner image and start skipping silently the day that
changed. The workflow assertions are therefore text assertions over the YAML,
which is what every other workflow test in this tree does.

The shell tokenizer below exists because the field/filter join cannot be done
with a regular expression: the jq programs contain pipes, braces and nested
quotes, and one `gh` call is inside a `"$(...)"` substitution inside a `printf`
argument. Tokenizing is the difference between checking every documented
invocation and checking the easy ones.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.check_coverage import main as coverage_gate_main
from tests.test_docs_consistency import REPO_ROOT

DOC_PATH = REPO_ROOT / "docs" / "metrics.md"
SNAPSHOT_DIR = REPO_ROOT / "docs" / "metrics"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
THRESHOLDS_PATH = REPO_ROOT / ".coverage-thresholds.json"

# The three workflows that run the coverage command the document quotes. The
# document is a fourth copy of it; these are the copies that decide CI.
COVERAGE_WORKFLOWS = ("test.yml", "nightly-compliance.yml", "ai-fix.yml")

# The workflows that build and promote a production image. The document's final
# section says there is no instrumentation on these, and that the missing
# production execution coverage is a tracked gap rather than a settled design.
PRODUCTION_WORKFLOWS = ("build.yml", "build-pr.yml", "build-branch.yml")

# Each alternative of the document's failure classifier, mapped to the tracked
# file that emits it -- or to None for a message this repository does not
# produce (buildah's own output, and a transient registry error), which the
# document's cause table attributes to somebody else.
CLASSIFIER_OWNERS = {
    "SIGNING_SECRET is not configured": ".github/workflows/build.yml",
    "unexpected EOF": None,
    "does not provide a kmod-zfs": "ci_tools/check_akmods_cache.py",
    "Promoted digest mismatch": "ci_tools/promote_stable.py",
    'Error: building at STEP "[A-Z]+ [^"]{0,40}': None,
}

FENCE = re.compile(r"^```(\S*)\s*$")

# A jq path step is a top-level field read only when the character before the
# dot cannot continue an identifier: `.author.login` reads `author` from the
# object and `login` from *that*, so only the first is a `--json` field.
JQ_FIELD = re.compile(r"(?<![A-Za-z0-9_])\.([A-Za-z_][A-Za-z0-9_]*)")


def doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def snapshots() -> list[Path]:
    """The dated readings under docs/metrics/, oldest first."""

    return sorted(SNAPSHOT_DIR.glob("*.md"))


def pages() -> list[tuple[str, str]]:
    """
    docs/metrics.md and every dated snapshot, as `(path, text)`.

    A snapshot is the same commands run once and pinned, so every check on a
    command's shape -- the field/filter join, read-only, a workflow that
    exists -- applies to it exactly as it applies to the page.
    """

    found = [(str(DOC_PATH.relative_to(REPO_ROOT)), doc())]
    for path in snapshots():
        found.append((str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8")))
    return found


def workflow(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


def fenced_blocks(text: str, info: str) -> list[str]:
    """Return the bodies of fenced blocks whose info string is exactly `info`."""

    blocks: list[str] = []
    body: list[str] | None = None
    inside_other = False
    for line in text.splitlines():
        match = FENCE.match(line)
        if match is None:
            if body is not None:
                body.append(line)
            continue
        if body is None and not inside_other:
            if match.group(1) == info:
                body = []
            else:
                inside_other = True
            continue
        if inside_other:
            inside_other = False
            continue
        blocks.append("\n".join(body or []))
        body = None
    return blocks


def tokenize(text: str) -> list[str]:
    """
    Split shell text into tokens, keeping quoted runs whole.

    Newlines, pipes and semicolons survive as their own tokens so an invocation
    can be bounded; `$(` opens a fresh quoting context and `)` restores the one
    it interrupted, which is what makes the `printf '%s  ' "$(gh run view ...)"`
    line in the failure classifier reachable at all.
    """

    tokens: list[str] = []
    token = ""
    started = False
    quote = ""
    interrupted: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal token, started
        if started:
            tokens.append(token)
        token = ""
        started = False

    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if char == "\\" and following == "\n":
            index += 2
            continue
        if char == "$" and following == "(":
            flush()
            interrupted.append(quote)
            quote = ""
            tokens.append("(")
            index += 2
            continue
        if quote:
            if quote == '"' and char == "\\" and following:
                token += following
                started = True
                index += 2
                continue
            if char == quote:
                quote = ""
                index += 1
                continue
            token += char
            started = True
            index += 1
            continue
        if char in "'\"":
            quote = char
            started = True
            index += 1
            continue
        if char == ")" and interrupted:
            flush()
            quote = interrupted.pop()
            tokens.append(")")
            index += 1
            continue
        if char == "\n" or char in "|;()":
            flush()
            tokens.append("\n" if char == "\n" else char)
            index += 1
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        token += char
        started = True
        index += 1
    flush()
    return tokens


BOUNDARIES = frozenset({"|", ";", "(", ")", "\n"})


def invocations(program: str, text: str | None = None) -> list[list[str]]:
    """Every `program ...` argv in a page's bash blocks (docs/metrics.md by default), in order."""

    found: list[list[str]] = []
    for block in fenced_blocks(doc() if text is None else text, "bash"):
        tokens = tokenize(block)
        for position, token in enumerate(tokens):
            if token != program:
                continue
            argv = [token]
            for later in tokens[position + 1 :]:
                if later in BOUNDARIES:
                    break
                argv.append(later)
            found.append(argv)
    return found


def flag_value(argv: list[str], *names: str) -> str | None:
    """The value of the first of `names` present as a separate-word flag."""

    for position, token in enumerate(argv[:-1]):
        if token in names:
            return argv[position + 1]
    return None


def fields_read(program: str) -> set[str]:
    """
    The `--json` fields a jq program reads off the payload `gh` returns.

    Everything from the first object construction onward is dropped: once the
    filter builds its own object, a later `.name` -- `sort_by(-.merged)` in the
    author split, for instance -- names a key the filter just invented, not a
    field `gh` was asked for.
    """

    head = program.split("{", 1)[0]
    return set(JQ_FIELD.findall(head))


def json_filter_pairs(text: str | None = None) -> list[tuple[list[str], list[str], str]]:
    """Every documented `gh` call that pairs `--json` with a jq filter."""

    pairs = []
    for argv in invocations("gh", text):
        fields = flag_value(argv, "--json")
        program = flag_value(argv, "-q", "--jq")
        if fields is None or program is None:
            continue
        pairs.append((argv, fields.split(","), program))
    return pairs


class GhInvocationTests(unittest.TestCase):
    """
    The `--json` field list and the jq filter are written metres apart on the
    same line and neither mentions the other. `gh` does not error when a filter
    reads a field that was not requested -- it prints `null`, or drops the key
    from every object -- so a documented command can go on running and start
    reporting nothing.
    """

    def test_the_document_still_carries_its_commands(self) -> None:
        """
        A floor on the parser, not on the document. Every other test here
        iterates over what the tokenizer finds, so a tokenizer that finds
        nothing would turn this whole file green while checking nothing.
        """

        self.assertGreaterEqual(len(fenced_blocks(doc(), "bash")), 7)
        self.assertGreaterEqual(len(invocations("gh")), 8)
        self.assertGreaterEqual(len(json_filter_pairs()), 6)

    def test_every_filter_only_reads_requested_fields(self) -> None:
        for page, text in pages():
            for argv, fields, program in json_filter_pairs(text):
                with self.subTest(page=page, filter=program):
                    read = fields_read(program)
                    if not read:
                        # The only filter allowed to read nothing is a bare
                        # count: `-q 'length'` works whatever `--json` asked
                        # for. Anything else reading nothing means the parse
                        # failed, and a failed parse would make this whole test
                        # pass while checking nothing.
                        self.assertEqual(
                            program.strip(),
                            "length",
                            f"no field read parsed out of {program!r}; the join is vacuous for "
                            f"{' '.join(argv)}",
                        )
                        continue
                    missing = sorted(read - set(fields))
                    self.assertEqual(
                        missing,
                        [],
                        f"{page}: {' '.join(argv)} filters on {missing} but does not request "
                        "it with --json, so the documented command prints null",
                    )

    def test_every_gh_command_is_a_read(self) -> None:
        """
        The document is a measurement page and says so: every command in it is
        something an operator runs to look. A mutating `gh` subcommand landing
        here would be a command someone runs on a production repository because
        a metrics page told them to.
        """

        readers = {("pr", "list"), ("run", "list"), ("run", "view"), ("api",)}
        for page, text in pages():
            for argv in invocations("gh", text):
                words = tuple(word for word in argv[1:] if not word.startswith("-"))
                with self.subTest(page=page, command=" ".join(argv)):
                    self.assertIn(
                        words[:2] if words[:2] in readers else words[:1],
                        readers,
                        f"{page}: {' '.join(argv)} is not one of the documented read commands",
                    )
                    if words[:1] == ("api",):
                        self.assertIsNone(
                            flag_value(argv, "-X", "--method"),
                            f"a `gh api` call in {page} names an HTTP method, so it is no "
                            "longer a plain read",
                        )

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_every_filter_runs_against_the_fields_it_requests(self) -> None:
        """
        Executes each documented filter against a payload carrying exactly the
        `--json` fields that invocation asks for. This catches what the static
        join cannot: a filter that is no longer valid jq, and one that indexes a
        requested field the wrong way.
        """

        # `gh pr list` and `gh run list` both return an array of objects; the
        # values are shaped to the ones the document's own worked examples show.
        sample = {
            "number": 28,
            "state": "MERGED",
            "mergedAt": "2026-09-04T00:00:00Z",
            "author": {"login": "Danathar"},
            "createdAt": "2026-09-04T06:00:00Z",
            "conclusion": "failure",
            "databaseId": 4242,
        }
        for page, text in pages():
            for argv, fields, program in json_filter_pairs(text):
                with self.subTest(page=page, filter=program):
                    one = {field: sample[field] for field in fields}
                    # `gh <thing> list` returns an array; `gh <thing> view`
                    # returns the object itself, and the documented filters are
                    # written for exactly that difference.
                    payload = json.dumps(one if "view" in argv else [one])
                    result = subprocess.run(
                        ["jq", "-r", program],
                        input=payload,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        f"{page}: {' '.join(argv)}'s filter failed on its own fields: "
                        f"{result.stderr.strip()}",
                    )
                    self.assertNotIn(
                        "null",
                        result.stdout,
                        f"{page}: {' '.join(argv)} prints null for a payload carrying every "
                        "field it requested",
                    )


class WorkflowTargetTests(unittest.TestCase):
    """
    Three of the documented commands name a workflow file and one of them also
    names an event. Both are strings `gh` accepts happily: a renamed workflow,
    or a `schedule:` trigger that was removed, turns the documented command into
    one that prints an empty list. An empty list from a health command reads as
    health.
    """

    def documented_workflows(self, text: str | None = None) -> list[tuple[list[str], str, str | None]]:
        named = []
        for argv in invocations("gh", text):
            name = flag_value(argv, "--workflow", "-w")
            if name is not None:
                named.append((argv, name, flag_value(argv, "--event", "-e")))
        return named

    def test_every_named_workflow_is_tracked(self) -> None:
        self.assertGreaterEqual(len(self.documented_workflows()), 3)
        for page, text in pages():
            for argv, name, _ in self.documented_workflows(text):
                with self.subTest(page=page, workflow=name):
                    self.assertTrue(
                        (WORKFLOW_DIR / name).is_file(),
                        f"{page}: {' '.join(argv)} names .github/workflows/{name}, which does "
                        "not exist",
                    )

    def test_event_schedule_is_only_asked_of_a_scheduled_workflow(self) -> None:
        for page, text in pages():
            for argv, name, event in self.documented_workflows(text):
                if event != "schedule":
                    continue
                with self.subTest(page=page, workflow=name):
                    source = workflow(name)
                    self.assertRegex(
                        source,
                        r"(?m)^  schedule:$",
                        f"{page}: {' '.join(argv)} filters on --event schedule but {name} has "
                        "no schedule trigger, so the documented command returns nothing",
                    )
                    self.assertRegex(
                        source,
                        r"(?m)^\s+- cron: ",
                        f"{name} has a schedule: block with no cron entry",
                    )

    def test_the_scheduled_workflow_is_the_one_that_moves_latest(self) -> None:
        """
        The section's whole argument is that a lost scheduled run is a skipped
        image refresh "for anything tracking `:latest`". That is only true of
        the workflow that promotes; if promotion moved elsewhere, the command
        would still run and the reasoning around it would be wrong.
        """

        scheduled = {
            name for _, name, event in self.documented_workflows() if event == "schedule"
        }
        self.assertEqual(scheduled, {"build.yml"})
        self.assertRegex(workflow("build.yml"), r"(?m)^  promote-stable:$")
        self.assertIn("ci_tools.cli promote-stable", workflow("build.yml"))

    def test_each_command_filters_the_way_its_section_reads(self) -> None:
        """
        The two sections ask different questions of different workflows, and the
        `--event` filter is the whole difference: scheduled-build health counts
        only runs the cron started, and unit CI health counts every run. Swap or
        drop the filter and the command still succeeds -- it just answers a
        question the prose around it does not ask.
        """

        expected = {"build.yml": "schedule", "test.yml": None}
        for argv, name, event in self.documented_workflows():
            with self.subTest(command=" ".join(argv)):
                self.assertIn(
                    name,
                    expected,
                    f"{name} is documented but this test records no expected event filter",
                )
                self.assertEqual(
                    event,
                    expected[name],
                    f"{' '.join(argv)} filters on --event {event!r}; the section around it "
                    f"reads as {expected[name]!r}",
                )

    def test_the_cheap_gate_runs_per_change_and_cancels_itself(self) -> None:
        """
        The unit CI section says two things about test.yml that are properties
        of the file: it is the gate that runs on every change (so the command
        deliberately passes no `--event`), and `cancelled` is "common and
        benign" because back-to-back merges cancel each other.
        """

        unscheduled = {
            name for _, name, event in self.documented_workflows() if event is None
        }
        self.assertEqual(unscheduled, {"test.yml"})
        text = workflow("test.yml")
        self.assertNotRegex(
            text,
            r"(?m)^  schedule:$",
            "docs/metrics.md documents test.yml health without --event, which only "
            "reads correctly while every run comes from a change",
        )
        self.assertRegex(text, r"(?m)^  pull_request:$")
        self.assertIn("cancel-in-progress: true", text)

    def test_the_bot_author_is_the_app_that_opens_pull_requests(self) -> None:
        """
        The author split names `app/danathar-atomic-hive`, which is how `gh`
        spells an App in `author.login` on a pull request. The same App is named
        `danathar-atomic-hive[bot]` in ai-fix.yml. Two spellings, one slug: if
        the App is ever replaced, the documented bucket silently becomes a
        bucket nothing lands in.
        """

        match = re.search(r"`app/([a-z0-9-]+)`", doc())
        self.assertIsNotNone(match, "the author split no longer names an app/ login")
        slug = match.group(1)
        self.assertIn(f"{slug}[bot]", workflow("ai-fix.yml"))


class CoverageCommandTests(unittest.TestCase):
    """
    The coverage section is a hand copy of a workflow step, and three workflows
    run that step. A `--cov` path added to the workflows and not here is a
    module the documented command does not measure, which makes
    `tests/check_coverage.py` fail for a reader following this page -- the gate
    treats an unmeasured floor as a stale entry to remove.
    """

    def documented_pytest(self) -> list[str]:
        for argv in invocations("python3"):
            if "pytest" in argv:
                return argv
        self.fail("docs/metrics.md no longer documents a pytest invocation")

    def cov_paths(self, text: str) -> set[str]:
        return set(re.findall(r"--cov=(\S+)", text))

    def test_the_documented_cov_set_is_the_set_ci_measures(self) -> None:
        documented = self.cov_paths(" ".join(self.documented_pytest()))
        self.assertEqual(len(documented), 4)
        for name in COVERAGE_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertEqual(
                    documented,
                    self.cov_paths(workflow(name)),
                    f"docs/metrics.md and {name} disagree about which paths are measured",
                )

    def test_every_documented_cov_path_exists(self) -> None:
        for path in self.cov_paths(" ".join(self.documented_pytest())):
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).is_dir(),
                    f"docs/metrics.md measures {path}, which is not a directory here",
                )

    def test_the_documented_command_measures_every_recorded_floor(self) -> None:
        """
        Both directions. A floor under no documented `--cov` path is a module
        the reader's run will not measure; a documented path holding no floor is
        a path being measured for no recorded reason.
        """

        documented = sorted(self.cov_paths(" ".join(self.documented_pytest())))
        floors = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))["floors"]
        for module_path in floors:
            with self.subTest(module=module_path):
                self.assertTrue(
                    any(module_path.startswith(f"{path}/") for path in documented),
                    f"{module_path} has a recorded floor but no documented --cov path "
                    "measures it",
                )
        for path in documented:
            with self.subTest(path=path):
                self.assertTrue(
                    any(module.startswith(f"{path}/") for module in floors),
                    f"docs/metrics.md measures {path}, which holds no recorded floor",
                )

    def test_the_documented_run_asks_for_the_report_the_gate_reads(self) -> None:
        argv = self.documented_pytest()
        self.assertIn("--cov-report=json", argv)
        self.assertIn("--cov-branch", argv)
        self.assertIn("tests/", argv)

    def test_the_documented_gate_command_is_the_command_ci_runs(self) -> None:
        gate = ["python3", "tests/check_coverage.py"]
        self.assertIn(gate, invocations("python3"))
        for name in COVERAGE_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn(" ".join(gate), workflow(name))

    def test_the_document_is_right_that_nothing_gates_on_a_percentage(self) -> None:
        """
        "Not a percentage" is the section's first claim, and the list at the end
        repeats it. `--cov-fail-under` anywhere in the tree would make it false.
        """

        collapsed = " ".join(doc().split())
        self.assertIn("Not a percentage", collapsed)
        self.assertIn("per-module covered-statement floor", collapsed)
        # Comment lines are where the workflows *explain* that they set no such
        # gate, so only a line that could run is an offender.
        offenders = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in WORKFLOW_DIR.glob("*.yml")
            if any(
                "--cov-fail-under" in line and not line.lstrip().startswith("#")
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        )
        self.assertEqual(
            offenders,
            [],
            "docs/metrics.md says there is no --cov-fail-under gate, but a workflow "
            f"sets one: {offenders}",
        )

    def test_the_production_workflows_carry_no_coverage_instrumentation(self) -> None:
        """
        The closing list calls production execution coverage a known gap with an
        issue number, not a settled design. If instrumentation ever lands in a
        build workflow, this page stops describing the repository.
        """

        for name in PRODUCTION_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertNotIn(
                    "--cov",
                    workflow(name),
                    f"{name} now measures coverage, so the 'deliberately not measured' "
                    "list in docs/metrics.md is stale",
                )
        self.assertRegex(
            doc(),
            r"Production execution coverage.*\n.*known gap",
            "the production-coverage entry no longer reads as a tracked gap",
        )


class GateOutputTests(unittest.TestCase):
    """
    The document quotes one line of the gate's output and explains what it
    means. The quotation is a hand copy of an f-string in
    tests/check_coverage.py, so it is checked the only way that proves anything:
    run the gate and read the line back.
    """

    def quoted_template(self) -> str:
        match = re.search(r"`(coverage: could raise[^`]*)`", doc())
        self.assertIsNotNone(match, "docs/metrics.md no longer quotes a gate output line")
        return match.group(1)

    def run_gate(self, floors: dict[str, int], covered: dict[str, int]) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thresholds = root / "thresholds.json"
            thresholds.write_text(json.dumps({"floors": floors, "unmeasured": {}}))
            report = root / "coverage.json"
            report.write_text(
                json.dumps(
                    {
                        "files": {
                            module: {
                                "summary": {
                                    "covered_lines": reached,
                                    "num_statements": max(reached, 1),
                                }
                            }
                            for module, reached in covered.items()
                        }
                    }
                )
            )
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = coverage_gate_main(
                    [
                        "--thresholds",
                        str(thresholds),
                        "--coverage-report",
                        str(report),
                        "--skip-completeness",
                    ]
                )
            self.assertEqual(exit_code, 0, stream.getvalue())
            return stream.getvalue()

    def test_the_quoted_line_is_the_line_the_gate_prints(self) -> None:
        """
        Builds the expected line from the document's own template, so rewording
        either side fails here. `<module>`, `N` and `M` are the document's
        placeholders; everything between them is its claim about the format.
        """

        template = self.quoted_template()
        pattern = re.escape(template)
        pattern = pattern.replace(re.escape("<module>"), r"(?P<module>\S+)")
        pattern = pattern.replace(re.escape("N"), r"(?P<floor>\d+)")
        pattern = pattern.replace(re.escape("M"), r"(?P<reached>\d+)")
        output = self.run_gate({"ci_tools/example.py": 7}, {"ci_tools/example.py": 9})
        lines = [line for line in output.splitlines() if "could raise" in line]
        self.assertEqual(len(lines), 1, output)
        match = re.fullmatch(pattern, lines[0])
        self.assertIsNotNone(
            match,
            f"the gate prints {lines[0]!r}, which does not match the form docs/metrics.md "
            f"quotes as {template!r}",
        )
        self.assertEqual(match.group("module"), "ci_tools/example.py")
        self.assertEqual(match.group("floor"), "7")
        self.assertEqual(match.group("reached"), "9")

    def test_the_message_means_what_the_document_says_it_means(self) -> None:
        """
        "a test landed that reaches more than the recorded floor" -- so a module
        sitting exactly on its floor must not print it. Without this, the
        quotation could be satisfied by a gate that announces every module.
        """

        self.assertIn("more than the recorded floor", doc())
        output = self.run_gate({"ci_tools/example.py": 9}, {"ci_tools/example.py": 9})
        self.assertNotIn("could raise", output)
        self.assertIn("coverage: PASS", output)

    def test_raising_the_floor_is_reported_and_never_applied(self) -> None:
        """
        The document sends the reader to CONTRIBUTING.md for why lowering a
        floor is a decision rather than a command, and says raising one "locks
        that in" -- a thing someone does, not a thing the gate does. The gate
        must therefore leave the manifest alone.
        """

        before = THRESHOLDS_PATH.read_text(encoding="utf-8")
        self.run_gate({"ci_tools/example.py": 1}, {"ci_tools/example.py": 5})
        self.assertEqual(THRESHOLDS_PATH.read_text(encoding="utf-8"), before)


class FailureClassifierTests(unittest.TestCase):
    """
    The classifier is the one command on the page whose output an operator acts
    on directly, and it is built entirely from strings copied out of other
    files. A reworded guard does not make it fail; it makes it print nothing for
    that run, which reads as an unclassified failure at worst and as no failure
    at best.

    Each alternative is accounted for by the table below, and an alternative
    that is not in the table fails the test. Adding one to the document is then
    a decision about where the string comes from, not a silent edit.
    """

    def classifier(self) -> str:
        for argv in invocations("grep"):
            if "-oE" in argv:
                return argv[argv.index("-oE") + 1]
        self.fail("docs/metrics.md no longer documents a grep -oE classifier")

    def alternatives(self) -> list[str]:
        pattern = self.classifier()
        self.assertTrue(pattern.startswith("(") and pattern.endswith(")"), pattern)
        return self.split(pattern)

    def split(self, pattern: str) -> list[str]:
        """
        Split the top-level alternation. A bracket expression in the last
        alternative contains a `|`-free but bracket-delimited range, so depth
        has to be tracked rather than assuming `|` separates alternatives.
        """

        parts: list[str] = []
        current = ""
        depth = 0
        in_bracket = False
        for position, char in enumerate(pattern[1:-1]):
            if in_bracket:
                current += char
                if char == "]" and pattern[position] != "[":
                    in_bracket = False
                continue
            if char == "[":
                in_bracket = True
                current += char
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "|" and depth == 0:
                parts.append(current)
                current = ""
                continue
            current += char
        parts.append(current)
        return parts

    def test_every_alternative_is_accounted_for(self) -> None:
        found = self.alternatives()
        self.assertEqual(
            sorted(found),
            sorted(CLASSIFIER_OWNERS),
            "the documented classifier's alternatives no longer match the table in this "
            "test; record where each new string comes from",
        )

    def test_every_repository_owned_string_is_still_emitted(self) -> None:
        for alternative, owner in CLASSIFIER_OWNERS.items():
            if owner is None:
                continue
            with self.subTest(alternative=alternative):
                text = (REPO_ROOT / owner).read_text(encoding="utf-8")
                self.assertIn(
                    alternative,
                    text,
                    f"docs/metrics.md greps for {alternative!r}, which {owner} no longer "
                    "emits, so the documented triage command matches nothing",
                )

    def test_the_upstream_strings_are_not_this_repository_s_messages(self) -> None:
        """
        The cause table attributes these two to COPR and to a registry CDN. If
        this repository ever started printing one of them, the table would be
        sending a reader upstream to chase its own guard.
        """

        # Only text this repository can print counts. ci_tools/resolve_build_inputs.py
        # discusses `unexpected EOF` at length in a comment explaining a registry
        # failure it works around, which is the document agreeing with itself, not
        # a message of ours -- so comments and docstrings are excluded and the
        # remaining string constants are what the module can emit.
        ours: list[str] = []
        for path in sorted((REPO_ROOT / "ci_tools").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                ast.get_docstring(node, clean=False)
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value not in docstrings
                ):
                    ours.append(node.value)
        emitted = "\n".join(ours)
        for alternative, owner in CLASSIFIER_OWNERS.items():
            if owner is not None:
                continue
            with self.subTest(alternative=alternative):
                literal = alternative.split("[")[0]
                self.assertNotIn(literal, emitted, f"ci_tools/ now emits {literal!r} itself")

    def test_the_classifier_matches_the_guard_it_is_aimed_at(self) -> None:
        """
        Runs the documented pattern against the real log line each guard writes.
        The static join above proves the substring is still in the source; this
        proves the pattern still selects it out of a line of output.
        """

        pattern = re.compile(self.classifier())
        live = {
            ".github/workflows/build.yml": "SIGNING_SECRET is not configured",
            "ci_tools/check_akmods_cache.py": "does not provide a kmod-zfs",
            "ci_tools/promote_stable.py": "Promoted digest mismatch",
        }
        for owner, alternative in live.items():
            with self.subTest(owner=owner):
                text = (REPO_ROOT / owner).read_text(encoding="utf-8")
                line = next(
                    line for line in text.splitlines() if alternative in line
                )
                match = pattern.search(line)
                self.assertIsNotNone(match, f"{owner}'s message is no longer selected")
                self.assertEqual(match.group(0), alternative)

    def test_the_cause_table_quotes_the_signing_guard_verbatim(self) -> None:
        """
        The worked example quotes the guard as a sentence, elided with an
        ellipsis. That is a longer copy than the grep alternative, and the part
        before the ellipsis has to still be what build.yml says -- otherwise the
        table teaches a message no run produces.
        """

        match = re.search(r"\| `(SIGNING_SECRET[^`]*?)…` \|", doc())
        self.assertIsNotNone(match, "the cause table no longer quotes the signing guard")
        quoted = match.group(1)
        self.assertIn("Refusing to publish an unsigned production image", quoted)
        self.assertIn(quoted, workflow("build.yml"))


class DatedFigureTests(unittest.TestCase):
    """
    The document's own rule, stated in bold near the top: every figure it quotes
    is dated, because the commands are the durable part and the numbers are
    worked examples. An undated figure is the failure mode -- it reads as a fact
    about the repository forever.
    """

    def test_every_as_of_figure_carries_a_real_date(self) -> None:
        text = doc()
        markers = re.findall(r"As of (\S+)", text)
        self.assertGreaterEqual(len(markers), 3)
        dated = re.findall(r"As of (\d{4}-\d{2}-\d{2})", text)
        self.assertEqual(
            len(dated),
            len(markers),
            "an 'As of' figure in docs/metrics.md is not followed by an ISO date",
        )
        for marker in dated:
            with self.subTest(marker=marker):
                dt.date.fromisoformat(marker)

    def test_the_worked_example_table_dates_every_row(self) -> None:
        """
        The classification table is the page's one table of numbers. Each row
        carries the dates it covers; a row without them cannot be re-checked
        against the command that produced it.
        """

        rows = [
            line
            for line in doc().splitlines()
            if line.startswith("| 20") and line.count("|") >= 4
        ]
        self.assertGreaterEqual(len(rows), 3)
        for row in rows:
            with self.subTest(row=row[:40]):
                self.assertRegex(row.split("|")[1], r"\d{4}-\d{2}-\d{2}")

    def test_no_pull_request_count_is_quoted_without_a_date(self) -> None:
        """
        The defect this was written for: the "deliberately not measured" list
        read "At thirteen pull requests these are noise" while the dated figure
        four sections above said 28 merged. An undated count is the exact thing
        the page's own rule forbids -- it is read as a fact about the repository
        and it was already wrong when this test was written. Fixed in the same
        change; this keeps the next one from landing.
        """

        counted = re.compile(
            r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
            r"thirteen|fourteen|fifteen|twenty|dozen)\s+pull requests\b",
            re.IGNORECASE,
        )
        for paragraph in re.split(r"\n\s*\n", doc()):
            collapsed = " ".join(paragraph.split())
            match = counted.search(collapsed)
            if match is None:
                continue
            with self.subTest(paragraph=collapsed[:60]):
                self.assertRegex(
                    collapsed,
                    r"\d{4}-\d{2}-\d{2}",
                    f"{match.group(0)!r} is quoted with no date beside it",
                )

    def test_the_page_states_that_its_numbers_may_be_stale(self) -> None:
        # Line-wrapped prose, so the needles are matched against the document
        # with its wrapping collapsed.
        text = " ".join(doc().split())
        self.assertIn("**Every figure quoted below is dated.**", text)
        self.assertIn("the command is right and the number is stale", text)

    def test_nothing_collects_these_numbers_on_a_schedule(self) -> None:
        """
        "There is no metrics service and no scheduled collector." A workflow
        that reads this page, or a job named for metrics, would make the opening
        paragraph false -- and would mean the commands here are no longer the
        only way the numbers are produced.
        """

        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            with self.subTest(workflow=path.name):
                text = path.read_text(encoding="utf-8")
                # Covers the dated snapshots under docs/metrics/ too: each is
                # read by hand and left as it was read.
                self.assertNotIn("docs/metrics", text)
                named = [
                    line.strip()
                    for line in text.splitlines()
                    if re.match(r"\s*(-\s*)?(name|run):", line) and "metric" in line.lower()
                ]
                self.assertEqual(named, [], f"{path.name} now collects metrics: {named}")


class SnapshotTests(unittest.TestCase):
    """
    docs/metrics/YYYY-MM-DD.md is one reading of this page's numbers, and it
    makes three promises in its first paragraph: it was read on the day in its
    name, every command names this repository, and every command is pinned so
    rerunning it later prints the numbers in the table. Each promise is a
    string an edit can quietly break -- a `--repo` dropped when a command is
    copied from the page, or a `--created` bound left at an older date -- and
    the command still runs, printing numbers that no longer match the table.

    The command-shape checks above (fields against filters, read-only, named
    workflows) already run over every snapshot through pages().
    """

    # The repository every snapshot command names, so it reads this repository
    # whatever the clone's remotes are.
    REPO = "Danathar/zfs-kinoite-complex"

    def test_the_page_points_at_a_snapshot_that_exists(self) -> None:
        found = snapshots()
        self.assertNotEqual(found, [], "docs/metrics/ holds no snapshot")
        linked = re.findall(r"\]\(\./metrics/([^)#]+)\)", doc())
        self.assertNotEqual(linked, [], "docs/metrics.md no longer links into docs/metrics/")
        for name in linked:
            with self.subTest(link=name):
                self.assertIn(name, [path.name for path in found])

    def test_each_snapshot_is_named_for_the_day_it_was_read(self) -> None:
        for path in snapshots():
            with self.subTest(snapshot=path.name):
                day = dt.date.fromisoformat(path.stem).isoformat()
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[0], f"# Metrics snapshot — {day}")
                self.assertIn(f"read once on {day}", " ".join(text.split()))

    def test_each_snapshot_still_carries_its_commands(self) -> None:
        # A floor on the tokenizer for these files, for the same reason as the
        # page's own: every check below iterates over what it finds.
        for path in snapshots():
            with self.subTest(snapshot=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertGreaterEqual(len(fenced_blocks(text, "bash")), 4)
                self.assertGreaterEqual(len(invocations("gh", text)), 6)

    def test_every_command_names_this_repository(self) -> None:
        for path in snapshots():
            for argv in invocations("gh", path.read_text(encoding="utf-8")):
                with self.subTest(snapshot=path.name, command=" ".join(argv)):
                    if argv[1] == "api":
                        endpoint = next(word for word in argv[2:] if not word.startswith("-"))
                        self.assertTrue(
                            endpoint.startswith(f"repos/{self.REPO}/"),
                            f"{endpoint} does not name {self.REPO}",
                        )
                    else:
                        self.assertEqual(flag_value(argv, "--repo", "-R"), self.REPO)

    def test_every_list_command_is_pinned_to_the_reading(self) -> None:
        """
        A run list is bounded by `--created '<day'`, the day in the file's
        name, and a pull request list by one `.number <= N` shared by the whole
        file. `gh run view` and `gh api` read one item named by a pinned list,
        so they need no bound of their own.
        """

        for path in snapshots():
            bounds: set[str] = set()
            for argv in invocations("gh", path.read_text(encoding="utf-8")):
                with self.subTest(snapshot=path.name, command=" ".join(argv)):
                    if argv[1:3] == ["run", "list"]:
                        self.assertEqual(flag_value(argv, "--created"), f"<{path.stem}")
                    elif argv[1:3] == ["pr", "list"]:
                        match = re.search(r"\.number <= (\d+)", flag_value(argv, "-q", "--jq") or "")
                        self.assertIsNotNone(match, "a pull request list with no .number <= bound")
                        bounds.add(match.group(1))
            with self.subTest(snapshot=path.name):
                self.assertEqual(len(bounds), 1, f"pull request bounds disagree: {sorted(bounds)}")


if __name__ == "__main__":
    unittest.main()
