"""
Script: tests/test_documentation_guide_doc.py
What: Joins docs/documentation-guide.md -- the page that calls itself "the map of the
documentation itself" -- to the tree it maps and to the files its prose describes.
Doing: Parses the page's documentation tree into paths and patterns and compares it to every
tracked Markdown file in the repository, in both directions; checks each description the page
gives a file against that file (the slash commands point at prompts, the pointer files send the
reader to AGENTS.md section 0, the prompts cite rules by number, the corrections carry the four
fields the page names, rule 3 is the rule the page says it is); and checks that every reading
path and placement rule is a contiguous numbered list of links to mapped documents.
Why: Six tests under `tests/` open this page, and all six read it as a SOURCE -- to confirm a
link to some other page is on it, or that a docs/ file is listed at the right path. Nothing read
it as a SUBJECT, and the existing omission check in tests/test_docs_consistency.py looks only
at files directly under docs/. Two claims had drifted when this file was written.
Goal: Make a new document nobody put on the map, or a sentence about a file that stopped being
true of it, fail here instead of sending a reader somewhere the map does not show.

The two stale claims this file was written against:

  * The tree left out `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
    `.github/pull_request_template.md` and the three slash-command files under
    `.claude/commands/`. `AGENTS.md` is the file the page's own prose, both pointer files and
    all three prompt files send the reader to, and the map never showed where it was.
  * "They link to `AGENTS.md` and `docs/`" about the prompt files. No prompt file has a link to
    `AGENTS.md`; each one cites a section 0 rule by number in plain text, and two of the three
    name nothing under docs/ at all.

No PyYAML and no third-party parser, for the reason tests/test_docs_consistency.py gives.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import unittest
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "documentation-guide.md"
AGENTS = REPO_ROOT / "AGENTS.md"
CORRECTIONS = REPO_ROOT / ".claude" / "memory" / "corrections.md"
REFLECTIONS_README = REPO_ROOT / "docs" / "reflections" / "README.md"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
COPILOT = REPO_ROOT / ".github" / "copilot-instructions.md"
CURSOR_RULE = REPO_ROOT / ".cursor" / "rules" / "zfs-kinoite-complex.mdc"
PROMPTS_DIR = REPO_ROOT / ".github" / "prompts"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
NUMBERED_RE = re.compile(r"^(\d+)\. (.*)$")
RULE_CITATION_RE = re.compile(r"AGENTS\.md\s+section\s+0\s+rule\s+(\d+)")

# The two files the page calls "pointers, not documents".
POINTER_FILES = (COPILOT, CURSOR_RULE)


def _squash(text: str) -> str:
    """Collapse the whitespace Markdown wrapping puts inside a sentence."""

    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str) -> str:
    """Return the body of the `## heading` section, up to the next `## `."""

    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise AssertionError(f"docs/documentation-guide.md has no '## {heading}' section")
    return match.group(1)


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def tree_entries() -> list[tuple[str, str]]:
    """
    Return `(path_or_pattern, description)` for every file line of the documentation tree.

    Same grammar tests/test_docs_consistency.py parses: an unindented line ending in `/` opens a
    directory, an indented line is a file in the open directory, and an unindented filename is a
    path from the repository root. Unlike that parser this keeps pattern lines (`*.md`) and the
    description after `<-`, because both are claims.
    """

    fence = re.search(r"```text\n(.*?)```", _section(doc_text(), "Documentation Tree"), re.DOTALL)
    if fence is None:
        raise AssertionError("the Documentation Tree section has no ```text fence")
    entries: list[tuple[str, str]] = []
    directory = ""
    for line in fence.group(1).splitlines():
        if not line.strip():
            continue
        left, _, description = line.partition("<-")
        entry = left.strip()
        if not line.startswith(" ") and entry.endswith("/"):
            directory = entry
            continue
        if line.startswith(" "):
            entries.append((f"{directory}{entry}", description.strip()))
        else:
            directory = ""
            entries.append((entry, description.strip()))
    return entries


def tracked_documents() -> set[str]:
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.md", "*.mdc"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()
    return set(listing)


def _is_pattern(entry: str) -> bool:
    return "*" in entry or "YYYY" in entry


def _pattern_matches(entry: str, tracked: set[str]) -> set[str]:
    """Tracked files a pattern line covers: same directory, basename matches the glob."""

    parent = str(PurePosixPath(entry).parent)
    glob = PurePosixPath(entry).name.replace(
        "YYYY-MM-DD", "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    )
    return {
        path
        for path in tracked
        if str(PurePosixPath(path).parent) == parent
        and fnmatch.fnmatchcase(PurePosixPath(path).name, glob)
    }


def described(entry: str) -> str:
    for path, description in tree_entries():
        if path == entry:
            return description
    raise AssertionError(f"the documentation tree has no line for {entry}")


def section_0_rules() -> dict[int, str]:
    """AGENTS.md section 0's numbered rules, continuation lines folded in."""

    text = AGENTS.read_text(encoding="utf-8")
    body = re.search(r"^## 0\..*?\n(.*?)(?=^## 1\.)", text, re.MULTILINE | re.DOTALL)
    if body is None:
        raise AssertionError("AGENTS.md has no '## 0.' section followed by '## 1.'")
    rules: dict[int, str] = {}
    current: int | None = None
    for line in body.group(1).splitlines():
        numbered = NUMBERED_RE.match(line)
        if numbered:
            current = int(numbered.group(1))
            rules[current] = numbered.group(2)
        elif current is not None and line.startswith("   ") and line.strip():
            rules[current] += " " + line.strip()
        elif not line.strip():
            current = None
    return rules


