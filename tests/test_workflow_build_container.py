"""
Script: tests/test_workflow_build_container.py
What: Guards how the privileged akmods build container is selected, that branch runs
stay read-only against shared production state, and that only branches may publish
unsigned (and only explicitly).
Doing: Reads the workflow files as text and asserts no run-time override exists and that
every hardcoded container literal matches the checked-in default.
Why: That container runs --privileged, as root, with `/` bind-mounted and a package-write
token, so whoever chooses it controls what the akmods cache contains -- and the final image
installs whatever RPMs that cache provides.
Goal: Make a re-introduced override, or a digest that silently drifts from ci/defaults.json,
fail here rather than in production.

Mostly parses with plain text matching rather than PyYAML: most of these assertions do
not need a real YAML parse, and a substring check is the stronger statement when the
claim is "this string appears nowhere in the file".

One assertion is the exception. The ai-fix branch exclusion is a value inside a trigger,
and the same string appears in a comment a few lines above it, so a substring check would
pass on the comment alone -- exactly the false green this file exists to prevent. That one
parses. PyYAML is a pytest dependency and is present in CI (see .github/workflows/test.yml,
which installs pytest, pytest-cov and ruff), but the import is guarded so the suite still
runs under `python3 -m unittest discover -s tests` with nothing installed.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# The two workflows whose jobs run the privileged akmods build container.
PRIVILEGED_CONTAINER_WORKFLOWS = ("build.yml", "build-branch.yml")

# Captures the rest of the line, not just non-whitespace: a GitHub expression
# like `${{ inputs.x || 'y' }}` contains spaces, and a pattern anchored on \S+
# would simply fail to match it -- silently passing the very case this is meant
# to catch.
CONTAINER_IMAGE_RE = re.compile(r"^\s*image:\s*(?P<image>.+?)\s*$", re.MULTILINE)


def _without_comments(text: str) -> str:
    """
    Return `text` with full-line comments removed.

    Only whole-line comments. A trailing comment after real YAML is left alone,
    because stripping one correctly means knowing whether the `#` is inside a
    quoted scalar -- which is parsing, and parsing is what this avoids.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _default_build_container() -> str:
    defaults = json.loads((REPO_ROOT / "ci" / "defaults.json").read_text(encoding="utf-8"))
    return defaults["DEFAULT_BUILD_CONTAINER_IMAGE"]


class BuildContainerSelectionTests(unittest.TestCase):
    def test_no_workflow_accepts_a_build_container_override(self) -> None:
        # A free-text workflow_dispatch input naming this image let anyone who
        # could dispatch the workflow run arbitrary code --privileged with `/`
        # mounted, then publish a cache that later trusted jobs sign, build
        # from, and promote. It cannot be validated inside the job either:
        # `container:` starts before any step runs, so a guard would execute
        # inside the container it was meant to gate. The only safe form is no
        # override at all.
        for name in sorted(p.name for p in WORKFLOW_DIR.glob("*.yml")):
            text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "build_container_image:",
                text,
                f"{name} declares a build-container override input; "
                "the build container must only be changeable by editing "
                "ci/defaults.json and the workflow literals in a reviewed PR.",
            )

    def test_privileged_container_image_is_never_expression_driven(self) -> None:
        for name in PRIVILEGED_CONTAINER_WORKFLOWS:
            text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            for match in CONTAINER_IMAGE_RE.finditer(text):
                image = match.group("image")
                self.assertNotIn(
                    "${{",
                    image,
                    f"{name} selects a container image from an expression ({image}); "
                    "the privileged akmods container must be a fixed literal.",
                )

    def test_container_literals_match_the_checked_in_default(self) -> None:
        # A job's `container:` block cannot read step outputs, so it cannot read
        # ci/defaults.json and the literal is kept in sync by hand. Nothing
        # enforced that until this test: a drifted literal would mean the job
        # runs one image while the build-inputs manifest records another.
        expected = _default_build_container()
        self.assertIn("@sha256:", expected, "the default build container must be digest-pinned")

        for name in PRIVILEGED_CONTAINER_WORKFLOWS:
            text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            devcontainer_images = [
                match.group("image")
                for match in CONTAINER_IMAGE_RE.finditer(text)
                if "devcontainer" in match.group("image")
            ]
            self.assertTrue(
                devcontainer_images,
                f"{name} no longer names the akmods build container; update this test "
                "if that job legitimately moved or was removed.",
            )
            for image in devcontainer_images:
                self.assertEqual(
                    image.strip("'\""),
                    expected,
                    f"{name} runs {image}, but ci/defaults.json says {expected}. "
                    "These must stay identical.",
                )


