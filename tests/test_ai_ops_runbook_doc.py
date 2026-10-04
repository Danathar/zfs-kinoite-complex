"""
Script: tests/test_ai_ops_runbook_doc.py
What: Joins docs/ai-ops-runbook.md to the workflows, hook, settings and CI tool output it tells an operator to look for.
Doing: Reads the page's per-workflow headings, quoted names and backticked paths, and checks each against the workflow files, `.claude/` files and `ci_tools/` sources the page describes, in both directions.
Why: The page is what a maintainer opens when a run is red or an agent did something odd, and every line of it names a job, a log message, a label or a trigger that lives somewhere else and can be renamed without anyone opening the page.
Goal: Make a workflow the page does not cover, a renamed job or step, a reworded log line, or a changed trigger the page describes fail here instead of leaving the operator with a runbook for a pipeline that no longer exists.

What is pinned, and what is not:

  * Every file in `.github/workflows/` has exactly one `### \\`name.yml\\`` entry, and no entry names a
    file that is not there. The quoted workflow name beside it is the file's `name:`.
  * Every double-quoted name on the page is a workflow, job or step name from some workflow, a heading
    in some tracked document, or one of a few quoted phrases listed in OTHER_QUOTED. Inside the
    workflow entries it must come from the workflow that entry is about, or from the page's own
    cross-references. This is the "quoted job/step names exist" check.
  * Every backticked path exists, and each `[file](link), "Heading"` cross-reference names a heading
    that exists in the linked page.
  * The behaviours the page asserts, one test each: the triggers and defaults it states for
    build.yml, build-branch.yml, build-pr.yml, test.yml, prune-registry.yml, ai-fix.yml and
    labeler.yml; the log lines it tells the reader to search for; the sticky issue titles, label and
    closing rule; and the hook's refusal and exit code.

What is not pinned: the live GitHub facts (who authored a pull request, what the Hive labels say).
The page gives the `gh` command for each rather than stating them as settled, and a test cannot
read them. Link and anchor resolution in general belongs to tests/test_docs_consistency.py; this
file repeats it for this one page because the assignment for the page asks for it by name.

No PyYAML, for the reason tests/test_docs_consistency.py gives: CI installs pytest, pytest-cov and
ruff by name, so an optional parser would skip in exactly the place these assertions must run.
Workflow facts are read with line-oriented helpers, and `ParserTests` carries their fixtures.

Each assertion was mutated in a scratch copy and failed as it should. The list is in the pull
request that added this file.
"""

from __future__ import annotations

import json
import re
import unittest

from tests.test_docs_consistency import (
    LINK_RE,
    REPO_ROOT,
    WORKFLOW_DIR,
    heading_slugs,
    tracked_markdown,
)

DOC = REPO_ROOT / "docs" / "ai-ops-runbook.md"
UPSTREAM_RESPONSE = REPO_ROOT / "docs" / "upstream-change-response.md"
DOC_GUIDE = REPO_ROOT / "docs" / "documentation-guide.md"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
HOOK = REPO_ROOT / ".claude" / "hooks" / "gate-git-diff.sh"
RULESET = REPO_ROOT / ".github" / "rulesets" / "main.json"
LABELER_CONFIG = REPO_ROOT / ".github" / "labeler.yml"
CLASSIFIER = REPO_ROOT / "ci_tools" / "classify_akmods_failure.py"
STABLE_SIGNAL = REPO_ROOT / "ci_tools" / "check_stable_signal.py"
PRUNE = REPO_ROOT / "ci_tools" / "prune_registry.py"

# Quoted phrases on the page that are neither a workflow name nor a heading. Each is something the
# page quotes from outside this repository's files, or a word in quotation marks rather than a name.
OTHER_QUOTED = {
    # The description GitHub holds for each Hive approval label (`gh label list`). Not in the tree.
    "Approved by a Hive merger/owner for auto-merge on green CI",
    # A word set in quotes as a verb, not a name.
    "retry",
}

