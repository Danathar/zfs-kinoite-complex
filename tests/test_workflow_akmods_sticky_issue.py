"""
Script: tests/test_workflow_akmods_sticky_issue.py
What: Tests the first three github-script steps in .github/workflows/akmods-failure-triage.yml:
the step that looks for the build's artifacts and downloads the akmods failure payload, the step
that opens or updates the sticky `akmods-failure` issue, and the step that closes those issues
after a green build.
Doing: Takes each step's own `script:` text from the workflow, runs it under node with stand-ins
for `github`, `context` and `core` that record every API call and every step output, in a
temporary working directory. The sticky issue step reads a payload made by
ci_tools/classify_akmods_failure.py's own `build_sticky_issue_payload`, the function whose output
the build uploads.
Why: tests/test_workflow_akmods_triage_guards.py checks when these steps run, not what they do,
and tests/test_workflow_build_failure_issue.py runs only the two build failure issue steps.
Nothing ran these three bodies. The download step's `build_ran` output is what keeps a
gate-skipped run, which reports success without building, from closing issues and turning the
badge green; its `has_payload` output decides whether a failure gets a sticky issue at all. A
change to either, to the key marker that keeps one issue per failure, or to which issues the
close step touches left the whole suite green.
Goal: Make a change to what these steps report, download, or write to fail on the pull request
that makes it.

This reads the workflow's own text. Copying the scripts into the test would assert that the copy
is right. The tests skip without PyYAML (see tests/test_workflow_publish_badges.py) or without
node; the CI runner image has both.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ci_tools.classify_akmods_failure import (
    FAILURE_KIND_UPSTREAM_COMPAT,
    build_sticky_issue_payload,
)

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "akmods-failure-triage.yml"
NODE = shutil.which("node")

DOWNLOAD_STEP = "Download failure payload if present"
OPEN_STEP = "Open or update sticky issue on failed run"
CLOSE_STEP = "Close stale sticky issues on successful run"
LABEL = "akmods-failure"
RUN = {"id": 4242, "head_sha": "0123abc"}
ZIP_BYTES = b"PK\x03\x04 not really a zip, only bytes to copy\x00\xff"

# Runs the step body the way actions/github-script does: as the body of an async function
# given `github`, `context` and `core`, in the current directory. Prints every API call and
# every `core.setOutput` as JSON, also when the script throws, so a test can see what was
# written before the failure.
HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
const outputs = [];
const listForRepo = function listForRepo() { throw new Error('call it through paginate'); };
const record = (name, result) => async (params) => { calls.push([name, params]); return result; };
const github = {
  paginate: async (method, params) => {
    calls.push(['paginate', { listForRepo: method === listForRepo, ...params }]);
    return input.issues;
  },
  rest: {
    actions: {
      listWorkflowRunArtifacts: record('listWorkflowRunArtifacts',
        { data: { artifacts: input.artifacts } }),
      downloadArtifact: async (params) => {
        calls.push(['downloadArtifact', params]);
        const bytes = Buffer.from(input.zip_base64, 'base64');
        // Octokit hands back an ArrayBuffer for a binary download.
        return { data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length) };
      },
    },
    issues: {
      listForRepo,
      getLabel: async (params) => {
        calls.push(['getLabel', params]);
        if (input.label_status !== null) {
          const error = new Error(`label lookup failed with ${input.label_status}`);
          error.status = input.label_status;
          throw error;
        }
        return { data: { name: params.name } };
      },
      createLabel: record('createLabel', { data: {} }),
      createComment: record('createComment', { data: {} }),
      create: record('create', { data: { number: 99 } }),
      update: record('update', { data: {} }),
    },
  },
};
const context = { repo: { owner: 'o', repo: 'r' }, payload: { workflow_run: input.run } };
const core = { info: () => {}, setOutput: (name, value) => { outputs.push([name, value]); } };
const done = (ok) => process.stdout.write(JSON.stringify({ ok, calls, outputs }));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction('github', 'context', 'core', input.script)(github, context, core).then(
  () => done(true),
  (error) => { console.error(error); done(false); },
);
"""


def artifact(artifact_id: int, name: str) -> dict:
    return {"id": artifact_id, "name": name}


def issue(number: int, body: str | None) -> dict:
    return {"number": number, "title": f"Sticky {number}", "body": body}


def payload(kernel: str = "6.14.2-300.fc42.x86_64") -> dict:
    return build_sticky_issue_payload(
        failure_kind=FAILURE_KIND_UPSTREAM_COMPAT,
        kernel_release=kernel,
        akmods_upstream_ref="abcdef0123456789",
        fedora_version="42",
        run_id="4242",
        run_url="https://github.com/o/r/actions/runs/4242",
        matched_patterns=["implicit declaration of function"],
    )


