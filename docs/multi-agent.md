# Multiple agents in this repository

If a term is unfamiliar, check the shared glossary first:
[`docs/glossary.md`](./glossary.md)

## Purpose

More than one AI agent opens pull requests here. This page says which ones,
where they run, how work reaches them, how they are kept from colliding, and
who merges. It describes what already happens. It adds no automation.

It does not cover what an agent is *allowed* to do: that is
[`SECURITY-AI.md`](./SECURITY-AI.md). It does not cover what to do when an
agent's output is wrong, or how to trace a change back to its task.

A few words used below:

- **Agent**: an AI coding session that reads the repository and opens a pull
  request.
- **Hive**: an external system, outside this repository, that starts agents and
  files issues for them. Its GitHub App appears as `app/danathar-atomic-hive`.
  Its code and settings are not in this repository, so this page can only
  describe what it leaves behind here.
- **Required check**: a check that must pass before GitHub lets a pull request
  merge.

## There is no orchestrator in this repository

Nothing in this repository decides which agent runs next. Hive does that, from
outside. The ACMM (an external maturity checklist that Hive runs against the
repository) looks for files named like an in-repo orchestrator. None of these
exist here:

```text
.github/workflows/dispatcher.yml
.github/workflows/orchestrate.yml
scripts/orchestrate.mjs
.claude/dispatcher/
orchestrator/
```

That is deliberate, not an omission. A workflow that starts agents on its own
schedule would be an automated way to open pull requests against a repository
where [a merge to `main` publishes a signed image](./SECURITY-AI.md). The
repository's one in-repo agent entry point, `.github/workflows/ai-fix.yml`,
shows the stance: it only starts for a human with write access, refuses to
start from a bot, and opens a pull request but never merges one. Its header
comment gives the reasons. A file added only to satisfy the checklist would
raise the score without changing anything, which is the thing to avoid.

## Which agents appear

These roles are read from the repository's own history, not from a Hive
document. Hive's pull requests and issues end with a signature line of the form
`— hive: agent=<role> backend=... model=...`, and the branch name starts with a
prefix for the role.

| Role in the signature | Branch prefix | What its pull requests tend to be |
| --- | --- | --- |
| `quality` | `quality/` | tests and small fixes found by reading the code |
| `sec-check` | `sec/` | security-minded fixes |
| `scanner` | `scanner/` | fixes for problems a scan turned up |
| `architect` | `arch/` (once `architect/`) | structure and contract changes |
| `guide` | `guide/` | documentation |
| `ci-maintainer` | `ci/` | workflow upkeep |
| `strategist` | none seen on a branch | issues that ask the maintainer to decide something |

"Tend to be" is a reading of titles, not a rule. Hive decides what each role
does. Roles also appear as `agent/<role>` labels on issues.

Two more authors are not agents in this sense:

- `Danathar`, the maintainer, who also opens pull requests by hand.
- Issues whose signature reads `— hive: agent=dashboard`, such as the ACMM
  gap issues. They come from a Hive evaluation rather than from a role.

To see the current picture instead of trusting this table:

```bash
# Who opens pull requests, from which branch prefix:
gh pr list --state all --limit 300 --json author,headRefName \
  -q '.[] | [.author.login, (.headRefName | split("/")[0])] | @tsv' | sort | uniq -c

# Which roles sign their pull requests:
gh pr list --state all --limit 300 --json body \
  -q '.[].body | capture("— hive: agent=(?<role>\\S+)").role' | sort | uniq -c
```

Both need `-R Danathar/zfs-kinoite-complex` outside a checkout.

## How work reaches an agent

There are two routes, and they are separate.

**Route 1: Hive, outside the repository.** Hive looks at the repository and at
its own backlog, starts an agent, and the agent clones the repository and pushes
a branch. Nothing here receives that request, so nothing here can be asked
about it. What is visible here is the result: a branch with a role prefix, a
pull request, and labels.

**Route 2: a maintainer hands over a task inside GitHub.** This is the only
intake that lives in the repository:
[`.github/workflows/ai-fix.yml`](../.github/workflows/ai-fix.yml).

- A maintainer applies the label `ai-fix-requested` to an issue, or writes
  `@claude` in an issue or pull request comment.
- The workflow checks that the sender is not a bot, that agent credentials are
  configured, and (for a comment on a pull request) that the branch is not from
  a fork. If any check fails it stops and says why in the run summary.
- Otherwise the agent works on an `ai-fix/*` branch and opens a pull request.

Hive also applies `ai-fix-requested` itself to the issues it files, for
example the ACMM gap issues. The workflow ignores that on purpose: a label
applied by a bot is not a request from a person. A maintainer who wants the
agent to act relays it with `@claude`.

