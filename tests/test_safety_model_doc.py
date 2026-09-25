"""
Script: tests/test_safety_model_doc.py
What: Joins docs/safety-model.md -- the page README sends a reader to before they put `:latest`
on a machine -- to the workflow, composite action and helpers whose behaviour it promises.
Doing: Parses the "What the image guarantees" list and compares its provenance items to the OCI
labels `.github/actions/build-native-image/action.yml` stamps on the image, in both directions;
recomputes which jobs `promote-stable` waits on and checks the page's list of promotion gates
against what those jobs run; and re-derives the ordering, scheduled-build, primary-kernel and
signing-key claims from the code that implements them.
Why: The page was read by no test as a subject. Its references under `tests/` were a docstring
sentence in tests/test_issue_templates.py, a comment in tests/test_code_reading_guide.py, and
tests/test_runtime_validation_proposal_doc.py reading one boundary sentence back out as a
source. Two claims had already gone stale when this file was written.
Goal: Make a new provenance label, a gate that stops gating, or a scheduled-build trigger the
page does not name fail here, instead of leaving an operator trusting a promise nothing keeps.

The two stale claims this file was written against:

  * Step 1 said the workflow resolves and records "the Fedora Kinoite base, kernel set, OpenZFS
    release, akmods commit, and cache digest". The build also resolves the Homebrew payload
    image to a digest, records it in `build-inputs.json` as `brew_image_ref` / `_pinned` /
    `_digest`, and stamps it on the image as `org.zfs-kinoite-complex.brew-image`. Brew is the
    image this repository's docs keep forgetting: docs/glossary.md, docs/zfs-kinoite-testing.md
    and docs/building-locally.md had each dropped it before.
  * "The `bootc container lint` check, package/module checks, signature verification, and unit
    tests are useful gates" -- the unit tests run in `test.yml`, which `build.yml` neither calls
    nor waits on. When this file was written `main` also had no ruleset, and docs/quality.md
    said a red `Python Unit Tests` "blocks nothing automatically". The ruleset added since
    (docs/branch-protection.md) makes that check required for MERGING a pull request; it
    still gates nothing in `build.yml`. A push to `main` runs both workflows side by side and
    `latest` moves whatever the tests say.

docs/code-reading-guide.md carried a third copy of the scheduled-build rule that named only the
Kinoite base and not the OpenZFS patch; it is checked here as well, because it cites this page
for the rule.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; see test.yml
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "safety-model.md"
CODE_READING_GUIDE = REPO_ROOT / "docs" / "code-reading-guide.md"
DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BUILD = WORKFLOWS / "build.yml"
BUILD_ACTION = REPO_ROOT / ".github" / "actions" / "build-native-image" / "action.yml"
MANIFEST_WRITER = REPO_ROOT / "ci_tools" / "write_build_inputs_manifest.py"
PROMOTE = REPO_ROOT / "ci_tools" / "promote_stable.py"
RESOLVE = REPO_ROOT / "ci_tools" / "resolve_build_inputs.py"
STABLE_SIGNAL = REPO_ROOT / "ci_tools" / "check_stable_signal.py"

LABEL_PREFIX = "org.zfs-kinoite-complex."
LABEL_RE = re.compile(r'--label "' + re.escape(LABEL_PREFIX) + r"([a-z-]+)=")

# Each item of the guarantees list's step 1, and what in the machine records it. An item is
# recorded either as OCI labels on the image or, for the kernel set, as a key of the
# `build-inputs.json` manifest (the image carries no kernel label). Every label the build
# action stamps must be claimed by exactly one item, so a new label fails until the page
# says what it records.
PROVENANCE_LABELS = {
    "Fedora Kinoite base": {"base-image", "stable-signal-image", "stable-signal-digest"},
    "kernel set": set(),
    "OpenZFS release": {"zfs-version"},
    "akmods commit": {"akmods-ref"},
    "Homebrew payload image": {"brew-image"},
    "cache digest": {"akmods-image"},
}
PROVENANCE_MANIFEST_KEYS = {
    "kernel set": {"detected_kernel_releases", "kernel_release"},
    "Homebrew payload image": {"brew_image_ref", "brew_image_pinned", "brew_image_digest"},
}

# The promotion gates the page names, and a needle proving each one runs inside a job that
# `promote-stable` waits on. `package/module checks` are the build script's own assertions
# and run in the same `buildah build` as the lint.
GATES = {
    "`bootc container lint` check": "RUN bootc container lint",
    "package/module checks": "/ctx/build-image.sh",
    "signature verification": "verify_candidate_signature",
}
TEST_COMMAND_RE = re.compile(r"\b(pytest|run_tests\.py|unittest)\b")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _section(heading: str) -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def _numbered_items(section: str) -> dict[int, str]:
    """Numbered list items, with wrapped continuation lines folded in."""
    items: dict[int, str] = {}
    current = None
    for line in section.splitlines():
        match = re.match(r"^(\d+)\. (.*)$", line)
        if match:
            current = int(match.group(1))
            items[current] = match.group(2)
        elif current is not None and line.startswith("   ") and line.strip():
            items[current] += " " + line.strip()
        else:
            current = None
    return {n: _squash(v) for n, v in items.items()}


def _step_one_items() -> list[str]:
    step = _numbered_items(_section("## What the image guarantees"))[1]
    prefix = "resolve and record the "
    if not step.startswith(prefix):
        raise AssertionError(f"step 1 no longer starts with {prefix!r}: {step!r}")
    body = step[len(prefix) :]
    return [part.strip() for part in re.split(r",\s*(?:and\s+)?", body) if part.strip()]


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _build_labels() -> set[str]:
    return set(LABEL_RE.findall(_strip_comments(BUILD_ACTION.read_text(encoding="utf-8"))))


def _manifest_input_keys() -> set[str]:
    tree = ast.parse(MANIFEST_WRITER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "inputs" in keys:
                inputs = node.values[keys.index("inputs")]
                return {k.value for k in inputs.keys if isinstance(k, ast.Constant)}
    raise AssertionError("write_build_inputs_manifest.py no longer builds an `inputs` block")


def _gate_sentence() -> str:
    section = _squash(_section("## What the image guarantees"))
    match = re.search(r"CI does not boot the image.*?\. (The .*?) but they are not", section)
    if match is None:
        raise AssertionError("the gate sentence after 'CI does not boot the image' is gone")
    return match.group(1)


def _call_lines(path: Path, function: str, callee: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return sorted(
                call.lineno
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == callee
            )
    raise AssertionError(f"{path.name} has no function {function}")


class ProvenanceListTests(unittest.TestCase):
    """Step 1's list of what a run resolves and records, against what the build records."""

    def test_step_one_names_exactly_the_provenance_items_this_file_maps(self) -> None:
        self.assertEqual(
            _step_one_items(),
            list(PROVENANCE_LABELS),
            "Step 1 of 'What the image guarantees' changed its list of recorded inputs. "
            "Update PROVENANCE_LABELS to say what records each item.",
        )

    def test_every_label_the_build_stamps_is_named_by_an_item_on_the_page(self) -> None:
        claimed = set().union(*PROVENANCE_LABELS.values())
        unclaimed = _build_labels() - claimed
        self.assertEqual(
            unclaimed,
            set(),
            "build-native-image stamps a provenance label that step 1 of docs/safety-model.md "
            "does not name. The page promises the run records its inputs; name this one too.",
        )

    def test_every_label_an_item_claims_is_really_stamped(self) -> None:
        claimed = set().union(*PROVENANCE_LABELS.values())
        self.assertEqual(claimed - _build_labels(), set())

    def test_no_label_is_claimed_by_two_items(self) -> None:
        seen: dict[str, str] = {}
        for item, labels in PROVENANCE_LABELS.items():
            for label in labels:
                self.assertNotIn(label, seen, f"{label} claimed by {seen.get(label)} and {item}")
                seen[label] = item

    def test_items_without_a_label_are_recorded_in_the_inputs_manifest(self) -> None:
        keys = _manifest_input_keys()
        for item, labels in PROVENANCE_LABELS.items():
            if not labels:
                self.assertIn(item, PROVENANCE_MANIFEST_KEYS, f"{item} is recorded nowhere")
        for item, wanted in PROVENANCE_MANIFEST_KEYS.items():
            with self.subTest(item=item):
                self.assertLessEqual(wanted, keys)

    def test_the_brew_image_is_on_the_page(self) -> None:
        # Brew is the image this repository's docs keep dropping; pin it by name so a
        # rewrite of the list that loses it again cannot pass by also editing the map.
        step = _numbered_items(_section("## What the image guarantees"))[1]
        self.assertIn("Homebrew payload image", step)
        self.assertIn("brew-image", _build_labels())


