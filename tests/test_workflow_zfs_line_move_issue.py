"""
Script: tests/test_workflow_zfs_line_move_issue.py
What: Tests the github-script step in .github/workflows/akmods-failure-triage.yml that opens or
updates the informational `OpenZFS moved from X to Y` issue.
Doing: Takes the step's own `script:` text from the workflow, runs it under node in a temporary
directory holding a build-inputs manifest, with stand-ins for `github`, `context` and `core` that
record every API call, and checks the calls made for each combination of line change and
promote-stable result.
Why: The notice must fire only when a promoted build moved to a newer OpenZFS line. A
build-inputs artifact also exists for a run that did not promote, so a notice keyed on the
manifest alone would announce a move that never reached `:latest`.
Goal: Make a change to when this notice opens, or to whose issue it writes, fail on the pull
request that makes it.

This reads the workflow's own text. The tests skip without PyYAML or without node; the CI runner
image has both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "akmods-failure-triage.yml"
BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "build.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "ai-ops-runbook.md"
NODE = shutil.which("node")

STEP = "Open or update the OpenZFS line-move notice"
TITLE = "OpenZFS moved from 2.4 to 2.5"
BOT = "github-actions[bot]"
PROMOTE_JOB = "Promote Candidate To Stable"
RUN = {"id": 123, "html_url": "https://github.com/o/r/actions/runs/123", "head_sha": "0123abc"}

MOVED_INPUTS = {
    "zfs_minor_version": "2.5",
    "zfs_version": "2.5.1",
    "latest_zfs_minor_version": "2.4",
    "kernel_release": "7.3.4-200.fc44.x86_64",
}

# Runs the step body the way actions/github-script does. Every API call is recorded, and
# `core.summary` records what it would write to the step summary.
HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const listForRepo = function listForRepo() { throw new Error('call it through paginate'); };
const listJobsForWorkflowRun = function listJobsForWorkflowRun() {
  throw new Error('call it through paginate');
};
const record = (name, result) => async (params) => { calls.push([name, params]); return result; };
const github = {
  paginate: async (method, params) => {
    if (method === listJobsForWorkflowRun) {
      calls.push(['listJobs', params]);
      return input.jobs;
    }
    if (method === listForRepo) {
      calls.push(['listIssues', params]);
      return input.issues;
    }
    throw new Error('unexpected paginate');
  },
  rest: {
    issues: {
      listForRepo,
      create: record('create', { data: { number: 99 } }),
      update: record('update', { data: {} }),
      createComment: record('createComment', { data: {} }),
    },
    actions: { listJobsForWorkflowRun },
  },
};
const context = { repo: { owner: 'o', repo: 'r' }, payload: { workflow_run: input.run } };
const summary = {
  addHeading: (text, level) => { calls.push(['summaryHeading', { text, level }]); return summary; },
  addRaw: (text) => { calls.push(['summaryRaw', { text }]); return summary; },
  write: async () => { calls.push(['summaryWrite', {}]); return summary; },
};
const core = { info: () => {}, summary };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core).then(
  () => process.stdout.write(JSON.stringify(calls)),
  (error) => { console.error(error); process.exit(1); },
);
"""


def job(name: str = PROMOTE_JOB, conclusion: str = "success") -> dict:
    return {"name": name, "conclusion": conclusion}


