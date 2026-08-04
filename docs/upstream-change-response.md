# Upstream Change Response

The image depends on several moving inputs: Fedora Kinoite, its kernel,
OpenZFS, the `Danathar/akmods` fork, the build container, and the GitHub
Actions runner/toolchain. A red build is expected to stop promotion while the
last known-good image remains available.

## First triage

Identify the failed job and exact step before changing repository code:

1. determine whether the failure is in input resolution, akmods, image
   composition, signing, or promotion
2. record the base-image digest, Fedora version, kernel release, OpenZFS
   version, akmods SHA, and cache image digest from the workflow summary
3. compare the failed run with the most recent successful run
4. inspect the relevant upstream repository or runner change

## Common failure classes

| Failure | First action |
|---|---|
| No matching `kmod-zfs` package | Confirm Fedora kernel/OpenZFS compatibility, then wait for or inspect `Danathar/akmods` |
| Final DNF transaction fails | Check the base image's existing ZFS userspace line and the configured OpenZFS line |
| Cache verification fails | Rebuild the cache on `main`; do not bypass signature or digest checks |
| Container build fails before steps run | Compare the job-level pinned build-container image and runner behavior |
| Signing fails | Check `SIGNING_SECRET`, environment restrictions, registry login, and the committed public key |
| Promotion fails | Keep `latest` unchanged; verify the candidate digest and signature before retrying |

## Response rules

Do not patch the cloned akmods checkout in a workflow run. If the akmods fork
needs a repository-specific fix, change that fork separately and let this repo
consume the resulting commit through its normal floating or explicit-ref
selection.

Do not weaken fail-closed checks to make a build green. If the base image or
kernel moved ahead of OpenZFS support, the correct short-term result is a red
candidate and an unchanged stable image.

For a temporary emergency freeze, set `AKMODS_UPSTREAM_REF` to a known-good
commit in `ci/defaults.json`, validate the result, and clear the pin when the
upstream issue is resolved. See [`akmods-fork-maintenance.md`](./akmods-fork-maintenance.md).

## After recovery

Record the root cause, the exact input versions, and the validation performed.
If the response changes workflow or trust boundaries, add or update a unit
test and document the new invariant before promotion.
