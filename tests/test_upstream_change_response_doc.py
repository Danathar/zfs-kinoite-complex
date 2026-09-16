"""
Script: tests/test_upstream_change_response_doc.py
What: Joins docs/upstream-change-response.md to the workflow jobs, helper modules and defaults
whose behaviour it restates by hand.
Doing: Extracts the runbook's own claims -- the failure areas it tells a responder to triage,
the six input values it tells them to record, its six-row failure table, its emergency-freeze
rule -- and recomputes each one against the tree, executing the real promotion, the real cache
check, the real install planner and the real ref cascade rather than grepping for strings.
Why: This is the page README.md, docs/documentation-guide.md, docs/reflections/README.md and
docs/akmods-fork-maintenance.md all send a maintainer to while a production build is red. Its
triage list is a hand copy of build.yml's job set, its table's first actions are hand copies of
fail-closed paths in ci_tools/ and containerfiles/, and its freeze instruction is a hand copy of
the cascade in resolve_build_inputs. tests/test_docs_consistency.py opens the file for the two
checks it runs over every doc -- links resolve, the file appears in the documentation map -- and
reads no sentence, so every one of those pointers could drift, or be fixed, and the runbook would
keep reading exactly the same.
Goal: Make the runbook fail here when the machine moves under it, in both directions.

Parses Markdown and YAML by hand. The CI job installs pytest, pytest-cov and ruff and nothing
else (see .github/workflows/test.yml), so a PyYAML import here would skip in exactly the place
these assertions are meant to run. `_section`, `_numbered_items`, `_table_rows` and
`_workflow_jobs` are the whole parser and carry their own case table in `ParserTests` below,
because a hand-rolled parser that is never wrong about a fixture is the only thing keeping the
assertions built on it honest.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from ci_tools import pin_akmods_cache, promote_stable, write_build_inputs_manifest
from ci_tools.check_akmods_cache import AkmodsCacheStatus, inspect_akmods_cache
from ci_tools.classify_akmods_failure import FAILURE_KIND_UPSTREAM_COMPAT, classify_log_text
from ci_tools.common import CiToolError
from ci_tools.resolve_build_inputs import _resolve_default_akmods_ref

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "upstream-change-response.md"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
PREPARE_ACTION = REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
DEFAULTS_FILE = REPO_ROOT / "ci" / "defaults.json"
SIBLING_DOC = REPO_ROOT / "docs" / "akmods-fork-maintenance.md"
COSIGN_PUBLIC_KEY = REPO_ROOT / "cosign.pub"

DOC_TEXT = DOC.read_text(encoding="utf-8")
BUILD_TEXT = BUILD_WORKFLOW.read_text(encoding="utf-8")
DEFAULTS = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))

# Every `##` section this module reads. `DocumentStructureTests` asserts this is the whole
# list in both directions, so a section added to the runbook arrives here unread exactly once.
SECTIONS = (
    "## First triage",
    "## Common failure classes",
    "## Response rules",
    "## After recovery",
)

BACKTICKED_RE = re.compile(r"`([^`]+)`")


def _load_installer():
    """
    Import the cache installer, which lives in a directory a plain import cannot name.

    `containerfiles/zfs-akmods/` is a path inside the image build, not a Python package, so
    tests/test_install_zfs_from_akmods_cache.py loads it by file location and this file does
    the same rather than inventing a second mechanism.
    """

    helper_path = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
    spec = importlib.util.spec_from_file_location("install_zfs_from_akmods_cache", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = _load_installer()


def _section(text: str, heading: str) -> list[str]:
    """
    Return the lines under `heading`, up to the next heading of the same or higher level.

    `heading` is the full heading line including its `#` markers, so asking for `## Response
    rules` can never accidentally match `### Response rules`. Raises when the heading is absent
    or the section is empty: a renamed or emptied section must fail loudly here rather than
    quietly hand every assertion below an empty list to pass against.
    """

    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"{DOC.name} has no heading {heading!r}") from exc

    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        body.append(line)

    if not any(line.strip() for line in body):
        raise AssertionError(f"{DOC.name} section {heading!r} is empty")
    return body


def _numbered_items(lines: list[str]) -> list[str]:
    """
    Return the first top-level ordered list in `lines`, each item flattened to one string.

    A line that is neither blank nor a new `N. ` item continues the current item, because this
    document hard-wraps its longer steps. A blank line ends the list. Numbering must run 1..n:
    a document that skips or repeats a number is a parse this file refuses rather than guesses
    at, since a silently dropped step is exactly the drift these assertions exist to catch.
    """

    items: list[str] = []
    open_item = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if items:
                break
            open_item = False
            continue
        match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if match and not line.startswith("   "):
            expected = len(items) + 1
            if int(match.group(1)) != expected:
                raise AssertionError(
                    f"{DOC.name} ordered list numbering jumped to {match.group(1)} "
                    f"where {expected} was expected"
                )
            items.append(match.group(2).strip())
            open_item = True
        elif open_item:
            items[-1] = f"{items[-1]} {stripped}"
    return items


def _table_rows(lines: list[str]) -> list[tuple[str, str]]:
    """
    Return the `(failure, first action)` pairs of the first two-column table in `lines`.

    The header row and the `|---|---|` separator are dropped. A row with a different column
    count is an error rather than a skip: the failure table is the part of this page a reader
    scans first, and a malformed row that silently vanished from this parse would take its
    assertion with it.
    """

    rows: list[tuple[str, str]] = []
    seen_separator = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 2:
            raise AssertionError(f"{DOC.name} table row is not two columns: {stripped!r}")
        if set("".join(cells)) <= set("-: "):
            seen_separator = True
            continue
        if not seen_separator:
            continue
        rows.append((cells[0], cells[1]))
    return rows


def _workflow_jobs(text: str) -> dict[str, list[str]]:
    """
    Return `{job id: body lines}` for one workflow's top-level `jobs:` mapping.

    Job ids are the two-space-indented keys under `jobs:`; the body is every line up to the
    next such key. Enough for the questions asked here -- which jobs exist, what each one's
    `if:`, `needs:`, `environment:` and `container:` say -- without pretending to be YAML.
    """

    lines = text.splitlines()
    try:
        start = lines.index("jobs:")
    except ValueError as exc:
        raise AssertionError("workflow has no top-level `jobs:` mapping") from exc

    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            jobs[current] = []
            continue
        if current is not None:
            jobs[current].append(line)
    return jobs


def _job_scalar(job_lines: list[str], key: str) -> str:
    """
    Return one job-level scalar, flattened to a single whitespace-normalised string.

    Handles both the inline form (`if: needs.x.result == 'success'`) and the folded block form
    (`if: >-` followed by indented lines), because `promote-stable` uses the second one and its
    condition is the claim the page's opening paragraph makes.
    """

    prefix = f"    {key}:"
    for index, line in enumerate(job_lines):
        if not line.startswith(prefix):
            continue
        inline = line[len(prefix) :].strip()
        if inline and inline not in {">-", ">", "|", "|-"}:
            return inline
        collected: list[str] = []
        for continuation in job_lines[index + 1 :]:
            if not continuation.strip():
                if collected:
                    break
                continue
            if not continuation.startswith("      "):
                break
            collected.append(continuation.strip())
        return " ".join(collected)
    raise AssertionError(f"job has no `{key}:` entry")


def _tracked(path: Path) -> bool:
    """True when `path` is committed, asked of git rather than of the filesystem."""

    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path.relative_to(REPO_ROOT))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


BUILD_JOBS = _workflow_jobs(BUILD_TEXT)


class ParserTests(unittest.TestCase):
    """The parsers above, against fixtures whose answers are known by inspection."""

    def test_section_stops_at_the_next_heading_of_the_same_level(self) -> None:
        text = "# Title\n\n## One\n\nalpha\n\n## Two\n\nbeta\n"
        self.assertEqual([line for line in _section(text, "## One") if line], ["alpha"])

    def test_section_keeps_deeper_headings_inside_the_section(self) -> None:
        text = "## One\n\nalpha\n\n### Deeper\n\nbeta\n\n## Two\n\ngamma\n"
        body = [line for line in _section(text, "## One") if line]
        self.assertEqual(body, ["alpha", "### Deeper", "beta"])

    def test_section_refuses_a_missing_heading(self) -> None:
        with self.assertRaises(AssertionError):
            _section("## One\n\nalpha\n", "## Absent")

    def test_section_refuses_an_empty_section(self) -> None:
        with self.assertRaises(AssertionError):
            _section("## One\n\n## Two\n\nbeta\n", "## One")

    def test_numbered_items_joins_a_hard_wrapped_item(self) -> None:
        lines = ["1. first line", "   continued here", "2. second", ""]
        self.assertEqual(_numbered_items(lines), ["first line continued here", "second"])

    def test_numbered_items_stops_at_the_end_of_the_first_list(self) -> None:
        lines = ["1. alpha", "", "prose", "", "1. beta"]
        self.assertEqual(_numbered_items(lines), ["alpha"])

    def test_numbered_items_refuses_a_renumbered_list(self) -> None:
        with self.assertRaises(AssertionError):
            _numbered_items(["1. alpha", "3. gamma"])

    def test_table_rows_drops_the_header_and_separator(self) -> None:
        lines = ["| Failure | First action |", "|---|---|", "| a | b |", "| c | d |", ""]
        self.assertEqual(_table_rows(lines), [("a", "b"), ("c", "d")])

    def test_table_rows_refuses_a_row_with_the_wrong_column_count(self) -> None:
        with self.assertRaises(AssertionError):
            _table_rows(["| Failure | First action |", "|---|---|", "| a | b | c |"])

    def test_workflow_jobs_splits_on_two_space_keys(self) -> None:
        text = "name: W\n\njobs:\n  one:\n    runs-on: x\n  two:\n    runs-on: y\n"
        jobs = _workflow_jobs(text)
        self.assertEqual(sorted(jobs), ["one", "two"])
        self.assertEqual([line.strip() for line in jobs["one"]], ["runs-on: x"])

    def test_workflow_jobs_does_not_treat_a_nested_key_as_a_job(self) -> None:
        text = "jobs:\n  one:\n    steps:\n      - uses: x\n    container:\n      image: y\n"
        self.assertEqual(sorted(_workflow_jobs(text)), ["one"])

    def test_job_scalar_reads_an_inline_value(self) -> None:
        self.assertEqual(_job_scalar(["    if: a == 'b'"], "if"), "a == 'b'")

    def test_job_scalar_folds_a_block_value(self) -> None:
        lines = ["    if: >-", "      a == 'b'", "      && c == 'd'", "    runs-on: x"]
        self.assertEqual(_job_scalar(lines, "if"), "a == 'b' && c == 'd'")

    def test_job_scalar_refuses_a_missing_key(self) -> None:
        with self.assertRaises(AssertionError):
            _job_scalar(["    runs-on: x"], "if")


class DocumentStructureTests(unittest.TestCase):
    """The page's own shape, so the assertions below cannot be scoped to a heading that moved."""

    def test_the_document_is_committed_and_titled(self) -> None:
        self.assertTrue(_tracked(DOC), f"{DOC.name} is not tracked by git")
        self.assertEqual(DOC_TEXT.splitlines()[0], "# Upstream Change Response")

    def test_every_section_in_the_document_is_read_by_this_module(self) -> None:
        headings = [line for line in DOC_TEXT.splitlines() if line.startswith("## ")]
        self.assertTrue(headings, f"{DOC.name} has no `##` sections")
        self.assertEqual(headings, list(SECTIONS))

    def test_every_section_this_module_reads_is_non_empty(self) -> None:
        for heading in SECTIONS:
            with self.subTest(heading=heading):
                self.assertTrue(any(line.strip() for line in _section(DOC_TEXT, heading)))

    def test_the_runbook_is_still_pointed_at_from_the_entry_points(self) -> None:
        # The page is only useful if the places a responder actually starts from still lead
        # here. Both of these link it today; a rename that left them behind would strand it.
        for path in (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "documentation-guide.md"):
            with self.subTest(path=path.name):
                self.assertIn("upstream-change-response.md", path.read_text(encoding="utf-8"))


