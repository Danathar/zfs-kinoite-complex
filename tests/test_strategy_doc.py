"""
Script: tests/test_strategy_doc.py
What: Holds docs/strategy.md to the files it links, the sentences it quotes, the commands it tells a reader to run, and the statement that no scheduled report workflow exists.
Doing: Joins every quoted sentence to the page that says it, runs the page's local `grep` and `sed` commands against the real tree, reuses the `gh` tokenizer from tests/test_metrics_doc.py to check each documented `gh` call names this repository, is a read, and filters only on fields it requested, and compares the "scheduled workflows that do exist" sentence to the `schedule:` triggers in .github/workflows.
Why: The page is a pointer and a set of commands, so nearly everything in it is either a sentence copied by hand from another page or a command a maintainer will run to learn how far along the project is. A reworded quotation or a `gh` call aimed at the wrong repository does not fail anything on its own; it makes the page quietly say something the source no longer does.
Goal: Make a change that moves the goal statement, the open-finding heading, the proposals' headings, or the set of scheduled workflows fail here, instead of leaving the strategy page describing a repository that no longer exists.

What this file does not restate: link and anchor resolution and the documentation
map (tests/test_docs_consistency.py), the `gh` tokenizer itself
(tests/test_metrics_doc.py), the policy file's own enforcement
(tests/test_workflow_permissions_policy.py).

The page promises "no stored numbers", which is mechanical, so it is checked: the
prose outside code spans and link targets carries no digit at all.

Standard library only, for the reason tests/test_docs_consistency.py gives.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest

from tests.test_docs_consistency import REPO_ROOT, heading_slugs, workflow_paths
from tests.test_metrics_doc import (
    fenced_blocks,
    fields_read,
    flag_value,
    invocations,
    json_filter_pairs,
)

DOC_PATH = REPO_ROOT / "docs" / "strategy.md"
DOCS = REPO_ROOT / "docs"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
POLICY_PATH = REPO_ROOT / ".github" / "policies" / "workflow-permissions.json"

SLUG = "Danathar/zfs-kinoite-complex"

# The workflows the page names as the ones that run on a schedule. If another
# one gains a `schedule:` trigger, the page's sentence is wrong and the person
# who added it should read it.
SCHEDULED = ("build.yml", "nightly-compliance.yml", "prune-registry.yml")

# `gh` subcommands the page may use. Every command on it is a look, not a change.
READERS = {("pr", "list"), ("issue", "list"), ("label", "list")}

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
QUOTE = re.compile(r'"([^"\n]{12,}?)"')


def doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def squash(text: str) -> str:
    """Collapse the whitespace that Markdown wrapping puts inside a sentence."""

    return " ".join(text.split())


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def shell_lines() -> list[str]:
    """The page's bash blocks, with `\\` continuations joined, one command per entry."""

    commands: list[str] = []
    for block in fenced_blocks(doc(), "bash"):
        commands.extend(
            line.strip() for line in block.replace("\\\n", " ").splitlines() if line.strip()
        )
    return commands


