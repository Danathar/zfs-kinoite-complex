# Runtime Validation Proposal

## Current boundary

The GitHub Actions pipeline validates composition, signatures, and package and
module invariants. It does not boot a Fedora Kinoite deployment or import a
real ZFS pool before moving `latest`. That boundary is intentional: a hosted
runner is not a safe place to expose production pool devices or signing
credentials to an experimental boot test.

## Recommended validation sequence

Use a disposable VM or representative test hardware after a candidate is
published and signed:

1. verify the image digest and signature
2. install or switch the candidate with enforced signature policy
3. reboot and confirm the expected deployment is active
4. check the running kernel and `zfs` userspace/module versions
5. load the module and create/import a disposable loopback-backed pool
6. exercise rollback and confirm the previous deployment remains usable

Do not point automated tests at production pool devices. A self-hosted runner
would require separate hardware, device isolation, credential boundaries, and
an explicit decision about whether runtime validation is allowed to gate
promotion.

## Future automation requirements

Any future implementation should document the device model, cleanup guarantees,
signature boundary, rollback behavior, and failure handling before adding a
workflow gate. Until then, the operator remains the final runtime validation
step.
