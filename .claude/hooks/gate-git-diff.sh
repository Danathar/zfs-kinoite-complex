#!/usr/bin/env bash
# PreToolUse gate on the Bash tool.
#
# `.claude/settings.json` denies the Read tool this repository's secret-shaped
# paths -- the signing key, a .env, any private key -- and allows `git diff`,
# `git log`, `git show` and `git blame` with no prompt. Those are rules on
# different tools: a deny row on Read says nothing about what an allowed Bash
# command opens or writes, and this family of commands carries one primitive of
# each kind.
#
# The read primitive: `git diff` compares its operands as plain files rather
# than as repository content as soon as two operands are given and either one
# is not a revision, so it prints any file this uid can read -- untracked,
# gitignored, or outside the checkout entirely. Reading the signing key with
# the Read tool prompts; the diff form did not.
#
# Seven things this gate has to get right, each of them a spelling that a
# simpler check misses:
#
#   1. The mode has no required flag. `git diff /dev/null ./cosign.key` prints
#      the file with no `--no-index` anywhere in the command, because git
#      enters that mode on its own once two operands are given. Matching the
#      flag string alone sees nothing.
#   2. The shell rewrites the command before git sees it. `--no-'index'` and
#      `--no-\index` both reach git as `--no-index` while a substring test on
#      the typed spelling finds neither.
#   3. `--` does not end the mode. `git diff -- /dev/null ./cosign.key` prints
#      the file: git's own scan (builtin/diff.c, cmd_diff) consumes a leading
#      `--` and then applies the same two-operand test to whatever follows.
#      Only an operand *before* the `--` stops that scan, which is why
#      `git diff HEAD -- path` can never be a plain-file read but
#      `git diff -- a b` can.
#   4. A lone `-` is an operand, not a flag. Git diff reads it as stdin and
#      counts it toward the same two-operand test, so `git diff /etc/shadow -`
#      prints the file. Skipping every dash-prefixed word -- which is right for
#      `--stat`, `-U0` and the rest, since git rejects an unknown one -- leaves
#      the operand count one short of the refusal.
#   5. Git decides inside-or-outside on the spelling, not on where the path
#      lands. `git diff -- ../<checkout>/cosign.key -` names a file inside this
#      repository by a route that leaves it and comes back; git calls that
#      outside and prints the file, while a test that folds `..` first sees a
#      tidy in-tree path and allows it. See `path_inside_worktree` below.
#   6. Bash expands braces before splitting words, so one word here can be two
#      words at git -- `git diff {/dev/null,./cosign.key}` -- and a flag name
#      split by a brace is no flag at all to a matcher working on the typed
#      spelling: `--outpu{t,t}=FILE` reaches git as `--output=FILE`. Both
#      refusals below are rebuilt by four characters. A brace bash would
#      expand is refused inside a git invocation rather than expanded; one
#      it would not -- git's own `HEAD@{1}`, `main@{upstream}` -- is left
#      alone. The brace test reads the words *as typed*, quotes and all:
#      `{a';',b}` is one word to bash and two paths after expansion, and a
#      test run on the quote-stripped words saw `{a` and `,b}` and passed
#      both. See `raw_words`, `brace_would_expand` and `BRACE_MSG`.
#   7. A git invocation ends where bash ends it, and only there. Every
#      unquoted `&` once counted as a command separator, so `git log 2>&1
#      --outpu{t,t}=FILE` closed the brace scope at the `&` of its
#      redirection and `git diff 2>&1 /dev/null ./cosign.key` reset the
#      operand count there; `>|` did the same as a pipe and `<(` as a
#      subshell. The split now reads redirections and process substitution
#      as bash does. See the split below `brace_would_expand`.
#
# The write primitive: `--output=FILE` sends the diff git would have printed to
# a path instead of stdout, so an allow-listed, unprompted call overwrites any
# file this uid can reach -- `cosign.pub`, which is the signature trust anchor
# committed here and copied into the image; `ci/inputs.lock.json`;
# `.claude/settings.json`; this hook; `~/.ssh/authorized_keys`. The deny rules
# are no help, because they gate the Read tool and say nothing about what an
# allowed Bash command writes. The operand scan below cannot see it either: it
# skips every dash-prefixed word, and it stops tracking git at the subcommand,
# while `git log -p --output=FILE` and `git show --output=FILE` are allow-listed
# and reach the same primitive. `git show` refuses the flag only for a
# *combined* diff -- a merge commit -- and writes an ordinary commit's diff in
# full, so "git show rejects --output" is not a reason to leave it out.
#
# The written content is diff-framed rather than byte-clean, which matters less
# than it sounds: the `+` lines carry whatever the caller committed, and for a
# trust anchor or a config file corruption alone is the event. Nothing
# legitimate needs the flag -- diff, log and show print to stdout, which the
# agent already reads -- so the refusal is the whole git invocation rather than
# one subcommand. `--output-indicator-new` and its siblings change the marker
# character rather than the destination and stay permitted.
#
# The shell has its own spelling of the same write, and it is the older one:
# `git diff HEAD >cosign.pub` truncates the file before git starts, and
# `>>`, `>|`, `&>`, `&>>`, `2>err`, `>&file` and `<>file` each open a path
# for writing the same way, wherever in the command they are written --
# `>cosign.pub git diff HEAD` is the same command as `git diff HEAD
# >cosign.pub`. Nothing in the allow rule sees it -- the rule
# matches a `git diff` prefix -- and the operand scan must not, because a
# redirection's target is the shell's word, not git's (counting it refused
# `git diff HEAD 2>&1`). So an output redirection inside a git invocation is
# refused outright, whatever it targets, on the same ground as `--output`:
# these commands print to stdout, and that is what to read. `>&N`, `N>&M`
# and `>&-` name a descriptor rather than a path and are not refused; nor is
# a redirection on some other command of the same string that no allow rule
# covers (`echo x >out; git diff HEAD` is echo's own). An input redirection
# opens nothing for writing and is decided with the reads below.
#
# The read primitive has a second door that needs no operand at all:
# `git blame` takes a file from its options. `--contents FILE` annotates
# FILE's lines in place of the working copy's, so `git blame --contents
# ./cosign.key README.md` prints every line of the key; `-S FILE` reads a
# graft file and reports each line it cannot parse as `error: bad graft
# data: LINE`, which is every line of a key or a .env; and
# `--ignore-revs-file FILE` stops at the first line with `fatal: invalid
# object name: LINE`. blame parses its options with git's parse-options,
# which takes any unambiguous prefix of a long option, so `--con`,
# `--content=` and `--ignore-revs` are those same options; and a short
# option that takes a value takes the rest of its word, so `-wS.env` is
# `-w -S .env`. They are refused in a blame or annotate invocation in each
# of those spellings. `-S` is refused there only: in diff, log and show it
# is the pickaxe, and its value is a search string rather than a path.
#
# The shell spells that read too. `<FILE` puts a file on git's stdin, and
# three allow-listed forms print what they read there: `git blame
# --contents - README.md <.env` prints every line, and `git log --stdin
# <.env` and `git show --stdin <.env` print the first as a bad revision. So
# an input redirection from a path inside a git invocation is refused,
# wherever in the command it is written, except `</dev/null`, which has
# nothing to print. A here-document or here-string (`<<`, `<<<`) carries
# text typed into the command itself and `<&N` duplicates a descriptor;
# neither opens a path, and neither is refused.
#
# The write primitive is not git's alone, and the rest of the allow list
# reaches it two ways. The shell spelling works on every one of them, because
# a rule ending in `:*` matches a command prefix while the redirection is the
# rest of the string: `python3 tests/run_tests.py >cosign.pub` truncates the
# trust anchor before a test is collected, and
# `gh run view 1 --log >.claude/settings.json` overwrites the file holding
# these rules. And `cosign verify`, alone among them, carries the flag
# spelling: `--output-file` is a *persistent* flag on cosign's root command,
# so every subcommand has it, and cosign creates and truncates the path
# before it verifies anything -- `cosign verify --output-file cosign.pub
# --key <key> <ref>` empties the anchor and then exits non-zero on a key it
# could not load. Both are refused for the commands named in
# `GATED_PREFIXES` below, which are the allow rows with a trailing `:*` other
# than git's. The two `ruff` rows are not among them and do not need to be:
# they carry no `:*`, so a redirection makes the string match neither row and
# Claude Code prompts, which is what `_note_ruff` narrowed them for.
#
# So this looks at the operands git would actually receive, and refuses the
# two-operand form unless every operand resolves as a revision -- which is what
# separates `git diff main feature` from `git diff /dev/null ./cosign.key`.
# After a bare `--` no word can be a revision, so there the test is git's own:
# two or more words where any one lies outside the working tree. The write
# primitive needs none of that machinery: `--output` anywhere in a git
# invocation is refused outright.
#
# One more rewrite sits between the typed word and the path git opens: an
# unquoted leading `~` is `$HOME` to bash and a literal `~` to a scan of the
# typed words, and `realpath -m -s` resolved that literal to `<checkout>/~/...`,
# an inside path. So `git diff -- ~/.aws/credentials ~/.bashrc` counted two
# operands, found both inside the working tree, and exited 0, and bash then
# handed git two files from the home directory, which it printed. A word of a
# git invocation that begins with an unquoted `~` (`~/...`, `~user/...`, `~`
# alone) is refused rather than expanded (see `TILDE_MSG`), and
# `path_inside_worktree` counts a leading `~` as outside as well, so the
# operand scan cannot be talked into the same answer by another route. A
# quoted or escaped tilde (`'~/x'`, `\~/x`) is a literal to bash and is not
# refused; nor is a tilde inside a word (`HEAD~1`). The containment test is
# stricter than that on purpose: it never resolves a leading `~` inside the
# tree, quoted or not, so two quoted tildes after a `--` (`git diff -- '~/x'
# '~/y'`) are refused as the plain-file form although bash would hand git two
# literal paths. Nothing here is named `~`, and the alternative is a
# containment test that has to know how each word was quoted.
#
# A program this hook does not step over can still run the words after it,
# or a string it hands to a shell: `setarch x86_64 gh pr view 1 --jq env`,
# `strace -o log git diff ...`, `sh -c '...'`, `script -qc '...'`. No allow
# rule covers them, so the default mode prompts, but bypass and auto modes
# prompt for nothing; so a command such a program would run is read on its
# own and refused when it would be refused standing alone (see the scan
# above `exit 0` at the end of this file).
#
# What it still cannot see, stated rather than implied: a command such a
# program takes from somewhere this hook cannot read -- a file, standard
# input, a variable -- one that changes directory out of the repository
# first, and anything a command reads or writes once it has started. A git argument built at runtime (`git diff $x $y`,
# `$(...)`, a backtick, `$'\x74'`) is no longer waved through -- every `$` and
# backtick in a word of a git invocation is refused, see `EXPAND_MSG` -- but
# that is a refusal, not an inspection. `xargs` is the one runtime reader
# refused by name: the words it reads from standard input (or from `-a FILE`)
# become operands of the command it runs, and Claude Code matches an allow
# row against `xargs <prefix>` as readily as against `<prefix>`, so
# `printf '%s\n' /dev/null ./cosign.key | xargs git diff` is the plain-file
# read of the key with no operand anywhere in the string (see `XARGS_MSG`).
# This re-gates the pre-approved commands that reach past the deny list; it is
# not a sandbox.

set -uo pipefail

refuse() {
  printf '%s\n' "$1" >&2
  exit 2
}

# A PreToolUse hook that runs past its timeout refuses nothing: Claude Code
# gives up on it and runs the command. The settings entry sets no timeout, so
# that is the 60-second default. Reading a command here costs more than its
# length -- every character is taken with `${command_string:i:1}`, and the
# word arrays are walked again by each scan below -- so a long enough string
# outlasts the default: `git status -- a a ...` with 30000 pathspecs and then
# `; git diff /dev/null ./cosign.key`, 60 KB in all, took 61 s to refuse
# under LC_ALL=C and 102 s under C.UTF-8, and the plain-file read ran instead.
# So the reading runs again under `timeout` with a budget well inside that
# default, and running out of it is a refusal like the rest. The re-run is
# marked by an argument rather than the environment, because Claude Code
# passes the hook no arguments and nothing in a command string can add one.
# Without `timeout` on PATH the hook refuses, as it does without jq.
GATE_BUDGET_SECONDS=10
# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
BUDGET_MSG="blocked: this command took longer than ${GATE_BUDGET_SECONDS} seconds to check, so this PreToolUse hook refuses it rather than let Claude Code's own hook timeout run it unchecked -- a hook that overruns its timeout refuses nothing. Reading a command here grows faster than the command does, and about 60 KB of allow-listed words in front of \`git diff /dev/null ./cosign.key\` was enough to outlast the 60-second default. Split the command into shorter ones."
if [[ "${1-}" != --within-budget ]]; then
  command -v timeout >/dev/null 2>&1 ||
    refuse 'blocked: this PreToolUse hook needs timeout (coreutils) to bound how long it reads a command and timeout is not on PATH. A hook that overruns Claude Code'"'"'s hook timeout lets the command through unchecked, so it refuses rather than read without a bound. Install coreutils.'
  timeout --kill-after=2 "${GATE_BUDGET_SECONDS}" "${BASH:-bash}" "${BASH_SOURCE[0]}" --within-budget
  status=$?
  ((status == 124 || status == 137)) && refuse "${BUDGET_MSG}"
  exit "${status}"
fi

# shellcheck disable=SC2016 # the message quotes shell spellings as literal
# text -- $'\x74' and $(...) are what the reader has to see, not what this
# script should expand.
EXPAND_MSG='blocked: bash expands ANSI-C quotes and substitutions before git sees the words, and this gate reads the words as typed, so two characters rebuild both spellings it refuses: `git diff $(...)` and a backtick supply operands the operand scan never saw (the plain-file read), and `--outpu$'"'"'\x74'"'"'=FILE` matches no word here and reaches git as --output=FILE. Expanding them correctly means reimplementing bash inside a hook, so every $ and backtick in a word of a git invocation is refused instead. Write the command out in full. Only words of a git invocation are affected: an awk or jq program elsewhere in the string is not, unless it carries a backtick after a git word.'

DIFF_MSG='blocked: this git diff would compare paths as plain files (git'"'"'s --no-index mode, which needs no flag once two operands are given), so it prints any file on disk -- the signing key, a .env, a private key outside this repository -- past the Read(...) deny rules in .claude/settings.json. Describe such a file with ls -l or wc -c instead.'

