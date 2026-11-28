"""
Script: tests/test_workflow_kernel_held_issue.py
What: Tests the github-script step in .github/workflows/akmods-failure-triage.yml that opens,
refreshes and closes the `Kernel held: waiting for OpenZFS to support the newest Fedora kernel`
issue.
Doing: Takes the step's own `script:` text from the workflow, runs it under node in a temporary
directory holding a build-inputs manifest written by ci_tools/write_build_inputs_manifest.py's
own field names, with stand-ins for `github`, `context` and `core` that record every issues API
call, and checks the calls made for a held and an unheld build against a table of open issues.
Why: A green build that held the base image on an older kernel is not a failure, and the step
before this one closes the akmods-failure issue that described the incompatibility, so this issue
is the only place the hold stays visible. Dropping the author check (the repository is public),
closing on a held build, or opening a second issue on every run would all leave the rest of the
suite green.
Goal: Make a change to when this issue opens or closes, or to whose issue it writes, fail on the
pull request that makes it.

This reads the workflow's own text. Copying the script into the test would assert that the copy
is right. The tests skip without PyYAML (see tests/test_workflow_publish_badges.py) or without
node; the CI runner image has both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ci_tools.resolve_build_inputs import HELD_BASE_PACKAGE, KERNEL_HOLD_MAX_DAYS

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "akmods-failure-triage.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "ai-ops-runbook.md"
NODE = shutil.which("node")

STEP = "Open, refresh or close the kernel-held issue"
TITLE = "Kernel held: waiting for OpenZFS to support the newest Fedora kernel"
BOT = "github-actions[bot]"
RUN = {"html_url": "https://github.com/o/r/actions/runs/123", "head_sha": "0123abc"}

HELD_INPUTS = {
    "kernel_held_from": "7.3.4-200.fc44.x86_64",
    "kernel_release": "7.2.15-200.fc44.x86_64",
    "zfs_version": "2.4.4",
    "base_image_name": "quay.io/fedora-ostree-desktops/kinoite",
    "base_image_tag": "44.20261110.0",
}
UNHELD_INPUTS = HELD_INPUTS | {"kernel_held_from": "", "kernel_release": "7.3.4-200.fc44.x86_64"}

# Runs the step body the way actions/github-script does: as the body of an async function
# given `github`, `context` and `core`, in the current directory. Every issues API call is
# recorded and printed as JSON.
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


def issue(number: int, title: str = TITLE, login: str = BOT, pull_request: bool = False) -> dict:
    entry: dict = {"number": number, "title": title, "user": {"login": login}}
    if pull_request:
        entry["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return entry


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(NODE is None, "node is not installed")
class KernelHeldIssueTests(unittest.TestCase):
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

    def run_step(self, issues: list[dict], copy_state: str = "") -> list[list]:
        payload = {"script": self.step["with"]["script"], "run": RUN, "issues": issues}
        env = {key: value for key, value in os.environ.items() if key != "HELD_BASE_COPY_STATE"}
        if copy_state:
            env["HELD_BASE_COPY_STATE"] = copy_state
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=self.work_dir,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def writes(self, calls: list[list]) -> list[tuple[str, int | None]]:
        return [(name, params.get("issue_number")) for name, params in calls if name != "paginate"]

    def test_the_manifest_path_is_where_the_unzip_step_puts_it(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        unzip = next(
            step
            for step in workflow["jobs"]["triage"]["steps"]
            if step.get("name") == "Unzip build inputs manifest"
        )
        self.assertIn("cd artifacts/build-inputs", unzip["run"])
        self.assertIn("'artifacts/build-inputs/build-inputs.json'", self.step["with"]["script"])

    def test_the_manifest_field_is_the_one_the_writer_records(self) -> None:
        writer = (REPO_ROOT / "ci_tools" / "write_build_inputs_manifest.py").read_text("utf-8")
        self.assertIn('"kernel_held_from": optional_env("KERNEL_HELD_FROM")', writer)
        self.assertIn("inputs.kernel_held_from", self.step["with"]["script"])

    def test_a_first_held_build_opens_the_issue(self) -> None:
        self.write_manifest(HELD_INPUTS)
        calls = self.run_step([])
        listing = calls[0]
        self.assertEqual(listing[0], "paginate")
        self.assertTrue(listing[1]["listForRepo"])
        self.assertEqual(
            (listing[1]["owner"], listing[1]["repo"], listing[1]["state"]), ("o", "r", "open")
        )
        self.assertEqual(self.writes(calls), [("create", None)])
        params = calls[1][1]
        self.assertEqual(params["title"], TITLE)
        body = params["body"]
        for value in HELD_INPUTS.values():
            self.assertIn(f"{value}", body)
        self.assertIn("`quay.io/fedora-ostree-desktops/kinoite:44.20261110.0`", body)
        self.assertIn(RUN["html_url"], body)

    def test_a_repeat_held_build_refreshes_the_bots_open_issue(self) -> None:
        self.write_manifest(HELD_INPUTS)
        calls = self.run_step([issue(7)])
        self.assertEqual(self.writes(calls), [("update", 7)])
        self.assertNotIn("state", calls[1][1])

    def test_a_same_titled_issue_from_someone_else_is_never_written_to(self) -> None:
        self.write_manifest(HELD_INPUTS)
        calls = self.run_step([issue(7, login="someone"), issue(8, pull_request=True)])
        self.assertEqual(self.writes(calls), [("create", None)])

    def test_an_unheld_build_closes_only_the_bots_open_issue(self) -> None:
        self.write_manifest(UNHELD_INPUTS)
        calls = self.run_step(
            [
                issue(7),
                issue(8, login="someone"),
                issue(9, title="Something else"),
                issue(10, pull_request=True),
            ]
        )
        self.assertEqual(self.writes(calls), [("createComment", 7), ("update", 7)])
        self.assertEqual(calls[-1][1]["state"], "closed")

    def test_an_unheld_build_with_nothing_open_writes_nothing(self) -> None:
        self.write_manifest(UNHELD_INPUTS)
        self.assertEqual(self.writes(self.run_step([])), [])

    def test_a_manifest_from_before_the_field_existed_reads_as_unheld(self) -> None:
        inputs = dict(UNHELD_INPUTS)
        del inputs["kernel_held_from"]
        self.write_manifest(inputs)
        self.assertEqual(
            self.writes(self.run_step([issue(7)])), [("createComment", 7), ("update", 7)]
        )

    def test_the_runbook_section_names_the_title_this_step_writes(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        section = runbook.split("## The kernel-held issue\n", 1)[1].split("\n## ", 1)[0]
        flat = " ".join(section.split())
        self.assertIn(f"`{TITLE}`", flat)
        self.assertIn(f"const title = '{TITLE}';", self.step["with"]["script"])

    def test_a_lock_file_replay_leaves_the_issue_alone(self) -> None:
        # A replay is never held, so its green build says nothing about the hold.
        self.write_manifest(UNHELD_INPUTS | {"use_input_lock": True})
        self.assertEqual(self.run_step([issue(7)]), [])

    def test_no_manifest_leaves_the_issue_alone(self) -> None:
        self.assertEqual(self.run_step([issue(7)]), [])

    def test_the_copy_state_comes_from_the_status_step(self) -> None:
        self.assertEqual(
            self.step["env"]["HELD_BASE_COPY_STATE"], "${{ steps.held_copy.outputs.state }}"
        )
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        status = [
            step for step in workflow["jobs"]["triage"]["steps"] if step.get("id") == "held_copy"
        ]
        self.assertEqual(len(status), 1)
        self.assertIn("python3 -m ci_tools.cli held-base-copy-status", status[0]["run"])
        self.assertEqual(
            status[0]["env"],
            {
                "BUILD_INPUTS_MANIFEST": "artifacts/build-inputs/build-inputs.json",
                "REGISTRY_ACTOR": "${{ github.actor }}",
                "REGISTRY_TOKEN": "${{ github.token }}",
            },
        )

    def test_a_copy_that_is_not_public_asks_the_owner_to_make_it_public(self) -> None:
        self.write_manifest(HELD_INPUTS)
        body = self.run_step([], copy_state="not-public")[1][1]["body"]
        self.assertIn("**Action needed:**", body)
        self.assertIn(f"`ghcr.io/o/{HELD_BASE_PACKAGE}`", body)
        self.assertIn("Change visibility", body)

    def test_a_missing_copy_says_the_next_build_makes_it(self) -> None:
        self.write_manifest(HELD_INPUTS)
        body = self.run_step([], copy_state="missing")[1][1]["body"]
        self.assertIn("does not exist yet", body)
        self.assertIn("kernel-held-copy-missing", body)
        self.assertNotIn("**Action needed:**", body)

    def test_a_public_copy_adds_nothing(self) -> None:
        self.write_manifest(HELD_INPUTS)
        body = self.run_step([], copy_state="public")[1][1]["body"]
        self.assertNotIn(HELD_BASE_PACKAGE, body)

    def test_the_body_states_the_hold_limit_resolution_uses(self) -> None:
        self.write_manifest(HELD_INPUTS)
        body = self.run_step([])[1][1]["body"]
        self.assertIn(f"from the last {KERNEL_HOLD_MAX_DAYS} days", body)
        self.assertIn("DEFAULT_ZFS_MINOR_VERSION", body)

    def test_a_backtick_or_newline_in_a_label_cannot_break_out_of_its_code_span(self) -> None:
        self.write_manifest(HELD_INPUTS | {"kernel_held_from": "7.3.4`\n## injected"})
        calls = self.run_step([])
        body = calls[1][1]["body"]
        self.assertIn("`7.3.4## injected`", body)


if __name__ == "__main__":
    unittest.main()
