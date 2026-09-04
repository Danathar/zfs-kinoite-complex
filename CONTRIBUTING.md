# Contributing

Read [`AGENTS.md`](./AGENTS.md) section 0 before your first change, and
[`docs/safety-model.md`](./docs/safety-model.md) for what this repository
actually guarantees today: it is testing-only, exercised in VMs, and not yet
used in production (see the warning in [`README.md`](./README.md#install)).
AGENTS.md's caution rules apply regardless — a bad image can still strand a
test machine or a real pool someone attached to try it — and they override the
general guidance that follows them, and they override this page too.

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
the layout and the reading order. To match what
[`.github/workflows/test.yml`](./.github/workflows/test.yml) installs today:

```bash
pip install pytest "ruff==0.16.1"
python3 -m pytest tests/ -v
ruff check ci_tools/ shared/ tests/ files/ containerfiles/
```

`ruff` is pinned because an unpinned local copy can enable or disable different
rules release to release and disagree with CI in either direction; a Renovate
custom manager in [`renovate.json`](./renovate.json) tracks that pin. `pytest`
is only a runner here — the suite is `unittest.TestCase` throughout, so
`python3 -m unittest discover -s tests` also works with nothing installed at
all, which is useful when you have no network.

Add `pytest-cov` for the [Coverage](#coverage) section below; `test.yml` does
not install it yet at the time of writing, so a local coverage run is not yet
something CI also produces on every PR.

`tests/e2e/` is collected by that same command and needs nothing extra. It runs
the CLI as a real subprocess rather than importing it — see
[`tests/e2e/README.md`](./tests/e2e/README.md) — and moves no coverage
number, because the code it exercises runs in a child process.

## Coverage

CI reports coverage on every pull request and push to `main` once
[#12](https://github.com/Danathar/zfs-kinoite-complex/pull/12) lands; until
then, reproduce the number locally the same way that workflow step will:

```bash
python3 -m pytest tests/ \
  --cov=ci_tools --cov=shared \
  --cov=containerfiles/zfs-akmods --cov=files/scripts \
  --cov-branch --cov-report=term-missing
```

**Coverage is reported, not gated.** There is no `--cov-fail-under`, and no
pull request is blocked on the percentage. The number exists so a reviewer can
read it off the job log instead of reconstructing it by hand.

### Three tiers, and they answer different questions

This repository has one *measured* tier, one *unmeasured but unmocked* tier,
and one *real but unmeasured* tier. They are not interchangeable, and no one of
them alone means "tested".

| | What it measures | Where it runs | Instrumented? |
|---|---|---|---|
| **Unit coverage** | Decision logic, with **every** external call mocked | `test.yml`, on every PR and push | Yes |
| **End-to-end (`tests/e2e/`)** | The process boundary a workflow step depends on: exit status, and the `GITHUB_OUTPUT` file a later step reads | `test.yml`, on every PR and push | **No** — the code under test runs in a child process |
| **Production execution** | The same modules against a real registry, real `cosign`, real `git` remote, real `podman` | `build.yml`, `build-pr.yml`, `build-branch.yml`, `akmods-failure-triage.yml` — daily | **No** |

The first row is the important caveat. As
[`docs/code-reading-guide.md`](./docs/code-reading-guide.md#running-tests)
states, all subprocess, registry, and filesystem calls are mocked there so the
suite runs without network access or container tooling. That is the right
design for a fast suite, but it means **a module at 100% unit coverage may
never have had its real-world path exercised**, and a module in the 60s may be
running against a live registry several times a day.

The second row exists because of a specific blind spot in the first, and
[`tests/e2e/README.md`](./tests/e2e/README.md) states it: an in-process test
calls `main()`, so it never observes the exit status. A guard that reports a
problem on stderr and then exits `0` does not stop a workflow step running
under `set -e`, and the unit suite passes on it either way. That tier reaches
no registry, `cosign`, `podman`, or `git`, so it is not a substitute for the
third row.

So the tiers are close to orthogonal. Read them together or you will draw the
wrong conclusion from any one of them.

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
patches the call to raise. [#13](https://github.com/Danathar/zfs-kinoite-complex/pull/13)
adds an example of this once merged: `tests/test_common.py` drives a genuine
`subprocess.TimeoutExpired` through `run_cmd` and asserts on the
classification consequence — specifically, that the resulting error message
is never mistaken for `_MISSING_IMAGE_ERROR_MARKERS` — rather than asserting
only that something raised.

## Documentation

[`docs/documentation-guide.md`](./docs/documentation-guide.md) is the map, and
its "Where To Put New Documentation" section says which file new prose belongs
in. Add new documents to that tree and to the router table in
[`README.md`](./README.md) in the same change.
