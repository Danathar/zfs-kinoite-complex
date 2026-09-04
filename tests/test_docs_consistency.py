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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DOCS_DIR = REPO_ROOT / "docs"
DOC_MAP = DOCS_DIR / "documentation-guide.md"
QUALITY = DOCS_DIR / "quality.md"

# Markdown inline links. Bare `<http://...>` autolinks and reference-style
# definitions are not used in this tree, so this is the whole surface.
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
            for path in sorted(
                list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))
            )
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


if __name__ == "__main__":
    unittest.main()
