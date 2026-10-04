"""
Script: tests/test_multi_agent_doc.py
What: Joins docs/multi-agent.md -- the page on which agents work in this repository, how work
reaches them, how they avoid colliding and who merges -- to the files its claims rest on.
Doing: Reads the five orchestrator paths the page says do not exist and checks that none of them
does. Reads the ai-fix.yml facts the page quotes (label, trigger phrase, branch prefix, bot
refusal, concurrency group) from the workflow and checks the page quotes the same values. Checks
the page's required check name, its "strict is off", "no merge queue", "no bypass" and "no
approving review" statements against .github/rulesets/main.json, and that the workflow named
`Python Unit Tests` and the two cancel-in-progress claims hold in test.yml and build-pr.yml.
Checks that the `area/*` labels the page attributes to the labeler workflow are the only ones
labeler.yml defines, that `.claude/settings.json` denies `gh pr merge`, and that the label the
page names as an approval label is on SECURITY-AI.md's list.
Why: The page is prose about files that change. "There is no orchestrator" stops being true the
day someone adds one, and "the ruleset has no merge queue" stops being true the day someone
turns one on; in both cases the page keeps reading as confident. Pull requests here are judged
one at a time by a non-strict required check, so a stale statement about that check is a wrong
instruction to whoever merges.
Goal: Make an orchestrator file added without updating the page, a changed ruleset, a renamed
trigger label or a dropped merge deny rule fail here, on the pull request that causes it.

What this cannot check: which roles Hive runs, what Hive does with an approval label, who merged
what, and whether agent credentials are set. Those are live GitHub state or live Hive state, and
the page carries the `gh` commands that show the first and third; the other two it says it does
not know.

No PyYAML and no third-party parser, for the reason tests/test_docs_consistency.py gives.
"""

from __future__ import annotations

import json
import re
import unittest

from tests.test_docs_consistency import REPO_ROOT

PAGE = REPO_ROOT / "docs" / "multi-agent.md"
AI_FIX = REPO_ROOT / ".github" / "workflows" / "ai-fix.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
BUILD_PR = REPO_ROOT / ".github" / "workflows" / "build-pr.yml"
LABELER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "labeler.yml"
LABELER_CONFIG = REPO_ROOT / ".github" / "labeler.yml"
RULESET = REPO_ROOT / ".github" / "rulesets" / "main.json"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
SECURITY_AI = REPO_ROOT / "docs" / "SECURITY-AI.md"
AGENTS = REPO_ROOT / "AGENTS.md"

# The names the ACMM (the external checklist) accepts for an in-repo orchestrator.
ORCHESTRATOR_PATHS = (
    ".github/workflows/dispatcher.yml",
    ".github/workflows/orchestrate.yml",
    "scripts/orchestrate.mjs",
    ".claude/dispatcher/",
    "orchestrator/",
)


