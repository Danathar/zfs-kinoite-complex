"""
Script: tests/test_git_diff_gate.py
What: Runs `.claude/hooks/gate-git-diff.sh`, the PreToolUse hook that re-gates the allow-listed git commands.
Doing: Feeds the hook real tool payloads and asserts exit 2 for the spellings that read or write an arbitrary file, and exit 0 for the ordinary ones.
Why: The hook is the only control standing between an unprompted `git diff` and this repository's secret-shaped paths, and every refusal in it is one a simpler check would have missed.
Goal: Make a weakened operand scan fail here, rather than the first time a signing key is printed by a pre-approved command.

`.claude/settings.json` allows `git diff`, `git log`, `git show` and
`git blame` with no prompt, and denies the *Read tool* this repository's
secret-shaped paths. Those are different tools, so the deny rows say nothing
about what the allowed commands open or write. The hook closes that, and the
claims it makes are each a separate way for it to silently stop working:

  * the two-operand read mode needs no flag, survives a leading `--`, and
    counts a lone `-` as its second operand;
  * `--output=FILE` writes, in every git subcommand rather than just `diff`,
    and `--output-indicator-*` is a different flag that must keep working;
  * the shell rewrites quoting and backslashes before git sees the word, so
    matching the typed spelling is not enough;
  * brace expansion rewrites it further -- one word becomes two operands, and
    a flag name split across a brace becomes the flag -- so a brace bash
    would expand is refused inside a git invocation, while a literal one
    (git's own `HEAD@{1}`) and every brace outside git are left alone;
  * an operator character with no whitespace around it still starts a command;
  * a two-token git global option (`-C dir`) must not be read as a subcommand;
  * it fails closed when `jq` is missing, per AGENTS.md section 0 rule 1.

The hook is run as a subprocess against a real checkout, because what it
decides depends on `git rev-parse` and `realpath` answering about this tree.
That is also why the gate is listed as `unmeasured` in
`.coverage-thresholds.json`: it is bash, so Python statement coverage never
sees it, and this file is where it is held instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / ".claude" / "hooks" / "gate-git-diff.sh"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"

BASH = shutil.which("bash")
JQ = shutil.which("jq")


def payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def two_revisions() -> tuple[str, str]:
    """Two operand spellings that resolve as commits in *this* checkout.

    The gate decides `git diff <a> <b>` by asking `git rev-parse` whether each
    operand is a commit, so a case built on revisions this checkout does not
    have tests the clone rather than the hook. `HEAD~1` is the trap: it exists
    in a development clone and does not exist under `actions/checkout`, which
    fetches depth 1 by default, so the gate refuses it there -- correctly, an
    operand that does not resolve as a revision is how a plain-file read is
    spelled. Ask git what history is present instead of assuming any.

    Falls back to naming `HEAD` twice when there is only one commit to name:
    still two operands, still both resolving, which is what the cases below
    are about.
    """
    listed = subprocess.run(
        ["git", "rev-list", "--max-count=2", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    revisions = listed.stdout.split()
    if len(revisions) >= 2:
        return revisions[1], revisions[0]
    return "HEAD", "HEAD"


OLDER_REVISION, NEWER_REVISION = two_revisions()


class GateWiringTests(unittest.TestCase):
    """The join between `.claude/settings.json` and the file it names.

    A hook that is not wired, or wired to a path that does not exist, refuses
    nothing while the enforcement table in `docs/SECURITY-AI.md` goes on
    crediting it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = json.loads(SETTINGS.read_text(encoding="utf-8"))

    def test_the_gate_exists_and_is_executable(self) -> None:
        self.assertTrue(GATE.is_file(), f"{GATE} is missing")
        self.assertTrue(os.access(GATE, os.X_OK), f"{GATE} is not executable")

    def test_a_pretooluse_entry_matches_bash_and_runs_the_gate(self) -> None:
        entries = self.settings["hooks"]["PreToolUse"]
        matching = [
            hook
            for entry in entries
            if entry.get("matcher") == "Bash"
            for hook in entry["hooks"]
            if hook["type"] == "command" and "gate-git-diff.sh" in hook["command"]
        ]
        self.assertEqual(len(matching), 1, f"expected one Bash gate entry, got {matching}")

    def test_the_commands_the_gate_re_gates_are_still_the_allow_listed_ones(self) -> None:
        # The gate is worth nothing if the rules it stands in front of moved.
        # If one of these leaves the allow list the hook is not wrong, but the
        # reason given for it in `_note_git_diff_gate` and in
        # docs/SECURITY-AI.md is, and that is what this pins.
        allow = self.settings["permissions"]["allow"]
        for rule in ("Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"):
            with self.subTest(rule=rule):
                self.assertIn(rule, allow)