class BranchIsolationTests(unittest.TestCase):
    """
    Branch runs must be read-only against shared production state.

    The production signing key is scoped to a main-only environment, so branch
    refs cannot sign. Everything here pins the consequences: the branch
    workflow never references the secret at all, never mutates the shared
    akmods cache, and publishes only explicitly-unsigned test images. build.yml
    must never take the unsigned path.
    """

    def _branch_workflow(self) -> str:
        return (WORKFLOW_DIR / "build-branch.yml").read_text(encoding="utf-8")

    def test_branch_workflow_never_references_the_signing_secret(self) -> None:
        # The strongest form of "branches cannot sign": not a guard around the
        # secret, but no reference to it anywhere in the file. A branch push
        # cannot exfiltrate or use a secret its workflow never requests.
        self.assertNotIn("SIGNING_SECRET", self._branch_workflow())

    def test_branch_cache_refresh_is_denied_for_everyone(self) -> None:
        text = self._branch_workflow()
        self.assertIn(
            'allow_cache_rebuild: "false"',
            text,
            "build-branch.yml must deny shared akmods cache republishing to ALL branch "
            "runs; without a signing key a branch rebuild publishes an unsigned cache "
            "that every later run rejects, and a compromised write credential could "
            "replace the signed cache at will.",
        )

    def test_branch_workflow_has_no_cache_signing_job(self) -> None:
        # The job cannot work without the key; leaving it in place would fail
        # loudly on every human cache rebuild with a misleading message.
        self.assertNotIn("sign-branch-akmods-cache", self._branch_workflow())

    def test_branch_publish_is_explicitly_unsigned(self) -> None:
        self.assertIn('allow_unsigned: "true"', self._branch_workflow())

    def test_agent_branches_never_reach_the_branch_publisher(self) -> None:
        """
        An ai-fix/* branch must not reach the branch publisher.

        Without this exclusion, opening a pull request would also publish an
        unsigned br-* tag to the registry -- more than "propose a change" should
        cost. `actor_is_bot` gates the push step as well, but that depends on
        which credential the agent pushed with, which is not this repository's
        property to guarantee. This one does not care who pushed.

        Matched against comment-stripped source: the `on:` block's own comment
        names the pattern, so the raw text would satisfy this even with the
        exclusion deleted.
        """
        branch = _without_comments(self._branch_workflow())
        self.assertIn(
            "- 'ai-fix/**'",
            branch,
            "build-branch.yml must not build or publish agent-authored branches; "
            "see the comment on its `on:` block and docs/SECURITY-AI.md.",
        )
        self.assertIn("- main", branch, "the pre-existing main exclusion must remain")

    def test_build_yml_never_allows_unsigned_publish(self) -> None:
        text = (WORKFLOW_DIR / "build.yml").read_text(encoding="utf-8")
        self.assertNotIn(
            "allow_unsigned",
            text,
            "build.yml must never pass allow_unsigned; the production path's "
            "no-key-no-push guard is the invariant that keeps user-facing tags signed.",
        )

    def test_main_signing_jobs_declare_the_environment(self) -> None:
        # Creating the environment in repository settings gates nothing on its
        # own; the jobs must declare it. (This exact settings-only mistake was
        # caught in review of the runtime-validation proposal.)
        text = (WORKFLOW_DIR / "build.yml").read_text(encoding="utf-8")
        for job in ("build-candidate-image:", "sign-akmods-cache:"):
            block = text.split(job, 1)[1]
            block = re.split(r"\n  [a-z][a-z0-9-]*:\n", block)[0]
            self.assertIn(
                "environment: production-signing",
                block.split("\n    steps:")[0],
                f"{job.rstrip(':')} must declare the production-signing environment; "
                "without it the secret stays reachable from any ref once the "
                "repository-level copy is removed, or signing silently breaks.",
            )

    def test_publish_action_fails_closed_by_default(self) -> None:
        action = (
            REPO_ROOT / ".github" / "actions" / "publish-native-image" / "action.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('default: "false"', action)
        self.assertIn(
            "inputs.cosign_private_key == '' && inputs.allow_unsigned != 'true'",
            action,
            "publish-native-image must refuse to push when there is no key unless the "
            "caller explicitly opted into unsigned publication.",
        )

    def test_prepare_action_supports_denying_cache_rebuild(self) -> None:
        action = (
            REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_cache_rebuild:", action)
        # The guard must fail the run rather than silently skipping the rebuild,
        # which would leave the caller believing a usable cache exists.
        self.assertIn("Refuse to refresh the shared akmods cache", action)


class AgentWorkflowIsolationTests(unittest.TestCase):
    """
    What .github/workflows/ai-fix.yml is allowed to reach.

    That workflow hands write access to an agent started from a label or a
    comment. docs/SECURITY-AI.md sets out what it may and may not do; these
    assertions are the mechanical half, so a later edit that quietly widens it
    fails here rather than in a registry.
    """

    def _agent_workflow(self) -> str:
        return (WORKFLOW_DIR / "ai-fix.yml").read_text(encoding="utf-8")

    def test_agent_workflow_never_references_the_signing_secret(self) -> None:
        # `secrets.SIGNING_SECRET`, not the bare name: the workflow's own header
        # explains why it has no access to the key, so the bare string appears
        # in a comment and always will.
        self.assertNotIn("secrets.SIGNING_SECRET", self._agent_workflow())

    def test_agent_workflow_cannot_push_an_image(self) -> None:
        """
        `packages: write` is what a GHCR push needs.

        Its absence is why `contents: write` here is a bounded grant rather than
        an open one. Checked against the comment-stripped source: the workflow
        header explains why the permission is excluded, so the literal string is
        in the file and always will be. A substring check over the raw text
        passes on that comment while the job quietly holds the permission --
        the first draft of this assertion failed on a clean tree for exactly
        that reason.
        """
        self.assertNotIn("packages:", _without_comments(self._agent_workflow()))

    def test_agent_workflow_refuses_bot_senders(self) -> None:
        # danathar-atomic-hive[bot] applies `ai-fix-requested` to every ACMM
        # issue it opens. Without this, an external system starts agents here on
        # its own schedule. See docs/SECURITY-AI.md, "Inputs to treat as
        # untrusted".
        self.assertIn("allowed_bots: ''", _without_comments(self._agent_workflow()))

    def test_agent_workflow_is_not_triggered_from_a_head_ref(self) -> None:
        # `issues` and `issue_comment` run the default branch's copy of the
        # workflow. The pull_request_review family runs the *head's* copy, so a
        # job holding `contents: write` would be reachable by adding a trigger
        # on a branch -- a real escalation, not a theoretical one.
        text = _without_comments(self._agent_workflow())
        for trigger in ("pull_request_review:", "pull_request_review_comment:", "pull_request:"):
            with self.subTest(trigger=trigger):
                self.assertNotIn(trigger, text)

    def test_agent_branch_prefix_matches_the_branch_publisher_exclusion(self) -> None:
        """
        The two halves of "an agent branch publishes nothing" must agree.

        build-branch.yml excludes `ai-fix/**` from its trigger, and ai-fix.yml
        tells the action to push branches under `ai-fix/`. Changing the prefix
        in one file without the other silently restores the behaviour the
        exclusion was added to remove -- an agent branch publishing an unsigned
        br-* tag -- and nothing else in the repository would notice.

        Both sides are matched against comment-stripped source, because both
        files mention the other's pattern in prose.
        """
        agent = _without_comments(self._agent_workflow())
        branch = _without_comments((WORKFLOW_DIR / "build-branch.yml").read_text("utf-8"))
        self.assertIn("branch_prefix: 'ai-fix/'", agent)
        self.assertIn("- 'ai-fix/**'", branch)


if __name__ == "__main__":
    unittest.main()
