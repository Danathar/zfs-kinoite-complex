# Strategy

If a term is unfamiliar, check the shared glossary first:
[`docs/glossary.md`](./glossary.md)

What this project is trying to be, and the commands that show how far along it
is. This page links to where the goal is written down and does not restate it.
It stores no counts: every number comes from a command you run, because a
number written here would be stale the next time a pull request merged.

It is not a dashboard and nothing keeps it current by itself. If a command below
prints something that disagrees with this page, the command is right.

## The goal, where the repository states it

The goal is written in four places. Read them in this order.

1. [`README.md` → Why This Repo Exists](../README.md#why-this-repo-exists) says
   what the image is for: a signed Fedora Kinoite image whose kernel and ZFS
   modules match.
2. [`docs/safety-model.md`](./safety-model.md) says what the project is **today**:
   testing-only, not used in production. Its opening paragraphs also say the
   author intends to keep the image building and testing, and that this does not
   change the production boundary.
3. [`docs/production-boundary-proposal.md`](./production-boundary-proposal.md)
   lists what would have to be configured and checked before a signed test image
   could be treated as more than a test. See its
   [Required settings](./production-boundary-proposal.md#required-settings) and
   [Review checklist](./production-boundary-proposal.md#review-checklist). It
   says plainly that those controls "do not make the repository
   production-approved".
4. [`docs/runtime-validation-proposal.md`](./runtime-validation-proposal.md) says
   what is not tested yet: CI does not boot the image or import a pool before
   `:latest` moves. See its
   [Current boundary](./runtime-validation-proposal.md#current-boundary) and
   [Future automation requirements](./runtime-validation-proposal.md#future-automation-requirements).

[`docs/maintenance-watchlist.md`](./maintenance-watchlist.md) records what
stands in the way: the moving parts nothing else watches, including one finding
marked
[Open](./maintenance-watchlist.md#open-the-production-signing-environment-is-not-branch-restricted).
The two proposals are proposals, not commitments with dates.

So the stated direction is: keep a testing-only image building, signed and
tested, and keep the production claim off the table until the two proposals are
answered. This page does not decide anything beyond that.

## Reading progress

Each command is a read. The `grep` and `sed` commands run from the root of a
clone of this repository. The `gh` commands name the repository, so they work
from any directory and do not depend on which fork `gh` would guess. `gh` is the
GitHub command-line tool, signed in to an account that can read the repository.

### What is still open on the production boundary

List the findings the watchlist marks as open:

```bash
grep -n '^### Open' docs/maintenance-watchlist.md
```

Each hit is a heading. The section under it says how to check the live GitHub
setting and which options close it. Settings live on GitHub, not in the
repository, so the files alone cannot tell you whether they are configured. The
proposal says the same and does not claim they are.

Print the proposal's checklist to work through by hand:

```bash
sed -n '/^## Review checklist/,$p' docs/production-boundary-proposal.md
```

An item is done when you have checked it live and can say how. Nothing in this
repository ticks these boxes for you.

### Whether runtime validation has started

The runtime-validation proposal says no workflow gates promotion on a boot or a
pool import. Look for one:

```bash
grep -rniE 'zpool (create|import)' .github/workflows
```

No output means no workflow creates or imports a pool. Output means someone
started the work; read the proposal's *Future automation requirements* against
it before believing it is finished.

### What merged recently, by branch prefix

Agent branches are named with a prefix such as `quality/` or `scanner/`. Group
merged pull requests since a date by that prefix:

```bash
since=$(date -d '30 days ago' +%F)
gh pr list -R Danathar/zfs-kinoite-complex --state merged --search "merged:>=$since" \
  --limit 200 --json headRefName \
  -q '[.[].headRefName | split("/")[0]] | group_by(.) | map({prefix: .[0], merged: length}) | sort_by(-.merged)[]'
```

Read it with three cautions:

- A prefix is a naming habit, not proof of who wrote the change. Use
  [`docs/metrics.md`](./metrics.md#pull-request-acceptance) to split by author.
- `--limit 200` caps the list. If the count equals the cap, narrow the date.
- This counts activity, not progress. Many merges under one prefix can mean
  steady maintenance or a loop of small fixes. Open a few and ask whether they
  moved anything in the previous section.

### What is waiting on a person

[`README.md` → Maintained with Hive (ACMM L5)](../README.md#maintained-with-hive-acmm-l5)
says Hive puts a `hold` label on every pull request an agent opens, and that a
human maintainer reviews them in batches. The open ones are the queue:

```bash
gh pr list -R Danathar/zfs-kinoite-complex --state open --label hold
```

Issues that need a human, and the ACMM gap issues the Hive evaluation opened:

```bash
gh issue list -R Danathar/zfs-kinoite-complex --state open --label needs-human
gh issue list -R Danathar/zfs-kinoite-complex --state open --label acmm
```

Labels live on GitHub, so check what a label means before relying on it:
`gh label list -R Danathar/zfs-kinoite-complex` prints each one's description.
`needs-human` has none and no page in this repository defines it, so read what
the issue itself asks.

ACMM is Hive's maturity model for how autonomously a repository can be
maintained. Its checks test that a file exists, so a gap issue closing says
nothing about whether anything changed. This page exists to be read, not to
raise a score.

### Whether the pipeline itself is healthy

That is a different question with its own pages. Use
[`docs/metrics.md`](./metrics.md) for the commands and
[`docs/quality.md`](./quality.md) for what each signal means.

## Why there is no scheduled report

The ACMM gap issue for this criterion (label `acmm`) lists
`.github/workflows/strategy-report.yml` as one of four filenames it accepts.
This repository does not have that workflow, on purpose; this page is the
filename it uses instead:

- **The repository already says it has no collector.**
  [`docs/metrics.md`](./metrics.md) opens: "There is no metrics service and no
  scheduled collector", because it "would be more machinery than the signal
  justifies at this size". A scheduled progress report is a collector.
- **The honest numbers cannot carry a conclusion.** The same page lists what is
  [deliberately not measured](./metrics.md#what-is-deliberately-not-measured)
  and explains that the pull request acceptance rate mostly measures how often the
  maintainer merges their own work. A report that printed such figures on a schedule would invite
  exactly that reading.
- **It would not be a documentation change.** Every workflow must be listed in
  [`.github/policies/workflow-permissions.json`](../.github/policies/workflow-permissions.json),
  or a test fails, and
  [`docs/risk-tiers.md`](./risk-tiers.md) puts that file in Tier 3. A new
  scheduled workflow that wrote to issues or a branch would also be new
  automation holding a token, and that file is where each workflow's token
  permissions are recorded.
- **The scheduled workflows that do exist** are `build.yml`, `nightly-compliance.yml`,
  `prune-registry.yml` and `agent-audit.yml`. Each does one job, and none reports
  on progress: `agent-audit.yml` is red or green on whether merged agent pull
  requests left their signature line and sign-offs.

If a report is ever wanted, the commands above are the starting point, and the
change goes through [`docs/risk-tiers.md`](./risk-tiers.md) like any other
workflow. Until then the commands are the report.