class OpeningClaimTests(unittest.TestCase):
    """The paragraph before the first heading: the moving inputs, and what a red build does."""

    def setUp(self) -> None:
        self.intro = DOC_TEXT.split("## First triage", 1)[0]

    def test_the_akmods_fork_the_intro_names_is_the_configured_upstream(self) -> None:
        self.assertIn("`Danathar/akmods`", self.intro)
        self.assertIn("Danathar/akmods", DEFAULTS["AKMODS_UPSTREAM_REPO"])

    def test_the_base_image_the_intro_names_is_the_configured_default(self) -> None:
        self.assertIn("Fedora Kinoite", self.intro)
        self.assertIn("kinoite", DEFAULTS["DEFAULT_BASE_IMAGE"])

    def test_the_build_container_the_intro_names_is_digest_pinned(self) -> None:
        self.assertIn("build container", self.intro)
        self.assertIn("@sha256:", DEFAULTS["DEFAULT_BUILD_CONTAINER_IMAGE"])

    def test_a_red_build_stops_promotion(self) -> None:
        # "A red build is expected to stop promotion while the last known-good image remains
        # available" is a claim about one `if:` expression: promotion runs only when both
        # build jobs succeeded, so a red run leaves `:latest` where it was.
        self.assertIn("stop promotion", self.intro)
        condition = _job_scalar(BUILD_JOBS["promote-stable"], "if")
        for job in ("build-zfs-akmods", "build-candidate-image"):
            with self.subTest(job=job):
                self.assertIn(f"needs.{job}.result == 'success'", condition)

    def test_promotion_depends_on_the_jobs_whose_result_it_reads(self) -> None:
        # A condition naming a job that is not in `needs:` is always false at evaluation time,
        # so the gate above is only real while both appear here too.
        needs = " ".join(BUILD_JOBS["promote-stable"])
        for job in ("build-zfs-akmods", "build-candidate-image"):
            with self.subTest(job=job):
                self.assertIn(f"- {job}", needs)


