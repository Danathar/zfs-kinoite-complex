"""
Module: shared/command_args.py
What: Shared redaction of credential values in command argv.
Doing: Replaces the value of each known credential flag with a placeholder so
an external command can be named in an error message.
Why: CI helpers and the image-build ZFS helper both report failed commands.
One flag list keeps a credential flag added for one of them from reaching the
other's error text unredacted.
Goal: Keep failure messages readable without printing registry credentials.
"""

from __future__ import annotations

from collections.abc import Sequence

# Flags whose *value* is a registry credential. No helper builds one any more
# -- `ci_tools.common.registry_auth_dir` hands the credential to `skopeo` and
# `cosign` through a `0600` file instead, so it never enters argv at all. The
# set stays because redaction is the last line of defence, not the first: it
# still covers any command a future caller assembles by hand.
SECRET_ARG_FLAGS = {
    "--creds",
    "--src-creds",
    "--dest-creds",
    "--registry-username",
    "--registry-password",
}


def redact_command_args(args: Sequence[str]) -> list[str]:
    """Return command args with known secret values replaced for error messages."""
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append("***REDACTED***")
            redact_next = False
            continue
        flag, separator, _value = arg.partition("=")
        if flag in SECRET_ARG_FLAGS:
            if separator:
                redacted.append(f"{flag}=***REDACTED***")
            else:
                redacted.append(arg)
                redact_next = True
            continue
        redacted.append(arg)
    return redacted