An `ai-fix/*` branch builds and publishes nothing. The details are in
[`SECURITY-AI.md`](./SECURITY-AI.md#what-an-agent-branch-can-actually-cause-here).

Whether agent credentials are set on the repository is not visible without
admin rights, so this page does not say. Until they are set, `ai-fix.yml` is
inert and succeeds.

### Labels

Labels on agent work come from three places:

- Hive applies `agent/<role>` labels and some others on its own pull requests
  and issues.
- `.github/workflows/labeler.yml` applies descriptive `area/*` labels from the
  paths a pull request changes, using
  [`.github/labeler.yml`](../.github/labeler.yml).
- A maintainer applies whatever else is needed.

Some labels mean more than their names suggest. Hive treats a label whose
description says "Approved by a Hive merger/owner for auto-merge on green CI"
as an approval, for example `hive/hive-wild-mole`. No automation here may apply
one. [`SECURITY-AI.md`](./SECURITY-AI.md#labels-carry-authority--automation-must-not-apply-them)
has the reasoning and the command that lists them today:

```bash
gh label list --limit 60 --json name,description \
  -q '.[] | select(.description | test("auto-merge"; "i")) | .name'
```

No workflow in this repository merges on these labels. They are still not
inert: their description, and `SECURITY-AI.md`, say Hive reads them as approval
to auto-merge on green CI. See [Who merges](#who-merges) for what has actually
happened.

## Keeping agents from colliding

Several agents, and the maintainer, can work at once. The repository gives only
two guards, and both are narrow.

**One `ai-fix.yml` agent per issue or pull request.** The workflow's
`concurrency` group is `ai-fix-` plus the issue or pull request number, with
`cancel-in-progress: false`. A second request on the same number waits for the
first run to finish instead of cancelling it half-way through pushing. This says
nothing about agents on *different* numbers, or about Hive's agents at all.

**A pull request's own checks are cancelled when it is updated.** `test.yml` and
`build-pr.yml` cancel an older run for the same ref. That keeps one pull request
tidy; it does not coordinate two.

Everything else is a habit, and it is a habit that the agent or the person
doing the work has to keep:

1. **Look for open work on the same files before starting.**

   ```bash
   gh pr list --state open --json number,title,headRefName,author
   gh pr list --state open --search "<file or topic>" --json number,title
   ```

2. **Look at the issue, not only the pull requests.** Hive labels an issue
   `hive/covered-by-pr` when it has verified that an open pull request claims
   it, and `hive/likely-done` when a merged one does. Both are Hive's reading,
   not a fact: the descriptions say "still actionable until confirmed" and
   "pending confirmation". Read the pull request before dropping a task.
3. **Say what you took.** Put `Closes #<issue>` in the pull request body so the
   issue links to it, the way the existing pull requests do.
4. **Keep a pull request to one change.** A small pull request that touches
   few files has few ways to overlap with another.

### Two green pull requests can be red together

The required check is `Python Unit Tests`. The ruleset in
[`.github/rulesets/main.json`](../.github/rulesets/main.json) sets
`strict_required_status_checks_policy` to `false`. In plain terms: GitHub does
not make a pull request catch up with `main` before it merges. A pull request
is judged by the code it was tested with, not by what `main` has become.

So if pull request A and pull request B each pass, and both change the same
function from different directions, merging A and then B can leave `main`
broken even though neither ever showed red. The failure then shows on `main`,
and a push to `main` that changes more than documentation starts `build.yml`,
the workflow that signs and promotes an image. See
[`branch-protection.md`](./branch-protection.md) for the ruleset itself, and
[`safety-model.md`](./safety-model.md) for what a bad `main` can reach.

The ruleset has no merge queue (a GitHub feature that tests each pull request on
top of the ones ahead of it), so nothing serialises merges. The protection is a
step the merger takes: when two open pull requests touch the same files, merge
one, update the other branch from `main`, wait for `Python Unit Tests` to
rerun, and only then merge it.

To check what the live ruleset says rather than trust this page:

```bash
gh api repos/Danathar/zfs-kinoite-complex/rulesets
```

## Who merges

The maintainer. When this was written, every merged pull request, Hive's
included, had been merged by `Danathar`. Check that it still holds:

```bash
gh pr list --state merged --limit 1000 --json mergedBy \
  -q 'map(.mergedBy.login) | unique'
```

The ruleset requires a pull request and a passing `Python Unit Tests`; it
requires no approving review, and it allows no bypass actors.

Agents do not merge. [`AGENTS.md`](../AGENTS.md) section 0 rule 6 says not to
push, promote, tag, or delete published artifacts on your own initiative, and
[`SECURITY-AI.md`](./SECURITY-AI.md) lists merging and approving as things no
agent may do on any instruction. `.claude/settings.json` denies the merge
command outright.

An approval label such as `hive/hive-wild-mole` is a signal to Hive's own
merge tooling and, as a reader of this repository, you will see it on pull
requests that `Danathar` then merged. This page does not know what Hive does
with the label on its side. If it ever merges on its own, the evidence will be a
`merged` event whose actor is the Hive App:

```bash
gh api repos/Danathar/zfs-kinoite-complex/issues/<number>/events \
  --jq '.[] | select(.event == "merged") | .actor.login'
```

What to read next:

- [`SECURITY-AI.md`](./SECURITY-AI.md): what an agent may and may not do.
- [`risk-tiers.md`](./risk-tiers.md): how much review a given change needs.
- [`review-rubric.md`](./review-rubric.md): what the merger checks.
- [`branch-protection.md`](./branch-protection.md): the ruleset on `main`.