OUT_MSG='blocked: git --output=FILE (and the space form) writes this diff or log to the path it names instead of stdout, overwriting any file this uid can reach -- cosign.pub, ci/inputs.lock.json, .claude/settings.json, this hook, ~/.ssh/authorized_keys -- with no Read(...) or Write(...) deny rule in its way. git diff, git log and git show print to stdout; read that instead. --output-indicator-* is a different flag and is unaffected.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
REDIRECT_MSG='blocked: an output redirection (>, >>, >|, &>, &>>, N>, >&FILE, <>) inside a git invocation makes the shell open its target for writing before git runs -- `git diff HEAD >cosign.pub` truncates the trust anchor, and `>> .claude/settings.json` or `2> .claude/hooks/gate-git-diff.sh` reach any file this uid can write -- and the allow rule for git diff, git log and git show sees none of it. These commands print to stdout; read that instead. Descriptor forms (2>&1, >&2, >&-) are not affected, and a redirection on another command of the same string is that command'"'"'s own.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
STDIN_MSG='blocked: an input redirection from a file (<FILE) inside a git invocation hands that file to git on stdin, and allow-listed git commands print what they read there -- `git blame --contents - README.md <.env` prints every line of it, and `git log --stdin <cosign.key` and `git show --stdin <cosign.key` print its first line as a bad revision -- past the Read(...) deny rules in .claude/settings.json, which gate the Read tool and not the shell. Name revisions and paths as arguments instead. </dev/null, a here-document or here-string (<<, <<<) and a descriptor form (<&0) open no file and are not affected.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
BLAME_MSG='blocked: git blame (and annotate) reads a file named by a blame option and prints it -- `--contents FILE` annotates every line of FILE, `-S FILE` reports every line of FILE as bad graft data, and `--ignore-revs-file FILE` reports its first line as an invalid object name -- so `git blame --contents ./cosign.key README.md` prints the signing key past the Read(...) deny rules in .claude/settings.json. blame takes any unambiguous prefix of a long option (--con, --content=, --ignore-revs) and the rest of a short option'"'"'s word as its value (-wS.env), so those spellings are refused too. Blame the working copy of a tracked file, or name revisions to skip with --ignore-rev REV.'

# shellcheck disable=SC2016 # the literal $HOME is what the reader has to see
TILDE_MSG='blocked: an unquoted leading ~ is $HOME to bash and a literal directory inside this checkout to this gate, so the path checked here is not the path git would open: `git diff -- ~/.aws/credentials ~/.bashrc` resolved both operands inside the working tree and printed both files out of the home directory as a plain-file diff, past the Read(...) deny rules in .claude/settings.json. A word of a git invocation that begins with an unquoted ~ (~/..., ~user/..., or ~ alone) is refused rather than expanded. Spell the path out in full, relative to the checkout. A tilde inside a word (HEAD~1) and a quoted or escaped one are literals to bash and are not refused by this rule.'

# shellcheck disable=SC2016 # the literal $(...) and backticks are what the reader has to see
EXTGLOB_SUBST_MSG='blocked: a command substitution (`$(...)`, a backtick) or a process substitution (`<(...)`, `>(...)`) is written inside an extended glob (`@(...)`, `?(...)`, `*(...)`, `+(...)`, `!(...)`). Bash runs that command before it matches the pattern -- with `shopt -s extglob`, `echo x@(a|$(cat cosign.key))` prints the file -- and this gate reads the whole pattern as one word, so it never checks the command inside. Run the command on its own line, outside the pattern, or quote the pattern in single quotes if it is meant literally.'

# shellcheck disable=SC2016 # the literal $G and $(...) are what the reader has to see
CMD_MSG='blocked: the name of a command in this string is not spelled literally -- it is built by an expansion (`$G diff ...`, `$(printf git) diff ...`, a backtick in command position), by a brace (`{,git} diff ...`), or by a glob (`g?t`, `/usr/bin/g[i]t`, and `/usr/bin/g@(i)t` once `shopt -s extglob` is on) -- so neither this gate nor the allow rule that matched the string'"'"'s literal prefix can tell which command bash will run, and `G=git; $G diff /dev/null ./cosign.key` runs the plain-file read this gate exists to refuse. Spell every command name literally, and drop a variable assignment that only exists to build one. After a wrapper such as command, env, exec or timeout the same holds for every word of that command, since the wrapper'"'"'s own options are not modelled here; xargs'"'"'s own options are read, and held to the same test. A literal name after an assignment is not what this rule refuses -- the assignment has a refusal of its own -- and a literal path to git (`/usr/bin/git diff`) is read as git. env -S (--split-string) splits a quoted string into a command this gate never sees and is refused outright.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
WRAPPER_PATH_MSG='blocked: a wrapper written as a path (`./shim/nohup git diff HEAD`, `/tmp/timeout 5 python3 tests/run_tests.py`) runs the file at that path, while Claude Code'"'"'s permission matcher cuts a wrapper'"'"'s path at its last / or \ and matches the allow rule against the words after it -- so a file an agent wrote and named nohup or timeout runs with the approval meant for `git diff HEAD`. An unquoted backslash needs no file: bash reads `/usr/bin\timeout 5 python3 tests/run_tests.py >out` as the missing command /usr/bintimeout, but truncates out before it says so, while the matcher saw timeout. Only the bare name and /usr/bin/NAME or /bin/NAME, typed without a backslash, are stepped over. Write the bare name (`nohup git diff HEAD`).'

# shellcheck disable=SC2016 # the literal ${VAR} is what the reader has to see
BRACE_MSG='blocked: bash expands braces before git sees the words, and this gate reads the words as typed, so a brace rebuilds both spellings it refuses: `git diff {/dev/null,./cosign.key}` passes the operand scan as one word and reaches git as two operands (the plain-file read), and `--outpu{t,t}=FILE` matches no word here and reaches git as --output=FILE. Expanding braces correctly means reimplementing bash inside a hook, so a brace bash could expand -- a { followed, anywhere later in the word, by a comma or a .. and then a }, or a ${VAR} -- is refused instead, and so is a process substitution (`git diff <(...)`), which supplies an operand this gate never saw. Write the command out in full. A brace with neither, such as HEAD@{1} or main@{upstream}, is a literal to bash and is not refused; a .. between two reflog entries (HEAD@{2}..HEAD@{1}) has the refused shape, so write HEAD~2..HEAD~1. Only words of a git invocation are affected: awk and jq programs elsewhere in the string are not.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
GIT_GLOBAL_MSG='blocked: a git global option written before the subcommand reaches the same primitives from outside the part an allow rule matches. `-c diff.external=/tmp/evil` runs that program once per changed path -- the config spelling of GIT_EXTERNAL_DIFF -- and `-c core.sshCommand`, `-c credential.helper` and `-c alias.x=!cmd` are the same shape; `--config-env` names an environment variable to take the value from; `-C <dir>` moves git to another directory, so `git -C /home/<user> diff -- .netrc .profile` printed a file outside this checkout while the containment test below resolved both operands inside it; and `--exec-path` is the value form of GIT_EXEC_PATH, which the environment rule refuses in every other spelling. So -c, -C, --config-env and --exec-path are refused between the name and the subcommand rather than stepped over. --git-dir, --work-tree, --namespace, --super-prefix and --attr-source only rename or relocate what git reports and are still stepped over.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
GLOB_MSG='blocked: bash expands a glob before git sees the words, so one word here is several operands at git and the operand scan never reaches the count it refuses at: `git diff /home/<user>/.ssh/*` is a single word to this gate and two operands to git, which prints the file. "A glob cannot leave the working directory" is no reason to expand it and check the result either, because the Read(...) deny rules this gate stands in front of name paths inside the checkout -- ./cosign.key, ./.env, **/*.pem. So a `*`, `?` or `[` bash would expand in a word of a git invocation is refused rather than expanded, for the reason the brace and $ rules give: expanding correctly means reimplementing bash in a hook. Quote the pathspec (`git diff -- '"'"'*.md'"'"'`), which is git'"'"'s own glob and is matched against repository content rather than the filesystem, or write the paths out.'

# Fail closed. This gate stands in front of the pre-approved commands that can
# read a denied path, so a missing dependency must not quietly disable it:
# AGENTS.md section 0 rule 1 says a fail-closed check is never weakened to make
# something pass, and a hook that lets calls through uninspected when jq is
# absent is exactly that weakening, applied by accident.
command -v jq >/dev/null 2>&1 ||
  refuse 'blocked: this PreToolUse hook needs jq to inspect the command and jq is not on PATH. It gates the pre-approved commands that can read a denied path, so it refuses rather than letting calls through uninspected. Install jq.'

payload="$(cat)"
command_string="$(printf '%s' "${payload}" | jq -r '.tool_input.command // empty')" ||
  refuse 'blocked: this PreToolUse hook could not parse the tool payload as JSON, so it cannot tell whether the call reads a denied path. It refuses rather than letting the call through uninspected.'

[[ -n "${command_string}" ]] || exit 0

