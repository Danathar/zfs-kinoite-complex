"""
Script: tests/test_workflow_build_failure_issue.py
What: Tests the two github-script steps in .github/workflows/akmods-failure-triage.yml that open,
comment on and close the `Scheduled run failing: Build And Promote Main Image` issue.
Doing: Takes each step's own `script:` text from the workflow, runs it under node with stand-ins
for `github`, `context` and `core` that record every issues API call, and checks the calls made
for a table of open issues: none, the bot's own issue, a same-titled issue opened by someone
else, a same-titled pull request, and an unrelated tracking issue.
Why: tests/test_workflow_akmods_triage_guards.py checks when these steps run, not what they do.
Nothing else ran their bodies, so dropping the author check -- which keeps a failure from being
reported into an issue anyone can open on this public repository -- the pull request filter, or
the title match, or closing every issue instead of only the bot's, left the whole suite green.
Goal: Make a change to who these steps write to, or what they write, fail on the pull request
that makes it.

This reads the workflow's own text. Copying the scripts into the test would assert that the copy
is right. The tests skip without PyYAML (see tests/test_workflow_publish_badges.py) or without
node; the CI runner image has both.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "akmods-failure-triage.yml"
BUILD_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build.yml"
NODE = shutil.which("node")

OPEN_STEP = "Open or update the build failure issue"
CLOSE_STEP = "Close the build failure issue on a real green build"
BOT = "github-actions[bot]"
RUN = {
    "html_url": "https://github.com/o/r/actions/runs/123",
    "conclusion": "timed_out",
    "head_sha": "0123abc",
}

# Runs the step body the way actions/github-script does: as the body of an async function
# given `github`, `context` and `core`. Every issues API call is recorded and printed as JSON.
HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const listForRepo = function listForRepo() { throw new Error('call it through paginate'); };
const record = (name, result) => async (params) => { calls.push([name, params]); return result; };
const github = {
  paginate: async (method, params) => {
    calls.push(['paginate', { listForRepo: method === listForRepo, ...params }]);
    return input.issues;
  },
  rest: { issues: {
    listForRepo,
    createComment: record('createComment', { data: {} }),
    create: record('create', { data: { number: 99 } }),
    update: record('update', { data: {} }),
  } },
};
const context = { repo: { owner: 'o', repo: 'r' }, payload: { workflow_run: input.run } };
const core = { info: () => {} };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core).then(
  () => process.stdout.write(JSON.stringify(calls)),
  (error) => { console.error(error); process.exit(1); },
);
"""