# Backticked tokens that look like paths and are not files in this tree.
NOT_FILES = {
    "app/danathar-atomic-hive",  # the GitHub App that opens agent pull requests
    "app/github-actions",  # the author of sticky issues
    "hive/likely-done",  # labels held by GitHub, not by a file here
    "hive/covered-by-pr",
    "danathar-atomic-hive[bot]",
    "quality/",  # branch prefixes
    "arch/",
    "architect/",
    "scanner/",
    "guide/",
    "sec/",
    "ci/",
    "ai-fix/",
}

# What each workflow entry quotes as a job or step name, and the page must keep quoting.
EXPECTED_QUOTED = {
    "build.yml": ["Build And Promote Main Image", "Evaluate Stable Signal Gate"],
    "build-pr.yml": ["Validate Pull Request Image Build", "Build PR Image (No Push)"],
    "build-branch.yml": [
        "Build Branch Image",
        "Compute Branch Tag",
        "Verify Shared ZFS Akmods Cache",
    ],
    "test.yml": ["Run Python Tests", "Python Unit Tests", "Lint with ruff"],
    "labeler.yml": ["Labeler", "Apply area labels"],
    "nightly-compliance.yml": ["Nightly compliance"],
    "prune-registry.yml": ["Prune registry"],
    "akmods-failure-triage.yml": [
        "Akmods Failure Triage",
        "Manage sticky akmods failure issue",
    ],
    "ai-fix.yml": ["AI fix", "Check credentials and target", "Run the agent"],
}

ENTRY_HEADING_RE = re.compile(r'^### `([\w.-]+\.yml)` — "([^"]+)"$')
QUOTED_RE = re.compile(r'"([^"\n]+)"')
BACKTICKED_RE = re.compile(r"`([^`\n]+)`")
XREF_RE = re.compile(r'\]\(([^)\s]+\.md)\)[^"\[\n#`]{0,40}"([^"]+)"')


# --- parsing helpers ---------------------------------------------------------------------------


def collapse(text: str) -> str:
    """Join a hard-wrapped paragraph, so a quote split across a line break is one string."""

    return re.sub(r"\s+", " ", text)


def yaml_names(text: str) -> set[str]:
    """Every `name:` value in a workflow: the workflow's, each job's, each step's."""

    names = set()
    for line in text.splitlines():
        match = re.match(r"^\s*(?:- )?name:\s*(.+?)\s*$", line)
        if match and not line.lstrip().startswith("#"):
            names.add(match.group(1).strip("'\""))
    return names


def top_block(text: str, key: str) -> list[str]:
    """Lines indented under a column-0 `key:`, comments and blanks removed."""

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            block = []
            for follow in lines[i + 1 :]:
                if follow.strip() and not follow.startswith(" "):
                    break
                if follow.strip() and not follow.strip().startswith("#"):
                    block.append(follow)
            return block
    return []


def indented_block(lines: list[str], key: str, strip: bool = True) -> list[str]:
    """Lines more indented than the first line that is exactly `key:` in `lines`.

    Stripped by default. Pass strip=False to keep the indentation, which a nested lookup needs.
    """

    for i, line in enumerate(lines):
        if line.strip() == f"{key}:":
            base = len(line) - len(line.lstrip())
            block = []
            for follow in lines[i + 1 :]:
                if len(follow) - len(follow.lstrip()) <= base:
                    break
                block.append(follow.strip() if strip else follow)
            return block
    return []


def step_block(text: str, step_name: str) -> str:
    """The text of the step whose `name:` is step_name, up to the next step."""

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*- name:\s*{re.escape(step_name)}\s*$", line):
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*- (name|uses):", lines[j]):
                    end = j
                    break
            return "\n".join(lines[i:end])
    raise AssertionError(f"no step named {step_name!r}")


