"""
Script: tests/test_check_coverage.py
What: Tests for the per-module coverage floor gate.
Doing: Drives load_thresholds, load_covered_counts, evaluate, and main over hand-built reports and threshold files.
Why: A gate that silently passes is worse than no gate, because it reads as evidence.
Goal: Pin each way the gate is meant to fail, and the message it fails with.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from check_coverage import (
    CoverageGateError,
    evaluate,
    load_covered_counts,
    load_thresholds,
    main,
)


def _write(directory: Path, name: str, document: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _report(files: dict[str, tuple[int, int]]) -> dict[str, object]:
    return {
        "files": {
            path: {"summary": {"covered_lines": covered, "num_statements": total}}
            for path, (covered, total) in files.items()
        }
    }


class LoadThresholdsTests(unittest.TestCase):
    def test_reads_integer_floors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write(Path(temp_dir), "thresholds.json", {"a.py": 12, "b.py": 0})
            self.assertEqual(load_thresholds(path), {"a.py": 12, "b.py": 0})

    def test_a_missing_file_is_a_gate_error_naming_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "absent.json"
            with self.assertRaises(CoverageGateError) as caught:
                load_thresholds(missing)
            self.assertIn(str(missing), str(caught.exception))

    def test_an_empty_object_is_rejected(self) -> None:
        # An empty manifest would otherwise pass every check while gating
        # nothing at all -- the failure mode this whole file exists to prevent.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write(Path(temp_dir), "thresholds.json", {})
            with self.assertRaises(CoverageGateError) as caught:
                load_thresholds(path)
            self.assertIn("non-empty object", str(caught.exception))

    def test_a_non_integer_floor_is_rejected_rather_than_compared(self) -> None:
        for floor in ("80", 80.5, None, True):
            with self.subTest(floor=floor), tempfile.TemporaryDirectory() as temp_dir:
                path = _write(Path(temp_dir), "thresholds.json", {"a.py": floor})
                with self.assertRaises(CoverageGateError) as caught:
                    load_thresholds(path)
                self.assertIn("non-negative integer", str(caught.exception))

    def test_invalid_json_is_reported_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "thresholds.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CoverageGateError) as caught:
                load_thresholds(path)
            self.assertIn("not valid JSON", str(caught.exception))


class LoadCoveredCountsTests(unittest.TestCase):
    def test_reads_covered_and_total_statements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write(Path(temp_dir), "coverage.json", _report({"a.py": (5, 7)}))
            self.assertEqual(load_covered_counts(path), {"a.py": (5, 7)})

    def test_a_report_measuring_nothing_is_a_gate_error(self) -> None:
        # A coverage run that measured no files produces a report the gate would
        # otherwise read as "no floors to check", and pass.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write(Path(temp_dir), "coverage.json", {"files": {}})
            with self.assertRaises(CoverageGateError) as caught:
                load_covered_counts(path)
            self.assertIn("measured nothing", str(caught.exception))

    def test_a_missing_report_says_how_to_produce_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(CoverageGateError) as caught:
                load_covered_counts(Path(temp_dir) / "coverage.json")
            self.assertIn("--cov-report=json", str(caught.exception))


class EvaluateTests(unittest.TestCase):
    def test_a_module_at_its_floor_passes(self) -> None:
        failures, passes, raisable = evaluate({"a.py": 5}, {"a.py": (5, 10)})
        self.assertEqual(failures, [])
        self.assertEqual(raisable, [])
        self.assertEqual(len(passes), 1)
        self.assertIn("5/10 statements (50%), floor 5", passes[0])

    def test_a_module_below_its_floor_fails_and_says_by_how_much(self) -> None:
        failures, passes, _ = evaluate({"a.py": 9}, {"a.py": (5, 10)})
        self.assertEqual(passes, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("floor is 9", failures[0])
        self.assertIn("4 fewer statements are reached", failures[0])

    def test_a_measured_module_with_no_floor_fails(self) -> None:
        # New code arriving without a recorded decision. This is the direction
        # that keeps the manifest complete as the repository grows.
        failures, _, _ = evaluate({"a.py": 1}, {"a.py": (1, 1), "b.py": (0, 4)})
        self.assertEqual(len(failures), 1)
        self.assertIn("b.py", failures[0])
        self.assertIn("has no floor", failures[0])

    def test_a_floor_for_an_unmeasured_module_fails(self) -> None:
        # The other direction: the manifest cannot rot after a rename or a
        # deletion, which would otherwise leave a floor nothing enforces.
        failures, _, _ = evaluate({"gone.py": 3}, {"a.py": (1, 1)})
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("gone.py" in line and "did not measure it" in line for line in failures))

    def test_coverage_above_the_floor_is_reported_as_raisable_but_still_passes(self) -> None:
        failures, passes, raisable = evaluate({"a.py": 5}, {"a.py": (8, 10)})
        self.assertEqual(failures, [])
        self.assertEqual(len(passes), 1)
        self.assertEqual(raisable, ["a.py: floor 5, reached 8"])

    def test_a_module_with_no_statements_is_reported_as_fully_covered(self) -> None:
        # An __init__.py has zero statements; a naive percentage would divide by
        # zero rather than report it.
        failures, passes, _ = evaluate({"pkg/__init__.py": 0}, {"pkg/__init__.py": (0, 0)})
        self.assertEqual(failures, [])
        self.assertIn("(100%)", passes[0])


class MainExitStatusTests(unittest.TestCase):
    def _paths(self, temp_dir: str, floors: dict[str, int], files: dict[str, tuple[int, int]]):
        directory = Path(temp_dir)
        return [
            "--thresholds",
            str(_write(directory, "thresholds.json", floors)),
            "--coverage-report",
            str(_write(directory, "coverage.json", _report(files))),
        ]

    def test_exits_zero_when_every_floor_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            argv = self._paths(temp_dir, {"a.py": 5}, {"a.py": (5, 10)})
            self.assertEqual(main(argv), 0)

    def test_exits_one_when_a_floor_fails(self) -> None:
        # 1, not 2: the gate ran and the repository failed it. The distinction
        # matters to whoever reads the job log.
        with tempfile.TemporaryDirectory() as temp_dir:
            argv = self._paths(temp_dir, {"a.py": 9}, {"a.py": (5, 10)})
            self.assertEqual(main(argv), 1)

    def test_exits_two_when_the_gate_cannot_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            argv = [
                "--thresholds",
                str(Path(temp_dir) / "absent.json"),
                "--coverage-report",
                str(Path(temp_dir) / "also-absent.json"),
            ]
            self.assertEqual(main(argv), 2)


class RecordedThresholdsTests(unittest.TestCase):
    def test_the_checked_in_manifest_parses_and_is_not_empty(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        floors = load_thresholds(repo_root / ".coverage-thresholds.json")
        self.assertGreater(len(floors), 1)
        # Every module under the measured roots has a threshold; this asserts
        # only that the file is real, since the completeness check itself needs
        # a coverage run and lives in .github/workflows/test.yml.
        self.assertIn("ci_tools/common.py", floors)


if __name__ == "__main__":
    unittest.main()
