"""
Script: tests/test_upstream_change_response_doc.py
What: Joins docs/upstream-change-response.md to the workflow, helpers and defaults file whose
behaviour it restates by hand.
Doing: Extracts the document's own claims -- the promotion gate, the five failure areas first
triage names, the six values it says to record, the six rows of its failure table and its
emergency-freeze rule -- and recomputes each one against the tree, executing the real
promotion helper, the real install planner, the real cache-status property and the real ref
cascade rather than grepping for the strings.
Why: This is the page a maintainer opens while a production build is red -- README.md,
docs/documentation-guide.md, docs/reflections/README.md and docs/akmods-fork-maintenance.md all
send a reader here. Every operational pointer on it is a hand copy of a job name, a job
condition, a workflow artifact, a secret name, a defaults key or a tag literal, and no test at
any tier opened the file: `grep -rl upstream-change-response tests/` returned nothing. The
repository-wide check in tests/test_docs_consistency.py resolves its links and confirms it is
in the documentation map, which leaves every claim that is not a link unchecked, and the
coverage flags in .github/workflows/test.yml name four Python paths and no Markdown, so no
tier could notice. One pointer had already drifted: step 2 sent a responder to "the workflow
summary" for six values, and no summary in the build path carries them.
Goal: Make the document fail here when the machine moves under it, in both directions.

Parses Markdown and YAML by hand. CI installs pytest, pytest-cov and ruff and nothing else
(see .github/workflows/test.yml), so a PyYAML import here would skip in exactly the place
these assertions are meant to run. `_section`, `_headings`, `_numbered_items`,
`_trailing_prose`, `_prose`, `_table`, `_job_names`, `_job_field` and `_job_block` are the
whole parser and carry their own case table in `ParserTests` below, because a hand-rolled parser that is never wrong about a
fixture is the only thing keeping the assertions built on it honest.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_tools.check_akmods_cache import AkmodsCacheStatus, inspect_akmods_cache
from ci_tools.common import CiToolError, load_repo_defaults
from ci_tools.pin_akmods_cache import akmods_cache_image_tag
from ci_tools.promote_stable import main as promote_stable_main
from ci_tools.resolve_build_inputs import _resolve_default_akmods_ref

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "upstream-change-response.md"
DEFAULTS_FILE = REPO_ROOT / "ci" / "defaults.json"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
PREPARE_ACTION = REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
PIN_CACHE_MODULE = REPO_ROOT / "ci_tools" / "pin_akmods_cache.py"
CHECK_CACHE_MODULE = REPO_ROOT / "ci_tools" / "check_akmods_cache.py"
COSIGN_PUB = REPO_ROOT / "cosign.pub"
TEST_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"

# The pages a responder actually starts from. The runbook is only reachable because these link
# it; a rename that left them behind would strand the page this module exists to keep honest.
ENTRY_POINTS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "documentation-guide.md",
    REPO_ROOT / "docs" / "reflections" / "README.md",
    REPO_ROOT / "docs" / "akmods-fork-maintenance.md",
)

DOC_TEXT = DOC.read_text(encoding="utf-8")
BUILD_TEXT = BUILD_WORKFLOW.read_text(encoding="utf-8")

# Every heading the assertions below scope themselves to. Asserted exhaustive in
# SectionInventoryTests, so a new section cannot arrive unread: a page that grows an
# unasserted claim is how this file stops meaning anything.
DOC_HEADINGS = (
    "# Upstream Change Response",
    "## First triage",
    "## Common failure classes",
    "## Response rules",
    "## After recovery",
)

# The five areas first triage tells a responder to place the failure in, against the jobs in
# build.yml that can actually be the failed one. Asserted exhaustive in both directions: an
# area naming no job sends a responder looking for something that does not exist, and a job no
# area covers is a failure class the page cannot triage.
AREA_TO_JOB = {
    "input resolution": "preflight",
    "akmods": "build-zfs-akmods",
    "image composition": "build-candidate-image",
    "signing": "sign-akmods-cache",
    "promotion": "promote-stable",
}

# The six values first triage step 2 says to record, mapped to the `inputs` key of
# `write_build_inputs_manifest` that holds each one. The cache image digest maps to None
# because the manifest does not carry it -- that absence is the drift this file exists to
# pin, and step 2 now names the job log for it instead.
VALUE_TO_MANIFEST_KEY = {
    "base-image digest": "base_image_digest",
    "Fedora version": "fedora_version",
    "kernel release": "kernel_release",
    "OpenZFS version": "zfs_version",
    "akmods SHA": "akmods_upstream_ref",
    "cache image digest": None,
}

# The failure table's first column, in document order. Kept as an explicit tuple so a row
# added, removed or reworded fails the exhaustiveness assertion first, before any of the
# per-row joins below silently stop matching anything.
FAILURE_ROWS = (
    "No matching `kmod-zfs` package",
    "Final DNF transaction fails",
    "Cache verification fails",
    "Container build fails before steps run",
    "Signing fails",
    "Promotion fails",
)

PROMOTE_ENV = {
    "GITHUB_REPOSITORY_OWNER": "Danathar",
    "REGISTRY_ACTOR": "actor",
    "REGISTRY_TOKEN": "token",
    "FEDORA_VERSION": "44",
    "IMAGE_NAME": "zfs-kinoite-complex",
    "GITHUB_RUN_NUMBER": "12",
    "GITHUB_SHA": "deadbeefcafefeed",
}

# Every environment variable the ref cascade reads. Wiped before each execution so a value
# inherited from the surrounding process cannot decide the result.
CASCADE_ENV = (
    "DEFAULT_AKMODS_REF",
    "AKMODS_UPSTREAM_REF",
    "AKMODS_UPSTREAM_TRACK",
    "AKMODS_UPSTREAM_REPO",
)

PIN_SHA = "b" * 40
TRACK_SHA = "c" * 40


def _section(text: str, heading: str) -> list[str]:
    """
    Return the lines under `heading`, up to the next heading of the same or higher level.

    `heading` is the full heading line including its `#` markers, so asking for `## First
    triage` can never accidentally match `### First triage`. Raises when the heading is absent
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


def _headings(text: str) -> list[str]:
    """Every ATX heading line in `text`, in document order, markers included."""

    return [line for line in text.splitlines() if re.match(r"^#{1,6} \S", line)]