# This file, by a path that still names it after the `cd` below: a command a
# program it does not step over would run is read by running this hook on
# that command alone (see `read_alone`).
gate_self="${BASH_SOURCE[0]}"
[[ "${gate_self}" == /* ]] || gate_self="${PWD}/${gate_self}"

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || true

# The shape of every brace expansion bash performs: a `{`, then a `,` or a
# `..` somewhere after it, then a `}` somewhere after that. Bash pairs a `{`
# with the last `}` it can, so `{a},b}` expands (to `a}` and `b}`) and a test
# that closed the brace at the first `}` missed the comma; no nesting or
# matching is tracked here, on purpose, and every refinement toward bash's
# real rule is a chance to disagree with it in some other direction. What
# this never does is call a word literal that bash would rewrite. Git's own
# `HEAD@{1}`, `main@{upstream}`, `@{-1}` and `@{2.days.ago}` have neither
# inside the braces and pass; so does a `{` that never closes, which bash
# leaves alone. `HEAD@{2}..HEAD@{1}` is refused although bash would not
# expand it -- the over-refusal is the safe direction, and BRACE_MSG names
# the `HEAD~2..HEAD~1` spelling. `${VAR}` is refused as well, as a
# runtime-built argument this hook cannot inspect.
#
# It is applied to the word as typed, quotes and backslashes included. A
# quoted comma or operator is still part of the word bash expands --
# `{a",",b}` and `{a';',b}` both become two words -- and a test on the
# quote-stripped spelling, cut at its `;`, saw `{a` and `,b}` and waved both
# through. A fully quoted `"{a,b}"`, which bash leaves alone, is refused as
# the price of that.
brace_would_expand() {
  # shellcheck disable=SC2016 # the literal `${` is what is being looked for
  [[ "$1" == *'${'* || "$1" == *'{'*','*'}'* || "$1" == *'{'*..*'}'* ]]
}

# The command's words as bash would delimit them, split once and read by both
# scans below. Each word is kept in two spellings: as typed, with every quote
# mark and backslash in place, because bash brace-expands exactly that form
# and `brace_would_expand` has to see it; and with the quotes and backslashes
# removed, which is the word git receives and what the operand scan compares.
# Alongside them is what kind of thing each entry is: a `word` of a command, a
# `sep` (an unquoted command separator: `;`, `&`, `&&`, `|`, `||`, `|&`, `(`,
# `)`, a newline, a backtick), or the `target` of a redirection.
#
# The separators are the only things that end a command, and the list has to
# be exactly bash's, in both directions. An earlier version of this split
# treated every unquoted `&` as a separator and reset the git scope at it, so
# `git log 2>&1 --outpu{t,t}=cosign.pub -1` passed: the `&` in `2>&1` closed
# the scope before the brace was seen, then bash expanded the flag and git
# overwrote the file. The same `&` reset the operand scan, and `git diff 2>&1
# /dev/null ./cosign.key` printed the key with neither operand counted. `>&`,
# `<&`, `&>` and `&>>` are redirections, and so is `>|` (the noclobber form),
# where the `|` is not a pipe. `|&` is a pipe and stays a separator. A `(`
# behind an unquoted `<` or `>` is a process substitution rather than a
# subshell: `git diff <(true) ./cosign.key` hands git a `/dev/fd/N` operand
# this scan never counted, so it is refused in a git invocation as an
# expanding brace is (see `raw_in_git` below) instead of resetting the scope
# at its `(`.
#
# A redirection is `[n]op word` -- an optional descriptor number written hard
# against the operator, one of `<`, `>`, `>>`, `<<`, `<<<`, `<>`, `>&`, `<&`,
# `>|`, `&>`, `&>>`, and the target word. None of it is a word git receives:
# the number is dropped, the target is kept as `target` so that the operand
# scan can skip it, and the operator is kept beside the target in
# `redirects`, because which operator it was decides whether the shell opens
# the target for writing (see `redirection_writes_a_path`). `git diff HEAD
# 2>&1` is a one-operand diff; counting `2` and `1` refused it. A heredoc's
# body lines are read as words of the command that opened it, which can only
# over-refuse.
#
# No real word is ever empty in the as-typed spelling: a typed `""` keeps its
# quotes. An unquoted `\` followed by a newline is a line continuation, which
# bash removes before anything else, and it is removed here.
raw_words=()
words=()
kinds=()
redirects=() # the operator, for a `target`; empty for anything else
globs=()     # 1 when the word carries a `*`, `?` or `[` bash would expand
raw_word=''
raw_glob=0
raw_quote=''
raw_escaped=0
redirect_pending=0 # the next word is the target of a redirection
redirect_op=''     # the operator of that redirection, as typed
after_redirect=0   # the previous unquoted character was `<` or `>`
subst_depth=0      # open `$(` substitutions, whose `)` is not a subshell's
extglob_depth=0    # open parentheses of an extended glob in the word being read

# Whether the word ends in an extglob operator (`@`, `+`, `!`, `?`, `*`) that
# bash leaves unquoted, so a `(` right after it opens an extglob pattern. The
# quote state is walked rather than read off the character before the
# operator: an empty quoted prefix (`''@(...)`, `""@(...)`) quotes nothing,
# and the `@` after it is live. Only content characters count; the quote
# marks themselves are not the operator.
word_ends_in_unquoted_extglob_op() {
  local raw="$1" quote='' escaped=0 i ch last='' last_unquoted=0
  for ((i = 0; i < ${#raw}; i++)); do
    ch="${raw:i:1}"
    if ((escaped)); then
      escaped=0
      last="${ch}"
      last_unquoted=0
      continue
    fi
    if [[ -n "${quote}" ]]; then
      if [[ "${ch}" == "${quote}" ]]; then
        quote=''
      elif [[ "${quote}" == '"' && "${ch}" == $'\\' ]]; then
        escaped=1
      else
        last="${ch}"
        last_unquoted=0
      fi
      continue
    fi
    case "${ch}" in
    $'\\') escaped=1 ;;
    "'" | '"') quote="${ch}" ;;
    *)
      last="${ch}"
      last_unquoted=1
      ;;
    esac
  done
  ((last_unquoted)) && [[ "${last}" == [@+!?*] ]]
}

# Whether a word, as typed, opens an extglob pattern: an unquoted `(` right
# after an unquoted `@`, `+`, `!`, `?` or `*`.
word_has_extglob() {
  local raw="$1" k
  [[ "${raw}" == *[@+!?*]'('* ]] || return 1
  for ((k = 1; k < ${#raw}; k++)); do
    [[ "${raw:k:1}" == '(' ]] || continue
    word_ends_in_unquoted_extglob_op "${raw:0:k}" && return 0
  done
  return 1
}

push_word() {
  raw_words+=("${raw_word}")
  words+=("${raw_word//[\'\"\\]/}")
  kinds+=("$1")
  redirects+=("${2-}")
  globs+=("${raw_glob}")
  raw_word=''
  raw_glob=0
}
end_word() {
  [[ -n "${raw_word}" ]] || return 0
  if ((redirect_pending)); then
    push_word target "${redirect_op}"
    redirect_pending=0
    redirect_op=''
  else
    push_word word
  fi
}
push_sep() {
  end_word
  raw_words+=('')
  words+=("$1")
  kinds+=(sep)
  redirects+=('')
  globs+=(0)
  redirect_pending=0
  redirect_op=''
}

for ((i = 0; i < ${#command_string}; i++)); do
  ch="${command_string:i:1}"
  next="${command_string:i+1:1}"
  prev_redirect="${after_redirect}"
  after_redirect=0
  if ((raw_escaped)); then
    raw_escaped=0
    if [[ "${ch}" == $'\n' ]]; then
      raw_word="${raw_word%\\}"
    else
      raw_word+="${ch}"
    fi
    continue
  fi
  # A command inside an extended glob. Bash expands the pattern before it
  # matches it, so `x@(a|$(cat cosign.key))` runs `cat cosign.key`. A `$(` or
  # a backtick there runs unquoted or in double quotes, and a `<(` or `>(`
  # runs unquoted. The reader below keeps the pattern as the letters of one
  # word, so none of these reaches the separators that split its body as a
  # command of its own. They are refused rather than split: a command inside
  # a glob has no use worth a second reader.
  if ((extglob_depth)) && [[ "${raw_quote}" != "'" ]]; then
    # shellcheck disable=SC2016 # the literal `$(` is the text matched
    if [[ "${ch}" == '`' || "${ch}${next}" == '$(' ]] ||
      [[ -z "${raw_quote}" && "${ch}" == [\<\>] && "${next}" == '(' ]]; then
      refuse "${EXTGLOB_SUBST_MSG}"
    fi
  fi
  if [[ -n "${raw_quote}" ]]; then
    raw_word+="${ch}"
    if [[ "${ch}" == "${raw_quote}" ]]; then
      raw_quote=''
    elif [[ "${raw_quote}" == '"' && "${ch}" == $'\\' ]]; then
      raw_escaped=1
    fi
    continue
  fi
  # An extended glob. With `shopt -s extglob` -- one line of the command can
  # set it for the next -- bash reads `@(`, `?(`, `*(`, `+(` and `!(` as the
  # start of a pattern inside the word, up to the matching `)`, spaces and
  # all, so `/usr/bin/g@(i)t` is one word that runs git. Split at the `(`,
  # it was a word ending in `@`, a subshell and a command `t`, and none of
  # them was a name this gate checks. It is kept as one word here and marked
  # as a glob, which every test of a word bash rewrites then reads as the
  # pattern it is. Without extglob the same text is a syntax error and
  # nothing runs, except `!(cmd)` in command position, which is then a
  # negated subshell; that one is refused as a name (write `! (cmd)`).
  if ((extglob_depth)); then
    case "${ch}" in
    $'\\' | "'" | '"') ;;
    *)
      raw_word+="${ch}"
      if [[ "${ch}" == '(' ]]; then
        ((extglob_depth++))
      elif [[ "${ch}" == ')' ]]; then
        ((extglob_depth--))
      fi
      continue
      ;;
    esac
  elif [[ "${ch}" == '(' ]] && word_ends_in_unquoted_extglob_op "${raw_word}"; then
    extglob_depth=1
    raw_glob=1
    raw_word+="${ch}"
    continue
  fi
  case "${ch}" in
  $'\\')
    raw_escaped=1
    raw_word+="${ch}"
    ;;
  "'" | '"')
    raw_quote="${ch}"
    raw_word+="${ch}"
    ;;
  ' ' | $'\t')
    end_word
    ;;
  '<' | '>')
    # `2>` and `10<`: the digits are the descriptor, not a word, and so is
    # bash's `{name}>` form, which allocates a descriptor into the variable.
    if ((!redirect_pending)) && [[ "${raw_word}" =~ ^([0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})$ ]]; then
      raw_word=''
      raw_glob=0
    else
      end_word
    fi
    if [[ "${next}" == '(' ]]; then
      # Process substitution. Kept as a word spelled `<(` or `>(` so the
      # brace scan can refuse it inside a git invocation; its body is a
      # command of its own and is split as one.
      raw_word="${ch}("
      push_word word
      push_sep '('
      ((i++))
      continue
    fi
    # A second `>` or `<` while the target is still to come extends the
    # operator (`>>`, `<<`, `<<<`, `<>`); after a target it opens a new one
    # (`>x>y`), and `end_word` above has already emptied `redirect_op`.
    redirect_op+="${ch}"
    redirect_pending=1
    after_redirect=1
    ;;
  '&')
    if ((prev_redirect)); then
      redirect_op+='&' # `>&` or `<&`: the operator continues and its target follows.
    elif [[ "${next}" == '>' ]]; then
      end_word # `&>` and `&>>`: the `>` that follows opens the redirection.
      redirect_op='&'
    else
      push_sep '&'
    fi
    ;;
  '|')
    if ((prev_redirect)); then
      redirect_op+='|' # `>|`: noclobber redirection, not a pipe.
    else
      push_sep '|'
    fi
    ;;
  $'\n') push_sep ';' ;;
  '(')
    # `$(`: a command substitution, not a subshell. It is a nested command,
    # so it is split as one, but the command around it goes on afterwards:
    # `>$(printf cosign.pub) git diff HEAD` is git's redirection, and a
    # scope that reset at the `(` had forgotten the target by the time it
    # reached `git`. The `$(` and `$)` separators let the scans below save
    # and restore the outer command's state instead of resetting it.
    if [[ "${raw_word}" == *'$' && "${raw_word}" != *'\$' ]]; then
      end_word
      # shellcheck disable=SC2016 # the literal `$(` is the separator's name
      push_sep '$('
      ((subst_depth++))
    else
      push_sep '('
    fi
    ;;
  ')')
    if ((subst_depth > 0)); then
      ((subst_depth--))
      push_sep '$)'
    else
      push_sep ')'
    fi
    ;;
  ';' | '`') push_sep "${ch}" ;;
  '*' | '?' | '[')
    # A pathname expansion. Recorded on the way past, where the quoting is
    # still known: the spellings kept above have had their quotes removed,
    # and `'*.md'` is a literal to bash while `*.md` is however many files
    # match.
    raw_glob=1
    raw_word+="${ch}"
    ;;
  *) raw_word+="${ch}" ;;
  esac
done
end_word

# Every scan below looks for a literal `git` word to open its scope, and the
# allow rules in .claude/settings.json match a literal `git diff`/`git log`
# prefix. Both are blind to a command whose *name* is not that word: in
# `git status; G=git; $G diff /dev/null ./cosign.key` the string is allowed
# on its `git status` prefix, `$G` is not the word `git`, so no scope opens
# and the hook exits 0 -- and bash runs the plain-file read (review on
# aurora-zfs-simple#205, the same hook). `$(printf git) diff ...` and a backtick in command position
# are the same thing spelled differently; so are `{,git} diff ...`, which
# bash brace-expands to `git`, and `g?t` or `/usr/bin/g[i]t`, which pathname
# expansion resolves to it; and so is the plain `/usr/bin/git diff ...`,
# which needs no expansion at all. Whether the permission layer would prompt
# for the second command on its own is not this gate's to assume.
#
# So the word that names each command has to be literal, and a literal path
# to git has to count as git. The name is the first word after a separator
# (or of the string) that is not a variable assignment (`FOO=bar git diff
# HEAD` names git) and not a shell keyword that takes a command (`{`, `!`,
# `if`, `then`, `time`, ...). After a wrapper that runs its arguments
# (`command`, `exec`, `env`, `nohup`, `noglob`, `xargs`, `timeout`, ...) the
# name is somewhere among the words that follow, behind options this gate
# does not model -- `command -- $G` -- so every remaining word of that
# command is held to the test. `noglob` is on that list because Claude Code
# steps over it before matching an allow row, so `noglob python3
# tests/run_tests.py >cosign.pub` matched the runner's row: zsh runs the
# command behind it, and bash, which has no `noglob`, opens the redirection
# before it reports the command missing, so the target is truncated either
# way. Each `xargs` stepped over is recorded as well, for the refusal in
# `check_gated_command` below, and its own options are read rather than
# guessed at (`xargs_word_kind`): the first word after them is the command
# it runs, and the words after that command are its arguments. A wrapper is
# stepped over by its bare name or as `/usr/bin/<name>` or `/bin/<name>`;
# any other path to one is refused (`WRAPPER_PATH_MSG`). A word in that
# position carrying a `$`, a backtick, a `*` or `?`, a `[` (other than the
# `[` and `[[` commands themselves), or a brace bash would expand is
# refused, and so is an unquoted backtick opening there, whose output would
# be the name. A literal name whose last path component is `git` is
# rewritten to `git`, so `/usr/bin/git diff` opens every scope that `git
# diff` does. A redirection's target is never the name. One option of
# another wrapper is modelled, because it is not an option but an
# interpreter: `env -S 'git diff /dev/null ./cosign.key'` (GNU and uutils
# `--split-string`) splits its quoted string into a command this scan never
# sees as words, so any `-S`, clustered (`-iS`) or long, after `env` is
# refused outright. `sh -c ...`, `script -c ...` and `eval` are not
# wrappers here; the scan at the end of this file reads the command string
# each of them carries.
#
# The cost is a backtick assignment (`X=\`date\``): the split ends the word
# `X=` at the backtick, and the backtick then opens in command position. The
# `$(...)` spelling of the same assignment is not affected. A glob or a `$`
# in an argument after a wrapper (`timeout 60 find . -name '*.sh'`) is
# refused too; without the wrapper it is not.
command_word_pending=1 # the next word of this command may be its name
after_wrapper=0        # a wrapper ran: every remaining word may be the name
command_names=()       # 1 at each index that names, or may name, a command
name_assignments=()    # 1 at each assignment this scan skipped before a name
xargs_words=()         # 1 at each xargs this scan stepped over as a wrapper
xargs_opts=0           # reading xargs's own options; its command comes next
xargs_value=0          # the next word is the value of an xargs option
xargs_cmd=0            # this word is the command xargs runs
wrapper_name=''
in_backtick=0
name_stack=() # the outer command's state, while a `$(...)` is being read

# Whether a word in the name position is spelled literally: no `$`, no
# backtick, no glob or extglob pattern (`/usr/bin/g@(i)t` is git once `shopt
# -s extglob` is on) and no brace bash would expand. `[` and `[[` are
# commands, not globs.
word_is_literal() {
  local raw="$1" plain="$2"
  [[ "${raw}" == *'$'* || "${raw}" == *'`'* || "${raw}" == *'*'* || "${raw}" == *'?'* ]] && return 1
  word_has_extglob "${raw}" && return 1
  brace_would_expand "${raw}" && return 1
  [[ "${raw}" == *'['* && "${plain}" != '[' && "${plain}" != '[[' ]] && return 1
  return 0
}

is_wrapper() {
  case "$1" in
  command | builtin | exec | env | nohup | nice | noglob | xargs | timeout | stdbuf | sudo | doas) return 0 ;;
  *) return 1 ;;
  esac
}

# How xargs reads one of its own words, GNU and uutils alike: 0 an option
# complete in itself (`-r`, `-0`, `-n1`, `-I{}`, `--null`, `--max-args=1`),
# 1 an option whose value is the next word (`-a FILE`, `-n 1`, `--arg-file
# FILE`), 2 the `--` that ends its options, 3 its command, and 4 an option
# this gate does not model or whose value the two disagree on (`-e`, `-i`,
# `-l`, a bare `--max-lines`), after which every word is held to the name
# test as after any other wrapper.
xargs_word_kind() {
  case "$1" in
  --) return 2 ;;
  --null | --exit | --interactive | --no-run-if-empty | --verbose | --open-tty | --show-limits) return 0 ;;
  --arg-file=* | --delimiter=* | --max-args=* | --max-chars=* | --max-procs=* | --process-slot-var=* | --eof=* | --replace=* | --max-lines=*) return 0 ;;
  --arg-file | --delimiter | --max-args | --max-chars | --max-procs | --process-slot-var) return 1 ;;
  -?*) ;;
  *) return 3 ;;
  esac
  [[ "$1" =~ ^-[0oprtx]+$ ]] && return 0
  [[ "$1" =~ ^-[0oprtx]*[adEILnPs]$ ]] && return 1
  [[ "$1" =~ ^-[0oprtx]*[adEILnPs]. ]] && return 0
  return 4
}

for ((idx = 0; idx < ${#words[@]}; idx++)); do
  case "${kinds[idx]}" in
  sep)
    # A `$(...)` substitution is a nested command: its own words are held
    # to the rule, and the command around it resumes where it left off, so
    # `>$(printf x) git diff HEAD` still finds its name at `git` and
    # `echo $(date) *.sh` does not read `*.sh` as a name.
    # shellcheck disable=SC2016 # the literal `$(` is the separator's name
    if [[ "${words[idx]}" == '$(' ]]; then
      name_stack+=("${command_word_pending} ${after_wrapper} ${xargs_opts} ${xargs_value} ${xargs_cmd} ${wrapper_name}")
      command_word_pending=1
      after_wrapper=0
      wrapper_name=''
      xargs_opts=0 xargs_value=0 xargs_cmd=0
      continue
    fi
    if [[ "${words[idx]}" == '$)' ]] && ((${#name_stack[@]})); then
      read -r command_word_pending after_wrapper xargs_opts xargs_value xargs_cmd wrapper_name <<<"${name_stack[-1]}"
      unset 'name_stack[-1]'
      continue
    fi
    if [[ "${words[idx]}" == '`' ]]; then
      if ((in_backtick)); then
        # Closing: the command that contained the substitution has its name.
        in_backtick=0
        command_word_pending=0
        after_wrapper=0
        wrapper_name=''
        xargs_opts=0 xargs_value=0 xargs_cmd=0
        continue
      fi
      ((command_word_pending)) && refuse "${CMD_MSG}"
      in_backtick=1
    fi
    command_word_pending=1
    after_wrapper=0
    wrapper_name=''
    xargs_opts=0 xargs_value=0 xargs_cmd=0
    continue
    ;;
  target) continue ;;
  *) ;;
  esac
  ((command_word_pending)) || continue
  raw_word="${raw_words[idx]}"
  word="${words[idx]}"
  # xargs's own options, read before the assignment and keyword tests
  # because none of them is either, and held to the literal test like any
  # word here. The first word after them is the command xargs runs; once
  # that command is known -- and is not itself a wrapper -- the words after
  # it are its arguments, not candidates for its name, so
  # `git ls-files | xargs grep -l git` is grep's search for the word git and
  # not an xargs in front of git (review on aurora-zfs-simple#224).
  if ((xargs_value)); then
    word_is_literal "${raw_word}" "${word}" || refuse "${CMD_MSG}"
    xargs_value=0
    continue
  fi
  if ((xargs_opts)); then
    word_is_literal "${raw_word}" "${word}" || refuse "${CMD_MSG}"
    xargs_word_kind "${word}"
    case $? in
    0) continue ;;
    1)
      xargs_value=1
      continue
      ;;
    2)
      xargs_opts=0
      xargs_cmd=1
      continue
      ;;
    3)
      xargs_opts=0
      xargs_cmd=1
      ;;
    *) xargs_opts=0 ;;
    esac
  fi
  if [[ "${word}" =~ ^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?= ]]; then
    # An assignment; the name is still to come. Recorded, because this scan
    # is the only one that knows an assignment stands *before* a name: the
    # per-command scan below reads words of the command, and bash hands the
    # assignment to the environment instead of to the argv. Read with its
    # quotes already removed (`${word}`, not `${raw_word}`): a wrapper such
    # as `env` reads its own argument after bash has stripped the quotes,
    # so `env 'GIT_EXTERNAL_DIFF'=/tmp/evil git diff HEAD` sets it although
    # the quote mark keeps bash's own leading-assignment grammar from
    # reading the word that way at all -- a plain `'FOO=bar' cmd` runs
    # nothing bash treats as `cmd`, since the quote disqualifies the word
    # as an assignment and bash tries to run the literal text `FOO=bar` as
    # a command instead, so reading it as an assignment here can only
    # over-refuse a command line bash would already have failed to run.
    name_assignments[idx]=1
    continue
  fi
  case "${word}" in
  '{' | '}' | '!' | if | then | else | elif | fi | do | done | while | until | time | coproc)
    continue # a keyword; the name is still to come
    ;;
  *) ;;
  esac
  if [[ "${wrapper_name}" == env ]] &&
    [[ "${raw_word}" =~ ^-[^-]*S || "${raw_word}" == --split-string* ]]; then
    refuse "${CMD_MSG}"
  fi
  word_is_literal "${raw_word}" "${word}" || refuse "${CMD_MSG}"
  # A wrapper, found by the last component of the word. Claude Code's
  # matcher cuts a wrapper's path at its last `/` or `\`, on the text as
  # typed, and matches the allow rule against the words after it, so
  # `./shim/nohup git diff HEAD` and `'./shim\nohup' git diff HEAD` are
  # approved as `git diff HEAD` -- while bash runs the file at that path,
  # which an agent can write. An unquoted backslash does the same without a
  # file: bash reads `/usr/bin\timeout` as `/usr/bintimeout`, which does not
  # exist, but it has already truncated the target of
  # `/usr/bin\timeout 5 python3 tests/run_tests.py >out` before it says so,
  # and the matcher saw `timeout` and approved the rest. So the word as
  # typed (quotes removed, backslashes kept) decides: a wrapper is stepped
  # over only when that is exactly its bare name, `/usr/bin/<name>` or
  # `/bin/<name>`, and any other spelling of one is refused
  # (`WRAPPER_PATH_MSG`). The last component is read both ways -- from the
  # word bash runs, cut at `/`, and from the typed word, cut at `/` or `\`
  # -- so neither view can hide a wrapper from the other. This runs after
  # the literal test, so `$D/env` is still refused as a name built at
  # runtime.
  typed="${raw_word//[\'\"]/}"
  wrapper="${word##*/}"
  is_wrapper "${wrapper}" || wrapper="${typed##*[\\/]}"
  if is_wrapper "${wrapper}"; then
    case "${typed}" in
    "${wrapper}" | "/usr/bin/${wrapper}" | "/bin/${wrapper}") ;;
    *) refuse "${WRAPPER_PATH_MSG}" ;;
    esac
    after_wrapper=1
    wrapper_name="${wrapper}"
    xargs_cmd=0
    if [[ "${wrapper}" == xargs ]]; then
      xargs_words[idx]=1
      xargs_opts=1
    fi
    continue
  fi
  # A literal path to git, or to the first word of a gated prefix, reaches
  # the same tool, and is rewritten so every rule the bare name opens applies:
  # `/usr/bin/gh pr view 1 --json number --jq env` printed GH_TOKEN and
  # `/usr/bin/skopeo inspect docker://x >cosign.pub` truncated the trust
  # anchor, while each bare spelling was refused (#479). No allow rule
  # matches the path spelling, but bypass and auto modes prompt for nothing.
  if [[ "${word}" == */* ]]; then
    case "${word##*/}" in
    git | gh | cosign | skopeo | python3)
      raw_words[idx]="${word##*/}"
      words[idx]="${word##*/}"
      ;;
    *) ;;
    esac
  fi
  command_names[idx]=1
  if ((xargs_cmd)); then
    xargs_cmd=0
    after_wrapper=0 # the words after xargs's command are that command's arguments
  fi
  ((after_wrapper)) || command_word_pending=0