def workflow_text(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


def workflow_files() -> list[str]:
    return sorted(
        path.name
        for path in list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))
    )


def page_sections(text: str) -> dict[str, str]:
    """{workflow file: the entry text} for each `### \\`x.yml\\` — "Name"` heading."""

    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        match = ENTRY_HEADING_RE.match(line)
        if match:
            current = match.group(1)
            sections[current] = ""
            continue
        if line.startswith("#"):
            current = None
        if current:
            sections[current] += line + "\n"
    return sections


def section_of(text: str, heading: str) -> str:
    """The text under a `## heading` line, up to the next `## `."""

    lines = text.splitlines()
    start = lines.index(f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end])


def all_headings() -> set[str]:
    headings = set()
    for doc in tracked_markdown():
        for line in doc.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                headings.add(line.lstrip("#").strip())
    return headings


def heading_matches(quoted: str, headings: set[str]) -> bool:
    """A quote may be a heading or its leading words ("Labels carry authority — ...")."""

    return any(h == quoted or h.startswith(quoted) for h in headings)


class RunbookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DOC.read_text(encoding="utf-8")
        # Prose only: a fenced command block holds quotes that are shell syntax, not names.
        cls.prose = re.sub(r"```.*?```", "", cls.text, flags=re.DOTALL)
        cls.flat = collapse(cls.prose)
        cls.sections = page_sections(cls.text)
        cls.headings = all_headings()


# --- the page covers the workflows, and only them -----------------------------------------------


class WorkflowCoverage(RunbookTest):
    def test_the_parse_finds_the_entries(self) -> None:
        # Guard the guard: set comparisons below pass vacuously on two empty sets.
        self.assertGreaterEqual(len(self.sections), 5)
        self.assertGreaterEqual(len(workflow_files()), 5)

    def test_every_workflow_file_has_an_entry_and_no_entry_names_a_missing_file(self) -> None:
        self.assertEqual(sorted(self.sections), workflow_files())

    def test_every_workflow_file_name_appears_on_the_page(self) -> None:
        # The entries are headings; this also demands the file name in running prose or a heading
        # as written, so a heading reworded away from the file name cannot pass the test above.
        missing = [name for name in workflow_files() if f"`{name}`" not in self.text]
        self.assertEqual(missing, [])

    def test_each_entry_quotes_the_workflows_own_name(self) -> None:
        for line in self.text.splitlines():
            match = ENTRY_HEADING_RE.match(line)
            if not match:
                continue
            file, quoted = match.groups()
            with self.subTest(workflow=file):
                top_name = re.search(r"^name:\s*(.+?)\s*$", workflow_text(file), re.MULTILINE)
                self.assertIsNotNone(top_name)
                self.assertEqual(top_name.group(1).strip("'\""), quoted)

    def test_the_page_says_every_workflow_has_an_entry(self) -> None:
        self.assertIn("Every file in `.github/workflows/` has an entry here.", self.text)


# --- quoted names exist --------------------------------------------------------------------------