def _numbered_items(lines: list[str]) -> list[str]:
    """
    Return the first top-level ordered list in `lines`, each item flattened to one string.

    A line that is neither blank nor a new `N. ` item continues the current item, because the
    document hard-wraps its longer steps -- step 2 is five lines. A blank line ends the list so
    a second list in the same section cannot be merged into the first. Numbering must run 1..n:
    a document that skips or repeats a number is a parse this file refuses rather than guesses
    at.
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
                raise AssertionError(f"ordered list is not sequential at item {match.group(1)!r}")
            items.append(match.group(2))
            open_item = True
        elif open_item:
            items[-1] = f"{items[-1]} {stripped}"
    return items


def _trailing_prose(lines: list[str]) -> str:
    """
    Return the first paragraph after the first ordered list in `lines`, collapsed to one string.

    First triage carries its early-failure path as a paragraph under the list rather than as a
    sixth numbered step, because it is not a step: it is what to do when step 2's artifact does
    not exist. Item continuation lines are indented, so an unindented line after an item starts
    the paragraph, and the paragraph ends at the first blank line or heading after it -- a
    later paragraph or subsection must not be folded into the one being asserted. Raises when
    there is no such paragraph, rather than letting the assertions below pass against "".
    """

    seen_item = False
    tail: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\d+\.\s+", stripped) and not line.startswith("   "):
            seen_item = True
            tail = []
            continue
        if not seen_item or line.startswith("   "):
            continue
        if not stripped or stripped.startswith("#"):
            if tail:
                break
            continue
        tail.append(stripped)

    if not tail:
        raise AssertionError("no prose after the ordered list")
    return re.sub(r"\s+", " ", " ".join(tail)).strip()


def _prose(lines: list[str]) -> str:
    """
    Return `lines` as one whitespace-collapsed string.

    The document hard-wraps at 79 columns, so a phrase it states in prose is routinely split
    across a newline: "a unit\\ntest", "normal floating or explicit-ref\\nselection". Searching
    the raw section text for either would fail on the wrap rather than on the claim, which is
    the difference between a test that pins a sentence and a test that pins a line break.
    """

    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _table(lines: list[str]) -> list[list[str]]:
    """
    Return the first pipe table in `lines` as rows of stripped cells, header included.

    The `|---|---|` separator is dropped, and a row whose cell count differs from the header's
    is refused: a table this file reads by column index must not be read off a row that has
    lost or gained a column. Raises when there is no table at all, so a section that stops
    carrying one fails here rather than handing the row joins an empty list.
    """

    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= set("-: ") for cell in cells):
            continue
        if rows and len(cells) != len(rows[0]):
            raise AssertionError(f"table row {cells!r} does not match the header's column count")
        rows.append(cells)

    if len(rows) < 2:
        raise AssertionError("no pipe table with at least one data row")
    return rows


def _job_names(workflow_text: str) -> list[str]:
    """
    Return the top-level job ids under `jobs:`, in file order.

    Indentation-bound at exactly two spaces, which is what a job key is in this workflow, so a
    `needs:`/`if:` key nested deeper cannot be mistaken for a job and a job cannot be missed
    because it sits after a long block.
    """

    lines = workflow_text.splitlines()
    try:
        start = lines.index("jobs:")
    except ValueError as exc:
        raise AssertionError("build.yml has no top-level `jobs:` key") from exc

    names: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent != 2:
            continue
        match = re.match(r"^ {2}([A-Za-z0-9_-]+):\s*$", line)
        if match:
            names.append(match.group(1))
    return names


def _job_field(workflow_text: str, job: str, field: str) -> str:
    """
    Return one scalar field of `job`, with a folded block (`>-`) joined into a single line.

    Comment lines are dropped: `promote-stable`'s `if:` sits under twelve lines of comment
    explaining it, and a comment that happens to contain `needs.` must not be read as part of
    the expression. Raises when the job or the field is missing, so a rename fails here.
    """

    lines = workflow_text.splitlines()
    try:
        start = lines.index(f"  {job}:")
    except ValueError as exc:
        raise AssertionError(f"build.yml has no job {job!r}") from exc

    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and len(line) - len(line.lstrip()) <= 2:
            break
        match = re.match(rf"^ {{4}}{re.escape(field)}:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value not in (">-", ">", "|", "|-"):
            return value
        folded: list[str] = []
        for continuation in lines[index + 1 :]:
            if not continuation.strip():
                continue
            if len(continuation) - len(continuation.lstrip()) <= 4:
                break
            if continuation.lstrip().startswith("#"):
                continue
            folded.append(continuation.strip())
        return " ".join(folded)
    raise AssertionError(f"job {job!r} has no `{field}:` field")


def _job_block(workflow_text: str, job: str) -> str:
    """Return every line of one job, as text, for the claims that are about a whole job."""

    lines = workflow_text.splitlines()
    try:
        start = lines.index(f"  {job}:")
    except ValueError as exc:
        raise AssertionError(f"build.yml has no job {job!r}") from exc

    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= 2:
            break
        body.append(line)
    return "\n".join(body)


def _wiped_env(**overrides: str) -> dict[str, str]:
    env = {name: "" for name in CASCADE_ENV}
    env.update(overrides)
    return env


def _install_helper():
    """Load the akmods cache installer, which is a script path rather than a package module."""

    helper_path = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
    spec = importlib.util.spec_from_file_location("install_zfs_from_akmods_cache", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParserTests(unittest.TestCase):
    """The parsers above, against fixtures whose right answer is written out by hand."""

    FIXTURE = textwrap.dedent(
        """\
        # Title

        ## Alpha

        1. first item
        2. second item that
           wraps onto another line

        prose after the list

        ### Alpha Nested

        nested body

        ## Beta

        | Failure | First action |
        |---|---|
        | one | do this |
        | two | do that |

        trailing prose
        """
    )

    WORKFLOW = textwrap.dedent(
        """\
        name: Build
        jobs:
          first:
            name: First
            needs: nothing
            if: github.event_name != 'schedule'
            container:
              image: ghcr.io/example/devcontainer@sha256:abc
          second:
            name: Second
            # a comment mentioning needs.first.result that is not the expression
            if: >-
              !cancelled()
              && needs.first.result == 'success'
            runs-on: ubuntu-24.04
        """
    )

    def test_section_stops_at_next_same_level_heading(self) -> None:
        body = "\n".join(_section(self.FIXTURE, "## Alpha"))
        self.assertIn("nested body", body)
        self.assertNotIn("trailing prose", body)

    def test_section_of_deeper_heading_excludes_its_parent(self) -> None:
        body = "\n".join(_section(self.FIXTURE, "### Alpha Nested"))
        self.assertEqual(body.strip(), "nested body")

    def test_section_rejects_missing_heading(self) -> None:
        with self.assertRaises(AssertionError):
            _section(self.FIXTURE, "## Gamma")

    def test_section_rejects_empty_section(self) -> None:
        with self.assertRaises(AssertionError):
            _section("## Alpha\n\n## Beta\n\nbody\n", "## Alpha")

    def test_headings_keep_their_markers_and_order(self) -> None:
        self.assertEqual(
            _headings(self.FIXTURE),
            ["# Title", "## Alpha", "### Alpha Nested", "## Beta"],
        )

    def test_headings_ignore_a_table_separator_and_a_bare_hash(self) -> None:
        self.assertEqual(_headings("|---|\n#\n#no space\n## Real\n"), ["## Real"])

    def test_numbered_items_join_wrapped_lines_and_stop_at_blank(self) -> None:
        self.assertEqual(
            _numbered_items(_section(self.FIXTURE, "## Alpha")),
            ["first item", "second item that wraps onto another line"],
        )

    def test_numbered_items_do_not_merge_a_second_list_in_the_same_section(self) -> None:
        lines = ["1. one", "2. two", "", "prose between the lists", "", "1. other list"]
        self.assertEqual(_numbered_items(lines), ["one", "two"])

    def test_numbered_items_reject_a_gap_in_the_numbering(self) -> None:
        with self.assertRaises(AssertionError):
            _numbered_items(["1. one", "3. three"])

    def test_trailing_prose_returns_the_paragraph_after_the_list(self) -> None:
        self.assertEqual(
            _trailing_prose(_section(self.FIXTURE, "## Alpha")),
            "prose after the list",
        )

    def test_trailing_prose_stops_at_the_paragraph_after_it(self) -> None:
        lines = ["1. one", "wrapped", "paragraph", "", "a second paragraph", "## Next"]
        self.assertEqual(_trailing_prose(lines), "wrapped paragraph")

    def test_trailing_prose_ignores_a_wrapped_item_and_rejects_a_bare_list(self) -> None:
        # The wrapped continuation of item 2 is indented; a list with nothing after it has no
        # paragraph at all, and must raise rather than return "".
        with self.assertRaises(AssertionError):
            _trailing_prose(["1. one", "2. two that", "   wraps", ""])

    def test_prose_joins_a_hard_wrapped_phrase(self) -> None:
        self.assertEqual(
            _prose(["add or update a unit", "test and document the new invariant"]),
            "add or update a unit test and document the new invariant",
        )

    def test_prose_collapses_blank_lines_between_paragraphs(self) -> None:
        self.assertEqual(
            _prose(["first rule.", "", "  second   rule.  ", ""]),
            "first rule. second rule.",
        )

    def test_table_drops_the_separator_and_keeps_the_header(self) -> None:
        self.assertEqual(
            _table(_section(self.FIXTURE, "## Beta")),
            [["Failure", "First action"], ["one", "do this"], ["two", "do that"]],
        )

    def test_table_stops_at_the_first_line_after_it(self) -> None:
        rows = _table(["| a | b |", "|---|---|", "| 1 | 2 |", "", "| x | y |"])
        self.assertEqual(rows, [["a", "b"], ["1", "2"]])

    def test_table_rejects_a_row_with_the_wrong_column_count(self) -> None:
        with self.assertRaises(AssertionError):
            _table(["| a | b |", "|---|---|", "| only-one |"])

    def test_table_rejects_a_section_with_no_table(self) -> None:
        with self.assertRaises(AssertionError):
            _table(["just prose", ""])

    def test_job_names_reads_only_two_space_keys(self) -> None:
        self.assertEqual(_job_names(self.WORKFLOW), ["first", "second"])

    def test_job_names_rejects_a_workflow_with_no_jobs_key(self) -> None:
        with self.assertRaises(AssertionError):
            _job_names("name: Build\non: push\n")

    def test_job_field_reads_a_plain_scalar_from_the_right_job(self) -> None:
        self.assertEqual(_job_field(self.WORKFLOW, "first", "needs"), "nothing")

    def test_job_field_folds_a_block_and_drops_its_comments(self) -> None:
        self.assertEqual(
            _job_field(self.WORKFLOW, "second", "if"),
            "!cancelled() && needs.first.result == 'success'",
        )

    def test_job_field_rejects_a_missing_job_or_field(self) -> None:
        with self.assertRaises(AssertionError):
            _job_field(self.WORKFLOW, "third", "if")
        with self.assertRaises(AssertionError):
            _job_field(self.WORKFLOW, "first", "environment")

    def test_job_block_stops_at_the_next_job(self) -> None:
        block = _job_block(self.WORKFLOW, "first")
        self.assertIn("ghcr.io/example/devcontainer@sha256:abc", block)
        self.assertNotIn("ubuntu-24.04", block)


class SectionInventoryTests(unittest.TestCase):
    """
    Every section this module asserts against exists, and the page has no others.

    The exhaustive direction is the one that matters: a section added later would carry claims
    nothing here reads, and the file would keep passing while covering less of the page.
    """

    def test_the_document_is_tracked_and_still_carries_its_title(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(DOC.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        )
        self.assertEqual(tracked.returncode, 0, f"{DOC.name} is not tracked by git")
        self.assertEqual(DOC_TEXT.splitlines()[0], "# Upstream Change Response")

    def test_the_runbook_is_still_pointed_at_from_every_entry_point(self) -> None:
        # The page is only useful if the places a responder starts from still lead here.
        for path in ENTRY_POINTS:
            with self.subTest(path=path.name):
                self.assertIn(
                    "upstream-change-response.md",
                    path.read_text(encoding="utf-8"),
                    f"{path.relative_to(REPO_ROOT)} no longer points at the runbook",
                )

    def test_every_asserted_section_exists(self) -> None:
        for heading in DOC_HEADINGS:
            with self.subTest(heading=heading):
                self.assertTrue(any(line.strip() for line in _section(DOC_TEXT, heading)))

    def test_the_document_has_no_section_this_module_does_not_read(self) -> None:
        self.assertEqual(
            _headings(DOC_TEXT),
            list(DOC_HEADINGS),
            "a heading was added, removed or renamed: extend DOC_HEADINGS and assert its claims",
        )

    def test_relative_links_resolve(self) -> None:
        targets = re.findall(r"\]\((?!https?://)([^)#]+)(?:#[^)]*)?\)", DOC_TEXT)
        self.assertGreaterEqual(len(targets), 1, "the document stopped linking to anything")
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(
                    (DOC.parent / target).resolve().exists(),
                    f"{DOC.name} links to {target}, which does not exist",
                )


class PromotionGateTests(unittest.TestCase):
    """
    "A red build is expected to stop promotion while the last known-good image remains
    available" -- against `promote-stable`'s own `if:`.

    The sentence is the whole premise of the page: if promotion did not stop, a red build
    would already have shipped and triage would be a post-mortem. It is a hand copy of one
    workflow expression.
    """

    def setUp(self) -> None:
        self.intro = _prose(_section(DOC_TEXT, "# Upstream Change Response"))
        self.condition = _job_field(BUILD_TEXT, "promote-stable", "if")

    def test_the_document_still_makes_the_claim(self) -> None:
        self.assertIn("red build", self.intro)
        self.assertIn("stop promotion", self.intro)
        self.assertIn("last known-good image remains available", self.intro)

    def test_the_moving_inputs_the_intro_names_are_the_configured_ones(self) -> None:
        # The premise rests on the page describing this repository. A base image or fork the
        # defaults file no longer points at would make the rest of the page someone else's.
        defaults = load_repo_defaults()
        self.assertIn("Fedora Kinoite", self.intro)
        self.assertIn("kinoite", defaults["DEFAULT_BASE_IMAGE"])
        self.assertIn("`Danathar/akmods`", self.intro)
        self.assertIn("Danathar/akmods", defaults["AKMODS_UPSTREAM_REPO"])

    def test_promotion_requires_both_build_jobs_to_have_succeeded(self) -> None:
        for job in ("build-zfs-akmods", "build-candidate-image"):
            with self.subTest(job=job):
                self.assertIn(f"needs.{job}.result == 'success'", self.condition)

    def test_promotion_does_not_run_on_a_failed_cache_signing(self) -> None:
        # The other half of "a red build stops promotion": a red signing job must not leave
        # `:latest` free to move. `skipped` is the legitimate case -- the cache was reused,
        # not rebuilt -- and it is the only non-success the expression accepts.
        self.assertIn("needs.sign-akmods-cache.result == 'success'", self.condition)
        self.assertIn("needs.sign-akmods-cache.result == 'skipped'", self.condition)
        self.assertNotIn("result == 'failure'", self.condition)

    def test_nothing_in_the_gate_promotes_on_an_unsuccessful_build(self) -> None:
        # `always()` would defeat the sentence outright; `!cancelled()` is what the job
        # actually uses, and it is only safe because of the explicit success checks above.
        self.assertIn("!cancelled()", self.condition)
        self.assertNotIn("always()", self.condition)

    def test_the_last_known_good_image_is_what_latest_still_points_at(self) -> None:
        # "remains available" is a claim about promotion being the only writer of `:latest`.
        # Promotion is the last job in the graph, so nothing after a red build touches it.
        self.assertEqual(_job_names(BUILD_TEXT)[-1], "promote-stable")


class FirstTriageAreaTests(unittest.TestCase):
    """Step 1's five areas against the jobs in build.yml, exhaustively in both directions."""

    def setUp(self) -> None:
        self.items = _numbered_items(_section(DOC_TEXT, "## First triage"))
        self.jobs = _job_names(BUILD_TEXT)

    def test_first_triage_still_has_four_steps(self) -> None:
        self.assertEqual(len(self.items), 4, self.items)

    def _documented_areas(self) -> list[str]:
        match = re.search(r"failure is in (.+)$", self.items[0])
        self.assertIsNotNone(match, "step 1 no longer names the failure areas")
        areas = [part.strip() for part in match.group(1).split(",")]
        return [re.sub(r"^or\s+", "", area) for area in areas if area]

    def test_the_document_names_exactly_the_areas_this_module_maps(self) -> None:
        self.assertEqual(sorted(self._documented_areas()), sorted(AREA_TO_JOB))

    def test_every_documented_area_names_a_job_that_exists(self) -> None:
        for area in self._documented_areas():
            with self.subTest(area=area):
                self.assertIn(AREA_TO_JOB[area], self.jobs)

    def test_every_job_in_the_build_workflow_is_covered_by_an_area(self) -> None:
        # The direction that catches the stale page: a job added to build.yml is a failure
        # class step 1 cannot place, and a responder would have nowhere to put it.
        self.assertEqual(sorted(self.jobs), sorted(AREA_TO_JOB.values()))


