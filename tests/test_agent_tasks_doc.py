"""
Script: tests/test_agent_tasks_doc.py
What: Joins docs/agent-tasks/README.md to the files in this repository that its claims rest on --
.github/workflows/ai-fix.yml, .github/rulesets/main.json, .github/pull_request_template.md and
.claude/session-summary.md -- and to the directories it says do not exist.
Doing: Reads the values the page quotes (the trigger label, the trigger phrase, the branch
prefix, the merge methods, the workflow filename in its `gh run list` command, every repository
path it names) out of the page's own text and recomputes each against the tree, as text, with no
YAML parser.
Why: The page is what a maintainer opens to find out which task an agent was on. It names the
label and phrase that start the in-repo path, the branch prefix that path uses, and the merge
methods the ruleset allows, and it says the repository keeps no task log. Each of those is a
fact about a file that someone can change without opening the page, and a page that sends a
reader to a renamed workflow or a dropped label is worse than none.
Goal: Make the page fail here when the files move under it.

Three deliberate omissions, named so they do not read as oversights.

The Hive signature line, the branch-prefix table and the `Closes #N` linkage describe pull
requests that live on GitHub and a system outside this repository. The unit suite has no token
and no network, and a shallow CI clone does not carry the history, so these stay review claims.
The page gives the `gh` commands that show them.

The `gh` and `git` commands are not run. Their flags are checked by hand against the live
repository when the page is written.

No PyYAML, for the reason tests/test_docs_consistency.py gives: the workflow tests that import
yaml skip when it is missing, and a doc-join test is the worst place to accept a silent skip.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "agent-tasks" / "README.md"
AI_FIX = REPO_ROOT / ".github" / "workflows" / "ai-fix.yml"
RULESET = REPO_ROOT / ".github" / "rulesets" / "main.json"
PR_TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"
SESSION_SUMMARY = REPO_ROOT / ".claude" / "session-summary.md"

# Paths the page names in backticks. Globs and placeholders are excluded by the character class.
_PATH_RE = re.compile(r"`((?:\.github|\.claude|docs|tests)/[A-Za-z0-9_./-]+)`")


# Paths the page names only to say they do not exist; NoTaskLogTests asserts that instead.
_NAMED_AS_ABSENT = {".github/agent-log/"}


def _flat(text: str) -> str:
    """Collapse line wrapping, so a phrase is found wherever the page happens to break it."""
    return " ".join(text.split())


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of one `## ` section, up to the next `## ` heading."""
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, flags=re.DOTALL | re.MULTILINE
    )
    if match is None:
        raise AssertionError(f"docs/agent-tasks/README.md has no '## {heading}' section")
    return match.group(1)


def _workflow_value(key: str) -> str:
    """Return the quoted or bare scalar a workflow `with:` key is set to."""
    match = re.search(
        rf"^\s+{re.escape(key)}:\s*['\"]?([^'\"\n]+?)['\"]?\s*$",
        AI_FIX.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"ai-fix.yml does not set `{key}`")
    return match.group(1)


class NamedPathTests(unittest.TestCase):
    def test_the_page_names_paths_and_every_one_exists(self) -> None:
        paths = [p for p in _PATH_RE.findall(_doc()) if p not in _NAMED_AS_ABSENT]
        self.assertGreaterEqual(len(paths), 4, "the page names no repository paths to check")
        missing = [p for p in paths if not (REPO_ROOT / p).exists()]
        self.assertEqual(missing, [], "docs/agent-tasks/README.md names paths that do not exist")

    def test_the_run_list_command_names_a_real_workflow_file(self) -> None:
        names = re.findall(r"gh run list --workflow (\S+)", _doc())
        self.assertEqual(len(names), 1, "expected exactly one `gh run list --workflow` command")
        self.assertTrue((REPO_ROOT / ".github" / "workflows" / names[0]).is_file(), names[0])


class InRepoPathTests(unittest.TestCase):
    """The page's `ai-fix.yml` section against the workflow it describes."""

    def test_label_phrase_and_prefix_are_the_ones_the_workflow_sets(self) -> None:
        section = _section(_doc(), "The In-Repo Path: `ai-fix.yml`")
        for key in ("label_trigger", "trigger_phrase", "branch_prefix"):
            value = _workflow_value(key)
            with self.subTest(key=key, value=value):
                self.assertIn(f"`{value}`", section)

    def test_the_workflow_starts_on_a_label_or_a_comment_and_skips_bots(self) -> None:
        workflow = AI_FIX.read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^on:\n  issues:\n    types:\n      - labeled\n")
        self.assertRegex(workflow, r"(?m)^  issue_comment:\n    types:\n      - created\n")
        self.assertIn('[ "${SENDER_TYPE}" = "Bot" ]', workflow)

    def test_comments_on_pull_requests_are_handled_as_the_page_says(self) -> None:
        # The page says `@claude` works in an issue or a pull request comment.
        self.assertIn("issue or pull request comment", _doc())
        self.assertIn("github.event.issue.pull_request", AI_FIX.read_text(encoding="utf-8"))


class MergeMethodTests(unittest.TestCase):
    def test_the_merge_methods_the_page_says_are_allowed_are_the_rulesets(self) -> None:
        match = re.search(r"allows `(\w+)`, `(\w+)`\s+and `(\w+)`", _doc())
        self.assertIsNotNone(match, "the page no longer lists the allowed merge methods")
        assert match is not None
        stated = set(match.groups())
        ruleset = json.loads(RULESET.read_text(encoding="utf-8"))
        allowed: set[str] = set()
        for rule in ruleset["rules"]:
            if rule["type"] == "pull_request":
                allowed = set(rule["parameters"]["allowed_merge_methods"])
        self.assertEqual(stated, allowed)


class TemplateTests(unittest.TestCase):
    def test_the_template_has_no_related_issue_heading(self) -> None:
        # The page says a hand-written pull request has no `Related Issue` heading from the
        # template, which is why `Closes #N` is only there if the author added it.
        self.assertIn("has no such heading", _doc())
        template = PR_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotRegex(template, r"(?im)^#+\s*related issue")


class SessionSummaryTests(unittest.TestCase):
    def test_the_summary_describes_itself_the_way_the_page_does(self) -> None:
        summary = SESSION_SUMMARY.read_text(encoding="utf-8")
        page = _section(_doc(), "What `.claude/session-summary.md` Is")
        page = _flat(page)
        self.assertIn("not a changelog", page.lower())
        self.assertIn("Not a changelog", summary)
        self.assertIn("`git log` is the changelog", summary)
        self.assertIn("work in flight", page)
        self.assertIn("work in flight", summary)
        self.assertIn("not yet visible in a diff", summary)


class NoTaskLogTests(unittest.TestCase):
    def test_the_directories_the_page_says_do_not_exist_do_not(self) -> None:
        self.assertIn("no `.agent/tasks/`", _doc())
        self.assertIn("`.github/agent-log/`", _doc())
        self.assertFalse((REPO_ROOT / ".agent").exists(), ".agent/ exists; the page says it does not")
        self.assertFalse(
            (REPO_ROOT / ".github" / "agent-log").exists(),
            ".github/agent-log/ exists; the page says it does not",
        )

    def test_the_page_directory_holds_only_the_page(self) -> None:
        self.assertIn("this directory holds only this page", _flat(_doc()))
        self.assertEqual([p.name for p in DOC.parent.iterdir()], ["README.md"])


if __name__ == "__main__":
    unittest.main()