class QuotedNames(RunbookTest):
    def test_the_expected_names_are_still_quoted_in_their_entry(self) -> None:
        for file, quoted in EXPECTED_QUOTED.items():
            section = collapse(self.sections.get(file, "")) + " " + self.flat_heading(file)
            for name in quoted:
                with self.subTest(workflow=file, name=name):
                    self.assertIn(f'"{name}"', section)

    def flat_heading(self, file: str) -> str:
        for line in self.text.splitlines():
            if line.startswith("### ") and f"`{file}`" in line:
                return line
        return ""

    def test_every_expected_name_exists_in_its_workflow(self) -> None:
        for file, quoted in EXPECTED_QUOTED.items():
            names = yaml_names(workflow_text(file))
            for name in quoted:
                with self.subTest(workflow=file, name=name):
                    self.assertIn(name, names)

    def test_every_quoted_string_in_an_entry_is_a_workflow_name_a_heading_or_listed(self) -> None:
        all_names: set[str] = set()
        for file in workflow_files():
            all_names |= yaml_names(workflow_text(file))
        unknown = []
        for quoted in QUOTED_RE.findall(self.flat):
            if quoted in all_names or quoted in OTHER_QUOTED:
                continue
            if heading_matches(quoted, self.headings):
                continue
            unknown.append(quoted)
        self.assertEqual(unknown, [], "quoted on the page, found nowhere it could come from")

    def test_names_quoted_in_an_entry_belong_to_that_workflow_or_the_cross_reference_set(
        self,
    ) -> None:
        # Stricter than the global check: inside the entry for build-pr.yml, "Build Branch Image"
        # would be a real name from another workflow and still be wrong. The names an entry may
        # quote from a *different* workflow are the ones its text explains (the required check
        # `Python Unit Tests`, which the ai-fix entry is about).
        cross = {"ai-fix.yml": {"Python Unit Tests"}, "build-branch.yml": set()}
        for file, section in self.sections.items():
            names = yaml_names(workflow_text(file)) | cross.get(file, set()) | OTHER_QUOTED
            for quoted in QUOTED_RE.findall(collapse(section)):
                if heading_matches(quoted, self.headings) and quoted not in names:
                    # A cross-reference to a heading, such as "Reading a red build".
                    continue
                with self.subTest(workflow=file, quoted=quoted):
                    self.assertIn(quoted, names)

    def test_the_required_check_the_page_names_is_the_one_the_ruleset_requires(self) -> None:
        required = [
            rule["parameters"]["required_status_checks"]
            for rule in json.loads(RULESET.read_text(encoding="utf-8"))["rules"]
            if rule["type"] == "required_status_checks"
        ]
        contexts = [check["context"] for group in required for check in group]
        self.assertEqual(contexts, ["Python Unit Tests"])
        self.assertIn('The\n  required job is "Python Unit Tests"', self.text)


# --- links, anchors, and quoted headings ---------------------------------------------------------


class Links(RunbookTest):
    def test_every_relative_link_resolves(self) -> None:
        links = LINK_RE.findall(self.text)
        self.assertGreater(len(links), 20)
        broken = []
        for target in links:
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if not (DOC.parent / target.partition("#")[0]).exists():
                broken.append(target)
        self.assertEqual(broken, [])

    def test_every_anchor_names_a_real_heading(self) -> None:
        broken = []
        for target in LINK_RE.findall(self.text):
            path, _, anchor = target.partition("#")
            if not anchor or target.startswith(("http://", "https://")):
                continue
            destination = DOC if not path else DOC.parent / path
            if anchor not in heading_slugs(destination):
                broken.append(target)
        self.assertEqual(broken, [])

    def test_a_link_followed_by_a_quoted_heading_names_a_heading_in_that_page(self) -> None:
        pairs = XREF_RE.findall(self.flat)
        self.assertGreaterEqual(len(pairs), 8)
        bad = []
        for link, quoted in pairs:
            page = DOC.parent / link
            headings = {
                line.lstrip("#").strip()
                for line in page.read_text(encoding="utf-8").splitlines()
                if line.startswith("#")
            }
            if not heading_matches(quoted, headings):
                bad.append(f"{link}: {quoted}")
        self.assertEqual(bad, [])

    def test_every_backticked_path_exists(self) -> None:
        checked = 0
        missing = []
        for token in BACKTICKED_RE.findall(self.text):
            if token in NOT_FILES or any(c in token for c in "*<> ="):
                continue
            if not re.fullmatch(r"[\w./-]+", token) or re.fullmatch(r"\.\w+", token):
                continue
            if "/" not in token and not re.search(r"\.(md|yml|py|json|sh|pub)$", token):
                continue
            checked += 1
            candidates = [REPO_ROOT / token, REPO_ROOT / "docs" / token, WORKFLOW_DIR / token]
            if not any(path.exists() for path in candidates):
                missing.append(token)
        self.assertGreater(checked, 25)
        self.assertEqual(missing, [])


