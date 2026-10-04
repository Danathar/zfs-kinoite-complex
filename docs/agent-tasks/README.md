# Agent Tasks: From A Change Back To Its Task

If a term is unfamiliar, check the shared glossary first:
[`docs/glossary.md`](../glossary.md)

This page answers one question: **an agent changed something here; which task
was it working on, and who asked?**

Every answer is already in GitHub and in git. This page says where to look and
gives the commands. It does not add a log.

## There Is No Task Log, On Purpose

This repository keeps no per-task record of its own. There is no `.agent/tasks/`
directory and no `.github/agent-log/` directory, and this directory holds only
this page.

The trail already exists in places that cannot drift from what happened: the
issue, the pull request, the commit, and the merge commit on `main`. A log kept
by hand next to them would be a second copy. Someone would forget to update it,
and from then on it would be wrong. That is the same reason
[`docs/documentation-guide.md`](../documentation-guide.md) prefers a link to a
copy.

## What Links A Change To Its Task

Follow these from the code outward. Each one points at the next.

| Link | Where it lives | What it tells you |
| --- | --- | --- |
| Merge commit | `git log` on `main` | Which pull request put the change on `main` |
| Pull request | GitHub | The agent that wrote it and the issue it closes |
| `Closes #N` | The pull request body | The task: the issue that asked for the work |
| Issue | GitHub | Who filed the task, and what they asked for |

### The Hive signature line

Agents here run in an outside system called Hive. Hive is not part of this
repository. Its agents open pull requests as `app/danathar-atomic-hive`.

Hive writes a signature as the last line of the issues and pull requests it
opens. It looks like this:

```text
— hive: agent=quality backend=claude model=claude-opus-5-5 effort=medium claude=2.1.287
```

- `agent` is the role that wrote it, for example `quality`, `architect`,
  `guide`, `scanner`, `sec-check`, `ci-maintainer` or `strategist`.
- `backend` and `model` say which tool and model ran.
- `effort` and `claude` say the effort level and the tool version. `effort` is
  not on every line.
- Issues that Hive's dashboard opens carry a shorter line:
  `— hive: agent=dashboard`.

Hive issues also carry a line just above it that names the instance and a
commit SHA. Do not treat the SHA as a pointer into this repository: some read
`unknown`.

The signature is **a hint, not proof**. Not every Hive pull request carries one,
and anyone can type that line into a comment. Check the author, not the text.

### The branch prefix

The first part of the branch name names the role. These pairs were read from
pull requests opened by `app/danathar-atomic-hive`:

| Branch prefix | `agent=` in the signature |
| --- | --- |
| `quality/` | `quality` |
| `arch/` and `architect/` | `architect` |
| `guide/` | `guide` |
| `scanner/` | `scanner` |
| `sec/` | `sec-check` |
| `ci/` | `ci-maintainer` |

A prefix is a habit, not a rule. Nothing in the repository enforces it, and the
maintainer uses some of the same prefixes (`fix/`, `sec/`, `docs/`) from their
own account. So read the prefix together with the pull request author. The
command to see the real pairs today is in the next section.

Branches made through the in-repo path below start with `ai-fix/`.

### `Closes #N`

GitHub closes an issue when a pull request whose body says `Closes #N` is
merged. Hive's pull requests put it under a `## Related Issue` heading. The
pull request template in
[`.github/pull_request_template.md`](../../.github/pull_request_template.md)
has no such heading, so for a pull request written by hand the link is only
there if the author added it.

If a pull request has no such line, it has no recorded task. Say so rather than
guessing one.

### The commit trailer

Some of Hive's commits are authored by the role, for example
`quality <quality@hive.kubestellar.io>`, and end with a `Signed-off-by:` line
naming the same role. Many are not: commits on agent branches also carry the
Hive app's identity (`danathar-atomic-hive[bot]`) or the maintainer's own name,
so a commit author is weaker evidence than the pull request author. `git log`
shows it without GitHub:

```sh
git log -1 --format='%an <%ae>%n%b' <sha>
```

### The merge commit on `main`

