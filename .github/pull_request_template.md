## What changed

<!-- The behavior change and why it is needed. -->

## Safety-critical statement

<!--
Required by CONTRIBUTING.md if this touches any file named in AGENTS.md
section 0 rule 2: build.yml, .github/actions/publish-native-image,
ci_tools/sign_image.py, ci_tools/promote_stable.py,
ci_tools/check_akmods_cache.py,
containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py, or
files/scripts/configure_signing_policy.py.

Say what could reach a booted machine if this change is wrong. That statement
is the review, not a formality. Write "Not applicable" if none are touched.
-->

## Rollback impact

<!--
Required if this changes the ZFS line, the kernel it builds against, or
anything pool-facing: an image that activates newer pool features can leave
the previous image unable to import those pools. Write "Not applicable"
otherwise.
-->

## Checks

- [ ] `python3 -m pytest tests/ -v` passes
- [ ] `ruff check ci_tools/ shared/ tests/ files/ containerfiles/` is clean
- [ ] No fail-closed check was relaxed, given a fallback, or made best-effort
      (AGENTS.md section 0 rule 1)
- [ ] Every changed line traces to the change this PR set out to make; adjacent
      cleanups are not included

## Verification

<!--
What you actually ran or read, not what you expect to be true. If you are
reporting on a CI run, say which run. A green pipeline proves the build
completed, not that the image is good (AGENTS.md section 0 rule 4).

State anything you could not verify rather than presenting an inference as a
fact.
-->