class FirstTriageRecordTests(unittest.TestCase):
    """
    Step 2's six values against the thing that records them.

    This is the pointer that was already wrong. Step 2 used to say "from the workflow
    summary", and no summary in the build path carries these values:
    `GITHUB_STEP_SUMMARY` is written in nightly-compliance.yml, ai-fix.yml and
    `classify_akmods_failure.write_step_summary`, and the only one on the build path is the
    akmods classifier -- four fields, none of them the base-image digest, the OpenZFS version
    or the cache digest, and only when an akmods failure was classified at all. The step now
    names the artifact that holds five of the six and the log line that holds the sixth, and
    each of those is joined below.
    """

    def setUp(self) -> None:
        self.step = _numbered_items(_section(DOC_TEXT, "## First triage"))[1]

    def test_the_step_names_every_value_this_module_maps(self) -> None:
        for value in VALUE_TO_MANIFEST_KEY:
            with self.subTest(value=value):
                self.assertIn(value, self.step)

    def test_the_step_no_longer_sends_a_responder_to_a_workflow_summary(self) -> None:
        self.assertNotIn("workflow summary", self.step)

    def test_the_step_names_the_artifact_that_carries_the_manifest(self) -> None:
        self.assertIn("build-inputs-<run_id>", self.step)
        self.assertIn("artifacts/build-inputs.json", self.step)
        self.assertIn("`inputs`", self.step)

    def test_that_artifact_name_is_the_one_the_prepare_action_uploads(self) -> None:
        action = PREPARE_ACTION.read_text(encoding="utf-8")
        self.assertIn("name: build-inputs-${{ github.run_id }}", action)
        self.assertIn("path: artifacts/build-inputs.json", action)
        self.assertIn("write-build-inputs-manifest", action)

    def test_the_manifest_really_records_five_of_the_six_values(self) -> None:
        # Executed, not grepped: the keys are read off a manifest the real writer produced.
        env = {
            "GITHUB_REPOSITORY": "Danathar/zfs-kinoite-complex",
            "GITHUB_WORKFLOW": "build",
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_NUMBER": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "f" * 40,
            "GITHUB_ACTOR": "someone",
            "USE_INPUT_LOCK": "false",
            "LOCK_FILE_PATH": "ci/inputs.lock.json",
            "FEDORA_VERSION": "44",
            "KERNEL_RELEASE": "7.1.4-204.fc44.x86_64",
            "DETECTED_KERNEL_RELEASES": "7.1.4-204.fc44.x86_64",
            "BASE_IMAGE_REF": "quay.io/example/kinoite:44",
            "BASE_IMAGE_NAME": "quay.io/example/kinoite",
            "BASE_IMAGE_TAG": "44",
            "BASE_IMAGE_PINNED": "quay.io/example/kinoite@sha256:" + "0" * 64,
            "BASE_IMAGE_DIGEST": "sha256:" + "0" * 64,
            "BUILD_CONTAINER_REF": "ghcr.io/example/devcontainer@sha256:" + "1" * 64,
            "BUILD_CONTAINER_PINNED": "ghcr.io/example/devcontainer@sha256:" + "1" * 64,
            "BUILD_CONTAINER_DIGEST": "sha256:" + "1" * 64,
            "BREW_IMAGE_REF": "ghcr.io/example/brew@sha256:" + "2" * 64,
            "BREW_IMAGE_PINNED": "ghcr.io/example/brew@sha256:" + "2" * 64,
            "BREW_IMAGE_DIGEST": "sha256:" + "2" * 64,
            "ZFS_MINOR_VERSION": "2.4",
            "ZFS_VERSION": "2.4.3",
            "AKMODS_UPSTREAM_REF": TRACK_SHA,
        }
        # Imported here: the module writes to a path relative to the process working
        # directory, so it must be driven from inside the temporary directory below.
        from ci_tools import write_build_inputs_manifest

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                with patch.dict(os.environ, env, clear=False):
                    write_build_inputs_manifest.main()
                document = json.loads(
                    (Path(temp_dir) / write_build_inputs_manifest.ARTIFACT_PATH).read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                os.chdir(previous)

        recorded = document["inputs"]
        for value, key in VALUE_TO_MANIFEST_KEY.items():
            with self.subTest(value=value):
                if key is None:
                    continue
                self.assertIn(key, recorded, f"the manifest stopped recording {value}")
                self.assertTrue(str(recorded[key]), f"the manifest recorded {value} as empty")

        # The absence the step's second half exists for. If the cache digest ever joins the
        # manifest, step 2 should stop sending a responder to the job log -- and this fails.
        self.assertNotIn(
            "akmods_cache_digest",
            recorded,
            "the manifest now carries the cache digest; step 2 can name the artifact for it",
        )
        for key in recorded:
            with self.subTest(key=key):
                self.assertNotIn("cache", key, "a cache key appeared in the manifest")

    def test_the_cache_digest_log_lines_the_step_names_are_the_ones_printed(self) -> None:
        self.assertIn("Digest-pinned akmods cache image for final build:", self.step)
        self.assertIn("Checked akmods cache digest:", self.step)
        self.assertIn(
            'print(f"Digest-pinned akmods cache image for final build: {image_pinned}")',
            PIN_CACHE_MODULE.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'print(f"Checked akmods cache digest: {status.source_image_pinned}")',
            CHECK_CACHE_MODULE.read_text(encoding="utf-8"),
        )


class FirstTriageEarlyFailureTests(unittest.TestCase):
    """
    The paragraph under the list, against the two runs where step 2's artifact never exists.

    Step 2 sends a responder to `build-inputs-<run_id>`, which the prepare action uploads after
    it has resolved the inputs and written the manifest. Two failures land before that upload
    and leave no artifact to open: `preflight` failing, which stops `build-zfs-akmods` from
    starting at all, and `resolve-build-inputs` raising inside the composite action. Neither
    leaves the values in a log either, because both resolvers print only after they return, so
    the paragraph has to say what is actually recoverable -- the configured starting points in
    `ci/defaults.json` -- and that the derived values do not exist for that run. Each half is
    joined below, and the two "print only on success" claims are executed. Its remaining
    claim, that an empty `AKMODS_UPSTREAM_REF` floats on `AKMODS_UPSTREAM_TRACK`, is the
    emergency-freeze rule, executed in EmergencyFreezeTests and not repeated here.
    """

    def setUp(self) -> None:
        self.prose = _trailing_prose(_section(DOC_TEXT, "## First triage"))
        self.action = PREPARE_ACTION.read_text(encoding="utf-8")

    def test_the_paragraph_names_the_two_runs_that_have_no_manifest(self) -> None:
        self.assertIn("preflight", self.prose)
        self.assertIn("build-zfs-akmods", self.prose)
        self.assertIn("resolve-build-inputs", self.prose)

    def test_a_failed_preflight_really_stops_the_akmods_job_from_starting(self) -> None:
        # "`build-zfs-akmods` never started" is a claim about the dependency edge, not about
        # any condition inside the job: a needed job that failed skips its dependents.
        self.assertEqual(_job_field(BUILD_TEXT, "build-zfs-akmods", "needs"), "preflight")
        self.assertIn("preflight", _job_names(BUILD_TEXT))

    def test_the_manifest_upload_really_sits_behind_input_resolution(self) -> None:
        # "raised before the manifest was written": the upload is downstream of both the
        # resolve step and the writer, so anything raising in resolution takes the artifact
        # with it. Compared by position in the composite action, which is where the order is.
        resolve = self.action.index("python3 -m ci_tools.cli resolve-build-inputs")
        write = self.action.index("python3 -m ci_tools.cli write-build-inputs-manifest")
        upload = self.action.index("name: build-inputs-${{ github.run_id }}")
        self.assertLess(resolve, write, "the manifest is now written before inputs resolve")
        self.assertLess(write, upload, "the artifact is now uploaded before it is written")

    def test_the_resolver_prints_nothing_when_resolution_raises(self) -> None:
        # Executed: "both resolvers print their results only after they succeed" is the reason
        # the paragraph does not send a responder to the job log for these values.
        from ci_tools import resolve_build_inputs

        stream = io.StringIO()
        with (
            patch.object(
                resolve_build_inputs, "resolve_build_inputs", side_effect=CiToolError("boom")
            ),
            contextlib.redirect_stdout(stream),
            self.assertRaises(CiToolError),
        ):
            resolve_build_inputs.main()
        self.assertEqual(stream.getvalue(), "", "a failed resolution now logs resolved inputs")

    def test_the_stable_signal_gate_prints_nothing_when_it_raises(self) -> None:
        # The other half of the same claim, for the job that fails first.
        from ci_tools import check_stable_signal

        stream = io.StringIO()
        with (
            patch.object(check_stable_signal, "_bypass_decision", side_effect=CiToolError("boom")),
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}, clear=False),
            contextlib.redirect_stdout(stream),
            self.assertRaises(CiToolError),
        ):
            check_stable_signal.main()
        self.assertEqual(stream.getvalue(), "", "a failed gate now logs a decision")

    def test_the_paragraph_sends_a_responder_to_keys_that_exist(self) -> None:
        defaults = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
        named = [key for key in defaults if key in self.prose]
        self.assertEqual(
            sorted(named),
            [
                "AKMODS_UPSTREAM_REF",
                "AKMODS_UPSTREAM_TRACK",
                "DEFAULT_BASE_IMAGE",
                "DEFAULT_BUILD_CONTAINER_IMAGE",
                "DEFAULT_ZFS_MINOR_VERSION",
            ],
            "the paragraph names a defaults key that does not exist, or stopped naming one",
        )

    def test_the_paragraph_says_the_derived_values_do_not_exist(self) -> None:
        # The half a responder acts on: not "look somewhere else", but "these were never
        # resolved". Every manifest-backed value step 2 lists is named here as absent.
        for value, key in VALUE_TO_MANIFEST_KEY.items():
            if key is None:
                continue
            with self.subTest(value=value):
                self.assertIn(value, self.prose)
        self.assertIn("do not exist for this run", self.prose)


