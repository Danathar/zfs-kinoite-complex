"""
Script: tests/test_production_boundary_docs.py
What: Joins the two documents that describe the production signing boundary --
docs/maintenance-watchlist.md and docs/production-boundary-proposal.md -- to the workflow
files, ci/defaults.json and the Containerfile whose contents they restate.
Doing: Extracts the named sections from each document and recomputes every claim they make
about the machine, then asserts the two agree.
Why: The watchlist is the standing record that `production-signing` has no branch policy, so
a dispatch against any branch can reach the production signing key. Every supporting fact it
gives -- which jobs declare the environment, which consume `secrets.SIGNING_SECRET`, that no
job guards on `github.ref`, that `promote_to_stable` defaults to `true` -- is a hand copy of
build.yml. Nothing opened either file, so build.yml could be fixed, or made worse, and the
document describing its trust boundary would keep reading exactly the same.
Goal: Make the documents fail here when the workflows move under them, in both directions: a
fix that nobody wrote down is as much a drift as a regression nobody noticed.

Parses by indentation rather than with PyYAML. CI installs pytest, pytest-cov and ruff and
nothing else (see .github/workflows/test.yml), so a PyYAML import here would skip in exactly
the place the assertions are supposed to run. `_keys()` is the whole parser and carries its
own case table below, because a hand-rolled parser that is never wrong about a fixture is
the only thing that keeps the assertions built on it from being vacuous.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DOCS_DIR = REPO_ROOT / "docs"

WATCHLIST = DOCS_DIR / "maintenance-watchlist.md"
BOUNDARY_PROPOSAL = DOCS_DIR / "production-boundary-proposal.md"
TESTING_DOC = DOCS_DIR / "zfs-kinoite-testing.md"

BUILD = WORKFLOW_DIR / "build.yml"

# The heading of the watchlist section that records the open finding. Everything the
# tests in OpenFindingTests recompute is quoted inside it.
OPEN_FINDING_HEADING = "### Open: the `production-signing` environment is not branch-restricted"

SIGNING_ENVIRONMENT = "production-signing"


def _decommented(text: str) -> list[str]:
    """
    Return `text` as lines with whole-line comments dropped.

    Whole-line only. A trailing comment after real YAML would need to know whether the
    `#` is inside a quoted scalar to strip correctly, and that is a parse this file does
    not need: nothing below reads a value that carries a trailing comment.

    Dropping the line rather than blanking it is safe here because every claim recomputed
    below is about the presence or value of a key, never about a line number.
    """

    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


class Entry:
    """One mapping key: the value written on its own line, and the lines nested under it."""

    def __init__(self, inline: str, children: list[str]) -> None:
        self.inline = inline
        self.children = children

    @property
    def value(self) -> str:
        """
        The key's scalar value, whether written inline or as a block scalar.

        `if: >-` followed by indented lines is one expression, and the folded form is
        what build.yml actually uses for the promotion gate. Reading only `inline` there
        would compare against the literal string ">-" and pass for any condition at all.
        """

        if self.inline in (">", ">-", "|", "|-"):
            return " ".join(line.strip() for line in self.children if line.strip())
        return self.inline


def _keys(lines: list[str]) -> dict[str, Entry]:
    """
    Split `lines` into the mapping keys at their shallowest indentation.

    Blank lines and lines below a key's indentation belong to that key. A sequence item
    (`- x`) is not a key, so a block like `needs:` keeps its items as children rather
    than being mistaken for a nested mapping.
    """

    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    if not indents:
        return {}
    top = min(indents)

    entries: dict[str, Entry] = {}
    current: Entry | None = None
    for line in lines:
        if not line.strip():
            if current is not None:
                current.children.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == top and not stripped.startswith("-") and ":" in stripped:
            key, _, inline = stripped.partition(":")
            current = Entry(inline.strip(), [])
            entries[key.strip()] = current
            continue
        if current is not None:
            current.children.append(line)
    return entries


def _workflow_keys(path: Path) -> dict[str, Entry]:
    return _keys(_decommented(path.read_text(encoding="utf-8")))


def _jobs(path: Path) -> dict[str, Entry]:
    top = _workflow_keys(path)
    if "jobs" not in top:
        raise AssertionError(f"{path.name} has no top-level `jobs:` key")
    return _keys(top["jobs"].children)


def _normalized(text: str) -> str:
    """Collapse whitespace so a claim wrapped across two lines still matches."""

    return re.sub(r"\s+", " ", text)


def _section(path: Path, heading: str) -> str:
    """
    Return the body of the Markdown section introduced by `heading`.

    Scoped to the section so a claim that moves out of it -- or a section that is deleted
    outright -- fails rather than quietly matching the same words somewhere else in the
    file.
    """

    text = path.read_text(encoding="utf-8")
    if heading not in text:
        raise AssertionError(f"{path.name} no longer contains the heading {heading!r}")
    level = len(heading.split(" ", 1)[0])

    body: list[str] = []
    fenced = False
    for line in text.split(heading, 1)[1].splitlines()[1:]:
        if line.startswith("```"):
            fenced = not fenced
        # Inside a fence a `#` starts a shell comment, not a heading. The open finding
        # quotes a `gh api` call whose output line begins with one, and reading that as
        # a heading would truncate the section in the middle of its own evidence.
        elif not fenced and re.match(rf"#{{1,{level}}} ", line):
            break
        body.append(line)
    return "\n".join(body)


def _defaults() -> dict[str, str]:
    return json.loads((REPO_ROOT / "ci" / "defaults.json").read_text(encoding="utf-8"))


class KeyParserTests(unittest.TestCase):
    """
    The case table for `_keys()`.

    Every assertion in this file is a statement about what `_keys()` returned, so a
    parser that silently returns nothing would turn the rest of the file green while
    checking nothing at all.
    """

    FIXTURE = """\