def marker(failure: dict) -> str:
    return f"<!-- akmods-failure-key: {failure['key']} -->"


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(NODE is None, "node is not installed")
class AkmodsStickyIssueStepTests(unittest.TestCase):
    def setUp(self) -> None:
        workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.steps = {
            step["name"]: step for step in workflow["jobs"]["triage"]["steps"] if "name" in step
        }
        self.work_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work_dir)

    def run_step(
        self,
        step_name: str,
        *,
        artifacts: list[dict] | None = None,
        issues: list[dict] | None = None,
        label_status: int | None = None,
        expect_ok: bool = True,
    ) -> tuple[list[list], dict[str, str]]:
        self.assertIn(step_name, self.steps, f"no step named {step_name!r}; was it renamed?")
        step = self.steps[step_name]
        self.assertTrue(step["uses"].startswith("actions/github-script@"), step["uses"])
        given = {
            "script": step["with"]["script"],
            "run": RUN,
            "artifacts": artifacts or [],
            "issues": issues or [],
            "label_status": label_status,
            "zip_base64": base64.b64encode(ZIP_BYTES).decode("ascii"),
        }
        result = subprocess.run(
            [NODE, "-e", HARNESS],
            input=json.dumps(given),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=self.work_dir,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["ok"], expect_ok, result.stderr)
        names = [name for name, _ in report["outputs"]]
        self.assertEqual(len(names), len(set(names)), f"an output was set twice: {names}")
        return report["calls"], dict(report["outputs"])

    def write_payload(self, failure: dict) -> None:
        path = self.work_dir / "artifacts" / "akmods-failure.json"
        path.parent.mkdir()
        path.write_text(json.dumps(failure), encoding="utf-8")

    # --- Download failure payload -----------------------------------------------------------

    def run_download(self, artifacts: list[dict]) -> tuple[list[list], dict[str, str]]:
        calls, outputs = self.run_step(DOWNLOAD_STEP, artifacts=artifacts)
        name, params = calls[0]
        self.assertEqual(name, "listWorkflowRunArtifacts")
        # The artifacts of the build that finished, not of this triage run.
        self.assertEqual(params, {"owner": "o", "repo": "r", "run_id": RUN["id"]})
        return calls[1:], outputs

    def zip_path(self) -> Path:
        return self.work_dir / "artifacts" / "akmods-failure.zip"

    def test_a_run_with_no_artifacts_did_not_build_and_has_no_payload(self) -> None:
        # A scheduled run the stable-signal gate skipped: it reports success, uploads nothing.
        calls, outputs = self.run_download([])
        self.assertEqual(outputs, {"build_ran": "false", "has_payload": "false"})
        self.assertEqual(calls, [])
        self.assertFalse((self.work_dir / "artifacts").exists())

    def test_the_build_inputs_artifact_means_a_real_build_ran(self) -> None:
        calls, outputs = self.run_download([artifact(1, f"build-inputs-{RUN['id']}")])
        self.assertEqual(outputs, {"build_ran": "true", "has_payload": "false"})
        self.assertEqual(calls, [])

    def test_a_failure_payload_is_downloaded_into_artifacts(self) -> None:
        artifacts = [
            artifact(1, f"build-inputs-{RUN['id']}"),
            artifact(2, "something-else"),
            artifact(3, f"akmods-failure-{RUN['id']}"),
        ]
        calls, outputs = self.run_download(artifacts)
        self.assertEqual(outputs, {"build_ran": "true", "has_payload": "true"})
        self.assertEqual(
            calls,
            [
                [
                    "downloadArtifact",
                    {"owner": "o", "repo": "r", "artifact_id": 3, "archive_format": "zip"},
                ]
            ],
        )
        # The next step runs `cd artifacts && unzip -o akmods-failure.zip`.
        self.assertIn("unzip -o akmods-failure.zip", self.steps["Unzip payload"]["run"])
        self.assertEqual(self.zip_path().read_bytes(), ZIP_BYTES)

    def test_build_ran_and_has_payload_are_reported_independently(self) -> None:
        calls, outputs = self.run_download([artifact(3, f"akmods-failure-{RUN['id']}")])
        self.assertEqual(outputs, {"build_ran": "false", "has_payload": "true"})
        self.assertEqual([name for name, _ in calls], ["downloadArtifact"])

    def test_artifact_names_are_matched_by_prefix_only(self) -> None:
        # A name that merely contains a prefix is some other artifact.
        artifacts = [
            artifact(1, f"old-build-inputs-{RUN['id']}"),
            artifact(2, f"pr-akmods-failure-{RUN['id']}"),
        ]
        calls, outputs = self.run_download(artifacts)
        self.assertEqual(outputs, {"build_ran": "false", "has_payload": "false"})
        self.assertEqual(calls, [])

    def test_both_prefixes_are_the_names_the_build_uploads(self) -> None:
        action = (
            REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
        ).read_text(encoding="utf-8")
        script = self.steps[DOWNLOAD_STEP]["with"]["script"]
        self.assertIn("name: build-inputs-${{ github.run_id }}", action)
        self.assertIn("a.name.startsWith('build-inputs-')", script)
        self.assertIn("name: akmods-failure-${{ github.run_id }}", action)
        self.assertIn("const prefix = 'akmods-failure-';", script)

    # --- Open or update the sticky issue ----------------------------------------------------

    def writes(self, calls: list[list]) -> list[list]:
        return [call for call in calls if call[0] not in ("getLabel", "paginate")]

    def check_listing(self, calls: list[list]) -> None:
        listing = [params for name, params in calls if name == "paginate"]
        self.assertEqual(len(listing), 1, calls)
        self.assertTrue(listing[0]["listForRepo"])
        self.assertEqual(
            (listing[0]["owner"], listing[0]["repo"], listing[0]["state"], listing[0]["labels"]),
            ("o", "r", "open", LABEL),
        )

    def test_a_new_failure_opens_a_labelled_issue_with_its_key_marker(self) -> None:
        failure = payload()
        self.write_payload(failure)
        calls, _ = self.run_step(OPEN_STEP)
        self.assertEqual(calls[0], ["getLabel", {"owner": "o", "repo": "r", "name": LABEL}])
        self.check_listing(calls)
        self.assertEqual(
            self.writes(calls),
            [
                [
                    "create",
                    {
                        "owner": "o",
                        "repo": "r",
                        "title": failure["title"],
                        "body": f"{marker(failure)}\n\n{failure['body']}",
                        "labels": [LABEL],
                    },
                ]
            ],
        )

    def test_a_repeat_failure_updates_the_issue_carrying_its_key(self) -> None:
        failure = payload()
        self.write_payload(failure)
        issues = [issue(5, None), issue(6, f"{marker(failure)}\n\nold body")]
        calls, _ = self.run_step(OPEN_STEP, issues=issues)
        self.assertEqual(
            self.writes(calls),
            [
                [
                    "update",
                    {
                        "owner": "o",
                        "repo": "r",
                        "issue_number": 6,
                        "body": f"{marker(failure)}\n\n{failure['body']}",
                    },
                ]
            ],
        )

    def test_a_different_failure_gets_its_own_issue(self) -> None:
        # One issue per distinct failure: a new kernel is a new key.
        old = payload(kernel="6.14.2-300.fc42.x86_64")
        new = payload(kernel="6.14.3-300.fc42.x86_64")
        self.write_payload(new)
        calls, _ = self.run_step(OPEN_STEP, issues=[issue(6, f"{marker(old)}\n\nold body")])
        self.assertEqual([name for name, _ in self.writes(calls)], ["create"])

    def test_a_key_that_extends_another_does_not_match_it(self) -> None:
        failure = payload()
        longer = dict(failure, key=failure["key"] + "9")
        self.write_payload(failure)
        calls, _ = self.run_step(OPEN_STEP, issues=[issue(6, f"{marker(longer)}\n\nold body")])
        self.assertEqual([name for name, _ in self.writes(calls)], ["create"])

    def test_a_missing_label_is_created_first(self) -> None:
        self.write_payload(payload())
        calls, _ = self.run_step(OPEN_STEP, label_status=404)
        created = [params for name, params in calls if name == "createLabel"]
        self.assertEqual(len(created), 1, calls)
        self.assertEqual((created[0]["owner"], created[0]["repo"]), ("o", "r"))
        self.assertEqual(created[0]["name"], LABEL)
        self.assertEqual([name for name, _ in self.writes(calls)], ["createLabel", "create"])

    def test_any_other_label_error_fails_the_step_before_writing(self) -> None:
        self.write_payload(payload())
        calls, _ = self.run_step(OPEN_STEP, label_status=500, expect_ok=False)
        self.assertEqual([name for name, _ in calls], ["getLabel"])

    def test_the_label_is_the_one_the_issue_form_documents(self) -> None:
        # tests/test_issue_templates.py ties the issue form to this literal.
        for step_name in (OPEN_STEP, CLOSE_STEP):
            with self.subTest(step=step_name):
                self.assertIn(
                    f"const stickyLabel = '{LABEL}';", self.steps[step_name]["with"]["script"]
                )

    # --- Close stale sticky issues ----------------------------------------------------------

    def test_a_green_build_comments_on_and_closes_every_open_sticky_issue(self) -> None:
        calls, _ = self.run_step(CLOSE_STEP, issues=[issue(5, "a"), issue(9, None)])
        self.check_listing(calls)
        writes = self.writes(calls)
        self.assertEqual(
            [(name, params["issue_number"]) for name, params in writes],
            [("createComment", 5), ("update", 5), ("createComment", 9), ("update", 9)],
        )
        for name, params in writes:
            self.assertEqual((params["owner"], params["repo"]), ("o", "r"))
            if name == "update":
                self.assertEqual(params["state"], "closed")
            else:
                self.assertIn(RUN["head_sha"], params["body"])

    def test_nothing_is_written_when_no_sticky_issue_is_open(self) -> None:
        calls, _ = self.run_step(CLOSE_STEP)
        self.check_listing(calls)
        self.assertEqual(self.writes(calls), [])


if __name__ == "__main__":
    unittest.main()