class FailureTableShapeTests(unittest.TestCase):
    """The table's rows, asserted exhaustive before any row is joined to anything."""

    def setUp(self) -> None:
        self.rows = _table(_section(DOC_TEXT, "## Common failure classes"))

    def test_the_table_header_is_the_one_the_row_joins_assume(self) -> None:
        self.assertEqual(self.rows[0], ["Failure", "First action"])

    def test_the_table_holds_exactly_the_rows_this_module_reads(self) -> None:
        self.assertEqual(
            [row[0] for row in self.rows[1:]],
            list(FAILURE_ROWS),
            "a failure class was added, removed or reworded: join the new row to what decides it",
        )

    def test_every_row_offers_a_first_action(self) -> None:
        for failure, action in (row for row in self.rows[1:]):
            with self.subTest(failure=failure):
                self.assertTrue(action, f"{failure!r} has no first action")


def _row_action(failure: str) -> str:
    """Return the "First action" cell of one failure row, by its exact first column."""

    rows = _table(_section(DOC_TEXT, "## Common failure classes"))
    for row in rows[1:]:
        if row[0] == failure:
            return row[1]
    raise AssertionError(f"the failure table has no row {failure!r}")


class KmodRowTests(unittest.TestCase):
    """"No matching `kmod-zfs` package" against the code that produces it."""

    def setUp(self) -> None:
        self.action = _row_action("No matching `kmod-zfs` package")
        self.helper = _install_helper()

    def test_the_row_sends_a_responder_at_compatibility_and_the_fork(self) -> None:
        self.assertIn("Fedora kernel/OpenZFS compatibility", self.action)
        self.assertIn("Danathar/akmods", self.action)
        self.assertIn(
            "Danathar/akmods",
            load_repo_defaults()["AKMODS_UPSTREAM_REPO"],
            "the row names a fork that is not the configured upstream",
        )

    def _uncovered_kernel_failure(self, supported_kernel: str) -> str:
        """
        Return the planner's own text for the failure this row is about, by raising it.

        The cache held kmod-zfs RPMs, just not for the kernel the base image actually boots.
        Every assertion below reads this message rather than a copy of it, so a reworded
        failure cannot leave the row's advice pinned to text the machine no longer produces.
        """

        with self.assertRaises(RuntimeError) as context:
            self.helper.build_install_plan(
                ["7.1.4-204.fc44.x86_64", supported_kernel],
                [Path("/tmp/kmod-zfs-7.1.4.rpm")],
                rpm_name_lookup=lambda _path: "kmod-zfs",
                kernel_release_lookup=lambda _path: "7.1.4-204.fc44.x86_64",
            )
        return str(context.exception)

    def test_the_planner_fails_closed_when_the_supported_kernel_has_no_kmod(self) -> None:
        # Executed, not grepped.
        message = self._uncovered_kernel_failure("7.1.6-200.fc44.x86_64")
        self.assertIn("No kmod-zfs RPM found for the supported primary kernel", message)
        self.assertIn("7.1.6-200.fc44.x86_64", message)

    def test_the_planner_fails_closed_when_the_cache_has_no_kmod_at_all(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No kmod-zfs RPMs found in cache image"):
            self.helper.build_install_plan(
                ["7.1.4-204.fc44.x86_64"],
                [Path("/tmp/zfs-2.4.3.rpm")],
                rpm_name_lookup=lambda _path: "zfs",
                kernel_release_lookup=lambda _path: "7.1.4-204.fc44.x86_64",
            )

    def test_the_classifier_reads_that_failure_as_an_upstream_compatibility_break(self) -> None:
        # The row tells a responder to wait for the fork, which is only the right advice if the
        # machine also classifies this as upstream drift rather than a repo bug. The text
        # classified here is the exception the planner just raised, not a copy of it: if the
        # planner rewords its message past what the patterns recognise, this fails.
        from ci_tools.classify_akmods_failure import (
            FAILURE_KIND_UPSTREAM_COMPAT,
            UPSTREAM_COMPAT_PATTERNS,
            classify_log_text,
        )

        kernel = "7.1.6-200.fc44.x86_64"
        message = self._uncovered_kernel_failure(kernel)
        kind, matched = classify_log_text(message, kernel_release=kernel)
        self.assertTrue(matched, "no pattern matched the planner's own fail-closed message")
        self.assertEqual(kind, FAILURE_KIND_UPSTREAM_COMPAT)
        self.assertTrue(
            any(pattern.search(message) for pattern in UPSTREAM_COMPAT_PATTERNS),
            "UPSTREAM_COMPAT_PATTERNS no longer covers the install helper's fail-closed path",
        )


class DnfRowTests(unittest.TestCase):
    """"Final DNF transaction fails" against the split and the version it is about."""

    def setUp(self) -> None:
        self.action = _row_action("Final DNF transaction fails")
        self.helper = _install_helper()

    def test_the_row_names_both_zfs_lines(self) -> None:
        self.assertIn("ZFS userspace line", self.action)
        self.assertIn("configured OpenZFS line", self.action)

    def test_the_planner_really_splits_userspace_from_the_kernel_module(self) -> None:
        # "the base image's existing ZFS userspace line and the configured OpenZFS line" is a
        # claim about two sets of RPMs reaching dnf5 from different places. Executed: the
        # userspace RPMs come back as `managed_rpms`, the kmod as one selected payload.
        userspace = Path("/tmp/zfs-2.4.3.rpm")
        libs = Path("/tmp/libzfs6-2.4.3.rpm")
        kmod = Path("/tmp/kmod-zfs-7.1.4.rpm")
        names = {userspace: "zfs", libs: "libzfs6", kmod: "kmod-zfs"}
        plan = self.helper.build_install_plan(
            ["7.1.4-204.fc44.x86_64"],
            [userspace, libs, kmod],
            rpm_name_lookup=names.__getitem__,
            kernel_release_lookup=lambda _path: "7.1.4-204.fc44.x86_64",
        )
        self.assertEqual(plan.managed_rpms, [userspace, libs])
        self.assertEqual(plan.supported_kmod_rpm, kmod)

    def test_both_sets_reach_one_dnf5_transaction(self) -> None:
        # "Final DNF transaction" -- one transaction, which is why a userspace conflict shows
        # up as a dnf failure rather than as a missing module.
        calls: list[list[str]] = []
        def record(cmd, **_kw):
            calls.append(cmd)

        with patch.object(self.helper, "_run_cmd", side_effect=record):
            self.helper.dnf5_install([Path("/tmp/zfs-2.4.3.rpm"), Path("/tmp/kmod-zfs-7.1.4.rpm")])
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][:3], ["dnf5", "install", "-y"])
        self.assertEqual(calls[0][3:], ["/tmp/zfs-2.4.3.rpm", "/tmp/kmod-zfs-7.1.4.rpm"])

    def test_the_configured_openzfs_line_is_a_checked_in_default(self) -> None:
        # The "configured" half of the row: a responder comparing lines needs the configured
        # one to be a value in the tree, not a runtime guess.
        defaults = load_repo_defaults()
        self.assertTrue(re.fullmatch(r"\d+\.\d+", defaults["DEFAULT_ZFS_MINOR_VERSION"]))
        self.assertEqual(
            json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["DEFAULT_ZFS_MINOR_VERSION"],
            defaults["DEFAULT_ZFS_MINOR_VERSION"],
        )


