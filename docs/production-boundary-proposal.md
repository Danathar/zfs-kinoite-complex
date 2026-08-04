# Production Boundary Configuration

The repository contains the workflow code for candidate-first test builds,
signature verification, and test-tag promotion. This repository is testing-only
and is not the production image stream; GitHub repository settings are still
part of its test trust boundary and must be configured separately.

## Required settings

Before promoting a signed test image, configure:

1. a `production-signing` Environment
2. `SIGNING_SECRET` as an environment secret, never as an unscoped repository
   secret
3. environment rules restricting the signing jobs to `main`
4. package write permission for the workflows that publish images
5. package visibility and deletion policy appropriate for the image stream
6. branch settings that protect reviewed changes to `main`

The exact GitHub UI and policy controls can change. Re-check them live after
creating the repository; this document does not claim that they are already
configured.

## Why the signing secret is environment-scoped

Branch and pull-request workflows build untrusted or experimental content.
They must not be able to use the production private key. The production jobs
run on `main`, require the secret, sign the exact digest they built or
consumed, and verify it with the committed `cosign.pub` key before promotion.

The privileged akmods job is separately constrained: its container image is a
reviewed digest-pinned literal, it has no free-text container override, and
its outputs are checked before any trusted job consumes them.

## Review checklist

After repository creation, verify:

- the default branch is `main`
- Actions can read contents and write only the required GHCR packages
- `production-signing` is unavailable to branch and pull-request runs
- `SIGNING_SECRET` is present only in that environment
- the committed public key matches the private key used for signing
- `latest` is moved only by the promotion job
- branch builds cannot sign or promote production tags

These controls do not make the repository production-approved. Keep the image
as a validation stream and do not represent it as a trusted production update
source.
