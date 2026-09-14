"""
Script: tests/test_contributor_instructions.py
What: Joins `.github/pull_request_template.md` and `.github/copilot-instructions.md` to the things they restate.
Doing: Resolves their safety-critical file list, their AGENTS.md rule citations, their check commands and their claims about the tree.
Why: Both files are hand-maintained copies of rules enforced elsewhere, and until now no test opened either one.
Goal: Make a renumbered rule, a moved safety-critical file, a changed CI command or a new dependency fail here instead of silently making the instructions wrong.

These two files are the contributor-facing and Copilot-facing restatement of
`AGENTS.md` section 0. They are load-bearing in the way documentation usually
is not:

  * The pull request template is the only place a contributor is *asked* for
    the safety-critical statement `CONTRIBUTING.md` item 3 and `AGENTS.md`
    section 0 rule 2 require. If its file list drifts from rule 2, a change to
    a safety-critical file arrives with no statement and nothing notices --
    the reviewer is reading a body that never prompted for one.
  * `.github/copilot-instructions.md` is loaded automatically by Copilot. It
    says "standard library only", "the version is pinned in test.yml" and
    "the floors live in .coverage-thresholds.json". A suggestion built on one
    of those sentences after it stopped being true is exactly the
    plausible-but-wrong class the agent docs are written against.

`tests/test_labeler_config.py` already joins the same file list to
`.github/labeler.yml`, the three agent docs and `docs/risk-tiers.md`, and
`tests/test_prompt_catalog.py` resolves the rule citations the `.github/prompts`
runbooks make. These two files were the remaining hand-maintained copies: the
only test that opened either of them was `tests/test_docs_consistency.py`, which
globs every tracked `*.md` and checks that their *links* resolve, so their
content was asserted by nothing.

No PyYAML and no third-party parser, for the reason
tests/test_workflow_build_container.py gives: the CI job installs only pytest,
pytest-cov and ruff, so anything else would depend on the runner image and skip
silently the day that changed. The one workflow this file reads is
`.github/workflows/test.yml`, whose steps it extracts by indentation.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"
COPILOT = REPO_ROOT / ".github" / "copilot-instructions.md"
AGENTS = REPO_ROOT / "AGENTS.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SCRIPTS_README = REPO_ROOT / ".github" / "scripts" / "README.md"
THRESHOLDS = REPO_ROOT / ".coverage-thresholds.json"

BACKTICKED_RE = re.compile(r"`([^`]+)`")
RULE_CITATION_RE = re.compile(r"AGENTS\.md\s+section 0\s+rule\s+(\d+)")
WHITESPACE_RE = re.compile(r"\s+")

# Section 0 rule 2's opening line, in the numbered rendering both AGENTS.md and
# CLAUDE.md use. tests/test_labeler_config.py reads the same marker.
RULE_2_MARKER = "Treat the build, promotion, and signing path as safety-critical"
NEXT_ITEM_RE = re.compile(r"^\s*(?:\d+\.|[-*])\s+\*\*")
SECTION_0_START = "## 0. This repository publishes a real image"

# Rule numbers the two files cite, mapped to a word that must appear in that
# rule's body. Resolving the number alone is not enough: inserting a rule
# pushes every later one down by one and every citation still resolves, just to
# the wrong rule.
CITED_RULES = {
    1: "weaken",
    2: "safety-critical",
    4: "green",
}

# The headings of the pull request template, in order. A section silently
# dropped is a question never asked, so this is compared as a whole list.
TEMPLATE_SECTIONS = [
    "What changed",
    "Safety-critical statement",
    "Rollback impact",
    "Checks",
    "Verification",
]

# CONTRIBUTING.md's numbered submission requirements, and the text in the
# template that asks for each one. The template is where a contributor meets
# these; a requirement with no prompt in the template is a requirement nobody
# is asked to meet.
CONTRIBUTING_PROMPTS = {
    1: ("python3 -m pytest tests/ -v", "ruff check ci_tools/ shared/ tests/ files/ containerfiles/"),
    2: ("Every changed line traces to the change",),
    3: ("## Safety-critical statement", "what could reach a booted machine"),
    4: ("## Rollback impact", "import those pools"),
}

# The commands the template's Checks list names, each paired with the step in
# .github/workflows/test.yml that actually runs it. A checkbox naming a command
# CI does not run is a contributor asserting something no gate checks.
CHECKBOX_COMMANDS = {
    "python3 -m pytest tests/ -v": "Run tests",
    "ruff check ci_tools/ shared/ tests/ files/ containerfiles/": "Lint with ruff",
}

# Modules `.github/copilot-instructions.md` names as carrying coverage floors
# while living outside ci_tools/ and shared/.
IMAGE_SIDE_MODULES = (
    "containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py",
    "files/scripts/configure_signing_policy.py",
)

# Trees the "Python 3, standard library only" claim covers: everything that
# runs as a module from a workflow step or inside the image build.
SHIPPED_TREES = ("ci_tools", "shared", "files/scripts", "containerfiles/zfs-akmods")

# Imports that are neither stdlib nor a module of this repository.
LOCAL_ROOTS = {"ci_tools", "shared", "tests", "check_coverage"}

# `.github/scripts/README.md` does not name these two commands today. Frozen
# rather than ignored: a new command that goes undocumented grows this set and
# fails, and documenting either of these shrinks it and fails, which is the
# only way a "documented" list stays honest without this test editing docs.
UNDOCUMENTED_COMMANDS = {"write-akmods-badge", "write-last-good-build-badge"}


def collapse(text: str) -> str:
    """Whitespace-insensitive form, so a wrapped sentence compares as one line."""

    return WHITESPACE_RE.sub(" ", text).strip()


def tracked_files() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()


def rule_2_paths() -> list[str]:
    """The backticked paths AGENTS.md section 0 rule 2 names."""

    lines = AGENTS.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if RULE_2_MARKER in line)
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if NEXT_ITEM_RE.match(line):
            break
        block.append(line)
    found = BACKTICKED_RE.findall("\n".join(block))
    return [token for token in found if "/" in token or token.endswith((".py", ".yml"))]


def template_sections() -> dict[str, str]:
    """{heading: body} for the template's `## ` sections, in file order."""

    sections: dict[str, str] = {}
    current = None
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def path_list(text: str) -> list[str]:
    """Path-looking tokens from a plain prose list, backticked or not.

    The template writes its file list as running prose inside an HTML comment
    (`build.yml, .github/actions/publish-native-image, ... or
    files/scripts/configure_signing_policy.py.`), so the backtick-based reader
    the agent docs allow would return nothing at all here -- and an empty list
    compares equal to nothing, which is how this kind of check passes while
    reading the wrong thing.
    """

    stripped = text.replace("`", "")
    tokens = re.findall(r"[A-Za-z0-9_./-]+", stripped)
    out = []
    for token in tokens:
        token = token.rstrip(".") if token.endswith((".", ",")) else token
        if token.endswith((".py", ".yml")) or "/" in token:
            out.append(token)
    return out