class CacheRowTests(unittest.TestCase):
    """"Cache verification fails" against the tag both helpers build and the reuse rule."""

    def setUp(self) -> None:
        self.action = _row_action("Cache verification fails")

    def test_the_row_says_rebuild_on_main_and_bypass_nothing(self) -> None:
        self.assertIn("`main`", self.action)
        self.assertIn("do not bypass signature or digest checks", self.action)

    def test_both_helpers_build_the_same_main_fedora_tag(self) -> None:
        # "Rebuild the cache on `main`" is only actionable because the tag the rebuild
        # publishes is the tag the reuse check reads. Executed on both sides.
        pinned = akmods_cache_image_tag(
            image_org="danathar", source_repo="zfs-kinoite-complex-akmods", fedora_version="44"
        )
        with patch(
            "ci_tools.check_akmods_cache.skopeo_inspect_json_optional", return_value=None
        ) as inspect:
            status = inspect_akmods_cache(
                image_org="danathar",
                source_repo="zfs-kinoite-complex-akmods",
                fedora_version="44",
                kernel_release="7.1.4-204.fc44.x86_64",
                zfs_version="2.4.3",
            )
        self.assertEqual(status.source_image, pinned)
        self.assertTrue(pinned.endswith(":main-44"), pinned)
        inspect.assert_called_once_with(f"docker://{pinned}", creds=None)

    def test_the_tag_tracks_the_fedora_version_rather_than_being_a_literal(self) -> None:
        self.assertTrue(
            akmods_cache_image_tag(
                image_org="danathar", source_repo="repo", fedora_version="45"
            ).endswith(":main-45")
        )

    def test_a_cache_is_not_reusable_without_a_verified_signature(self) -> None:
        # "do not bypass signature or digest checks" -- executed against the real property.
        content_only = AkmodsCacheStatus(
            source_image="ghcr.io/danathar/repo:main-44",
            image_exists=True,
            source_image_pinned="ghcr.io/danathar/repo@sha256:" + "0" * 64,
            signature_verified=False,
        )
        self.assertTrue(content_only.content_matches)
        self.assertFalse(content_only.reusable)

        signed = AkmodsCacheStatus(
            source_image=content_only.source_image,
            image_exists=True,
            source_image_pinned=content_only.source_image_pinned,
            signature_verified=True,
        )
        self.assertTrue(signed.reusable)

    def test_a_signed_cache_missing_the_kernel_is_still_not_reusable(self) -> None:
        # The digest half of the same sentence: a signature on the wrong content is not a pass.
        self.assertFalse(
            AkmodsCacheStatus(
                source_image="ghcr.io/danathar/repo:main-44",
                image_exists=True,
                missing_release="7.1.6-200.fc44.x86_64",
                signature_verified=True,
            ).reusable
        )