def numbered_links(block: str) -> list[tuple[int, list[str]]]:
    """`(number, [link targets])` for each item of a numbered list, continuation lines folded."""

    items: list[tuple[int, list[str]]] = []
    for line in block.splitlines():
        numbered = NUMBERED_RE.match(line)
        if numbered:
            items.append((int(numbered.group(1)), LINK_RE.findall(numbered.group(2))))
        elif items and line.startswith("   ") and line.strip():
            items[-1][1].extend(LINK_RE.findall(line))
    return items


def _resolve(target: str) -> str:
    """A link from docs/documentation-guide.md, as a repository-relative path."""

    path = (DOC.parent / target.split("#")[0]).resolve()
    return str(path.relative_to(REPO_ROOT))


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        return text.split("\n---\n", 1)[1]
    return text


class TheMapCoversTheTreeTests(unittest.TestCase):
    """The page's first claim is that it is the map. Check it against every document, not only docs/."""

    def test_the_tree_parses_into_something_plausible(self) -> None:
        entries = [path for path, _ in tree_entries()]
        self.assertGreater(len(entries), 30, entries)
        self.assertIn("README.md", entries)
        self.assertIn(".github/prompts/*.prompt.md", entries)

    def test_every_tracked_document_is_on_the_map(self) -> None:
        tracked = tracked_documents()
        covered: set[str] = set()
        for entry, _ in tree_entries():
            if _is_pattern(entry):
                covered |= _pattern_matches(entry, tracked)
            else:
                covered.add(entry)
        self.assertEqual(
            sorted(tracked - covered),
            [],
            "tracked documents the documentation tree neither lists nor covers with a pattern",
        )

    def test_every_pattern_on_the_map_covers_a_file_nothing_else_lists(self) -> None:
        # A pattern that matches only files listed by name is a line that maps nothing, and one
        # that matches nothing at all is a road to nowhere.
        tracked = tracked_documents()
        named = {entry for entry, _ in tree_entries() if not _is_pattern(entry)}
        for entry, _ in tree_entries():
            if not _is_pattern(entry):
                continue
            with self.subTest(pattern=entry):
                self.assertTrue(
                    _pattern_matches(entry, tracked) - named, f"{entry} covers nothing new"
                )

    def test_no_line_on_the_map_is_listed_twice(self) -> None:
        entries = [path for path, _ in tree_entries()]
        self.assertEqual(sorted({p for p in entries if entries.count(p) > 1}), [])

    def test_every_line_on_the_map_carries_a_description(self) -> None:
        # README.md is the one bare entry: it is the front door and needs no gloss.
        missing = [path for path, description in tree_entries() if not description]
        self.assertEqual(missing, ["README.md"])