The maintainer merges with merge commits, so the subject reads
`Merge pull request #N from Danathar/<branch>`. That names the pull request and
the branch in one line.

This is how things are done, not a setting. The ruleset allows `merge`, `squash`
and `rebase` (see [`.github/rulesets/main.json`](../../.github/rulesets/main.json)
and [`docs/branch-protection.md`](../branch-protection.md)). A squashed or rebased
change has no such subject. The GitHub lookup below works for all three.

## The In-Repo Path: `ai-fix.yml`

Most agent work reaches the repository from Hive. One path starts inside it:
[`.github/workflows/ai-fix.yml`](../../.github/workflows/ai-fix.yml).

- A person with write access labels an issue `ai-fix-requested`, or writes
  `@claude` in an issue or pull request comment.
- The agent works on a branch whose name starts with `ai-fix/` and opens a pull
  request. It never merges one.
- Runs started by a bot are skipped. That includes the bot that adds
  `ai-fix-requested` to Hive's issues, so a label from Hive starts nothing.
  A maintainer relays the request with `@claude`.
- The request is the trace. The label or comment sits on the issue or pull
  request that asked. The run it started is in the workflow's run list.

What the workflow may and may not do is covered in
[`docs/SECURITY-AI.md`](../SECURITY-AI.md). This page only says how to find the
run.

## Commands

These need the [GitHub CLI](https://cli.github.com/) (`gh`), signed in. Replace
`<sha>`, `<N>` and the role. Run them from a clone of this repository, or add
`-R Danathar/zfs-kinoite-complex`.

Find the pull request that put a commit on `main`:

```sh
gh pr list --state merged --search <sha> --json number,title,headRefName,author
```

Without `gh`, use git. For a commit that came in with a pull request,
`--ancestry-path` lists the merge commits between it and `main`. A branch that
merged `main` into itself before it landed has those merges on the path too,
and they come first, so keep only the first pull request merge:

```sh
git log --merges --ancestry-path --reverse --format='%h %s' <sha>..main | grep -m1 ' Merge pull request #'
```

That line is the merge that brought it in.

Find the issue a pull request closes:

```sh
gh pr view <N> --json closingIssuesReferences --jq '.closingIssuesReferences[].number'
```

Read the signature line, and the labels, on a pull request or issue:

```sh
gh pr view <N> --json body,labels --jq '(.body | split("\n") | map(select(length > 0)) | last), [.labels[].name]'
gh issue view <N> --json body --jq '.body | split("\n") | map(select(length > 0)) | last'
```

List one agent's pull requests, newest first:

```sh
gh pr list --state all --author app/danathar-atomic-hive --search '"agent=quality" in:body' --json number,title,headRefName
```

See which branch prefix goes with which role, from the real pull requests:

```sh
gh pr list --state all --author app/danathar-atomic-hive --limit 300 --json headRefName,body \
  --jq '.[] | [(.headRefName | split("/")[0]), ((.body | capture("— hive: agent=(?<a>[a-z-]+)")? | .a) // "none")] | @tsv' | sort | uniq -c
```

List agent branches started by the in-repo path:

```sh
gh pr list --state all --limit 300 --json number,title,headRefName \
  --jq '.[] | select(.headRefName | startswith("ai-fix/"))'
```

Find the workflow runs behind an `ai-fix/` pull request:

```sh
gh run list --workflow ai-fix.yml --limit 20
```

## What `.claude/session-summary.md` Is

[`.claude/session-summary.md`](../../.claude/session-summary.md) is **not** a task
record. It holds state carried between agent sessions: work in flight and
decisions that are not yet visible in a diff. Its own header says it is not a
changelog and that `git log` is a better one.

It has no task numbers, no run IDs and no per-change history. Do not look there
to find out why a change was made. Use the links above. It is the right place
only to ask what the next session was told to pick up.

## Where This Stops

- A change a person made in their own session with an agent may carry no
  signature. Its trace is then the pull request that person opened.
- This page cannot tell you why an agent chose its approach. Read the pull
  request body and the diff for that.
- This page only finds the task. It does not say who the agents are, or what to
  do when one gets it wrong.
