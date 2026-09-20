"""
Script: tests/test_workflow_publish_badges.py
What: Tests the shell body of akmods-failure-triage.yml's "Publish badges to status branch" step,
by extracting it from the workflow and running it against a real local git remote.
Doing: Parses the workflow with PyYAML, pulls out that one step's `run:` script, and executes it
under bash with `git` rewritten (url.<path>.insteadOf) to point at a bare repository in a
temporary directory instead of github.com.
Why: Both badge payloads are produced by Python modules that are measured and well tested, but
the step that decides what actually lands on the `status` branch is shell in a workflow file --
which no tier reaches. Its comment states an invariant the shell alone enforces: a run that
refreshes only one badge must not wipe out the other badge's last known-good content. Nothing
asserted that, so a `cp` moved out of its `if`, a `git add -A` against a wiped work tree, or a
lost `git fetch` fallback would silently blank a published badge and no test would fail.
Goal: Make the three outcomes of that step -- create the branch, preserve the badge this run did
not rebuild, and commit nothing when nothing changed -- fail here rather than on the status
branch, where the only symptom is a badge that quietly stops being true.

This runs the workflow's own text, not a copy of it. Copying the script into the test would
assert that the copy works; extracting it means a change to the workflow step is a change to
what these tests execute, and a renamed or deleted step fails loudly in `_publish_step_script`
rather than silently testing nothing.

Everything the step touches is real except the remote: real bash, real git, real commits. The
remote is a bare repository in a temporary directory, reached by rewriting the hardcoded
`https://github.com/<owner>/<repo>.git` URL with git's `url.<base>.insteadOf`. That keeps the
URL construction in the step under test -- a step that built the wrong URL would still fail
here -- while never leaving the machine.

The URL carries no credential (#147): the token reaches git through a `store` credential file
instead, because /proc/<pid>/cmdline is world-readable and a token in argv is readable by every
uid on the runner. `test_the_token_never_reaches_argv` is what holds that -- it puts a
recording `git` shim on PATH and fails if the token appears in any git command line.

PyYAML is not a pytest dependency; CI installs it by name (see .github/workflows/test.yml).
The import is guarded so the suite still runs under `python3 -m unittest discover -s tests`
with nothing installed, matching tests/test_workflow_build_container.py.
"""

from __future__ import annotations

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

PUBLISH_STEP_NAME = "Publish badges to status branch"

# The values the step interpolates. Only their shape matters: the test rewrites this exact URL
# to a local path, so a step that assembled a different one would fail to reach the remote at
# all. FAKE_TOKEN is no longer part of the URL -- it goes in via the credential helper -- but it
# is still handed to the step as GH_TOKEN, which is what lets the argv test look for it.
FAKE_TOKEN = "test-token-not-a-real-credential"
FAKE_REPO = "Danathar/zfs-kinoite-complex"
REMOTE_URL = f"https://github.com/{FAKE_REPO}.git"

AKMODS_BADGE = "akmods-badge.json"
LAST_GOOD_BADGE = "last-good-build-badge.json"


def _publish_step_script() -> str:
    """
    Return the `run:` body of the publish step, as the workflow writes it.

    Raises rather than returning a default when the step is missing: a step that was renamed
    or removed must fail this file, not quietly leave it asserting nothing.
    """

    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") != PUBLISH_STEP_NAME:
                continue
            if "run" not in step:
                raise AssertionError(
                    f"the {PUBLISH_STEP_NAME!r} step no longer has a `run:` body; "
                    "update this test if that work legitimately moved."
                )
            return step["run"]
    raise AssertionError(
        f"no step named {PUBLISH_STEP_NAME!r} in {WORKFLOW_PATH.name}; "
        "update this test if the step was renamed or removed."
    )


def _git(*args: str, cwd: Path, gitconfig: Path) -> subprocess.CompletedProcess[str]:
    """Run git with only the test's own global config, so a developer's ~/.gitconfig cannot
    change what these tests observe."""

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(gitconfig.parent),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        text=True,
        check=True,
    )


