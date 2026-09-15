"""
Script: tests/test_code_reading_guide.py
What: Joins `docs/code-reading-guide.md` to the tree, the workflows and the test tiers it describes.
Doing: Parses the guide's layout table, workflow claims, test list and run recipe, and recomputes each against the files that decide them.
Why: The guide is the page a newcomer reads instead of looking, and until this file nothing opened it -- the repository-wide link check in tests/test_docs_consistency.py resolves its links, which leaves every claim that is not a link unchecked.
Goal: Make a stale sentence in the guide fail here rather than mislead the next reader.

The guide is a map. A map is consulted *instead of* looking, so a wrong one is
worse than none: it spends the reader's trust on a path that is no longer
there. `tests/test_docs_consistency.py` already resolves every relative link in
every tracked markdown file, so "the file this points at exists" is covered.
What it cannot see is everything the guide states without linking:

  * the `Repository Layout` block -- a fenced code block, so its paths are
    plain text, not links, and nothing resolved them;
  * the `Core Workflows` bullets -- "no push and no signing", "runs on pull
    requests and pushes to main", "scheduled runs skip when the upstream
    Kinoite base image has not advanced", "Docs-only changes do not trigger
    image builds". Each is a statement about a workflow trigger or a workflow
    step, and each was checked by nobody;
  * "Every CI tool module in `ci_tools/` has a corresponding
    `tests/test_<module_name>.py` file" -- a claim about the tree that reads as
    a promise about how this repository is maintained;
  * the `Running Tests` recipe, which tells a contributor to install `pytest`
    and run `python3 -m pytest tests/ -v`. If that drifts from what
    `.github/workflows/test.yml` runs, the reader's green run and CI's red one
    disagree, and CONTRIBUTING.md links straight to this section twice;
  * "Tests use `unittest.TestCase` ... and have no external dependencies beyond
    `pytest` as the test runner", plus the `tests/e2e/` paragraph's claim that
    the tier mocks nothing and runs the CLI as a real subprocess.

Three of those were already stale when this file was written:

  * section 8 listed 16 test files and called them the tests for the modules in
    `ci_tools/`, while five modules that do have tests --
    `check_stable_signal`, `pin_akmods_cache`, `write_akmods_badge`,
    `write_last_good_build_badge` and `zfs_release` -- were missing from it. The
    guide's own "every module has a corresponding test file" sentence sits
    eleven lines below the list that contradicted it;
  * the layout block named every directory `ruff` lints except `tests/`, so the
    tier a reader is about to be told to run did not appear on the map of the
    repository;
  * the `build.yml` bullet ended `(see "Safety Model" above)`, and this document
    has no "Safety Model" section, above or anywhere. The section is in
    `docs/safety-model.md`; the reader was sent scrolling for a heading that
    does not exist.

The parsers here are hand-rolled for the reason
`tests/test_docs_consistency.py` gives: the CI job installs only `pytest`,
`pytest-cov` and `ruff`, so a third-party parser would depend on the runner
image and skip silently the day that changed. They are indentation-bound and
carry their own guard tests, because a parser that quietly returns nothing
turns every assertion built on it into a pass.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDE = REPO_ROOT / "docs" / "code-reading-guide.md"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
TEST_WORKFLOW = WORKFLOW_DIR / "test.yml"
CI_TOOLS = REPO_ROOT / "ci_tools"
TESTS_DIR = REPO_ROOT / "tests"

# Modules that live in this repository rather than on PyPI. `check_coverage` is
# tests/check_coverage.py, imported by tests/test_check_coverage.py.
FIRST_PARTY = {"ci_tools", "shared", "tests", "check_coverage"}


def guide_lines() -> list[str]:
    return GUIDE.read_text(encoding="utf-8").splitlines()


def flat(text: str) -> str:
    """Collapse whitespace so a re-wrap of a paragraph does not break a needle."""

    return " ".join(text.split())


def guide_prose() -> str:
    return flat(GUIDE.read_text(encoding="utf-8"))


def section(heading: str) -> list[str]:
    """
    The lines under `heading`, up to the next heading at the same or a higher level.

    Heading level matters: section 8's body must not swallow `#### Running
    Tests`, and `## Core Workflows` must stop at `## Read This Repo In This
    Order`.
    """

    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    collecting = False
    for line in guide_lines():
        if line.startswith("#"):
            if line.strip() == heading:
                collecting = True
                continue
            if collecting and len(line) - len(line.lstrip("#")) <= level:
                break
        if collecting:
            body.append(line)
    return body


def fenced_block(lines: list[str]) -> list[str]:
    """The first fenced code block in `lines`, fence markers removed."""

    block: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("```"):
            if inside:
                return block
            inside = True
            continue
        if inside:
            block.append(line)
    return block


