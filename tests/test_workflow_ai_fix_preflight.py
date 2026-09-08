"""
Script: tests/test_workflow_ai_fix_preflight.py
What: Executes the "Decide whether this run can do anything" step of
.github/workflows/ai-fix.yml -- the gate that decides whether an agent holding
`contents: write` starts at all.
Doing: Extracts that one step's `run:` body from the workflow with PyYAML and runs it
under bash with the step's own environment, a stub `gh` on PATH, and real
GITHUB_OUTPUT/GITHUB_STEP_SUMMARY files, then asserts the output and the summary.
Why: The step is 60 lines of shell that no tier ran. Its three refusals -- bot sender,
no credentials, fork pull request -- are the mechanical half of docs/SECURITY-AI.md,
and tests/test_workflow_build_container.py only asserts the file's *static* properties
(which secrets it names, which permissions it grants). A refusal that stopped firing
would leave every one of those assertions green.
Goal: Make a bot-triggered run, an unconfigured repository, or a fork head fail here
rather than by starting an agent that should not have started.

The step's text is executed rather than copied. A renamed or deleted step fails the
extraction loudly instead of leaving this file silently asserting nothing.

Nothing here reaches the network. `gh` is a stub that records its argv and prints a
scripted `.head.repo.full_name`, and PATH deliberately omits the directory holding the
real `gh`, so a step that stopped stubbing out would fail rather than call GitHub.

PyYAML is a pytest dependency and is present in CI (see .github/workflows/test.yml,
which installs pytest, pytest-cov and ruff), but the import is guarded so the suite
still runs under `python3 -m unittest discover -s tests` with nothing installed --
matching tests/test_workflow_build_container.py.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ai-fix.yml"

JOB = "preflight"
STEP_ID = "check"

# The step calls `gh`, so PATH must reach the stub. It must NOT reach the real
# `gh`, which on a developer machine is usually /usr/local/bin -- excluded here
# so an unstubbed call fails instead of authenticating against github.com.
SAFE_PATH = "/usr/bin:/bin"

# A bare `gh` that answers the one query the step makes. `$1..` is recorded so a
# test can assert the API path the step built, which is where ${REPO} and the
# resolved pull request number are actually used.
GH_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "${GH_CALLS}"
printf '%s\\n' "${GH_HEAD_REPO}"
"""


def _preflight_step_body() -> str:
    """
    Return the `run:` body of ai-fix.yml's preflight decision step.

    Located by job and step `id`, not by position: a step added above it should
    not silently move this test onto different shell.
    """

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    if JOB not in jobs:
        raise AssertionError(f"ai-fix.yml has no `{JOB}` job")

    for step in jobs[JOB]["steps"]:
        if step.get("id") == STEP_ID:
            body = step.get("run")
            if not body:
                raise AssertionError(
                    f"ai-fix.yml step `{STEP_ID}` no longer has a `run:` body"
                )
            return body

    raise AssertionError(
        f"ai-fix.yml job `{JOB}` has no step with `id: {STEP_ID}`; this test "
        "executes that step's shell and cannot find it"
    )


