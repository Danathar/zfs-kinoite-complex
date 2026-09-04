# Quality

Where the signal about this repository actually comes from, and what each source
can and cannot tell you.

There is no dashboard service here. The dashboard is three badges in
[`README.md`](../README.md), a handful of gates, and a set of fail-closed checks
— and the useful thing to write down is what each one *means*, because two of
the badges are routinely misread and the most important gates are the ones that
stop a bad image rather than the ones that lint code.

[`docs/metrics.md`](./metrics.md) is the companion: this page is what the
signals mean, that one is how to get numbers.

## The badges

| Badge | Source | Answers |
| --- | --- | --- |
| **build** | Actions status for `build.yml` on `main` | Did the last production run pass? |
| **last good build** | `status` branch payload written from the published `:latest` image | Is there a usable signed image, and how old is it? |
| **OpenZFS/kernel status** | `status` branch payload written by `akmods-failure-triage.yml` | If the build is red, is it because ZFS cannot be built for the current kernel? |

**Read them together.** A red **build** with a recent **last good build** means
the published image is still installable and still boots — it just has not been
refreshed. That is a very different situation from a red build with a stale last
good build, which means machines tracking `:latest` are drifting.

**A cancelled run reads as a failed one.** The **build** badge is GitHub's own,
and it reports the most recent *completed* run — where "completed" includes
cancelled. A cancelled run renders as `failing`, identically to a real failure.

That is not hypothetical here, because `build.yml` is built to cancel itself:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref || github.run_id }}
  cancel-in-progress: true