class FirstTriageTests(unittest.TestCase):
    """The four numbered steps, joined to the jobs and to the record the run leaves behind."""

    # The failure areas step 1 names, each mapped to the build.yml job that owns it. Asserted
    # exhaustive in both directions below: a new job in build.yml is a job this page does not
    # tell a responder how to triage, and that should fail here rather than be discovered
    # during an outage.
    AREA_JOBS: ClassVar[dict[str, tuple[str, ...]]] = {
        "input resolution": ("preflight",),
        "akmods": ("build-zfs-akmods",),
        "image composition": ("build-candidate-image",),
        "signing": ("sign-akmods-cache",),
        "promotion": ("promote-stable",),
    }

    # The values step 2 tells a responder to record, mapped to the key each one has in the
    # `inputs` block of the build-inputs artifact. The cache image digest is deliberately not
    # here: it is not in that manifest, and `test_the_cache_digest_comes_from_the_log_line_it
    # _names` joins it to the line that does print it.
    RECORDED_VALUES: ClassVar[dict[str, str]] = {
        "base-image digest": "base_image_digest",
        "Fedora version": "fedora_version",
        "kernel release": "kernel_release",
        "OpenZFS version": "zfs_version",
        "akmods SHA": "akmods_upstream_ref",
    }

    MANIFEST_ENV: ClassVar[dict[str, str]] = {
        "GITHUB_REPOSITORY": "Danathar/zfs-kinoite-complex",
        "GITHUB_WORKFLOW": "Build And Promote Main Image",
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_NUMBER": "1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "d" * 40,
        "GITHUB_ACTOR": "someone",
        "USE_INPUT_LOCK": "false",
        "LOCK_FILE_PATH": "ci/inputs.lock.json",
        "FEDORA_VERSION": "44",
        "KERNEL_RELEASE": "7.1.4-204.fc44.x86_64",
        "DETECTED_KERNEL_RELEASES": "7.1.4-204.fc44.x86_64",
        "BASE_IMAGE_REF": "quay.io/fedora-ostree-desktops/kinoite:44",
        "BASE_IMAGE_NAME": "quay.io/fedora-ostree-desktops/kinoite",
        "BASE_IMAGE_TAG": "44",
        "BASE_IMAGE_PINNED": "quay.io/fedora-ostree-desktops/kinoite@sha256:" + "0" * 64,
        "BASE_IMAGE_DIGEST": "sha256:" + "0" * 64,
        "BUILD_CONTAINER_REF": "ghcr.io/ublue-os/devcontainer",
        "BUILD_CONTAINER_PINNED": "ghcr.io/ublue-os/devcontainer@sha256:" + "1" * 64,
        "BUILD_CONTAINER_DIGEST": "sha256:" + "1" * 64,
        "BREW_IMAGE_REF": "ghcr.io/ublue-os/brew",
        "BREW_IMAGE_PINNED": "ghcr.io/ublue-os/brew@sha256:" + "2" * 64,
        "BREW_IMAGE_DIGEST": "sha256:" + "2" * 64,
        "ZFS_MINOR_VERSION": "2.4",
        "ZFS_VERSION": "2.4.4",
        "AKMODS_UPSTREAM_REF": "e" * 40,
    }

    def setUp(self) -> None:
        self.items = _numbered_items(_section(DOC_TEXT, "## First triage"))
        self.assertTrue(self.items, "the First triage section has no ordered list")

    def _phrases(self, text: str) -> list[str]:
        """Split one comma-and-`or`/`and` list into its phrases, dropping a leading article."""

        parts = re.split(r",\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+", text)
        return [re.sub(r"^(?:the|a)\s+", "", part.strip()) for part in parts if part.strip()]

    def test_the_section_still_has_four_steps(self) -> None:
        self.assertEqual(len(self.items), 4)

    def test_the_failure_areas_are_exactly_the_jobs_the_build_workflow_runs(self) -> None:
        segment = self.items[0].split("failure is in", 1)
        self.assertEqual(len(segment), 2, f"step 1 no longer names failure areas: {self.items[0]!r}")
        self.assertEqual(sorted(self._phrases(segment[1])), sorted(self.AREA_JOBS))

        attributed = [job for jobs in self.AREA_JOBS.values() for job in jobs]
        self.assertEqual(sorted(attributed), sorted(BUILD_JOBS))

    def test_every_value_step_two_names_is_in_the_record_it_points_at(self) -> None:
        segment = self.items[1].split("record", 1)[1].split(" from ", 1)[0]
        self.assertEqual(sorted(self._phrases(segment)), sorted(self.RECORDED_VALUES))

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, self.MANIFEST_ENV, clear=True),
        ):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    write_build_inputs_manifest.main()
                document = json.loads((Path(tmp) / "artifacts" / "build-inputs.json").read_text())
            finally:
                os.chdir(cwd)

        for phrase, key in self.RECORDED_VALUES.items():
            with self.subTest(value=phrase):
                self.assertIn(key, document["inputs"])

    def test_the_artifact_step_two_names_is_the_artifact_the_run_uploads(self) -> None:
        # The manifest is only reachable to a responder because `prepare-main-akmods` uploads
        # it under a run-scoped name. The page has to name that artifact, not a summary.
        self.assertIn("build-inputs-<run_id>", self.items[1])
        action = PREPARE_ACTION.read_text(encoding="utf-8")
        self.assertIn("name: build-inputs-${{ github.run_id }}", action)
        self.assertIn("path: artifacts/build-inputs.json", action)
        self.assertIn("write-build-inputs-manifest", action)

    def test_the_cache_digest_comes_from_the_log_line_it_names(self) -> None:
        # The sixth value is not in the manifest. It is printed by the cache pinning step, so
        # the page quotes that line -- and this executes the step to prove it still prints it.
        self.assertIn("cache image digest", self.items[1])
        quoted = [literal for literal in BACKTICKED_RE.findall(self.items[1]) if " " in literal]
        self.assertTrue(quoted, "step 2 quotes no log line for the cache image digest")

        env = {
            "GITHUB_REPOSITORY_OWNER": "Danathar",
            "FEDORA_VERSION": "44",
            "AKMODS_REPO": DEFAULTS["AKMODS_REPO"],
        }
        digest = "sha256:" + "3" * 64
        with tempfile.TemporaryDirectory() as tmp:
            env["GITHUB_OUTPUT"] = str(Path(tmp) / "outputs")
            Path(env["GITHUB_OUTPUT"]).write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(pin_akmods_cache, "skopeo_inspect_digest", return_value=digest),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    pin_akmods_cache.main()
        printed = buffer.getvalue()

        for literal in quoted:
            with self.subTest(literal=literal):
                self.assertIn(literal.rstrip(":"), printed)
        self.assertIn(digest, printed)

    def test_step_two_does_not_send_a_responder_to_a_step_summary(self) -> None:
        # `GITHUB_STEP_SUMMARY` is written by nightly-compliance.yml, ai-fix.yml and the akmods
        # failure classifier. Only the last is on the build path, it renders four fields, and
        # it exists only for a classified akmods failure -- one of the six rows tabulated
        # below. Pointing a responder there for six values was the drift this test file was
        # written for; this keeps it from coming back.
        self.assertNotIn("workflow summary", DOC_TEXT)