class ContainerRowTests(unittest.TestCase):
    """"Container build fails before steps run" against the job-level pin it names."""

    def setUp(self) -> None:
        self.action = _row_action("Container build fails before steps run")

    def test_the_row_names_the_job_level_pin(self) -> None:
        self.assertIn("job-level pinned build-container image", self.action)

    def test_the_akmods_job_really_pins_its_container_by_digest(self) -> None:
        # "before steps run" is why this row exists at all: `container:` is resolved and
        # started before any step, so no in-job guard can report on it.
        block = _job_block(BUILD_TEXT, "build-zfs-akmods")
        match = re.search(r"^ {6}image: (\S+)$", block, re.MULTILINE)
        self.assertIsNotNone(match, "build-zfs-akmods no longer has a `container: image:` pin")
        image = match.group(1)
        self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_the_pin_matches_the_checked_in_default(self) -> None:
        # The literal cannot read a step output, so it is kept in sync by hand -- exactly the
        # kind of copy a responder comparing images has to be able to trust.
        block = _job_block(BUILD_TEXT, "build-zfs-akmods")
        image = re.search(r"^ {6}image: (\S+)$", block, re.MULTILINE).group(1)
        self.assertEqual(image, load_repo_defaults()["DEFAULT_BUILD_CONTAINER_IMAGE"])