# --- the split with the other pages --------------------------------------------------------------


class Placement(RunbookTest):
    def test_the_page_defers_to_the_upstream_response_page_and_it_points_back(self) -> None:
        self.assertIn("(./upstream-change-response.md)", self.text)
        self.assertIn("item 6", self.text)
        guide = DOC_GUIDE.read_text(encoding="utf-8")
        item_6 = re.search(r"^6\. (.+(?:\n   .+)*)", guide, re.MULTILINE)
        self.assertIsNotNone(item_6)
        self.assertIn("upstream-change-response.md", item_6.group(1))
        self.assertIn("runbook", item_6.group(1))
        self.assertIn("(./ai-ops-runbook.md)", UPSTREAM_RESPONSE.read_text(encoding="utf-8"))

    def test_the_documentation_guide_maps_the_page(self) -> None:
        guide = DOC_GUIDE.read_text(encoding="utf-8")
        self.assertRegex(guide, r"(?m)^  ai-ops-runbook\.md\s+<-")
        self.assertIn("(./ai-ops-runbook.md)", guide)


# --- behaviours the entries assert ---------------------------------------------------------------


class BuildEntry(RunbookTest):
    def test_triggers_and_the_promote_default(self) -> None:
        on = top_block(workflow_text("build.yml"), "on")
        keys = {line.strip().rstrip(":") for line in on if re.match(r"^  \S", line)}
        self.assertEqual(keys, {"schedule", "push", "workflow_dispatch"})
        self.assertIn("- main", indented_block(on, "branches"))
        inputs = indented_block(on, "inputs", strip=False)
        promote = indented_block(inputs, "promote_to_stable")
        self.assertIn("default: true", promote)
        self.assertIn("`promote_to_stable` defaults to\n  `true`", self.text)

    def test_a_scheduled_run_can_skip_the_build_jobs_on_the_gate(self) -> None:
        text = workflow_text("build.yml")
        self.assertIn(
            "if: github.event_name != 'schedule' || needs.preflight.outputs.should_build == 'true'",
            text,
        )
        self.assertIn("name: Evaluate Stable Signal Gate", text)

    def test_the_gate_prints_the_reason_the_page_tells_the_reader_to_search_for(self) -> None:
        source = STABLE_SIGNAL.read_text(encoding="utf-8")
        self.assertIn('reason="stable-signal-unchanged"', source)
        self.assertIn("f\"reason={decision.reason} \"", source)
        self.assertIn("`reason=stable-signal-unchanged`", self.text)


class PullRequestAndBranchEntries(RunbookTest):
    def test_markdown_only_changes_start_neither_build_workflow_nor_production(self) -> None:
        for file in ("build.yml", "build-pr.yml", "build-branch.yml"):
            with self.subTest(workflow=file):
                ignored = indented_block(top_block(workflow_text(file), "on"), "paths-ignore")
                self.assertIn("- docs/**", ignored)
                self.assertIn('- "**/*.md"', ignored)

    def test_the_pull_request_workflow_has_no_push_or_sign_step(self) -> None:
        text = workflow_text("build-pr.yml")
        names = yaml_names(text)
        self.assertIn("Build PR Image (No Push)", names)
        # The job name says "No Push" and the tool install names cosign; no other step may.
        others = names - {"Build PR Image (No Push)", "Install skopeo and cosign"}
        pushy = sorted(n for n in others if re.search(r"push|sign", n, re.IGNORECASE))
        self.assertEqual(pushy, [])

    def test_branch_workflow_excludes_main_and_agent_branches(self) -> None:
        on = top_block(workflow_text("build-branch.yml"), "on")
        ignored = indented_block(on, "branches-ignore")
        self.assertEqual(ignored, ["- main", "- 'ai-fix/**'"])

    def test_branch_image_publish_is_skipped_for_a_bot(self) -> None:
        step = step_block(workflow_text("build-branch.yml"), "Push unsigned branch test image")
        self.assertIn("if: steps.registry.outputs.actor_is_bot != 'true'", step)

    def test_the_old_branch_tags_are_in_the_prune_rule(self) -> None:
        header = workflow_text("prune-registry.yml").split("on:")[0]
        self.assertIn("`br-*`", header)
        self.assertIn("`br-*` tags are removed by `prune-registry.yml`", collapse(self.text))


