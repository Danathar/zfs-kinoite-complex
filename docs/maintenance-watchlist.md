# Maintenance Watchlist

This is the short list of moving parts that can change whether the image is
safe to build and publish.

## Fedora Kinoite and kernels

The default base currently tracks the Fedora Kinoite major tag configured in
`ci/defaults.json`. A new Fedora major or kernel can arrive before OpenZFS
supports it. Keep the build fail-closed until the akmods cache contains a
matching module for the selected primary kernel.

When changing the Fedora major:

1. update the base image in both `ci/defaults.json` and `Containerfile`
2. verify the corresponding akmods image tag convention
3. run the tests and a branch build
4. rebuild the shared cache on `main` before promotion

## OpenZFS

The configured minor line is not an independent promise: Fedora's existing
userspace packages constrain what can be installed in the final image. Test
the complete image composition after changing it.

## Akmods fork

The normal mode follows `Danathar/akmods` `main`, resolves that ref to one SHA,
and records the SHA for the run. The shared cache is still consumed by digest
and signature, so a reused cache may have been produced by an earlier fork
commit. Trace the cache image digest when establishing module provenance.

## Build and Actions dependencies

Review digest-pinned GitHub Actions, the privileged akmods job's build
container, Podman/buildah behavior, Chunkah, cosign, and the Fedora registry
tools. A change to a privileged job or registry trust path requires review of
the whole job boundary, not only the step that reports the failure.

## Signing and GitHub settings

Keep `cosign.pub`, `SIGNING_SECRET`, the `production-signing` environment, and
GHCR package permissions aligned. Verify that branch and environment rules
prevent untrusted branch runs from reaching the production signing key.

### Open: the `production-signing` environment is not branch-restricted

[`production-boundary-proposal.md`](./production-boundary-proposal.md) lists
"environment rules restricting the signing jobs to `main`" as a **required
setting**, and its verification checklist says `production-signing` must be
"unavailable to branch and pull-request runs".
[`zfs-kinoite-testing.md`](./zfs-kinoite-testing.md) goes further and states as
fact that signing happens "inside the `production-signing` environment that only
`main` refs can reach".

As checked on 2026-09-04 that restriction is not configured:

```bash
gh api repos/Danathar/zfs-kinoite-complex/environments \
  --jq '.environments[] | "\(.name): rules=\(.protection_rules|length) branch_policy=\(.deployment_branch_policy)"'
# production-signing: rules=0 branch_policy=null
```

`deployment_branch_policy: null` means any ref may deploy to it.

**Exposure today is limited, and only by workflow content rather than by the
setting.** No branch-reachable workflow declares that environment:
`build-branch.yml` never references `SIGNING_SECRET` at all, which
`tests/test_workflow_build_container.py` pins. So nothing currently reaches the
key from a branch. What is missing is the second layer the docs claim exists —
if any workflow ever declared `environment: production-signing` on a
branch-reachable path, the setting would not stop it.

This cannot be checked from CI without an admin-scoped token, which is why it
belongs here rather than in a test. Two things to decide: configure the
restriction, or correct the two documents that assert it is already in place.
Leaving both as they are is the one option that should not stand, because a
documented boundary nobody enforces is worse than an acknowledged gap.

## Runtime validation

CI does not import a real pool or boot the image. Before relying on a new
release, validate it on representative hardware or a disposable VM, including
module loading, ZFS userspace/kernel version agreement, pool import, and
rollback.
