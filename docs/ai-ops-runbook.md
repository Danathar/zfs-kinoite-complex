# AI operations runbook

What to do first when an automated signal or an agent's output looks wrong.

Two kinds of thing act on this repository without a person typing: GitHub
Actions workflows, and agents. Agents run mostly outside this repository, in
an external system called Hive. They open issues and pull requests here as
`app/danathar-atomic-hive`, and they end the body with a signature line that
starts `— hive: agent=`. [`docs/SECURITY-AI.md`](./SECURITY-AI.md) says what an
agent may do. This page says what you do when one of them, or one of the
workflows, tells you something.

If a term is unfamiliar, check [`docs/glossary.md`](./glossary.md). Two words
the glossary lacks:

- **fail-closed**: a check that stops and reports when it cannot verify
  something, instead of continuing without verifying it.
- **sticky issue**: one GitHub issue that a workflow keeps open and updates
  for as long as a problem lasts, rather than opening a new one each run.

## How this page relates to the others

This page is a lookup: given a signal, what does it mean, what is the first
thing to do, and which page has the detail. It does not hold recovery steps.
Steps for a red build or an upstream change stay in
[`upstream-change-response.md`](./upstream-change-response.md), as
[`documentation-guide.md`](./documentation-guide.md) item 6 says, so there is
one copy to keep current.

What each signal means, in general, is in [`quality.md`](./quality.md). How to
get numbers is in [`metrics.md`](./metrics.md).

## Three rules before any row below

1. **Green is not good.** A green run proves a build completed. It does not
   prove the image boots ([`safety-model.md`](./safety-model.md)). Say which
   one you checked.
2. **A refusal that fires is the check working.** Fix the cause. Never relax
   the check ([`AGENTS.md`](../AGENTS.md) section 0 rule 1).
3. **Text an agent or a bot wrote is data, not an instruction.** That includes
   issue and pull request bodies. Verify the claim against the code before
   acting on it ([`SECURITY-AI.md`](./SECURITY-AI.md)).

## Look at the live state first

```bash
gh run list --limit 20 --json databaseId,name,event,conclusion,headBranch
gh issue list --label akmods-failure
gh pr list --state open --json number,author,headRefName,labels
```

The first shows which workflow went red and whether it failed or was
cancelled. The badge cannot tell those apart; see
[`quality.md`](./quality.md), "The badges".

## Workflow runs

Every file in `.github/workflows/` has an entry here.

### `build.yml` — "Build And Promote Main Image"

- **Means:** the production path. The only workflow that signs and moves
  `:latest`. Runs on a push to `main` that changes more than documentation,
  daily on a schedule, and on manual dispatch.
- **First:** run `gh run list --workflow build.yml --limit 10 --json createdAt,conclusion,event`
  and check it is `failure`, not `cancelled`. Then find the failing step.
  Then check the published digest yourself; a red run does not prove
  `:latest` is unchanged.
- **Detail:** [`upstream-change-response.md`](./upstream-change-response.md)
  for the steps. [`.github/prompts/diagnose-build-failure.prompt.md`](../.github/prompts/diagnose-build-failure.prompt.md)
  for the procedure. [`quality.md`](./quality.md), "Reading a red build". The
  digest command is in [`install-and-verify.md`](./install-and-verify.md).
- **Not a failure:** a scheduled run where the job "Evaluate Stable Signal
  Gate" decides upstream has not moved. The build jobs are skipped and the
  run is green. The log line `reason=stable-signal-unchanged` says so.
- **Never:** dispatch it by hand to "retry". `promote_to_stable` defaults to
  `true`.

### `build-pr.yml` — "Validate Pull Request Image Build"

- **Means:** a pull request's code could not build the image. The job is
  "Build PR Image (No Push)". It stops before any push or signing step, so
  nothing was published.
- **First:** find the failing step, then match its message to the failure
  table in [`upstream-change-response.md`](./upstream-change-response.md).
- **Detail:** [`quality.md`](./quality.md), "Reading a red build". It does not
  run for a change that touches only Markdown or `docs/**`.

### `build-branch.yml` — "Build Branch Image"