@unittest.skipIf(yaml is None, "PyYAML is not installed")
@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class PublishBadgesToStatusBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _publish_step_script()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

        self.remote = self.root / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)], check=True, capture_output=True
        )

        # The rewrite that keeps the step's own URL construction under test while sending the
        # traffic to a bare repository on disk. `insteadOf` is matched against the URL the step
        # builds, so a step that stopped interpolating REPO -- or that put the credential back
        # into the URL -- would not match, and the fetch and push would fail.
        self.gitconfig = self.root / "gitconfig"
        self.gitconfig.write_text(
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[user]\n"
            "\tname = test\n"
            "\temail = test@example.invalid\n"
            f'[url "{self.remote}"]\n'
            f"\tinsteadOf = {REMOTE_URL}\n",
            encoding="utf-8",
        )

        self.workspace = self.root / "workspace"
        (self.workspace / "artifacts").mkdir(parents=True)

    # -- helpers -------------------------------------------------------------

    def _write_artifacts(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            (self.workspace / "artifacts" / name).write_text(content, encoding="utf-8")

    def _seed_status_branch(self, files: dict[str, str]) -> None:
        """Publish `files` on the remote's `status` branch, as an earlier run would have."""

        seed = self.root / "seed"
        seed.mkdir()
        _git("init", "-q", cwd=seed, gitconfig=self.gitconfig)
        for name, content in files.items():
            (seed / name).write_text(content, encoding="utf-8")
        _git("add", "-A", cwd=seed, gitconfig=self.gitconfig)
        _git("commit", "-q", "-m", "seed", cwd=seed, gitconfig=self.gitconfig)
        _git("push", "-q", REMOTE_URL, "HEAD:status", cwd=seed, gitconfig=self.gitconfig)

    def _run_step(self, path_prefix: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.workspace,
            env={
                "PATH": path_prefix + "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(self.root),
                "TMPDIR": str(self.root),
                "GIT_CONFIG_GLOBAL": str(self.gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GH_TOKEN": FAKE_TOKEN,
                "REPO": FAKE_REPO,
                "GITHUB_WORKSPACE": str(self.workspace),
            },
            capture_output=True,
            text=True,
            # Not check=True: the step's exit status is one of the things under test, and a
            # raised CalledProcessError would hide the stdout that says why it failed.
            check=False,
        )

    def _status_branch_contents(self) -> dict[str, str]:
        listing = _git(
            "ls-tree", "-r", "--name-only", "status", cwd=self.remote, gitconfig=self.gitconfig
        )
        contents = {}
        for name in listing.stdout.split():
            blob = _git("show", f"status:{name}", cwd=self.remote, gitconfig=self.gitconfig)
            contents[name] = blob.stdout
        return contents

    def _status_branch_sha(self) -> str:
        return _git(
            "rev-parse", "status", cwd=self.remote, gitconfig=self.gitconfig
        ).stdout.strip()

    def _assert_succeeded(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            f"publish step failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    # -- tests ---------------------------------------------------------------

    def test_creates_the_status_branch_when_the_repository_has_none(self) -> None:
        # First ever run: `git fetch origin status` finds nothing, so the step has to fall back
        # to an orphan branch. Losing that fallback would leave every badge unpublished until
        # someone created the branch by hand, and the step would fail on a repository that has
        # never published a badge -- the one case nobody re-tests.
        self._write_artifacts({AKMODS_BADGE: '{"message": "ok"}\n', LAST_GOOD_BADGE: '{"d": 1}\n'})

        self._assert_succeeded(self._run_step())

        self.assertEqual(
            self._status_branch_contents(),
            {AKMODS_BADGE: '{"message": "ok"}\n', LAST_GOOD_BADGE: '{"d": 1}\n'},
        )

    def test_a_run_that_rebuilt_one_badge_preserves_the_other(self) -> None:
        # The invariant the step's own comment states. Both badges are published to one branch,
        # but a given run only writes the artifacts it rebuilt: the badge step is conditional on
        # this run's conclusion, while last-good-build advances on every run. Copying
        # unconditionally, or committing a work tree that only ever contained this run's files,
        # would blank whichever badge this run had nothing to say about.
        self._seed_status_branch(
            {AKMODS_BADGE: '{"message": "old"}\n', LAST_GOOD_BADGE: '{"days": 3}\n'}
        )
        self._write_artifacts({LAST_GOOD_BADGE: '{"days": 4}\n'})

        self._assert_succeeded(self._run_step())

        self.assertEqual(
            self._status_branch_contents(),
            {
                # Not rebuilt this run, so its last known-good content must survive.
                AKMODS_BADGE: '{"message": "old"}\n',
                LAST_GOOD_BADGE: '{"days": 4}\n',
            },
        )

    def test_updates_the_badge_this_run_rebuilt_on_an_existing_branch(self) -> None:
        # The fetch path, as distinct from the orphan path above: the branch already exists, so
        # the step must check it out and commit on top of it rather than start a new history.
        self._seed_status_branch({AKMODS_BADGE: '{"message": "old"}\n'})
        before = self._status_branch_sha()
        self._write_artifacts({AKMODS_BADGE: '{"message": "new"}\n'})

        self._assert_succeeded(self._run_step())

        self.assertEqual(self._status_branch_contents(), {AKMODS_BADGE: '{"message": "new"}\n'})
        parents = _git(
            "rev-list", "--count", "status", cwd=self.remote, gitconfig=self.gitconfig
        ).stdout.strip()
        self.assertEqual(
            parents,
            "2",
            "the update must build on the existing status history, not replace it",
        )
        self.assertNotEqual(before, self._status_branch_sha())

    def test_commits_nothing_when_the_badge_content_is_unchanged(self) -> None:
        # Every triage run reaches this step whenever either badge reports `updated`, and the
        # other badge's file is copied over identical content. Without the `git diff --cached
        # --quiet` guard the status branch would collect an empty commit per run forever.
        unchanged = '{"message": "same"}\n'
        self._seed_status_branch({AKMODS_BADGE: unchanged})
        before = self._status_branch_sha()
        self._write_artifacts({AKMODS_BADGE: unchanged})

        result = self._run_step()

        self._assert_succeeded(result)
        self.assertIn("Badge content unchanged; nothing to commit.", result.stdout)
        self.assertEqual(before, self._status_branch_sha())

    def test_commits_as_the_actions_bot_rather_than_the_runner_default(self) -> None:
        # The step sets an identity because a bare runner has none: `git commit` would abort
        # with "Please tell me who you are" and the badge would never publish. Asserting the
        # identity rather than just the success keeps these commits attributable to the bot on
        # a branch whose whole audience is people reading it after the fact.
        self._write_artifacts({AKMODS_BADGE: '{"message": "ok"}\n'})

        self._assert_succeeded(self._run_step())

        author = _git(
            "log", "-1", "--format=%an <%ae>", "status", cwd=self.remote, gitconfig=self.gitconfig
        ).stdout.strip()
        self.assertEqual(
            author,
            "github-actions[bot] <github-actions[bot]@users.noreply.github.com>",
        )

    def test_the_token_never_reaches_argv(self) -> None:
        # The reason this step was rewritten (#147): /proc/<pid>/cmdline is mode 0444, so a
        # token on a command line is readable by every uid on the runner for as long as that
        # child lives -- and this job's token can push branches and write issues. The step now
        # hands git a `store` credential file and a credential-free URL, and the only way to
        # hold that is to look at what was actually in argv: a shim first on PATH records every
        # git command line before exec'ing the real git. A future edit that puts the token back
        # into the remote URL passes every other test in this file and fails this one.
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        argv_log = self.root / "git-argv.log"
        real_git = shutil.which("git")
        shim = bin_dir / "git"
        shim.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> "{argv_log}"\n'
            f'exec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)

        self._write_artifacts({AKMODS_BADGE: '{"message": "ok"}\n'})
        result = self._run_step(path_prefix=f"{bin_dir}:")
        self._assert_succeeded(result)

        recorded = argv_log.read_text(encoding="utf-8")
        self.assertIn("remote add origin", recorded, "the shim recorded no git calls at all")
        self.assertNotIn(
            FAKE_TOKEN,
            recorded,
            "the token appeared in a git command line:\n" + recorded,
        )
        self.assertIn(
            "credential.helper",
            recorded,
            "the step no longer configures a credential helper, so the token has no way in",
        )

    def test_the_credential_file_is_removed_when_the_step_exits(self) -> None:
        # The trap is the difference between a token that lives for one step and one left on
        # the runner's disk for whatever runs next. TMPDIR is the test's own directory, so
        # everything the step created is under self.root and can simply be searched.
        self._write_artifacts({AKMODS_BADGE: '{"message": "ok"}\n'})

        self._assert_succeeded(self._run_step())

        leaked = [
            path
            for path in self.root.rglob("*")
            if path.is_file() and FAKE_TOKEN.encode() in path.read_bytes()
        ]
        self.assertEqual(leaked, [], f"token still on disk after the step: {leaked}")


if __name__ == "__main__":
    unittest.main()
