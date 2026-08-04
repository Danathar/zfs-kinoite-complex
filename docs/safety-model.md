# Safety Model And Recovery Policy

Read this before using `:latest` in a test environment. This repository builds
a testing-only Fedora Kinoite image with an out-of-tree ZFS kernel module; a bad image can
prevent a machine from booting or leave its ZFS pools unavailable.

Unlike `zfs-aurora-complex`, this repository is not used in production. Anyone
forking it or using its artifacts should treat them as test artifacts and
carefully evaluate whether they are suitable for their own machines or pools.

The author tests the image in virtual machines and intends to keep the build
and test pipeline active as images evolve. That testing goal does not change
the production boundary.

## What the image guarantees

The workflow is deliberately candidate-first:

1. resolve and record the Fedora Kinoite base, kernel set, OpenZFS release,
   akmods commit, and cache digest
2. reuse or rebuild a signed akmods cache containing the supported primary
   kernel's `kmod-zfs` package
3. build and sign a candidate image
4. run image validation and promotion checks
5. move `latest` only after the exact signed candidate digest passes

If a candidate fails, the previous `latest` digest remains in place.

CI does not boot the image or import a real pool before promotion. The
`bootc container lint` check, package/module checks, signature verification,
and unit tests are useful gates, but they are not a substitute for testing the
image on disposable hardware and test pools.

## ZFS userspace and kernel-module versions

`DEFAULT_ZFS_MINOR_VERSION` selects the OpenZFS line used for the akmods build.
The Fedora Kinoite base may already contain ZFS userspace packages, so the
selected line must remain compatible with that base. A successful akmods build
alone does not prove that the final image's package transaction will succeed.

Do not change the ZFS line casually. When changing it, validate both the
akmods cache build and the final image composition before promoting anything.

The image supports the newest detected kernel as its primary kernel. Older
kernels that happen to remain in the base image are not promised to have a
matching ZFS module; image rollback is the supported recovery path.

## Pool recovery discipline

Do not run `zpool upgrade` merely because `zpool status` reports that newer
features are available. Importing a pool does not enable those features, but
`zpool upgrade` is a one-way compatibility decision. Keep the previous image
available as a rollback target until the new ZFS line and image have been
trusted on the machine.

If the new deployment is bad:

```bash
sudo bootc rollback
sudo systemctl reboot
```

After reboot, inspect `bootc status`, verify that the expected deployment is
active, and confirm the pool state before attempting another upgrade.

## Updates and signing

This image does not make a failed candidate user-visible. Scheduled builds are
gated on movement of the Fedora Kinoite base or a newer OpenZFS patch; pushes
and manual runs build when requested.

Published images and the shared akmods cache must be signed. The private key
belongs in the `production-signing` GitHub Environment as `SIGNING_SECRET`,
with that environment restricted to `main`. The repository contains only the
public verification key, `cosign.pub`.

The workflow also requires the repository owner to configure the relevant
GitHub package permissions, environment protection, and branch settings. Those
settings are external to the files committed here and must be reviewed before
using the image as a production update target.

## Operator checklist

Before switching a machine:

1. read the release notes and workflow result for the exact image digest
2. keep a known-good deployment available for rollback
3. confirm the image signature policy is installed and enforced
4. test ZFS import, `zpool status`, and module loading after reboot
5. do not run `zpool upgrade` until rollback is no longer needed

This is a testing image stream, not a production image and not a Fedora or
OpenZFS support commitment.