def layout_entries() -> dict[str, str]:
    """
    The `Repository Layout` block as {path: description}.

    Two or more spaces separate the columns; a single space inside a
    description must not split one. A line with no column separator is a
    format change this parser should not paper over, so it raises.
    """

    entries: dict[str, str] = {}
    for line in fenced_block(section("## Repository Layout")):
        if not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        if len(parts) != 2:
            raise AssertionError(f"layout line has no description column: {line!r}")
        entries[parts[0]] = parts[1]
    return entries


def core_workflow_claims() -> dict[str, list[str]]:
    """
    The `Core Workflows` bullets as {workflow file name: [claim, ...]}.

    Indentation-bound: a top-level `- ` opens a workflow, a two-space-indented
    `- ` is one of its claims. Re-indenting the block is a change this parser
    is entitled to notice.
    """

    claims: dict[str, list[str]] = {}
    current = ""
    for line in section("## Core Workflows"):
        if line.startswith("- `"):
            current = Path(line.strip().strip("- ").strip("`")).name
            claims[current] = []
        elif line.startswith("  - ") and current:
            claims[current].append(flat(line.strip()[2:]))
    return claims


def listed_test_files() -> list[str]:
    """
    The repository-relative test paths section 8's numbered list names, in order.

    Stops at `#### Running Tests`: that subsection belongs to section 8 but is
    prose, and the paths it mentions are not part of the reading order.
    """

    listed = []
    for line in section("### 8. Tests"):
        if line.startswith("####"):
            break
        for match in re.finditer(r"\]\(\.\./(tests/[^)#]+)\)", line):
            listed.append(match.group(1))
    return listed


def reading_order_modules() -> list[str]:
    """
    Every Python module the reading order links, by repository-relative path.

    Sections 2 through 7 are the walk through the code itself; section 8 is the
    list of tests for what that walk covered, which is the join this file
    checks.
    """

    modules = []
    for number in range(2, 8):
        heading = next(
            (line.strip() for line in guide_lines() if line.startswith(f"### {number}. ")),
            "",
        )
        for line in section(heading):
            for match in re.finditer(r"\]\(\.\./([^)#]+\.py)\)", line):
                modules.append(match.group(1))
    return modules


def running_tests_commands() -> list[str]:
    return [line.strip() for line in fenced_block(section("#### Running Tests")) if line.strip()]


def workflow_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def step_run_body(path: Path, step_name: str) -> str:
    """
    The `run:` body of the named step, joined by newlines.

    Hand-rolled instead of PyYAML, and indentation-bound: the body is every
    line indented deeper than the `run: |` key, which is how a block scalar
    ends in YAML.
    """

    lines = workflow_lines(path)
    for index, line in enumerate(lines):
        if flat(line).lstrip("- ") != f"name: {step_name}":
            continue
        step_indent = len(line) - len(line.lstrip())
        for cursor in range(index + 1, len(lines)):
            current = lines[cursor]
            if current.strip() and len(current) - len(current.lstrip()) < step_indent:
                break
            if current.strip().startswith("run:"):
                inline = current.strip()[len("run:") :].strip()
                run_indent = len(current) - len(current.lstrip())
                if inline and inline != "|":
                    return inline
                body = []
                for tail in lines[cursor + 1 :]:
                    if tail.strip() and len(tail) - len(tail.lstrip()) <= run_indent:
                        break
                    body.append(tail.strip())
                return "\n".join(body).strip()
    raise AssertionError(f"no step named {step_name!r} with a run: body in {path.name}")


