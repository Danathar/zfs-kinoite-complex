# Branch protection

Merging to `main` publishes: `build.yml` builds, signs and promotes whatever
`main` holds to `:latest`. This file says what keeps changes to `main` behind a
pull request, why each rule is there, and how to check that GitHub is really
enforcing it. A nightly job now makes that check too; see
[Checking the live rules every night](#checking-the-live-rules-every-night).

## Status

The ruleset below has been active on `main` since 2026-09-24, as ruleset
`23959550`. It was applied from this file. Check it yourself; neither call
needs admin rights:

```bash
gh api repos/Danathar/zfs-kinoite-complex/branches/main --jq .protected
gh api repos/Danathar/zfs-kinoite-complex/rulesets
```

The first prints `true` and the second lists `protect main`. `false` and `[]`
mean someone has removed it, and `main` is unprotected again.

## Why it matters here

Every other gate in this repository sits behind a pull request: the unit suite,
the review rubric, the risk tiers, and the rule that a pull request cannot
publish or sign. Without a ruleset nothing makes anyone open one. Any token that
holds `contents: write` could push to `main` directly, and that push would start
`build.yml`, which signs and promotes it.

One such token is the `fix` job in
[`.github/workflows/ai-fix.yml`](../.github/workflows/ai-fix.yml). Its prompt
tells the agent never to push to `main`. That is an instruction to a model that
reads issue bodies written by strangers. It is not a control. This ruleset is.

## The ruleset

[`.github/rulesets/main.json`](../.github/rulesets/main.json) is the definition
the live ruleset was applied from. It is in GitHub's import format, so it
applies as-is. What each rule does:

- **Targets `~DEFAULT_BRANCH`**, so it follows a rename of `main`. The `status`
  branch that `akmods-failure-triage.yml` pushes badges to is not covered, and
  does not need to be: nothing builds from it.
- **No bypass actors.** A bypass for Actions or for an App hands back the direct
  push this exists to stop.
- **`deletion` and `non_fast_forward`** stop `main` being deleted or rewritten.
- **`pull_request` with 0 approvals, and code owner review required.** GitHub
  does not let anyone approve their own pull request. On a single-maintainer
  repository, requiring one approval for *every* pull request means nothing can
  ever merge, including the change that relaxes the rule. What 0 still enforces
  is that every change arrives as a pull request and is merged through one.
  `require_code_owner_review` is what puts the owner on the merge button where
  it matters: [`.github/CODEOWNERS`](../.github/CODEOWNERS) lists only the
  Tier 3 paths ([`risk-tiers.md`](./risk-tiers.md)), so a pull request that
  touches one of them cannot merge, by anyone or any token, until the owner
  approves it on GitHub, and that approval is on record. Any other pull request
  touches no listed path and merges on the required check alone. That is why
  `CODEOWNERS` has no catch-all `*` line, and `tests/test_codeowners.py` fails
  if one comes back.

  The cost: the owner is the only code owner and cannot approve their own pull
  request, so a pull request the owner opens that touches a Tier 3 path cannot
  merge either. Either have an agent open it and approve that, or add a bypass
  for the admin role in `pull_request` mode (`{"actor_id": 5, "actor_type":
  "RepositoryRole", "bypass_mode": "pull_request"}` in `bypass_actors`). That
  bypass lets an admin merge a pull request past *all* of its rules, the
  required check included, so it is not in the file. Every workflow file is
  Tier 3, so Renovate's pull requests that bump a pin in a workflow, or in
  `publish-native-image/`, also wait for the owner's approval.
- **One required check, `Python Unit Tests`.** It is the only check every pull
  request gets. `test.yml` runs it on every pull request, with no path filter
  and no `if:`. `Build PR Image (No Push)` in `build-pr.yml` is not required,
  because `build-pr.yml` ignores Markdown and `docs/**`, so a docs-only pull
  request never gets it and would wait forever. `Apply area labels`
  classifies a change rather than checking it. `Mutation check (advisory)` in
  `test.yml` reports on the step summary and never fails the run, so it has
  nothing to require. `integration_id` 15368 is
  GitHub Actions, so a status with the same name posted by anything else does
  not count.

Nothing in this repository pushed to `main` outside a pull request before the
ruleset went on. Every first-parent commit on `main` since 2026-08-05 is a pull
request merge. No workflow pushes to `main`: the only `git push` in
`.github/workflows/` targets the `status` branch, and `ai-fix.yml` pushes
`ai-fix/*` branches. Renovate does not automerge: `renovate.json` never turns
`automerge` on, `tests/test_renovate_config.py` keeps it that way, and none of
the presets it extends (`config:best-practices`, `schedule:weekly`) turns it on
either, so there is no `automergeType: branch` push to break. Applying the
ruleset changed nothing about how work lands.

## A pull request that never gets the check

GitHub does not start `pull_request` workflows for a pull request opened or
updated with a workflow's own `GITHUB_TOKEN`. `ai-fix.yml` hands the agent
`github.token`, so a pull request it opens would have no `Python Unit Tests`
run and could not merge. No agent credential is set here today, so this has not
happened. If it does, a person closing and reopening the pull request starts the
workflows, because the `reopened` event is then theirs. Do not fix it by adding
a bypass actor.

## Applying and changing it

A pull request cannot change repository settings. A repository admin applied
it once, with:

```bash
gh api --method POST repos/Danathar/zfs-kinoite-complex/rulesets \
  --input .github/rulesets/main.json
```

To change it later, edit the file through a pull request, then update the live
ruleset from the file:

```bash
gh api --method PUT repos/Danathar/zfs-kinoite-complex/rulesets/23959550 \
  --input .github/rulesets/main.json
```

Renaming the `Python Unit Tests` job, adding a path filter or an `if:` to it, or
putting it in a matrix fails `tests/test_branch_protection.py`. Change the
ruleset in the same pull request, and `PUT` it straight after the merge.

## Checking the live rules every night

The `PUT` is a separate step after the merge, and it has been forgotten: the
file asked for code owner review while the live ruleset did not (#454, #488).
So the `ruleset-drift` job in
[`.github/workflows/nightly-compliance.yml`](../.github/workflows/nightly-compliance.yml)
compares the two every night. It runs
[`ci_tools/check_ruleset_drift.py`](../ci_tools/check_ruleset_drift.py), which
reads the rules GitHub enforces on the default branch from
`GET /repos/{owner}/{repo}/rules/branches/{branch}`. That endpoint needs only
`contents: read`, so the job uses the run's own token and no admin secret.

It fails only when GitHub is weaker than the file:

- a rule type in the file (`deletion`, `non_fast_forward`, `pull_request`,
  `required_status_checks`) that GitHub does not enforce;
- `require_code_owner_review` true in the file and false live;
- a lower `required_approving_review_count` live;
- `strict_required_status_checks_policy` true in the file and false live;
- a required check missing live, or required live from a different
  `integration_id` than the file pins.

Extra rules, or stricter ones, pass. When two rulesets apply to the branch,
GitHub returns a rule of the same type once for each, and the strongest one
counts. A failed run opens an issue through
[`.github/workflows/auto-issues.yml`](../.github/workflows/auto-issues.yml).
The fix is the `PUT` above, not an edit to the file.

What it does not compare: the bypass list and the branch conditions, which the
endpoint does not return, and the other `pull_request` settings, such as
dismissing stale reviews. For those, a repository admin reads the ruleset
itself with `gh api repos/Danathar/zfs-kinoite-complex/rulesets/23959550`.

The same job also covers the rest of the fleet. A second step runs the script
with `--fleet`, which makes the same comparison for each repository in its
`FLEET` list: it reads that repository's committed
`.github/rulesets/{branch}.json` (`master.json` in exult and sensi, `main.json`
elsewhere) through the contents API, and its effective rules from the endpoint
above. They are public, so the run's own token reads them too. The report has
one section per repository, each with the `PUT` an admin of that repository
runs, and the step fails when any of them is weaker than its file or when its
file or rules cannot be read. One job here replaces a copy of the script in
each repository (#490).

## When there is a second reviewer

Set `required_approving_review_count` to 1. Consider
`require_last_push_approval`, so a push after approval needs a fresh one.
