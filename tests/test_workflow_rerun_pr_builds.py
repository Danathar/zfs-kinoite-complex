"""
Script: tests/test_workflow_rerun_pr_builds.py
What: Tests the `rerun-pr-builds` job in .github/workflows/akmods-failure-triage.yml, which
re-runs the failed pull request image builds once a green main build clears an akmods build stop.
Doing: Checks how the job is wired (it runs only on the triage job's `cleared` output, which only
the close step sets, and its token holds nothing but `actions: write` and `pull-requests: read`),
then runs the job's own `script:` text under node with stand-ins for `github`, `context` and
`core` that record every API call, against a table of open pull requests and their newest
`build-pr.yml` run.
Why: A pull request's image build checks the shared akmods image the main build publishes, so an
akmods build stop fails every pull request build too, and nothing started them again once the
stop cleared: Renovate's weekly pull request is never rebased, so it stayed red until someone
re-ran it by hand. This job is what re-runs them. Re-running the wrong thing is the risk: a
fork's build, a build that is still going, or an older build than the one the pull request shows.
Goal: Make a change to when the job runs, what its token can do, or which runs it re-runs fail
on the pull request that makes it.

This reads the workflow's own text. Copying the script into the test would assert that the copy
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
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS / "akmods-failure-triage.yml"
NODE = shutil.which("node")

JOB = "rerun-pr-builds"
STEP = "Re-run failed pull request image builds"
CLOSE_STEP = "Close stale sticky issues on successful run"

# Runs the step body the way actions/github-script does. `runs` maps a head SHA to the runs the
# API lists for it, newest first; `rerun_errors` lists the run ids whose re-run is refused.
HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const warnings = [];
const list = function list() { throw new Error('call it through paginate'); };
const github = {
  paginate: async (method, params) => {
    calls.push(['paginate', { pullsList: method === list, ...params }]);
    return input.pulls;
  },
  rest: {
    pulls: { list },
    actions: {
      listWorkflowRuns: async (params) => {
        calls.push(['listWorkflowRuns', params]);
        const runs = input.runs[params.head_sha] || [];
        return { data: { workflow_runs: runs.slice(0, params.per_page) } };
      },
      reRunWorkflowFailedJobs: async (params) => {
        calls.push(['reRunWorkflowFailedJobs', params]);
        if (input.rerun_errors.includes(params.run_id)) {
          throw new Error(`run ${params.run_id} cannot be re-run`);
        }
        return { data: {} };
      },
    },
  },
};
const context = { repo: { owner: 'o', repo: 'r' } };
const core = { info: () => {}, warning: (message) => { warnings.push(message); } };
const done = (ok) => process.stdout.write(JSON.stringify({ ok, calls, warnings }));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core).then(
  () => done(true),
  (error) => { console.error(error); done(false); },
);
"""


def pull(number: int, sha: str, head_repo: str | None = "o/r") -> dict:
    repo = None if head_repo is None else {"full_name": head_repo}
    return {"number": number, "head": {"sha": sha, "repo": repo}}