class FailureTableTests(unittest.TestCase):
    """The six-row table, each first action recomputed against the code that decides it."""

    EXPECTED_FAILURES = (
        "No matching `kmod-zfs` package",
        "Final DNF transaction fails",
        "Cache verification fails",
        "Container build fails before steps run",
        "Signing fails",
        "Promotion fails",
    )

    def setUp(self) -> None:
        self.rows = dict(_table_rows(_section(DOC_TEXT, "## Common failure classes")))
        self.assertTrue(self.rows, "the failure table parsed to nothing")

    def test_the_table_lists_exactly_the_failure_classes_this_module_checks(self) -> None:
        self.assertEqual(sorted(self.rows), sorted(self.EXPECTED_FAILURES))

    def test_the_missing_kmod_row_is_the_installers_fail_closed_path(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            INSTALLER.build_install_plan(
                ["7.1.4-204.fc44.x86_64"],
                [Path("kmod-zfs-7.0.1-100.fc44.x86_64-2.4.4-1.fc44.x86_64.rpm")],
                rpm_name_lookup=lambda path: "kmod-zfs",
                kernel_release_lookup=lambda path: "7.0.1-100.fc44.x86_64",
            )
        message = str(raised.exception)
        self.assertIn("kmod-zfs", message)

        # The same sentence is what turns a red build into the sticky upstream-compat issue
        # and the README badge, which is why the row tells a responder to look upstream first.
        self.assertEqual(
            classify_log_text(message, kernel_release="7.1.4-204.fc44.x86_64")[0],
            FAILURE_KIND_UPSTREAM_COMPAT,
        )
        self.assertIn("Danathar/akmods", self.rows["No matching `kmod-zfs` package"])
        self.assertIn("Danathar/akmods", DEFAULTS["AKMODS_UPSTREAM_REPO"])

    def test_the_dnf_row_names_the_two_lines_that_transaction_installs(self) -> None:
        action = self.rows["Final DNF transaction fails"]
        self.assertIn("userspace", action)
        self.assertIn("OpenZFS", action)

        plan = INSTALLER.build_install_plan(
            ["7.1.4-204.fc44.x86_64"],
            [Path("zfs-2.4.4.rpm"), Path("kmod-zfs-7.1.4.rpm")],
            rpm_name_lookup=lambda path: "kmod-zfs" if path.name.startswith("kmod") else "zfs",
            kernel_release_lookup=lambda path: "7.1.4-204.fc44.x86_64",
        )
        # The transaction is exactly the two lines the row tells a responder to compare: the
        # userspace RPMs that came with the cache, plus the one kmod for the supported kernel.
        self.assertEqual([path.name for path in plan.managed_rpms], ["zfs-2.4.4.rpm"])
        self.assertEqual(plan.supported_kmod_rpm.name, "kmod-zfs-7.1.4.rpm")

        calls: list[list[str]] = []
        with patch.object(
            INSTALLER, "_run_cmd", side_effect=lambda args, **kwargs: calls.append(list(args))
        ):
            INSTALLER.dnf5_install([*plan.managed_rpms, plan.supported_kmod_rpm])
        self.assertEqual(calls[0][:3], ["dnf5", "install", "-y"])
        self.assertEqual(calls[0][3:], ["zfs-2.4.4.rpm", "kmod-zfs-7.1.4.rpm"])

        # "the configured OpenZFS line" is a checked-in value, not a free choice at run time.
        self.assertTrue(DEFAULTS["DEFAULT_ZFS_MINOR_VERSION"])

    def test_the_cache_row_names_the_branch_the_cache_tag_is_built_from(self) -> None:
        action = self.rows["Cache verification fails"]
        self.assertIn("`main`", action)

        tag = pin_akmods_cache.akmods_cache_image_tag(
            image_org="danathar", source_repo=DEFAULTS["AKMODS_REPO"], fedora_version="44"
        )
        self.assertTrue(tag.endswith(":main-44"), tag)

        # The check side builds the same tag independently, so a rebuild "on `main`" is only
        # what a later run looks for while both agree.
        with patch.dict(os.environ, {}, clear=True), patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional", return_value=None
        ):
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo=DEFAULTS["AKMODS_REPO"],
                fedora_version="44",
                kernel_release="7.1.4-204.fc44.x86_64",
                zfs_version="2.4.4",
            )
        self.assertEqual(status.source_image, tag)

    def test_the_cache_row_refuses_to_bypass_the_signature_check(self) -> None:
        self.assertIn("do not bypass signature", self.rows["Cache verification fails"].lower())
        matching_but_unsigned = AkmodsCacheStatus(
            source_image="ghcr.io/danathar/x:main-44",
            image_exists=True,
            source_image_pinned="ghcr.io/danathar/x@sha256:" + "4" * 64,
            signature_verified=False,
        )
        self.assertTrue(matching_but_unsigned.content_matches)
        self.assertFalse(matching_but_unsigned.reusable)

    def test_the_container_row_points_at_a_job_level_digest_pin(self) -> None:
        action = self.rows["Container build fails before steps run"]
        self.assertIn("job-level pinned build-container image", action)

        body = "\n".join(BUILD_JOBS["build-zfs-akmods"])
        self.assertIn("\n    container:\n", f"\n{body}\n")
        images = [
            line.split("image:", 1)[1].strip()
            for line in BUILD_JOBS["build-zfs-akmods"]
            if line.startswith("      image:")
        ]
        self.assertEqual(len(images), 1, "build-zfs-akmods no longer declares one container image")
        self.assertIn("@sha256:", images[0])
        self.assertEqual(images[0], DEFAULTS["DEFAULT_BUILD_CONTAINER_IMAGE"])

    def test_the_signing_row_names_the_four_things_that_gate_signing(self) -> None:
        action = self.rows["Signing fails"]
        self.assertIn("`SIGNING_SECRET`", action)
        self.assertIn("environment", action)
        self.assertIn("registry login", action)
        self.assertIn("public key", action)

        signing_jobs = [
            job for job, lines in BUILD_JOBS.items() if "secrets.SIGNING_SECRET" in "\n".join(lines)
        ]
        self.assertTrue(signing_jobs, "no job in build.yml consumes secrets.SIGNING_SECRET")
        for job in signing_jobs:
            with self.subTest(job=job):
                body = "\n".join(BUILD_JOBS[job])
                self.assertIn("    environment:", body)
                # Two shapes of "registry login" are in use: a `docker/login-action` step for
                # the jobs whose helper reads the ambient Docker config, and credentials
                # threaded into the publishing action for the one that pushes through it.
                # Either is a login; neither being present is the failure the row describes.
                self.assertTrue(
                    "docker/login-action@" in body or "registry_password:" in body,
                    f"{job} consumes SIGNING_SECRET but authenticates to no registry",
                )

        self.assertTrue(_tracked(COSIGN_PUBLIC_KEY), "cosign.pub is not committed")

    def test_the_promotion_row_verifies_the_committed_key_before_any_tag_moves(self) -> None:
        action = self.rows["Promotion fails"]
        self.assertIn("`latest`", action)
        self.assertIn("signature", action)

        calls: list[list[str]] = []
        with patch.dict(os.environ, {}, clear=True), patch.object(
            promote_stable, "run_cmd", side_effect=lambda args, **kwargs: calls.append(list(args))
        ):
            promote_stable.verify_candidate_signature(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                candidate_digest="sha256:" + "5" * 64,
            )
        self.assertEqual(calls[0][:2], ["cosign", "verify"])
        self.assertIn(str(COSIGN_PUBLIC_KEY), calls[0])