def trigger_block(path: Path) -> list[str]:
    """The lines under a workflow's `on:` key."""

    lines = workflow_lines(path)
    for index, line in enumerate(lines):
        if line.rstrip() != "on:":
            continue
        block = []
        for tail in lines[index + 1 :]:
            if tail.strip() and not tail.startswith(" "):
                break
            block.append(tail)
        return block
    raise AssertionError(f"no `on:` key in {path.name}")


def paths_ignore(path: Path) -> set[str]:
    """Every `paths-ignore` entry in a workflow's triggers, across all events."""

    ignored: set[str] = set()
    block = trigger_block(path)
    for index, line in enumerate(block):
        if line.strip() != "paths-ignore:":
            continue
        key_indent = len(line) - len(line.lstrip())
        for tail in block[index + 1 :]:
            if not tail.strip():
                continue
            if len(tail) - len(tail.lstrip()) <= key_indent:
                break
            ignored.add(tail.strip().lstrip("- ").strip().strip('"').strip("'"))
    return ignored


def jobs(path: Path) -> dict[str, list[str]]:
    """
    A workflow's jobs as {job id: body lines}.

    Indentation-bound in the same way as the step parser: `jobs:` is at column
    zero and a job id is the only key two spaces in under it.
    """

    lines = workflow_lines(path)
    start = lines.index("jobs:")
    found: dict[str, list[str]] = {}
    current = ""
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line.rstrip()):
            current = line.strip().rstrip(":")
            found[current] = []
        elif current:
            found[current].append(line)
    return found


def image_building_workflows() -> list[Path]:
    """
    The workflows that compose an image, computed rather than listed.

    "Composes an image" means "uses the build-native-image composite action",
    which is the only way this repository builds one.
    """

    return sorted(
        path
        for path in WORKFLOW_DIR.glob("*.yml")
        if "./.github/actions/build-native-image" in path.read_text(encoding="utf-8")
    )


def ci_tool_modules() -> list[str]:
    return sorted(path.stem for path in CI_TOOLS.glob("*.py") if path.stem != "__init__")


def tracked(*patterns: str) -> list[str]:
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", *patterns],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return listing.stdout.split()


def test_sources() -> list[Path]:
    return [REPO_ROOT / name for name in tracked("tests/*.py", "tests/e2e/*.py")]