class TestEntry(RunbookTest):
    def test_the_required_job_runs_on_every_pull_request_with_no_filter(self) -> None:
        text = workflow_text("test.yml")
        on = top_block(text, "on")
        self.assertIn("  pull_request:", on)
        self.assertNotIn("paths", "\n".join(on))
        self.assertNotIn("paths-ignore", "\n".join(on))
        self.assertIn("name: Python Unit Tests", text)

    def test_the_build_workflow_never_runs_the_unit_suite(self) -> None:
        # The page says a red test.yml does not stop a publish "because build.yml never runs it".
        build = workflow_text("build.yml")
        for command in ("pytest", "run_tests", "ruff check"):
            self.assertNotIn(command, build)

    def test_the_lint_step_the_page_points_at_carries_the_ruff_command(self) -> None:
        step = step_block(workflow_text("test.yml"), "Lint with ruff")
        self.assertIn("ruff check ci_tools/ shared/ tests/ files/ containerfiles/", step)
        self.assertTrue((REPO_ROOT / "tests" / "run_tests.py").exists())


class LabelerEntry(RunbookTest):
    def test_every_label_the_config_applies_is_in_the_descriptive_namespace(self) -> None:
        keys = re.findall(r"^'?([^\s'#][^'\n:]*)'?:\s*$", LABELER_CONFIG.read_text(), re.MULTILINE)
        self.assertGreaterEqual(len(keys), 5)
        self.assertEqual([k for k in keys if not k.startswith("area/")], [])

    def test_the_workflow_only_reads_the_changed_file_list(self) -> None:
        text = workflow_text("labeler.yml")
        self.assertIn("pull_request_target:", text)
        self.assertNotIn("actions/checkout", text)
        self.assertIn("name: Apply area labels", text)


class NightlyEntry(RunbookTest):
    def test_the_two_jobs_are_named_as_the_page_names_them(self) -> None:
        names = yaml_names(workflow_text("nightly-compliance.yml"))
        self.assertTrue({"suite", "published-image"} <= names)
        self.assertIn("- `published-image` red:", self.text)
        self.assertIn("- `suite` red with no recent commit:", self.text)

    def test_the_published_image_job_verifies_against_the_committed_key(self) -> None:
        step = step_block(
            workflow_text("nightly-compliance.yml"), "Verify the published :latest signature"
        )
        self.assertIn("--key cosign.pub", step)
        self.assertTrue((REPO_ROOT / "cosign.pub").exists())


class PruneEntry(RunbookTest):
    def test_only_a_manual_dispatch_with_the_delete_input_deletes(self) -> None:
        text = workflow_text("prune-registry.yml")
        on = top_block(text, "on")
        self.assertIn("schedule:", "\n".join(on))
        delete = indented_block(indented_block(on, "inputs", strip=False), "delete")
        self.assertIn("default: false", delete)
        self.assertIn(
            "PRUNE_DELETE: ${{ github.event_name == 'workflow_dispatch' && inputs.delete"
            " && 'true' || 'false' }}",
            text,
        )

    def test_the_log_lines_the_page_quotes_are_what_the_tool_prints(self) -> None:
        source = PRUNE.read_text(encoding="utf-8")
        self.assertIn('"mode: " + ("delete" if delete else "dry run (nothing deleted)")', source)
        self.assertIn('print(f"  FAILED to delete {vid}: {exc}", file=sys.stderr)', source)
        self.assertIn("`mode: dry run (nothing deleted)` or\n  `mode: delete`", self.text)
        self.assertIn("A line starting `FAILED to delete`", collapse(self.text))


