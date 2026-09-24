# zfs-kinoite-complex

[![build](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/build.yml)
[![tests](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/test.yml)
[![nightly compliance](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/nightly-compliance.yml/badge.svg?branch=main)](https://github.com/Danathar/zfs-kinoite-complex/actions/workflows/nightly-compliance.yml)
[![last good build](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FDanathar%2Fzfs-kinoite-complex%2Fstatus%2Flast-good-build-badge.json)](https://github.com/Danathar/zfs-kinoite-complex/pkgs/container/zfs-kinoite-complex)
[![OpenZFS/kernel status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FDanathar%2Fzfs-kinoite-complex%2Fstatus%2Fakmods-badge.json)](https://github.com/Danathar/zfs-kinoite-complex/issues?q=is%3Aissue+is%3Aopen+label%3Aakmods-failure)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Danathar/zfs-kinoite-complex)
[![Maintenance assisted by Hivecommons Hive](https://img.shields.io/badge/maintenance%20assisted%20by-Hivecommons%20Hive-1f6feb)](https://github.com/hivecommons/hive)
[![ACMM L4 Security-Aware](https://img.shields.io/badge/ACMM-L4%20Security--Aware-2da44e)](https://github.com/hivecommons/hive#acmm-levels)
[![AI assisted](https://img.shields.io/badge/AI-assisted-d29922)](#about-this-project)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)

## Why This Repo Exists

Fedora-family images move kernels quickly, and ZFS is an out-of-tree kernel module — so a new
Fedora kernel can land before a matching OpenZFS release exists. Build that carelessly and you
publish a Fedora Kinoite image whose kernel and ZFS modules do not match.

This repository builds a signed Fedora Kinoite image with ZFS userspace and kernel modules installed
from a self-hosted akmods cache image (a container image holding prebuilt ZFS kernel-module
packages), Distrobox from Fedora Kinoite, Homebrew from the `ublue-os/brew` payload, and a single-repository
signing policy for signed `bootc upgrade`. It deliberately stays close to standard tooling: one
`Containerfile`, direct `buildah`/Open Container Initiative (OCI) build arguments, one image
repository (`ghcr.io/danathar/zfs-kinoite-complex`), and one shared akmods cache repository
(`ghcr.io/danathar/zfs-kinoite-complex-akmods`).

OpenZFS is not hand-pinned to a patch version. Each build resolves the newest stable release in
a configured minor line (`ZFS_MINOR_VERSION`, `2.4` by default — see
[`ci/defaults.json`](./ci/defaults.json)) from
[OpenZFS's own GitHub releases](https://github.com/openzfs/zfs/releases) at build time, and that
is the version it attempts to build and install.

> [!IMPORTANT]
> **Changing this repository is a safety-sensitive change, not a demonstration.** A bad build can
> break a booted test machine and put pooled data at risk, so the build, promotion, and signing
> paths are held to a high standard — understand the blast radius before changing them. AI agents
> working here must read [`CLAUDE.md`](./CLAUDE.md) first.

> [!NOTE]
> Developed with significant AI assistance — see [About this project](#about-this-project).
> For a simpler, more direct approach to the same problem, see
> [`aurora-zfs-simple`](https://github.com/Danathar/aurora-zfs-simple) — the minimal expression
> of the same idea. This repo carries the fuller pipeline: candidate-first promotion,
> input pinning, digest resolution, shared akmods caching, image signing, and unit tests
> throughout.

## Install

> [!WARNING]
> **This repository is testing-only. Unlike `zfs-aurora-complex`, it is not used
> in production.** Anyone forking this repository or using its artifacts should
> treat them as test artifacts, use a disposable VM, disposable pool, or
> dedicated test hardware, and evaluate the risks for their own environment.
> The author exercises it in VMs and intends to keep the image building and
> testing as the pipeline evolves. CI does not boot the image or import a pool
> before `:latest` moves; promotion proves composition, signing, and `bootc
> container lint`, not production runtime safety. See
> [`docs/safety-model.md`](./docs/safety-model.md).

First, teach the host to trust this repository's signing key. This is a one-time
setup per host, and it has to happen **before** the switch: the image ships its
own copy of all three files below, but a copy that arrives inside the image
cannot verify the image that carried it.

```bash
# From a clone of this repository, so `cosign.pub` is the committed one.
sudo install -Dm0644 cosign.pub /etc/pki/containers/zfs-kinoite-complex.pub

sudo install -d -m 0755 /etc/containers/registries.d
sudo tee /etc/containers/registries.d/ghcr.io-danathar-zfs-kinoite-complex.yaml >/dev/null <<'YAML'
docker:
  ghcr.io/danathar/zfs-kinoite-complex:
    use-sigstore-attachments: true
YAML
```

Then add this entry to the `transports.docker` map in
`/etc/containers/policy.json`. Stock Fedora Kinoite ships that file, so edit the
existing map rather than replacing it:

```json
"ghcr.io/danathar/zfs-kinoite-complex": [
  {
    "type": "sigstoreSigned",
    "keyPath": "/etc/pki/containers/zfs-kinoite-complex.pub",
    "signedIdentity": { "type": "matchRepository" }
  }
]
```

Now switch:

```bash
sudo bootc switch --enforce-container-sigpolicy ghcr.io/danathar/zfs-kinoite-complex:latest
sudo systemctl reboot
```

`--enforce-container-sigpolicy` is required on the first switch, not optional --
it makes bootc evaluate the pull against the container signature policy, and it
records the deployment as policy-verified instead of as an unverified registry
image. That policy is the host's until the image is booted, which is why the
setup above comes first: without it, the stock policy's
`insecureAcceptAnything` default accepts this repository's image unchecked, and
enforcement would begin only on the next upgrade -- against whatever the first
switch happened to install. Afterwards, `sudo bootc upgrade` is the normal path
and uses the identical rule from inside the image.

Full steps, post-boot validation commands, and manual signature verification:
[`docs/install-and-verify.md`](./docs/install-and-verify.md).

## Documentation

Start here depending on what you want:

| I want to... | Read |
|---|---|
| run this image on a machine | [`docs/install-and-verify.md`](./docs/install-and-verify.md) |
| know what this promises, and what to do when a build is bad | [`docs/safety-model.md`](./docs/safety-model.md) |
| build or fork it myself | [`docs/building-locally.md`](./docs/building-locally.md) |
| understand the design | [`docs/architecture-overview.md`](./docs/architecture-overview.md) |
| find my way around the code | [`docs/code-reading-guide.md`](./docs/code-reading-guide.md) |
| understand image signing and bootc trust | [`docs/signing-and-bootc.md`](./docs/signing-and-bootc.md) |
| fix a broken build | [`docs/upstream-change-response.md`](./docs/upstream-change-response.md) |
| read the deep design history and validation notes | [`docs/zfs-kinoite-testing.md`](./docs/zfs-kinoite-testing.md) |
| change which akmods commit is built | [`docs/akmods-fork-maintenance.md`](./docs/akmods-fork-maintenance.md) |
| contribute a change | [`CONTRIBUTING.md`](./CONTRIBUTING.md) |
| review someone else's change | [`docs/review-rubric.md`](./docs/review-rubric.md) |
| know how much scrutiny a change needs | [`docs/risk-tiers.md`](./docs/risk-tiers.md) |
| let an AI agent work here safely | [`docs/SECURITY-AI.md`](./docs/SECURITY-AI.md) |
| know what stops a direct push to `main` | [`docs/branch-protection.md`](./docs/branch-protection.md) |
| learn what past mistakes here should change | [`docs/reflections/`](./docs/reflections/) |
| know what green means here, and what it does not | [`docs/quality.md`](./docs/quality.md) |
| get a number out of this repo without over-reading it | [`docs/metrics.md`](./docs/metrics.md) |
| look up a term | [`docs/glossary.md`](./docs/glossary.md) |
| see the whole documentation map | [`docs/documentation-guide.md`](./docs/documentation-guide.md) |

The CDDL/GPLv2 position on redistributing a binary ZFS module is recorded in
[`docs/licensing.md`](./docs/licensing.md). It is not legal advice; read it
before redistributing this image or basing a downstream image on it.

## References

- `Danathar/aurora-zfs-simple`: https://github.com/Danathar/aurora-zfs-simple (simpler daily-driver approach)
- `ublue-os/brew`: https://github.com/ublue-os/brew
- OpenZFS releases: https://github.com/openzfs/zfs/releases

## About this project

> [!NOTE]
> **This project was developed with significant AI assistance and should be treated
> cautiously.** Read its output the way you would read any unreviewed contribution.
>
> It is a third-party image. It is not an official Universal Blue image, is not sanctioned by
> the Universal Blue project, is not an official Fedora image, is not sanctioned by the Fedora
> Project, and is not affiliated with OpenZFS.
>
> It is provided as-is, with no promise that it is safe for your machines, your pools, or your
> data. [`docs/safety-model.md`](./docs/safety-model.md) states what this pipeline actually
> proves and what it does not. The maintainer is not responsible for data loss, unbootable
> systems, failed builds, or other consequences of using it.

> [!NOTE]
> **Maintenance on this repository is assisted by
> [Hivecommons Hive](https://github.com/hivecommons/hive) at ACMM level 4.**
>
> The repository is maintained by [@Danathar](https://github.com/Danathar). Hive orchestrates a
> fleet of AI agents that continuously review this codebase and report what they find as issues
> here.
>
> At **L4 (Security-Aware)** all agents may file issues, and the quality, sec-check and CI
> agents may additionally open pull requests that carry a `hold` label. The rest stay advisory:
> they report, they do not act. Every change is still reviewed and merged by a human
> maintainer.
>
> Labels in this repository carry approval authority, and automation here must never apply one.
> [`docs/SECURITY-AI.md`](./docs/SECURITY-AI.md) records which labels those are and why.
>
> Learn more: [Hive](https://github.com/hivecommons/hive) · [Hive Hub](https://hive.kubestellar.io) · [full ACMM policy matrix](https://github.com/hivecommons/hive/blob/v4/src/docs/acmm-policy-matrix.md)

## License

This repository is distributed under the GNU General Public License v3.0.
See [`LICENSE`](LICENSE) for the applicable terms.

Material previously distributed under the Apache License 2.0 retains its
applicable licensing and attribution. See
[`LICENSE.APACHE-2.0`](LICENSE.APACHE-2.0).

Third-party software, packages, dependencies, and other incorporated components
remain subject to their respective licenses.

This section is provided for reference only and does not modify or replace the
terms of any applicable license.