def issue(number: int, title: str, login: str = BOT, pull_request: bool = False) -> dict:
    entry: dict = {"number": number, "title": title, "user": {"login": login}}
    if pull_request:
        entry["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return entry


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(NODE is None, "node is not installed")
class BuildFailureIssueTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.steps = {
            step["name"]: step for step in workflow["jobs"]["triage"]["steps"] if "name" in step
        }
        build = yaml.safe_load(BUILD_WORKFLOW_PATH.read_text(encoding="utf-8"))
        # auto-issues.yml's convention: `Scheduled run failing:` and the workflow's name.
        self.title = f"Scheduled run failing: {build['name']}"

    def run_step(self, step_name: str, issues: list[dict]) -> list[list]:
        self.assertIn(step_name, self.steps, f"no step named {step_name!r}; was it renamed?")
        step = self.steps[step_name]
        self.assertTrue(step["uses"].startswith("actions/github-script@"), step["uses"])
        payload = {"script": step["with"]["script"], "run": RUN, "issues": issues}
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        # Both steps list the open issues first, through the paginated issues listing.
        self.assertEqual(calls[0][0], "paginate")
        listing = calls[0][1]
        self.assertTrue(listing["listForRepo"])
        self.assertEqual((listing["owner"], listing["repo"], listing["state"]), ("o", "r", "open"))
        return calls[1:]

    def writes(self, calls: list[list]) -> list[tuple[str, int | None]]:
        return [(name, params.get("issue_number")) for name, params in calls]

    def failure_line(self) -> str:
        return (
            f"Scheduled run {RUN['html_url']} ended with {RUN['conclusion']} on {RUN['head_sha']}."
        )

    # --- Open or update -------------------------------------------------------------------

    def test_a_first_failure_opens_the_issue(self) -> None:
        calls = self.run_step(OPEN_STEP, [])
        self.assertEqual(self.writes(calls), [("create", None)])
        params = calls[0][1]
        self.assertEqual((params["owner"], params["repo"]), ("o", "r"))
        self.assertEqual(params["title"], self.title)
        # The run link, its conclusion and the commit, so the issue says which run to open.
        self.assertTrue(params["body"].startswith(self.failure_line() + "\n\n"), params["body"])
        self.assertNotIn("labels", params)

    def test_a_repeat_failure_comments_on_the_bots_open_issue(self) -> None:
        calls = self.run_step(OPEN_STEP, [issue(7, self.title)])
        self.assertEqual(self.writes(calls), [("createComment", 7)])
        self.assertEqual(calls[0][1]["body"], self.failure_line())

    def test_a_same_titled_issue_opened_by_someone_else_is_never_written_to(self) -> None:
        # The repository is public: anyone can open an issue with this title.
        calls = self.run_step(OPEN_STEP, [issue(7, self.title, login="someone")])
        self.assertEqual(self.writes(calls), [("create", None)])

    def test_a_same_titled_pull_request_is_not_the_issue(self) -> None:
        calls = self.run_step(OPEN_STEP, [issue(7, self.title, pull_request=True)])
        self.assertEqual(self.writes(calls), [("create", None)])

    def test_another_workflows_tracking_issue_is_not_the_issue(self) -> None:
        other = issue(7, "Scheduled run failing: Nightly compliance")
        prefix = issue(8, self.title + " (old)")
        calls = self.run_step(OPEN_STEP, [other, prefix])
        self.assertEqual(self.writes(calls), [("create", None)])

    def test_the_bots_issue_is_found_among_the_others(self) -> None:
        issues = [
            issue(3, self.title, login="someone"),
            issue(4, self.title, pull_request=True),
            issue(5, self.title),
        ]
        calls = self.run_step(OPEN_STEP, issues)
        self.assertEqual(self.writes(calls), [("createComment", 5)])

    # --- Close ------------------------------------------------------------------------------

    def test_a_real_green_build_comments_on_and_closes_every_bot_issue(self) -> None:
        issues = [
            issue(5, self.title),
            issue(6, self.title, login="someone"),
            issue(7, self.title, pull_request=True),
            issue(8, "Scheduled run failing: Nightly compliance"),
            issue(9, self.title),
        ]
        calls = self.run_step(CLOSE_STEP, issues)
        self.assertEqual(
            self.writes(calls),
            [("createComment", 5), ("update", 5), ("createComment", 9), ("update", 9)],
        )
        for name, params in calls:
            self.assertEqual((params["owner"], params["repo"]), ("o", "r"))
            if name == "update":
                self.assertEqual(params["state"], "closed")
            else:
                self.assertIn(RUN["html_url"], params["body"])
                self.assertIn(RUN["head_sha"], params["body"])

    def test_nothing_is_written_when_no_bot_issue_is_open(self) -> None:
        issues = [issue(6, self.title, login="someone"), issue(7, self.title, pull_request=True)]
        self.assertEqual(self.run_step(CLOSE_STEP, issues), [])

    def test_both_steps_use_the_same_title(self) -> None:
        # The close step must find what the open step filed; a title edited in one step only
        # leaves the issue open forever.
        for step_name in (OPEN_STEP, CLOSE_STEP):
            with self.subTest(step=step_name):
                self.assertIn(
                    f"const title = '{self.title}';", self.steps[step_name]["with"]["script"]
                )


if __name__ == "__main__":
    unittest.main()
