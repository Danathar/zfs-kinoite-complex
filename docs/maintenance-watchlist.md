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
3. run the tests and check the pull request build
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

### Closed: the `production-signing` environment is restricted to `main`

[`production-boundary-proposal.md`](./production-boundary-proposal.md) lists
"environment rules restricting the signing jobs to `main`" as a **required
setting**, and its verification checklist says `production-signing` must be
"unavailable to branch and pull-request runs".
[`zfs-kinoite-testing.md`](./zfs-kinoite-testing.md) used to go further and
state as fact that signing happens "inside the `production-signing` environment
that only `main` refs can reach". It no longer does: its signing section now
links here instead.

Until 2026-10-17 the restriction was missing: `deployment_branch_policy` was
`null`, so any ref could deploy to the environment. Since 2026-10-17 only the
`main` branch may, and administrators cannot bypass it. Check it live:

```bash
gh api repos/Danathar/zfs-kinoite-complex/environments/production-signing \
  --jq '"admins_bypass=\(.can_admins_bypass) branch_policy=\(.deployment_branch_policy)"'
# admins_bypass=false branch_policy={"custom_branch_policies":true,"protected_branches":false}
gh api repos/Danathar/zfs-kinoite-complex/environments/production-signing/deployment-branch-policies \
  --jq '[.branch_policies[].name]'
# ["main"]
```

**This setting is the only thing keeping a branch away from the signing key.**
`build.yml` itself would be the path, not a hypothetical future workflow:

- It carries a `workflow_dispatch` trigger, and `gh workflow run build.yml --ref
  <branch>` runs *that branch's* copy of the file.
- `sign-akmods-cache` and `build-candidate-image` both declare `environment:
  production-signing` and consume `secrets.SIGNING_SECRET`.
- No job in `build.yml` guards on `github.ref`. The only ref-shaped expression in
  the file is the `concurrency` group key, which gates nothing.
- `promote-stable` is conditioned on `github.event.inputs.promote_to_stable ==
  'true'`, and that input **defaults to `true`**.

Without the restriction, a dispatch against an arbitrary branch signs a
candidate with the production key and copies it to `:latest` by default.

**Still open: the restriction does not cover `build-zfs-akmods`.** That job
declares no environment, holds `packages: write`, runs privileged, and has no
`github.ref` guard. A `workflow_dispatch` against a branch, for example with
`rebuild_akmods=true`, runs that branch's `ci_tools/` and `.github/actions/`
code there and can republish the shared `main-<fedora>` akmods tag before its
signing job is refused. The republished cache is unsigned, so the next `main`
run rejects it and has to rebuild: a denial of service and a cache-poisoning
attempt, not a signed image. Dispatching needs a token with `actions: write`.
A job-level `if: github.ref == 'refs/heads/main'` on `build-zfs-akmods` would
close it; that is not done.

This cannot be checked from CI without an admin-scoped token, which is why it
belongs here rather than in a test. Re-check it after any change to the
environment. A `github.ref` guard on the signing and promotion jobs in
`build.yml` would enforce the same boundary in the workflow itself; that is not
done.

## Runtime validation

CI does not import a real pool or boot the image. Before relying on a new
release, validate it on representative hardware or a disposable VM, including
module loading, ZFS userspace/kernel version agreement, pool import, and
rollback.