class PromotionGateTests(unittest.TestCase):
    """The page's list of promotion gates, against the jobs `promote-stable` waits on."""

    @classmethod
    def setUpClass(cls) -> None:
        if yaml is None:
            raise unittest.SkipTest("PyYAML not installed")
        cls.jobs = yaml.safe_load(BUILD.read_text(encoding="utf-8"))["jobs"]

    def _needs_closure(self, job: str) -> set[str]:
        seen: set[str] = set()
        pending = [job]
        while pending:
            needs = self.jobs[pending.pop()].get("needs", [])
            for name in [needs] if isinstance(needs, str) else needs:
                if name not in seen:
                    seen.add(name)
                    pending.append(name)
        return seen | {job}

    def _gating_text(self) -> str:
        """Every `run:` and `uses:` in promote-stable and the jobs it waits on, with the
        composite actions and scripts they reach inlined."""
        chunks: list[str] = []
        for name in self._needs_closure("promote-stable"):
            for step in self.jobs[name].get("steps", []):
                chunks.append(step.get("run", ""))
                uses = step.get("uses", "")
                if uses.startswith("./"):
                    chunks.append(_strip_comments((REPO_ROOT / uses / "action.yml").read_text()))
        text = "\n".join(chunks)
        if "./Containerfile" in text:
            chunks.append(CONTAINERFILE.read_text(encoding="utf-8"))
        if "promote-stable" in text:
            chunks.append(PROMOTE.read_text(encoding="utf-8"))
        return "\n".join(chunks)

    def test_the_promotion_job_still_waits_on_both_builds(self) -> None:
        closure = self._needs_closure("promote-stable")
        self.assertLessEqual({"build-zfs-akmods", "build-candidate-image"}, closure)

    def test_the_page_names_exactly_the_gates_this_file_checks(self) -> None:
        sentence = _gate_sentence()
        for gate in GATES:
            with self.subTest(gate=gate):
                self.assertIn(gate, sentence)
        named = re.sub(r"^The ", "", sentence.split(" run inside")[0])
        parts = [p.strip() for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", named) if p.strip()]
        self.assertEqual(parts, list(GATES))

    def test_every_gate_the_page_names_runs_before_promotion(self) -> None:
        text = self._gating_text()
        for gate, needle in GATES.items():
            with self.subTest(gate=gate):
                self.assertTrue(
                    needle in text, f"{needle!r} runs in no job promote-stable waits on"
                )

    def test_unit_tests_are_named_as_a_gate_only_if_promotion_waits_on_them(self) -> None:
        runs_tests = bool(TEST_COMMAND_RE.search(self._gating_text()))
        claims_gate = "unit tests" in _gate_sentence()
        self.assertEqual(
            claims_gate,
            runs_tests,
            "docs/safety-model.md lists unit tests as a promotion gate, but no job promote-stable "
            "waits on runs them"
            if claims_gate
            else "promote-stable now waits on a test run; docs/safety-model.md should say so",
        )

    def test_the_page_says_where_the_unit_tests_do_run(self) -> None:
        section = _squash(_section("## What the image guarantees"))
        self.assertIn("unit tests are not among those gates", section)
        self.assertIn("`test.yml`", section)
        test_yml = yaml.safe_load((WORKFLOWS / "test.yml").read_text(encoding="utf-8"))
        runs = "\n".join(
            step.get("run", "") for job in test_yml["jobs"].values() for step in job["steps"]
        )
        self.assertRegex(runs, TEST_COMMAND_RE)
        self.assertNotIn("test.yml", BUILD.read_text(encoding="utf-8"))

    def test_a_failed_candidate_leaves_latest_where_it_was(self) -> None:
        condition = _squash(self.jobs["promote-stable"]["if"])
        self.assertIn("needs.build-candidate-image.result == 'success'", condition)
        self.assertIn("needs.build-zfs-akmods.result == 'success'", condition)


class PromotionOrderTests(unittest.TestCase):
    def test_signature_is_verified_before_any_tag_moves(self) -> None:
        verify = _call_lines(PROMOTE, "main", "verify_candidate_signature")
        copies = _call_lines(PROMOTE, "main", "_copy_and_verify_digest")
        self.assertTrue(verify, "promote_stable.main no longer verifies the candidate signature")
        self.assertTrue(copies)
        self.assertLess(max(verify), min(copies))

    def test_step_five_still_promises_that_order(self) -> None:
        step = _numbered_items(_section("## What the image guarantees"))[5]
        self.assertIn("only after the exact signed candidate digest passes", step)


class ScheduledBuildTests(unittest.TestCase):
    """'Scheduled builds are gated on movement of the Fedora Kinoite base or a newer OpenZFS
    patch; pushes and manual runs build when requested.'"""

    SENTENCE = (
        "Scheduled builds are gated on movement of the Fedora Kinoite base or a newer OpenZFS "
        "patch; pushes and manual runs build when requested."
    )

    def test_the_page_still_makes_the_claim(self) -> None:
        self.assertIn(self.SENTENCE, _squash(DOC.read_text(encoding="utf-8")))

    def test_the_gate_compares_both_the_base_and_the_zfs_patch(self) -> None:
        source = STABLE_SIGNAL.read_text(encoding="utf-8")
        self.assertIn(
            'STABLE_SIGNAL_DIGEST_LABEL = "org.zfs-kinoite-complex.stable-signal-digest"', source
        )
        self.assertIn('ZFS_VERSION_LABEL = "org.zfs-kinoite-complex.zfs-version"', source)
        self.assertIn("resolve_latest_zfs_version", source)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_only_scheduled_runs_consult_the_gate(self) -> None:
        workflow = yaml.safe_load(BUILD.read_text(encoding="utf-8"))
        # PyYAML reads the bare key `on` as True.
        triggers = workflow.get("on", workflow.get(True))
        self.assertLessEqual({"schedule", "push", "workflow_dispatch"}, set(triggers))
        for job in ("build-zfs-akmods", "build-candidate-image"):
            with self.subTest(job=job):
                self.assertEqual(
                    _squash(workflow["jobs"][job]["if"]),
                    "github.event_name != 'schedule' || needs.preflight.outputs.should_build == 'true'",
                )

    def test_the_code_reading_guide_copy_names_both_triggers(self) -> None:
        lines = [
            line
            for line in CODE_READING_GUIDE.read_text(encoding="utf-8").splitlines()
            if "scheduled runs skip" in line
        ]
        self.assertEqual(len(lines), 1)
        self.assertIn("Kinoite base image", lines[0])
        self.assertIn("OpenZFS patch", lines[0])


class NamedThingsTests(unittest.TestCase):
    def test_the_zfs_line_key_is_a_real_default(self) -> None:
        self.assertIn("`DEFAULT_ZFS_MINOR_VERSION`", DOC.read_text(encoding="utf-8"))
        defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
        self.assertRegex(defaults["DEFAULT_ZFS_MINOR_VERSION"], r"^\d+\.\d+$")

    def test_the_primary_kernel_is_the_newest_detected(self) -> None:
        # "The image supports the newest detected kernel as its primary kernel."
        self.assertIn("newest detected kernel as its primary kernel", _squash(DOC.read_text()))
        source = RESOLVE.read_text(encoding="utf-8")
        self.assertIn("detected_kernel_releases = sort_kernel_releases(", source)
        self.assertIn("kernel_release = detected_kernel_releases[-1]", source)

    def test_the_signing_secret_and_environment_are_the_ones_build_yml_uses(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        build = BUILD.read_text(encoding="utf-8")
        self.assertIn("`production-signing` GitHub Environment as `SIGNING_SECRET`", _squash(text))
        self.assertIn("environment: production-signing", build)
        self.assertIn("secrets.SIGNING_SECRET", build)

    def test_the_repository_holds_only_the_public_key(self) -> None:
        self.assertTrue((REPO_ROOT / "cosign.pub").is_file())
        self.assertIn("only the public verification key, `cosign.pub`", _squash(DOC.read_text()))
        offenders = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.rglob("*")
            if ".git" not in path.parts
            and path.is_file()
            and (path.suffix == ".key" or b"PRIVATE " + b"KEY-----" in path.read_bytes()[:4096])
        ]
        self.assertEqual(offenders, [])

    def test_the_rollback_commands_are_the_bootc_ones(self) -> None:
        section = _section("## Pool recovery discipline")
        self.assertIn("```bash\nsudo bootc rollback\nsudo systemctl reboot\n```", section)


if __name__ == "__main__":
    unittest.main()
