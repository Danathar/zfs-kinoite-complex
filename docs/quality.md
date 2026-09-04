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

Start with the failing step's stderr line, not the log body. A known refusal
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
