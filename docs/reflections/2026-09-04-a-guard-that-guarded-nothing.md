# A guard that guarded nothing

*2026-09-04*

## What happened

Two assertions were added to `tests/test_workflow_build_container.py` to hold
security properties of the new agent workflow: that its job holds no
`packages:` permission, and that it never references the signing secret. Both
were written the obvious way:

```python
self.assertNotIn("packages:", self._agent_workflow())
```

It failed on a clean tree.

The reason is that `.github/workflows/ai-fix.yml` explains **in its own header
comment** why `packages: write` is excluded — so the literal string is in the
file, and always will be, precisely because someone documented the decision
well. The assertion was reading the explanation and concluding the permission
was present.

The failure was the lucky outcome. Reverse the wording — a header that said
"this job holds no package permissions" without the literal token — and the
assertion passes while the job quietly holds `packages: write`. A green test
asserting the opposite of the truth.

A second version parsed the YAML instead, which was correct but introduced a
different version of the same problem: `PyYAML` is not in the CI install list.
It happened to work because the `ubuntu-24.04` runner image ships PyYAML, so
the guard held **by accident**, and would have started skipping silently — and
staying green — the day that image changed.

## What changed

The assertions now match against the file with full-line comments stripped, so
prose about a setting cannot satisfy a check about the setting. No parser, so
no undeclared dependency and no skip path: all seventeen assertions in that
module run under `python3 -m unittest discover -s tests` with nothing installed.

Every one of them was then mutation-tested — the guard is only worth having if
removing the thing it guards makes it fail.

## What to carry forward

**A check over source text is a check over the comments too.** This repository
writes unusually explanatory comments on purpose, so it is unusually exposed to
this. Any assertion of the form "this string does not appear" needs to say what
it does about the string appearing in prose.

**Write the mutation before trusting the assertion.** Both defects here were
found by breaking the thing on purpose and watching whether the test noticed —
not by reading the test, which looked right both times.

**An optional import in a test is a skip nobody sees.** `pytest` reports skips
in a summary line most people scroll past, and CI stays green. If an assertion
matters, it must not be able to opt out of running. If it does not matter enough
to run everywhere, it does not matter enough to write.

This is the same failure `CONTRIBUTING.md` already warns about under a different
name: *"A test that asserts only that something raises. `assertRaises(CiToolError)`
passes for any reason at all."* Both are checks that pass without exercising the
thing they claim to cover.
