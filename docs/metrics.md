# Metrics

Reproducible commands, and an honest account of what the numbers are worth on a
repository this size.

There is no metrics service and no scheduled collector. Adding one would be more
machinery than the signal justifies — thirteen merged pull requests total at the
time of writing. What follows is what to run when the question actually comes
up, and how to avoid drawing a conclusion the data does not support.

[`docs/quality.md`](./quality.md) is the companion: this page is how to get
numbers, that one is what the gates and signals mean.

## Pull request acceptance

```bash
merged=$(gh pr list --state merged --limit 500 --json number -q 'length')
unmerged=$(gh pr list --state closed --limit 500 --json number,mergedAt \
  -q '[.[] | select(.mergedAt == null)] | length')
printf 'merged %s, closed unmerged %s\n' "$merged" "$unmerged"
```

At the time of writing: **13 merged, 0 closed unmerged**.

**That is not a 100% acceptance rate in any useful sense.** Every pull request
here so far was opened by the maintainer or by the repository's own automation,
and nothing has been rejected because nothing has needed to be. At n=13 on a
single-maintainer repository, the ratio measures how often the maintainer merges
their own work.

Break it out by author before reading anything into it:

```bash
gh pr list --state merged --limit 500 --json author -q \
  '[.[].author.login] | group_by(.) | map({author: .[0], merged: length}) | sort_by(-.merged)[]'
```

At the time of writing: 9 `Danathar`, 4 `app/danathar-atomic-hive`. Separate bot
pull requests from human ones before treating a trend as one.

## Which changes get pushed back on

More useful than the acceptance rate, because it is about substance rather than
count:

```bash
gh pr list --state merged --limit 50 --json number -q '.[].number' | while read -r n; do
  c=$(gh api "repos/{owner}/{repo}/pulls/$n/comments" -q 'length')
  [ "$c" -gt 0 ] && printf '%s\t%s\n' "$c" "$n"
done | sort -rn
```

Most inline review comments here come from `chatgpt-codex-connector[bot]`. Its
findings carry a severity badge and are *often* right, not automatically right.
A pull request that accumulated several P1/P2 findings is worth reading
afterward to ask whether the class of mistake is one the tests, the rubric in
[`review-rubric.md`](./review-rubric.md), or a fail-closed guard should have
caught first.

## Scheduled build health

The number that matters is **not** the pass rate. It is *how many consecutive
scheduled builds were lost*, because each one is a skipped image refresh and
therefore missed Fedora and security updates for anything tracking `:latest`.

```bash
gh run list --workflow build.yml --event schedule --limit 30 \
  --json createdAt,conclusion -q '.[] | "\(.createdAt[0:10])\t\(.conclusion)"'
```

### Read the causes, not the ratio

At the time of writing the last 40 `build.yml` runs were 27 scheduled failures
against 3 scheduled successes — a number that looks alarming and means almost
nothing on its own. Classifying nine consecutive failures gives three different
stories:

| Dates | Cause | Whose problem |
| --- | --- | --- |
| 2026-08-23 → 08-29 (7 runs) | `SIGNING_SECRET is not configured. Refusing to publish an unsigned production image…` | **This repository.** A fail-closed guard doing exactly its job while the signing key was unset. |
| 2026-08-30 | `Error: building at STEP "ADD https://copr.fedorainfracloud.org/…"` in the akmods build | Upstream COPR. |
| 2026-08-31 | `unexpected EOF` pulling the base image from `cdn01.quay.io` | Transient registry/CDN. |

Scheduled builds have succeeded on 2026-09-01, 09-02 and 09-03 since.

So "27 of 40 failed" is not one signal. Classify before reporting:

```bash
for id in $(gh run list --workflow build.yml --event schedule --limit 15 \
    --json databaseId,conclusion -q '.[] | select(.conclusion=="failure") | .databaseId'); do
  printf '%s  ' "$(gh run view "$id" --json createdAt -q '.createdAt[0:10]')"
  gh run view "$id" --log-failed 2>&1 | sed 's/\x1b\[[0-9;]*m//g' \
    | grep -oE "(SIGNING_SECRET is not configured|unexpected EOF|does not provide a kmod-zfs|Promoted digest mismatch|Error: building at STEP \"[A-Z]+ [^\"]{0,40})" \
    | head -1
done
```

`--log-failed` labels the step `UNKNOWN STEP` here and dumps a lot of
image-pull noise, so the grep is required rather than cosmetic.

## Unit CI health

```bash
gh run list --workflow test.yml --limit 30 \
  --json conclusion -q '[.[].conclusion] | group_by(.) | map({k:.[0],n:length}) | .[]'
```

At the time of writing: 29 success, 1 cancelled, 0 failures. This is the cheap
gate and it is expected to be green; a failure here is a real signal precisely
because it is rare.

## Coverage

Not a percentage. The number CI enforces is a **per-module covered-statement
floor**, and the gate prints every module's standing on each run:

```bash
python3 -m pytest tests/ \
  --cov=ci_tools --cov=shared \
  --cov=containerfiles/zfs-akmods --cov=files/scripts \
  --cov-branch --cov-report=json
python3 tests/check_coverage.py
```

`coverage: could raise <module>: floor N, reached M` means a test landed that
reaches more than the recorded floor. Raising it locks that in. See
[`CONTRIBUTING.md` → Coverage](../CONTRIBUTING.md#coverage) for why lowering one
is a decision rather than a command.

## What is deliberately not measured

- **A repository-wide coverage percentage as a gate.** It falls when code is
  added and lets one module mask another. `CONTRIBUTING.md` is explicit that
  the percentage moving is not a finding.
- **Time-to-merge and review latency.** At thirteen pull requests these are
  noise, and optimising them would push toward merging faster, which is the
  opposite of what a repository publishing a signed `:latest` wants.
- **Production execution coverage.** There is no instrumentation on the
  production workflows, which is a known gap tracked in
  [#10](https://github.com/Danathar/zfs-kinoite-complex/issues/10) — not a
  settled design.
