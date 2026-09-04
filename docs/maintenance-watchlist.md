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

**A branch can already reach the signing key today.** `build.yml` itself is the
path, not a hypothetical future workflow:

- It carries a `workflow_dispatch` trigger, and `gh workflow run build.yml --ref
  <branch>` runs *that branch's* copy of the file.
- `sign-akmods-cache` and `build-candidate-image` both declare `environment:
  production-signing` and consume `secrets.SIGNING_SECRET`.
- No job in `build.yml` guards on `github.ref`. The only ref-shaped expression in
  the file is the `concurrency` group key, which gates nothing.
- `promote-stable` is conditioned on `github.event.inputs.promote_to_stable ==
  'true'`, and that input **defaults to `true`**.

So a dispatch against an arbitrary branch signs a candidate with the production
key and copies it to `:latest` by default. `deployment_branch_policy: null` is
the reason nothing stops it.

What bounds this is repository write access rather than the environment:
dispatching a workflow needs a token with `actions: write`, so it is not a route
open to fork pull requests. But the environment restriction is precisely the
control meant to make the signing key harder to reach than ordinary write
access, and it is absent. `build-branch.yml` is separately clean — it never
references `SIGNING_SECRET`, which `tests/test_workflow_build_container.py` pins
— but auditing only that workflow would hide the live path through `build.yml`.

This cannot be checked from CI without an admin-scoped token, which is why it
belongs here rather than in a test. Three ways to close it: configure the
environment's branch policy, add a `github.ref` guard to the signing and
promotion jobs in `build.yml` so the workflow enforces it itself, or correct the
two documents that assert the boundary is already in place. Leaving it as it
stands is the one option that should not, because a documented boundary nobody
enforces is worse than an acknowledged gap.

## Runtime validation

CI does not import a real pool or boot the image. Before relying on a new
release, validate it on representative hardware or a disposable VM, including
module loading, ZFS userspace/kernel version agreement, pool import, and
rollback.