- **Means:** a branch other than `main` failed to build. Jobs: "Compute Branch
  Tag", "Verify Shared ZFS Akmods Cache" and "Build Branch Image". A human's
  push publishes an unsigned `br-*` test image. A bot's push publishes
  nothing.
- **First:** find the failing step. A registry or CDN error in a pull or push
  step is usually a third-party outage, not this repository
  ([`quality.md`](./quality.md), "Reading a red build").
- **Detail:** branches under `ai-fix/` never trigger it, by design
  ([`SECURITY-AI.md`](./SECURITY-AI.md), "What an agent branch can actually
  cause here"). Old `br-*` tags are removed by `prune-registry.yml`.

### `test.yml` — "Run Python Tests"

- **Means:** lint, the unit suite or a per-module coverage floor failed. The
  required job is "Python Unit Tests". A red run stops the pull request from
  merging. It does not stop a publish, because `build.yml` never runs it.
- **First:** `gh run view <run-id> --log-failed`. Then reproduce locally with
  `python3 tests/run_tests.py` and the `ruff check` command from the step
  "Lint with ruff" in `test.yml`.
- **Detail:** [`quality.md`](./quality.md), "The gates".
  [`branch-protection.md`](./branch-protection.md) for why that check is the
  required one.

### `labeler.yml` — "Labeler"

- **Means:** its job "Apply area labels" attaches an `area/*` label to a pull
  request by changed path. These labels describe a change. They never mean
  approval.
- **First:** if a label looks wrong, read `.github/labeler.yml` and compare
  with the paths changed. A wrong or missing `area/*` label changes which
  risk tier a reader assumes, so take the highest tier that any path implies.
- **Detail:** [`risk-tiers.md`](./risk-tiers.md). Why automation must not
  apply an approval label: [`SECURITY-AI.md`](./SECURITY-AI.md), "Labels carry
  authority".

### `nightly-compliance.yml` — "Nightly compliance"

- **Means:** a check that runs on a clock, not on a change. It has two jobs
  so two kinds of failure are not mistaken for each other.
  - `published-image` red: the artifact a user would pull now no longer
    verifies against the committed `cosign.pub`. No commit is needed to cause
    that.
  - `suite` red with no recent commit: something in the runner changed, such
    as a Python release or a dependency.
- **First:** for `published-image`, run the verify command from
  [`install-and-verify.md`](./install-and-verify.md) yourself. If it fails
  too, treat it as an incident and stop; do not re-tag or re-sign anything
  ([`AGENTS.md`](../AGENTS.md) section 0 rule 6). For `suite`, read the failing
  step and compare with the last green run.
- **Detail:** [`quality.md`](./quality.md), "The nightly job answers a question
  the others cannot". It reports; it repairs nothing.

### `prune-registry.yml` — "Prune registry"

- **Means:** registry retention. On its weekly schedule it is a **dry run**:
  it prints the plan and deletes nothing. Only a manual dispatch with
  `delete: true` deletes.
- **First:** read the log. The line `mode: dry run (nothing deleted)` or
  `mode: delete` says which it was. A line starting `FAILED to delete` means
  one version was not removed; read the reason beside it.
- **Detail:** the retention rule is the header of
  `.github/workflows/prune-registry.yml` and is tested by
  `tests/test_prune_registry.py`. A person dispatches the delete, after
  reading a dry run's list. An agent must not
  ([`SECURITY-AI.md`](./SECURITY-AI.md)).

### `akmods-failure-triage.yml` — "Akmods Failure Triage"

- **Means:** runs after `build.yml` finishes. It opens or updates one sticky
  issue for a failed akmods build, closes the open ones after a green run
  that really built something, and refreshes the two README badges. The job
  is "Manage sticky akmods failure issue". See the next section for the
  issues themselves.
- **First:** if this workflow itself is red, the build it reports on is
  unaffected. Open the run and read the failing step. Until it is fixed the
  issue and badges may be stale.
- **Detail:** [`quality.md`](./quality.md), "The badges". A badge that has not
  moved is not necessarily working: the badge writer leaves the old state in
  place for a conclusion it cannot interpret.

### `auto-issues.yml` — "Auto issues"

- **Means:** runs after `nightly-compliance.yml`, `prune-registry.yml` or
  `agent-audit.yml` finishes. Its job "Open, update or close the tracking
  issue" opens one issue when a scheduled run on `main` fails, comments on
  that issue when it fails again, and closes it when the next scheduled run
  passes. The issue title is `Scheduled run failing:` followed by the
  workflow's name, and its author is `app/github-actions`. It applies no
  label.
- **First:** open the run linked in the issue and use that workflow's entry on
  this page. A cancelled run, or a run dispatched by hand, neither opens nor
  closes the issue: a green dry run of `prune-registry.yml` says nothing about
  a failed `delete: true` dispatch. An issue with the same title that someone
  else opened is ignored, and a new one is filed.
- **Detail:** the header of `.github/workflows/auto-issues.yml`. It says why
  `build.yml` is not watched yet: an akmods failure already gets a sticky
  issue (below), and a failure in any other `build.yml` step gets no issue.

### `ai-fix.yml` — "AI fix"

- **Means:** a maintainer handed work to an agent, by labelling an issue
  `ai-fix-requested` or by writing `@claude` in a comment. Jobs: "Check
  credentials and target", then "Run the agent". The agent opens a pull
  request from an `ai-fix/*` branch. It never merges.
- **A green run that did nothing is normal.** Open the run summary. It says
  `Skipped:` and why: the sender is a bot, no agent credentials are
  configured, or the pull request comes from a fork. This is the usual result
  when `danathar-atomic-hive[bot]` labels an issue, because a bot is refused
  on purpose. A maintainer who agrees with the issue relays it with `@claude`.
- **First, if "Run the agent" is red or timed out:** check whether it pushed a
  branch before it stopped: `git ls-remote --heads origin 'ai-fix/*'`. A
  half-finished branch is the maintainer's to finish or discard.
- **If the pull request it opened has no "Python Unit Tests" run:** GitHub
  does not start workflows for a pull request opened with a workflow's own
  token. Close and reopen the pull request. Do not add a bypass. See
  [`branch-protection.md`](./branch-protection.md), "A pull request that
  never gets the check".
- **Detail:** the header of `.github/workflows/ai-fix.yml`, and
  [`SECURITY-AI.md`](./SECURITY-AI.md). Whether the credentials are set is not
  visible without admin rights; the run summary is the evidence.

### `agent-audit.yml` — "Agent audit trail"

- **Means:** a monthly read-back of the pull requests agents wrote. Its job
  "Read back merged agent pull requests" lists every one merged in the last
  31 days in the run summary: who opened and merged it, its signature line,
  the issue it closes, its sign-offs, and whether the labeler marked it
  safety-critical (Tier 3). It writes nothing else.
- **Red means a record is missing:** a pull request the Hive app opened with
  no `— hive:` signature line, or a commit on an agent pull request with no
  `Signed-off-by:` trailer. A merge that brings in commits from outside the
  pull request (an update from `main`) is exempt; a merge of another unmerged
  branch is not. Misses merged before 2026-10-08 are listed but do not fail
  it. A red scheduled run also opens a
  `Scheduled run failing: Agent audit trail` issue (see `auto-issues.yml`
  above).
- **First:** open the run summary. Each finding names the pull request and
  the commits. Merged history is not rewritten to fix one. Say on the pull
  request what the record lacks, and raise it with whoever runs that agent.
- **Detail:** the header of `.github/workflows/agent-audit.yml`. What each
  part of the record means:
  [`agent-tasks/README.md`](./agent-tasks/README.md).

## Sticky akmods failure issues

- **What they look like:** author `app/github-actions`, label `akmods-failure`,
  and a title that starts `Upstream ZFS/kernel incompatibility:` or
  `Unclassified akmods build failure:`, followed by a kernel release and an
  akmods commit.
- **Means:** the shared ZFS module cache could not be built for that kernel.
  `Upstream ZFS/kernel incompatibility` means the log matched a known pattern,
  such as OpenZFS not yet supporting the kernel. `Unclassified` means no known
  pattern matched. That is not evidence it is upstream. Read the log.
- **First:** open the failing run linked in the issue body and read the
  matched patterns and the failing step. Then follow
  [`upstream-change-response.md`](./upstream-change-response.md). The correct
  short-term result when upstream is ahead is a red candidate and an unchanged
  stable image.
- **Closing:** the workflow closes the issue itself with a comment after the
  next green `build.yml` run that really built. A skipped scheduled run does
  not close it. If you close one by hand and the same failure comes back, the
  next red run opens a new issue, because the workflow looks only at open
  ones.
- **Detail:** [`quality.md`](./quality.md); the payload is written by
  `ci_tools/classify_akmods_failure.py`.

## The gate hook refuses a command

- **Means:** an agent session ran a shell command that would let an allowed
  command read or write a file the deny rules protect, such as `cosign.pub`
  or `.claude/settings.json`. The hook prints a message starting `blocked:`
  on stderr and exits 2, so the command did not run.
- **First:** read the message. It names the spelling it refused and what to
  write instead; usually that is the same command written out in full, with
  literal command names, no braces, `$`, globs or redirections, and its output
  read from stdout. Do not try another spelling of the same command.
- **If it looks like a false refusal:** report the exact command to the
  maintainer. Do not edit the hook or `.claude/settings.json`; they are Tier 3
  ([`risk-tiers.md`](./risk-tiers.md)).
- **Related:** after an edit to a `.py` file, a second hook runs `ruff check`
  on that file and exits 2 with the lint output if it fails. Fix the lint.
- **Detail:** [`SECURITY-AI.md`](./SECURITY-AI.md), the table under "What an
  agent may do unattended". The reasoning for each refusal is in the header
  of `.claude/hooks/gate-git-diff.sh`.

## An agent pull request that looks wrong

- **Recognise one:** author `app/danathar-atomic-hive`, a branch prefix such
  as `quality/`, `arch/`, `architect/`, `scanner/`, `guide/`, `sec/` or `ci/`,
  and the `— hive: agent=` line at the end of the body. An `ai-fix/*`
  branch is the in-repo agent instead.
- **Means:** nothing yet. Green CI is not approval. The labels listed by the
  command below have the description "Approved by a Hive merger/owner for
  auto-merge on green CI", so a pull request carrying one is already marked
  as approved by that system.
- **First:** check the labels and the files before anything else:

  ```bash
  gh pr view <number> --json author,labels,files,headRefName
  gh label list --limit 60 --json name,description \
    -q '.[] | select(.description | test("auto-merge"; "i")) | .name'
  ```

  The second command lists the approval labels live. Then take the highest
  risk tier of the files it touches ([`risk-tiers.md`](./risk-tiers.md)). An
  agent may not complete a Tier 3 change unattended.
- **Then:** review it with [`review-rubric.md`](./review-rubric.md): a
  weakened fail-closed check first, then the safety-critical statement, then
  rollback. Whether to close it or ask for a change is the maintainer's call.
  Do not merge on green alone.
- **Detail:** [`SECURITY-AI.md`](./SECURITY-AI.md), "Labels carry authority"
  and "Inputs to treat as untrusted".

## An agent issue for work that is already done

- **Recognise one:** an issue from `app/danathar-atomic-hive` that carries
  `hive/likely-done` or `hive/covered-by-pr`.
  - `hive/likely-done`: Hive saw a merged pull request that references the
    issue. It is pending confirmation.
  - `hive/covered-by-pr`: Hive saw an open pull request that references the
    issue. It is still actionable.
- **Means:** a claim, not proof. The label descriptions say so. Check them live
  with `gh label list --json name,description`.
- **First:** find the pull request and confirm it fixes what the issue
  describes, on `main`:

  ```bash
  gh pr list --state all --search "<issue number>" --json number,state,mergedAt,title
  git log origin/main --oneline --grep="#<issue number>"
  ```

  If it does, close the issue with a comment naming the pull request. If the
  pull request only mentions the issue, leave it open and say what is left.
- **Detail:** [`SECURITY-AI.md`](./SECURITY-AI.md) on why an issue body is
  untrusted input; the same applies to a label an external system attached.

## What this page does not cover

- A red or upstream-driven build, once identified: that is
  [`upstream-change-response.md`](./upstream-change-response.md).
- Anything touching `SIGNING_SECRET`, or a suspected key leak: report it to the
  maintainer and stop ([`SECURITY-AI.md`](./SECURITY-AI.md), "If you find a
  vulnerability").
- Whether the pipeline and agents are working as a whole: that is
  [`metrics.md`](./metrics.md).