@unittest.skipUnless(BASH and JQ, "the gate is a bash script written in terms of jq")
class GateBehaviourTests(unittest.TestCase):
    """Run the hook. Each case is a command line and the exit code it must produce."""

    def run_gate(self, command: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd or REPO_ROOT))
        return subprocess.run(
            [BASH, str(GATE)],
            input=payload(command),
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(cwd or REPO_ROOT),
            env=env,
            check=False,
        )

    def assertRefused(self, command: str, expected_message: str) -> None:
        result = self.run_gate(command)
        self.assertEqual(
            result.returncode,
            2,
            f"{command!r} was not refused; stderr={result.stderr!r}",
        )
        self.assertIn(expected_message, result.stderr)

    def assertAllowed(self, command: str) -> None:
        result = self.run_gate(command)
        self.assertEqual(
            result.returncode,
            0,
            f"{command!r} was refused; stderr={result.stderr!r}",
        )

    # --- the read primitive ------------------------------------------------

    def test_the_two_operand_form_is_refused_without_any_flag(self) -> None:
        # Git enters --no-index mode on its own once two operands are given and
        # either one is not repository content. Nothing in the command says so.
        for command in (
            "git diff /dev/null ./cosign.key",
            "git diff /dev/null /etc/shadow",
            "git diff ./cosign.pub /etc/passwd",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    def test_the_flag_is_refused_however_the_shell_would_spell_it(self) -> None:
        for command in (
            "git diff --no-index /etc/passwd /dev/null",
            "git diff --no-'index' /etc/passwd /dev/null",
            "git diff --no-\\index /etc/passwd /dev/null",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    def test_a_brace_that_would_become_two_operands_is_refused(self) -> None:
        # Bash expands braces before it splits words, so `{a,b}` is one word to
        # a scan working on the typed string and two operands to git. Without
        # the refusal the operand count never reaches 2 and the plain-file read
        # goes through unexamined.
        for command in (
            "git diff {/dev/null,./cosign.key}",
            "git diff -- {/dev/null,./LICENSE}",
            "git diff /dev/nul{l,l} ./cosign.key",
            "git diff --no-inde{x,x} /dev/null ./LICENSE",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "expands braces")

    def test_a_leading_dashdash_does_not_end_the_mode(self) -> None:
        # git consumes a leading `--` and applies the same two-operand test to
        # what follows, so this prints the file.
        self.assertRefused("git diff -- /dev/null /etc/shadow", "--no-index mode")

    def test_a_lone_dash_counts_as_the_second_operand(self) -> None:
        # `-` is stdin, not an option. A scan that skips every dash-prefixed
        # word counts one operand here and never reaches the refusal.
        for command in (
            "git diff /etc/shadow -",
            "git diff - /etc/shadow",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    def test_a_path_that_climbs_out_and_back_in_is_still_outside(self) -> None:
        # git's own inside-the-repo test works on the spelling, so a `..` that
        # leaves the checkout and returns reads as outside to it too -- which
        # is how a denied path *inside* this repository is reached after a
        # bare `--`. Built from the checkout's own name rather than hard-coded,
        # because the directory a clone lands in is not fixed.
        climbed = f"../{REPO_ROOT.name}/cosign.pub"
        for command in (
            # Both operands land inside the checkout once `..` is folded, so a
            # gate that resolves before comparing sees nothing wrong -- and git
            # prints the file, because its own test read the spelling.
            f"git diff -- {climbed} -",
            f"git diff -- {climbed} ./LICENSE",
            f"git diff -- /dev/null {climbed}",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    def test_the_pathspec_forms_a_climb_must_not_break(self) -> None:
        # The other side of the same test: a plain relative path after `--` is
        # a pathspec git resolves against the repository, and refusing those
        # would break the ordinary `git diff -- <two files>` review command.
        self.assertAllowed("git diff -- cosign.pub LICENSE")

    def test_an_operator_with_no_whitespace_still_starts_a_command(self) -> None:
        # `ls&&git diff ...` tokenizes as `ls&&git` on a whitespace split, so
        # without the operator-splitting step the word `git` never appears.
        for command in (
            "ls&&git diff /dev/null /etc/shadow",
            "true;git diff /dev/null /etc/shadow",
            "(git diff /dev/null /etc/shadow)",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    # --- the write primitive ----------------------------------------------

    def test_output_is_refused_in_every_subcommand_that_reaches_it(self) -> None:
        for command in (
            "git diff --output=cosign.pub HEAD~1 HEAD",
            "git log -p --output=cosign.pub -1",
            "git show --output=.claude/settings.json HEAD",
            "git log --output cosign.pub -1",
            "git diff --output /home/user/.ssh/authorized_keys",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--output=FILE")

    def test_output_is_refused_after_an_operator_inside_an_argument(self) -> None:
        # Splitting on operator characters ends the operand scan mid-argument;
        # `in_git` deliberately latches so the write primitive stays watched.
        self.assertRefused("git log --grep=a|b --output=cosign.pub -1", "--output=FILE")

    def test_output_is_refused_in_a_second_command_in_the_same_string(self) -> None:
        self.assertRefused("git log -1 && git log -p --output=cosign.pub -1", "--output=FILE")

    def test_a_brace_that_would_rebuild_the_output_flag_is_refused(self) -> None:
        # The flag name split by a brace matches neither `--output` nor
        # `--output=*`, and arrives at git as `--output=FILE --output=FILE`.
        # Every allow-listed subcommand that reaches the diff machinery carries
        # it, so each is named here rather than `diff` alone.
        for command in (
            "git log -p --outpu{t,t}=cosign.pub -1",
            "git show --outpu{t,t}=.claude/settings.json HEAD",
            f"git diff --outpu{{t,t}}=cosign.pub {OLDER_REVISION} {NEWER_REVISION}",
            "git log --{output,output} cosign.pub -1",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "expands braces")

    def test_a_brace_bash_would_not_expand_is_left_alone(self) -> None:
        # Bash expands a brace only when a comma or a `..` range sits inside
        # it; any other brace is a literal, and git's own `@{...}` revision
        # syntax is spelled with exactly that. `git diff HEAD@{1}` is the
        # ordinary diff against the previous commit and touches neither
        # primitive, so a gate that refused it was a false positive with a
        # real cost. One operand each, so nothing here depends on the reflog
        # this checkout happens to have; the last case pins that a `{` which
        # never closes is a literal too.
        for command in (
            "git diff HEAD@{1}",
            "git diff HEAD@{1} -- docs/SECURITY-AI.md",
            "git log main@{upstream} -1",
            "git rev-parse @{-1}",
            "git log @{2.days.ago} -1",
            "git log HEAD@{1 -1",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_the_brace_test_is_what_bash_would_expand_not_the_spelling(self) -> None:
        # The line is drawn where bash draws it, and errs toward refusing.
        # `@{1,2}` reads as revision syntax and is two words to bash; `{x..x}`
        # is a one-element sequence that rebuilds the flag; a comma nested one
        # level down still expands (`{{a,b}}` is `{a} {b}`); and `${VAR}` is
        # a runtime-built argument the hook cannot inspect, refused as before.
        # Then the two spellings the sibling ports were found to pass: bash
        # pairs a `{` with the last `}` it can, so `{a},b}` expands to `a}`
        # and `b}` and a depth counter that closed at the first `}` never saw
        # the comma; and a quoted `;` inside the brace is part of the word
        # bash expands, while the hook's operator split cut the word in two
        # before the brace test saw it. Last, a `..` between two reflog
        # entries has the refused shape and is refused, though bash would
        # leave it alone; the message names the spelling to use.
        for command in (
            "git diff HEAD@{1,2}",
            "git diff --no-inde{x..x} /dev/null ./LICENSE",
            "git diff {{/dev/null,./cosign.key}}",
            "git diff ${SECRET} HEAD",
            "git diff {a},b} /dev/null ./cosign.key",
            "git log {--format=%h},--output=cosign.pub} -1",
            "git diff {/tmp/reference';',./cosign.key}",
            "git log -p --outpu{t,'t '}=cosign.pub -1",
            "git log HEAD@{2}..HEAD@{1}",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "expands braces")
        self.assertRefused("git log HEAD@{2}..HEAD@{1}", "HEAD~2..HEAD~1")

    # Git's revision syntax. Bash leaves each of these alone and the hook
    # must too; `HEAD@{1` pins that an unclosed brace is a literal as well.
    LITERAL_BRACE_WORDS = (
        "HEAD@{1}",
        "main@{upstream}",
        "@{-1}",
        "@{2.days.ago}",
        "HEAD@{1",
    )

    # The brace rule's corpus: the literal set, the ordinary expansions, the
    # two bypasses (mismatched braces, a quoted operator inside the brace),
    # quoted and escaped commas, nesting, ranges, `${VAR}`, mismatched forms
    # in both directions, braces after --output, and quoted jq/awk programs
    # that bash leaves alone. Each word is inserted verbatim into a bash
    # script, so the quoting is bash's.
    BRACE_CORPUS = LITERAL_BRACE_WORDS + (
        "HEAD@{2}..HEAD@{1}",
        "{a,b}",
        "{1..3}",
        "x{1..3}y",
        "a{,b}",
        "{{a,b}}",
        "--no-inde{x,x}",
        "--outpu{t,t}=FILE",
        "HEAD@{1,2}",
        "{--src-prefix=x},--no-index}",
        "{a},b}",
        "{/tmp/reference';',./cosign.key}",
        '{a",",b}',
        "{a\\,b,c}",
        '"{a,b}"',
        "'{a,b}'",
        "{a,b",
        "{a,b}}",
        "{{a,b}",
        "${OPERANDS}",
        "--output={a,b}",
        "--output=x{,}",
        "'{print $1}'",
        "'{a:1}'",
        "'{a: .x, b: .y}'",
    )

    @staticmethod
    def bash_expands(word: str) -> bool:
        """Whether bash turns `word` into more than one word.

        The word is inserted verbatim into the script text on purpose: the
        corpus is this file's, and the point is to hand bash the spelling an
        agent would type. `OPERANDS` is set so that `${OPERANDS}` splits into
        two words the way a runtime-built argument would.
        """
        result = subprocess.run(
            [BASH, "--norc", "--noprofile", "-c", 'printf "%s\\0" ' + word],
            capture_output=True,
            env={"PATH": os.environ.get("PATH", ""), "OPERANDS": "/dev/null ./cosign.key"},
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"bash could not run {word!r}: {result.stderr!r}")
        return result.stdout.count(b"\0") > 1

    def test_the_brace_rule_against_bash_rather_than_a_label(self) -> None:
        # Bash is the ground truth. Every corpus word bash expands must be
        # refused, and every word of the literal set must be allowed. A word
        # in neither class is only held to the first rule, so an over-refusal
        # there is not a failure. The counts keep the check from going
        # vacuous if the corpus shrinks or bash reads it differently.
        self.assertGreaterEqual(len(self.BRACE_CORPUS), 25)
        self.assertEqual(len(set(self.BRACE_CORPUS)), len(self.BRACE_CORPUS))
        expanding = 0
        for word in self.BRACE_CORPUS:
            with self.subTest(word=word):
                if not self.bash_expands(word):
                    continue
                expanding += 1
                self.assertRefused(f"git diff {word}", "expands braces")
        self.assertGreaterEqual(expanding, 15)
        for word in self.LITERAL_BRACE_WORDS:
            with self.subTest(word=word):
                self.assertFalse(self.bash_expands(word), f"bash expands {word!r}")
                self.assertAllowed(f"git log {word} -1")

    def test_a_brace_outside_a_git_invocation_is_left_alone(self) -> None:
        # The refusal is scoped to the words of a git invocation, because a
        # brace is ordinary syntax everywhere else and a gate that refused it
        # wholesale would break the commands an agent runs all day. `in_git`
        # latches once seen, so these carry no `git` anywhere in the string.
        for command in (
            "awk '{print $1}' /dev/null",
            "jq '{ref: .ref}' ci/inputs.lock.json",
            "jq '{a: .x, b: .y}' ci/inputs.lock.json",
            "cp cosign.pub{,.bak}",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_the_output_indicator_flags_are_a_different_flag(self) -> None:
        # Anchoring matters: these change the marker character, not the
        # destination, and refusing them would be a false positive that trains
        # the reader to ignore the gate.
        for command in (
            "git log --output-indicator-new=% -1",
            f"git diff --output-indicator-old=- {OLDER_REVISION} {NEWER_REVISION}",
            f"git diff --output-indicator-frag=@ {OLDER_REVISION} {NEWER_REVISION}",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    # --- what must keep working -------------------------------------------

    def test_the_ordinary_forms_are_not_refused(self) -> None:
        for command in (
            "git diff",
            "git diff --stat",
            f"git diff {OLDER_REVISION} {NEWER_REVISION}",
            "git diff HEAD -- docs/SECURITY-AI.md",
            "git diff -- docs/SECURITY-AI.md",
            f"git diff {OLDER_REVISION} {NEWER_REVISION} -- tests/",
            "git log --oneline -5",
            "git show HEAD",
            "git status",
            "ls -l cosign.pub",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_a_non_git_command_is_left_alone(self) -> None:
        # The gate re-gates git. A `diff` that is not git's is somebody else's
        # command and is decided by the permission rules, not here.
        for command in (
            "diff /dev/null /etc/shadow",
            "python3 tests/run_tests.py",
        ):
            with self.subTest(command=command):
                self.assertAllowed(command)

    # --- the scan's own edges ---------------------------------------------

    def test_a_two_token_git_global_option_does_not_hide_the_subcommand(self) -> None:
        # Without skipping the value half, `/` is read as the subcommand, git
        # is forgotten, and the operand scan never starts.
        for command in (
            "git -C / diff /dev/null /etc/shadow",
            "git -c core.pager=cat diff /dev/null /etc/shadow",
            "git --git-dir=/tmp/x diff /dev/null /etc/shadow",
        ):
            with self.subTest(command=command):
                self.assertRefused(command, "--no-index mode")

    def test_an_operand_that_is_not_a_revision_here_is_refused(self) -> None:
        # The test is "does this checkout resolve the word as a commit", not
        # "does the word look like a revision spelling". A name git cannot
        # resolve is exactly how the plain-file mode is entered, so the gate
        # refuses it even when it reads like history -- which is also why a
        # depth-1 clone refuses `git diff HEAD~1 HEAD`, and why the cases
        # above name revisions this checkout actually has.
        self.assertRefused("git diff v0.0.0-not-a-tag HEAD", "--no-index mode")

    def test_an_empty_or_absent_command_is_not_refused(self) -> None:
        for body in ('{"tool_input": {}}', '{"tool_input": {"command": ""}}'):
            with self.subTest(body=body):
                result = subprocess.run(
                    [BASH, str(GATE)],
                    input=body,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=str(REPO_ROOT),
                    env=dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO_ROOT)),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_fails_closed_when_jq_is_missing(self) -> None:
        # AGENTS.md section 0 rule 1: a fail-closed check is not weakened to
        # make something pass. A hook that exits 0 because a dependency is
        # absent has weakened itself by accident.
        bindir = Path(tempfile.mkdtemp(prefix="gate-path-"))
        try:
            for name in ("git", "realpath", "cat", "printf"):
                found = shutil.which(name)
                if found:
                    (bindir / name).symlink_to(found)
            result = subprocess.run(
                [BASH, str(GATE)],
                input=payload("git diff HEAD~1 HEAD"),
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(REPO_ROOT),
                env={"PATH": str(bindir), "CLAUDE_PROJECT_DIR": str(REPO_ROOT)},
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("jq", result.stderr)
        finally:
            shutil.rmtree(bindir, ignore_errors=True)

    def test_it_fails_closed_on_a_payload_it_cannot_parse(self) -> None:
        result = subprocess.run(
            [BASH, str(GATE)],
            input="not json at all",
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO_ROOT)),
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