def workflow_steps(path: Path) -> dict[str, str]:
    """{step name: run body} for one workflow, read by indentation.

    Only `test.yml` is read this way, and only its `run:` bodies are needed, so
    this handles the two forms that file uses: a one-line `run:` and a `run: |`
    block. Step names repeat across jobs in some workflows in this repository;
    test.yml has a single job, and the caller asserts the names it expects are
    present rather than trusting a lookup to have found the right one.
    """

    steps: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    name = None
    body: list[str] | None = None
    block_indent = None
    for line in lines:
        match = re.match(r"^\s*-\s*name:\s*(.*\S)\s*$", line)
        if match:
            if name is not None and body is not None:
                steps[name] = "\n".join(body).strip()
            name = match.group(1)
            body, block_indent = None, None
            continue
        inline = re.match(r"^(\s*)run:\s*(\S.*)$", line)
        if inline and name is not None and inline.group(2) not in ("|", ">", "|-", ">-"):
            steps[name] = inline.group(2).strip()
            name, body = None, None
            continue
        block = re.match(r"^(\s*)run:\s*[|>]-?\s*$", line)
        if block and name is not None:
            body = []
            block_indent = len(block.group(1)) + 2
            continue
        if body is not None and block_indent is not None:
            if line.strip() and not line.startswith(" " * block_indent):
                steps[name] = "\n".join(body).strip()
                name, body, block_indent = None, None, None
                continue
            body.append(line[block_indent:] if len(line) >= block_indent else line)
    if name is not None and body is not None:
        steps[name] = "\n".join(body).strip()
    return steps