on:
  workflow_dispatch:
    inputs:
      promote_to_stable:
        default: true

jobs:
  first:
    environment: an-environment
    needs:
      - second
    if: >-
      a == 'b'
      && c == 'd'
  second:
    name: Second
"""

    def setUp(self) -> None:
        self.lines = _decommented(self.FIXTURE)

    def test_top_level_keys_are_found(self) -> None:
        self.assertEqual(sorted(_keys(self.lines)), ["jobs", "on"])

    def test_nested_mapping_is_reachable(self) -> None:
        jobs = _keys(_keys(self.lines)["jobs"].children)
        self.assertEqual(sorted(jobs), ["first", "second"])
        self.assertEqual(_keys(jobs["first"].children)["environment"].value, "an-environment")

    def test_a_sequence_item_is_not_read_as_a_key(self) -> None:
        first = _keys(_keys(_keys(self.lines)["jobs"].children)["first"].children)
        self.assertNotIn("- second", first)
        self.assertEqual(first["needs"].children, ["      - second"])

    def test_a_folded_block_scalar_is_joined_into_one_value(self) -> None:
        first = _keys(_keys(_keys(self.lines)["jobs"].children)["first"].children)
        self.assertEqual(first["if"].value, "a == 'b' && c == 'd'")

    def test_a_deeply_nested_scalar_is_reachable(self) -> None:
        dispatch = _keys(_keys(self.lines)["on"].children)["workflow_dispatch"]
        inputs = _keys(_keys(dispatch.children)["inputs"].children)
        self.assertEqual(_keys(inputs["promote_to_stable"].children)["default"].value, "true")

    def test_whole_line_comments_are_dropped_and_nothing_else_is(self) -> None:
        self.assertEqual(
            _decommented("a: 1\n  # note\n#\nb: 2  # trailing\n"),
            ["a: 1", "b: 2  # trailing"],
        )


class SectionExtractorTests(unittest.TestCase):
    def test_the_open_finding_section_is_not_empty(self) -> None:
        # An extractor that returns "" would make every `assertIn` below vacuous.
        self.assertGreater(len(_section(WATCHLIST, OPEN_FINDING_HEADING).strip()), 500)

    def test_the_section_stops_at_the_next_heading(self) -> None:
        section = _section(WATCHLIST, OPEN_FINDING_HEADING)
        self.assertNotIn("## Runtime validation", section)

    def test_a_missing_heading_is_an_error_rather_than_an_empty_string(self) -> None:
        with self.assertRaises(AssertionError):
            _section(WATCHLIST, "### Open: a heading nobody wrote")


class BaseImageChangeProcedureTests(unittest.TestCase):
    """
    The watchlist's Fedora Kinoite section tells a maintainer to update the base image in
    two files. That instruction is only correct while the two actually agree.
    """

    def setUp(self) -> None:
        self.section = _section(WATCHLIST, "## Fedora Kinoite and kernels")

    def test_the_section_still_names_both_files_it_tells_you_to_edit(self) -> None:
        for name in ("`ci/defaults.json`", "`Containerfile`"):
            self.assertIn(name, self.section)

    def test_the_two_files_the_procedure_names_actually_agree(self) -> None:
        containerfile = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")
        match = re.search(r'^ARG BASE_IMAGE="(?P<image>[^"]+)"', containerfile, re.MULTILINE)
        self.assertIsNotNone(match, "Containerfile no longer declares ARG BASE_IMAGE")
        self.assertEqual(
            match.group("image"),
            _defaults()["DEFAULT_BASE_IMAGE"],
            "The watchlist's step 1 says to update the base image in both ci/defaults.json "
            "and the Containerfile. They disagree, so one of the two edits was missed.",
        )

    def test_the_akmods_fork_the_watchlist_names_is_the_one_configured(self) -> None:
        section = _section(WATCHLIST, "## Akmods fork")
        defaults = _defaults()
        self.assertIn(f"`{defaults['AKMODS_UPSTREAM_TRACK']}`", section)
        # "follows `Danathar/akmods`" -- the owner/name, not the clone URL.
        owner_and_name = defaults["AKMODS_UPSTREAM_REPO"].removesuffix(".git").split("/")[-2:]
        self.assertIn("`" + "/".join(owner_and_name) + "`", section)


class SigningBoundaryNamesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.section = _section(WATCHLIST, "## Signing and GitHub settings")

    def test_the_committed_public_key_the_section_names_exists(self) -> None:
        self.assertIn("`cosign.pub`", self.section)
        self.assertTrue((REPO_ROOT / "cosign.pub").is_file())

    def test_the_secret_and_environment_it_names_are_the_ones_build_yml_uses(self) -> None:
        self.assertIn("`SIGNING_SECRET`", self.section)
        self.assertIn(f"`{SIGNING_ENVIRONMENT}`", self.section)
        build = BUILD.read_text(encoding="utf-8")
        self.assertIn("secrets.SIGNING_SECRET", build)
        self.assertIn(f"environment: {SIGNING_ENVIRONMENT}", build)


class OpenFindingTests(unittest.TestCase):
    """
    Each bullet of the watchlist's open finding, recomputed from build.yml.

    These fail in both directions by design. If the branch-restriction gap is closed in
    the workflow, the recomputed facts stop matching what the document asserts and the
    document has to be rewritten in the same change -- which is the point, because a
    finding that is quietly no longer true reads exactly like one nobody has checked.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.section = _section(WATCHLIST, OPEN_FINDING_HEADING)
        cls.top = _workflow_keys(BUILD)
        cls.jobs = _jobs(BUILD)

    def _job(self, name: str) -> dict[str, Entry]:
        self.assertIn(name, self.jobs, f"build.yml no longer defines a `{name}` job")
        return _keys(self.jobs[name].children)

    def test_build_yml_still_carries_the_workflow_dispatch_trigger(self) -> None:
        self.assertIn("workflow_dispatch", self.section)
        triggers = _keys(self.top["on"].children)
        self.assertIn(
            "workflow_dispatch",
            triggers,
            "The finding's first bullet is that `gh workflow run build.yml --ref <branch>` "
            "runs that branch's copy. Without the trigger that route is gone and the "
            "watchlist needs updating.",
        )

    def test_the_two_jobs_it_names_are_exactly_the_jobs_in_the_environment(self) -> None:
        for name in ("`sign-akmods-cache`", "`build-candidate-image`"):
            self.assertIn(name, self.section)
        in_environment = {
            name
            for name in self.jobs
            if _keys(self.jobs[name].children).get("environment", Entry("", [])).value
            == SIGNING_ENVIRONMENT
        }
        self.assertEqual(
            in_environment,
            {"sign-akmods-cache", "build-candidate-image"},
            "The watchlist names these two jobs as the ones that reach the production "
            "signing key. build.yml now scopes a different set to the environment.",
        )

    def test_both_named_jobs_consume_the_signing_secret(self) -> None:
        self.assertIn("`secrets.SIGNING_SECRET`".strip("`"), self.section)
        for name in ("sign-akmods-cache", "build-candidate-image"):
            body = "\n".join(self.jobs[name].children)
            self.assertIn(
                "secrets.SIGNING_SECRET",
                body,
                f"The watchlist says {name} consumes the signing secret; it no longer does.",
            )

    def test_no_job_guards_on_the_ref_and_only_the_concurrency_key_mentions_it(self) -> None:
        self.assertIn("No job in `build.yml` guards on `github.ref`", self.section)
        guarded = sorted(
            name for name, job in self.jobs.items() if "github.ref" in "\n".join(job.children)
        )
        self.assertEqual(
            guarded,
            [],
            "The watchlist records that no job guards on `github.ref`, which is why a "
            f"dispatch from any branch reaches the signing key. {guarded} now does. If "
            "that is the fix, rewrite the open finding in docs/maintenance-watchlist.md "
            "in the same change.",
        )
        # "The only ref-shaped expression in the file is the `concurrency` group key."
        self.assertEqual(
            _keys(self.top["concurrency"].children)["group"].value,
            "${{ github.workflow }}-${{ github.ref || github.run_id }}",
        )
        self.assertEqual(
            sum("github.ref" in line for line in _decommented(BUILD.read_text(encoding="utf-8"))),
            1,
        )

    def test_the_promotion_gate_reads_the_input_the_finding_quotes(self) -> None:
        condition = self._job("promote-stable")["if"].value
        self.assertIn(
            "github.event.inputs.promote_to_stable == 'true'",
            condition,
            "The finding quotes this condition verbatim from promote-stable's `if`.",
        )
        # The quote wraps across two lines in the document, so compare it collapsed.
        self.assertIn(
            "github.event.inputs.promote_to_stable == 'true'", _normalized(self.section)
        )

    def test_that_input_still_defaults_to_true(self) -> None:
        self.assertIn("defaults to `true`", self.section)
        dispatch = _keys(_keys(self.top["on"].children)["workflow_dispatch"].children)
        inputs = _keys(dispatch["inputs"].children)
        self.assertIn("promote_to_stable", inputs)
        self.assertEqual(
            _keys(inputs["promote_to_stable"].children)["default"].value,
            "true",
            "The finding's severity rests on this default: a dispatch that says nothing "
            "about promotion still moves `:latest`. Flipping it to false changes the "
            "finding and the watchlist has to say so.",
        )

    def test_build_branch_yml_is_still_the_clean_one_the_finding_contrasts(self) -> None:
        self.assertIn("`build-branch.yml` is separately clean", self.section)
        text = (WORKFLOW_DIR / "build-branch.yml").read_text(encoding="utf-8")
        self.assertNotIn("SIGNING_SECRET", text)


