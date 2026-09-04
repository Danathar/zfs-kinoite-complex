# Review rubric

What to check on a pull request here, in the order things actually go wrong.

This is not a style checklist. `ruff` and `.editorconfig` handle style, and CI
runs the tests. The questions below are the ones a machine cannot answer, and
they are ordered so the disqualifying ones come first — there is no point
weighing a design trade-off on a change that weakens a guard.

For a change touching the signing or promotion path, use
[`.github/prompts/review-safety-critical-change.prompt.md`](../.github/prompts/review-safety-critical-change.prompt.md),
which is this rubric's section 1 expanded into a procedure.

## How to read the verdicts

| Verdict | Meaning |
| --- | --- |
| **Blocking** | The pull request does not merge in this form. Not a trade-off to weigh against the benefit. |
| **Answer required** | The change may be right, but it cannot be judged until the author answers. |
| **Comment** | Worth saying; not worth holding the change for. |

An honest "I could not verify this" is a complete review outcome. It is better
than a confident inference, and AGENTS.md section 0 rule 3 exists because
inferences here have been wrong before.

## 1. Was a fail-closed check weakened? — *Blocking*

The first question, and the one most likely to be answered wrongly, because the
weakening usually looks like robustness.

Look for:

- A `raise` that became a warning, a `print`, or a `return`.
- A comparison that got looser: exact digest → prefix, `==` → `in`, an exact
  version → a range or a glob.
- A new fallback, default, or `or`-clause on a path that previously refused.
- `|| true`, `continue-on-error`, or `if: always()` added to a checking step.
- A test changed so a guard stops being exercised.
- A floor lowered in `.coverage-thresholds.json` with no reason in the commit
  message.
- A timeout or retry added around a check so it eventually passes.

If any of these is present, that **is** the review. The correct response is the
underlying cause (AGENTS.md section 0 rule 1). "The guard was too strict" is a
claim that needs evidence about the guard, not about the inconvenience.

## 2. Does the safety-critical statement exist and say something? — *Blocking if missing*

Required by `CONTRIBUTING.md` when the diff touches any of the seven files in
AGENTS.md section 0 rule 2. The statement has to answer *what could reach a
booted machine if this change is wrong* — not "this is low risk", not "tests
pass".

"Not applicable, and here is why" is a valid answer. An absent section is not.

## 3. Rollback and pool safety — *Blocking if unaddressed*

Applies if the change touches the ZFS version, the kernel it builds against, or
anything pool-facing.

An image that activates newer on-disk pool features can leave the previous image
unable to import those pools — which breaks the rollback path the whole safety
model rests on. The author has to say so explicitly. `renovate.json` already
refuses to automerge an OpenZFS minor-line bump for this reason; a hand-written
change deserves the same scrutiny.

## 4. Are the claims verified or inferred? — *Answer required*

Docs here have drifted from the code before, so a pull request body asserting how
something behaves is a claim to check, not evidence.

- If it cites a CI run, does it say **which** run? A green pipeline proves the
  build completed, not that the image is good.
- If it describes existing behavior, was the code read, or the doc describing
  the code?
- If it quotes a file path, line, or message, does it still match?

A body that distinguishes "verified by X" from "I could not verify Y" is doing
this correctly and should be credited, not treated as weakness.

## 5. Do the tests test anything? — *Blocking for the first item*

- **`assertRaises(CiToolError)` with no message assertion.** It passes for any
  reason at all, including an unrelated missing environment variable, so it
  reports a guard as covered when the guard never ran. Pin the message or the
  specific guard.
- Does a mocked error path use the *real* failure shape? A registry timeout, a
  rate limit, a malformed manifest. A test that patches a call to raise agrees
  with whatever it was told.
- If the change adds a module, does it have a floor in
  `.coverage-thresholds.json`? CI fails without one, so this should already be
  answered — but check the floor is what the suite reaches rather than zero.
- Does a new test have a side effect outside a temporary directory? A test that
  can reach the network, a registry, or an absolute path such as `/tmp/akmods`
  is not a test (see `.claude/memory/corrections.md`).

## 6. Scope — *Comment, unless it hides something*

`CONTRIBUTING.md`: every changed line traces to the change the pull request set
out to make; adjacent cleanups belong in their own pull request.

This is normally a comment. It becomes blocking when the unrelated change is in
a file from section 2's list, because a reformat in a signing path makes the
real diff unreadable.

## 7. Documentation that will drift — *Comment*

- Does a new document belong in `docs/documentation-guide.md`'s tree and the
  `README.md` router?
- Does the change make an existing sentence false? Editing a workflow often
  invalidates prose describing it, and this repository has landed that mistake
  before.
- Is the new prose a *second copy* of something? `AGENTS.md` is the single
  source; `.github/copilot-instructions.md`, `.cursor/rules/`, and
  `.claude/commands/` are pointers at it on purpose.

## 8. Automated review findings — *Answer required*

`chatgpt-codex-connector[bot]` reviews pull requests here and its findings carry
a severity badge. They are *often* right, not automatically right.

Each finding needs one of: a fix, or a stated reason it does not apply. A
finding that is silently ignored is the one worth reading twice — the P1 on
[#61](https://github.com/Danathar/zfs-kinoite-complex/pull/61) was correct and
identified a test that could have published to a registry.

## A minimal review, for a small change

Sections 1, 2 and 5 are never skippable. If a change touches no safety-critical
file, adds no test, and is documentation only, sections 4 and 7 are the review.
Say which sections you applied so the next reader knows what was and was not
looked at.