def module_imports(path: Path) -> list[tuple[str, int, bool]]:
    """(top-level module, line, guarded) for every import in one file.

    `guarded` means the import sits inside a `try:` whose handlers name
    ImportError -- the optional-PyYAML pattern the workflow tests use.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
            for handler in node.handlers
        ):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                guarded_lines.add(child.lineno)
    out: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name.split(".")[0], node.lineno, node.lineno in guarded_lines))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module.split(".")[0], node.lineno, node.lineno in guarded_lines))
    return out


def third_party(path: Path) -> list[tuple[str, int, bool]]:
    return [
        entry
        for entry in module_imports(path)
        if entry[0] not in sys.stdlib_module_names and entry[0] not in LOCAL_ROOTS
    ]


class TemplateStructureTests(unittest.TestCase):
    """The pull request template asks for what CONTRIBUTING.md requires."""

    def setUp(self) -> None:
        self.sections = template_sections()
        self.text = collapse(TEMPLATE.read_text(encoding="utf-8"))

    def test_the_template_parses_into_the_sections_it_is_supposed_to_have(self) -> None:
        # Guard the guard: several assertions below look up a section by name,
        # and a parser that returned nothing would make them all vacuous.
        self.assertEqual(TEMPLATE_SECTIONS, list(self.sections))

    def test_every_contributing_requirement_is_prompted_for(self) -> None:
        raw = CONTRIBUTING.read_text(encoding="utf-8")
        body = raw.split("## Submitting a change", 1)[1].split("\n## ", 1)[0]
        items = {int(match) for match in re.findall(r"^(\d+)\. ", body, re.MULTILINE)}
        self.assertEqual(
            set(CONTRIBUTING_PROMPTS),
            items,
            "CONTRIBUTING.md's submission list changed; the pull request template is "
            "where a contributor is asked for these, so add or drop the matching prompt",
        )
        whole = collapse(TEMPLATE.read_text(encoding="utf-8"))
        for item, anchors in sorted(CONTRIBUTING_PROMPTS.items()):
            for anchor in anchors:
                with self.subTest(item=item, anchor=anchor):
                    self.assertIn(
                        collapse(anchor),
                        whole,
                        f"CONTRIBUTING.md item {item} is not asked for by the template",
                    )

    def test_no_checkbox_ships_pre_ticked(self) -> None:
        # A `- [x]` in the template arrives already asserted on every pull
        # request, which is worse than not asking: the body claims the check
        # passed and no one ticked it.
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("- [x]", text.lower())
        self.assertEqual(4, text.count("- [ ] "), "the Checks list changed size")

    def test_the_fail_closed_checkbox_still_names_all_three_relaxations(self) -> None:
        # AGENTS.md section 0 rule 1 forbids three distinct moves, and a
        # checkbox that named only the first would let the other two through
        # while looking like the rule.
        checks = collapse(self.sections["Checks"])
        for phrase in ("relaxed", "given a fallback", "made best-effort"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, checks)


class SafetyCriticalListTests(unittest.TestCase):
    """The file list is maintained by hand in both files; join it to rule 2."""

    def setUp(self) -> None:
        self.rule_2 = rule_2_paths()
        self.tracked = set(tracked_files())

    def test_rule_2_still_parses(self) -> None:
        self.assertEqual(7, len(self.rule_2), f"rule 2 parsed as {self.rule_2}")

    def test_the_template_names_exactly_the_rule_2_files(self) -> None:
        section = template_sections()["Safety-critical statement"]
        named = path_list(section)
        self.assertEqual(
            sorted(self.rule_2),
            sorted(named),
            "the pull request template's safety-critical list has drifted from "
            "AGENTS.md section 0 rule 2; a change to a file only rule 2 names "
            "would arrive with no statement asked for",
        )

    def test_copilot_instructions_name_exactly_the_rule_2_files(self) -> None:
        text = COPILOT.read_text(encoding="utf-8")
        sentence = text.split("The safety-critical files are", 1)[1]
        sentence = sentence.split("A change to any of them", 1)[0]
        self.assertEqual(sorted(self.rule_2), sorted(path_list(sentence)))

    def test_every_named_file_exists(self) -> None:
        # A list that names a moved file is not a list: the reviewer prompt
        # points at nothing and the real file is unguarded.
        for path in self.rule_2:
            with self.subTest(path=path):
                self.assertTrue(
                    path in self.tracked
                    or any(name.startswith(path.rstrip("/") + "/") for name in self.tracked)
                    or any(name.endswith("/" + path) for name in self.tracked),
                    f"{path} is named as safety-critical but is not in the tracked tree",
                )


class RuleCitationTests(unittest.TestCase):
    """Both files defer to AGENTS.md section 0 by rule number."""

    def setUp(self) -> None:
        body = AGENTS.read_text(encoding="utf-8").split(SECTION_0_START, 1)[1]
        body = body.split("\n## ", 1)[0]
        self.rules: dict[int, str] = {}
        current = None
        for line in body.splitlines():
            match = re.match(r"^(\d+)\. (.*)$", line)
            if match:
                current = int(match.group(1))
                self.rules[current] = match.group(2)
            elif current is not None and line.startswith("   "):
                self.rules[current] += " " + line.strip()
            elif current is not None and not line.strip():
                continue
            else:
                current = None

    def test_section_0_parses_into_a_contiguous_rule_list(self) -> None:
        self.assertEqual(list(range(1, len(self.rules) + 1)), sorted(self.rules))

    def test_every_cited_rule_exists_and_still_says_what_the_citation_assumes(self) -> None:
        cited = set()
        for path in (TEMPLATE, COPILOT):
            cited.update(int(n) for n in RULE_CITATION_RE.findall(collapse(path.read_text(encoding="utf-8"))))
        self.assertTrue(cited, "no AGENTS.md rule citations were found")
        self.assertEqual(
            set(CITED_RULES),
            cited,
            "the cited rule set changed; add the new number to CITED_RULES with "
            "the word that anchors it to the right rule",
        )
        for number, anchor in sorted(CITED_RULES.items()):
            with self.subTest(rule=number):
                self.assertIn(number, self.rules, f"rule {number} is cited but does not exist")
                self.assertIn(
                    anchor,
                    self.rules[number].lower(),
                    f"AGENTS.md rule {number} no longer discusses {anchor!r}; the "
                    "citations pointing at it by number now point at a different rule",
                )


class CheckCommandTests(unittest.TestCase):
    """The commands the two files name are the commands CI runs."""

    def setUp(self) -> None:
        self.steps = workflow_steps(TEST_WORKFLOW)

    def test_the_step_reader_found_the_steps(self) -> None:
        for step in sorted(set(CHECKBOX_COMMANDS.values())):
            with self.subTest(step=step):
                self.assertIn(step, self.steps)
                self.assertTrue(self.steps[step].strip())

    def test_each_checkbox_command_is_the_command_its_step_runs(self) -> None:
        for command, step in sorted(CHECKBOX_COMMANDS.items()):
            with self.subTest(command=command):
                body = self.steps[step]
                first = collapse(body.split("\\\n")[0] if "\\" in body else body.splitlines()[0])
                self.assertTrue(
                    first == command or first.startswith(command + " "),
                    f"the template's {command!r} checkbox does not match what the "
                    f"{step!r} step runs: {first!r}",
                )

    def test_the_template_checkboxes_are_the_commands_contributing_documents(self) -> None:
        # CONTRIBUTING.md's Tests block is the third copy of these commands.
        tests_block = CONTRIBUTING.read_text(encoding="utf-8").split("## Tests", 1)[1]
        for command in sorted(CHECKBOX_COMMANDS):
            with self.subTest(command=command):
                self.assertIn(command, tests_block)

    def test_copilot_instructions_name_the_same_ruff_invocation(self) -> None:
        ruff = "ruff check ci_tools/ shared/ tests/ files/ containerfiles/"
        self.assertIn(ruff, CHECKBOX_COMMANDS)
        self.assertIn(ruff, collapse(COPILOT.read_text(encoding="utf-8")))

    def test_the_ruff_pin_really_lives_where_copilot_instructions_say(self) -> None:
        # The claim is specifically "with the version pinned in
        # .github/workflows/test.yml". A pin that moved elsewhere leaves the
        # sentence pointing at a file with no version in it.
        claim = collapse(COPILOT.read_text(encoding="utf-8"))
        self.assertIn("the version pinned in `.github/workflows/test.yml`", claim)
        self.assertRegex(TEST_WORKFLOW.read_text(encoding="utf-8"), r'"ruff==\d+\.\d+\.\d+"')


class CopilotClaimTests(unittest.TestCase):
    """The claims copilot-instructions.md makes about the tree, checked."""

    def setUp(self) -> None:
        self.text = collapse(COPILOT.read_text(encoding="utf-8"))
        self.tracked = set(tracked_files())

    def test_the_shipped_trees_import_only_the_standard_library(self) -> None:
        self.assertIn("Python 3, standard library only", self.text)
        offenders = []
        for tree in SHIPPED_TREES:
            root = REPO_ROOT / tree
            self.assertTrue(root.is_dir(), f"{tree} is named by the instructions but is missing")
            for path in sorted(root.rglob("*.py")):
                for module, line, _ in third_party(path):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{line} imports {module}")
        self.assertEqual(
            [],
            offenders,
            "copilot-instructions.md tells Copilot not to add a dependency, and "
            f"one is now imported: {offenders}",
        )

    def test_the_suite_still_runs_with_nothing_installed(self) -> None:
        # "`python3 -m unittest discover -s tests` works with nothing
        # installed" is only true while every third-party import under tests/
        # is optional. An unguarded `import yaml` turns the documented no-install
        # command into a collection error, and CI would not notice: its job
        # installs pytest, pytest-cov and ruff.
        self.assertIn("python3 -m unittest discover -s tests", self.text)
        unguarded = []
        for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
            for module, line, guarded in third_party(path):
                if not guarded:
                    unguarded.append(f"{path.relative_to(REPO_ROOT)}:{line} imports {module}")
        self.assertEqual([], unguarded, f"unguarded third-party imports under tests/: {unguarded}")

    def test_the_optional_import_pattern_is_actually_in_use(self) -> None:
        # Guard the guard: the assertion above passes trivially if nothing
        # under tests/ imports anything third-party at all, which would also be
        # what a broken import reader produces.
        guarded = [
            path.name
            for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
            if any(entry[2] for entry in third_party(path))
        ]
        self.assertGreater(len(guarded), 5, "the third-party import scan found almost nothing")

    def test_the_coverage_floors_live_where_the_instructions_say(self) -> None:
        self.assertIn("`.coverage-thresholds.json`", self.text)
        self.assertIn(".coverage-thresholds.json", self.tracked)
        floors = json.loads(THRESHOLDS.read_text(encoding="utf-8"))["floors"]
        for module in IMAGE_SIDE_MODULES:
            with self.subTest(module=module):
                # The instructions say both of these "do have host-side tests"
                # and "both carry coverage floors" -- they are the two measured
                # modules outside ci_tools/ and shared/.
                self.assertIn(module, floors)
                self.assertGreater(floors[module], 0)

    def test_the_step_to_command_map_the_instructions_point_at_is_complete(self) -> None:
        self.assertIn("`.github/scripts/README.md` maps step to command to module", self.text)
        from ci_tools.cli import command_map

        documented = SCRIPTS_README.read_text(encoding="utf-8")
        absent = {name for name in command_map() if name not in documented}
        self.assertEqual(
            UNDOCUMENTED_COMMANDS,
            absent,
            "the set of ci_tools.cli commands missing from .github/scripts/README.md "
            "changed; the instructions send Copilot there for the step-to-module map",
        )


class BuildTriggerTests(unittest.TestCase):
    """Editing either file must not publish an image."""

    def setUp(self) -> None:
        text = BUILD_WORKFLOW.read_text(encoding="utf-8")
        block = text.split("paths-ignore:", 1)[1].split("\n  workflow_dispatch:", 1)[0]
        self.ignored = [
            line.strip().lstrip("- ").strip('"').strip("'")
            for line in block.splitlines()
            if line.strip().startswith("- ")
        ]

    def test_the_paths_ignore_list_parses(self) -> None:
        self.assertIn("docs/**", self.ignored)
        self.assertGreater(len(self.ignored), 8)

    def test_the_template_is_ignored_by_the_production_build(self) -> None:
        # build.yml resolves inputs, publishes a candidate, signs it and moves
        # `:latest`. A wording change in the template must not do that, and the
        # comment above the list says the rule for being on it is that the path
        # is neither copied into the image nor read by a workflow that builds
        # it -- which is the next test.
        self.assertIn(".github/pull_request_template.md", self.ignored)
        self.assertTrue(
            any(pattern in self.ignored for pattern in ("**/*.md", "*.md")),
            "copilot-instructions.md relies on the markdown glob to stay inert",
        )

    def test_no_workflow_step_reads_either_file(self) -> None:
        # Being on paths-ignore is only safe while nothing that builds reads
        # the file. A step that started reading the template would make an
        # ignored edit change what ships.
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            if workflow == BUILD_WORKFLOW:
                text = text.replace(".github/pull_request_template.md", "")
            for name in ("pull_request_template.md", "copilot-instructions.md"):
                with self.subTest(workflow=workflow.name, file=name):
                    self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