class TriageEntry(RunbookTest):
    def test_the_titles_the_page_quotes_are_the_ones_the_classifier_writes(self) -> None:
        source = CLASSIFIER.read_text(encoding="utf-8")
        for title in ("Upstream ZFS/kernel incompatibility:", "Unclassified akmods build failure:"):
            with self.subTest(title=title):
                self.assertIn(f'f"{title} {{kernel_release}} + akmods@{{safe_ref}}"', source)
                self.assertIn(f"`{title}`", self.flat)

    def test_the_sticky_label_and_the_open_only_lookup(self) -> None:
        text = workflow_text("akmods-failure-triage.yml")
        self.assertIn("const stickyLabel = 'akmods-failure';", text)
        self.assertIn("`akmods-failure`", self.text)
        # The page says a hand-closed issue is not reopened: the lookup only lists open issues.
        lookup = text.split("Look for an existing open issue")[1].split("if (match)")[0]
        self.assertIn("state: 'open'", lookup)

    def test_a_skipped_scheduled_run_does_not_close_the_issue(self) -> None:
        step = step_block(
            workflow_text("akmods-failure-triage.yml"),
            "Close stale sticky issues on successful run",
        )
        self.assertIn("steps.download.outputs.build_ran == 'true'", step)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", step)

    def test_the_workflow_runs_after_build_and_manages_the_issue_job_the_page_names(self) -> None:
        text = workflow_text("akmods-failure-triage.yml")
        self.assertIn('workflows: ["Build And Promote Main Image"]', text)
        self.assertIn("name: Manage sticky akmods failure issue", text)


class AiFixEntry(RunbookTest):
    def test_the_two_triggers_and_what_they_listen_for(self) -> None:
        text = workflow_text("ai-fix.yml")
        on = top_block(text, "on")
        self.assertEqual(
            {line.strip().rstrip(":") for line in on if re.match(r"^  \S", line)},
            {"issues", "issue_comment"},
        )
        self.assertIn("label_trigger: ai-fix-requested", text)
        self.assertIn("trigger_phrase: '@claude'", text)
        self.assertIn("branch_prefix: 'ai-fix/'", text)

    def test_a_bot_is_refused_and_a_skip_says_so_in_the_summary(self) -> None:
        text = workflow_text("ai-fix.yml")
        self.assertIn("allowed_bots: ''", text)
        self.assertIn('if [ "${SENDER_TYPE}" = "Bot" ]; then', text)
        self.assertEqual(text.count('echo "Skipped:'), 3)
        # Bot, no credentials, fork: the three reasons the page lists, in the order it lists them.
        reasons = re.findall(r'echo "Skipped: (.+)$', text, re.MULTILINE)
        self.assertEqual(len(reasons), 3)
        self.assertIn("triggered by", reasons[0])
        self.assertIn("no agent credentials", reasons[1])
        self.assertIn("comes from a fork", reasons[2])
        self.assertIn("`Skipped:`", self.text)

    def test_the_skip_succeeds_rather_than_failing(self) -> None:
        # "A green run that did nothing is normal": the `fix` job is skipped, not failed, by this gate.
        text = workflow_text("ai-fix.yml")
        self.assertIn("if: needs.preflight.outputs.run == 'yes'", text)
        self.assertIn('echo "run=${run}" >> "${GITHUB_OUTPUT}"', text)

    def test_the_agent_pushes_a_branch_it_can_leave_behind_and_cannot_push_images(self) -> None:
        text = workflow_text("ai-fix.yml")
        fix = text.split("  fix:")[1]
        self.assertIn("contents: write", fix)
        self.assertNotIn("packages:", fix)
        self.assertNotIn("SIGNING_SECRET", text.split("steps:", 2)[2])


