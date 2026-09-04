# Corrections

Newest first. See [`README.md`](README.md) for what belongs here.

---

## Checking that the CLI accepts a command by *running* it can publish

**Believed:** to verify that every `python3 -m ci_tools.cli <command>` a
workflow invokes is still a valid command, run each one with an empty
environment. They all stop at their first `require_env`, so it is a read-only
probe.

**True:** the CLI dispatches as soon as the parser accepts a name, so that runs
the handler. Eighteen of the nineteen do stop at a `require_env`.
`akmods-build-and-publish` does not: with no `KERNEL_RELEASE` it falls through
to `just build`, `just login`, `just push`, `just manifest`
(`ci_tools/akmods_build_and_publish.py:141-146`). Its guard is
`AKMODS_WORKTREE.exists()`, and `AKMODS_WORKTREE` is the absolute `/tmp/akmods`
— so choosing a different working directory does not isolate it. On a machine
that had run the clone step, with whatever authentication was to hand, a test
could have published.

**Established by:** reading `akmods_build_and_publish.py:141-146` after a Codex
review raised it on #61. Confirmed by running each of the nineteen commands
with `env -i` and reading the exit code and first stderr line — seventeen
`Missing required environment variable`, one `Expected akmods checkout at
/tmp/akmods`, and `classify-akmods-failure` exiting **0** while writing
`artifacts/akmods-failure.json` into the working directory.

**Avoid by:** never dispatching to check a name. An unknown command is rejected
during parsing, before any handler runs, and argparse names every accepted
choice when it does — so one deliberately invalid command yields the whole set.
More generally: in this repository, "it will fail fast anyway" is a claim about
a code path, and it has to be read rather than assumed.

---

## `unittest discover` silently skips a test directory with no `__init__.py`

**Believed:** `tests/e2e/` is picked up by both runners, because `pytest tests/`
collects it recursively.

**True:** `pytest` does. `python3 -m unittest discover -s tests` only recurses
into an importable subdirectory, so without `tests/e2e/__init__.py` it found
**218** tests instead of 228 and reported `OK`. CONTRIBUTING advertises that
command as the no-dependencies path, so a whole tier would have been quietly
absent from it.

**Established by:** running both runners before and after adding the
`__init__.py` — 218/`OK` versus 228/`OK`.

**Avoid by:** treating "the suite passed" as a claim about a *count*. When
adding a test directory, check that both runners report the same number.

---

## The build-inputs artifact is not named `build-inputs`

**Believed:** `gh run download <run-id> --name build-inputs` fetches the
manifest a run recorded.

**True:** the artifact is `build-inputs-${{ github.run_id }}`
(`.github/actions/prepare-main-akmods/action.yml:170`). The file inside is
`build-inputs.json`; the artifact is not.

**Established by:** reading the `upload-artifact` step rather than inferring the
name from the file it uploads.

**Avoid by:** reading the `name:` on the upload step. This one is cheap to get
wrong because the plausible name is also the filename.

---

## The `E402` per-file-ignore is inert with the pinned ruff

**Believed:** `per-file-ignores` for
`containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py` is what keeps
`ruff check` clean over its post-`sys.path` imports.

**True:** ruff 0.16.1's default rule set is 415 rules and does not include
`E402`. `ruff check --isolated` on that file passes; `--select E402` reports two
violations. The ignore only starts mattering if the rule set is widened.

**Established by:** `ruff check --isolated --show-settings`, then `--isolated`
versus `--isolated --select E402` on the file itself.

**Avoid by:** not assuming ruff's defaults are `E4,E7,E9,F`. They have not been
for some time, and "the config must be load-bearing or lint would fail" is not
an argument — check by removing it.

---

## CONTRIBUTING said `test.yml` does not install `pytest-cov`

**Believed:** per `CONTRIBUTING.md`, a local coverage run is not something CI
also produces on every pull request.

**True:** `.github/workflows/test.yml` installs `pytest pytest-cov
"ruff==0.16.1"` and has reported coverage on every run since #12 merged. The
sentence is stale.

**Established by:** reading the `Install test runner and linter` step.

**Avoid by:** AGENTS.md section 0 rule 3 — check the workflow, not the page
describing it. This file is a worked example of that rule rather than an
exception to it.