done

# Whether the shell hands git a file on stdin: `<FILE`, the one input form
# that opens a path. `<<` and `<<<` carry text typed into the command, `<&N`
# duplicates a descriptor, and `<>` is refused as a write already. /dev/null
# has nothing to print.
redirection_reads_a_path() {
  local op="$1" target="$2"
  [[ "${op}" == '<' && "${target}" != /dev/null ]]
}

# Whether the shell opens a redirection's target for writing. Every operator
# with a `>` in it does -- `>`, `>>`, `>|`, `&>`, `&>>`, and `<>`, which
# opens read-write and creates the file -- and so does `>&` when its target
# is a path: `>&file` is bash's older spelling of `&>file`. The exception is
# a target that names a descriptor: `>&1`, `2>&1` and `>&-` duplicate or
# close a descriptor and touch no path. `2>&file` is an "ambiguous redirect"
# error in bash and writes nothing, and is refused anyway -- the rule is the
# operator and the target's shape, not a model of bash's error paths. `<`,
# `<<`, `<<<` and `<&` open nothing for writing.
redirection_writes_a_path() {
  local op="$1" target="$2"
  [[ "${op}" == *'>'* ]] || return 1
  if [[ "${op}" == *'&' ]]; then
    [[ "${target}" =~ ^[0-9]+$ || "${target}" == '-' ]] && return 1
  fi
  return 0
}