class Result:
    """The three things the step produces: its outputs, its summary, its gh calls."""

    def __init__(self, outputs: dict[str, str], summary: str, gh_calls: list[str]):
        self.outputs = outputs
        self.summary = summary
        self.gh_calls = gh_calls


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class AiFixPreflightStepTests(unittest.TestCase):
    """
    What ai-fix.yml's preflight step decides, run as shell.

    Every case runs the workflow's own text. The environment is the step's `env:`
    block with the `${{ }}` expressions resolved the way GitHub would resolve
    them -- an unset secret arrives as the empty string, which is exactly the
    "no credentials" case below.
    """

    def _run(
        self,
        *,
        event: str = "issues",
        sender_type: str = "User",
        sender: str = "danathar",
        api_key: str = "sk-test",
        oauth_token: str = "",
        issue_number: str = "42",
        issue_is_pr: str = "false",
        gh_head_repo: str = "Danathar/zfs-kinoite-complex",
        repo: str = "Danathar/zfs-kinoite-complex",
    ) -> Result:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            stub = bindir / "gh"
            stub.write_text(GH_STUB, encoding="utf-8")
            stub.chmod(0o755)

            output = root / "github_output"
            summary = root / "github_step_summary"
            calls = root / "gh_calls"
            for path in (output, summary, calls):
                path.touch()

            env = {
                "PATH": f"{bindir}:{SAFE_PATH}",
                "GH_TOKEN": "ghs_stub",
                "REPO": repo,
                "EVENT": event,
                "SENDER_TYPE": sender_type,
                "SENDER": sender,
                "API_KEY": api_key,
                "OAUTH_TOKEN": oauth_token,
                "ISSUE_NUMBER": issue_number,
                "ISSUE_IS_PR": issue_is_pr,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "GH_CALLS": str(calls),
                "GH_HEAD_REPO": gh_head_repo,
            }

            completed = subprocess.run(
                ["bash", "-c", _preflight_step_body()],
                env=env,
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                # The step runs under `set -euo pipefail`, so a nonzero exit is a
                # failed workflow run, not a refusal. Show why.
                f"the preflight step exited {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )

            outputs = {}
            for line in output.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    outputs[key] = value

            gh_calls = [
                line
                for line in calls.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return Result(outputs, summary.read_text(encoding="utf-8"), gh_calls)

    # -- the run that should happen ---------------------------------------

    def test_labelled_issue_from_a_human_hands_off(self) -> None:
        """
        The path the workflow exists for: a maintainer labels an issue.

        `run=yes` is the only value the `fix` job's `if:` accepts, so this is the
        assertion that the gate can still open at all.
        """

        result = self._run(event="issues")
        self.assertEqual(result.outputs.get("run"), "yes")
        self.assertIn("Handing off to the agent", result.summary)
        self.assertIn("does not merge", result.summary)

    def test_the_oauth_token_alone_is_enough(self) -> None:
        """
        Either credential configures the repository, not just ANTHROPIC_API_KEY.

        The workflow header promises exactly this, and the action accepts either.
        """

        result = self._run(api_key="", oauth_token="oauth-test")
        self.assertEqual(result.outputs.get("run"), "yes")
        self.assertNotIn("Skipped", result.summary)

    def test_the_summary_always_gets_the_heading(self) -> None:
        """The run summary is headed whether the answer is yes or no."""

        for kwargs in ({}, {"sender_type": "Bot"}):
            with self.subTest(**kwargs):
                self.assertTrue(
                    self._run(**kwargs).summary.startswith("### AI fix\n"),
                    "the step writes its heading before deciding",
                )

    # -- the three refusals -----------------------------------------------

    def test_a_bot_sender_is_refused(self) -> None:
        """
        `danathar-atomic-hive[bot]` labels every ACMM issue it opens.

        Without this branch each of those would start an agent holding
        `contents: write`. The summary names the sender because "skipped" with no
        subject is what makes people stop reading run summaries.

        The sender here is deliberately *not* `danathar-atomic-hive[bot]`: the
        summary's fixed prose names that account, so a step that stopped
        interpolating `${SENDER}` would still satisfy an assertion written with
        it -- a false green that a mutation of the summary line found.
        """

        result = self._run(sender_type="Bot", sender="renovate[bot]")
        self.assertEqual(result.outputs.get("run"), "no")
        self.assertIn("renovate[bot]", result.summary)
        self.assertIn("which is a bot", result.summary)

    def test_no_credentials_is_a_skip_and_not_a_failure(self) -> None:
        """
        Inert, not broken: the step succeeds and says why.

        A red workflow nobody can act on trains people to ignore red workflows --
        the reason the workflow header gives for this branch existing.
        """

        result = self._run(api_key="", oauth_token="")
        self.assertEqual(result.outputs.get("run"), "no")
        self.assertIn("no agent credentials are configured", result.summary)
        self.assertIn("ANTHROPIC_API_KEY", result.summary)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", result.summary)

    def test_a_fork_pull_request_is_refused(self) -> None:
        """
        A fork head lives in another repository and this token cannot push there.

        Refused before the expensive part rather than after it.
        """

        result = self._run(
            event="issue_comment",
            issue_is_pr="true",
            issue_number="77",
            gh_head_repo="stranger/zfs-kinoite-complex",
        )
        self.assertEqual(result.outputs.get("run"), "no")
        self.assertIn("comes from a fork", result.summary)
        self.assertIn("#77", result.summary)

    def test_an_unreadable_head_repository_fails_closed(self) -> None:
        """
        `--jq '.head.repo.full_name // ""'` yields "" for a deleted fork.

        Empty is not this repository, so the run stops. Pinned because the
        alternative -- treating an unanswerable question as "same repo" -- is the
        kind of default that only shows up once it is being exploited.
        """

        result = self._run(
            event="issue_comment", issue_is_pr="true", gh_head_repo=""
        )
        self.assertEqual(result.outputs.get("run"), "no")
        self.assertIn("comes from a fork", result.summary)

    def test_the_bot_check_comes_before_the_credential_check(self) -> None:
        """
        A bot on an unconfigured repository is refused *as a bot*.

        Order matters for the reader of the summary: "no credentials" invites
        someone to add the secret, which would then let the bot start agents.
        """

        result = self._run(sender_type="Bot", sender="some[bot]", api_key="")
        self.assertEqual(result.outputs.get("run"), "no")
        self.assertIn("which is a bot", result.summary)
        self.assertNotIn("no agent credentials", result.summary)

    # -- which pull request, if any, this is about ------------------------

    def test_a_comment_on_a_plain_issue_asks_github_nothing(self) -> None:
        """
        `.issue.pull_request` is absent on a plain issue, so there is no head to check.

        The step must both succeed and reach no further than the local decision:
        the commonest event this workflow sees is a comment on an ordinary issue,
        and asking GitHub for a pull request that does not exist would spend a
        token call to learn nothing.
        """

        result = self._run(event="issue_comment", issue_is_pr="false")
        self.assertEqual(result.outputs.get("run"), "yes")
        self.assertEqual(result.gh_calls, [])

    def test_a_labelled_issue_asks_github_nothing(self) -> None:
        """`issues` never carries a pull request, whatever `.pull_request` says."""

        result = self._run(event="issues", issue_is_pr="true")
        self.assertEqual(result.outputs.get("run"), "yes")
        self.assertEqual(result.gh_calls, [])

    def test_the_head_repository_query_names_this_repo_and_pull_request(self) -> None:
        """
        The fork check is only as good as the API path it builds.

        A query against the wrong repository, or against a hardcoded number,
        would answer for something other than the pull request in hand -- and
        would still look like a working same-repo check.
        """

        result = self._run(
            event="issue_comment",
            issue_is_pr="true",
            issue_number="123",
            repo="Danathar/zfs-kinoite-complex",
        )
        self.assertEqual(result.outputs.get("run"), "yes")
        self.assertEqual(len(result.gh_calls), 1, result.gh_calls)
        self.assertIn(
            "api repos/Danathar/zfs-kinoite-complex/pulls/123", result.gh_calls[0]
        )
        self.assertIn("head.repo.full_name", result.gh_calls[0])


if __name__ == "__main__":
    unittest.main()