def run_local(prefix: str) -> subprocess.CompletedProcess[str]:
    """Run the page's one command that starts with `prefix`, from the repository root."""

    matches = [line for line in shell_lines() if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one `{prefix}` command, found {matches}"
    return subprocess.run(
        matches[0],
        shell=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class LinkTargetTests(unittest.TestCase):
    """The page points at its sources; it must keep pointing at all of them."""

    def test_the_page_links_every_source_of_the_goal(self) -> None:
        targets = {match.group(1).partition("#")[0] for match in LINK.finditer(doc())}
        for source in (
            "../README.md",
            "./safety-model.md",
            "./production-boundary-proposal.md",
            "./runtime-validation-proposal.md",
            "./maintenance-watchlist.md",
            "./metrics.md",
            "./quality.md",
            "./risk-tiers.md",
            "../.github/policies/workflow-permissions.json",
        ):
            with self.subTest(source=source):
                self.assertIn(source, targets)

    def test_the_open_finding_anchor_is_the_watchlists_open_heading(self) -> None:
        # The link names an anchor that `grep '^### Open'` is meant to find. If the
        # heading is renamed so the grep stops matching, the anchor and the command
        # disagree, and the generic anchor test only sees the anchor.
        anchors = [
            match.group(1).partition("#")[2]
            for match in LINK.finditer(doc())
            if match.group(1).startswith("./maintenance-watchlist.md#")
        ]
        self.assertEqual(len(anchors), 1)
        watchlist = DOCS / "maintenance-watchlist.md"
        self.assertIn(anchors[0], heading_slugs(watchlist))
        opens = [
            line for line in watchlist.read_text(encoding="utf-8").splitlines()
            if re.match(r"### Open", line)
        ]
        self.assertTrue(opens, "the watchlist has no `### Open` heading, so the page's example is stale")
        slug = re.sub(r"[^\w\s-]", "", opens[0].lstrip("#").strip()).strip().lower().replace(" ", "-")
        self.assertEqual(anchors[0], slug)


class QuotationTests(unittest.TestCase):
    """Every sentence the page puts in quotation marks is a hand copy of another page."""

    SOURCES = (
        "README.md",
        "docs/safety-model.md",
        "docs/production-boundary-proposal.md",
        "docs/runtime-validation-proposal.md",
        "docs/metrics.md",
    )

    def test_every_quotation_appears_in_a_source_page(self) -> None:
        prose = "\n".join(
            line for line in doc().splitlines() if not line.startswith(("    ", "```"))
        )
        # Drop fenced blocks, whose quotes are shell quoting, not quotation.
        for block in fenced_blocks(doc(), "bash"):
            prose = prose.replace(block, "")
        quoted = QUOTE.findall(squash(prose))
        self.assertGreaterEqual(len(quoted), 3, f"the scan found too few quotations: {quoted}")
        haystack = squash(" ".join(read(source) for source in self.SOURCES))
        for sentence in quoted:
            with self.subTest(quoted=sentence):
                self.assertIn(sentence, haystack)

    def test_paraphrases_the_page_depends_on_are_still_true_of_their_sources(self) -> None:
        facts = {
            "README.md": (
                "testing-only",
                "puts a `hold` label on every pull request an agent opens",
                "reviews agent pull requests in batches",
            ),
            "docs/safety-model.md": (
                "testing-only",
                "intends to keep the build\nand test pipeline active",
                "does not change\nthe production boundary",
            ),
            "docs/runtime-validation-proposal.md": ("does not boot a Fedora Kinoite deployment",),
            "docs/production-boundary-proposal.md": ("do not make the repository\nproduction-approved",),
        }
        for source, needles in facts.items():
            text = squash(read(source))
            for needle in needles:
                with self.subTest(source=source, needle=needle):
                    self.assertIn(squash(needle), text)

    def test_the_headings_the_page_names_exist_under_those_names(self) -> None:
        # The page says "Review checklist", "Required settings", "Current boundary" and
        # "Future automation requirements" by name, and runs `sed` from one of them.
        for path, names in {
            "production-boundary-proposal.md": ("Required settings", "Review checklist"),
            "runtime-validation-proposal.md": ("Current boundary", "Future automation requirements"),
        }.items():
            lines = (DOCS / path).read_text(encoding="utf-8").splitlines()
            for name in names:
                with self.subTest(page=path, heading=name):
                    self.assertIn(f"## {name}", lines)
                    self.assertIn(name, doc())


class LocalCommandTests(unittest.TestCase):
    """The `grep` and `sed` commands run from a clone's root; run them there."""

    @unittest.skipUnless(shutil.which("grep") and shutil.which("sed"), "grep or sed is missing")
    def test_open_finding_grep_lists_only_open_headings(self) -> None:
        result = run_local("grep -n '^### Open'")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^\d+:### Open")

    @unittest.skipUnless(shutil.which("sed"), "sed is missing")
    def test_checklist_sed_prints_the_review_checklist(self) -> None:
        result = run_local("sed -n")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.splitlines()
        self.assertEqual(out[0], "## Review checklist")
        self.assertTrue(any(line.startswith("- ") for line in out), "no checklist items printed")

    @unittest.skipUnless(shutil.which("grep"), "grep is missing")
    def test_no_workflow_creates_or_imports_a_pool(self) -> None:
        # The page tells the reader that no output means runtime validation has not
        # started. That is true today; the day it is not, the page's sentence and the
        # proposal's "Future automation requirements" both need a reader.
        result = run_local("grep -rniE")
        self.assertEqual(
            result.returncode,
            1,
            f"a workflow now mentions creating or importing a pool: {result.stdout}",
        )
        self.assertEqual(result.stderr, "", "the grep failed instead of finding nothing")

    def test_every_path_in_a_local_command_exists(self) -> None:
        for line in shell_lines():
            if line.startswith(("grep", "sed")):
                for token in re.findall(r"(?:\.github|docs)/[\w./-]+", line):
                    with self.subTest(command=line, path=token):
                        self.assertTrue((REPO_ROOT / token).exists())


class GhCommandTests(unittest.TestCase):
    """Each documented `gh` call is a read of this repository that says what it filters on."""

    def calls(self) -> list[list[str]]:
        return invocations("gh", doc())

    def test_the_parser_finds_the_commands(self) -> None:
        # A floor on the tokenizer, so the loops below cannot pass over nothing.
        self.assertGreaterEqual(len(self.calls()), 4)
        self.assertGreaterEqual(len(json_filter_pairs(doc())), 1)

    def test_the_slug_is_this_repository(self) -> None:
        self.assertIn(f"github.com/{SLUG}/", read("README.md"))

    def test_every_call_names_this_repository(self) -> None:
        for argv in self.calls():
            with self.subTest(command=" ".join(argv)):
                self.assertEqual(flag_value(argv, "-R", "--repo"), SLUG)

    def test_every_call_is_a_read(self) -> None:
        for argv in self.calls():
            words = tuple(word for word in argv[1:] if not word.startswith("-"))
            with self.subTest(command=" ".join(argv)):
                self.assertIn(words[:2], READERS)

    def test_every_filter_reads_only_requested_fields(self) -> None:
        for argv, fields, program in json_filter_pairs(doc()):
            with self.subTest(command=" ".join(argv)):
                read_fields = fields_read(program)
                self.assertTrue(read_fields, f"no field parsed out of {program!r}")
                self.assertEqual(sorted(read_fields - set(fields)), [])

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_prefix_filter_groups_by_prefix(self) -> None:
        pairs = [pair for pair in json_filter_pairs(doc()) if "headRefName" in pair[1]]
        self.assertEqual(len(pairs), 1)
        _, _, program = pairs[0]
        payload = [
            {"headRefName": "quality/a"},
            {"headRefName": "quality/b"},
            {"headRefName": "scanner/c"},
            {"headRefName": "strategist"},
        ]
        result = subprocess.run(
            ["jq", "-c", program],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            rows,
            [
                {"prefix": "quality", "merged": 2},
                {"prefix": "scanner", "merged": 1},
                {"prefix": "strategist", "merged": 1},
            ],
        )

    def test_the_stated_limit_is_the_one_the_command_uses(self) -> None:
        prefix_calls = [
            argv for argv in self.calls() if "--search" in argv and "--state" in argv
        ]
        self.assertEqual(len(prefix_calls), 1)
        self.assertEqual(flag_value(prefix_calls[0], "--state"), "merged")
        limit = flag_value(prefix_calls[0], "--limit")
        self.assertIn(f"`--limit {limit}`", doc())

    def test_the_hold_queue_uses_the_label_the_readme_names(self) -> None:
        labels = {
            flag_value(argv, "--label")
            for argv in self.calls()
            if flag_value(argv, "--state") == "open" and argv[1] == "pr"
        }
        self.assertEqual(labels, {"hold"})
        self.assertIn("`hold` label", read("README.md"))


class NoStoredNumbersTests(unittest.TestCase):
    """The page's own rule: it carries no count that a merged pull request would stale."""

    def test_prose_contains_no_digits(self) -> None:
        text = doc()
        for block in fenced_blocks(text, "bash"):
            text = text.replace(block, "")
        text = re.sub(r"`[^`\n]*`", "", text)
        text = LINK.sub(lambda match: match.group(0).split("](")[0], text)
        # The README heading is named in a link's text, and L5 is part of its name.
        text = text.replace("(ACMM L5)", "")
        # "Tier 3" is the name of a risk tier, and "1." is list numbering.
        text = text.replace("Tier 3", "")
        text = re.sub(r"^\s*\d+\. ", "", text, flags=re.MULTILINE)
        digits = [line for line in text.splitlines() if re.search(r"\d", line)]
        self.assertEqual(digits, [], "numbers in prose go stale; run a command instead")


class ScheduledReportTests(unittest.TestCase):
    """The page says no scheduled report exists, and why. Both have to stay true."""

    def test_no_strategy_report_workflow_exists(self) -> None:
        self.assertFalse((WORKFLOW_DIR / "strategy-report.yml").exists())
        self.assertIn("strategy-report.yml", doc())

    def test_the_workflows_with_a_schedule_are_the_ones_the_page_names(self) -> None:
        scheduled = sorted(
            path.name
            for path in workflow_paths()
            if re.search(r"^  schedule:\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
        )
        self.assertEqual(scheduled, sorted(SCHEDULED))
        for name in SCHEDULED:
            self.assertIn(f"`{name}`", doc())

    def test_the_permissions_policy_claim_is_the_policys_own(self) -> None:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.assertIn("when a workflow is missing from this file", policy["$comment"])
        self.assertIn("test_workflow_permissions_policy.py", policy["$comment"])
        self.assertTrue((REPO_ROOT / "tests" / "test_workflow_permissions_policy.py").exists())

    def test_risk_tiers_places_the_policy_file_in_tier_3(self) -> None:
        text = read("docs/risk-tiers.md")
        start = text.index("### Tier 3")
        end = text.index("### Tier 2")
        self.assertIn(".github/policies/workflow-permissions.json", text[start:end])
        self.assertIn("Tier 3", doc())


if __name__ == "__main__":
    unittest.main()
