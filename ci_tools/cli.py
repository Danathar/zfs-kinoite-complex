"""
Script: ci_tools/cli.py
What: Command entrypoint used by workflow steps.
Doing: Maps command text to module `main()` functions and runs the selected command, passing
any options after the command name to the commands in `TAKES_ARGUMENTS`.
Why: Workflows can call one simple command pattern.
Goal: Keep step-to-code mapping clear and easy to maintain.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence

from ci_tools.common import CiToolError

# Commands whose `main()` takes the options written after the command name, such as
# `check-ruleset-drift --fleet`. Every other command takes none, and the parser refuses them.
TAKES_ARGUMENTS = frozenset({"check-ruleset-drift"})


def command_map() -> dict[str, Callable[..., None]]:
    """
    Map CLI command names to Python entry functions.

    Each value is a `main()` function from one workflow helper module.
    """

    from ci_tools.akmods_build_and_publish import main as akmods_build_and_publish
    from ci_tools.akmods_clone_pinned import main as akmods_clone_pinned
    from ci_tools.akmods_configure_zfs_target import main as akmods_configure_zfs_target
    from ci_tools.boot_test import main as boot_test
    from ci_tools.check_akmods_cache import main as check_akmods_cache
    from ci_tools.check_ruleset_drift import main as check_ruleset_drift
    from ci_tools.check_stable_signal import main as check_stable_signal
    from ci_tools.classify_akmods_failure import main as classify_akmods_failure
    from ci_tools.export_repo_defaults import main as export_repo_defaults
    from ci_tools.pin_akmods_cache import main as pin_akmods_cache
    from ci_tools.prepare_validation_build import main as prepare_validation_build
    from ci_tools.promote_stable import main as promote_stable
    from ci_tools.prune_registry import main as prune_registry
    from ci_tools.resolve_build_inputs import main as resolve_build_inputs
    from ci_tools.tagging_context import (
        main_compute_candidate_tag,
        main_export_registry_context,
    )
    from ci_tools.write_akmods_badge import main as write_akmods_badge
    from ci_tools.write_build_inputs_manifest import main as write_build_inputs_manifest
    from ci_tools.write_last_good_build_badge import main as write_last_good_build_badge

    return {
        "resolve-build-inputs": resolve_build_inputs,
        "write-build-inputs-manifest": write_build_inputs_manifest,
        "check-akmods-cache": check_akmods_cache,
        "check-stable-signal": check_stable_signal,
        "compute-candidate-tag": main_compute_candidate_tag,
        "export-repo-defaults": export_repo_defaults,
        "export-registry-context": main_export_registry_context,
        "prepare-validation-build": prepare_validation_build,
        "pin-akmods-cache": pin_akmods_cache,
        "promote-stable": promote_stable,
        "prune-registry": prune_registry,
        "akmods-clone-pinned": akmods_clone_pinned,
        "akmods-configure-zfs-target": akmods_configure_zfs_target,
        "akmods-build-and-publish": akmods_build_and_publish,
        "classify-akmods-failure": classify_akmods_failure,
        "write-akmods-badge": write_akmods_badge,
        "write-last-good-build-badge": write_last_good_build_badge,
        "boot-test": boot_test,
        "check-ruleset-drift": check_ruleset_drift,
    }


def build_parser(commands: Mapping[str, Callable[..., None]]) -> argparse.ArgumentParser:
    """Build argument parser with one positional command choice."""

    parser = argparse.ArgumentParser(
        prog="python3 -m ci_tools.cli",
        description="Run one workflow helper command.",
    )
    parser.add_argument("command", choices=sorted(commands.keys()))
    return parser


def run_command(
    command: str, commands: Mapping[str, Callable[..., None]], arguments: Sequence[str] = ()
) -> None:
    """
    Run one registered command, handing it `arguments` when there are any.

    `commands` is passed in to keep this function easy to test.
    """

    if arguments:
        commands[command](list(arguments))
    else:
        commands[command]()


def main(argv: list[str] | None = None) -> None:
    commands = command_map()
    parser = build_parser(commands)
    args, arguments = parser.parse_known_args(argv)
    if arguments and args.command not in TAKES_ARGUMENTS:
        parser.error(f"{args.command} takes no options: {' '.join(arguments)}")

    try:
        run_command(args.command, commands, arguments)
    except CiToolError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