def section(text: str, heading: str) -> str:
    """Return the body of a `## ` or `### ` section, up to the next heading of any level."""
    match = re.search(
        rf"^#{{2,3}} {re.escape(heading)}\n(.*?)(?=^#{{1,3}} |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise AssertionError(f"docs/multi-agent.md has no section {heading!r}")
    return match.group(1)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def workflow_value(text: str, key: str) -> str:
    """Return the quoted-or-bare value of a top-level-ish `key: value` line in a workflow."""
    match = re.search(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$", text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"no `{key}:` line found")
    return match.group(1).strip("'\"")


class OrchestratorAbsenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = PAGE.read_text(encoding="utf-8")

    def test_page_lists_exactly_the_accepted_orchestrator_paths(self) -> None:
        body = section(self.page, "There is no orchestrator in this repository")
        block = re.search(r"```text\n(.*?)```", body, re.DOTALL)
        self.assertIsNotNone(block, "the section lost its list of paths")
        listed = tuple(line.strip() for line in block.group(1).splitlines() if line.strip())
        self.assertEqual(tuple(sorted(listed)), tuple(sorted(ORCHESTRATOR_PATHS)))

    def test_none_of_those_paths_exists(self) -> None:
        present = [p for p in ORCHESTRATOR_PATHS if (REPO_ROOT / p).exists()]
        self.assertEqual(
            present,
            [],
            "docs/multi-agent.md says there is no in-repo orchestrator; update that "
            "section in the same pull request that adds one",
        )


class AiFixIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = squash(PAGE.read_text(encoding="utf-8"))
        self.workflow = AI_FIX.read_text(encoding="utf-8")

    def test_page_quotes_the_workflows_trigger_label_phrase_and_branch_prefix(self) -> None:
        for key in ("label_trigger", "trigger_phrase"):
            value = workflow_value(self.workflow, key)
            self.assertIn(f"`{value}`", self.page, f"page does not quote {key}={value!r}")
        # The page writes the branch prefix as a glob.
        self.assertIn("`ai-fix/*`", self.page)
        self.assertEqual(workflow_value(self.workflow, "branch_prefix"), "ai-fix/")

    def test_workflow_triggers_are_issue_label_and_comment(self) -> None:
        on_block = re.search(r"^on:\n(.*?)(?=^\S)", self.workflow, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(on_block)
        events = re.findall(r"^  (\w+):$", on_block.group(1), re.MULTILINE)
        self.assertEqual(events, ["issues", "issue_comment"])
        self.assertIn("labeled", on_block.group(1))
        self.assertIn("created", on_block.group(1))

    def test_bots_are_refused_and_the_allow_list_is_empty(self) -> None:
        self.assertIn('SENDER_TYPE}" = "Bot"', self.workflow)
        self.assertEqual(workflow_value(self.workflow, "allowed_bots"), "")
        self.assertIn("bot", self.page)
        self.assertIn("A maintainer who wants the agent to act relays it with `@claude`", self.page)

    def test_fork_branches_and_missing_credentials_are_skipped(self) -> None:
        self.assertIn('"${same_repo}" = "no"', self.workflow)
        self.assertIn('configured}" = "no"', self.workflow)
        self.assertIn("is not from a fork", self.page)

    def test_concurrency_group_is_per_issue_and_does_not_cancel(self) -> None:
        match = re.search(
            r"^concurrency:\n(?:\s+#.*\n)*\s+group: (.+)\n\s+cancel-in-progress: (\w+)",
            self.workflow,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "ai-fix.yml concurrency block changed shape")
        group, cancel = match.groups()
        self.assertEqual(group, "ai-fix-${{ github.event.issue.number }}")
        self.assertEqual(cancel, "false")
        self.assertIn("`concurrency` group is `ai-fix-`", self.page)
        self.assertIn("`cancel-in-progress: false`", self.page)


class RulesetClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = squash(PAGE.read_text(encoding="utf-8"))
        self.ruleset = json.loads(RULESET.read_text(encoding="utf-8"))
        self.rules = {rule["type"]: rule.get("parameters", {}) for rule in self.ruleset["rules"]}

    def test_required_check_named_on_the_page_is_the_rulesets(self) -> None:
        contexts = [
            c["context"] for c in self.rules["required_status_checks"]["required_status_checks"]
        ]
        self.assertEqual(contexts, ["Python Unit Tests"])
        self.assertIn("The required check is `Python Unit Tests`", self.page)

    def test_the_check_is_a_job_of_that_name_in_test_workflow(self) -> None:
        self.assertRegex(
            TEST_WORKFLOW.read_text(encoding="utf-8"),
            re.compile(r"^    name: Python Unit Tests$", re.MULTILINE),
        )

    def test_strict_policy_is_off(self) -> None:
        self.assertIs(
            self.rules["required_status_checks"]["strict_required_status_checks_policy"], False
        )
        self.assertIn("`strict_required_status_checks_policy` to `false`", self.page)

    def test_no_merge_queue_rule(self) -> None:
        self.assertNotIn("merge_queue", self.rules)
        self.assertIn("The ruleset has no merge queue", self.page)

    def test_no_bypass_and_no_required_approving_review(self) -> None:
        self.assertEqual(self.ruleset["bypass_actors"], [])
        self.assertEqual(self.rules["pull_request"]["required_approving_review_count"], 0)
        self.assertIn("it requires no approving review, and it allows no bypass actors", self.page)

    def test_pull_request_checks_cancel_older_runs_for_the_same_ref(self) -> None:
        for path in (TEST_WORKFLOW, BUILD_PR):
            text = path.read_text(encoding="utf-8")
            pattern = re.compile(
                r"^concurrency:\n(?:\s+#.*\n)*\s+group: .*\n\s+cancel-in-progress: true$",
                re.MULTILINE,
            )
            self.assertRegex(text, pattern, path.name)
        self.assertIn(
            "`test.yml` and `build-pr.yml` cancel an older run for the same ref", self.page
        )


class LabelAndMergeClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page = squash(PAGE.read_text(encoding="utf-8"))

    def test_labeler_workflow_exists_and_defines_only_area_labels(self) -> None:
        self.assertTrue(LABELER_WORKFLOW.is_file())
        labels = re.findall(
            r"^'([^']+)':$", LABELER_CONFIG.read_text(encoding="utf-8"), re.MULTILINE
        )
        self.assertGreater(len(labels), 0)
        self.assertEqual([x for x in labels if not x.startswith("area/")], [])
        self.assertIn(
            "`.github/workflows/labeler.yml` applies descriptive `area/*` labels", self.page
        )

    def test_the_approval_label_the_page_names_is_on_security_ais_list(self) -> None:
        listing = re.search(
            r"```text\n(.*?)```", SECURITY_AI.read_text(encoding="utf-8"), re.DOTALL
        )
        self.assertIsNotNone(listing)
        self.assertIn("hive/hive-wild-mole", listing.group(1).split())
        self.assertIn("`hive/hive-wild-mole`", self.page)

    def test_settings_deny_gh_pr_merge(self) -> None:
        deny = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["deny"]
        self.assertIn("Bash(gh pr merge:*)", deny)
        self.assertIn("`.claude/settings.json` denies the merge command outright", self.page)

    def test_agents_md_rule_six_is_the_rule_the_page_cites(self) -> None:
        text = AGENTS.read_text(encoding="utf-8")
        self.assertRegex(
            text, r"\n6\. \*\*Do not push, promote, tag, or delete published artifacts"
        )
        self.assertIn(
            "section 0 rule 6 says not to push, promote, tag, or delete published artifacts",
            self.page,
        )

    def test_security_ai_says_agents_never_merge_or_approve(self) -> None:
        text = squash(SECURITY_AI.read_text(encoding="utf-8"))
        self.assertIn("push to `main`, force-push a shared branch, merge, or approve", text)
        self.assertIn("lists merging and approving as things no agent may do", self.page)


if __name__ == "__main__":
    unittest.main()
