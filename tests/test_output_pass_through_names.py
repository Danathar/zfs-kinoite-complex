"""
Script: tests/test_output_pass_through_names.py
What: Checks that a job output or composite-action output which passes another output along reads
the output of its own name.
Doing: Reads each `outputs:` map in .github/workflows/ and .github/actions/ (either YAML spelling)
with PyYAML, collects every `steps.<id>.outputs.<name>` and `needs.<job>.outputs.<name>` its
value references, and requires `<name>` to be the output's own name, except for the renames
listed in `RENAMES`.
Why: The resolved build inputs reach the image build through two of these maps: the
prepare-main-akmods action's `outputs:` and the `outputs:` of the job that runs it. Each repeats
the nineteen resolved input names by hand, and nothing compared them. Wiring the action's
`base_image_pinned` to `steps.resolve.outputs.base_image_ref`, or `zfs_version` to
`zfs_minor_version`, kept every test green, while production would have built on the floating
base tag, or handed the build the ZFS minor line instead of the patch it resolved.
Goal: A pass-through output can only carry the value its name promises, and a deliberate rename
has to be written down here.

PyYAML is not a pytest dependency; CI installs it by name (see .github/workflows/test.yml). The
import is guarded so the suite still runs under `python3 -m unittest discover -s tests` with
nothing installed.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

from tests.test_docs_consistency import action_paths, workflow_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_REFERENCE = re.compile(r"\b(?:steps|needs)\.[A-Za-z0-9_-]+\.outputs\.([A-Za-z0-9_-]+)")

# (file, job or None for a composite action, output) -> the differently named output it reads.
# Held in both directions: an entry whose output is gone, or no longer renames, fails too.
RENAMES = {
    (".github/workflows/build.yml", "build-zfs-akmods", "fedora_version"): "version",
    (".github/workflows/build-branch.yml", "build-branch-akmods", "fedora_version"): "version",
    (".github/actions/prepare-main-akmods/action.yml", None, "akmods_cache_exists"): "exists",
}


def _output_maps() -> list[tuple[tuple[str, str | None], dict[str, str]]]:
    """Every `outputs:` map in the workflows and composite actions, as name -> expression."""

    maps: list[tuple[tuple[str, str | None], dict[str, str]]] = []
    for path in workflow_paths():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in (workflow.get("jobs") or {}).items():
            outputs = job.get("outputs") or {}
            maps.append(
                (
                    (str(path.relative_to(REPO_ROOT)), job_id),
                    {name: str(value) for name, value in outputs.items()},
                )
            )
    for path in action_paths():
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        outputs = action.get("outputs") or {}
        maps.append(
            (
                (str(path.relative_to(REPO_ROOT)), None),
                {name: str(spec.get("value", "")) for name, spec in outputs.items()},
            )
        )
    return maps


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class PassThroughOutputNameTests(unittest.TestCase):
    def test_the_scan_reaches_the_maps_the_build_inputs_travel_through(self) -> None:
        names = {where: set(outputs) for where, outputs in _output_maps()}
        for where in (
            (".github/actions/prepare-main-akmods/action.yml", None),
            (".github/workflows/build.yml", "build-zfs-akmods"),
            (".github/workflows/build-branch.yml", "build-branch-akmods"),
        ):
            with self.subTest(where=where):
                self.assertIn("base_image_pinned", names.get(where, set()))

    def test_each_output_reads_the_output_of_its_own_name(self) -> None:
        for (path, job), outputs in _output_maps():
            for name, expression in outputs.items():
                expected = RENAMES.get((path, job, name), name)
                for read in OUTPUT_REFERENCE.findall(expression):
                    with self.subTest(file=path, job=job, output=name):
                        self.assertEqual(
                            read,
                            expected,
                            f"{path} output {name!r} reads {read!r}; republish an output under "
                            "its own name, or list a deliberate rename in RENAMES",
                        )

    def test_every_listed_rename_still_exists(self) -> None:
        outputs = dict(_output_maps())
        for (path, job, name), source in RENAMES.items():
            with self.subTest(file=path, job=job, output=name):
                expression = outputs.get((path, job), {}).get(name)
                self.assertIsNotNone(expression, f"{path} no longer has output {name!r}")
                self.assertEqual(OUTPUT_REFERENCE.findall(expression), [source])


if __name__ == "__main__":
    unittest.main()
