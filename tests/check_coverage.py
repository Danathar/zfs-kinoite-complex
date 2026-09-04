"""
Script: tests/check_coverage.py
What: Enforces the per-module coverage floors recorded in .coverage-thresholds.json.
Doing: Reads the coverage.json produced by the test run and compares each module's covered-statement count against its floor.
Why: A repository-wide percentage is the wrong gate here -- CONTRIBUTING says the number moving is not a finding, and one well-tested module can hide another that stopped being exercised at all.
Goal: Make a test that stops reaching a module fail on the pull request that stops it, without ever blocking a change on a percentage.

Deliberately not a --cov-fail-under gate. Three reasons, in order:

1. A percentage moves when code is added, so it punishes growth. A covered-line
   count only falls when a test stops reaching lines that used to run.
2. A single repository-wide number lets one module's coverage mask another's.
   The floors are per module for exactly the reason arch-bootc's are: a
   well-tested helper cannot hide a regression that stops a shipped module from
   being exercised.
3. CONTRIBUTING is explicit that the percentage itself is not a finding. This
   does not change that. Nothing here reads or asserts a percentage; the
   percentages printed alongside each result are context for a reader, not the
   thing being gated.

Floors are raise-only by convention, and raising is never automatic. Raising a
floor is evidenced by an absence being closed -- the suite demonstrably reached
those lines. Lowering one is evidenced by an absence, and a deleted test and a
deliberately removed code path look identical from the count alone. So lowering
a floor stays a decision a person makes in a diff, with a reason in the commit.

Run by .github/workflows/test.yml. Also runnable by hand:

    python3 -m pytest tests/ --cov=ci_tools --cov=shared \\
      --cov=containerfiles/zfs-akmods --cov=files/scripts \\
      --cov-branch --cov-report=json
    python3 tests/check_coverage.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
THRESHOLDS_PATH = REPO_ROOT / ".coverage-thresholds.json"
DEFAULT_COVERAGE_REPORT = REPO_ROOT / "coverage.json"


class CoverageGateError(RuntimeError):
    """Raised when the gate cannot run at all, as distinct from a floor failing."""


def load_thresholds(path: Path) -> dict[str, int]:
    """Return the recorded floors, rejecting a file that cannot mean what it says."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoverageGateError(f"No thresholds file at {path}") from exc
    except json.JSONDecodeError as exc:
        raise CoverageGateError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict) or not raw:
        raise CoverageGateError(f"{path} must be a non-empty object of path -> minimum")

    floors: dict[str, int] = {}
    for module_path, floor in raw.items():
        # A float or a string here would compare in surprising ways rather than
        # failing, and "80" >= 80 is a TypeError only sometimes.
        if not isinstance(floor, int) or isinstance(floor, bool) or floor < 0:
            raise CoverageGateError(
                f"{path}: floor for {module_path} must be a non-negative integer, got {floor!r}"
            )
        floors[module_path] = floor
    return floors


def load_covered_counts(path: Path) -> dict[str, tuple[int, int]]:
    """
    Return `{module path: (covered statements, total statements)}` from coverage.json.

    Statement counts, not branch counts: statements are what a missing test
    stops reaching, and mixing the two into one number would make a failure
    ambiguous to read.
    """

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoverageGateError(
            f"No coverage report at {path}. Run pytest with --cov-report=json first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise CoverageGateError(f"{path} is not valid JSON: {exc}") from exc

    files = report.get("files")
    if not isinstance(files, dict) or not files:
        raise CoverageGateError(f"{path} reports no files; the coverage run measured nothing")

    counts: dict[str, tuple[int, int]] = {}
    for module_path, entry in files.items():
        summary = entry["summary"]
        counts[module_path] = (summary["covered_lines"], summary["num_statements"])
    return counts


def evaluate(
    floors: dict[str, int],
    counts: dict[str, tuple[int, int]],
) -> tuple[list[str], list[str], list[str]]:
    """
    Compare floors against measured counts.

    Returns (failures, passes, raisable). Both directions are checked so the
    manifest cannot rot: a module measured with no floor is a failure (new code
    with no decision recorded), and a floor naming a module the run did not
    measure is a failure (stale after a rename or deletion).
    """

    failures: list[str] = []
    passes: list[str] = []
    raisable: list[str] = []

    for module_path, floor in sorted(floors.items()):
        if module_path not in counts:
            failures.append(
                f"{module_path}: has a floor of {floor} but the coverage run did not measure it "
                f"-- remove the stale entry, or restore the module to a --cov path"
            )
            continue

        covered, total = counts[module_path]
        percent = round(covered * 100 / total) if total else 100
        if covered < floor:
            failures.append(
                f"{module_path}: {covered}/{total} statements ({percent}%), floor is {floor} "
                f"-- {floor - covered} fewer statements are reached than before"
            )
            continue

        passes.append(f"{module_path}: {covered}/{total} statements ({percent}%), floor {floor}")
        if covered > floor:
            raisable.append(f"{module_path}: floor {floor}, reached {covered}")

    for module_path in sorted(set(counts) - set(floors)):
        covered, total = counts[module_path]
        failures.append(
            f"{module_path}: measured ({covered}/{total} statements) but has no floor "
            f"-- add an entry to .coverage-thresholds.json recording what it reaches today"
        )

    return failures, passes, raisable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 tests/check_coverage.py",
        description="Enforce the per-module coverage floors in .coverage-thresholds.json.",
    )
    parser.add_argument(
        "--coverage-report",
        type=Path,
        default=DEFAULT_COVERAGE_REPORT,
        help="Path to the coverage.json written by --cov-report=json.",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=THRESHOLDS_PATH,
        help="Path to the recorded floors.",
    )
    args = parser.parse_args(argv)

    try:
        floors = load_thresholds(args.thresholds)
        counts = load_covered_counts(args.coverage_report)
    except CoverageGateError as exc:
        print(f"coverage: {exc}", file=sys.stderr)
        return 2

    failures, passes, raisable = evaluate(floors, counts)

    for line in passes:
        print(f"coverage: PASS {line}")
    for line in raisable:
        # Reported, never applied. See the module docstring on why raising is a
        # decision someone makes in a diff.
        print(f"coverage: could raise {line}")

    if failures:
        for line in failures:
            print(f"coverage: FAIL {line}", file=sys.stderr)
        print(f"coverage: {len(failures)} floor(s) failed", file=sys.stderr)
        return 1

    print(f"coverage: all {len(passes)} per-module floors hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
