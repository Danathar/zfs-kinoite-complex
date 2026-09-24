"""
Script: tests/test_session_summary.py
What: Joins .claude/session-summary.md to the machine it makes claims about -- the workflows it
names, the AGENTS.md rule it cites, the failure window docs/metrics.md classifies, and the two
routing destinations whose purpose it restates.
Doing: Derives each assertion from the file's own prose -- the workflow filename, the branch, the
ignored globs, the job names, the verbs, the dates and the version strings are read out of the
sentences and then recomputed against .github/workflows/, AGENTS.md, ci/defaults.json,
docs/metrics.md and docs/quality.md.
Why: Nothing opened this file. It is the first thing a fresh session reads and it exists to be
acted on -- it names the checks a session should wait for and the ones it should not, and its own
header says stale content here is worse than an empty file, "a session that trusts it will act on
it". It was already wrong: it told every session that a pull request triggers `Build Or Reuse
Shared ZFS Akmods Cache` and `Build Branch Image`, which are jobs in build-branch.yml, a workflow
with no `pull_request` trigger at all and with `ai-fix/**` in its `branches-ignore` -- so an agent
session on an ai-fix branch was told to wait for two checks that can never appear.
Goal: Make a claim here fail on the pull request that falsifies it, rather than on the session
that believes it.

Three deliberate omissions, named so they do not read as oversights.

Whether the ruleset on `main` is live is a fact about repository settings, not about the tree.
A test can only settle it through the GitHub API, which needs a token and a network the unit suite
does not have, so it stays a review claim; tests/test_branch_protection.py checks the committed
ruleset, and docs/branch-protection.md carries the commands that show the live one. The same goes for `:latest` being published and current,
and for the "about ten seconds" and "tens of minutes" timings: those are observations about runs,
and the tree holds no record to recompute them from. What *is* checked about the current-state
paragraph is the part the tree can settle -- that the ZFS series and the Fedora major it names
are still the ones ci/defaults.json configures.

Nothing here re-checks that this file's links resolve; tests/test_docs_consistency.py already
resolves every relative link in every tracked markdown file, and its `git ls-files *.md` listing
includes this one. What is checked instead is what those links say *about* their targets.

No PyYAML, for the reason tests/test_docs_consistency.py gives: the CI job installs pytest,
pytest-cov, ruff and pyyaml, and the workflow tests that import yaml skip without it. A doc-join
test is the worst place to accept a silent skip, so the workflows are read as text here.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DOCS_DIR = REPO_ROOT / "docs"

SUMMARY = REPO_ROOT / ".claude" / "session-summary.md"
AGENTS = REPO_ROOT / "AGENTS.md"
DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
METRICS = DOCS_DIR / "metrics.md"
QUALITY = DOCS_DIR / "quality.md"
MEMORY_README = REPO_ROOT / ".claude" / "memory" / "README.md"
PROMPTS_README = REPO_ROOT / ".github" / "prompts" / "README.md"

# A job's `name:` sits at exactly four spaces -- one level under `jobs:`, which is top level.
# Step names are deeper, and are written `- name:` rather than `name:`.
JOB_NAME_RE = re.compile(r"^ {4}name: (.+)$")
BACKTICKED_RE = re.compile(r"`([^`]+)`")
WORKFLOW_REF_RE = re.compile(r"`([A-Za-z0-9_.-]+\.ya?ml)`")

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
}


def flat(text: str) -> str:
    """Collapse wrapped prose to one line, so a claim can be matched across a line break."""
    return re.sub(r"\s+", " ", text).strip()


def bullet(text: str, lead: str) -> str:
    """The flattened body of the `- **<lead>...` bullet, up to the next bullet or heading."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("- ") and lead in line:
            collected = [line[2:]]
            for following in lines[index + 1 :]:
                if not following.strip() or following.startswith(("- ", "#")):
                    break
                collected.append(following)
            return flat(" ".join(collected))
    raise AssertionError(f"{SUMMARY.name} no longer has a bullet mentioning {lead!r}")


