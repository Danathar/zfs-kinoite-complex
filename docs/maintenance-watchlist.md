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

## Runtime validation

CI does not import a real pool or boot the image. Before relying on a new
release, validate it on representative hardware or a disposable VM, including
module loading, ZFS userspace/kernel version agreement, pool import, and
rollback.