def imported_modules(path: Path) -> list[tuple[str, bool]]:
    """
    Every module `path` imports, paired with whether the import is guarded.

    "Guarded" means the import sits inside a `try` whose handlers catch
    `ImportError`, which is how this tree makes PyYAML optional. An unguarded
    import is a hard dependency of the suite; a guarded one is not.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import = any(
            handler.type is not None and "ImportError" in ast.unparse(handler.type)
            for handler in node.handlers
        )
        if not catches_import:
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                guarded.add(id(inner))

    found: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            found.append((name, id(node) in guarded))
    return found


class ParserGuardTests(unittest.TestCase):
    """
    Guard the guards.

    Every assertion below reports an empty difference on success, and an empty
    difference is also what a parser that stopped finding anything produces.
    """

    def test_the_layout_block_parses(self) -> None:
        entries = layout_entries()
        self.assertGreater(len(entries), 8, f"implausibly small layout block: {entries}")
        self.assertIn("ci_tools/", entries)
        self.assertIn("Containerfile", entries)

    def test_the_core_workflow_bullets_parse(self) -> None:
        claims = core_workflow_claims()
        self.assertGreaterEqual(len(claims), 4, f"implausibly few workflows: {claims}")
        for workflow, bullets in claims.items():
            with self.subTest(workflow=workflow):
                self.assertTrue(bullets, "workflow bullet with no claims under it")

    def test_the_core_workflow_parser_reads_every_bullet_in_the_section(self) -> None:
        # The parser is indentation-bound, so a re-indent would silently drop
        # claims and leave the assertions built on them checking a shorter
        # list. Count instead: every bullet in the section is either a
        # workflow or one of its claims.
        bullets = [line for line in section("## Core Workflows") if line.lstrip().startswith("- ")]
        claims = core_workflow_claims()
        parsed = len(claims) + sum(len(values) for values in claims.values())
        self.assertEqual(parsed, len(bullets), "bullets the Core Workflows parser did not read")

    def test_the_test_list_parses(self) -> None:
        self.assertGreaterEqual(len(listed_test_files()), 16)

    def test_the_run_recipe_parses(self) -> None:
        commands = running_tests_commands()
        self.assertGreaterEqual(len(commands), 2, f"implausibly short recipe: {commands}")

    def test_the_workflow_step_extractor_finds_a_body(self) -> None:
        self.assertIn("pytest", step_run_body(TEST_WORKFLOW, "Run tests"))

    def test_the_trigger_parser_finds_paths_ignore(self) -> None:
        self.assertIn("docs/**", paths_ignore(WORKFLOW_DIR / "build.yml"))

    def test_the_image_building_workflows_are_found(self) -> None:
        names = [path.name for path in image_building_workflows()]
        self.assertIn("build.yml", names)
        self.assertGreaterEqual(len(names), 3, names)


class RepositoryLayoutTests(unittest.TestCase):
    def test_every_layout_path_exists_as_the_kind_it_is_written_as(self) -> None:
        # A trailing slash in the block means "directory". Writing `shared/`
        # for a file, or naming a directory that was renamed, is the drift a
        # reader cannot detect from the page.
        wrong = []
        for entry in layout_entries():
            target = REPO_ROOT / entry
            if entry.endswith("/"):
                if not target.is_dir():
                    wrong.append(f"{entry} (not a directory)")
            elif not target.is_file():
                wrong.append(f"{entry} (not a file)")
        self.assertEqual(wrong, [], "layout entries that do not describe the tree")

    def test_the_layout_names_every_directory_the_coverage_run_measures(self) -> None:
        # `--cov=` in test.yml is this repository's own list of the code its CI
        # decisions run through. A new measured path is a new part of the
        # machine, and a map that omits it sends the reader past it.
        measured = set(re.findall(r"--cov=([^\s\\]+)", step_run_body(TEST_WORKFLOW, "Run tests")))
        self.assertGreaterEqual(len(measured), 4, measured)
        entries = layout_entries()
        missing = sorted(path for path in measured if f"{path.rstrip('/')}/" not in entries)
        self.assertEqual(missing, [], "measured source paths the layout block does not name")

    def test_the_layout_names_every_directory_ruff_lints(self) -> None:
        # The same argument, for the other half of the CI job: a directory
        # worth linting holds code, and code the map omits is code nobody was
        # told to read. `tests/` was the one this caught.
        lint = step_run_body(TEST_WORKFLOW, "Lint with ruff").split()
        targets = [word for word in lint if word.endswith("/")]
        self.assertGreaterEqual(len(targets), 4, targets)
        entries = layout_entries()
        missing = sorted(
            target
            for target in targets
            if target not in entries and not any(key.startswith(target) for key in entries)
        )
        self.assertEqual(missing, [], "linted directories the layout block does not name")

    def test_the_layout_names_the_workflow_and_action_directories(self) -> None:
        # The guide's whole reading order starts at a workflow, so these two
        # are load-bearing entries rather than incidental ones.
        entries = layout_entries()
        for path in (".github/workflows/", ".github/actions/", "ci/defaults.json"):
            with self.subTest(path=path):
                self.assertIn(path, entries)


class CoreWorkflowClaimTests(unittest.TestCase):
    def test_every_named_workflow_exists(self) -> None:
        missing = [name for name in core_workflow_claims() if not (WORKFLOW_DIR / name).is_file()]
        self.assertEqual(missing, [], "workflows the guide describes that do not exist")

    def test_every_image_building_workflow_is_described(self) -> None:
        # Computed from the tree, not from a remembered list: a fourth workflow
        # that builds an image has to appear on the map of the workflows that
        # build images.
        described = set(core_workflow_claims())
        missing = sorted(path.name for path in image_building_workflows() if path.name not in described)
        self.assertEqual(missing, [], "image-building workflows the Core Workflows section omits")

    def test_docs_only_changes_really_do_not_trigger_image_builds(self) -> None:
        # "Docs-only changes do not trigger image builds." Every workflow that
        # can build one has to ignore both the docs directory and markdown
        # anywhere, or the sentence is false for whichever one dropped it.
        self.assertIn("Docs-only changes do not trigger image builds.", guide_prose())
        for path in image_building_workflows():
            with self.subTest(workflow=path.name):
                ignored = paths_ignore(path)
                self.assertIn("docs/**", ignored)
                self.assertIn("**/*.md", ignored)

    def test_the_pull_request_build_neither_pushes_nor_signs(self) -> None:
        # The claim under build-pr.yml. Both needles are checked against a
        # workflow that does use them, so a rename of the action or the command
        # fails here instead of silently making the claim unfalsifiable.
        claims = core_workflow_claims()
        self.assertIn("no push and no signing", claims["build-pr.yml"])
        production = (WORKFLOW_DIR / "build.yml").read_text(encoding="utf-8")
        self.assertIn("./.github/actions/publish-native-image", production)
        self.assertIn("cli sign-image", production)
        pull_request = (WORKFLOW_DIR / "build-pr.yml").read_text(encoding="utf-8")
        self.assertNotIn("./.github/actions/publish-native-image", pull_request)
        self.assertNotIn("cli sign-image", pull_request)

    def test_scheduled_runs_really_are_gated_on_the_stable_signal(self) -> None:
        # "scheduled runs skip when the upstream Kinoite base image has not
        # advanced since the last promoted image": a preflight job evaluates the
        # gate, and the build jobs carry the condition that acts on it.
        claim = " ".join(core_workflow_claims()["build.yml"])
        self.assertIn("scheduled runs skip", claim)
        production = workflow_lines(WORKFLOW_DIR / "build.yml")
        self.assertIn(
            "python3 -m ci_tools.cli check-stable-signal",
            step_run_body(WORKFLOW_DIR / "build.yml", "Evaluate stable signal gate"),
        )
        # Every job that spends a runner on building the image carries the
        # condition, not merely one of them: a scheduled run that skips the
        # akmods build and composes the image anyway has not skipped.
        del production
        building = {
            job: body
            for job, body in jobs(WORKFLOW_DIR / "build.yml").items()
            if any("actions/build-native-image" in line for line in body)
            or any("Build Shared ZFS Akmods Cache" in line for line in body)
        }
        self.assertGreaterEqual(len(building), 2, sorted(building))
        for job, body in building.items():
            with self.subTest(job=job):
                condition = [line for line in body if line.strip().startswith("if:")]
                self.assertTrue(condition, "job with no condition at all")
                self.assertIn("github.event_name != 'schedule'", condition[0])
                self.assertIn("should_build", condition[0])

    def test_the_test_workflow_runs_where_the_guide_says_it_does(self) -> None:
        # "runs on pull requests and pushes to main".
        self.assertIn("runs on pull requests and pushes to main", core_workflow_claims()["test.yml"])
        block = [line.rstrip() for line in trigger_block(TEST_WORKFLOW)]
        self.assertIn("  pull_request:", block)
        self.assertIn("  push:", block)
        self.assertIn("      - main", block)

    def test_the_test_workflow_runs_unit_tests_and_ruff_over_the_ci_tools(self) -> None:
        # "Python unit tests and `ruff` lint for all CI tool modules".
        self.assertIn(
            "Python unit tests and `ruff` lint for all CI tool modules",
            core_workflow_claims()["test.yml"],
        )
        lint = step_run_body(TEST_WORKFLOW, "Lint with ruff")
        self.assertTrue(lint.startswith("ruff check"), lint)
        self.assertIn("ci_tools/", lint.split())
        self.assertIn("tests/", step_run_body(TEST_WORKFLOW, "Run tests"))


class TestListTests(unittest.TestCase):
    """Section 8 and the sentence eleven lines below it have to agree."""

    def test_the_guide_still_claims_every_ci_tool_module_has_a_test(self) -> None:
        # Deleting the sentence would make the next assertion a check of
        # something the guide no longer promises.
        self.assertIn(
            "Every CI tool module in `ci_tools/` has a corresponding "
            "`tests/test_<module_name>.py` file.",
            guide_prose(),
        )

    def test_every_ci_tool_module_has_a_test_file(self) -> None:
        modules = ci_tool_modules()
        self.assertGreater(len(modules), 10, modules)
        missing = [name for name in modules if not (TESTS_DIR / f"test_{name}.py").is_file()]
        self.assertEqual(missing, [], "ci_tools modules with no tests/test_<module>.py")

    def test_section_eight_lists_the_test_for_every_ci_tool_module(self) -> None:
        # The direction that catches the stale list: five modules had tests the
        # reading order never mentioned.
        listed = set(listed_test_files())
        missing = sorted(
            f"tests/test_{name}.py"
            for name in ci_tool_modules()
            if f"tests/test_{name}.py" not in listed
        )
        self.assertEqual(missing, [], "ci_tools tests missing from the section 8 reading order")

    def test_section_eight_lists_the_test_for_every_module_the_walk_covers(self) -> None:
        # Sections 2 through 7 walk the reader through the code; section 8 is
        # "and here is where each of those is tested". A module read in the
        # walk whose test the list omits breaks that promise --
        # `shared/oci_layout.py` was the one this caught.
        walked = reading_order_modules()
        self.assertGreater(len(walked), 10, walked)
        listed = set(listed_test_files())
        missing = sorted(
            f"tests/test_{Path(name).stem}.py"
            for name in walked
            if (TESTS_DIR / f"test_{Path(name).stem}.py").is_file()
            and f"tests/test_{Path(name).stem}.py" not in listed
        )
        self.assertEqual(missing, [], "tests for walked modules missing from section 8")

    def test_section_eight_lists_nothing_but_tests_for_a_module_in_the_tree(self) -> None:
        # The opposite direction. Without it the list could be padded with
        # unrelated files and still satisfy the checks above, and it would stop
        # meaning "the tests for the modules you just read".
        stems = {Path(name).stem for name in tracked("*.py")}
        strays = sorted(
            name
            for name in listed_test_files()
            if not Path(name).stem.startswith("test_")
            or Path(name).stem[len("test_") :] not in stems
        )
        self.assertEqual(strays, [], "section 8 entries that test no module in this tree")

    def test_every_listed_test_file_exists(self) -> None:
        missing = [name for name in listed_test_files() if not (REPO_ROOT / name).is_file()]
        self.assertEqual(missing, [], "listed test files that do not exist")


class RunningTestsRecipeTests(unittest.TestCase):
    """
    CONTRIBUTING.md sends a contributor here twice, by anchor. What this
    section says has to be what CI does, or a green local run and a red CI run
    disagree for a reason the reader has no way to see.
    """

    def test_the_documented_command_is_the_command_ci_runs(self) -> None:
        documented = [line for line in running_tests_commands() if "pytest tests/" in line]
        self.assertEqual(len(documented), 1, running_tests_commands())
        first_line = step_run_body(TEST_WORKFLOW, "Run tests").splitlines()[0]
        self.assertEqual(documented[0], first_line.rstrip("\\").strip())

    def test_the_documented_install_is_a_subset_of_the_ci_install(self) -> None:
        # The guide tells a reader to install the runner only; CI adds coverage
        # and lint tooling. A package the guide names that CI does not install
        # is a recipe nobody runs.
        documented = [line for line in running_tests_commands() if line.startswith("pip install")]
        self.assertEqual(len(documented), 1, running_tests_commands())
        asked = set(documented[0].split()[2:])
        installed = {
            word.strip('"').split("==")[0]
            for word in step_run_body(TEST_WORKFLOW, "Install test runner and linter").split()
            if not word.startswith("-") and word not in {"pip", "install"}
        }
        self.assertEqual(sorted(asked - installed), [], "packages the guide names that CI omits")

    def test_no_test_file_hard_imports_a_third_party_module(self) -> None:
        # "no external dependencies beyond `pytest` as the test runner". An
        # unguarded import of anything else makes the documented `pip install`
        # line wrong for a fresh checkout -- and wrong for CI, whose install
        # step names the same three packages.
        foreign = []
        for path in test_sources():
            for name, guarded in imported_modules(path):
                if guarded:
                    continue
                root = name.split(".")[0]
                if not root or root in FIRST_PARTY or root in sys.stdlib_module_names:
                    continue
                foreign.append(f"{path.relative_to(REPO_ROOT)} imports {name}")
        self.assertEqual(sorted(foreign), [], "third-party imports the guide does not account for")

    def test_every_optional_third_party_import_is_skipped_when_absent(self) -> None:
        # The claim survives PyYAML only because those imports sit under
        # `try: ... except ImportError` and the tests that need it skip. An
        # import guarded but not skipped errors instead of skipping on the
        # runner CI actually provisions, which is the same broken recipe by a
        # slower route.
        unskipped = []
        for path in test_sources():
            optional = {
                name.split(".")[0]
                for name, guarded in imported_modules(path)
                if guarded
                and name.split(".")[0] not in FIRST_PARTY
                and name.split(".")[0] not in sys.stdlib_module_names
            }
            if not optional:
                continue
            source = path.read_text(encoding="utf-8")
            guards = re.findall(r"@unittest\.skip(?:If|Unless)\(([^)]*)\)", source)
            for module in sorted(optional):
                if not any(module in guard for guard in guards):
                    unskipped.append(f"{path.relative_to(REPO_ROOT)} ({module})")
        self.assertEqual(sorted(unskipped), [], "optional imports with no skip guard")

    def test_the_tests_really_are_unittest_test_cases(self) -> None:
        # "Tests use `unittest.TestCase`". pytest is the runner, not the
        # framework, and that is what makes the suite runnable by
        # `python3 -m unittest` as well.
        wrong = []
        for path in test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not node.name.endswith(("Test", "Tests", "TestCase")):
                    continue
                bases = {ast.unparse(base) for base in node.bases}
                if not bases & {"unittest.TestCase", "unittest.IsolatedAsyncioTestCase"}:
                    wrong.append(f"{path.relative_to(REPO_ROOT)}:{node.name} -> {sorted(bases)}")
        self.assertEqual(sorted(wrong), [], "test classes that are not unittest.TestCase subclasses")


class EndToEndParagraphTests(unittest.TestCase):
    """The paragraph that tells a reader what the second tier is for."""

    def test_the_end_to_end_tier_runs_the_cli_as_a_subprocess(self) -> None:
        self.assertIn(
            "`tests/e2e/` is the exception and mocks nothing: it runs "
            "`python3 -m ci_tools.cli <command>` as a real subprocess",
            guide_prose(),
        )
        sources = [path for path in test_sources() if path.parent.name == "e2e"]
        self.assertTrue(sources, "no end-to-end sources found")
        body = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertIn('"-m", "ci_tools.cli"', body)
        self.assertIn("subprocess.run", body)

    def test_the_end_to_end_tier_really_mocks_nothing(self) -> None:
        # The distinguishing claim of the tier. A mock imported here would make
        # it a second unit suite that costs a subprocess.
        for path in (path for path in test_sources() if path.parent.name == "e2e"):
            with self.subTest(path=path.name):
                self.assertNotIn("unittest.mock", path.read_text(encoding="utf-8"))

    def test_the_tier_documents_its_own_limits(self) -> None:
        # The guide hands the reader on to this file for what the tier cannot
        # reach; an empty or missing page would end the trail.
        readme = REPO_ROOT / "tests" / "e2e" / "README.md"
        self.assertTrue(readme.is_file())
        self.assertGreater(len(readme.read_text(encoding="utf-8").split()), 50)


class CrossReferenceTests(unittest.TestCase):
    def test_no_bullet_points_at_a_section_this_document_does_not_have(self) -> None:
        # `(see "Safety Model" above)` sent the reader scrolling for a heading
        # that is in docs/safety-model.md, not here. A quoted see-reference
        # either names a heading in this file or is a link.
        headings = {
            flat(line.lstrip("#")).lower()
            for line in guide_lines()
            if line.startswith("#")
        }
        dangling = [
            match.group(1)
            for match in re.finditer(r'see "([^"]+)" (?:above|below)', guide_prose())
            if flat(match.group(1)).lower() not in headings
        ]
        self.assertEqual(dangling, [], "see-references naming no heading in this document")

    def test_the_guide_is_reachable_from_the_pages_that_send_readers_to_it(self) -> None:
        # The guide is an entry point only if the entry points still point at
        # it; CONTRIBUTING.md uses the `#running-tests` anchor specifically.
        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("docs/code-reading-guide.md#running-tests", contributing)
        self.assertIn("Running Tests", "\n".join(guide_lines()))


if __name__ == "__main__":
    unittest.main()