def run(run_id: int, conclusion: str | None, status: str = "completed") -> dict:
    return {"id": run_id, "status": status, "conclusion": conclusion}


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class RerunPrBuildsWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.jobs = self.workflow["jobs"]
        self.assertIn(JOB, self.jobs, f"no job named {JOB!r}; was it renamed?")
        self.job = self.jobs[JOB]

    def test_the_job_runs_only_when_the_triage_job_cleared_a_stop(self) -> None:
        self.assertEqual(self.job["needs"], "triage")
        self.assertEqual(self.job["if"], "needs.triage.outputs.cleared == 'true'")

    def test_the_cleared_output_comes_from_the_close_step(self) -> None:
        triage = self.jobs["triage"]
        self.assertEqual(
            triage["outputs"], {"cleared": "${{ steps.close_sticky.outputs.cleared }}"}
        )
        by_id = {step.get("id"): step for step in triage["steps"]}
        self.assertEqual(by_id["close_sticky"]["name"], CLOSE_STEP)

    def test_the_token_can_re_run_builds_and_nothing_more(self) -> None:
        # `actions: write` stays out of the triage job, whose token can already push.
        self.assertEqual(self.job["permissions"], {"actions": "write", "pull-requests": "read"})
        self.assertEqual(self.jobs["triage"]["permissions"]["actions"], "read")

    def test_the_workflow_it_re_runs_is_the_pull_request_image_build(self) -> None:
        script = self.job["steps"][0]["with"]["script"]
        self.assertIn("workflow_id: 'build-pr.yml',", script)
        build_pr = yaml.safe_load((WORKFLOWS / "build-pr.yml").read_text(encoding="utf-8"))
        # PyYAML reads the bare `on:` key as True.
        self.assertIn("pull_request", build_pr[True])
        names = [job["name"] for job in build_pr["jobs"].values()]
        self.assertEqual(names, ["Build PR Image (No Push)"])


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(NODE is None, "node is not installed")
class RerunPrBuildsStepTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        steps = {step["name"]: step for step in workflow["jobs"][JOB]["steps"]}
        self.assertIn(STEP, steps, f"no step named {STEP!r}; was it renamed?")
        self.assertTrue(steps[STEP]["uses"].startswith("actions/github-script@"))
        self.script = steps[STEP]["with"]["script"]

    def run_step(
        self,
        pulls: list[dict],
        runs: dict[str, list[dict]] | None = None,
        rerun_errors: list[int] | None = None,
    ) -> tuple[list[list], list[str]]:
        given = {
            "script": self.script,
            "pulls": pulls,
            "runs": runs or {},
            "rerun_errors": rerun_errors or [],
        }
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps(given),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"], result.stderr)
        calls = report["calls"]
        name, params = calls[0]
        self.assertEqual(name, "paginate")
        self.assertEqual(
            params, {"pullsList": True, "owner": "o", "repo": "r", "state": "open", "per_page": 100}
        )
        return calls[1:], report["warnings"]

    @staticmethod
    def reruns(calls: list[list]) -> list[int]:
        return [params["run_id"] for name, params in calls if name == "reRunWorkflowFailedJobs"]

    def test_the_newest_failed_build_of_each_pull_request_is_re_run(self) -> None:
        calls, warnings = self.run_step(
            [pull(1, "aaa"), pull(2, "bbb")],
            {"aaa": [run(11, "failure")], "bbb": [run(21, "failure")]},
        )
        self.assertEqual(self.reruns(calls), [11, 21])
        self.assertEqual(warnings, [])
        for name, params in calls:
            self.assertEqual((params["owner"], params["repo"]), ("o", "r"))
            if name == "listWorkflowRuns":
                self.assertEqual(params["workflow_id"], "build-pr.yml")
                self.assertEqual(params["event"], "pull_request")
                self.assertEqual(params["per_page"], 1)
        self.assertEqual(
            [params["head_sha"] for name, params in calls if name == "listWorkflowRuns"],
            ["aaa", "bbb"],
        )

    def test_only_the_newest_build_counts(self) -> None:
        # An older failure under a newer green build is not what the pull request shows.
        calls, _ = self.run_step(
            [pull(1, "aaa")], {"aaa": [run(12, "success"), run(11, "failure")]}
        )
        self.assertEqual(self.reruns(calls), [])

    def test_a_build_that_did_not_fail_is_left_alone(self) -> None:
        for newest in (
            run(11, "success"),
            run(11, "cancelled"),
            run(11, "skipped"),
            run(11, None, status="in_progress"),
            run(11, None, status="queued"),
        ):
            with self.subTest(newest=newest):
                calls, _ = self.run_step([pull(1, "aaa")], {"aaa": [newest]})
                self.assertEqual(self.reruns(calls), [])

    def test_a_pull_request_with_no_build_is_skipped(self) -> None:
        calls, _ = self.run_step([pull(1, "aaa"), pull(2, "bbb")], {"bbb": [run(21, "failure")]})
        self.assertEqual(self.reruns(calls), [21])

    def test_a_fork_pull_request_is_never_re_run_or_looked_up(self) -> None:
        calls, _ = self.run_step(
            [pull(1, "aaa", head_repo="someone/r"), pull(2, "bbb", head_repo=None)],
            {"aaa": [run(11, "failure")], "bbb": [run(21, "failure")]},
        )
        self.assertEqual(calls, [])

    def test_a_refused_re_run_does_not_stop_the_rest(self) -> None:
        calls, warnings = self.run_step(
            [pull(1, "aaa"), pull(2, "bbb")],
            {"aaa": [run(11, "failure")], "bbb": [run(21, "failure")]},
            rerun_errors=[11],
        )
        self.assertEqual(self.reruns(calls), [11, 21])
        self.assertEqual(len(warnings), 1)
        self.assertIn("11", warnings[0])
        self.assertIn("#1", warnings[0])


if __name__ == "__main__":
    unittest.main()
