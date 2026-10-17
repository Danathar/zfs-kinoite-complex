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

**Why it matters.** `build.yml` itself is the path to the key, not a
hypothetical future workflow:

- It carries a `workflow_dispatch` trigger, and `gh workflow run build.yml --ref
  <branch>` runs *that branch's* copy of the file.
- `sign-akmods-cache` and `build-candidate-image` both declare `environment:
  production-signing` and consume `secrets.SIGNING_SECRET`.
- Only `build-zfs-akmods` guards on `github.ref`: it runs only on
  `refs/heads/main`. It is privileged and holds `packages: write`, and without
  the guard a branch dispatch would run that branch's `ci_tools/` and
  `.github/actions/` code there and could republish the shared `main-<fedora>`
  akmods tag.
  `sign-akmods-cache`, `build-candidate-image` and `promote-stable` all need it,
  so on another ref nothing after it runs either.
- `promote-stable` is conditioned on `github.event.inputs.promote_to_stable ==
  'true'`, and that input **defaults to `true`**.

The two guards are independent. The job guard lives in `build.yml`, so a branch
whose copy drops it can run the akmods job again; editing workflow files needs
the Workflows permission. The environment restriction lives in GitHub settings,
so no branch can drop it, and it still keeps such a run away from the signing
key.

The environment setting cannot be checked from CI without an admin-scoped token,
which is why it belongs here rather than in a test. Re-check it after any change
to the environment.

### Open: hosts accept any signed image from this repository under any tag

The signature policy the image installs uses `signedIdentity: matchRepository`
([`configure_signing_policy.py`](../files/scripts/configure_signing_policy.py)),
and the signatures `sign-image` writes name the repository without a tag. Check
one:

```bash
cosign verify --key cosign.pub ghcr.io/danathar/zfs-kinoite-complex:latest \
  | jq -r '.[0].critical.identity["docker-reference"]'
# ghcr.io/danathar/zfs-kinoite-complex
```

So a host accepts every image this repository ever signed, whatever tag it is
pulled through. Anything that can write tags in the package could point
`:latest` at an older signed `candidate-*` or `stable-*` image, and `bootc
upgrade` would likely accept it as a rollback. [unverified: not exercised.]
Today that means `build.yml`'s jobs on `main`, `prune-registry.yml`'s job, and
anyone holding a token with package write access.

No fix is chosen. Narrowing who can write the package reduces the exposure; a
signature bound to a tag, or a host-side check that refuses older images, would
close it. Neither has been evaluated.

## Runtime validation

CI does not import a real pool or boot the image. Before relying on a new
release, validate it on representative hardware or a disposable VM, including
module loading, ZFS userspace/kernel version agreement, pool import, and
rollback.