def issue(number: int, title: str = TITLE, login: str = BOT, pull_request: bool = False) -> dict:
    entry: dict = {"number": number, "title": title, "user": {"login": login}}
    if pull_request:
        entry["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return entry


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(NODE is None, "node is not installed")
class ZfsLineMoveIssueTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        steps = {
            step["name"]: step for step in workflow["jobs"]["triage"]["steps"] if "name" in step
        }
        self.assertIn(STEP, steps, f"no step named {STEP!r}; was it renamed?")
        self.step = steps[STEP]
        self.assertTrue(self.step["uses"].startswith("actions/github-script@"), self.step["uses"])
        self.work_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work_dir)

    def write_manifest(self, inputs: dict) -> None:
        path = self.work_dir / "artifacts" / "build-inputs" / "build-inputs.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "inputs": inputs}), encoding="utf-8")

    def run_step(self, *, jobs: list[dict], issues: list[dict] | None = None) -> list[list]:
        payload = {
            "script": self.step["with"]["script"],
            "run": RUN,
            "jobs": jobs,
            "issues": issues or [],
        }
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=self.work_dir,
            env=dict(os.environ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def writes(self, calls: list[list]) -> list[tuple[str, int | None]]:
        return [
            (name, params.get("issue_number"))
            for name, params in calls
            if name in {"create", "update", "createComment"}
        ]

    def test_the_promote_job_name_is_the_one_build_yml_runs(self) -> None:
        build = yaml.safe_load(BUILD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(build["jobs"]["promote-stable"]["name"], PROMOTE_JOB)
        self.assertIn(f"'{PROMOTE_JOB}'", self.step["with"]["script"])

    def test_the_manifest_fields_are_the_ones_the_writer_records(self) -> None:
        writer = (REPO_ROOT / "ci_tools" / "write_build_inputs_manifest.py").read_text("utf-8")
        self.assertIn(
            '"latest_zfs_minor_version": optional_env("LATEST_ZFS_MINOR_VERSION")', writer
        )
        self.assertIn('"zfs_minor_version": require_env("ZFS_MINOR_VERSION")', writer)
        script = self.step["with"]["script"]
        self.assertIn("inputs.latest_zfs_minor_version", script)
        self.assertIn("inputs.zfs_minor_version", script)

    def test_a_promoted_move_opens_the_notice_and_writes_the_summary(self) -> None:
        self.write_manifest(MOVED_INPUTS)
        calls = self.run_step(jobs=[job("Build Candidate Image"), job()])
        listing = next(params for name, params in calls if name == "listJobs")
        self.assertEqual(listing["run_id"], RUN["id"])
        self.assertEqual(self.writes(calls), [("create", None)])
        created = next(params for name, params in calls if name == "create")
        self.assertEqual(created["title"], TITLE)
        body = created["body"]
        self.assertIn("Existing pools are untouched.", body)
        self.assertIn("`zpool upgrade`", body)
        self.assertIn("will not import on the previous image", body)
        self.assertIn("Nothing needs doing.", body)
        self.assertIn("`2.5.1`", body)
        self.assertIn(RUN["html_url"], body)
        self.assertIn(["summaryHeading", {"text": TITLE, "level": 2}], calls)
        self.assertEqual(calls[-1][0], "summaryWrite")

    def test_a_repeat_updates_the_bots_open_notice(self) -> None:
        self.write_manifest(MOVED_INPUTS)
        calls = self.run_step(
            jobs=[job()],
            issues=[issue(7, login="someone"), issue(8, pull_request=True), issue(9)],
        )
        self.assertEqual(self.writes(calls), [("update", 9)])

    def test_nothing_is_opened_when_promotion_did_not_succeed(self) -> None:
        self.write_manifest(MOVED_INPUTS)
        for jobs in (
            [],
            [job(conclusion="skipped")],
            [job(conclusion="failure")],
            [job("Build Candidate Image")],
        ):
            with self.subTest(jobs=jobs):
                self.assertEqual(self.writes(self.run_step(jobs=jobs)), [])

    def test_nothing_is_opened_when_the_line_did_not_move(self) -> None:
        for latest in ("2.5", "2.6", "", None, "2.5.1"):
            with self.subTest(latest=latest):
                shutil.rmtree(self.work_dir / "artifacts", ignore_errors=True)
                inputs = dict(MOVED_INPUTS)
                if latest is None:
                    del inputs["latest_zfs_minor_version"]
                else:
                    inputs["latest_zfs_minor_version"] = latest
                self.write_manifest(inputs)
                calls = self.run_step(jobs=[job()])
                self.assertEqual(self.writes(calls), [])
                # The job list is not even read: the manifest already says no move.
                self.assertNotIn("listJobs", [name for name, _ in calls])

    def test_lines_compare_numerically(self) -> None:
        self.write_manifest(MOVED_INPUTS | {"zfs_minor_version": "2.10"})
        calls = self.run_step(jobs=[job()])
        created = next(params for name, params in calls if name == "create")
        self.assertEqual(created["title"], "OpenZFS moved from 2.4 to 2.10")

    def test_the_runbook_describes_this_notice(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        section = runbook.split("## The OpenZFS line-move notice\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`OpenZFS moved from X to", section)
        self.assertIn("informational and asks for nothing", section)
        self.assertIn("`zpool upgrade`", section)

    def test_no_manifest_writes_nothing(self) -> None:
        self.assertEqual(self.run_step(jobs=[job()]), [])


if __name__ == "__main__":
    unittest.main()
