"""
Script: tests/test_docs_consistency.py
What: Holds the mechanically checkable claims the documentation makes about the tree.
Doing: Resolves every relative link, and checks the documentation map and the quality page against what actually exists.
Why: Doc drift is a defect in this repository, not a nit -- AGENTS.md section 0 rule 3 exists because it has happened.
Goal: Turn "someone should re-read the docs" into something CI does on every pull request.

Only the claims a machine can settle. Whether a sentence is *true* is a review
question; whether the file it names exists is not, and that is the class of
error that actually accumulates. A hand audit found four of these in one pass,
two of them introduced the same day by stacked pull requests -- which is the
argument for checking them automatically rather than periodically.

No PyYAML and no third-party parser, for the reason
tests/test_workflow_build_container.py gives: the CI job installs only pytest,
pytest-cov and ruff, so anything else would depend on the runner image and skip
silently the day that changed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions"
DOCS_DIR = REPO_ROOT / "docs"
DOC_MAP = DOCS_DIR / "documentation-guide.md"
QUALITY = DOCS_DIR / "quality.md"

# Markdown inline links. Bare `<http://...>` autolinks and reference-style
# definitions are not used in this tree, so this is the whole surface.
def workflow_paths() -> list[Path]:
    """Every workflow GitHub runs: each `.yml` and `.yaml` file in WORKFLOW_DIR.

    A scan that globbed one extension would not narrow its check, it would
    exempt every workflow spelled the other way. Scans over the workflows read
    their list from here.
    """

    return sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])


def action_paths() -> list[Path]:
    """Every local composite action: each `action.yml` and `action.yaml` under ACTION_DIR.

    GitHub loads an action from either spelling, so the same rule as
    workflow_paths() applies: a scan that globbed `action.yml` alone would
    exempt an action written as `action.yaml`. Scans over the actions read
    their list from here.
    """

    return sorted([*ACTION_DIR.glob("*/action.yml"), *ACTION_DIR.glob("*/action.yaml")])


LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def tracked_markdown() -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / name for name in listing]


def heading_slugs(path: Path) -> set[str]:
    """GitHub's anchor slugs for a markdown file's headings."""

    slugs = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        slugs.add(re.sub(r"[^\w\s-]", "", heading).strip().lower().replace(" ", "-"))
    return slugs


class LinkTests(unittest.TestCase):
    def test_the_scan_finds_something(self) -> None:
        # Guard the guard: every assertion below reports an empty list on
        # success, and an empty list is also what a broken scan produces.
        self.assertGreater(len(tracked_markdown()), 20)

    def test_every_relative_link_resolves(self) -> None:
        broken = []
        for doc in tracked_markdown():
            for match in LINK_RE.finditer(doc.read_text(encoding="utf-8")):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path = target.partition("#")[0]
                if not path:
                    continue
                if not (doc.parent / path).exists():
                    broken.append(f"{doc.relative_to(REPO_ROOT)} -> {target}")
        # The real example: docs/building-locally.md linked to
        # `docs/architecture-overview.md` from inside docs/, which resolves to
        # docs/docs/ and 404s on GitHub. It survived because nothing looked.
        self.assertEqual(broken, [], "relative links that do not resolve")

    def test_every_link_anchor_names_a_real_heading(self) -> None:
        broken = []
        for doc in tracked_markdown():
            for match in LINK_RE.finditer(doc.read_text(encoding="utf-8")):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, anchor = target.partition("#")
                if not anchor:
                    continue
                destination = doc if not path else doc.parent / path
                if not destination.exists() or destination.suffix != ".md":
                    continue
                if anchor not in heading_slugs(destination):
                    broken.append(f"{doc.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(broken, [], "link anchors naming no such heading")


def mapped_paths() -> set[str]:
    """
    Return the repository-relative paths the documentation map declares.

    The map is a directory tree: an unindented line ending in `/` opens a
    directory, indented lines below it are files in that directory, and an
    unindented line that is a filename is a repository-root file.

    Paths, not basenames. A basename comparison cannot see a file listed under
    the wrong directory, and the map had exactly that defect -- `quality.md` and
    `metrics.md` stranded below the `docs/reflections/` header, so the map
    claimed they lived there. Every basename existed somewhere, so a basename
    check passed while the map was wrong.
    """

    mapped: set[str] = set()
    directory = ""
    for line in DOC_MAP.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("```"):
            continue
        entry = line.split("<-")[0].strip()
        if not entry:
            continue
        indented = line.startswith(" ")
        if not indented and entry.endswith("/"):
            directory = entry
            continue
        if not entry.endswith(".md") and not entry.endswith(".mdc"):
            continue
        if indented:
            mapped.add(f"{directory}{entry}")
        else:
            directory = ""
            mapped.add(entry)
    return mapped


class DocumentationMapTests(unittest.TestCase):
    """
    docs/documentation-guide.md calls itself the map. A map missing a road, or
    showing one in the wrong place, is worse than no map -- it is consulted
    instead of looking.
    """

    def test_the_map_parses_into_something_plausible(self) -> None:
        mapped = mapped_paths()
        self.assertGreater(len(mapped), 20, f"implausibly small map: {mapped}")
        self.assertIn("docs/quality.md", mapped)
        self.assertIn("tests/e2e/README.md", mapped)

    def test_every_doc_appears_in_the_documentation_map(self) -> None:
        mapped = mapped_paths()
        actual = {
            str(path.relative_to(REPO_ROOT))
            for path in tracked_markdown()
            if path.parent == DOCS_DIR
        }
        self.assertEqual(
            sorted(actual - mapped),
            [],
            "documents in docs/ that the documentation guide's tree does not list at that path",
        )

    def test_the_map_places_every_entry_where_the_file_actually_is(self) -> None:
        # The direction that catches misplacement rather than omission.
        #
        # `.mdc` as well as `.md`: the map lists the Cursor rule file, which is
        # a document by every measure except its extension.
        listing = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "*.md", "*.mdc"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.split()
        actual = set(listing)
        # `YYYY-MM-DD-*.md` and `*.prompt.md` are patterns, not files.
        stale = sorted(
            path
            for path in mapped_paths() - actual
            if "YYYY-" not in path and "*" not in path
        )
        self.assertEqual(
            stale,
            [],
            "the documentation map lists these paths, but no such file exists there",
        )


class WorkflowCoverageTests(unittest.TestCase):
    def test_every_workflow_is_described_somewhere_in_the_docs(self) -> None:
        """
        A workflow nothing documents is one nobody knows runs.

        This caught nightly-compliance.yml: it was added, and docs/quality.md --
        the page whose entire subject is where the signal about this repository
        comes from -- did not mention it.
        """
        prose = "\n".join(doc.read_text(encoding="utf-8") for doc in tracked_markdown())
        undocumented = [
            path.name
            for path in workflow_paths()
            if path.name not in prose
        ]
        self.assertEqual(undocumented, [], "workflows named in no document")

    def test_quality_page_covers_every_workflow_that_can_fail_a_change(self) -> None:
        # Narrower and stricter than the check above: these four decide whether
        # a change is allowed to proceed, so the page a reader consults to
        # interpret a red run has to account for all of them.
        quality = QUALITY.read_text(encoding="utf-8")
        for workflow in ("build.yml", "build-pr.yml", "build-branch.yml", "test.yml"):
            with self.subTest(workflow=workflow):
                self.assertIn(workflow, quality)


# A glob or rglob call whose quoted pattern ends in `.yml`, the one spelling.
ONE_SPELLING_GLOB_RE = re.compile(r"\.r?glob\(\s*\"([^\"]*\.yml)\"\s*\)")

# Scans that read `.yml` alone on purpose, each with the test that guards the other spelling.
ONE_SPELLING_ALLOWED = {
    # test_no_template_uses_the_yaml_extension_github_ignores fails on any `.yaml` form.
    ("tests/test_issue_templates.py", "*.yml"),
}


def single_spelling_globs(text: str) -> list[tuple[int, str]]:
    """Each `.yml` glob pattern in `text`, by line number, whose `.yaml` partner is not quoted
    on the same line."""

    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in ONE_SPELLING_GLOB_RE.findall(line):
            if f'"{pattern[: -len(".yml")]}.yaml"' not in line:
                found.append((number, pattern))
    return found


def tree_single_spelling_globs() -> dict[tuple[str, str], list[int]]:
    """single_spelling_globs() over every Python file under tests/, keyed by (path, pattern)."""

    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for number, pattern in single_spelling_globs(path.read_text(encoding="utf-8")):
            found.setdefault((relative, pattern), []).append(number)
    return found


class YamlSpellingTests(unittest.TestCase):
    """
    GitHub runs a workflow, and loads a composite action, from either `.yml` or `.yaml`.
    Every scan over them reads its list from workflow_paths() or action_paths(), so those
    two helpers decide what the scans can see, and a helper that dropped one spelling would
    exempt every file written that way from every scan at once.
    """

    def _listing(self, helper, attribute: str, files: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in files:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("name: x\n", encoding="utf-8")
            with mock.patch.object(sys.modules[__name__], attribute, root):
                return [path.relative_to(root).as_posix() for path in helper()]

    def test_workflow_paths_lists_both_spellings_and_nothing_else(self) -> None:
        listed = self._listing(
            workflow_paths, "WORKFLOW_DIR", ["a.yml", "b.yaml", "notes.md", "nested/c.yml"]
        )
        self.assertEqual(listed, ["a.yml", "b.yaml"])

    def test_action_paths_lists_both_spellings_and_nothing_else(self) -> None:
        listed = self._listing(
            action_paths,
            "ACTION_DIR",
            [
                "one/action.yml",
                "two/action.yaml",
                "three/other.yml",
                "action.yml",
                "four/deep/action.yml",
                "five/action.yhtml",
            ],
        )
        self.assertEqual(listed, ["one/action.yml", "two/action.yaml"])

    def test_the_real_actions_are_all_listed(self) -> None:
        actions = action_paths()
        self.assertGreater(len(actions), 1)
        self.assertEqual(
            {path.parent for path in actions},
            {path for path in ACTION_DIR.iterdir() if path.is_dir()},
            "a directory under .github/actions has no action.yml or action.yaml",
        )

    def test_the_detector_flags_only_a_glob_left_without_its_partner(self) -> None:
        def call(verb: str, pattern: str) -> str:
            return f'DIR.{verb}("{pattern}")'

        flagged = {
            call("glob", "*.yml"): [(1, "*.yml")],
            call("rglob", "*/action.yml"): [(1, "*/action.yml")],
            # The partner has to be a quoted pattern, not the word in a comment.
            call("glob", "*.yml") + "  # *.yaml is rare": [(1, "*.yml")],
            f'[*{call("glob", "*.yml")}, *{call("glob", "*.yaml")}]': [],
            f'[*{call("glob", "*/action.yml")}, *{call("glob", "*/action.yaml")}]': [],
            call("glob", "*.md"): [],
            call("glob", "*.yaml"): [],
        }
        for line, expected in flagged.items():
            with self.subTest(line=line):
                self.assertEqual(single_spelling_globs(line), expected)
        self.assertEqual(single_spelling_globs("x = 1\n" + call("glob", "*.yml")), [(2, "*.yml")])

    def test_no_test_globs_a_single_yaml_spelling(self) -> None:
        offenders = sorted(set(tree_single_spelling_globs()) - ONE_SPELLING_ALLOWED)
        self.assertEqual(
            offenders,
            [],
            "these scans read one YAML spelling; use workflow_paths() or action_paths()",
        )

    def test_the_single_spelling_allowance_is_still_needed(self) -> None:
        # An allowance whose glob is gone would quietly excuse the next one written there.
        stale = sorted(ONE_SPELLING_ALLOWED - set(tree_single_spelling_globs()))
        self.assertEqual(stale, [], f"allowed, but no longer in the tree: {stale}")


if __name__ == "__main__":
    unittest.main()
