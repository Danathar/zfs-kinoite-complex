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
# Six things this gate has to get right, each of them a spelling that a
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
#      alone. The brace test reads the words *as typed*, quotes and all,
#      before the normalization below strips quotes and splits on operators:
#      `{a';',b}` is one word to bash and two paths after expansion, and a
#      test run after the split saw `{a` and `,b}` and passed both. See
#      `raw_words`, `brace_would_expand` and `BRACE_MSG`.
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
# So this looks at the operands git would actually receive, and refuses the
# two-operand form unless every operand resolves as a revision -- which is what
# separates `git diff main feature` from `git diff /dev/null ./cosign.key`.
# After a bare `--` no word can be a revision, so there the test is git's own:
# two or more words where any one lies outside the working tree. The write
# primitive needs none of that machinery: `--output` anywhere in a git
# invocation is refused outright.
#
# What it still cannot see, stated rather than implied: a command that hides a
# git invocation behind another interpreter (`sh -c ...`), one that changes
# directory out of the repository first, and anything a command reads or writes
# once it has started. A git argument built at runtime (`git diff $x $y`,
# `$(...)`, a backtick, `$'\x74'`) is no longer waved through -- every `$` and
# backtick in a word of a git invocation is refused, see `EXPAND_MSG` -- but
# that is a refusal, not an inspection. This re-gates the pre-approved commands
# that reach past the deny list; it is not a sandbox.

set -uo pipefail

refuse() {
  printf '%s\n' "$1" >&2
  exit 2
}

# shellcheck disable=SC2016 # the message quotes shell spellings as literal
# text -- $'\x74' and $(...) are what the reader has to see, not what this
# script should expand.
EXPAND_MSG='blocked: bash expands ANSI-C quotes and substitutions before git sees the words, and this gate reads the words as typed, so two characters rebuild both spellings it refuses: `git diff $(...)` and a backtick supply operands the operand scan never saw (the plain-file read), and `--outpu$'"'"'\x74'"'"'=FILE` matches no word here and reaches git as --output=FILE. Expanding them correctly means reimplementing bash inside a hook, so every $ and backtick in a word of a git invocation is refused instead. Write the command out in full. Only words of a git invocation are affected: an awk or jq program elsewhere in the string is not, unless it carries a backtick after a git word.'

DIFF_MSG='blocked: this git diff would compare paths as plain files (git'"'"'s --no-index mode, which needs no flag once two operands are given), so it prints any file on disk -- the signing key, a .env, a private key outside this repository -- past the Read(...) deny rules in .claude/settings.json. Describe such a file with ls -l or wc -c instead.'

OUT_MSG='blocked: git --output=FILE (and the space form) writes this diff or log to the path it names instead of stdout, overwriting any file this uid can reach -- cosign.pub, ci/inputs.lock.json, .claude/settings.json, this hook, ~/.ssh/authorized_keys -- with no Read(...) or Write(...) deny rule in its way. git diff, git log and git show print to stdout; read that instead. --output-indicator-* is a different flag and is unaffected.'

# shellcheck disable=SC2016 # the literal ${VAR} is what the reader has to see
BRACE_MSG='blocked: bash expands braces before git sees the words, and this gate reads the words as typed, so a brace rebuilds both spellings it refuses: `git diff {/dev/null,./cosign.key}` passes the operand scan as one word and reaches git as two operands (the plain-file read), and `--outpu{t,t}=FILE` matches no word here and reaches git as --output=FILE. Expanding braces correctly means reimplementing bash inside a hook, so a brace bash could expand -- a { followed, anywhere later in the word, by a comma or a .. and then a }, or a ${VAR} -- is refused instead. Write the command out in full. A brace with neither, such as HEAD@{1} or main@{upstream}, is a literal to bash and is not refused; a .. between two reflog entries (HEAD@{2}..HEAD@{1}) has the refused shape, so write HEAD~2..HEAD~1. Only words of a git invocation are affected: awk and jq programs elsewhere in the string are not.'

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
# `{a",",b}` and `{a';',b}` both become two words -- and the normalization
# below would strip the quotes and cut the second at its `;`, leaving
# `{a` and `,b}` for a per-word test to wave through. A fully quoted
# `"{a,b}"`, which bash leaves alone, is refused as the price of that.
brace_would_expand() {
  # shellcheck disable=SC2016 # the literal `${` is what is being looked for
  [[ "$1" == *'${'* || "$1" == *'{'*','*'}'* || "$1" == *'{'*..*'}'* ]]
}

