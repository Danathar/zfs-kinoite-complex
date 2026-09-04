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


class DocumentationMapTests(unittest.TestCase):
    """
    docs/documentation-guide.md calls itself the map. A map missing a road is
    worse than no map, because it is consulted instead of looking.
    """

    def test_every_doc_appears_in_the_documentation_map(self) -> None:
        listed = set(re.findall(r"([A-Za-z0-9_.-]+\.md)", DOC_MAP.read_text(encoding="utf-8")))
        actual = {path.name for path in DOCS_DIR.glob("*.md")}
        self.assertEqual(
            sorted(actual - listed),
            [],
            "documents in docs/ that the documentation guide's tree does not list",
        )

    def test_the_map_does_not_name_documents_that_are_gone(self) -> None:
        # `\.md(?![A-Za-z])` and not `\.md`: the map also lists
        # `zfs-kinoite-complex.mdc`, and an unanchored suffix match happily
        # reads that as a `.md` file that does not exist.
        listed = set(
            re.findall(
                r"^\s{2,}([A-Za-z0-9_.-]+\.md)(?![A-Za-z])",
                DOC_MAP.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        # Every tracked markdown file, not just docs/ -- the map deliberately
        # also lists co-located READMEs and the files under .claude/.
        actual = {path.name for path in tracked_markdown()}
        # `YYYY-MM-DD-*.md` in the reflections block is a pattern, not a file.
        stale = sorted(name for name in listed - actual if not name.startswith("YYYY-"))
        self.assertEqual(stale, [], "the documentation map names files that no longer exist")


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
            path.name for path in sorted(WORKFLOW_DIR.glob("*.yml")) if path.name not in prose
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