class TheTreeDescriptionsTests(unittest.TestCase):
    """Each `<-` description is a claim about one file. These are the ones a machine can settle."""

    def test_every_agent_instruction_file_carries_a_section_0(self) -> None:
        agent_files = [path for path, description in tree_entries() if "section 0" in description]
        self.assertEqual(sorted(agent_files), ["AGENTS.md", "CLAUDE.md", "GEMINI.md"])
        for path in agent_files:
            with self.subTest(path=path):
                text = (REPO_ROOT / path).read_text(encoding="utf-8")
                self.assertRegex(text, re.compile(r"^## 0\. ", re.MULTILINE))

    def test_the_two_copies_name_the_model_their_file_is_for(self) -> None:
        for path, model in (("CLAUDE.md", "Claude"), ("GEMINI.md", "Gemini")):
            with self.subTest(path=path):
                self.assertIn(f"worded for {model}", described(path))
                header = (REPO_ROOT / path).read_text(encoding="utf-8").split("## 0.")[0]
                self.assertIn(model if model != "Claude" else "CLAUDE.md", header)

    def test_the_prompt_pattern_counts_the_procedures_it_names(self) -> None:
        entry = ".github/prompts/*.prompt.md"
        description = described(entry)
        named = description.split(":", 1)[1].split(",")
        prompts = _pattern_matches(entry, tracked_documents())
        self.assertEqual(len(named), len(prompts), f"{description!r} vs {sorted(prompts)}")

    def test_each_slash_command_points_at_exactly_one_prompt_and_covers_them_all(self) -> None:
        self.assertIn("each pointing at one prompt file", described(".claude/commands/*.md"))
        self.assertIn("thin pointers at .github/prompts/", described(".claude/commands/README.md"))
        commands = sorted(p for p in COMMANDS_DIR.glob("*.md") if p.name != "README.md")
        self.assertTrue(commands)
        pointed: list[str] = []
        for command in commands:
            links = {
                _link
                for _link in LINK_RE.findall(command.read_text(encoding="utf-8"))
                if _link.endswith(".prompt.md")
            }
            with self.subTest(command=command.name):
                self.assertEqual(len(links), 1, f"{command.name} links {sorted(links)}")
                target = (command.parent / links.pop()).resolve()
                self.assertTrue(target.is_file(), target)
                self.assertEqual(target.parent, PROMPTS_DIR.resolve())
                pointed.append(target.name)
        self.assertEqual(sorted(pointed), sorted(p.name for p in PROMPTS_DIR.glob("*.prompt.md")))

    def test_the_settings_file_really_has_a_deny_list(self) -> None:
        self.assertIn("the deny list is the safety boundary", described(".claude/settings.json"))
        deny = json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"]["deny"]
        self.assertGreater(len(deny), 0)

    def test_the_cursor_rule_really_is_a_short_form(self) -> None:
        self.assertIn("the same short form", described(".cursor/rules/zfs-kinoite-complex.mdc"))
        agents = len(AGENTS.read_text(encoding="utf-8").splitlines())
        for pointer in POINTER_FILES:
            with self.subTest(pointer=pointer.name):
                self.assertLess(len(pointer.read_text(encoding="utf-8").splitlines()), agents)


