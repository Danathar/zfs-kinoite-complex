"""
Script: tests/test_review_rubric_doc.py
What: Joins docs/review-rubric.md to the tree it tells a reviewer to check -- AGENTS.md
section 0, .github/labeler.yml, .github/prompts/review-safety-critical-change.prompt.md,
renovate.json, tests/check_coverage.py, .claude/memory/corrections.md and the pages that
cite this one by section number.
Doing: Parses the page's verdict table, its numbered sections and their bullets, then
recomputes each claim from the tree instead of restating it: the seven safety-critical
paths come out of AGENTS.md rule 2 and are compared against the labeler globs and the
prompt file; the fail-closed signals in the prompt are checked against this page's own
list; the section numbers .github/labeler.yml and docs/SECURITY-AI.md cite are parsed out
of those files and resolved here; the rollback claim is recomputed from renovate.json and
the coverage-floor claim by running tests/check_coverage.py's real gate.
Why: This is the page README.md, CONTRIBUTING.md, docs/risk-tiers.md, docs/SECURITY-AI.md,
docs/metrics.md and .github/workflows/ai-fix.yml all send a reviewer to, and no test at any
tier opened it: `grep -rn review-rubric tests/` returned one hit, a sentence inside
tests/test_labeler_config.py's docstring. The end-to-end suite (tests/e2e/) drives the
ci_tools CLIs and opens no docs/ file, and the four `--cov` paths in
.github/workflows/test.yml are all Python, so no coverage number at any tier could have
shown the hole. Two config files cite this page *by section number*, which means renumbering
a heading falsifies them silently. Committed sentences were falsified one at a time (seven
-> eight files, section 2's verdict Blocking -> Comment, renovate's openzfs automerge false
-> true, the prompt link retargeted, `/tmp/akmods` dropped from section 5) and the suite
stayed green every time.
Goal: Make the page fail here when the tree moves under it, in both directions.

Parses Markdown by hand. CI installs pytest, pytest-cov and ruff and nothing else (see
.github/workflows/test.yml), so a third-party Markdown parser here would skip in exactly the
place these assertions are meant to run. `_sections`, `_verdicts`, `_bullets`, `_code_spans`,
`_numbered_rules` and `_repo_paths` are the whole parser and carry their own case table in
`ParserTests` below, because a hand-rolled parser that is never wrong about a fixture is the
only thing keeping the assertions built on it honest.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "review-rubric.md"
AGENTS = REPO_ROOT / "AGENTS.md"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
SECURITY_AI = REPO_ROOT / "docs" / "SECURITY-AI.md"
METRICS = REPO_ROOT / "docs" / "metrics.md"
DOC_GUIDE = REPO_ROOT / "docs" / "documentation-guide.md"
LABELER_CONFIG = REPO_ROOT / ".github" / "labeler.yml"
PROMPT = REPO_ROOT / ".github" / "prompts" / "review-safety-critical-change.prompt.md"
RENOVATE = REPO_ROOT / "renovate.json"
THRESHOLDS = REPO_ROOT / ".coverage-thresholds.json"
CORRECTIONS = REPO_ROOT / ".claude" / "memory" / "corrections.md"
CHECK_COVERAGE = REPO_ROOT / "tests" / "check_coverage.py"

# The pages that send a reader here, with the phrase each uses. The rubric is only
# reachable because these link it; a rename or a reframing that left them behind would
# strand the page this module exists to keep honest.
ENTRY_POINTS = (README, CONTRIBUTING, DOC_GUIDE, SECURITY_AI, METRICS)

# Number words the page actually uses for a count of files. Spelled out here rather than
# imported from a library because the assertion is about one word in one sentence.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

# `/tmp/akmods` is in the page as an example of what a test must NOT reach (section 5), so
# it is the one path-shaped span that must not exist. Excluded by the leading slash rather
# than by name, because any absolute path in that sentence means the same thing.
CODE_SPAN = re.compile(r"`([^`\n]+)`")
HEADING = re.compile(r"^## (?:(\d+)\. )?(.+?)(?: — \*(.+?)\*)?$")
TABLE_VERDICT = re.compile(r"^\| \*\*(.+?)\*\* \| (.+?) \|$")
NUMBERED_RULE = re.compile(r"^(\d+)\. \*\*(.+?)\*\*", re.MULTILINE)
PATH_LIKE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?$")


def _sections(text: str) -> list[dict[str, object]]:
    """
    Return one entry per `##` heading: number (or None), title, verdict (or None), body.

    The page's headings are `## N. Title — *Verdict*`, with two unnumbered sections (the
    verdict table and the closing note). Split on the heading line rather than on blank
    lines so a bullet list inside a section stays with it.
    """

    found: list[dict[str, object]] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match is None:
            if found:
                found[-1]["body"] = f"{found[-1]['body']}\n{line}"
            continue
        number, title, verdict = match.groups()
        found.append(
            {
                "number": int(number) if number else None,
                "title": title.strip(),
                "verdict": verdict.strip() if verdict else None,
                "body": "",
            }
        )
    return found


def _verdicts(text: str) -> dict[str, str]:
    """Return `{verdict: meaning}` from the "How to read the verdicts" table."""

    table: dict[str, str] = {}
    for line in text.splitlines():
        match = TABLE_VERDICT.match(line)
        if match is not None:
            table[match.group(1).strip()] = match.group(2).strip()
    return table


def _bullets(body: str) -> list[str]:
    """
    Return the top-level `- ` bullets of a section, each joined into one line.

    Continuation lines are indented, so a bullet ends at the next `- ` or at a line that is
    neither indented nor blank.
    """

    items: list[str] = []
    for line in body.splitlines():
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items and line.startswith("  ") and line.strip():
            items[-1] = f"{items[-1]} {line.strip()}"
        elif not line.strip():
            continue
        elif items:
            break
    return items


def _code_spans(text: str) -> list[str]:
    """Return every backticked span, in order, duplicates kept."""

    return CODE_SPAN.findall(text)


def _numbered_rules(text: str, heading_prefix: str) -> dict[int, str]:
    """
    Return `{rule number: rule body}` for the numbered rules under a `##` heading.

    Used on AGENTS.md section 0, whose rules this page cites by number. The section's own
    prose carries bullets but no `N. **...**` line, so the pattern is specific enough.
    """

    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"## {heading_prefix}")),
        None,
    )
    if start is None:
        return {}
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    section = "\n".join(lines[start:end])

    rules: dict[int, str] = {}
    matches = list(NUMBERED_RULE.finditer(section))
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        rules[int(match.group(1))] = section[match.start() : stop]
    return rules


def _repo_paths(spans: list[str]) -> list[str]:
    """
    Return the spans that name a path in this repository.

    A span qualifies when it is path-shaped and either contains a `/` or carries a file
    extension. `raise`, `in`, `or` and `ruff` fail both tests; `if: always()`,
    `assertRaises(CiToolError)` and `chatgpt-codex-connector[bot]` fail the shape. A leading
    `/` disqualifies: section 5 names `/tmp/akmods` as something a test must not reach, so
    that span is deliberately not a path in the tree.
    """

    paths = []
    for span in spans:
        if span.startswith("/") or not PATH_LIKE.match(span):
            continue
        if "/" in span or re.search(r"\.[a-z0-9]+$", span):
            paths.append(span)
    return sorted(set(paths))


def _tracked_files() -> list[str]:
    """Every tracked path, from git. The same idiom tests/check_coverage.py uses."""

    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if line.strip()]


def _resolve(path: str, tracked: list[str]) -> str:
    """
    Resolve a path as written to the tracked path it names.

    AGENTS.md rule 2 writes `build.yml` where .github/labeler.yml writes
    `.github/workflows/build.yml`. Matching on the suffix lets the three lists be compared
    as sets without either file having to spell the other's form. A suffix that matches more
    than one tracked file is ambiguous and is returned unchanged, which fails the comparison
    loudly rather than picking one.
    """

    if path in tracked or (REPO_ROOT / path).exists():
        return path
    hits = [candidate for candidate in tracked if candidate.endswith(f"/{path}")]
    return hits[0] if len(hits) == 1 else path


def _load_check_coverage():
    """
    Load tests/check_coverage.py by path, under its own module name.

    It is a script rather than a package member, and loading it here must not collide with
    the module object tests/test_check_coverage.py builds.
    """

    spec = importlib.util.spec_from_file_location("check_coverage_review_rubric", CHECK_COVERAGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOC_TEXT = DOC.read_text(encoding="utf-8")
SECTIONS = _sections(DOC_TEXT)
NUMBERED = {section["number"]: section for section in SECTIONS if section["number"]}


class ParserTests(unittest.TestCase):
    """
    Fixtures for the parser above. Every assertion in this module is built on these five
    functions, so a parser that is quietly wrong would make the rest of the file agree with
    itself and with nothing else.
    """

    def test_sections_split_on_headings_and_keep_the_verdict(self) -> None:
        parsed = _sections(
            textwrap.dedent(
                """\
                # Title

                intro

                ## How to read it

                table

                ## 1. First question? — *Blocking*

                body one
                more body

                ## 2. Second — *Comment, unless it matters*

                body two
                """
            )
        )
        self.assertEqual([s["number"] for s in parsed], [None, 1, 2])
        self.assertEqual(parsed[1]["title"], "First question?")
        self.assertEqual(parsed[1]["verdict"], "Blocking")
        self.assertEqual(parsed[2]["verdict"], "Comment, unless it matters")
        self.assertIn("more body", parsed[1]["body"])
        self.assertNotIn("body two", parsed[1]["body"])

    def test_sections_ignore_a_heading_that_is_not_level_two(self) -> None:
        parsed = _sections("# One\n\n### Three\n\n## 1. Real — *Blocking*\n\nbody\n")
        self.assertEqual([section["title"] for section in parsed], ["Real"])
        self.assertIn("body", parsed[0]["body"])

    def test_verdict_table_reads_the_bold_column(self) -> None:
        table = _verdicts(
            "| Verdict | Meaning |\n"
            "| --- | --- |\n"
            "| **Blocking** | Does not merge. |\n"
            "| **Comment** | Worth saying. |\n"
        )
        self.assertEqual(table, {"Blocking": "Does not merge.", "Comment": "Worth saying."})

    def test_bullets_join_continuations_and_stop_at_prose(self) -> None:
        self.assertEqual(
            _bullets("lead in\n\n- first\n- second\n  wrapped\n\nclosing prose\n\n- later\n"),
            ["first", "second wrapped"],
        )

    def test_code_spans_keep_order_and_duplicates(self) -> None:
        self.assertEqual(_code_spans("a `x` b `y` c `x`"), ["x", "y", "x"])

    def test_numbered_rules_split_a_section_into_its_rules(self) -> None:
        rules = _numbered_rules(
            textwrap.dedent(
                """\
                ## 0. Heading

                prose with a - bullet

                1. **First rule.** body one
                2. **Second rule.** body two
                   continued

                ## 1. Next section

                3. **Not a rule here.** ignored
                """
            ),
            "0.",
        )
        self.assertEqual(sorted(rules), [1, 2])
        self.assertIn("body two", rules[2])
        self.assertNotIn("Not a rule here", rules[2])

    def test_repo_paths_keeps_paths_and_drops_prose_and_absolutes(self) -> None:
        self.assertEqual(
            _repo_paths(
                [
                    "AGENTS.md",
                    "ci_tools/sign_image.py",
                    ".cursor/rules/",
                    "raise",
                    "ruff",
                    "==",
                    "|| true",
                    "if: always()",
                    "assertRaises(CiToolError)",
                    "chatgpt-codex-connector[bot]",
                    "/tmp/akmods",
                ]
            ),
            [".cursor/rules/", "AGENTS.md", "ci_tools/sign_image.py"],
        )


class SectionStructureTests(unittest.TestCase):
    """
    The page's own shape. Two config files cite it by section number, so a renumbered or
    unverdicted heading is a defect here and not a matter of taste.
    """

    def test_the_page_parses_into_numbered_sections(self) -> None:
        self.assertTrue(NUMBERED, "no numbered sections parsed out of docs/review-rubric.md")

    def test_sections_are_numbered_contiguously_from_one(self) -> None:
        numbers = sorted(NUMBERED)
        self.assertEqual(
            numbers,
            list(range(1, len(numbers) + 1)),
            f"section numbers are {numbers}; a gap or a duplicate breaks every citation by number",
        )

    def test_every_numbered_heading_carries_a_verdict_the_table_defines(self) -> None:
        table = _verdicts(DOC_TEXT)
        self.assertTrue(table, "the 'How to read the verdicts' table did not parse")
        for number, section in sorted(NUMBERED.items()):
            with self.subTest(section=number):
                verdict = section["verdict"]
                self.assertIsNotNone(verdict, f"section {number} has no verdict on its heading")
                self.assertTrue(
                    any(verdict.startswith(defined) for defined in table),
                    f"section {number}'s verdict {verdict!r} is not one of {sorted(table)}",
                )

    def test_every_defined_verdict_is_used_by_a_heading(self) -> None:
        used = [section["verdict"] or "" for section in NUMBERED.values()]
        for defined in _verdicts(DOC_TEXT):
            with self.subTest(verdict=defined):
                self.assertTrue(
                    any(verdict.startswith(defined) for verdict in used),
                    f"the table defines {defined!r} but no section carries it",
                )

    def test_the_minimal_review_names_sections_that_exist(self) -> None:
        """
        The closing note tells a reviewer which sections are never skippable and which two
        are the whole review for a documentation change. Those numbers are read off this
        page's own headings, so they rot the moment a section moves.
        """

        closing = next(
            section for section in SECTIONS if section["title"].startswith("A minimal review")
        )
        cited = {
            int(number)
            for phrase in re.findall(r"[Ss]ections? ([\d,\s]*\d)(?: and (\d+))?", closing["body"])
            for chunk in phrase
            if chunk
            for number in re.findall(r"\d+", chunk)
        }
        self.assertTrue(cited, "the closing note cites no section numbers any more")
        for number in sorted(cited):
            with self.subTest(section=number):
                self.assertIn(
                    number,
                    NUMBERED,
                    f"the minimal review sends a reviewer to section {number}, which does not exist",
                )


class SafetyCriticalFileListTests(unittest.TestCase):
    """
    Section 2's list of files lives in four places: AGENTS.md rule 2 is the source, and
    .github/labeler.yml, the prompt file and this page each restate it. This page states
    only the count, so the count is what is checked here -- and the three lists against
    each other.
    """

    def setUp(self) -> None:
        self.tracked = _tracked_files()
        self.rules = _numbered_rules(AGENTS.read_text(encoding="utf-8"), "0.")
        self.assertIn(2, self.rules, "AGENTS.md section 0 has no rule 2 to name the files")
        self.rule_paths = {
            _resolve(path, self.tracked) for path in _repo_paths(_code_spans(self.rules[2]))
        }

    def test_agents_rule_two_still_names_a_list_of_files(self) -> None:
        self.assertGreater(len(self.rule_paths), 1, f"rule 2 parsed as {sorted(self.rule_paths)}")

    def test_the_count_this_page_states_matches_agents_rule_two(self) -> None:
        section = NUMBERED[2]
        match = re.search(
            r"(\w+) files in\s+`?AGENTS\.md`? section 0 rule 2", section["body"], re.IGNORECASE
        )
        self.assertIsNotNone(
            match, "section 2 no longer states how many files AGENTS.md rule 2 names"
        )
        word = match.group(1).lower()
        self.assertIn(word, NUMBER_WORDS, f"unrecognised count word {word!r} in section 2")
        self.assertEqual(
            NUMBER_WORDS[word],
            len(self.rule_paths),
            f"section 2 says {word} files; AGENTS.md rule 2 names "
            f"{len(self.rule_paths)}: {sorted(self.rule_paths)}",
        )

    def test_every_safety_critical_path_exists(self) -> None:
        for path in sorted(self.rule_paths):
            with self.subTest(path=path):
                self.assertTrue(
                    (REPO_ROOT / path).exists(),
                    f"AGENTS.md rule 2 names {path}, which is not in the tree",
                )

    def test_the_labeler_globs_name_the_same_files(self) -> None:
        """
        `area/safety-critical` is the label that makes the missing-statement case visible on
        a pull request, which is what section 2 treats as blocking. If a file rule 2 names
        is missing from the globs, a change to it arrives unlabelled and section 2 is applied
        to nothing. Containment rather than equality: the label deliberately covers two more
        paths (`cosign.pub`, `ci/defaults.json`), each with its reason in a comment there.
        """

        text = LABELER_CONFIG.read_text(encoding="utf-8")
        block = text.split("'area/safety-critical':", 1)
        self.assertEqual(len(block), 2, ".github/labeler.yml no longer defines area/safety-critical")
        globs = set()
        for line in block[1].splitlines():
            stripped = line.strip()
            if stripped.startswith("- '") and stripped.endswith("'"):
                globs.add(_resolve(stripped[3:-1].removesuffix("/**"), self.tracked))
            elif stripped.startswith("'") and stripped.endswith("':"):
                break
        self.assertEqual(
            self.rule_paths - globs,
            set(),
            "area/safety-critical no longer covers every file AGENTS.md section 0 rule 2 names",
        )

    def test_the_prompt_file_names_the_same_files(self) -> None:
        prompt_paths = set()
        for bullet in _bullets(PROMPT.read_text(encoding="utf-8")):
            for path in _repo_paths(_code_spans(bullet)):
                prompt_paths.add(_resolve(path, self.tracked))
        self.assertEqual(
            prompt_paths & self.rule_paths,
            self.rule_paths,
            f"{PROMPT.relative_to(REPO_ROOT)} no longer lists every file AGENTS.md rule 2 names",
        )


class ExpandedProcedureTests(unittest.TestCase):
    """
    The intro calls the prompt file "this rubric's section 1 expanded into a procedure".
    That is a claim about two lists staying in step, and nothing else in the repository
    checks it.
    """

    def setUp(self) -> None:
        self.prompt_text = PROMPT.read_text(encoding="utf-8")

    def test_the_intro_links_a_file_that_exists(self) -> None:
        targets = [
            target
            for _, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", DOC_TEXT)
            if not target.startswith("http")
        ]
        self.assertTrue(targets, "the page links no local file any more")
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(
                    (DOC.parent / target).resolve().exists(),
                    f"the page links {target}, which does not resolve",
                )
        self.assertIn(
            str(PROMPT.relative_to(REPO_ROOT)),
            DOC_TEXT,
            "the intro no longer names the safety-critical review prompt",
        )

    def test_the_prompt_opens_with_the_same_disqualifying_question(self) -> None:
        first = next(
            (line for line in self.prompt_text.splitlines() if re.match(r"^## 1\. ", line)), None
        )
        self.assertIsNotNone(first, "the prompt file has no section 1 to expand")
        self.assertIn(
            "fail-closed",
            first.lower(),
            f"the prompt's section 1 is {first!r}; this page's section 1 is "
            f"{NUMBERED[1]['title']!r}, and the intro says one expands the other",
        )
        self.assertIn("fail-closed", NUMBERED[1]["title"].lower())

    def test_every_signal_the_prompt_lists_is_a_signal_this_page_lists(self) -> None:
        """
        The prompt expands section 1, so its weakening signals are section 1's. Compared as
        code spans because that is the part of each bullet that names a thing rather than
        describing it -- `raise`, `|| true`, `continue-on-error`,
        `.coverage-thresholds.json`. A signal added to the procedure and not to the rubric
        means a reviewer following the page misses what the prompt would catch.
        """

        prompt_section = self.prompt_text.split("## 1. ", 1)[1].split("\n## ", 1)[0]
        prompt_signals = {
            span for bullet in _bullets(prompt_section) for span in _code_spans(bullet)
        }
        rubric_signals = {
            span for bullet in _bullets(NUMBERED[1]["body"]) for span in _code_spans(bullet)
        }
        self.assertTrue(prompt_signals, "the prompt's section 1 lists no signals")
        self.assertTrue(rubric_signals, "this page's section 1 lists no signals")
        self.assertEqual(
            prompt_signals - rubric_signals,
            set(),
            "the prompt names weakening signals docs/review-rubric.md section 1 does not",
        )


class CitationTests(unittest.TestCase):
    """
    Other files cite this page by section number. A citation is only as good as the heading
    it points at, and nothing renumbers headings for you.
    """

    def test_every_section_number_cited_anywhere_in_the_tree_resolves(self) -> None:
        pattern = re.compile(r"review-rubric\.md`?[^\n]{0,40}?section (\d+)")
        found = 0
        for tracked in _tracked_files():
            path = REPO_ROOT / tracked
            if path == DOC or not path.is_file() or path.suffix not in {".md", ".yml", ".py"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number in pattern.findall(text):
                found += 1
                with self.subTest(file=tracked, section=number):
                    self.assertIn(
                        int(number),
                        NUMBERED,
                        f"{tracked} cites docs/review-rubric.md section {number}, which does not exist",
                    )
        self.assertGreater(found, 0, "no file cites this page by section number any more")

    def test_the_labeler_comment_claim_still_holds(self) -> None:
        """
        .github/labeler.yml says section 2 treats a missing statement as blocking. That is
        why the label exists, so the claim is load-bearing for the config and not just prose.
        """

        text = LABELER_CONFIG.read_text(encoding="utf-8")
        match = re.search(r"review-rubric\.md section (\d+)\s*#?\s*\n?#?\s*treats ([^.]+)\.", text)
        self.assertIsNotNone(match, ".github/labeler.yml no longer cites a section of this page")
        section = NUMBERED[int(match.group(1))]
        self.assertTrue(
            section["verdict"].startswith("Blocking"),
            f"labeler.yml says section {match.group(1)} is blocking; its verdict is "
            f"{section['verdict']!r}",
        )
        self.assertIn("statement", section["body"].lower())
        self.assertIn("CONTRIBUTING.md", section["body"])

    def test_the_security_page_quote_is_still_in_the_section_it_cites(self) -> None:
        """
        docs/SECURITY-AI.md classifies review comments as untrusted input and cites this
        page's wording as the reason. It quotes it, so the quote can be checked.
        """

        text = SECURITY_AI.read_text(encoding="utf-8")
        match = re.search(r"review-rubric\.md\)? section (\d+) says (.+?)\.\s+That", text)
        self.assertIsNotNone(match, "docs/SECURITY-AI.md no longer quotes a section of this page")
        section = NUMBERED[int(match.group(1))]
        quote = re.sub(r"[*`]", "", match.group(2)).replace("a finding is ", "")
        self.assertIn(
            quote.strip(),
            re.sub(r"[*`]", "", section["body"]),
            f"docs/SECURITY-AI.md attributes {quote!r} to section {match.group(1)}, "
            "which no longer says it",
        )

    def test_every_agents_rule_this_page_cites_exists(self) -> None:
        rules = _numbered_rules(AGENTS.read_text(encoding="utf-8"), "0.")
        cited = {int(number) for number in re.findall(r"section 0 rule (\d+)", DOC_TEXT)}
        self.assertTrue(cited, "the page cites no AGENTS.md rule any more")
        for number in sorted(cited):
            with self.subTest(rule=number):
                self.assertIn(number, rules, f"the page cites AGENTS.md rule {number}, which is gone")

    def test_the_rules_this_page_leans_on_still_say_what_it_says_they_say(self) -> None:
        """
        Rule numbers resolving is not enough: the page attributes a specific instruction to
        each one. Rule 1 is why section 1's answer is the underlying cause, rule 2 is the
        file list, rule 3 is why "I could not verify this" is a complete outcome.
        """

        rules = _numbered_rules(AGENTS.read_text(encoding="utf-8"), "0.")
        expected = {
            1: "fail-closed",
            2: "safety-critical",
            3: "Verify claims",
        }
        for number, marker in sorted(expected.items()):
            with self.subTest(rule=number):
                self.assertIn(number, rules)
                self.assertIn(
                    marker.lower(),
                    rules[number].lower(),
                    f"AGENTS.md rule {number} no longer says {marker!r}, so this page's "
                    "citation of it points at the wrong rule",
                )


class TreeClaimTests(unittest.TestCase):
    """
    The claims the page makes about other files. Each is recomputed from the file rather
    than matched as a string, so a config that changed shape fails here.
    """

    def test_renovate_refuses_to_automerge_an_openzfs_line_bump(self) -> None:
        """
        Section 3 cites renovate.json as already refusing this, and uses that as the reason
        a hand-written change deserves the same scrutiny. If the config started automerging,
        the sentence would be arguing from something that is not true.
        """

        config = json.loads(RENOVATE.read_text(encoding="utf-8"))
        rules = [
            rule
            for rule in config.get("packageRules", [])
            if any("openzfs" in name.lower() for name in rule.get("matchPackageNames", []))
        ]
        self.assertTrue(rules, "renovate.json has no packageRule matching OpenZFS any more")
        for rule in rules:
            with self.subTest(rule=rule.get("matchPackageNames")):
                self.assertIs(
                    rule.get("automerge"),
                    False,
                    "section 3 says renovate.json refuses to automerge an OpenZFS minor-line bump",
                )
        self.assertIn(
            "renovate.json",
            NUMBERED[3]["body"],
            "section 3 no longer cites the config this test checks",
        )

    def test_the_coverage_manifest_is_where_floors_live(self) -> None:
        manifest = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
        self.assertTrue(
            isinstance(manifest.get("floors"), dict) and manifest["floors"],
            f"{THRESHOLDS.name} has no non-empty 'floors' object; sections 1 and 5 both "
            "send a reviewer to it",
        )

    def test_the_gate_really_fails_a_module_with_no_floor(self) -> None:
        """
        Section 5 says CI fails when a new module has no floor, and uses that to tell a
        reviewer the question is already answered -- so the remaining job is to check the
        floor is not zero. Run the real gate rather than trust the sentence.
        """

        gate = _load_check_coverage()
        failures, _, _ = gate.evaluate(
            floors={"ci_tools/common.py": 10},
            counts={"ci_tools/common.py": (20, 20), "ci_tools/brand_new.py": (5, 10)},
        )
        self.assertTrue(
            any("brand_new" in failure for failure in failures),
            f"tests/check_coverage.py did not fail a measured module with no floor: {failures}",
        )

    def test_the_correction_section_five_cites_names_the_path_it_quotes(self) -> None:
        text = CORRECTIONS.read_text(encoding="utf-8")
        quoted = [
            span for span in _code_spans(NUMBERED[5]["body"]) if span.startswith("/")
        ]
        self.assertTrue(quoted, "section 5 no longer quotes an absolute path as the bad case")
        for path in quoted:
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    text,
                    f"section 5 sends a reviewer to {CORRECTIONS.relative_to(REPO_ROOT)} for "
                    f"{path}, which that file does not mention",
                )

    def test_a_bare_assert_raises_passes_for_an_unrelated_error(self) -> None:
        """
        Section 5's first item is the only blocking one in that section, and its reason is a
        property of unittest and of the real exception class: `assertRaises(CiToolError)`
        with no message assertion passes for any CiToolError at all, including one raised
        somewhere the test was not aiming. Demonstrated here so the claim is evidence rather
        than assertion.
        """

        common = importlib.util.spec_from_file_location(
            "common_review_rubric", REPO_ROOT / "ci_tools" / "common.py"
        )
        assert common is not None and common.loader is not None
        module = importlib.util.module_from_spec(common)
        common.loader.exec_module(module)

        with self.assertRaises(module.CiToolError):
            raise module.CiToolError("MISSING_ENV: nothing to do with the guard under test")

        with self.assertRaises(module.CiToolError) as caught:
            raise module.CiToolError("the guard actually under test refused")
        self.assertIn("actually under test", str(caught.exception))

        self.assertIn(
            "assertRaises(CiToolError)",
            NUMBERED[5]["body"],
            "section 5 no longer names the pattern this test demonstrates",
        )

    def test_the_pointer_locations_section_seven_names_exist_and_point_somewhere(self) -> None:
        """
        Section 7 says AGENTS.md is the single source and the other three are pointers at it
        on purpose. A pointer that stopped pointing is the drift the section warns about, so
        every file under each named location has to link out -- at AGENTS.md itself, or at
        `.github/prompts/`, which is where `.claude/commands/` sends a reader and which
        AGENTS.md's own procedures live beside.
        """

        sentence = next(
            (
                part
                for part in re.split(r"(?<=[.?])\s", re.sub(r"\s+", " ", NUMBERED[7]["body"]))
                if "single source" in part
            ),
            None,
        )
        self.assertIsNotNone(
            sentence, "section 7 no longer claims AGENTS.md is the single source"
        )
        named = [span for span in _repo_paths(_code_spans(sentence)) if span != "AGENTS.md"]
        self.assertTrue(named, "section 7 names no pointer locations any more")
        self.assertIn("AGENTS.md", _code_spans(sentence), "the source itself is unnamed")
        for span in named:
            with self.subTest(path=span):
                target = REPO_ROOT / span.rstrip("/")
                self.assertTrue(target.exists(), f"section 7 names {span}, which is not in the tree")
                files = sorted(target.rglob("*")) if target.is_dir() else [target]
                files = [candidate for candidate in files if candidate.is_file()]
                self.assertTrue(files, f"{span} is empty, so it points at nothing")
                for candidate in files:
                    text = candidate.read_text(encoding="utf-8")
                    self.assertTrue(
                        "AGENTS.md" in text or ".github/prompts/" in text,
                        f"{candidate.relative_to(REPO_ROOT)} links neither AGENTS.md nor the "
                        "prompts directory, so it is no longer a pointer",
                    )

    def test_the_bot_name_matches_the_pages_that_describe_the_same_bot(self) -> None:
        """
        Section 8 is addressed to findings from one named bot, and docs/metrics.md and
        docs/SECURITY-AI.md name the same one. A rename that reached two of the three would
        leave this page telling a reviewer to answer findings from an account that no longer
        comments.
        """

        names = [span for span in _code_spans(NUMBERED[8]["body"]) if span.endswith("[bot]")]
        self.assertEqual(len(set(names)), 1, f"section 8 names {sorted(set(names))}")
        name = names[0]
        for page in (METRICS, SECURITY_AI):
            with self.subTest(page=page.name):
                self.assertIn(
                    name,
                    page.read_text(encoding="utf-8"),
                    f"section 8 names {name}; {page.relative_to(REPO_ROOT)} names a different bot",
                )

    def test_the_pull_request_section_eight_cites_belongs_to_this_repository(self) -> None:
        links = re.findall(r"\((https://github\.com/[^)]+)\)", NUMBERED[8]["body"])
        self.assertTrue(links, "section 8 no longer cites the finding it calls correct")
        for link in links:
            with self.subTest(link=link):
                self.assertRegex(
                    link,
                    r"^https://github\.com/Danathar/zfs-kinoite-complex/(pull|issues)/\d+$",
                    "section 8 cites a pull request outside this repository",
                )

    def test_every_repository_path_the_page_names_exists(self) -> None:
        for span in _repo_paths(_code_spans(DOC_TEXT)):
            with self.subTest(path=span):
                self.assertTrue(
                    (REPO_ROOT / span.rstrip("/")).exists(),
                    f"the page names {span}, which is not in the tree",
                )


class EntryPointTests(unittest.TestCase):
    """
    The page is only ever read because something sends a reader to it. tests/
    test_docs_consistency.py resolves links repository-wide; these assertions are about the
    specific routes that make this page part of the review procedure rather than a file in
    docs/.
    """

    def test_every_entry_point_still_links_the_page(self) -> None:
        for page in ENTRY_POINTS:
            with self.subTest(page=str(page.relative_to(REPO_ROOT))):
                self.assertIn(
                    "review-rubric.md",
                    page.read_text(encoding="utf-8"),
                    f"{page.relative_to(REPO_ROOT)} no longer routes a reader to the rubric",
                )

    def test_the_readme_router_has_a_row_for_reviewing(self) -> None:
        rows = [
            line
            for line in README.read_text(encoding="utf-8").splitlines()
            if line.startswith("|") and "review-rubric.md" in line
        ]
        self.assertEqual(
            len(rows), 1, f"the README router has {len(rows)} rows pointing at the rubric"
        )
        self.assertIn("review", rows[0].split("|")[1].lower())


if __name__ == "__main__":
    unittest.main()
