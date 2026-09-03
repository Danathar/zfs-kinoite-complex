# Contributing

Read [`AGENTS.md`](./AGENTS.md) section 0 before your first change. This
repository is not a demo: the maintainer daily-drives the image it publishes,
on real hardware, with multi-terabyte ZFS pools attached, and there is no
staging tier between `main` and that machine. The rules in section 0 override
the general guidance that follows them, and they override this page too.

If a term is unfamiliar, check [`docs/glossary.md`](./docs/glossary.md) first.

## Submitting a change

1. Unit tests pass and `ruff check` is clean (see [Tests](#tests)).
2. Every changed line traces to the change you set out to make. Adjacent
   cleanups belong in their own pull request.
3. If you touched one of the safety-critical files named in `AGENTS.md`
   section 0 rule 2, say in the pull request body **what could reach a booted
   machine if the change is wrong**. That statement is the review, not a
   formality.
4. If you changed the ZFS line, the kernel it builds against, or anything
   pool-facing, address rollback explicitly: an image that activates newer
   pool features can leave the previous image unable to import those pools.

## Tests

[`docs/code-reading-guide.md`](./docs/code-reading-guide.md#running-tests) covers
the layout and the reading order. To match CI exactly, install the versions
[`.github/workflows/test.yml`](./.github/workflows/test.yml) installs:

```bash
pip install pytest pytest-cov "ruff==0.16.1"
python3 -m pytest tests/ -v
ruff check ci_tools/ shared/ tests/ files/ containerfiles/
```

`ruff` is pinned because an unpinned local copy can enable or disable different
rules release to release and disagree with CI in either direction; a Renovate
custom manager in [`renovate.json`](./renovate.json) tracks that pin. `pytest`
is only a runner here — the suite is `unittest.TestCase` throughout, so
`python3 -m unittest discover -s tests` also works with nothing installed at
all, which is useful when you have no network.

## Coverage

CI reports coverage on every pull request and push to `main`. To reproduce the
number locally:

```bash
python3 -m pytest tests/ \
  --cov=ci_tools --cov=shared \
  --cov=containerfiles/zfs-akmods --cov=files/scripts \
  --cov-branch --cov-report=term-missing
```

**Coverage is reported, not gated.** There is no `--cov-fail-under`, and no
pull request is blocked on the percentage. The number exists so a reviewer can
read it off the job log instead of reconstructing it by hand.

### Two tiers, and they answer different questions

This repository has one *measured* tier and one *real but unmeasured* tier.
They are not interchangeable, and neither one alone means "tested".

| | What it measures | Where it runs | Instrumented? |
|---|---|---|---|
| **Unit coverage** | Decision logic, with **every** external call mocked | `test.yml`, on every PR and push | Yes |
| **Production execution** | The same modules against a real registry, real `cosign`, real `git` remote, real `podman` | `build.yml`, `build-pr.yml`, `build-branch.yml`, `akmods-failure-triage.yml` — daily | **No** |

The first row is the important caveat. As
[`docs/code-reading-guide.md`](./docs/code-reading-guide.md#running-tests)
states, all subprocess, registry, and filesystem calls are mocked so the suite
runs without network access or container tooling. That is the right design for
a fast suite, but it means **a module at 100% unit coverage may never have had
its real-world path exercised**, and a module in the 60s may be running
against a live registry several times a day.

So the two tiers are close to orthogonal. Read them together or you will draw
the wrong conclusion from either.

### Establishing whether a path runs in production

There is no coverage instrumentation on the production workflows, so today the
only way to answer this is by hand — `gh run list` for the trigger mix, then
the job logs for the branch you care about. For example, at the time of
writing, `build.yml`'s last 40 runs (2026-08-04 to 2026-09-02) were 29
`schedule` and 5 `push`, with **zero** `workflow_dispatch`. Any path reachable
only from a manual dispatch had therefore not run in production for a month.

This does not scale as a review practice, and that is a known gap rather than a
settled design — see [#10](https://github.com/Danathar/zfs-kinoite-complex/issues/10).
If you find yourself doing this archaeology repeatedly for the same file, say
so on that issue.

### Priority order for a real gap

Rank a coverage gap by how many tiers miss it, not by the percentage:

1. **Covered by neither.** The highest priority. The worked example is
   `resolve_build_inputs.py`'s replay mode (`use_input_lock=true`): it is
   reachable only from a manual `workflow_dispatch`, so its fail-closed
   validation guards had no unit tests *and* no production execution. It is
   also an incident-reproduction tool, meaning a broken guard would have
   surfaced only when someone needed to replay a build to diagnose an
   outage — the worst possible moment. See
   [#9](https://github.com/Danathar/zfs-kinoite-complex/issues/9).
2. **Runs in production, no unit test.** Real but unverified: you find out it
   broke by watching a build fail. Worth a test, lower urgency.
3. **Unit-tested only, where a real call would behave differently.** Mocks
   agree with whatever you told them. Registry error shapes, `cosign` output
   formats, and `skopeo` failure messages are the usual offenders.

### What not to file

- **The percentage itself.** A number moving from 78% to 76% is not a finding.
  Name the path and which tier misses it.
- **Coverage of a fail-closed guard obtained by weakening it.** `AGENTS.md`
  section 0 rule 1 is absolute: when a check fires because ZFS does not match
  the primary kernel, a signature will not verify, a digest does not match, or
  the akmods cache does not cover the required kernel, the fix is the
  underlying cause. Never relax the check, add a fallback, or make it
  best-effort — including to make a test easier to write.
- **A test that asserts only that something raises.** Prefer pinning the
  message or the specific guard. `assertRaises(CiToolError)` passes for any
  reason at all, including an unrelated missing environment variable, so it can
  report a guard as covered when the guard never ran.

### A note on mocked error paths

Because every external call is mocked, error-handling branches are the ones
most likely to be covered on paper and untested in fact. When a branch exists
to handle a *real* failure — a registry timeout, a rate limit, a malformed
manifest — prefer a test that produces the real failure shape over one that
patches the call to raise. `tests/test_common.py`'s timeout tests are written
this way: they drive a genuine `subprocess.TimeoutExpired` through `run_cmd`
and assert on the classification consequence, not merely that something raised.

## Documentation

[`docs/documentation-guide.md`](./docs/documentation-guide.md) is the map, and
its "Where To Put New Documentation" section says which file new prose belongs
in. Add new documents to that tree and to the router table in
[`README.md`](./README.md) in the same change.
