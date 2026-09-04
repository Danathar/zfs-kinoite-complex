---
description: Review a diff that touches the build, promotion, or signing path
---

Follow [`.github/prompts/review-safety-critical-change.prompt.md`](../../.github/prompts/review-safety-critical-change.prompt.md).

That file is the procedure; do not restate it here. The first question is
disqualifying rather than a trade-off: **was any fail-closed check weakened?**
A `raise` that became a warning, a comparison that got looser, a new fallback,
`|| true`, `continue-on-error`, or a coverage floor lowered in
`.coverage-thresholds.json` without a reason in the commit message.

Answer the one question CONTRIBUTING requires: what could reach a booted
machine if this change is wrong. End with what you verified and what you could
not — an honest "I could not verify X" is a better review than a confident
inference.

Diff, PR number, or files, if given: $ARGUMENTS