class SigningRowTests(unittest.TestCase):
    """"Signing fails" against the four things the row tells a responder to check."""

    def setUp(self) -> None:
        self.action = _row_action("Signing fails")
        self.block = _job_block(BUILD_TEXT, "sign-akmods-cache")

    def test_the_row_names_the_secret_the_job_reads(self) -> None:
        self.assertIn("`SIGNING_SECRET`", self.action)
        self.assertIn("secrets.SIGNING_SECRET", self.block)

    def test_the_job_refuses_to_run_without_that_secret(self) -> None:
        # "Check `SIGNING_SECRET`" is the right first action only because a missing secret
        # fails the job loudly instead of leaving a rebuilt cache quietly unsigned.
        self.assertIn("HAS_SIGNING_SECRET != 'true'", self.block)
        self.assertIn("SIGNING_SECRET is not configured", self.block)

    def test_the_row_names_environment_restrictions_the_job_declares(self) -> None:
        self.assertIn("environment restrictions", self.action)
        self.assertEqual(
            _job_field(BUILD_TEXT, "sign-akmods-cache", "environment"), "production-signing"
        )

    def test_the_row_names_a_registry_login_the_job_performs(self) -> None:
        self.assertIn("registry login", self.action)
        self.assertIn("uses: docker/login-action@", self.block)
        self.assertIn("registry: ghcr.io", self.block)

    def test_the_committed_public_key_the_row_names_is_what_verification_uses(self) -> None:
        self.assertIn("committed public key", self.action)
        self.assertTrue(COSIGN_PUB.is_file(), "cosign.pub is not committed")
        source = (REPO_ROOT / "ci_tools" / "promote_stable.py").read_text(encoding="utf-8")
        self.assertIn('REPO_ROOT / "cosign.pub"', source)