# From a `git` word to the end of *that command*: the scope opens at `git`
# and closes at the next separator, so `git diff HEAD | jq '{a,b}'` leaves
# the jq program alone while `git log -1; git diff {a,b}` and `echo x | git
# diff {a,b}` are each refused at their own `git`. A redirection does not
# close it: `git log 2>&1 --outpu{t,t}=FILE` is one command, and the brace
# in it is git's. This is narrower than the `in_git` latch below, which holds
# to the end of the string, and can be: that latch guards the `--output`
# test, which is kept wide on purpose. The word is compared with
# its quotes removed so `'git'` opens the scope as `git` does; a `git`
# assembled from an expansion (`g{i,i}t`) matches no allow rule and prompts on
# its own.
#
# The same scope decides the output redirections: bash attaches a redirection
# to the simple command it is written in, so `git diff HEAD >cosign.pub` is
# git's and `echo x >out; git diff HEAD` and `git diff HEAD | jq . >out` are
# not -- those are decided by whatever rule covers `echo` and `jq`, the way
# `git diff HEAD | tee cosign.pub` already is. Bash also lets a redirection
# *precede* the command name -- `>cosign.pub git diff HEAD` is the same
# command as `git diff HEAD >cosign.pub`, and `git status; >cosign.pub git
# diff HEAD` truncated the trust anchor while a scope that opened at the
# `git` word had not yet seen the target (review on arch-bootc#317). So a
# writing target seen before any `git` word of its command is carried until
# the command's name is known, and refused if that name turns out to be git;
# it is dropped at the next separator, so `>out echo x; git diff HEAD` is
# still echo's own, and it is taken only by a `git` the command-name scan
# above marked as naming its command, so `>out printf %s git` is printf's. A brace found anywhere in the string wins the refusal:
# an expanding brace means the words here are not the words git would
# receive, and that message is the one to act on first.
#
# The same scope refuses a word that begins with an unquoted `~`: bash
# expands it to `$HOME` before git runs, and the operand scan below, which
# reads the quote-stripped spelling, resolved the literal `~` inside the
# checkout and let `git diff -- ~/.aws/credentials ~/.bashrc` through. The
# test is on the word as typed, so `'~/x'` and `\~/x`, which bash leaves
# alone, are not refused. A redirection's target is not a word of git's and
# is decided above.
raw_in_git=0
writing_redirect=0
prefix_writing_redirect=0 # a writing target seen before this command's git word
reading_redirect=0
prefix_reading_redirect=0 # the same, for a file put on stdin (`<.env git log --stdin`)
scope_stack=()            # the outer command's state, while a `$(...)` is being read
for ((idx = 0; idx < ${#raw_words[@]}; idx++)); do
  if [[ "${kinds[idx]}" == sep ]]; then
    # A `$(...)` substitution is a nested command; the scope of the command
    # around it, and a writing target waiting for that command's name, are
    # saved at the `$(` and restored at its `)` rather than reset.
    # shellcheck disable=SC2016 # the literal `$(` is the separator's name
    if [[ "${words[idx]}" == '$(' ]]; then
      scope_stack+=("${raw_in_git} ${prefix_writing_redirect} ${prefix_reading_redirect}")
    elif [[ "${words[idx]}" == '$)' ]] && ((${#scope_stack[@]})); then
      read -r raw_in_git prefix_writing_redirect prefix_reading_redirect <<<"${scope_stack[-1]}"
      unset 'scope_stack[-1]'
      continue
    fi
    raw_in_git=0
    prefix_writing_redirect=0
    prefix_reading_redirect=0
    continue
  fi
  raw_word="${raw_words[idx]}"
  if ((raw_in_git)); then
    if brace_would_expand "${raw_word}" ||
      [[ "${raw_word}" == '<(' || "${raw_word}" == '>(' ]]; then
      refuse "${BRACE_MSG}"
    fi
    if [[ "${kinds[idx]}" == word && "${raw_word}" == '~'* ]]; then
      refuse "${TILDE_MSG}"
    fi
    if [[ "${kinds[idx]}" == word ]] && ((${globs[idx]:-0})); then
      refuse "${GLOB_MSG}"
    fi
    if [[ "${kinds[idx]}" == target ]] &&
      redirection_writes_a_path "${redirects[idx]}" "${words[idx]}"; then
      writing_redirect=1
    fi
    if [[ "${kinds[idx]}" == target ]] &&
      redirection_reads_a_path "${redirects[idx]}" "${words[idx]}"; then
      reading_redirect=1
    fi
  elif [[ "${kinds[idx]}" == target ]]; then
    redirection_writes_a_path "${redirects[idx]}" "${words[idx]}" && prefix_writing_redirect=1
    redirection_reads_a_path "${redirects[idx]}" "${words[idx]}" && prefix_reading_redirect=1
  fi
  if [[ "${kinds[idx]}" == word && "${words[idx]}" == "git" ]]; then
    raw_in_git=1
    ((prefix_writing_redirect)) && ((${command_names[idx]:-0})) && writing_redirect=1
    ((prefix_reading_redirect)) && ((${command_names[idx]:-0})) && reading_redirect=1
  fi
done
((writing_redirect)) && refuse "${REDIRECT_MSG}"

# The same write, reached by the allow-listed commands that are not git.
#
# Everything above is scoped to a `git` word, and the write primitive is not
# git's alone. `.claude/settings.json` allows twelve other command prefixes
# with a trailing `:*` -- "this command with any arguments" -- and an output
# redirection is part of the string that rule matches, so the shell opens the
# target before the command runs and nothing prompts:
# `python3 tests/run_tests.py >cosign.pub` truncates the trust anchor before a
# test is collected, and `gh run view 1 --log >.claude/settings.json`
# overwrites the file that holds these rules. The `Read(...)` deny rows gate
# the Read tool and say nothing about it, exactly as they say nothing about
# `git diff HEAD >cosign.pub`.
#
# One of those commands also carries the flag spelling. `--output-file` is a
# *persistent* flag on cosign's root command ("log output to a file"), so
# `cosign verify` has it, and cosign creates and truncates the path before it
# verifies anything: `cosign verify --output-file cosign.pub --key k <ref>`
# empties the trust anchor and then exits 1 on a key it could not load
# (observed with cosign v3.1.3). `_note_ruff` narrowed the two ruff rows to
# exact commands instead of a hook because every documented ruff invocation is
# a fixed string; cosign's is not -- the image reference is an argument -- so
# the rule keeps its `:*` and the refusal has to live here.
#
# The prefixes below are the allow rows with a trailing `:*` other than git's,
# which the scan above already covers. `Bash(ruff check)` and the full lint
# command are absent on purpose: they carry no `:*`, so a redirection makes
# the string match neither row and Claude Code prompts.
# `tests/test_git_diff_gate.py` derives this list from the settings file
# rather than restating it, so a rule added there fails here until it is
# listed.
GATED_PREFIXES=(
  'cosign verify'
  'gh issue list'
  'gh issue view'
  'gh pr diff'
  'gh pr list'
  'gh pr view'
  'gh run list'
  'gh run view'
  'python3 tests/check_coverage.py'
  'python3 tests/run_tests.py'
  'skopeo inspect'
)

# shellcheck disable=SC2016 # the message quotes shell spellings as literal text
GATED_REDIRECT_MSG='blocked: an output redirection (>, >>, >|, &>, &>>, N>, >&FILE, <>) inside an allow-listed command makes the shell open its target for writing before the command runs, and the allow rule matches a command prefix while the redirection is the rest of the string, so nothing prompts: `python3 tests/run_tests.py >cosign.pub` truncates the trust anchor before a test is collected, and `gh run view 1 --log >.claude/settings.json` overwrites the file holding these rules. It is the same write .claude/hooks/gate-git-diff.sh already refuses for `git diff HEAD >cosign.pub`. These commands print to stdout; read that, or pipe it. Descriptor forms (2>&1, >&2, >&-) and input redirections (<, <<, <<<, <&) are not affected, and a command no allow rule covers is left alone -- that one prompts on its own.'

# shellcheck disable=SC2016 # the backticks quote a command spelling for the reader
COSIGN_OUT_MSG='blocked: cosign --output-file FILE (and the = form) sends cosign output to the path it names, and cosign creates and truncates that path before it verifies anything, so `cosign verify --output-file cosign.pub --key cosign.pub <ref>` empties the trust anchor and then fails. It is a persistent flag on cosign root command, so every subcommand carries it, and Bash(cosign verify:*) approves the whole command line -- the image reference is an argument, so that rule cannot drop its trailing :* the way the ruff rows did. cosign prints to stdout; read that instead.'

# shellcheck disable=SC2016 # the literal ${VAR} and $(...) are what the reader has to see
COSIGN_EXPAND_MSG='blocked: a brace bash could expand, a $ or a backtick in a word of a cosign invocation is refused rather than expanded, for the reason BRACE_MSG and EXPAND_MSG give for git: bash rewrites the words before cosign sees them, so `--output-fil{e,e}=FILE` matches no flag spelling here and reaches cosign as --output-file=FILE, and $(...), ${VAR} and a backtick supply a word this gate never saw. Four characters rebuilt the git refusals twice this way. Write the command out in full.'

# shellcheck disable=SC2016 # the backticks quote a command spelling for the reader
SKOPEO_CONF_MSG='blocked: skopeo --registries-conf FILE (and the = form) hands skopeo a registries file of the caller'"'"'s choosing, and skopeo parses it as TOML before it contacts a registry and quotes what it could not read back on error -- `skopeo inspect --registries-conf ./.env docker://x` prints a .env key and its value, past the Read(...) deny rules in .claude/settings.json, which gate the Read tool and not the shell. It also re-points where image names resolve. It is a global option, so every skopeo subcommand takes it, and Bash(skopeo inspect:*) approves the whole command line with no prompt. Nothing in this repository passes it. Run the command without the option.'

# shellcheck disable=SC2016 # the message quotes shell spellings as literal text
GATED_ENV_MSG='blocked: an assignment before a command (`NAME=value cmd ...`) is an environment the command runs under rather than a word of it, and for the commands this gate covers that environment changes what runs or where it goes. A git invocation carries both primitives refused elsewhere here: `GIT_EXTERNAL_DIFF=prog git diff HEAD~1 HEAD` runs prog once per changed path, `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0=prog` reaches that same driver under another name, `PATH=dir git diff HEAD` runs a different git, and `GIT_DIR` and `GIT_INDEX_FILE` re-point the repository the operand scan was reasoning about -- each of them from a string the allow rows match on their `git diff` prefix. The other allow-listed families are the same: `GH_HOST=other gh pr list` and `GH_CONFIG_DIR=dir gh run view 1` send the token somewhere else. A deny list of variable names is the wrong shape for this, since it would have to track git'"'"'s own environment as git grows it. Run the command without the assignment.'

# shellcheck disable=SC2016 # the message quotes shell spellings as literal text
EXPORT_ENV_MSG='blocked: an assignment made by the export family (`export NAME=value`, `declare -x`, `typeset -x`, `readonly`) reaches a later command of the same string exactly as a leading `NAME=value` does, and this string arms one and then runs git or an allow-listed command: `export GIT_EXTERNAL_DIFF=prog; git diff HEAD~1 HEAD` runs prog once per changed path while no word of the git invocation carries an assignment at all. The builtin is refused rather than its options read, because the flag that exports has several spellings (-x, -gx, an earlier `declare -x NAME` with a plain `NAME=value` after it) and a half-modelled option list is a gate that disagrees with bash in some other direction. Only a string that also runs one of those commands is refused; an export on its own is not this gate'"'"'s business, and does not need to be -- a PreToolUse hook reads one command string, and Claude Code'"'"'s own permission matcher already holds every later command of that same string to its own allow rule, on every recognized separator (`;`, `&&`, `||`, `|`, `|&`, `&`, a newline): the export half of `git status; export FOO=1` prompts on its own account whether or not this gate says anything about it.'

# shellcheck disable=SC2016 # the message quotes shell spellings as literal text
XARGS_MSG='blocked: xargs adds the words it reads from standard input (or from the file named by -a) to the command it runs, so the operands git or an allow-listed command receive are not in this string and nothing here can check them: `printf '"'"'%s\n'"'"' /dev/null ./cosign.key | xargs git diff` is the plain-file read of the key with no operand written anywhere, and `xargs cosign verify <args.txt` hands cosign an --output-file this gate never sees. The allow rule is no stop either: Claude Code matches Bash(git diff:*) against `xargs git diff` as readily as against `git diff`, so nothing prompts. So xargs is refused when the command it runs is git or one of the allow-listed prefixes, wherever it stands among the wrappers (`timeout 5 xargs git diff`, `xargs -a list.txt git diff`). Name the operands in the command itself instead. xargs in front of any other command (`git diff --name-only | xargs echo`) is not affected: it matches no allow row in .claude/settings.json, so it is left to the permission prompt.'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
GH_JQ_ENV_MSG='blocked: gh evaluates its --jq (-q) filter with gojq, which hands the filter the whole process environment through the `env` builtin and `$ENV`, so `gh pr view 1 --json number --jq env` prints every variable this shell holds -- GH_TOKEN, GITHUB_TOKEN, ANTHROPIC_API_KEY and whatever else is exported -- under the Bash(gh pr view:*), Bash(gh pr list:*), Bash(gh issue view:*), Bash(gh issue list:*), Bash(gh run view:*) and Bash(gh run list:*) allow rows with no prompt. Those rows are there to read pull requests, issues and runs, not the environment, and no Read(...) deny rule stands in front of a variable. A filter naming `env` or `$ENV` is refused wherever it stands in the filter, a quoted string included, and so is a filter bash rewrites before gh sees it -- a $ or a backtick outside single quotes, a brace, an unquoted glob or extglob -- since `{e,}nv` and `$(echo env)` reach gh as env. Name the fields you want, in single quotes: --jq '"'"'.title'"'"', --jq '"'"'.jobs[].conclusion'"'"'.'

command_is_gated() {
  local joined="$1" prefix
  for prefix in "${GATED_PREFIXES[@]}"; do
    [[ "${joined}" == "${prefix}" ]] && return 0
  done
  return 1
}

# Whether the words so far could still grow into a gated prefix. A command whose
# leading words cannot -- because a wrapper's own option took the first name
# slot (`env -u X python3 tests/run_tests.py`) -- starts its prefix over at the
# next name candidate, which is the word the wrapper actually runs.
prefix_could_match() {
  local joined="$1" prefix
  for prefix in "${GATED_PREFIXES[@]}"; do
    [[ "${prefix}" == "${joined} "* ]] && return 0
  done
  return 1
}

# The command that just ended. Only two facts about it are kept -- whether its
# leading words matched one of the prefixes above, and whether a redirection
# in it opens a path -- because a redirection can be written before the name
# (`>cosign.pub cosign verify <ref>` is the same command as
# `cosign verify <ref> >cosign.pub`), so neither fact is complete until the
# command ends.
check_gated_command() {
  # xargs in this command's wrapper chain, in front of git or a gated prefix.
  # The words it reads from stdin or `-a FILE` become operands of the command
  # it runs, so every scan here -- the operand count, `--output`, cosign's
  # `--output-file` -- is reading a command that is not the one that runs,
  # and the allow row matches `xargs <prefix>` as it matches `<prefix>`.
  # Decided first: the words of this command are not the words that run, so
  # that is the message to act on.
  ((cmd_xargs && (cmd_gated || cmd_git))) && refuse "${XARGS_MSG}"
  ((cmd_gated && cmd_writes)) && refuse "${GATED_REDIRECT_MSG}"
  # An assignment before the name is an environment the command runs under.
  # Git was exempt until this refusal, on the reading that a git invocation is
  # decided by the operand scan below; that scan reads words, and an assignment
  # is not one. `GIT_EXTERNAL_DIFF=prog git diff HEAD~1 HEAD` runs prog once
  # per changed path, and the allow row matches the string on its `git diff`
  # prefix all the same.
  ((cmd_gated && cmd_assign)) && refuse "${GATED_ENV_MSG}"
  ((cmd_git && cmd_assign)) && refuse "${GATED_ENV_MSG}"
  return 0
}

# Whether a jq filter names gojq's environment: the `env` builtin -- the word
# `env` with no identifier character on either side and no `.` or `$` before
# it, since `.env` is a field of the JSON gh fetched and `$env` a variable the
# filter bound itself -- or the `$ENV` variable. A string literal is not told
# apart from code (`select(.name == "env")` is refused too), because telling
# them apart means parsing jq here.
jq_filter_reads_env() {
  [[ "$1" =~ (^|[^A-Za-z0-9_.$])env([^A-Za-z0-9_]|$) || "$1" =~ \$ENV([^A-Za-z0-9_]|$) ]]
}

# Whether bash would substitute into a word as typed: a `$` outside single
# quotes that a backslash does not escape. Inside single quotes a `$` is jq's
# own variable syntax (`.[] as $x | $x.name`), which bash leaves alone.
word_substitutes() {
  local raw="$1" quote='' ch i
  for ((i = 0; i < ${#raw}; i++)); do
    ch="${raw:i:1}"
    if [[ "${quote}" == "'" ]]; then
      [[ "${ch}" == "'" ]] && quote=''
      continue
    fi
    if [[ "${ch}" == $'\\' ]]; then
      i=$((i + 1))
      continue
    fi
    case "${ch}" in
    "'") if [[ -z "${quote}" ]]; then quote="'"; fi ;;
    '"') if [[ -z "${quote}" ]]; then quote='"'; else quote=''; fi ;;
    '$') return 0 ;;
    *) ;;
    esac
  done
  return 1
}

# The filter word of a gh command, read as gh reads it: after `--jq` or as its
# `=` value, and after `-q` alone, attached (`-qenv`, `-q=env`) or last in a
# cluster of one-letter flags (`-wq env`). It is refused when it names the
# environment, when bash would rewrite it before gh reads it, and when it
# opens an extglob -- `@(env)`, which the split above keeps as one word.
check_gh_filter() {
  local at="$1" filter="$2"
  jq_filter_reads_env "${filter}" && refuse "${GH_JQ_ENV_MSG}"
  word_substitutes "${raw_words[at]}" && refuse "${GH_JQ_ENV_MSG}"
  brace_would_expand "${raw_words[at]}" && refuse "${GH_JQ_ENV_MSG}"
  ((${globs[at]:-0})) && refuse "${GH_JQ_ENV_MSG}"
  word_has_extglob "${raw_words[at]}" && refuse "${GH_JQ_ENV_MSG}"
  return 0
}

reset_command() {
  cmd_prefix=''
  cmd_writes=0
  cmd_cosign=0
  cmd_skopeo=0
  cmd_assign=0
  cmd_named=0
  cmd_gated=0
  cmd_git=0
  cmd_export=0
  cmd_xargs=0
  cmd_gh_jq=0
}

# The words of a command from its *name* onward: a leading assignment
# (`FOO=bar cosign verify ...`) is not part of the prefix an allow rule
# matches, and neither is a redirection's target, which is the shell's word
# rather than the command's. `command_names` above marks the name, and every
# word after it belongs to the same command until a separator.
cmd_prefix='' # the words so far, space-joined, while a prefix is still possible
cmd_writes=0  # a redirection in this command opens a path for writing
cmd_cosign=0  # its name is cosign, so the flag and expansion rules apply
cmd_skopeo=0  # its name is skopeo, so the registries-file rule applies
cmd_assign=0  # an assignment stands before this command's name
cmd_named=0   # the name has been seen; every later word belongs to it
cmd_gated=0   # its leading words matched one of GATED_PREFIXES
cmd_git=0     # its name is git, which the allow rows cover with their own `*`
cmd_export=0  # its name is export/declare/typeset/readonly: its own words assign
cmd_xargs=0   # an xargs in its wrapper chain appends words this gate never sees
cmd_gh_jq=0   # the next word of this gh command is its --jq/-q filter
cmd_stack=()  # the outer command's state, while a `$(...)` is being read
export_idx=-1 # the first word that an export-family command assigns
allexport=0   # `set -a`/`set -o allexport` ran: every later bare assignment exports
gate_idx=-1   # the last word at which a gated command or git is running
reset_command
for ((idx = 0; idx < ${#words[@]}; idx++)); do
  case "${kinds[idx]}" in
  sep)
    # A gh `--jq`/`-q` whose filter word never came: the word that follows is
    # a `$(...)`, a backtick or a process substitution, which hands gh a filter
    # this gate never read (`--jq $(printf env)`), or the command ended there.
    ((cmd_gh_jq)) && refuse "${GH_JQ_ENV_MSG}"
    # A `$(...)` or a backtick inside a cosign invocation builds a word this
    # gate never saw, the way one inside a git invocation does.
    # shellcheck disable=SC2016 # the literal `$(` is the separator's name
    if ((cmd_cosign)) && [[ "${words[idx]}" == '$(' || "${words[idx]}" == *'`'* ]]; then
      refuse "${COSIGN_EXPAND_MSG}"
    fi
    # A `$(...)` substitution is a nested command: it is decided on its own,
    # and the command around it -- including a redirection of its own already
    # seen -- resumes at the `)` rather than starting over, so
    # `python3 tests/run_tests.py $(date) >cosign.pub` is still that command's
    # write.
    # shellcheck disable=SC2016 # the literal `$(` is the separator's name
    if [[ "${words[idx]}" == '$(' ]]; then
      cmd_stack+=("${cmd_writes} ${cmd_cosign} ${cmd_skopeo} ${cmd_assign} ${cmd_named} ${cmd_gated} ${cmd_git} ${cmd_export} ${cmd_xargs} ${cmd_prefix}")
      reset_command
      continue
    fi
    if [[ "${words[idx]}" == '$)' ]] && ((${#cmd_stack[@]})); then
      check_gated_command
      read -r cmd_writes cmd_cosign cmd_skopeo cmd_assign cmd_named cmd_gated cmd_git cmd_export cmd_xargs cmd_prefix <<<"${cmd_stack[-1]}"
      unset 'cmd_stack[-1]'
      continue
    fi
    check_gated_command
    reset_command
    continue
    ;;
  target)
    redirection_writes_a_path "${redirects[idx]}" "${words[idx]}" && cmd_writes=1
    continue
    ;;
  *) ;;
  esac
  # An assignment before the name is an environment the command runs under,
  # never a word of its argv, so the scans that read words never see it. It is
  # recorded for the command and decided when the command ends, because a
  # refusal here would have to guess at a name that has not been seen yet.
  # `set -a`/`set -o allexport` (below) turns *every* bare assignment after
  # it into the export family's own reach, without an `export` word anywhere
  # near it, so a bare assignment seen while that mode is on is treated the
  # same as one exported (arms export_idx too), the same over-refusing
  # direction `readonly`/bare `declare` already take.
  if ((${name_assignments[idx]:-0})); then
    cmd_assign=1
    ((allexport)) && ((export_idx < 0)) && export_idx=${idx}
    continue
  fi
  # An xargs the command-name scan stepped over as a wrapper of this command.
  # Like an assignment it is decided when the command ends, once its name --
  # git, a gated prefix, or anything else -- is known.
  ((${xargs_words[idx]:-0})) && cmd_xargs=1
  ((cmd_named)) || ((${command_names[idx]:-0})) || continue
  cmd_named=1
  if ((cmd_gated == 0)); then
    # The words so far cannot grow into a gated prefix and this word may still
    # be the name (a wrapper's own option came first): start over here. Without
    # it `env -u X python3 tests/run_tests.py >cosign.pub` built the prefix
    # `-u X python3 ...`, matched no row, and the redirection refusal never
    # fired.
    if [[ -n "${cmd_prefix}" ]] && ((cmd_git == 0)) && ((cmd_cosign == 0)) &&
      ((${command_names[idx]:-0})) && ! prefix_could_match "${cmd_prefix}"; then
      cmd_prefix=''
    fi
    # The name, decided here rather than at the first word of the command,
    # because after a wrapper the wrapper's own option is a name candidate of
    # its own (`env -u X git diff`). Once the name is git or cosign the restart
    # above stops: every later word is a name candidate too, and `git`, `diff`
    # and `HEAD` would each take their turn as the name.
    [[ -z "${cmd_prefix}" && "${words[idx]}" == "git" ]] && cmd_git=1
    [[ -z "${cmd_prefix}" && "${words[idx]}" == "cosign" ]] && cmd_cosign=1
    [[ -z "${cmd_prefix}" && "${words[idx]}" == "skopeo" ]] && cmd_skopeo=1
    # The export family. Its assignments stand *after* the name rather than
    # before it, so the scan that records a leading `NAME=value` never sees
    # them, and bash applies them to every later command of the string.
    if [[ -z "${cmd_prefix}" ]]; then
      case "${words[idx]}" in
      export | declare | typeset | readonly) cmd_export=1 ;;
      *) ;;
      esac
    fi
    # `set -a`/`set -o allexport` puts the shell itself into a mode where
    # every later bare assignment exports, with no `export` word anywhere
    # near it -- checked against `cmd_prefix` before this word is appended
    # to it, so it reads a *later* word of a `set` command (`-a`, clustered
    # as `-ea`, or the long form's own argument `allexport`) rather than
    # the name `set` itself.
    if [[ "${cmd_prefix}" == "set" || "${cmd_prefix}" == "set "* ]] &&
      [[ "${words[idx]}" == -*a* || "${words[idx]}" == "allexport" ]]; then
      allexport=1
    fi
    cmd_prefix="${cmd_prefix:+${cmd_prefix} }${words[idx]}"
    command_is_gated "${cmd_prefix}" && cmd_gated=1
  fi
  # The last word position at which this string runs something the gate covers.
  # Compared against the first exported assignment below, so that an export
  # written *after* the command it cannot reach is left alone.
  ((cmd_gated || cmd_git)) && gate_idx=${idx}
  # A word of an export-family command that assigns. `export FOO=1` and
  # `declare -x FOO=1` put FOO in the environment of every command bash runs
  # after them in this string, which is the same reach as `FOO=1 cmd` by a
  # spelling the leading-assignment scan is not looking at.
  if ((cmd_export)) && ((export_idx < 0)) &&
    [[ "${raw_words[idx]}" =~ ^[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?= ]]; then
    export_idx=${idx}
  fi
  # gh's --jq filter runs in gojq, whose `env` builtin and `$ENV` are the
  # process environment: `gh pr view 1 --json number --jq env` prints GH_TOKEN
  # and every other exported variable under the gh allow rows. gh parses its
  # flags with pflag: `--jq V`, `--jq=V`, `-q V`, `-qV`, `-q=V`, and `-q` last
  # in a cluster of other one-letter flags (`-wq V`). A value-taking letter
  # earlier in the cluster (`-Rq`) is read as a filter flag too, which can
  # only over-refuse.
  if ((cmd_gated)) && [[ "${cmd_prefix}" == gh\ * ]]; then
    if ((cmd_gh_jq)); then
      cmd_gh_jq=0
      check_gh_filter "${idx}" "${words[idx]}"
    else
      case "${words[idx]}" in
      --jq) cmd_gh_jq=1 ;;
      --jq=*) check_gh_filter "${idx}" "${words[idx]#--jq=}" ;;
      -q* | -[!-]*q*)
        gh_filter="${words[idx]#*q}"
        gh_filter="${gh_filter#=}"
        if [[ -n "${gh_filter}" ]]; then
          check_gh_filter "${idx}" "${gh_filter}"
        else
          cmd_gh_jq=1
        fi
        ;;
      *) ;;
      esac
    fi
  fi
  # skopeo reads the file `--registries-conf` names as TOML before it
  # contacts a registry, and its error quotes the key it last read and the
  # bare value it could not, `(last key "API_TOKEN"): expected value but
  # found "sekretvalue" instead`, so `skopeo inspect --registries-conf
  # ./.env docker://x` prints a .env line back under the `skopeo inspect`
  # allow row. It is a global option, valid after every subcommand, so it is
  # refused in any skopeo invocation, in both the detached and the attached
  # (`--registries-conf=FILE`) spellings; skopeo's flag parser takes no
  # abbreviation of a long option.
  if ((cmd_skopeo)); then
    case "${words[idx]}" in
    --registries-conf | --registries-conf=*) refuse "${SKOPEO_CONF_MSG}" ;;
    *) ;;
    esac
  fi
  ((cmd_cosign)) || continue
  case "${words[idx]}" in
  --output-file | --output-file=*) refuse "${COSIGN_OUT_MSG}" ;;
  *) ;;
  esac
  if brace_would_expand "${raw_words[idx]}" || [[ "${raw_words[idx]}" == *'$'* ]]; then
    refuse "${COSIGN_EXPAND_MSG}"
  fi
