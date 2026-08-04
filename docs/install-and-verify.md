# Install, Rebase, And Verify

If a term is unfamiliar, check the shared glossary first:
[`docs/glossary.md`](./glossary.md)

## Purpose

Operator-facing steps: switching a machine onto this image, checking that
ZFS actually works afterwards, and verifying the image signature by hand.

## Install And Rebase

> [!WARNING]
> **Testing-only:** unlike `zfs-aurora-complex`, this repository is not used in
> production. Anyone forking it or using its artifacts should treat them as
> test artifacts, use a disposable VM, disposable pool, or dedicated test
> hardware, and evaluate the risks for their own environment. The author tests
> it in VMs and aims to keep the image building and testing as it evolves. The
> pipeline builds, signs, and promotes candidates, but it does not boot the
> image or import a pool before `:latest` moves.

Fresh stock Fedora Kinoite can switch to the published image after the GitHub workflow
has produced a signed `latest` tag:

```bash
sudo bootc switch --enforce-container-sigpolicy ghcr.io/danathar/zfs-kinoite-complex:latest
sudo systemctl reboot
```

That `--enforce-container-sigpolicy` flag is intentional. It makes the first
custom-image deployment use the in-image container signature policy instead of
recording the origin as an unverified registry image.

If a test VM was already switched with plain `bootc switch`, switch it again
with the command above and reboot before relying on `bootc upgrade`.

Why this image flow stays easier to reason about:

1. the stable and candidate image tags live in the same repository
2. after you boot into this image family once, the in-image policy only needs to trust one repository path
3. there is no dual-repository policy normalization or host repair path to keep in sync

## Quick Validation After Boot

```bash
rpm -q kmod-zfs
modinfo zfs | head
lsmod | grep '^zfs'
zpool --version
zfs --version
distrobox --version
brew --version
```

For virtual machine (VM) testing with a secondary disk:

```bash
sudo wipefs -a /dev/vdb
sudo zpool create -f -o ashift=12 -O mountpoint=none testpool /dev/vdb
sudo zfs create -o mountpoint=/var/mnt/testpool testpool/data
sudo zpool status
sudo zfs list
```

## Signature Verification

```bash
cosign verify \
  --key cosign.pub \
  --new-bundle-format=false \
  ghcr.io/danathar/zfs-kinoite-complex:latest
```

`--new-bundle-format=false` is required: this repo signs with legacy cosign
registry attachments so Fedora/Kinoite's bootc signature policy path can
discover them via `use-sigstore-attachments`, which default cosign v3
verification does not use. For the full signing model, key rotation, and the
in-image trust policy, read [`docs/signing-and-bootc.md`](./signing-and-bootc.md).

## Testing An Unsigned Branch Image

`br-*` branch tags are **deliberately unsigned**. Branch workflow runs cannot
reach the production signing key (it is scoped to a `main`-only environment),
and they publish test images through an explicit unsigned opt-in instead. That
makes them a different kind of artifact from everything else in this
repository, with three hard rules:

1. **Fresh, throwaway VMs only.** A machine already enforcing this repository's
   signature policy -- including any machine that followed the install steps
   above -- will refuse to pull an unsigned `br-*` tag. That refusal is the
   policy working, not a bug.
2. **Plain `bootc switch`, no `--enforce-container-sigpolicy`.** Enforcement
   cannot be enabled against an unsigned image. This is a deliberate, weaker,
   test-only posture -- the opposite of the rule for real installs above.
3. **Never let such a VM become a durable machine.** `bootc upgrade` on it will
   keep tracking the unsigned branch tag with no verification, indefinitely,
   and it can never be moved into enforcement while it does. Test, conclude,
   delete the VM.

To test the real, signed artifact instead, run the main workflow with
`workflow_dispatch` and `promote_to_stable=false`: that publishes a fully
signed `candidate-*` tag from `main` -- switchable with enforcement, like any
other validated test deployment -- without moving `latest`.