class PromotionRowTests(unittest.TestCase):
    """
    "Promotion fails: keep `latest` unchanged; verify the candidate digest and signature
    before retrying" -- by executing `promote_stable.main` rather than reading it.

    Three separate orderings are claimed in one cell: that the signature is verified before
    any tag moves, that the audit tag is written before `:latest`, and that a failed
    verification leaves `:latest` alone. Each is executed below.
    """

    def setUp(self) -> None:
        self.action = _row_action("Promotion fails")

    def test_the_row_still_makes_the_claim(self) -> None:
        self.assertIn("`latest`", self.action)
        self.assertIn("candidate digest and signature", self.action)

    def test_the_signature_is_verified_before_any_tag_moves(self) -> None:
        order: list[str] = []

        def note_verify(*_args, **_kwargs):
            order.append("verify")

        with (
            patch.dict(os.environ, PROMOTE_ENV, clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.run_cmd", side_effect=note_verify),
            patch(
                "ci_tools.promote_stable.skopeo_copy",
                side_effect=lambda _source, destination, **_kw: order.append(destination),
            ),
        ):
            promote_stable_main()

        self.assertEqual(order[0], "verify", order)
        self.assertEqual(len(order), 3, order)

    def test_the_audit_tag_is_written_before_latest(self) -> None:
        destinations: list[str] = []
        with (
            patch.dict(os.environ, PROMOTE_ENV, clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.run_cmd"),
            patch(
                "ci_tools.promote_stable.skopeo_copy",
                side_effect=lambda _source, destination, **_kw: destinations.append(destination),
            ),
        ):
            promote_stable_main()

        self.assertEqual(
            destinations,
            [
                "docker://ghcr.io/danathar/zfs-kinoite-complex:stable-12-deadbee",
                "docker://ghcr.io/danathar/zfs-kinoite-complex:latest",
            ],
        )

    def test_latest_is_untouched_when_the_signature_does_not_verify(self) -> None:
        # The "keep `latest` unchanged" half. A responder is told to verify before retrying,
        # which is only sound because a failed verification cannot have moved the tag already.
        with (
            patch.dict(os.environ, PROMOTE_ENV, clear=True),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch(
                "ci_tools.promote_stable.run_cmd",
                side_effect=CiToolError("cosign verify failed"),
            ),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            self.assertRaises(CiToolError),
        ):
            promote_stable_main()

        skopeo_copy.assert_not_called()

    def test_promotion_verifies_against_a_key_file_that_must_exist(self) -> None:
        with (
            patch.dict(
                os.environ,
                dict(PROMOTE_ENV, COSIGN_PUBLIC_KEY_PATH=str(REPO_ROOT / "no-such-key.pub")),
                clear=True,
            ),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", return_value="sha256:abc"),
            patch("ci_tools.promote_stable.skopeo_copy") as skopeo_copy,
            self.assertRaisesRegex(CiToolError, "Missing required verification key file"),
        ):
            promote_stable_main()

        skopeo_copy.assert_not_called()


class ResponseRuleTests(unittest.TestCase):
    """The emergency-freeze rule against the defaults key and the cascade that reads it."""

    def setUp(self) -> None:
        self.rules = _prose(_section(DOC_TEXT, "## Response rules"))
        self.defaults = load_repo_defaults()

    def test_the_rule_names_the_key_and_the_file_that_holds_it(self) -> None:
        self.assertIn("AKMODS_UPSTREAM_REF", self.rules)
        self.assertIn("ci/defaults.json", self.rules)
        self.assertIn("clear the pin", self.rules)

    def test_the_checked_in_pin_is_empty_so_the_repository_is_not_frozen(self) -> None:
        # The rule describes an exception. A pin left behind in the committed file would mean
        # this page describes a repository that is not this one.
        self.assertEqual(
            json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["AKMODS_UPSTREAM_REF"], ""
        )

    def test_a_pin_in_the_defaults_file_really_freezes_the_build(self) -> None:
        # Executed in the explicit-pin mode the rule tells a maintainer to use, with the
        # floating resolver patched so a network lookup cannot be what answers.
        pinned = dict(self.defaults, AKMODS_UPSTREAM_REF=PIN_SHA)
        with (
            patch.dict(os.environ, _wiped_env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=pinned),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
        ):
            self.assertEqual(_resolve_default_akmods_ref(), PIN_SHA)
        ls_remote.assert_not_called()

    def test_clearing_the_pin_returns_the_build_to_the_floating_track(self) -> None:
        # The other half of the rule, and the reason it is safe to tell someone to clear the
        # pin: with the committed empty value the cascade floats to the tracking ref.
        with (
            patch.dict(os.environ, _wiped_env(), clear=False),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve", return_value=TRACK_SHA
            ) as ls_remote,
        ):
            self.assertEqual(_resolve_default_akmods_ref(), TRACK_SHA)
        ls_remote.assert_called_once_with(
            self.defaults["AKMODS_UPSTREAM_REPO"], self.defaults["AKMODS_UPSTREAM_TRACK"]
        )

    def test_the_rule_against_patching_the_checkout_names_the_fork_it_redirects_to(self) -> None:
        self.assertIn("Do not patch the cloned akmods checkout", self.rules)
        self.assertIn("floating or explicit-ref selection", self.rules)

    def test_the_fail_closed_rule_matches_what_a_red_candidate_leaves_behind(self) -> None:
        self.assertIn("Do not weaken fail-closed checks", self.rules)
        self.assertIn("unchanged stable image", self.rules)
        # Same gate as the intro sentence, from the other end of the page.
        self.assertIn(
            "needs.build-candidate-image.result == 'success'",
            _job_field(BUILD_TEXT, "promote-stable", "if"),
        )


class AfterRecoveryTests(unittest.TestCase):
    """The closing section promises a test and a documented invariant. Both are checkable."""

    def setUp(self) -> None:
        self.body = _prose(_section(DOC_TEXT, "## After recovery"))

    def test_the_section_asks_for_the_inputs_this_page_told_a_responder_to_record(self) -> None:
        self.assertIn("exact input versions", self.body)

    def test_the_section_asks_for_a_test_when_trust_boundaries_move(self) -> None:
        self.assertIn("unit test", self.body)
        self.assertIn("before promotion", self.body)

    def test_the_test_the_section_promises_runs_on_every_pull_request(self) -> None:
        # "add or update a unit test ... before promotion" is only an instruction a responder
        # can follow if the suite it names actually gates a pull request.
        workflow = TEST_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("pytest", workflow)


if __name__ == "__main__":
    unittest.main()
