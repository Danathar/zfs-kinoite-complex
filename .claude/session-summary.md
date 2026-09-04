# Session summary

Carried state between agent sessions. Read this before starting; update it
before finishing.

Not a changelog — `git log` is the changelog, and it is better than this file
will ever be. This holds only what a fresh session cannot recover from the tree:
work in flight, decisions taken but not yet visible in a diff, and things known
to be true that no file states.

Keep it short. If a section grows past a screen, the durable part of it belongs
somewhere permanent:

- A wrong belief that cost time → [`.claude/memory/corrections.md`](memory/corrections.md)
- How something works → `docs/`, per [`docs/documentation-guide.md`](../docs/documentation-guide.md)
- A pin or decision on a clock nobody watches → [`docs/maintenance-watchlist.md`](../docs/maintenance-watchlist.md)
- A procedure → [`.github/prompts/`](../.github/prompts/)

Stale content here is worse than an empty file: a session that trusts it will
act on it. When in doubt, delete the section rather than leave it uncertain.

---

## Current state

Last updated: 2026-09-03.

`:latest` is published and current — ZFS 2.4.4 on kernel
`7.1.12-200.fc44.x86_64`. Scheduled `build.yml` runs have been green since
2026-09-01.

That follows a run of failures worth knowing about, because the shape recurs:
seven consecutive scheduled runs from 2026-08-23 to 08-29 failed on
`SIGNING_SECRET is not configured`, which is a fail-closed guard refusing to
publish an unsigned production image — not a broken build. See
[`docs/quality.md`](../docs/quality.md) for how to tell that class of failure
apart from the two others in the same window.

## In flight

Nothing. Update this when leaving work unfinished, with enough to resume:
branch, what is done, what is not, and what you were about to check.

## Standing constraints a session should not rediscover

- **Merging to `main` publishes.** `build.yml` triggers on push to `main`
  (excluding `**/*.md` and `docs/**`), then builds, signs, and promotes to
  `:latest`. There is no staging tier. AGENTS.md section 0 rule 6: propose, do
  not push, promote, tag, or delete.
- **`main` is not branch-protected.** A green `Python Unit Tests` check blocks
  nothing; a person declining to merge is what blocks.
- **Image builds are slow.** A pull request triggers `Build Or Reuse Shared ZFS
  Akmods Cache`, `Build PR Image (No Push)` and `Build Branch Image`, which take
  tens of minutes. `Python Unit Tests` returns in about ten seconds. Do not
  read a pending image build as a hang.
- **`unexpected EOF` during a blob copy is a registry or CDN failure**, common
  enough here to rule out first, and never a reason to change code.
