---
description: Reproduce a specific past build with pinned inputs, to diagnose it
mode: agent
---

# Replay a build

**Goal:** rebuild with the exact inputs a past run used, so a difference in
behavior is attributable to something other than the inputs.

Read this before starting: replay mode is reachable **only** from a manual
`workflow_dispatch`. `CONTRIBUTING.md` names it as the worked example of a path
covered by neither tier — it is an incident-reproduction tool, so a broken guard
in it surfaces exactly when someone needs it. Treat its refusals as
informative, not as obstacles.

## 1. Get the inputs of the run you are reproducing

Every run uploads a `build-inputs` artifact written by
`ci_tools/write_build_inputs_manifest.py`.

```bash
gh run download <run-id> --name "build-inputs-<run-id>"
cat build-inputs.json
```

The artifact name carries the run id (`build-inputs-${{ github.run_id }}` in
`.github/actions/prepare-main-akmods/action.yml`), so a bare `build-inputs`
will not resolve. `gh run view <run-id> --json ...` or the run page lists the
exact name if you are unsure.

The `inputs` object holds everything the lock file needs. If the artifact is
missing, the run predates it or failed before that step — say so rather than
reconstructing the values by hand from logs.

## 2. Fill in the lock file

`ci/inputs.lock.json` ships with `REPLACE_ME` placeholders. Copy from
`build-inputs.json`:

| Lock field | From `build-inputs.json` |
| --- | --- |
| `base_image` | `inputs.base_image_pinned` — the digest form, not the tag |
| `build_container` | `inputs.build_container_pinned` |
| `zfs_minor_version` | `inputs.zfs_minor_version` |
| `zfs_version` | `inputs.zfs_version` |

**`zfs_version` is the one people leave empty and should not.** Left empty, the
resolver re-resolves the newest release on `zfs_minor_version`, so once a newer
patch exists the replay does not reproduce the original ZFS version — and the
whole point was to hold the inputs still.

`akmods_upstream_ref` is deliberately not in the lock file. It comes from
`ci/defaults.json` so there is one source of truth.

## 3. Expect the build-container guard to fire

If `DEFAULT_BUILD_CONTAINER_IMAGE` in `ci/defaults.json` has moved since the run
you are replaying, `resolve_build_inputs` refuses with a message naming both
refs. That guard is correct: the container it would actually run in is not the
one the lock file names, so the replay would not be a replay.

The message tells you the resolution — set `DEFAULT_BUILD_CONTAINER_IMAGE` to
the locked ref *and* the matching literal in each workflow's `container:` block,
through a reviewed pull request. That container runs `--privileged` as root with
`/` mounted, which is why it cannot be overridden from a dispatch input.

Do not work around this by editing the lock file to match the current container.
That produces a green run that proves nothing.

## 4. Dispatch

```bash
gh workflow run build.yml \
  -f use_input_lock=true \
  -f lock_file=ci/inputs.lock.json \
  -f promote_to_stable=false
```

**`promote_to_stable=false` is not optional for a diagnostic replay.** The
default is `true`. A replay that promotes moves `:latest` to an image built for
diagnosis, which `bootc upgrade` then pulls onto whatever is tracking it.

## 5. Report

Say which run you replayed, which inputs the lock held, whether the replay
reproduced the behavior, and — explicitly — whether you promoted. If the replay
did **not** reproduce it, that is a finding: the cause was not in the inputs.