```

So **merging several pull requests to `main` in quick succession turns the badge
red**, and it stays red until the last run finishes. Each merge starts a run and
cancels the one before it. Nothing is wrong, the guard is doing exactly what its
comment says — never letting two `main` runs race to publish — and the signed
`:latest` from the last completed promotion is still there the whole time.

Two ways to avoid inflicting it on yourself: space the merges out, or merge the
documentation-only pull requests last, since `build.yml`'s `paths-ignore`
(`**/*.md`, `docs/**`) means those start no run at all.

Note the asymmetry with this repo's own badges. `write_akmods_badge.py`
deliberately refuses to let a cancelled or skipped run change the OpenZFS/kernel
badge. GitHub's build badge has no such rule, and this repo does not control it.

Two properties of the badge pipeline are deliberate:

- **It refuses to guess.** `write_akmods_badge.py` leaves the previous badge
  state untouched on any conclusion it cannot interpret, rather than
  overwriting it with something invented. A stale-but-true badge beats a
  fresh-but-wrong one — so a badge that has not moved is not automatically a
  badge that is working.
- **A skipped or cancelled run cannot turn a red badge green.** That rule is
  enforced inside `ci_tools/write_akmods_badge.py`, not only in the workflow
  that calls it, so it stays correct if the workflow is edited.

## The gates

| Gate | Runs on | Blocks |
| --- | --- | --- |
| `Python Unit Tests` (`test.yml`) | every PR and push to `main` | **nothing automatically** — see below |
| `ruff check` | same job | same |
| Per-module coverage floors (`tests/check_coverage.py`) | same job | same |
| `Evaluate Stable Signal Gate` (`build.yml`) | scheduled and `main` pushes | *skips* the build on a scheduled run when upstream has not moved (`build.yml:97`) — a skip, not a failure |
| `check-akmods-cache` strict mode | before the candidate build | the build, if the cache does not carry a matching `kmod-zfs` |
| `sign-image` | before publish | publishing — an unsigned production image is refused outright |
| `promote-stable` digest re-read | after the copy to `:latest` | promotion, if the copy did not land at the signed digest |
| `nightly-compliance.yml` | 05:00 UTC daily, and on dispatch | nothing — it reports. See below. |

### The first three block nothing on their own

`main` is **not branch-protected** — `gh api repos/{owner}/{repo}/branches/main/protection`
returns `Branch not protected`. So a red `Python Unit Tests` does not prevent a
merge; a person deciding not to merge is what prevents it. Worth knowing before
treating a green check mark as a gate.

The rows below it are different: those run inside `build.yml` and stop the
publish itself.

### What each one does not cover

**`Python Unit Tests` cannot reach the image.** Everything under `tests/` except
`tests/e2e/` mocks every external call, and `tests/e2e/` runs the CLI as a
subprocess but touches no registry, `cosign`, `podman`, or `git`. The
`Containerfile`, `build_files/`, and the image-side scripts only execute inside
an image build. A green suite is not evidence for a change to any of them.

**Coverage floors measure the unit tier only.** A module at 100% may never have
had its real-world path exercised, and a module in the 60s may be running
against a live registry several times a day. `CONTRIBUTING.md` sets out the
three tiers and the priority order for a real gap.

**There is no coverage instrumentation on the production workflows.** Whether a
path actually runs in production is established by hand today, which does not
scale as a review practice. That is a known gap, tracked in
[#10](https://github.com/Danathar/zfs-kinoite-complex/issues/10).

### The nightly job answers a question the others cannot

Every gate above runs when something changes, and every signature check among
them runs at *publish* time against an image the same run just built.
[`nightly-compliance.yml`](../.github/workflows/nightly-compliance.yml) asks a
different question on a clock: **does the artifact a user would pull right now
still verify against the committed `cosign.pub`?**

A tag can move, a signature can be pruned, a registry can lose something — none
of which involves a commit, and none of which any other check here would
notice. It runs an hour before `build.yml`'s 06:00 schedule so a failure is
visible before the production build rather than tangled up with it, and it uses
the exact command [`install-and-verify.md`](./install-and-verify.md) gives
users, `--new-bundle-format=false` included, because verifying differently from
the documented command would test something users are not doing.

It also re-runs the unit suite and the coverage gate. That is deliberate
duplication: a dependency of the *runner* can break those with no commit on this
side, and that failure is worth seeing on its own rather than landing on whoever
opens the next pull request.

## The checks that actually protect a booted machine

These are not lint. Each refuses to continue rather than publish something it
could not verify, and each has a message you will see in a job log:

| Refusal | What it prevents |
| --- | --- |
| `SIGNING_SECRET is not configured. Refusing to publish an unsigned production image…` | An unsigned image reaching a machine whose policy requires a signature — which would either fail verification or, worse, succeed on a machine with a looser policy. |
| `Shared akmods cache … does not provide a kmod-zfs for primary kernel … at ZFS version …` | An image labelled with one ZFS version while shipping another. |
| `Promoted digest mismatch: … produced …, expected …` | `:latest` pointing at something other than what was signed. |
| `Missing required verification key file: …` | Signing or promotion proceeding without the public key that makes the signature checkable. |

When one of these fires, the build is **working**, not broken. The repository's
first rule (AGENTS.md section 0 rule 1) is that the fix is the underlying cause
— never relaxing the check.

## Reading a red build

**First, check which workflow went red.** It changes what the failure means, and
only one of them can publish anything signed:

| Workflow | Runs on | A red run means |
| --- | --- | --- |
| `build.yml` | push to `main`, and 06:00 UTC daily | The production path. Nothing was signed or promoted; `:latest` still points at the last good promotion. |
| `build-pr.yml` | every pull request | Validation only. Its header says it intentionally stops before any push or signing step, so nothing was published either way. |
| `build-branch.yml` | push to any branch except `main` and `ai-fix/**` | A branch test image. Publishes *unsigned* `br-*` tags, and only for human-attributed pushes. Never promoted. |
| `test.yml` | every pull request and push to `main` | Lint, unit suite, coverage floors. Blocks nothing automatically — see above. |

**Then check that it actually failed.** `gh run list --workflow build.yml
--limit 10 --json createdAt,conclusion` distinguishes `failure` from
`cancelled`, and the badge does not. A run of `cancelled` entries with no
`failure` among them means someone was merging quickly, not that anything
broke.

Then start with the failing step's stderr line, not the log body. A known refusal
exits 1 with one line, and that line is the diagnosis.
[`.github/prompts/diagnose-build-failure.prompt.md`](../.github/prompts/diagnose-build-failure.prompt.md)
is the procedure, including the table mapping each refusal to what to do.

Three things worth knowing before you conclude anything:

1. **A run of scheduled failures is not one story.** Nine consecutive failures
   in August 2026 were seven `SIGNING_SECRET` refusals, one upstream COPR
   failure, and one quay.io CDN transfer failure. `docs/metrics.md` has the
   command that classifies them.
2. **Rule out a third-party service before anything else.** Every workflow here
   pulls from quay.io, pushes to ghcr.io, and bootstraps `cosign` from
   Sigstore, so three different external services can turn a run red without
   anything in this repository being wrong. All three shapes have been observed:

   | Message | Where it surfaces | What actually happened |
   | --- | --- | --- |
   | `happened during read: unexpected EOF` | pulling the base image, in `Build Candidate Image` or `Build Or Reuse Shared ZFS Akmods Cache` | A quay.io CDN blob transfer died mid-download. |
   | `writing blob: … received unexpected HTTP status: 500` | pushing, in `Build Branch Image` | ghcr.io returned a server error on a layer upload. |
   | `Error: trusted root is required when using new bundle format`, preceded by `failed to download https://tuf-repo-cdn.sigstore.dev/…root.json, http status code: 403` | the `Install skopeo and cosign` step, before any of this repo's own code runs | Sigstore's TUF CDN was unavailable, so the installer could not verify the `cosign` binary it downloads. |

   The third one is the trap. Its message names a trusted root and a bundle
   format, so it reads like a signature-policy problem in this repository — and
   it is not: it fails in a *tool installation* step, before `sign-image` or
   `check-akmods-cache` have run at all. Check which step failed before
   concluding anything about signing. The preceding `403` line is the tell.
3. **A green pipeline is not a good image.** A successful run proves the build
   completed. Say which run you read, and what it actually proves.