def yaml_block(lines: list[str], index: int) -> list[str]:
    """The lines nested under `lines[index]`, by indentation. Comments and blanks dropped."""
    indent = len(lines[index]) - len(lines[index].lstrip())
    collected: list[str] = []
    for line in lines[index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        collected.append(line)
    return collected


def triggers(workflow: Path) -> dict[str, list[str]]:
    """The workflow's top-level `on:` keys, each mapped to the raw lines beneath it."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() == "on:":
            block = yaml_block(lines, index)
            found: dict[str, list[str]] = {}
            for offset, entry in enumerate(block):
                match = re.match(r"^ {2}([A-Za-z_][A-Za-z0-9_-]*):", entry)
                if match:
                    found[match.group(1)] = yaml_block(block, offset)
            return found
    raise AssertionError(f"{workflow.name} has no top-level `on:` block")


def sequence(lines: list[str], key: str) -> list[str]:
    """The `- item` values of the `key:` list somewhere inside `lines`."""
    for index, line in enumerate(lines):
        if line.strip() == f"{key}:":
            return [entry.strip()[2:].strip().strip("\"'") for entry in yaml_block(lines, index)]
    return []


def job_names() -> dict[str, Path]:
    """Every job `name:` in .github/workflows/, mapped to the workflow that defines it."""
    found: dict[str, Path] = {}
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = JOB_NAME_RE.match(line)
            if match:
                found[match.group(1).strip()] = workflow
    return found


def step_names(workflow: Path) -> list[str]:
    text = workflow.read_text(encoding="utf-8")
    return [match.strip() for match in re.findall(r"^\s*- name: (.+)$", text, flags=re.MULTILINE)]


class SessionSummaryShape(unittest.TestCase):
    """The headings and the date the rest of this module reads out of."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SUMMARY.read_text(encoding="utf-8")

    def test_the_three_sections_the_header_promises_are_present(self):
        """The header describes work in flight, decisions taken, and things known to be true.

        Each is a section below it. A rename that drops one silently empties whichever test
        reads it, so the names are pinned here once rather than in every reader.
        """
        for heading in ("## Current state", "## In flight", "## Standing constraints"):
            self.assertIn(heading, self.text, f"{SUMMARY.name} no longer has a {heading!r} section")

    def test_last_updated_is_a_real_date(self):
        """`Last updated:` is the only thing telling a reader how far to trust the section.

        Not asserted recent: the file is updated when something changes, and a freshness
        deadline would fail on quiet weeks rather than on wrong content.
        """
        match = re.search(r"Last updated: (\d{4}-\d{2}-\d{2})", self.text)
        self.assertIsNotNone(match, f"{SUMMARY.name} has no `Last updated: YYYY-MM-DD` line")
        dt.date.fromisoformat(match.group(1))


class CurrentStateClaims(unittest.TestCase):
    """What the current-state paragraph says about the configured build."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SUMMARY.read_text(encoding="utf-8")
        cls.flat = flat(cls.text)
        cls.defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))

    def test_named_zfs_release_is_in_the_configured_series(self):
        """"ZFS 2.4.4" is only current while 2.4 is the minor series ci/defaults.json selects.

        Bumping `DEFAULT_ZFS_MINOR_VERSION` and leaving this paragraph alone leaves a session
        being told the wrong series ships.
        """
        match = re.search(r"ZFS (\d+\.\d+)\.\d+", self.flat)
        self.assertIsNotNone(match, "the current-state paragraph no longer names a ZFS release")
        self.assertEqual(
            match.group(1),
            self.defaults["DEFAULT_ZFS_MINOR_VERSION"],
            "the ZFS series named here is not the one ci/defaults.json configures",
        )

    def test_named_kernel_is_built_for_the_configured_fedora_major(self):
        """The kernel is quoted as `7.1.12-200.fc44.x86_64`; `fc44` is a claim about the base.

        The base image tag in ci/defaults.json is the Fedora major this repository builds on,
        so a base bump with this paragraph left behind is exactly the drift to catch.
        """
        match = re.search(r"`\d+\.\d+\.\d+-\d+\.fc(\d+)\.[a-z0-9_]+`", self.flat)
        self.assertIsNotNone(match, "the current-state paragraph no longer names a kernel")
        base = self.defaults["DEFAULT_BASE_IMAGE"]
        self.assertTrue(
            base.endswith(f":{match.group(1)}"),
            f"kernel named here is fc{match.group(1)}, but the base image is {base}",
        )

    def test_the_failure_window_matches_the_metrics_table(self):
        """The paragraph's run count and dates are a summary of docs/metrics.md's table row.

        Two places holding the same window is how a corrected count in one of them goes
        unnoticed in the other, so the numbers are recomputed from metrics.md rather than
        restated here.
        """
        match = re.search(
            r"(\w+) consecutive scheduled runs from (\d{4}-\d{2}-\d{2}) to (\d{2}-\d{2})", self.flat
        )
        self.assertIsNotNone(match, "the current-state paragraph no longer names a failure window")
        count = NUMBER_WORDS[match.group(1).lower()]
        start, end = match.group(2), match.group(3)

        metrics = METRICS.read_text(encoding="utf-8")
        row = next(
            (line for line in metrics.splitlines() if line.startswith("|") and start in line), None
        )
        self.assertIsNotNone(row, f"docs/metrics.md has no classified row for {start}")
        self.assertIn(f"{end} ({count} runs)", row, "the window in metrics.md is a different one")
        self.assertIn("SIGNING_SECRET is not configured", row)

    def test_the_quoted_refusal_is_a_line_build_yml_actually_prints(self):
        """The paragraph quotes a stderr line and then says what the guard refuses to do.

        Both halves are checked: the quote has to be the start of a line build.yml echoes, and
        the description has to match what that same line says it is refusing.
        """
        self.assertIn("SIGNING_SECRET is not configured", self.flat)
        description = "refusing to publish an unsigned production image"
        self.assertIn(description, self.flat.lower())

        build = (WORKFLOW_DIR / "build.yml").read_text(encoding="utf-8")
        refusals = [line for line in build.splitlines() if "SIGNING_SECRET is not configured" in line]
        self.assertTrue(refusals, "build.yml no longer prints the line quoted in the summary")
        self.assertTrue(
            any(description in line.lower() for line in refusals),
            "no SIGNING_SECRET refusal in build.yml is the one the summary describes",
        )

    def test_quality_md_still_tells_this_class_apart_from_two_others(self):
        """The paragraph routes the reader to docs/quality.md "apart from the two others".

        Two others means the window held three causes. quality.md's first numbered point is
        where that split lives, so the count is read back out of it.
        """
        self.assertIn("the two others in the same window", self.flat)
        quality = flat(QUALITY.read_text(encoding="utf-8"))
        match = re.search(
            r"(\w+) consecutive failures in August 2026 were ([^.]+)\.",
            quality,
            flags=re.IGNORECASE,
        )
        self.assertIsNotNone(match, "docs/quality.md no longer classifies the August 2026 run")
        causes = [part for part in re.split(r",|\band\b", match.group(2)) if part.strip()]
        self.assertEqual(
            len(causes), 3, f"quality.md now names {len(causes)} causes, not the summary's three"
        )
        self.assertTrue(any("SIGNING_SECRET" in cause for cause in causes))


class StandingConstraints(unittest.TestCase):
    """The four bullets a session is told not to rediscover."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SUMMARY.read_text(encoding="utf-8")
        cls.jobs = job_names()

    def test_merging_to_main_publishes_is_true_of_the_workflow_it_names(self):
        """"`build.yml` triggers on push to `main` (excluding `**/*.md` and `docs/**`)".

        Filename, branch and both globs are read out of the sentence and recomputed against the
        workflow's own `on: push:` block. This bullet is the reason AGENTS.md section 0 rule 6
        exists, so it is the one worst served by drifting.
        """
        claim = bullet(self.text, "publishes.")
        named = WORKFLOW_REF_RE.search(claim)
        self.assertIsNotNone(named, f"the bullet no longer names a workflow file: {claim!r}")
        workflow_name = named.group(1)
        workflow = WORKFLOW_DIR / workflow_name
        self.assertTrue(workflow.exists(), f"{workflow_name} named in the summary does not exist")

        branch = re.search(r"triggers on push to `([^`]+)`", claim).group(1)
        excluded = BACKTICKED_RE.findall(
            re.search(r"\(excluding ([^)]+)\)", claim).group(1)
        )
        self.assertTrue(excluded, "the bullet no longer names the paths a push can ignore")

        on = triggers(workflow)
        self.assertIn("push", on, f"{workflow_name} no longer triggers on push")
        self.assertIn(branch, sequence(on["push"], "branches"))
        ignored = sequence(on["push"], "paths-ignore")
        for glob in excluded:
            self.assertIn(glob, ignored, f"{workflow_name} no longer ignores {glob}")

    def test_the_same_workflow_promotes_to_the_tag_the_bullet_names(self):
        """"then builds, signs, and promotes to `:latest`" -- the promotion step has to exist."""
        claim = bullet(self.text, "publishes.")
        tag = re.search(r"promotes to `:?([A-Za-z0-9_.-]+)`", claim).group(1)
        workflow = WORKFLOW_DIR / WORKFLOW_REF_RE.search(claim).group(1)
        promotions = [
            name for name in step_names(workflow) if "promote" in name.lower() and tag in name
        ]
        self.assertTrue(
            promotions, f"no step in {workflow.name} promotes anything to `{tag}`"
        )

    def test_agents_rule_6_says_what_the_bullet_cites_it_as_saying(self):
        """The bullet compresses AGENTS.md section 0 rule 6 into four verbs.

        A citation is a claim about the cited text. The verbs are read out of the bullet and
        looked for in the numbered item itself, so renumbering or rewording section 0 fails
        here rather than leaving a confident wrong citation in the first file a session reads.
        """
        claim = bullet(self.text, "publishes.")
        cited = re.search(r"AGENTS\.md section (\d+) rule (\d+): ([^.]+)\.", claim)
        self.assertIsNotNone(cited, "the bullet no longer cites an AGENTS.md rule")
        section, rule, gist = cited.group(1), cited.group(2), cited.group(3)

        agents = AGENTS.read_text(encoding="utf-8").splitlines()
        start = next(
            (i for i, line in enumerate(agents) if line.startswith(f"## {section}.")), None
        )
        self.assertIsNotNone(start, f"AGENTS.md has no section {section}")
        body: list[str] = []
        for line in agents[start + 1 :]:
            if line.startswith("## "):
                break
            body.append(line)

        item_start = next(
            (i for i, line in enumerate(body) if line.startswith(f"{rule}. ")), None
        )
        self.assertIsNotNone(item_start, f"AGENTS.md section {section} has no rule {rule}")
        item = [body[item_start]]
        for line in body[item_start + 1 :]:
            if re.match(r"^\d+\. ", line):
                break
            item.append(line)
        item_text = flat(" ".join(item)).lower()

        verbs = [word for word in re.split(r",|\bor\b|\bnot\b", gist.lower()) if word.strip()]
        for verb in verbs:
            self.assertIn(
                verb.strip(),
                item_text,
                f"AGENTS.md section {section} rule {rule} does not say {verb.strip()!r}",
            )

    def test_the_check_a_merge_waits_for_is_a_real_job_name(self):
        """"A pull request cannot merge until a green `Python Unit Tests` check reports".

        Whether the ruleset is live is a repository setting and is not checked here -- see this
        module's header. That the named check exists at all is checkable, and a renamed job
        would leave a session watching for a check GitHub never reports.
        """
        claim = bullet(self.text, "only changes through a pull request.")
        check = re.search(r"a green `([^`]+)`\s+check", claim).group(1)
        self.assertIn(check, self.jobs, f"no workflow defines a job named {check!r}")

    def test_the_slow_builds_bullet_names_the_right_trigger_for_each_job(self):
        """"Pushing a branch triggers ...; opening the pull request triggers ...".

        This is the bullet that was wrong. It attributed all three jobs to the pull request,
        and two of them are in build-branch.yml, which has no `pull_request` trigger -- so a
        session was told to wait for checks that a pull request does not produce. Each half of
        the sentence is now checked against the `on:` block of the workflow that defines the
        jobs it lists.
        """
        claim = bullet(self.text, "Image builds are slow.")
        attributions = {
            "push": re.search(r"Pushing a branch triggers ([^;.]+)", claim),
            "pull_request": re.search(r"opening the pull request triggers ([^;.]+)", claim),
        }
        for event, match in attributions.items():
            self.assertIsNotNone(match, f"the bullet no longer attributes any job to {event}")
            named = BACKTICKED_RE.findall(match.group(1))
            self.assertTrue(named, f"the {event} half of the bullet names no job")
            for job in named:
                self.assertIn(job, self.jobs, f"no workflow defines a job named {job!r}")
                on = triggers(self.jobs[job])
                self.assertIn(
                    event,
                    on,
                    f"{job!r} lives in {self.jobs[job].name}, which does not trigger on {event}",
                )

    def test_the_branch_excluded_from_the_branch_build_really_is_excluded(self):
        """The bullet warns that an `ai-fix/**` branch gets no branch build.

        That exclusion is one line in build-branch.yml's `branches-ignore`, and dropping it
        would make the warning false in the direction that wastes a run rather than hangs a
        session -- but false either way.
        """
        claim = bullet(self.text, "Image builds are slow.")
        excluded = re.search(r"An `([^`]+)` branch is excluded", claim)
        self.assertIsNotNone(excluded, "the bullet no longer names the excluded branch pattern")
        branch_build = self.jobs["Build Branch Image"]
        self.assertIn(
            excluded.group(1), sequence(triggers(branch_build)["push"], "branches-ignore")
        )

    def test_the_transient_failure_bullet_agrees_with_the_triage_table(self):
        """"`unexpected EOF` during a blob copy is a registry or CDN failure".

        docs/quality.md holds the triage table a session is sent to. If that table stopped
        attributing this message to a CDN, the shortcut here would be telling sessions to rule
        out the wrong thing first.
        """
        claim = bullet(self.text, "unexpected EOF")
        message = BACKTICKED_RE.search(claim).group(1)
        quality = QUALITY.read_text(encoding="utf-8").splitlines()
        rows = [
            line
            for line in quality
            if line.strip().startswith("|") and message in line and "CDN" in line
        ]
        self.assertTrue(
            rows, f"docs/quality.md's triage table no longer attributes {message!r} to a CDN"
        )


class RoutingDestinations(unittest.TestCase):
    """The "if a section grows past a screen, it belongs here instead" list."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SUMMARY.read_text(encoding="utf-8")

    def test_the_wrong_belief_route_points_at_a_file_that_wants_wrong_beliefs(self):
        """"A wrong belief that cost time" is a restatement of what .claude/memory/ accepts.

        Its README sets the bar -- someone believed something false and it cost time. A route
        is only useful while the destination still takes what is being routed to it.
        """
        self.assertIn("A wrong belief that cost time", self.text)
        readme = flat(MEMORY_README.read_text(encoding="utf-8")).lower()
        self.assertIn("turned out to be wrong", readme)
        self.assertIn("cost time", readme)

    def test_the_procedure_route_points_at_the_catalog_of_procedures(self):
        """"A procedure" routes to .github/prompts/, whose README calls its entries procedures."""
        self.assertIn("A procedure", self.text)
        readme = flat(PROMPTS_README.read_text(encoding="utf-8")).lower()
        self.assertIn("procedure", readme)
        self.assertTrue(
            sorted((REPO_ROOT / ".github" / "prompts").glob("*.prompt.md")),
            ".github/prompts/ holds no prompt files for the route to land on",
        )


if __name__ == "__main__":
    unittest.main()