class PromotionOrderTests(unittest.TestCase):
    """`Keep latest unchanged` and `verify ... before retrying`, executed against the real main."""

    ENV: ClassVar[dict[str, str]] = {
        "GITHUB_REPOSITORY_OWNER": "Danathar",
        "REGISTRY_ACTOR": "actor",
        "REGISTRY_TOKEN": "token",
        "FEDORA_VERSION": "44",
        "IMAGE_NAME": "zfs-kinoite-complex",
        "GITHUB_RUN_NUMBER": "7",
        "GITHUB_SHA": "f" * 40,
    }
    DIGEST = "sha256:" + "6" * 64

    def _run_promotion(self, *, verify_fails: bool) -> list[tuple[str, str]]:
        copies: list[tuple[str, str]] = []

        def fake_verify(**kwargs: object) -> None:
            copies.append(("verify", str(kwargs["candidate_digest"])))
            if verify_fails:
                raise CiToolError("signature verification failed")

        def fake_copy(source: str, destination: str, **kwargs: object) -> None:
            copies.append(("copy", destination))

        with patch.dict(os.environ, self.ENV, clear=True), patch.object(
            promote_stable, "verify_candidate_signature", side_effect=fake_verify
        ), patch.object(promote_stable, "skopeo_copy", side_effect=fake_copy), patch.object(
            promote_stable, "skopeo_inspect_digest", return_value=self.DIGEST
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                if verify_fails:
                    with self.assertRaises(CiToolError):
                        promote_stable.main()
                else:
                    promote_stable.main()
        return copies

    def test_the_signature_is_verified_before_any_tag_moves(self) -> None:
        steps = self._run_promotion(verify_fails=False)
        self.assertEqual(steps[0], ("verify", self.DIGEST))
        destinations = [destination for kind, destination in steps if kind == "copy"]
        self.assertEqual(len(destinations), 2)
        self.assertIn(":stable-7-", destinations[0])
        self.assertTrue(destinations[1].endswith(":latest"), destinations[1])

    def test_latest_is_untouched_when_verification_fails(self) -> None:
        steps = self._run_promotion(verify_fails=True)
        self.assertEqual([kind for kind, _ in steps], ["verify"])


class ResponseRuleTests(unittest.TestCase):
    """The freeze instruction and the fail-closed rule, against the cascade and the workflow."""

    def setUp(self) -> None:
        self.section = "\n".join(_section(DOC_TEXT, "## Response rules"))

    def test_the_freeze_key_the_section_names_exists_and_is_clear(self) -> None:
        self.assertIn("`AKMODS_UPSTREAM_REF`", self.section)
        self.assertIn("`ci/defaults.json`", self.section)
        self.assertIn("AKMODS_UPSTREAM_REF", DEFAULTS)
        # An emergency freeze is meant to be temporary, and the sibling page's step 6 says to
        # clear it. A pin left in the committed tree would mean the floating ref is not what
        # `main` actually builds from -- which is the state this page tells you to leave.
        self.assertEqual(DEFAULTS["AKMODS_UPSTREAM_REF"], "")

    def test_setting_the_freeze_key_wins_the_cascade(self) -> None:
        pinned = "a" * 40
        defaults = dict(DEFAULTS, AKMODS_UPSTREAM_REF=pinned)
        with patch.dict(os.environ, {}, clear=True), patch(
            "ci_tools.resolve_build_inputs.load_repo_defaults", return_value=defaults
        ), patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote:
            self.assertEqual(_resolve_default_akmods_ref(), pinned)
        ls_remote.assert_not_called()

    def test_clearing_it_returns_to_the_floating_selection_the_section_names(self) -> None:
        self.assertIn("floating", self.section)
        floated = "b" * 40
        with patch.dict(os.environ, {}, clear=True), patch(
            "ci_tools.resolve_build_inputs.load_repo_defaults", return_value=dict(DEFAULTS)
        ), patch(
            "ci_tools.resolve_build_inputs.git_ls_remote_resolve", return_value=floated
        ) as ls_remote:
            self.assertEqual(_resolve_default_akmods_ref(), floated)
        ls_remote.assert_called_once_with(
            DEFAULTS["AKMODS_UPSTREAM_REPO"], DEFAULTS["AKMODS_UPSTREAM_TRACK"]
        )

    def test_the_section_defers_to_the_page_that_documents_the_pin(self) -> None:
        self.assertIn("akmods-fork-maintenance.md", self.section)
        self.assertIn("AKMODS_UPSTREAM_REF", SIBLING_DOC.read_text(encoding="utf-8"))

    def test_the_fail_closed_rule_is_what_the_signing_jobs_actually_do(self) -> None:
        self.assertIn("Do not weaken fail-closed checks", self.section)
        guards = [
            job
            for job, lines in BUILD_JOBS.items()
            if "if: env.HAS_SIGNING_SECRET != 'true'" in "\n".join(lines)
        ]
        self.assertTrue(guards, "no job refuses to continue without SIGNING_SECRET")
        for job in guards:
            with self.subTest(job=job):
                body = "\n".join(BUILD_JOBS[job])
                self.assertIn("Refusing to", body)
                self.assertIn("exit 1", body)


class AfterRecoveryTests(unittest.TestCase):
    """The closing rule: a changed invariant gets a test, in the suite that runs on every PR."""

    def test_the_unit_test_the_section_promises_runs_on_every_pull_request(self) -> None:
        # The section is hard-wrapped, so "a unit\ntest" has to be flattened before it can be
        # matched at all -- a whole-file `assertIn` would simply never fire.
        section = " ".join("\n".join(_section(DOC_TEXT, "## After recovery")).split())
        self.assertIn("unit test", section)
        workflow = TEST_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("pytest", workflow)


if __name__ == "__main__":
    unittest.main()