class HookEntry(RunbookTest):
    def test_a_refusal_prints_to_stderr_and_exits_two(self) -> None:
        text = HOOK.read_text(encoding="utf-8")
        refuse = re.search(r"^refuse\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(refuse)
        self.assertIn("printf '%s\\n' \"$1\" >&2", refuse.group(1))
        self.assertIn("exit 2", refuse.group(1))

    def test_every_refusal_message_starts_with_blocked(self) -> None:
        text = HOOK.read_text(encoding="utf-8")
        messages = re.findall(r"^[A-Z_]+_MSG='(.{0,8})", text, re.MULTILINE)
        self.assertGreaterEqual(len(messages), 10)
        self.assertEqual({m for m in messages}, {"blocked:"})
        self.assertIn("starting `blocked:`", self.flat)

    def test_the_hook_is_registered_on_bash_and_ruff_runs_after_python_edits(self) -> None:
        hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
        pre = hooks["PreToolUse"]
        self.assertEqual([entry["matcher"] for entry in pre], ["Bash"])
        self.assertIn("gate-git-diff.sh", pre[0]["hooks"][0]["command"])
        post = hooks["PostToolUse"][0]
        self.assertEqual(post["matcher"], "Edit|Write|MultiEdit")
        command = post["hooks"][0]["command"]
        self.assertIn("*.py)", command)
        self.assertIn("ruff check", command)
        self.assertIn("exit 2", command)


# --- the parser's own fixtures -------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    YAML = (
        "name: Example\n"
        "# name: commented\n"
        "on:\n"
        "  push:\n"
        "    branches-ignore:\n"
        "      - main\n"
        "      - 'x/**'\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      go:\n"
        "        default: true\n"
        "jobs:\n"
        "  a:\n"
        "    name: Job A\n"
        "    steps:\n"
        "      - name: Step One\n"
        "        run: echo 1\n"
        "      - name: Step Two\n"
        "        if: x\n"
    )

    def test_yaml_names_skips_comments_and_keeps_every_level(self) -> None:
        self.assertEqual(yaml_names(self.YAML), {"Example", "Job A", "Step One", "Step Two"})

    def test_top_block_stops_at_the_next_column_zero_key(self) -> None:
        block = top_block(self.YAML, "on")
        self.assertEqual(block[0], "  push:")
        self.assertNotIn("jobs:", "\n".join(block))

    def test_indented_block_returns_only_deeper_lines(self) -> None:
        on = top_block(self.YAML, "on")
        self.assertEqual(indented_block(on, "branches-ignore"), ["- main", "- 'x/**'"])
        self.assertEqual(
            indented_block(indented_block(on, "inputs", strip=False), "go"), ["default: true"]
        )

    def test_step_block_ends_at_the_next_step(self) -> None:
        self.assertEqual(
            step_block(self.YAML, "Step One"), "      - name: Step One\n        run: echo 1"
        )
        with self.assertRaises(AssertionError):
            step_block(self.YAML, "Absent")

    def test_collapse_joins_a_wrapped_quote(self) -> None:
        self.assertEqual(QUOTED_RE.findall(collapse('see "a\n   b" now')), ["a b"])

    def test_entry_heading_regex_and_sections(self) -> None:
        page = '## Workflow runs\n\n### `a.yml` — "A"\n\nbody "q"\n\n### Other\n\nnot a\n'
        self.assertEqual(page_sections(page), {"a.yml": '\nbody "q"\n\n'})

    def test_heading_prefix_match(self) -> None:
        headings = {"Labels carry authority — automation must not apply them"}
        self.assertTrue(heading_matches("Labels carry authority", headings))
        self.assertFalse(heading_matches("carry authority", headings))


if __name__ == "__main__":
    unittest.main()