done
check_gated_command
# Decided once, over the whole string: the export may be written before the
# command it arms, and only then does it reach it.
((export_idx >= 0 && gate_idx > export_idx)) && refuse "${EXPORT_ENV_MSG}"

# The whole string with quoting removed, for the one test that is a substring
# match rather than a word: the shell removes quotes and backslashes on the
# way to git, so `--no-'index'` and `--no-\index` both reach it as
# `--no-index`.
normalized="${command_string//[\'\"\\]/}"

case "${normalized}" in
*--no-index*) refuse "${DIFF_MSG}" ;;
*) ;;
esac

# Git's path_inside_repo, which decides on the *spelling* rather than on where
# the path ends up. That distinction is the whole of this function, and folding
# `..` before the comparison gets it backwards: `git diff -- ../<checkout>/
# cosign.key -` names a file inside this repository by a route that leaves it
# and comes back, git's test calls that outside and enters the plain-file mode,
# and a gate that resolved the path first saw a tidy in-tree path and allowed
# it -- reading a denied path with two operands that both look local. So an
# absolute path, any `..` component, and the stdin operand `-` each count as
# outside here, and only a plain relative path is resolved at all. So does a
# leading `~`: to bash that is a home directory, never a path under this
# checkout, and resolving the literal put `~/.aws/credentials` inside the
# tree. Anything this cannot decide -- no working tree, no realpath on the
# host -- counts as outside too, so the gate refuses rather than guesses.
path_inside_worktree() {
  local candidate toplevel
  case "$1" in
  - | /* | '~'*) return 1 ;;
  ../* | */../* | */..) return 1 ;;
  ..) return 1 ;;
  esac
  toplevel="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1
  candidate="$(realpath -m -s -- "$1" 2>/dev/null)" || return 1
  [[ "${candidate}" == "${toplevel}" || "${candidate}" == "${toplevel}"/* ]]
}

# Every test below reads a word as typed, and bash rewrites the words before
# git receives them. `$(...)`, `${x}`, `$x` and a backtick each supply words
# the operand scan never counted, so `git diff $(echo /dev/null) ./cosign.key`
# is one operand here and two at git -- one short of the refusal -- and
# `$'\x74'` is the letter t, so `--outpu$'\x74'=FILE` matches neither
# `--output` nor `--output=*` here and arrives at git as `--output=FILE`. The
# brace scan above catches a `${VAR}` and nothing else of this.
#
# They are refused rather than expanded, for the reason the brace scan gives:
# correct expansion means reimplementing bash in a hook -- nesting, quoting,
# word splitting on $IFS -- and a half-right expansion is a gate that
# disagrees with the shell in some other direction. A refusal cannot be
# half-right, and a git argument built at runtime was already outside what
# this hook can vouch for, so refusing it turns a silent pass into a visible
# refusal. Neither character has a literal form git relies on, the way
# `HEAD@{1}` relies on a brace, so unlike the brace test this one is every
# `$` and every backtick.
#
# The `$` half is scoped like the brace scan: to the words as typed of the
# command that starts at a `git` word and ends at the next separator bash
# honours, so `git diff HEAD | awk '{print $1}'` and `jq '.[$x]' f | git
# diff` are left alone while `git log -1; git diff $x` is refused at its
# own `git`. The words as typed are the right ones here as well: the
# normalized split cuts a *quoted* operator inside an argument, and a scope
# that closed there would hand `git log --grep='a|b' --outpu$'\x74'=FILE -1`
# its `$` word unwatched. A `$` *before* the first `git` word is not checked
# and does not need to be: the allow rules in .claude/settings.json match a
# literal `git diff`/`git log` prefix, so an invocation assembled out of an
# expansion (`$GIT diff ...`) matches no allow rule and prompts on its own.
expand_in_git=0
for raw_word in "${raw_words[@]+"${raw_words[@]}"}"; do
  if [[ -z "${raw_word}" ]]; then
    expand_in_git=0
    continue
  fi
  if ((expand_in_git)) && [[ "${raw_word}" == *'$'* ]]; then
    refuse "${EXPAND_MSG}"
  fi
  [[ "${raw_word//[\'\"\\]/}" == "git" ]] && expand_in_git=1
done

# The backtick half cannot use that scope: an unquoted backtick is itself a
# separator to the split above, so it closes the scope it would have to be
# refused in and leaves no word behind. It is refused on the normalized
# words instead -- where it survives as a word of its own -- from the first
# `git` word to the end of the string, the latch `--output` uses below. The
# cost is a backtick in a quoted program piped from git, which no session
# needs; the alternative is `git diff \`echo /dev/null\` ./cosign.key`.
expand_in_git=0
for word in "${words[@]+"${words[@]}"}"; do
  if ((expand_in_git)) && [[ "${word}" == *'`'* ]]; then
    refuse "${EXPAND_MSG}"
  fi
  [[ "${word}" == "git" ]] && expand_in_git=1
done

# The input redirection found by the scope scan above, refused only here so
# that a more specific refusal of the same string -- `xargs git diff
# <list.txt` is xargs's -- is the message the caller reads.
((reading_redirect)) && refuse "${STDIN_MSG}"

# Whether a word of a blame invocation names a file for blame to read (see
# the header). A long option counts in any spelling parse-options accepts:
# `--con` is the shortest unambiguous prefix of --contents (`--co` is also
# --color-lines), and `--ignore-revs` the shortest of --ignore-revs-file
# that is not --ignore-rev, which takes a revision. A short cluster is read
# a letter at a time until the first letter that takes a value: `-S` names
# a file, and the rest of a `-L` word is a line range.
blame_option_reads_a_file() {
  local word="$1" name letters letter
  if [[ "${word}" == --* ]]; then
    name="${word#--}"
    name="${name%%=*}"
    ((${#name} >= 3)) && [[ "contents" == "${name}"* ]] && return 0
    ((${#name} >= 11)) && [[ "ignore-revs-file" == "${name}"* ]] && return 0
    return 1
  fi
  letters="${word#-}"
  while [[ -n "${letters}" ]]; do
    letter="${letters:0:1}"
    letters="${letters:1}"
    [[ "${letter}" == S ]] && return 0
    [[ "${letter}" == L ]] && return 1
  done
  return 1
}

seen_git=0
in_git=0
in_diff=0
in_blame=0
blame_after_dashdash=0
operands=0
unresolved=0
after_dashdash=0
skip_git_option_value=0

for ((idx = 0; idx < ${#words[@]}; idx++)); do
  word="${words[idx]}"
  kind="${kinds[idx]}"
  if [[ "${kind}" == sep ]]; then
    # The operand scan starts over at each command boundary. `in_git` does not:
    # it latches for the rest of the command string, so an `--output` in any
    # later command of the same string -- `git log --grep=a|b
    # --output=cosign.pub -1` is `git log --grep=a` piped into `b --output=...`
    # -- is refused rather than handed back unwatched. The cost is refusing an
    # `--output` that belongs to some later non-git command; the alternative
    # is a bypass spelled with one pipe.
    seen_git=0
    in_diff=0
    in_blame=0
    blame_after_dashdash=0
    skip_git_option_value=0
    continue
  fi

  # The target of a redirection is the shell's, not git's: `git diff HEAD
  # 2>&1` has one operand, and the `1` is neither a revision nor a path. The
  # targets the shell would open for writing were refused above.
  [[ "${kind}" == target ]] && continue

  # Scoped to the git invocation as a whole, and checked before anything below
  # skips a dash-prefixed word: the write primitive belongs to the
  # diff-generation machinery rather than to one subcommand, so `git log -p
  # --output=FILE` and `git show --output=FILE` reach it without the word
  # `diff` appearing anywhere. `--output=x` and a bare `--output` (the space
  # form, whose path is the next word) are the two spellings; the pattern is
  # anchored so `--output-indicator-new=%` does not match it.
  #
  # This is deliberately wider than the allow list: a `git commit -m` whose
  # message happens to contain the word --output is refused too. That costs a
  # rephrased commit message; the alternative is a list of which git
  # subcommands accept the flag, and the subcommand this hook forgot is the
  # hole.
  # Brace expansion is the last rewrite bash performs that this scan can still
  # see, and it undoes both refusals below. It splits one word into several --
  # `git diff {/dev/null,./cosign.key}` is a single word here and two operands
  # to git, so the operand count never reaches 2 -- and it splits a flag name
  # apart -- `--outpu{t,t}=FILE` matches neither `--output` nor `--output=*`
  # here and arrives at git as `--output=FILE --output=FILE`. It needs no
  # variable and no subshell, so it is not one of the runtime-built arguments
  # this hook says it cannot see; it is plainly in the string and simply was
  # not expanded.
  #
  # Refused rather than expanded. Expanding means reimplementing bash's rules
  # in this hook -- nesting, `{1..9}` sequences, quoting -- and a half-right
  # expansion is a gate that disagrees with the shell in some other direction.
  # A refusal cannot be half-right. It is not every brace, though: bash leaves
  # a brace alone unless a comma or a `..` range sits inside it, and git's own
  # `@{...}` revision syntax -- `HEAD@{1}`, `main@{upstream}`, `@{-1}`,
  # `@{2.days.ago}` -- is spelled with exactly that literal form. Refusing it
  # blocks the ordinary diff against the previous commit for no gain, so the
  # test is `brace_would_expand`: a comma or `..` somewhere after a `{` and
  # before a `}`, which every expansion bash performs must have, and nothing
  # bash would leave alone needs. That test ran above, on the words as
  # typed, because the words here have had their quotes removed and a quote
  # is what keeps `{a';',b}` one word.
  #
  # Scoped to the git invocation's own words, so `awk '{print}'` and
  # `jq '{a:1}'` are untouched whether they come before, after, or without a
  # git command in the same string (see `raw_in_git` above). The cost is a
  # `${VAR}` inside a git invocation, which is a runtime-built argument this
  # hook already cannot inspect -- refusing it is stricter than the status
  # quo, not weaker. A brace *before* the first `git` word is not
  # checked and does not need to be: the allow rules in .claude/settings.json
  # match a literal `git diff`/`git log` prefix, so a git invocation assembled
  # out of braces (`{git,:} diff ...`, `g{i,i}t diff ...`) matches no allow
  # rule and prompts on its own.
  if ((in_git)); then
    case "${word}" in
    --output | --output=*) refuse "${OUT_MSG}" ;;
    esac
  fi

  # A blame invocation's words up to `--`: after it every word is a path.
  if ((in_blame)); then
    ((blame_after_dashdash)) && continue
    if [[ "${word}" == "--" ]]; then
      blame_after_dashdash=1
    elif [[ "${word}" == -?* ]] && blame_option_reads_a_file "${word}"; then
      refuse "${BLAME_MSG}"
    fi
    continue
  fi

  if ((in_diff)); then
    if [[ "${word}" == "--" ]]; then
      if ((operands > 0)); then
        # A revision or path already stopped git's scan, so what follows is a
        # pathspec resolved against the repository, never a plain file.
        seen_git=0
        in_diff=0
      else
        # Nothing preceded the `--`: git consumes it and applies the
        # two-operand test to the words after it. Count those instead.
        after_dashdash=1
      fi
      continue
    fi
    if ((after_dashdash)); then
      # Git does not parse options here: `-x` after `--` is a path named -x.
      ((operands++))
      path_inside_worktree "${word}" || unresolved=1
      if ((operands >= 2 && unresolved)); then
        refuse "${DIFF_MSG}"
      fi
      continue
    fi
    # `-` is not an option here. Git diff reads it as the stdin operand and
    # counts it toward the same two-operand test, so `git diff /etc/shadow -`
    # prints the file with one flagless operand and one dash -- while a scan
    # that skips every dash-prefixed word sees a single operand and never
    # reaches the refusal. It is the one word git treats as an operand and a
    # loop like this one would treat as an option: every other `-x` is a flag
    # git would reject if it were not one.
    [[ "${word}" == -* && "${word}" != "-" ]] && continue
    ((operands++))
    git rev-parse --verify --quiet "${word}^{commit}" >/dev/null 2>&1 || unresolved=1
    if ((operands >= 2 && unresolved)); then
      refuse "${DIFF_MSG}"
    fi
    continue
  fi

  if ((seen_git)); then
    if ((skip_git_option_value)); then
      # The value half of a two-token git global option. Without this the
      # directory or setting is read as the subcommand, git is forgotten, and
      # the operand scan never starts at all: `git -C / diff /dev/null
      # etc/shadow` would go through uninspected. No allow rule matches that
      # spelling today, so it prompts -- but a gate whose coverage depends on
      # an allow rule's exact prefix is one allow-list edit from silence.
      skip_git_option_value=0
      continue
    fi
    case "${word}" in
    # The git-level options that load a program or move git somewhere else.
    # Stepping over them was enough while the only question was where the
    # subcommand is; it is not, because each reaches past the words an allow
    # rule matched: `-c diff.external=` runs a program per changed path, and
    # `-C <dir>` makes the containment test below answer about a directory
    # git has already left. Both attached and separated values are the same
    # option to git (`-C/tmp`, `-ccolor.ui=false`), so the pattern is the
    # prefix; in this position no other git option begins with `-c` or `-C`.
    -c* | -C* | --config-env | --config-env=* | --exec-path | --exec-path=*)
      refuse "${GIT_GLOBAL_MSG}"
      ;;
    --git-dir | --work-tree | --namespace | --super-prefix | --attr-source)
      skip_git_option_value=1
      continue
      ;;
    esac
    # git-level options such as --no-pager sit between `git` and the subcommand.
    [[ "${word}" == -* ]] && continue
    if [[ "${word}" == "diff" ]]; then
      in_diff=1
      operands=0
      unresolved=0
      after_dashdash=0
      continue
    fi
    if [[ "${word}" == "blame" || "${word}" == "annotate" ]]; then
      in_blame=1
      blame_after_dashdash=0
      seen_git=0
      continue
    fi
    seen_git=0
  fi

  if [[ "${word}" == "git" ]]; then
    seen_git=1
    in_git=1
  fi
done

# A gated command behind a program this hook does not step over.
#
# `is_wrapper` names the wrappers the name scan steps over, but no list of
# programs that run their arguments as a command is complete: `setarch
# x86_64 gh pr view 1 --jq env`, `strace -o /dev/null gh ...`, `prlimit git
# diff ...` and `setpriv git diff ...` run the words after them, and `sh -c
# '...'`, `script -qc '...'` and `eval '...'` hand a shell a whole string.
# The git scans above open at any `git` word, so the plain-file read behind
# `setarch` was already refused, but the gh filter rule reads gh only as a
# command's name, and a command string is one word to every scan here, so
# `setarch x86_64 gh pr view 1 --json number --jq env` printed GH_TOKEN and
# `script -qc 'git diff /dev/null ./.env' /dev/null` printed the file. None
# of them matches an allow rule, so the default mode prompts, but bypass and
# auto modes prompt for nothing, and the fleet settled that every gate
# refuses the protected action behind any program
# (Danathar/atomic-image-builder#781).
#
# So in a command whose name is neither git nor the start of a gated prefix,
# each later word where git or a gated prefix starts is read as a command
# of its own, from that word to the end of the command, and so is each
# command string a later word carries: the word itself when it holds more
# than one word, the value after a long option's `=`, and every tail of a
# short-option cluster (`-qc'git diff ...'`). Such a string is unquoted the
# way bash would hand it over, `$'...'` included, and skipped when it is not
# valid shell, since a shell handed it runs nothing. Each is read by running
# this hook on it alone, inside the same budget, and the command is refused
# when that reading refuses -- so `setarch x86_64 git diff HEAD` and
# `sh -c 'git status'` stay allowed. What the program carries for the
# command it runs -- an assignment before it, an export earlier in the
# string, a redirection -- is held to the rules git and the gated prefixes
# are held to. A program that only prints or matches its words
# (`ARGUMENT_ONLY_PROGRAMS`), typed as its bare name, `/usr/bin/<name>` or
# `/bin/<name>`, is not read at all, so `echo gh pr view 1 --jq env` and
# `grep -rn 'git diff --no-index' docs` stay allowed. A command name built at
# runtime behind such a program (`setarch x86_64 $G diff ...`) is not read;
# it is one of the residuals the header names.
ARGUMENT_ONLY_PROGRAMS=(echo printf grep egrep fgrep)

# The first word of each gated command, for a cheap test before the full one:
# as a list, and as one space-delimited string to match a word against.
gated_first_words=(git)
for prefix in "${GATED_PREFIXES[@]}"; do
  gated_first_words+=("${prefix%% *}")
done
GATED_FIRST_WORDS=" ${gated_first_words[*]} "

# The characters that separate the words of a command string, besides
# whitespace: the shell's own punctuation and the quote marks.
COMMAND_STRING_SEPARATORS=';&|()<>`'"'"'"'

# shellcheck disable=SC2016 # the backticks quote command spellings for the reader
UNLISTED_TAIL='That program is not one this hook steps over (command, env, nice, nohup, timeout and the other wrappers the name scan reads), so no allow rule covers what it runs and Claude Code prompts for it only in the default mode; bypass and auto modes prompt for nothing, so a command such a program would run -- the words from a git or allow-listed name onward (`setarch x86_64 gh pr view ...`, `strace -o log git diff ...`) or a command string it hands a shell (`sh -c '"'"'...'"'"'`, `script -qc '"'"'...'"'"'`) -- is read as though it stood alone. echo, printf and grep only print or match their words and are not read. Run the command on its own instead.'

# Whether git or a gated prefix starts at word $1 of the command string.
gated_name_at() {
  local at="$1" base="${words[$1]##*/}" prefix k
  local -a parts
  [[ "${base}" == git ]] && return 0
  [[ "${GATED_FIRST_WORDS}" == *" ${base} "* ]] || return 1
  for prefix in "${GATED_PREFIXES[@]}"; do
    read -ra parts <<<"${prefix}"
    [[ "${base}" == "${parts[0]}" ]] || continue
    for ((k = 1; k < ${#parts[@]}; k++)); do
      [[ "${kinds[at + k]:-}" == word && "${words[at + k]}" == "${parts[k]}" ]] || continue 2
    done
    return 0
  done
  return 1
}

# Whether a command string names git or a gated prefix at any of its words,
# cut at whitespace and COMMAND_STRING_SEPARATORS. Sets `text_git` when the
# name is git, whose stdin rule then applies.
#
# The shell the string is handed to removes quotes and backslashes and
# expands braces and globs before it looks a name up, so `g""it`, `g\it`,
# `g$""it`, `g{,}it`, `{git,diff}` and `/usr/bin/g?t` all run git. The string
# is read again with those quote characters taken out, once with the braces
# and commas taken out (`g{,}it` is `git`) and once with them as separators
# (`{git,diff}` is `git diff`), and a word holding a glob character is
# matched as the pattern it is. A name built from a variable is not found,
# as the header says.
text_gated_name() {
  local spelled="${1//[\"\'\\\$]/}" text
  for text in "$1" "${spelled//[\{\},]/}" "${spelled//[\{\},]/ }"; do
    text_names_gated "${text}" && return 0
  done
  return 1
}

# Whether a word cut from a command string is the name $2: as itself, or as
# a glob that matches it, an extglob pattern (`g@(i)t`) included, which
# `[[ == ]]` matches as extglob whatever the shell option says.
piece_names() {
  [[ "$1" == "$2" ]] && return 0
  # shellcheck disable=SC2053 # the word is matched as a pattern on purpose
  [[ "$1" == *[*?[]* || "$1" == *[@+!?*]'('* ]] && [[ "$2" == $1 ]]
}

# The words of a string that hold an extglob pattern, into `extglob_found`,
# each cut where bash would end it: at whitespace or an operator outside the
# pattern's parentheses, not inside them. The cut at
# COMMAND_STRING_SEPARATORS below splits `/usr/bin/g@(i)t` into `/usr/bin/g@`,
# `i` and `t`, none of which is git.
extglob_words() {
  local text="$1" k ch depth=0 word=''
  extglob_found=()
  for ((k = 0; k < ${#text}; k++)); do
    ch="${text:k:1}"
    if ((depth)); then
      word+="${ch}"
      if [[ "${ch}" == '(' ]]; then
        depth=$((depth + 1))
      elif [[ "${ch}" == ')' ]]; then
        depth=$((depth - 1))
      fi
      continue
    fi
    case "${ch}" in
    [$' \t\n;&<>`|'])
      [[ "${word}" == *[@+!?*]'('* ]] && extglob_found+=("${word}")
      word=''
      ;;
    '(')
      [[ "${word}" == *[@+!?*] ]] && depth=1
      word+="${ch}"
      ;;
    *) word+="${ch}" ;;
    esac
  done
  [[ "${word}" == *[@+!?*]'('* ]] && extglob_found+=("${word}")
  return 0
}

text_names_gated() {
  local at base prefix k first word
  local -a pieces parts
  text_git=0
  if [[ "$1" == *[@+!?*]'('* ]]; then
    extglob_words "$1"
    for word in "${extglob_found[@]+"${extglob_found[@]}"}"; do
      if piece_names "${word##*/}" git; then
        text_git=1
        return 0
      fi
      for first in "${gated_first_words[@]}"; do
        piece_names "${word##*/}" "${first}" && return 0
      done
    done
  fi
  IFS=$' \t\n' read -r -d '' -a pieces <<<"${1//["${COMMAND_STRING_SEPARATORS}"]/ }" || true
  for ((at = 0; at < ${#pieces[@]}; at++)); do
    base="${pieces[at]##*/}"
    if piece_names "${base}" git; then
      text_git=1
      return 0
    fi
    for prefix in "${GATED_PREFIXES[@]}"; do
      read -ra parts <<<"${prefix}"
      piece_names "${base}" "${parts[0]}" || continue
      for ((k = 1; k < ${#parts[@]}; k++)); do
        [[ "${pieces[at + k]:-}" == "${parts[k]}" ]] || continue 2
      done
      return 0
    done
  done
  return 1
}

# Whether a word, as typed, is one the shell turns into git or a gated
# prefix only by expanding it: a brace group (`{git,diff}` is `git diff`), a
# glob that holds a `/` (`/usr/bin/g?t` is `/usr/bin/git`), or an unquoted
# glob with no `/` ($2 is 1): `g?t` matches a file named git in the working
# directory, and the program then looks that name up on PATH and runs the
# real git, so `touch git; setarch x86_64 g?t diff ...` runs git diff. Sets
# `expansions` to each set of words the shell may make of it -- one per gated
# name the glob matches, since `*` matches git and gh alike -- so the command
# can be read from there as the shell will run it: `ls /usr/bin/g*` reads
# `git` alone, which passes, rather than a glob in command position, which is
# refused. A quoted pattern is a literal to bash, so
# `find . -name '*.sh'` is not read as the `*.sh` command.
expands_to_gated_name() {
  local spelled="${1//[\"\'\\\$]/}" first expanded
  expansions=()
  if [[ "$1" == *"{"* ]]; then
    for expanded in "${spelled//[\{\},]/}" "${spelled//[\{\},]/ }"; do
      if text_names_gated "${expanded}"; then
        expansions=("${expanded}")
        return 0
      fi
    done
  fi
  if [[ "$1" == */* && "$1" == *[*?[]* ]] || ((${2:-0})); then
    for first in "${gated_first_words[@]}"; do
      piece_names "${spelled##*/}" "${first}" && expansions+=("${first}")
    done
  fi
  ((${#expansions[@]}))
}

# The word a program receives for a word as typed, in `unquoted`: quotes
# removed and backslash escapes undone the way bash does both, and an ANSI-C
# `$'...'` decoded with printf's own escapes, which are bash's for that form
# (`\xHH`, `\NNN`, `\uHHHH`, `\n`, `\t`, ...). The spellings kept by the split
# above drop every quote mark and backslash, which reads `'.[] | .name'`
# inside a double-quoted string as an unquoted glob and `$'\x67it'` as no
# git at all.
unquote_word() {
  local raw="$1" ch next quote='' ansi='' decoded i
  unquoted=''
  for ((i = 0; i < ${#raw}; i++)); do
    ch="${raw:i:1}"
    if [[ "${quote}" == ansi ]]; then
      if [[ "${ch}" == $'\\' ]]; then
        ansi+="${ch}${raw:i+1:1}"
        ((i++))
      elif [[ "${ch}" == "'" ]]; then
        # shellcheck disable=SC2059 # the escapes are the format's own, and every % is doubled
        printf -v decoded -- "${ansi//%/%%}"
        unquoted+="${decoded}"
        quote=''
      else
        ansi+="${ch}"
      fi
      continue
    fi
    if [[ "${quote}" == "'" ]]; then
      if [[ "${ch}" == "'" ]]; then quote=''; else unquoted+="${ch}"; fi
      continue
    fi
    if [[ "${ch}" == $'\\' ]]; then
      next="${raw:i+1:1}"
      if [[ "${quote}" == '"' && "${next}" != [\$\`\"\\] ]]; then
        unquoted+="${ch}"
      else
        unquoted+="${next}"
        ((i++))
      fi
      continue
    fi
    if [[ "${quote}" == '"' ]]; then
      if [[ "${ch}" == '"' ]]; then quote=''; else unquoted+="${ch}"; fi
      continue
    fi
    case "${ch}" in
    "'" | '"') quote="${ch}" ;;
    '$')
      if [[ "${raw:i+1:1}" == "'" ]]; then
        quote=ansi
        ansi=''
        ((i++))
      else
        unquoted+='$'
      fi
      ;;
    *) unquoted+="${ch}" ;;
    esac
  done
}

# The command strings an unquoted word may hand to a shell, in `texts`: the
# word itself when it holds more than one word of its own, the value after a
# long option's `=`, and every tail of a short-option cluster, since which
# letter takes a value is not modelled and trying each can only over-refuse.
#
# Each string is read a second time with its backslash-newline pairs
# removed, when it has any. The shell the string is handed to drops a
# `\<newline>` outside single quotes before it reads a word, so
# `bash -c 'gi\<newline>t diff ...'` runs git while neither `gi` nor `t` is a
# gated name; the single quotes around the string kept the pair from the
# join this hook makes of the command it was given. The second reading can
# only add a refusal.
command_string_texts() {
  local word="$1" cut k count
  texts=()
  [[ "${word}" == *["${COMMAND_STRING_SEPARATORS}"$' \t\n']* ]] && texts+=("${word}")
  if [[ "${word}" == --* ]]; then
    [[ "${word}" == *=* ]] && texts+=("${word#*=}")
  elif [[ "${word}" =~ ^-[A-Za-z0-9]+ ]]; then
    for ((cut = 2; cut <= ${#BASH_REMATCH[0]}; cut++)); do
      texts+=("${word:cut}")
    done
  fi
  count=${#texts[@]}
  for ((k = 0; k < count; k++)); do
    [[ "${texts[k]}" == *$'\\\n'* ]] && texts+=("${texts[k]//$'\\\n'/}")
  done
  return 0
}

# The command that starts at word $1, as typed, up to the end of its own
# command: a `$(...)`, a backtick or a process substitution inside it is
# carried along, and the separator that ends it is where it stops. $2 is 1
# when the command is itself inside a backtick, whose closing backtick ends
# it.
command_text_from() {
  local k depth=0 tick=0 w
  command_text=''
  for ((k = $1; k < ${#words[@]}; k++)); do
    w="${words[k]}"
    case "${kinds[k]}" in
    sep)
      # shellcheck disable=SC2016 # the literal `$(` is the separator's name
      if [[ "${w}" == '$(' ]]; then
        ((depth++))
        command_text+='('
        continue
      fi
      if [[ "${w}" == '(' && "${raw_words[k - 1]:-}" == [\<\>]'(' ]]; then
        ((depth++))
        continue
      fi
      if [[ "${w}" == '$)' || "${w}" == ')' ]] && ((depth > 0)); then
        ((depth--))
        command_text+=' )'
        continue
      fi
      if [[ "${w}" == '`' ]] && ((tick || $2 == 0)); then
        ((tick = !tick))
        command_text+=' `'
        continue
      fi
      ((depth > 0 || tick)) || break
      command_text+=" ${w}"
      ;;
    target) command_text+=" ${redirects[k]}${raw_words[k]}" ;;
    *) command_text+=" ${raw_words[k]}" ;;
    esac
  done
  command_text="${command_text# }"
}

# Run this hook on one command alone, inside the budget this run is already
# under, and refuse when that run does. Any exit but 0 is a refusal: the
# hook fails closed on a reading it could not finish.
read_alone() {
  local inner="$1" reason status
  # shellcheck disable=SC2069 # stderr is the reason to keep, stdout is dropped
  reason="$(jq -cn --arg c "${inner}" '{tool_name: "Bash", tool_input: {command: $c}}' |
    "${BASH:-bash}" "${gate_self}" --within-budget 2>&1 >/dev/null)"
  status=$?
  ((status == 0)) && return 0
  reason="${reason#blocked: }"
  refuse "blocked: ${seg_program} runs \`${inner}\` as a command of its own, and that command is refused here on its own: ${reason:-this hook could not finish reading it.} ${UNLISTED_TAIL}"
}

refuse_behind() {
  refuse "blocked: ${seg_program} runs a command this hook covers as a command of its own, so what is written on ${seg_program} for it is held to the same rules: ${1#blocked: } ${UNLISTED_TAIL}"
}

# What is written on the program for the command it runs, decided when the
# command ends, because a redirection can follow the word it applies to.
finish_segment() {
  ((seg_hit)) || return 0
  ((seg_assign)) && refuse_behind "${GATED_ENV_MSG}"
  ((export_idx >= 0 && seg_hit_idx > export_idx)) && refuse_behind "${EXPORT_ENV_MSG}"
  if ((seg_writes)); then
    ((seg_git)) && refuse_behind "${REDIRECT_MSG}"
    refuse_behind "${GATED_REDIRECT_MSG}"
  fi
  ((seg_git && seg_reads)) && refuse_behind "${STDIN_MSG}"
  return 0
}

reset_segment() {
  seg_named=0   # the command's name has been seen
  seg_reading=0 # the name is a program whose later words are read
  seg_assign=0  # an assignment stands before the name
  seg_hit=0     # a gated command runs behind it
  seg_hit_idx=-1
  seg_git=0    # and that command is git
  seg_writes=0 # a redirection of this command opens a path for writing
  seg_reads=0  # or puts a file on stdin
  seg_program=''
}

seg_stack=() # the outer command's state while a nested one is read
reset_segment
for ((idx = 0; idx < ${#words[@]}; idx++)); do
  word="${words[idx]}"
  case "${kinds[idx]}" in
  sep)
    top=''
    ((${#seg_stack[@]})) && top="${seg_stack[-1]%% *}"
    opens=''
    # shellcheck disable=SC2016 # the literal `$(` is the separator's name
    if [[ "${word}" == '$(' ]]; then
      opens=s
    elif ((idx > 0)) && [[ "${word}" == '(' && "${raw_words[idx - 1]}" == [\<\>]'(' ]]; then
      opens=p
    elif [[ "${word}" == '`' && "${top}" != b ]]; then
      opens=b
    fi
    if [[ -n "${opens}" ]]; then
      seg_stack+=("${opens} ${seg_named} ${seg_reading} ${seg_assign} ${seg_hit} ${seg_hit_idx} ${seg_git} ${seg_writes} ${seg_reads} ${seg_program}")
      reset_segment
      continue
    fi
    finish_segment
    if [[ (-n "${top}") && ("${word}" == '$)' && "${top}" == s || "${word}" == ')' && "${top}" == p || "${word}" == '`' && "${top}" == b) ]]; then
      read -r _ seg_named seg_reading seg_assign seg_hit seg_hit_idx seg_git seg_writes seg_reads seg_program <<<"${seg_stack[-1]}"
      unset 'seg_stack[-1]'
      continue
    fi
    reset_segment
    continue
    ;;
  target)
    redirection_writes_a_path "${redirects[idx]}" "${word}" && seg_writes=1
    redirection_reads_a_path "${redirects[idx]}" "${word}" && seg_reads=1
    continue
    ;;
  *) ;;
  esac
  if ((!seg_named)); then
    if ((${command_names[idx]:-0} == 0)); then
      ((${name_assignments[idx]:-0})) && seg_assign=1
      continue
    fi
    seg_named=1
    seg_program="${raw_words[idx]}"
    seg_reading=1
    gated_name_at "${idx}" && seg_reading=0
    typed="${raw_words[idx]//[\'\"]/}"
    for program in "${ARGUMENT_ONLY_PROGRAMS[@]}"; do
      case "${typed}" in
      "${program}" | "/usr/bin/${program}" | "/bin/${program}") seg_reading=0 ;;
      *) ;;
      esac
    done
    continue
  fi
  ((seg_reading)) || continue
  expansions=()
  if gated_name_at "${idx}" || expands_to_gated_name "${raw_words[idx]}" "${globs[idx]:-0}"; then
    seg_hit=1
    ((seg_hit_idx < 0)) && seg_hit_idx=${idx}
    [[ "${word##*/}" == git ]] && seg_git=1
    for expanded in "${expansions[@]+"${expansions[@]}"}"; do
      [[ "${expanded%% *}" == git ]] && seg_git=1
    done
    in_tick=0
    ((${#seg_stack[@]})) && [[ "${seg_stack[-1]}" == 'b '* ]] && in_tick=1
    command_text_from "${idx}" "${in_tick}"
    if ((${#expansions[@]})); then
      for expanded in "${expansions[@]}"; do
        read_alone "${expanded}${command_text#"${raw_words[idx]}"}"
      done
    else
      read_alone "${command_text}"
    fi
  fi
  # A command string, looked for only in a word that names a gated command
  # somewhere in it, or that carries an ANSI-C quote that could spell one.
  candidate=0
  # The word as typed with the quote characters the shell removes taken out
  # (`g$""it` is `git`), and any glob, which may match a gated name it does
  # not spell (`/usr/bin/g?t`).
  # A backslash-newline pair is dropped first, as the shell a command string
  # is handed to drops it (`'gi\<newline>t'` is `git` there).
  joined="${raw_words[idx]//$'\\\n'/}"
  spelled="${joined//[\"\'\\\$\{\},]/}"
  for first in "${gated_first_words[@]}"; do
    [[ "${word}" == *"${first}"* || "${spelled}" == *"${first}"* ]] && candidate=1
  done
  [[ "${raw_words[idx]}" == *"\$'"* || "${raw_words[idx]}" == *[*?[]* ]] && candidate=1
  [[ "${joined}" == *[@+!?*]'('* ]] && candidate=1
  ((candidate)) || continue
  unquote_word "${raw_words[idx]}"
  command_string_texts "${unquoted}"
  for text in "${texts[@]+"${texts[@]}"}"; do
    [[ -n "${text}" ]] || continue
    text_gated_name "${text}" || continue
    "${BASH:-bash}" -O extglob -n -c "${text}" 2>/dev/null || continue
    seg_hit=1
    ((seg_hit_idx < 0)) && seg_hit_idx=${idx}
    ((text_git)) && seg_git=1
    read_alone "${text}"
  done
done
finish_segment

exit 0
