"""
Script: tests/test_workflow_kernel_held_wiring.py
What: Follows the held kernel from input resolution to the image label through the YAML that
carries it: the prepare-main-akmods action's output, the build-zfs-akmods job's output in
.github/workflows/build.yml, and the build-candidate-image job's `with:` into the
build-native-image action.
Doing: Parses the workflow and the two composite actions with PyYAML and asserts each hop names
the one before it.
Why: Promotion (ci_tools/promote_stable.py) and the scheduled gate (ci_tools/check_stable_signal.py)
learn that a candidate was held only from its org.zfs-kinoite-complex.kernel-held-from label.
Each hop above is one line of YAML, the action input defaults to "", and the unit tests of
either end feed their own values, so deleting any one of the three lines left the whole suite
green while every held image was labelled as not held: promotion then skips its refusal to move
`latest` back to an older base, and the gate rebuilds the same held base every day.
Goal: Make a broken hop fail on the pull request that breaks it.

PyYAML is not a pytest dependency; CI installs it by name (see .github/workflows/test.yml). The
import is guarded so the suite still runs under `python3 -m unittest discover -s tests` with
nothing installed, matching tests/test_action_build_native_image.py.
"""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
PREPARE_ACTION = REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
BUILD_ACTION = REPO_ROOT / ".github" / "actions" / "build-native-image" / "action.yml"

PREPARE_USES = "./.github/actions/prepare-main-akmods"
BUILD_USES = "./.github/actions/build-native-image"
LABEL = "org.zfs-kinoite-complex.kernel-held-from"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def step_using(job: dict, uses: str) -> dict:
    steps = [step for step in job.get("steps", []) if step.get("uses") == uses]
    if len(steps) != 1:
        raise AssertionError(f"expected exactly one step using {uses}, found {len(steps)}")
    return steps[0]


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class KernelHeldWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = load(BUILD_WORKFLOW)["jobs"]

    def test_the_prepare_action_returns_what_input_resolution_wrote(self) -> None:
        action = load(PREPARE_ACTION)
        resolve = [step for step in action["runs"]["steps"] if step.get("id") == "resolve"]
        self.assertEqual(len(resolve), 1)
        self.assertEqual(
            action["outputs"]["kernel_held_from"]["value"],
            "${{ steps.resolve.outputs.kernel_held_from }}",
        )

    def test_the_akmods_job_exports_the_prepare_step_output(self) -> None:
        job = self.jobs["build-zfs-akmods"]
        prepare = step_using(job, PREPARE_USES)
        self.assertEqual(
            job["outputs"]["kernel_held_from"],
            f"${{{{ steps.{prepare['id']}.outputs.kernel_held_from }}}}",
        )

    def test_the_candidate_build_labels_the_image_with_the_akmods_job_output(self) -> None:
        job = self.jobs["build-candidate-image"]
        self.assertIn("build-zfs-akmods", job["needs"])
        build = step_using(job, BUILD_USES)
        self.assertEqual(
            build["with"]["kernel_held_from"],
            "${{ needs.build-zfs-akmods.outputs.kernel_held_from }}",
        )

    def test_the_build_action_turns_the_input_into_the_label_promotion_reads(self) -> None:
        # tests/test_action_build_native_image.py runs the step and checks the label value;
        # this pins that the label name is the one the readers look up.
        from ci_tools import check_stable_signal, promote_stable

        self.assertEqual(check_stable_signal.KERNEL_HELD_FROM_LABEL, LABEL)
        self.assertEqual(promote_stable.KERNEL_HELD_FROM_LABEL, LABEL)
        self.assertIn(f"{LABEL}=${{KERNEL_HELD_FROM}}", BUILD_ACTION.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
