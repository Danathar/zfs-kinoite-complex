# Branch protection

Merging to `main` publishes: `build.yml` builds, signs and promotes whatever
`main` holds to `:latest`. This file says what keeps changes to `main` behind a
pull request, why each rule is there, and how to check that GitHub is really
enforcing it. A nightly job now makes that check too; see
[Checking the live rules every night](#checking-the-live-rules-every-night).

## Status

The ruleset `protect main` has been active on `main` since 2026-09-24, as
ruleset `23959550`. It was applied from
[`.github/rulesets/main.json`](../.github/rulesets/main.json). A second
ruleset, `required checks`, holds the required check on its own, applied from
[`.github/rulesets/main-checks.json`](../.github/rulesets/main-checks.json).
It was split out of `protect main` so that Renovate's bypass cannot skip the
check; see
[Renovate merges its own non-major updates](#renovate-merges-its-own-non-major-updates).
Check them yourself; neither call needs admin rights:

```bash
gh api repos/Danathar/zfs-kinoite-complex/branches/main --jq .protected
gh api repos/Danathar/zfs-kinoite-complex/rulesets
```

The first prints `true` and the second lists `protect main` and
`required checks`. `false` and `[]` mean someone has removed them, and `main`
is unprotected again. If the second call lists only `protect main`, the split
has not been applied yet: the check is then still inside `protect main`, which
then has no bypass either.

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

[`.github/rulesets/main.json`](../.github/rulesets/main.json) (`protect main`)
and [`.github/rulesets/main-checks.json`](../.github/rulesets/main-checks.json)
(`required checks`) are the definitions the live rulesets were applied from.
They are in GitHub's import format, so they apply as-is. GitHub enforces both
on every change to `main`. What each rule does:

- **Targets `~DEFAULT_BRANCH`**, so it follows a rename of `main`. Both files
  target it. The `status` branch that `akmods-failure-triage.yml` pushes badges
  to is not covered, and does not need to be: nothing builds from it.
- **One bypass actor, the Renovate app, and only through a pull request.**
  `protect main` lists `{"actor_id": 2740, "actor_type": "Integration",
  "bypass_mode": "pull_request"}`. 2740 is the Renovate GitHub App
  (`gh api /apps/renovate`). In `pull_request` mode Renovate can merge its own
  pull request past the rules of `protect main`. It was added so that merge
  would not wait for code owner review; with that review off since
  2026-11-23 it skips nothing another pull request has to meet, and it is
  unchanged. It still cannot push to `main` directly, delete it or rewrite it. A bypass only skips the rules of
  the ruleset that lists it, and `required checks` lists no bypass actors, so
  Renovate's pull request still has to pass `Python Unit Tests`. No other app,
  and not Actions, may bypass: that would hand back the direct push this
  exists to stop.
- **`deletion` and `non_fast_forward`** stop `main` being deleted or rewritten.
- **`pull_request` with 0 approvals, and no code owner review.** GitHub
  does not let anyone approve their own pull request. On a single-maintainer
  repository, requiring one approval for *every* pull request means nothing can
  ever merge, including the change that relaxes the rule. What 0 still enforces
  is that every change arrives as a pull request and is merged through one.
  `require_code_owner_review` is `false`, by the owner's decision of
  2026-11-23: ordinary changes, Tier 3 ones included, merge on the required
  check and the hive's AI reviewer, with no owner review. Until then it was
  `true`, and every Tier 3 path waited for the owner's approval on GitHub.

  What still needs the owner is a gate on the hive's side, not GitHub's. The
  hive's `auto_merge.human_merge_paths` setting lists the owner-merge paths:
  image signing (`cosign.pub`, `ci_tools/sign_image.py`,
  `ci_tools/promote_stable.py`, `files/scripts/configure_signing_policy.py`,
  `.github/actions/install-signing-tools/` and
  `.github/actions/publish-native-image/`), every workflow in
  `.github/workflows/`, the rulesets in `.github/rulesets/`, and
  `.github/CODEOWNERS`. Workflows are on the list because the `production-signing` environment has only a branch policy and no required
  reviewer, so any workflow on `main` can request it and be handed the cosign key.
  The hive labels a pull request that touches one of them `needs-owner-merge`
  and leaves the merge to the owner.
  [`.github/CODEOWNERS`](../.github/CODEOWNERS) lists the same paths, so GitHub
  asks for the owner's review on those pull requests; it no longer gates
  anything by itself. `tests/test_codeowners.py` keeps the two lists the same,
  and fails if a catch-all `*` line comes back.

  The cost: the hive's gate holds only the Hive app. A person or another token
  that can merge here can merge a green pull request that touches an
  owner-merge path, and GitHub will not stop it.
- **One required check, `Python Unit Tests`, in its own ruleset.** It is the
  only rule in `main-checks.json`, so the Renovate bypass in `main.json`
  does not reach it. Strict mode is off
  (`strict_required_status_checks_policy` is `false`): a pull request does not
  have to be up to date with `main` before it merges, but the check must pass
  on its own head commit. See
  [Pull requests do not have to be up to date](#pull-requests-do-not-have-to-be-up-to-date).
  It is the only check every pull request gets. `test.yml` runs it on every
  pull request, with no path filter and no `if:`. `Build PR Image (No Push)` in `build-pr.yml` is not required,
  because `build-pr.yml` ignores Markdown and `docs/**`, so a docs-only pull
  request never gets it and would wait forever. `Apply area labels`
  classifies a change rather than checking it. `Mutation check (advisory)` in
  `test.yml` reports on the step summary and never fails the run, so it has
  nothing to require. `Check the agent signature line` in
  `agent-signature.yml` runs only on agent pull requests and is advisory for
  now; it can be added here once it has run clean for a month. `integration_id` 15368 is
  GitHub Actions, so a status with the same name posted by anything else does
  not count.

Nothing in this repository pushed to `main` outside a pull request before the
ruleset went on. Every first-parent commit on `main` since 2026-08-05 is a pull
request merge. No workflow pushes to `main`: the only `git push` in
`.github/workflows/` targets the `status` branch, and `ai-fix.yml` pushes
`ai-fix/*` branches. Renovate merges only through a pull request
(`"automergeType": "pr"`), never by pushing its branch to `main`.

## Pull requests do not have to be up to date

Strict mode used to be on: GitHub refused to merge a pull request until its
branch had been updated with the latest `main` and the check had run again.
It is off now. The owner turned it off (November 2026) because of Renovate's
pull requests. Every merge to `main` made each open bump pull request stale
again, and nothing brought them up to date: `renovate.json` sets `rebaseWhen`
to `never`, so Renovate does not update them itself, and once anyone else
pushes to a Renovate branch, Renovate leaves that branch alone. So bump pull
requests piled up waiting for the owner to update them by hand, although
[AGENTS.md section 10, "Owner's standing decisions"](../AGENTS.md#10-owners-standing-decisions)
already approves routine patch and minor updates with green CI.

What still holds: `Python Unit Tests` must pass on the pull request's own head
commit before anything merges. What is given up: two pull requests that each
pass on their own can still break `main` together, because neither was tested
on top of the other. `test.yml` also runs `Python Unit Tests` on every push to
`main`, so such a pair shows red there, but only after the merge, and
`build.yml` starts on that same push.

## Renovate merges its own non-major updates

Renovate now merges its own patch, minor, digest and pin updates once the
required check passes, without the owner's approval. This is AGENTS.md's
standing decision for routine updates, applied by the bot instead of by hand.
Three settings make it work, and each one is needed:

- `renovate.json` turns `automerge` on for those update types, with
  `"automergeType": "pr"` and `"platformAutomerge": false`. Renovate itself
  merges the pull request once its checks are green. GitHub's own auto-merge is
  not used. It was ruled out while code owner review was on, because it would
  have waited for that review and ignored the bypass.
- `protect main` lists the Renovate app as a bypass actor in `pull_request`
  mode. It kept Renovate's merge from waiting for code owner review on Tier 3
  paths; since that review went off on 2026-11-23 it skips nothing.
- The required check lives in `required checks`, a ruleset with no bypass
  actors. A bypass skips only the rules of the ruleset that lists it, so CI is
  still enforced on Renovate's pull requests by GitHub, not only by Renovate's
  own wait for green checks.

What still waits for the owner: major updates, an OpenZFS minor-line bump
(new on-disk pool features can break rollback to the previous image), and a
cosign bump (`docs/architecture-overview.md` makes version-specific claims
about it). `renovate.json` keeps `automerge` off for each of those, and
`tests/test_renovate_config.py` checks that. A new GitHub Actions release also waits three days
(`minimumReleaseAge`) before Renovate takes it, so a compromised action release
has time to be found and pulled before it can merge into a signing workflow. Every pull request from
anyone else follows [the ruleset](#the-ruleset) and the hive's owner-merge paths.

`renovate.json` deliberately does not list the Hive app in
`gitIgnoredAuthors`. A commit anyone other than Renovate adds to a Renovate
branch makes Renovate stop merging that branch, and that is wanted: otherwise a
Hive commit on a Renovate branch would be merged by Renovate itself, outside
the hive's owner-merge gate. When a Renovate branch needs rebuilding, the Hive ticks the
rebase box on the pull request and Renovate regenerates the branch from scratch.

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
`protect main` once, with:

```bash
gh api --method POST repos/Danathar/zfs-kinoite-complex/rulesets \
  --input .github/rulesets/main.json
```

and creates `required checks` the same way from its own file. Create it
before the `PUT` that takes the check out of `protect main`, so that `main` is
never without a required check:

```bash
gh api --method POST repos/Danathar/zfs-kinoite-complex/rulesets \
  --input .github/rulesets/main-checks.json
```

To change either later, edit its file through a pull request, then update the
live ruleset from the file:

```bash
gh api --method PUT repos/Danathar/zfs-kinoite-complex/rulesets/23959550 \
  --input .github/rulesets/main.json
id=$(gh api repos/Danathar/zfs-kinoite-complex/rulesets --jq '.[] | select(.name == "required checks") | .id')
gh api --method PUT repos/Danathar/zfs-kinoite-complex/rulesets/$id \
  --input .github/rulesets/main-checks.json
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
`contents: read`, so the job uses the run's own token and no admin secret. It
returns the rules of both rulesets in one list, so the job adds the rules of
`main-checks.json` to those of `main.json` and compares the live list against
both files together.

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
counts. Strict mode is `false` in this repository's file, so strict mode off
live is not drift. Code owner review is `false` in this repository's file, so
code owner review off live is not drift. A failed run opens an issue through
[`.github/workflows/auto-issues.yml`](../.github/workflows/auto-issues.yml).
The fix is the `PUT` above, not an edit to the file.

What it does not compare: the bypass list (including the Renovate bypass) and
the branch conditions, which the endpoint does not return, and the other `pull_request` settings, such as
dismissing stale reviews. For those, a repository admin reads the ruleset
itself with `gh api repos/Danathar/zfs-kinoite-complex/rulesets/23959550`.

The same job also covers the rest of the fleet. A second step runs the script
with `--fleet`, which makes the same comparison for each repository in its
`FLEET` list: it reads that repository's committed
`.github/rulesets/{branch}.json` (`master.json` in exult and sensi, `main.json`
elsewhere) through the contents API, together with
`.github/rulesets/{branch}-checks.json` when that repository splits its
required checks out the way this one does (a missing file is fine), and its effective rules from the endpoint
above. They are public, so the run's own token reads them too. The report has
one section per repository, each with the `PUT` an admin of that repository
runs, and the step fails when any of them is weaker than its file or when its
file or rules cannot be read. One job here replaces a copy of the script in
each repository (#490).

## When there is a second reviewer

Set `required_approving_review_count` to 1. Consider
`require_last_push_approval`, so a push after approval needs a fresh one.