# The command's words as bash would delimit them, and nothing else done to
# them: split on unquoted whitespace and unquoted operator characters, with
# every quote mark and backslash kept in place. Bash brace-expands exactly
# these words, so `brace_would_expand` has to see them in this form; the
# normalized words below have lost their quotes and been cut at operators.
# An unquoted command separator -- `;`, `&`, `|`, `(`, `)`, a newline, a
# backtick -- leaves an empty entry behind it as the boundary marker the
# brace scan resets on; a redirection character (`<`, `>`) ends a word but
# not a command. No real word is ever empty here: a typed `""` keeps its
# quotes.
raw_words=()
raw_word=''
raw_quote=''
raw_escaped=0
for ((i = 0; i < ${#command_string}; i++)); do
  ch="${command_string:i:1}"
  if ((raw_escaped)); then
    raw_word+="${ch}"
    raw_escaped=0
    continue
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
  case "${ch}" in
  $'\\')
    raw_escaped=1
    raw_word+="${ch}"
    ;;
  "'" | '"')
    raw_quote="${ch}"
    raw_word+="${ch}"
    ;;
  ' ' | $'\t' | '<' | '>')
    [[ -n "${raw_word}" ]] && raw_words+=("${raw_word}")
    raw_word=''
    ;;
  $'\n' | ';' | '&' | '|' | '(' | ')' | '`')
    [[ -n "${raw_word}" ]] && raw_words+=("${raw_word}")
    raw_word=''
    raw_words+=('')
    ;;
  *) raw_word+="${ch}" ;;
  esac
done
[[ -n "${raw_word}" ]] && raw_words+=("${raw_word}")

# From a `git` word to the end of *that command*: the scope opens at `git`
# and closes at the next unquoted separator, so `git diff HEAD | jq '{a,b}'`
# leaves the jq program alone while `git log -1; git diff {a,b}` and
# `echo x | git diff {a,b}` are each refused at their own `git`. This is
# narrower than the `in_git` latch below, which holds to the end of the
# string, and can be: that latch stays up because the normalized split cuts
# a *quoted* operator inside an argument, and these words keep their quotes,
# so the only separators here are the ones bash itself honours. The word is
# compared with its quotes removed so `'git'` opens the scope as `git` does;
# a `git` assembled from an expansion (`g{i,i}t`) matches no allow rule and
# prompts on its own.
raw_in_git=0
for raw_word in "${raw_words[@]+"${raw_words[@]}"}"; do
  if [[ -z "${raw_word}" ]]; then
    raw_in_git=0
    continue
  fi
  if ((raw_in_git)) && brace_would_expand "${raw_word}"; then
    refuse "${BRACE_MSG}"
  fi
  [[ "${raw_word//[\'\"\\]/}" == "git" ]] && raw_in_git=1
done

# Match the word git receives, not the spelling typed: the shell removes
# quoting and backslashes on the way.
normalized="${command_string//[\'\"\\]/}"
normalized="${normalized//$'\n'/ }"
normalized="${normalized//$'\t'/ }"

case "${normalized}" in
*--no-index*) refuse "${DIFF_MSG}" ;;
esac

# Shell operators need no whitespace around them, and this scan splits on
# whitespace alone. `git log -1 && (git log -p --output=cosign.pub -1)`
# tokenizes as `(git`, which is not the word `git`, so the second command would
# never be recognized as a git invocation and every test below would stay
# switched off for it -- the hook exits 0 while the trust anchor is
# overwritten. The read half has the same hole: `ls&&git diff /dev/null
# ./cosign.key`. Give each operator character whitespace of its own, so a
# command written hard against one is still a command here.
for operator_char in '(' ')' ';' '&' '|' '`'; do
  normalized="${normalized//"${operator_char}"/ ${operator_char} }"
done

read -r -a words <<<"${normalized}"

# Git's path_inside_repo, which decides on the *spelling* rather than on where
# the path ends up. That distinction is the whole of this function, and folding
# `..` before the comparison gets it backwards: `git diff -- ../<checkout>/
# cosign.key -` names a file inside this repository by a route that leaves it
# and comes back, git's test calls that outside and enters the plain-file mode,
# and a gate that resolved the path first saw a tidy in-tree path and allowed
# it -- reading a denied path with two operands that both look local. So an
# absolute path, any `..` component, and the stdin operand `-` each count as
# outside here, and only a plain relative path is resolved at all. Anything
# this cannot decide -- no working tree, no realpath on the host -- counts as
# outside too, so the gate refuses rather than guesses.
path_inside_worktree() {
  local candidate toplevel
  case "$1" in
  - | /*) return 1 ;;
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

seen_git=0
in_git=0
in_diff=0
operands=0
unresolved=0
after_dashdash=0
skip_git_option_value=0

for word in "${words[@]+"${words[@]}"}"; do
  case "${word}" in
  ';' | '&&' | '||' | '|' | '&' | '(' | ')' | '`')
    # The operand scan starts over at each command boundary. `in_git` does not:
    # it latches for the rest of the command string. Splitting on operator
    # characters above means one sitting inside an argument -- `git log
    # --grep=a|b --output=cosign.pub -1` -- would otherwise end the git
    # invocation as far as this scan is concerned and hand the write primitive
    # back unwatched. The cost is refusing an `--output` that belongs to some
    # later non-git command in the same string; the alternative is a bypass
    # spelled with one pipe.
    seen_git=0
    in_diff=0
    skip_git_option_value=0
    continue
    ;;
  esac

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
  # typed, because the normalization that produced these words strips the
  # quotes that keep `{a';',b}` one word and cuts it at the `;`.
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
    -C | -c | --git-dir | --work-tree | --namespace | --super-prefix | --config-env | --attr-source)
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
    seen_git=0
  fi

  if [[ "${word}" == "git" ]]; then
    seen_git=1
    in_git=1
  fi
done

exit 0
