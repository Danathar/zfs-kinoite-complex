# Risk tiers

How much scrutiny a change needs here, and why the answer is not "how many
lines did it touch".

The usual proxies do not work in this repository. A one-line diff in
`promote_stable.py` can move `:latest` to something unsigned; a thousand-line
documentation rewrite cannot reach a booted machine at all. So tiers are
assigned by **what the change can cause**, not by size, and the `area/*` labels
that [`.github/labeler.yml`](../.github/labeler.yml) attaches are a first
approximation of that.

[`review-rubric.md`](./review-rubric.md) is what a reviewer does. This page is
how much of it to do.

## The tiers

### Tier 3 — can reach a booted machine

**Anything touching the seven files in `AGENTS.md` section 0 rule 2**, plus the
ZFS line and anything pool-facing. Labelled `area/safety-critical`.

- `.github/workflows/build.yml`
- `.github/actions/publish-native-image/`
- `ci_tools/sign_image.py`
- `ci_tools/promote_stable.py`
- `ci_tools/check_akmods_cache.py`
- `containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py`
- `files/scripts/configure_signing_policy.py`
- `cosign.pub`, `ci/defaults.json`'s ZFS and base-image values

**What it requires:** the full rubric. The safety-critical statement is
mandatory and is the review, not a formality (`CONTRIBUTING.md` item 3). If the
ZFS line or anything pool-facing moves, rollback must be addressed explicitly —
an image that activates newer pool features can leave the previous image unable
to import those pools, which breaks the recovery path everything else assumes.

**Never in this tier, regardless of who asks:** weakening a fail-closed check.
That is not a tier, it is a refusal (`AGENTS.md` section 0 rule 1).

**An agent may not complete a Tier 3 change unattended.** It may propose one,
with the analysis, on a branch.

### Tier 2 — can break the build or a published artifact indirectly

The rest of the image and the decision logic: `Containerfile`,
`build_files/`, the other `ci_tools/` and `shared/` modules, `ci/defaults.json`,
`renovate.json`, and workflows other than `build.yml`. Labelled `area/build`,
`area/ci`, `area/ci-tools`.

**What it requires:** rubric sections 1, 4, 5 and 6. A green unit suite is not
sufficient evidence — `Containerfile` and `build_files/` execute only inside an
image build, so say how the change was verified or say plainly that it was not.

Watch specifically for a widened `permissions:` block, a new secret reference,
or a new external action. Those are Tier 3 questions arriving inside a Tier 2
diff.

### Tier 1 — can only break CI or mislead a reader

`tests/`, `.coverage-thresholds.json`, docs, and agent instructions. Labelled
`area/tests`, `area/docs`, `area/agent-config`.

**What it requires:** rubric sections 5 and 7. Two specific things still bite at
this tier:

- **A test that does not test.** `assertRaises(CiToolError)` alone passes for
  any reason at all, including an unrelated missing environment variable.
- **Doc drift.** This repository has shipped prose that contradicted the code.
  A doc change that makes an existing sentence false is a defect, not a nit.

A lowered coverage floor is Tier 1 by path and Tier 2 by meaning — it is a claim
that a code path went away. Say which.

### Tier 0 — cannot affect anything that runs

Nothing here reaches CI or the image: `README.md` badges, `LICENSE`,
`.editorconfig`, `.gitignore`.

Worth knowing because `build.yml`, `build-pr.yml` and `build-branch.yml` all set
`paths-ignore` for `**/*.md` and `docs/**`, so a documentation-only change
**starts no build at all**. That is a useful property when merging a batch — see
[`quality.md`](./quality.md) on why back-to-back merges turn the build badge
red.

## Assigning a tier when the labels disagree

The labels are path-based and a change can carry several. **Take the highest
tier any label implies**, and note that `area/*` labels are descriptive only:
they never mean approval, which is the whole reason they live in their own
namespace ([`SECURITY-AI.md`](./SECURITY-AI.md), "Labels carry authority").

Two cases the paths get wrong on their own:

| Looks like | Actually |
| --- | --- |
| A test-only diff that deletes a test exercising a fail-closed guard | Tier 2. The guard stops being covered, and the floor in `.coverage-thresholds.json` is the only thing that notices. |
| A docs-only diff that edits `AGENTS.md` or `.claude/settings.json` | Tier 2. It changes what future agent sessions are permitted to do. |

When two readings are defensible, take the higher one and say why in the pull
request. The cost of over-reviewing a change here is minutes; the cost of
under-reviewing one is an image on somebody's machine.