class QuotedDocumentTests(unittest.TestCase):
    """
    The open finding quotes two other documents to establish that the boundary is
    asserted elsewhere as fact. A quote that no longer appears at the source is how a
    finding becomes unfalsifiable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.section = _normalized(_section(WATCHLIST, OPEN_FINDING_HEADING))

    def test_the_proposal_still_requires_the_rule_the_finding_quotes(self) -> None:
        quote = "environment rules restricting the signing jobs to `main`"
        self.assertIn(quote, self.section)
        proposal = _normalized(BOUNDARY_PROPOSAL.read_text(encoding="utf-8"))
        self.assertIn(quote, proposal)
        # The finding calls it a "required setting"; that is the proposal's own heading.
        self.assertIn("## Required settings", BOUNDARY_PROPOSAL.read_text(encoding="utf-8"))

    def test_the_proposals_checklist_line_is_quoted_accurately(self) -> None:
        quote = "unavailable to branch and pull-request runs"
        self.assertIn(quote, self.section)
        self.assertIn(quote, _normalized(BOUNDARY_PROPOSAL.read_text(encoding="utf-8")))

    def test_the_testing_document_still_states_the_boundary_as_fact(self) -> None:
        quote = f"inside the `{SIGNING_ENVIRONMENT}` environment that only `main` refs can reach"
        self.assertIn(quote, self.section)
        self.assertIn(quote, _normalized(TESTING_DOC.read_text(encoding="utf-8")))

    def test_the_test_the_finding_credits_still_pins_what_it_says(self) -> None:
        self.assertIn("tests/test_workflow_build_container.py", self.section)
        pinning_test = REPO_ROOT / "tests" / "test_workflow_build_container.py"
        self.assertTrue(pinning_test.is_file())
        self.assertIn("SIGNING_SECRET", pinning_test.read_text(encoding="utf-8"))


class ReviewChecklistTests(unittest.TestCase):
    """
    The two lines of the proposal's review checklist that are decidable from the tree.

    The rest of that checklist is repository settings, which is exactly why these two are
    worth pinning: they are the part a workflow edit can break without anyone logging in
    to GitHub to notice.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.checklist = _section(BOUNDARY_PROPOSAL, "## Review checklist")
        cls.workflows = sorted(WORKFLOW_DIR.glob("*.yml"))

    def test_the_scan_covers_every_workflow(self) -> None:
        self.assertGreaterEqual(len(self.workflows), 7)

    def test_the_signing_secret_is_referenced_only_inside_that_environment(self) -> None:
        self.assertIn("`SIGNING_SECRET` is present only in that environment", self.checklist)
        stray = []
        for path in self.workflows:
            for name, job in _jobs(path).items():
                body = "\n".join(job.children)
                if "secrets.SIGNING_SECRET" not in body:
                    continue
                environment = _keys(job.children).get("environment", Entry("", [])).value
                if environment != SIGNING_ENVIRONMENT:
                    stray.append(f"{path.name}:{name} (environment: {environment or 'none'})")
        self.assertEqual(
            stray,
            [],
            "The proposal's checklist says the signing secret lives only in the "
            f"`{SIGNING_ENVIRONMENT}` environment, but these jobs read it outside it.",
        )

    def test_latest_is_moved_only_by_the_promotion_job(self) -> None:
        self.assertIn("`latest` is moved only by the promotion job", self.checklist)
        self.assertIn("branch builds cannot sign or promote production tags", self.checklist)
        promoters = []
        for path in self.workflows:
            for name, job in _jobs(path).items():
                if "ci_tools.cli promote-stable" in "\n".join(job.children):
                    promoters.append(f"{path.name}:{name}")
        self.assertEqual(promoters, ["build.yml:promote-stable"])

    def test_no_branch_or_pull_request_workflow_signs(self) -> None:
        for name in ("build-branch.yml", "build-pr.yml"):
            text = (WORKFLOW_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("ci_tools.cli sign-image", text)
            self.assertNotIn("ci_tools.cli promote-stable", text)


if __name__ == "__main__":
    unittest.main()
