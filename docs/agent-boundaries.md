# Agent boundaries

Which limits on an AI agent working here are enforced by a tool, and which are
only asked of it.

If a term is unfamiliar, check the shared glossary first:
[`docs/glossary.md`](./glossary.md)

## Purpose

Most of what this repository tells an agent is a request: keep a change to one
thing, write the safety-critical statement, never weaken a fail-closed check. A
**structural gate** is different. It is a setting, a check or a refusal that
stops the action whatever the agent decides, so a mistaken or misled agent is
stopped rather than trusted.

This page lists the gates, says what each one stops, and links the page that
explains it. It restates none of them. The linked page is the reference, and
most of those pages are checked against the files they describe by a test of
their own.

What an agent is *allowed* to do is [`SECURITY-AI.md`](./SECURITY-AI.md). How
much review a change needs is [`risk-tiers.md`](./risk-tiers.md).

## The gates

| Gate | What it stops | Holds for | Explained in |
| --- | --- | --- | --- |
| [`.github/rulesets/main.json`](../.github/rulesets/main.json) | Any change reaching `main` except as a pull request merge. Deleting or rewriting `main`. It has no bypass actors. | every agent and every person | [branch-protection.md](./branch-protection.md#the-ruleset) |
| The required `Python Unit Tests` check in [`test.yml`](../.github/workflows/test.yml) | Merging a pull request whose unit suite, `ruff check` or per-module coverage floors fail. Many tests read the documents, so a page that drifts from the files it describes fails here too. | every agent and every person | [quality.md](./quality.md#the-gates) |
| [`.github/policies/workflow-permissions.json`](../.github/policies/workflow-permissions.json) | A workflow's `GITHUB_TOKEN` gaining a scope the policy file does not grant. `tests/test_workflow_permissions_policy.py` fails until the same pull request changes both files. | every agent | [SECURITY-AI.md](./SECURITY-AI.md#what-an-agent-may-do-unattended) |
| The `preflight` job in [`ai-fix.yml`](../.github/workflows/ai-fix.yml) | An agent started by a bot, from a fork, or with no credentials. The agent job it starts has no `packages: write` and no `SIGNING_SECRET`. | the agent `ai-fix.yml` starts | [multi-agent.md](./multi-agent.md#how-work-reaches-an-agent) |
| The `ai-fix/**` exclusion in [`build-branch.yml`](../.github/workflows/build-branch.yml) | An `ai-fix/*` branch publishing a branch image. `build.yml` runs only on `main`, and `build-pr.yml` stops before any push or signing step. | the agent `ai-fix.yml` starts | [SECURITY-AI.md](./SECURITY-AI.md#what-an-agent-branch-can-actually-cause-here) |
| [`.claude/settings.json`](../.claude/settings.json) | Reading `cosign.key`, `.env`, a `.pem` or `.p12` file, or an SSH private key named `id_rsa` or `id_ed25519`; a key under any other name is not denied. Signing, registry pushes and deletes, merging, dispatching a workflow, releases, secrets, labels, the force-push and delete spellings a prefix rule can see, `git reset --hard`, `git clean`, and destructive `zpool` and `zfs` commands. It asks before an ordinary push, commit or pull request. | Claude Code sessions only | [SECURITY-AI.md](./SECURITY-AI.md#what-an-agent-may-do-unattended) |
| [`.claude/hooks/gate-git-diff.sh`](../.claude/hooks/gate-git-diff.sh) | An allow-listed command, such as `git diff` or `gh pr view --jq`, spelled so it reads or writes a file the deny rules protect, or prints the environment. | Claude Code sessions only | [SECURITY-AI.md](./SECURITY-AI.md#what-an-agent-may-do-unattended) |
| [`tests/run_tests.py`](../tests/run_tests.py) | The one test command that runs with no prompt importing code outside `tests/` or code git does not track, or writing to a path it names. | Claude Code sessions only | [SECURITY-AI.md](./SECURITY-AI.md#every-deny-row-is-conditional-on-what-may-be-imported) |

"Claude Code sessions only" matters. The settings file and its hook are Claude
Code's format, and an agent on another backend does not read them. The runner
is a gate only because the settings file allows it and not `pytest` itself.
The gates that hold for every agent are the ones on GitHub's side.

The first three rows stop a merge, not a publish. The permissions-policy test
runs inside `Python Unit Tests`, and `build.yml` runs neither, so the daily build signs and promotes whatever is on `main`.
[`quality.md`](./quality.md#the-first-three-block-a-merge-not-a-publish) says
what does stop a publish.

## What is asked, not enforced

These are real rules, and nothing mechanical stops an agent breaking them.
Review is what catches it.

- **A person merges.** The ruleset needs no approval, because a sole maintainer
  cannot approve their own pull request. So a token that can write contents
  could merge a green pull request through the API. Only Claude Code sessions
  have the merge command denied. [`multi-agent.md`](./multi-agent.md#who-merges)
  records who has merged so far.
- **Never weaken a fail-closed check.** [`AGENTS.md`](../AGENTS.md) section 0
  rule 1. No test can tell a relaxed guard from a fixed cause.
- **The safety-critical statement.** [`CONTRIBUTING.md`](../CONTRIBUTING.md)
  item 3 asks for it, and the pull request template has a heading for it.
  Nothing fails when it is missing or empty.
- **Evidence in proportion to reach.** [`risk-tiers.md`](./risk-tiers.md) says
  an agent may not complete a Tier 3 change unattended. The `area/*` labels
  describe the tier; they do not block a merge.
- **No approval labels from automation.** The settings file denies creating,
  editing or deleting a label. Adding one to a pull request with `gh pr edit`
  only prompts.
  [`SECURITY-AI.md`](./SECURITY-AI.md#labels-carry-authority--automation-must-not-apply-them)
  has the list.

## Why there is no CODEOWNERS file

A `CODEOWNERS` file is the usual way to put a gate on part of a repository, and
here it would gate nothing. The ruleset sets `require_code_owner_review` to
`false` and needs no approval, for the reason above. A `CODEOWNERS` file would
only ask the one maintainer to review every pull request, which is already what
happens.

When there is a second reviewer,
[`branch-protection.md`](./branch-protection.md#when-there-is-a-second-reviewer)
is where the ruleset changes. Code-owner review belongs in the same change,
with a `CODEOWNERS` file naming the Tier 3 paths.