class TheProseTests(unittest.TestCase):
    """The paragraphs under the tree each describe files that live elsewhere."""

    def test_both_pointer_files_send_the_reader_to_section_0_first(self) -> None:
        prose = _squash(_section(doc_text(), "Documentation Tree"))
        self.assertIn("Both say to read [`../AGENTS.md`](../AGENTS.md) section 0 first", prose)
        for pointer in POINTER_FILES:
            with self.subTest(pointer=pointer.name):
                opening = _squash(strip_frontmatter(pointer.read_text(encoding="utf-8")))[:600]
                self.assertRegex(opening, r"Read \[?`?AGENTS\.md`?(?:\]\([^)]*\))? section 0 first")
                self.assertRegex(opening, r"deliberately (?:does|do) not restate")

    def test_every_prompt_cites_a_real_section_0_rule_by_number(self) -> None:
        prose = _squash(_section(doc_text(), "Documentation Tree"))
        self.assertIn(
            "They cite `AGENTS.md` section 0 by rule number rather than restating it", prose
        )
        rules = section_0_rules()
        prompts = sorted(PROMPTS_DIR.glob("*.prompt.md"))
        self.assertTrue(prompts)
        for prompt in prompts:
            cited = {
                int(n)
                for n in RULE_CITATION_RE.findall(_squash(prompt.read_text(encoding="utf-8")))
            }
            with self.subTest(prompt=prompt.name):
                self.assertTrue(cited, f"{prompt.name} cites no AGENTS.md section 0 rule")
                self.assertLessEqual(cited, set(rules))

    def test_no_sentence_claims_the_prompts_link_to_agents_md(self) -> None:
        # The sentence this file replaced. Nothing in a prompt is a link to AGENTS.md.
        self.assertNotRegex(_squash(doc_text()), r"They link to `AGENTS\.md`")
        for prompt in PROMPTS_DIR.glob("*.prompt.md"):
            links = LINK_RE.findall(prompt.read_text(encoding="utf-8"))
            with self.subTest(prompt=prompt.name):
                self.assertFalse([t for t in links if t.endswith("AGENTS.md")])

    def test_every_docs_link_in_a_prompt_resolves(self) -> None:
        for prompt in PROMPTS_DIR.glob("*.prompt.md"):
            for target in LINK_RE.findall(prompt.read_text(encoding="utf-8")):
                if "docs/" not in target:
                    continue
                with self.subTest(prompt=prompt.name, target=target):
                    self.assertTrue((prompt.parent / target.split("#")[0]).is_file())

    def test_the_reflections_readme_has_the_table_that_tells_the_two_apart(self) -> None:
        self.assertIn("Its own README has the table", _squash(doc_text()))
        rows = [
            line
            for line in REFLECTIONS_README.read_text(encoding="utf-8").splitlines()
            if line.startswith("|") and not line.startswith("| ---")
        ]
        firsts = [row.split("|")[1].strip() for row in rows]
        self.assertTrue(any("corrections.md" in cell for cell in firsts), firsts)
        self.assertIn("This directory", firsts)

    def test_every_correction_carries_exactly_the_four_fields_the_page_names(self) -> None:
        match = re.search(r"citable -- ([a-z, ]+) -- and fixes one fact", _squash(doc_text()))
        self.assertIsNotNone(match, "the 'short and citable -- ... --' sentence moved")
        assert match is not None
        fields = {f.strip().lower() for f in match.group(1).split(",")}
        self.assertEqual(len(fields), 4)
        entries = re.split(r"^## ", CORRECTIONS.read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]
        self.assertTrue(entries)
        for entry in entries:
            labels = {
                label.lower()
                for label in re.findall(r"^\*\*([A-Za-z ]+):\*\*", entry, re.MULTILINE)
            }
            with self.subTest(entry=entry.splitlines()[0]):
                self.assertEqual(labels, fields)

    def test_the_rule_the_corrections_paragraph_cites_is_the_verify_rule(self) -> None:
        match = re.search(r"practical form of AGENTS\.md section 0 rule (\d+)", _squash(doc_text()))
        self.assertIsNotNone(match)
        assert match is not None
        rule = section_0_rules()[int(match.group(1))]
        self.assertIn("Verify claims against reality", rule)


class TheReadingPathsTests(unittest.TestCase):
    """Each goal and each placement rule is a numbered list of links. Every road must be on the map."""

    def _mapped(self) -> set[str]:
        return {entry for entry, _ in tree_entries() if not _is_pattern(entry)}

    def test_every_reading_path_is_contiguous_and_leads_only_to_mapped_documents(self) -> None:
        block = _section(doc_text(), "What To Read First (By Goal)")
        goals = re.split(r"^### ", block, flags=re.MULTILINE)[1:]
        self.assertGreaterEqual(len(goals), 7)
        for goal in goals:
            items = numbered_links(goal)
            with self.subTest(goal=goal.splitlines()[0]):
                self.assertEqual([n for n, _ in items], list(range(1, len(items) + 1)))
                for _, links in items:
                    self.assertTrue(links)
                    self.assertLessEqual({_resolve(t) for t in links}, self._mapped())

    def test_every_placement_rule_is_contiguous_and_names_mapped_documents(self) -> None:
        items = numbered_links(_section(doc_text(), "Where To Put New Documentation"))
        self.assertGreaterEqual(len(items), 10)
        self.assertEqual([n for n, _ in items], list(range(1, len(items) + 1)))
        for number, links in items:
            with self.subTest(item=number):
                self.assertTrue(links)
                self.assertLessEqual({_resolve(t) for t in links}, self._mapped())


if __name__ == "__main__":
    unittest.main()
