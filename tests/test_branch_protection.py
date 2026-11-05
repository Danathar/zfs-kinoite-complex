"""
Script: tests/test_branch_protection.py
What: Joins docs/branch-protection.md to .github/rulesets/main.json, the ruleset that keeps `main`
behind a pull request, and joins the ruleset's required checks to the workflows that report them.
Doing: Parses the page's rule bullets and compares them to the JSON in both directions: rule
types, the target, the bypass list, the approval count, the required check names and their
`integration_id`. Reads every workflow with PyYAML and checks that each required check is a job
that runs on every pull request: no path or branch filter on the trigger, no `if:`, no `needs:`,
no matrix. Checks that the Status section's ruleset id is the one the `PUT` command updates, and
that no other document still says `main` is unprotected. Joins the page's nightly section to the
`ruleset-drift` job and to `find_weaker_rules`: each weaker shape the page lists is built from the
committed file and has to fail, each shape it says passes has to pass.
Why: Merging to `main` publishes, and until 2026-09-24 nothing stopped a direct push to it: the
`contents: write` token in ai-fix.yml was held back from `main` by a sentence in a prompt. The
ruleset is the control now, and it can break in two quiet ways. A loosened file (a bypass actor,
a dropped rule) reopens the direct push the next time someone `PUT`s it. A required check that
some pull requests never get leaves them waiting forever, and the obvious fix then is deleting
the ruleset. The page explaining it is a second copy of the JSON, and the aurora-zfs-simple copy
of this page was wrong on the day it was written.
Goal: Make a loosened ruleset, a renamed or filtered required job, or a page that no longer
matches the file fail here, on the pull request that causes it.

What this cannot check: the live ruleset is repository configuration, and reading it needs the
GitHub API. The page carries the two commands that show it, and the `ruleset-drift` job in
nightly-compliance.yml compares the live rules with the file every night
(ci_tools/check_ruleset_drift.py, tested in tests/test_check_ruleset_drift.py).

PyYAML is imported the way the other workflow tests here import it: used when present, skipped
when not. test.yml installs it by name, so in CI the workflow joins always run; the skip is for a
checkout with only pytest installed.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from ci_tools import check_ruleset_drift
from ci_tools.check_ruleset_drift import FLEET, fetch_live_rules, find_weaker_rules, ruleset_path
from tests.test_docs_consistency import workflow_paths

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; see test.yml
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "docs" / "branch-protection.md"
RULESET = REPO_ROOT / ".github" / "rulesets" / "main.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RISK_TIERS = REPO_ROOT / "docs" / "risk-tiers.md"
SECURITY = REPO_ROOT / "docs" / "SECURITY-AI.md"

GITHUB_ACTIONS_APP_ID = 15368
REPO_SLUG = "Danathar/zfs-kinoite-complex"
RULE_TYPES = {"deletion", "non_fast_forward", "pull_request", "required_status_checks"}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}
# The event types a bare `pull_request:` trigger fires on. A `types:` list that drops one of
# these leaves some pushes to a pull request without a run.
DEFAULT_PR_TYPES = {"opened", "synchronize", "reopened"}
# Trigger keys that narrow which pull requests run the workflow at all.
PR_FILTERS = ("paths", "paths-ignore", "branches", "branches-ignore")

BACKTICKED_RE = re.compile(r"`([^`]+)`")

# Phrasings that said `main` was unprotected before the ruleset was applied. A document that
# needs to say it again should first be sure it is, then change this list.
STALE = (
    r"`main` is not branch-protected",
    r"\*\*not branch-protected\*\*",
    r"`main` is not protected",
    r"branch protection if it is ever enabled",
    r"no ruleset or branch protection",
    r"[Bb]locks nothing automatically",
    r"for an admin to apply",
    r"RULESET_ID",
)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def outside_fences(text: str) -> str:
    kept, fenced = [], False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"{PAGE.name} has no '## {heading}' section")
    return match.group(1)


def bullets(body: str) -> list[str]:
    """Top-level `- ` bullets with their continuation lines, squashed."""
    items: list[str] = []
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("- "):
            if current is not None:
                items.append(squash(current))
            current = line[2:]
        elif current is not None and line.startswith("  "):
            current += " " + line
        elif current is not None:
            items.append(squash(current))
            current = None
    if current is not None:
        items.append(squash(current))
    return items


def load_ruleset() -> dict:
    return json.loads(RULESET.read_text(encoding="utf-8"))


def rules_by_type(ruleset: dict) -> dict[str, dict]:
    return {rule["type"]: rule.get("parameters", {}) for rule in ruleset["rules"]}


def required_checks(ruleset: dict) -> list[dict]:
    return rules_by_type(ruleset).get("required_status_checks", {}).get("required_status_checks", [])


def workflows() -> dict[str, dict]:
    loaded = {}
    for path in workflow_paths():
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        loaded[path.name] = doc
    return loaded


def triggers(doc: dict) -> dict:
    # `on:` is YAML 1.1 true; PyYAML resolves the bare key to the boolean.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {name: None for name in on}
    return on or {}


def job_name(job_id: str, job: dict) -> str:
    """The check-run name GitHub reports a job under: its `name:`, or its id without one."""
    return str(job.get("name", job_id))


def why_not_every_pull_request(doc: dict, job: dict) -> list[str]:
    """Reasons this job can be missing from some pull request. Empty means it never is."""
    reasons = []
    on = triggers(doc)
    if "pull_request" not in on:
        reasons.append("the workflow has no `pull_request` trigger")
        return reasons
    trigger = on["pull_request"] or {}
    for key in PR_FILTERS:
        if key in trigger:
            reasons.append(f"the `pull_request` trigger has `{key}:` {trigger[key]}")
    if "types" in trigger and not DEFAULT_PR_TYPES <= set(trigger["types"]):
        reasons.append(f"the `pull_request` trigger's `types:` drops {sorted(DEFAULT_PR_TYPES - set(trigger['types']))}")
    if "if" in job:
        reasons.append(f"the job has `if: {job['if']}`")
    if "needs" in job:
        reasons.append(f"the job `needs:` {job['needs']}, and is skipped when that does not succeed")
    if "strategy" in job:
        reasons.append("the job has a `strategy:`; a matrix reports under a different name per leg")
    return reasons


class RulesetKeepsMainBehindAPullRequest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ruleset = load_ruleset()
        cls.rules = rules_by_type(cls.ruleset)

    def test_it_is_enforced_on_the_default_branch(self) -> None:
        self.assertEqual(self.ruleset["target"], "branch")
        self.assertEqual(self.ruleset["enforcement"], "active")
        self.assertEqual(self.ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual(self.ruleset["conditions"]["ref_name"].get("exclude", []), [])

    def test_nothing_may_bypass_it(self) -> None:
        # A bypass for Actions or for an App hands back the direct push, and it is the fix
        # someone reaches for the first time a workflow's push to main is refused.
        self.assertEqual(self.ruleset.get("bypass_actors"), [])

    def test_it_carries_the_four_rules_and_no_others(self) -> None:
        self.assertEqual(set(self.rules), RULE_TYPES)
        self.assertEqual(len(self.ruleset["rules"]), len(RULE_TYPES), "a rule type is listed twice")

    def test_a_pull_request_needs_no_approval_a_sole_maintainer_cannot_give(self) -> None:
        # Nobody can approve their own pull request. One approval on a single-maintainer
        # repository means nothing can merge, including the change that relaxes this.
        self.assertEqual(self.rules["pull_request"]["required_approving_review_count"], 0)

    def test_every_required_check_is_pinned_to_github_actions(self) -> None:
        checks = required_checks(self.ruleset)
        self.assertTrue(checks, "the ruleset requires no check")
        for check in checks:
            with self.subTest(context=check.get("context")):
                self.assertEqual(check.get("integration_id"), GITHUB_ACTIONS_APP_ID)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class EveryPullRequestGetsTheRequiredChecks(unittest.TestCase):
    """A required check a pull request never gets leaves that pull request unmergeable."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflows = workflows()
        cls.contexts = [check["context"] for check in required_checks(load_ruleset())]

    def test_the_scan_finds_the_workflows(self) -> None:
        # Guard the guard: an empty scan would make every assertion below vacuous.
        self.assertIn("test.yml", self.workflows)
        self.assertTrue(self.contexts)

    def test_each_required_check_is_a_job_that_runs_on_every_pull_request(self) -> None:
        for context in self.contexts:
            with self.subTest(context=context):
                providers = {
                    f"{file}:{job_id}": why_not_every_pull_request(doc, job)
                    for file, doc in self.workflows.items()
                    for job_id, job in (doc.get("jobs") or {}).items()
                    if job_name(job_id, job) == context
                }
                self.assertTrue(
                    providers,
                    f"no workflow job reports a check named {context!r}; a pull request would "
                    "wait for it forever",
                )
                unconditional = [where for where, reasons in providers.items() if not reasons]
                self.assertTrue(
                    unconditional,
                    f"every job named {context!r} can be missing from a pull request: {providers}",
                )


class ThePageMatchesTheRuleset(unittest.TestCase):
    """docs/branch-protection.md, joined to .github/rulesets/main.json both ways."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.page = PAGE.read_text(encoding="utf-8")
        cls.prose = outside_fences(cls.page)
        cls.ruleset = load_ruleset()
        cls.rules = rules_by_type(cls.ruleset)
        cls.contexts = [check["context"] for check in required_checks(cls.ruleset)]
        cls.leads: dict[str, str] = {}
        for item in bullets(section(cls.prose, "The ruleset")):
            match = re.match(r"\*\*(.+?)\*\*", item)
            if match is None:
                raise AssertionError(f"a rule bullet has no bold lead: {item[:60]}")
            cls.leads[match.group(1)] = item

    def lead(self, pattern: str) -> tuple[str, str]:
        found = [(key, body) for key, body in self.leads.items() if re.search(pattern, key, re.IGNORECASE)]
        self.assertEqual(len(found), 1, f"expected one rule bullet matching {pattern!r}: {sorted(self.leads)}")
        return found[0]

    def test_the_rules_the_page_explains_are_the_rules_in_the_file(self) -> None:
        named = set()
        for key in self.leads:
            named |= {tok for tok in BACKTICKED_RE.findall(key) if re.fullmatch(r"[a-z_]+", tok)}
            if re.search(r"\brequired checks?\b", key, re.IGNORECASE):
                named.add("required_status_checks")
        self.assertEqual(named, set(self.rules))

    def test_the_target_the_page_names_is_the_files(self) -> None:
        key, _ = self.lead(r"^Targets ")
        self.assertEqual(BACKTICKED_RE.findall(key), self.ruleset["conditions"]["ref_name"]["include"])

    def test_the_bypass_list_the_page_states_is_the_files(self) -> None:
        key, _ = self.lead(r"bypass")
        self.assertEqual(key.startswith("No bypass actors"), self.ruleset.get("bypass_actors") == [])

    def test_the_approval_count_the_page_states_is_the_files(self) -> None:
        key, _ = self.lead(r"`pull_request`")
        stated = re.search(r"with (\d+) approvals?", key)
        self.assertIsNotNone(stated, key)
        self.assertEqual(int(stated.group(1)), self.rules["pull_request"]["required_approving_review_count"])

    def test_the_required_checks_the_page_names_are_the_files(self) -> None:
        key, body = self.lead(r"required checks?")
        count = re.match(r"(\w+) required checks?", key, re.IGNORECASE)
        self.assertIsNotNone(count, key)
        self.assertEqual(NUMBER_WORDS.get(count.group(1).lower()), len(self.contexts))
        self.assertEqual(sorted(BACKTICKED_RE.findall(key)), sorted(self.contexts))
        stated_ids = {int(n) for n in re.findall(r"`integration_id` (\d+)", body)}
        self.assertEqual(stated_ids, {c["integration_id"] for c in required_checks(self.ruleset)})

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_every_pull_request_job_is_required_or_named_as_not_required(self) -> None:
        """
        A new pull-request job nobody classified is the one missed when the required set is
        revisited, and a job name the bullet mentions that no workflow defines is stale.
        """
        _, body = self.lead(r"required checks?")
        mentioned = set(BACKTICKED_RE.findall(body))
        all_jobs, pr_jobs = set(), set()
        for doc in workflows().values():
            on = triggers(doc)
            for job_id, job in (doc.get("jobs") or {}).items():
                name = job_name(job_id, job)
                all_jobs.add(name)
                if {"pull_request", "pull_request_target"} & set(on):
                    pr_jobs.add(name)
        self.assertEqual(sorted(pr_jobs - set(self.contexts) - mentioned), [])
        self.assertEqual(sorted(tok for tok in mentioned if " " in tok and tok not in all_jobs), [])
        for wf in re.findall(r"`([\w-]+\.yml)`", body):
            self.assertTrue((WORKFLOWS / wf).is_file(), wf)

    def test_the_status_section_records_the_live_ruleset(self) -> None:
        status = squash(section(self.prose, "Status"))
        self.assertIn(f"lists `{self.ruleset['name']}`", status)
        ids = re.findall(r"as ruleset `(\d+)`", status)
        self.assertEqual(len(ids), 1, f"the Status section should record one numeric ruleset id: {status}")
        puts = re.findall(r"--method PUT repos/([\w.-]+/[\w.-]+)/rulesets/(\S+)", self.page)
        self.assertEqual(puts, [(REPO_SLUG, ids[0])])

    def test_every_command_on_the_page_names_this_repository_and_the_committed_file(self) -> None:
        slugs = set(re.findall(r"gh api (?:--method \w+ )?repos/([\w.-]+/[\w.-]+)/", self.page))
        self.assertEqual(slugs, {REPO_SLUG})
        for verb in ("POST", "PUT"):
            inputs = re.findall(rf"--method {verb} .*?\\\n\s*--input (\S+)", self.page)
            self.assertEqual(inputs, [".github/rulesets/main.json"], verb)

    def test_the_status_branch_it_leaves_uncovered_is_the_one_a_workflow_pushes(self) -> None:
        match = re.search(r"`status` branch that `([\w-]+\.yml)` pushes", squash(self.prose))
        self.assertIsNotNone(match, "the page no longer names the workflow that pushes `status`")
        source = (WORKFLOWS / match.group(1)).read_text(encoding="utf-8")
        self.assertRegex(source, r"git push \S+ HEAD:status\b")


class TheNightlySectionMatchesTheJob(unittest.TestCase):
    """"Checking the live rules every night", joined to the job and to the comparison it runs."""

    HEADING = "Checking the live rules every night"
    JOB = "ruleset-drift"
    COMMAND = "python3 -m ci_tools.cli check-ruleset-drift"

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = section(outside_fences(PAGE.read_text(encoding="utf-8")), cls.HEADING)
        cls.text = squash(cls.body)
        cls.items = bullets(cls.body)
        cls.ruleset = load_ruleset()

    def live(self) -> list[dict]:
        """The committed rules as the effective-rules endpoint would return them."""
        return copy.deepcopy(self.ruleset["rules"])

    def live_rule(self, rules: list[dict], rule_type: str) -> dict:
        return next(rule for rule in rules if rule["type"] == rule_type)["parameters"]

    def bullet(self, needle: str) -> str:
        found = [item for item in self.items if needle in item]
        self.assertEqual(len(found), 1, f"expected one bullet naming {needle}: {self.items}")
        return found[0]

    def test_the_committed_file_applied_as_is_passes(self) -> None:
        self.assertEqual(find_weaker_rules(self.ruleset, self.live()), [])

    def test_the_rule_types_the_page_lists_are_the_files(self) -> None:
        listed = BACKTICKED_RE.findall(self.bullet("that GitHub does not enforce"))
        self.assertEqual(sorted(listed), sorted(rules_by_type(self.ruleset)))
        for rule_type in listed:
            kept = [rule for rule in self.live() if rule["type"] != rule_type]
            self.assertEqual(len(find_weaker_rules(self.ruleset, kept)), 1, rule_type)

    def test_code_owner_review_off_live_fails_as_the_page_says(self) -> None:
        self.assertIn("`require_code_owner_review` true in the file and false live", self.text)
        self.assertTrue(rules_by_type(self.ruleset)["pull_request"]["require_code_owner_review"])
        live = self.live()
        self.live_rule(live, "pull_request")["require_code_owner_review"] = False
        self.assertEqual(len(find_weaker_rules(self.ruleset, live)), 1)

    def test_a_lower_approval_count_live_fails_as_the_page_says(self) -> None:
        self.assertIn("a lower `required_approving_review_count` live", self.text)
        committed = copy.deepcopy(self.ruleset)
        live = self.live()
        self.live_rule(committed["rules"], "pull_request")["required_approving_review_count"] = 2
        self.live_rule(live, "pull_request")["required_approving_review_count"] = 1
        self.assertEqual(len(find_weaker_rules(committed, live)), 1)

    def test_strict_mode_off_live_fails_as_the_page_says(self) -> None:
        self.assertIn("`strict_required_status_checks_policy` true in the file and false live", self.text)
        self.assertTrue(
            rules_by_type(self.ruleset)["required_status_checks"]["strict_required_status_checks_policy"]
        )
        live = self.live()
        self.live_rule(live, "required_status_checks")["strict_required_status_checks_policy"] = False
        self.assertEqual(len(find_weaker_rules(self.ruleset, live)), 1)

    def test_a_missing_or_foreign_required_check_fails_as_the_page_says(self) -> None:
        self.assertIn(
            "a required check missing live, or required live from a different `integration_id` than the file pins",
            self.text,
        )
        missing = self.live()
        self.live_rule(missing, "required_status_checks")["required_status_checks"] = []
        self.assertEqual(len(find_weaker_rules(self.ruleset, missing)), 1)
        foreign = self.live()
        for check in self.live_rule(foreign, "required_status_checks")["required_status_checks"]:
            check["integration_id"] = GITHUB_ACTIONS_APP_ID + 1
        self.assertEqual(len(find_weaker_rules(self.ruleset, foreign)), 1)

    def test_the_page_lists_every_way_the_comparison_can_fail(self) -> None:
        # One bullet per kind of finding: a new `findings.append` needs a bullet here.
        self.assertEqual(len(self.items), 5, self.items)
        source = Path(check_ruleset_drift.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("findings.append("), 5)

    def test_extra_and_stricter_rules_pass_as_the_page_says(self) -> None:
        self.assertIn("Extra rules, or stricter ones, pass.", self.text)
        live = self.live()
        self.live_rule(live, "pull_request")["required_approving_review_count"] = 3
        live.append({"type": "required_signatures"})
        self.assertEqual(find_weaker_rules(self.ruleset, live), [])

    def test_the_strongest_of_two_rulesets_counts_as_the_page_says(self) -> None:
        self.assertIn(
            "When two rulesets apply to the branch, GitHub returns a rule of the same type once "
            "for each, and the strongest one counts.",
            self.text,
        )
        weaker = {"type": "pull_request", "parameters": {"require_code_owner_review": False}}
        self.assertEqual(find_weaker_rules(self.ruleset, [weaker, *self.live()]), [])

    def test_the_settings_it_says_it_does_not_compare_are_not_compared(self) -> None:
        self.assertIn("the other `pull_request` settings, such as dismissing stale reviews", self.text)
        self.assertTrue(rules_by_type(self.ruleset)["pull_request"]["dismiss_stale_reviews_on_push"])
        live = self.live()
        self.live_rule(live, "pull_request")["dismiss_stale_reviews_on_push"] = False
        self.assertEqual(find_weaker_rules(self.ruleset, live), [])

    def test_the_endpoint_the_page_names_is_the_one_it_reads(self) -> None:
        self.assertIn("`GET /repos/{owner}/{repo}/rules/branches/{branch}`", self.text)
        paths: list[str] = []

        def fake_get(path: str, token: str) -> tuple[object, str]:
            paths.append(path)
            return ({"default_branch": "trunk"} if len(paths) == 1 else []), ""

        with mock.patch.object(check_ruleset_drift, "_get", fake_get):
            fetch_live_rules("owner/repo", "token")
        self.assertEqual(paths[0], "/repos/owner/repo")
        self.assertEqual(paths[1].split("?")[0], "/repos/owner/repo/rules/branches/trunk")

    def test_the_admin_command_reads_the_ruleset_the_status_section_records(self) -> None:
        status = squash(section(outside_fences(PAGE.read_text(encoding="utf-8")), "Status"))
        recorded = re.findall(r"as ruleset `(\d+)`", status)
        self.assertEqual(re.findall(rf"`gh api repos/{REPO_SLUG}/rulesets/(\d+)`", self.text), recorded)

    def test_the_fleet_paragraph_matches_the_script(self) -> None:
        self.assertIn(f"`{ruleset_path('{branch}')}`", self.text)
        self.assertIn(f"(`{ruleset_path('master').rsplit('/', 1)[1]}` in exult and sensi", self.text)
        self.assertIn(f"`{ruleset_path('main').rsplit('/', 1)[1]}` elsewhere)", self.text)
        for name in ("exult", "sensi"):
            self.assertIn(f"Danathar/{name}", FLEET)
        self.assertIn("each repository in its `FLEET` list", self.text)
        self.assertIn("with `--fleet`", self.text)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_the_job_is_the_one_the_page_describes(self) -> None:
        doc = workflows()["nightly-compliance.yml"]
        self.assertIn(f"`{self.JOB}` job", self.text)
        self.assertIn("(../.github/workflows/nightly-compliance.yml)", self.body)
        self.assertIn("(../ci_tools/check_ruleset_drift.py)", self.body)
        job = doc["jobs"][self.JOB]
        run_steps = [step for step in job["steps"] if "run" in step]
        self.assertEqual([step["run"] for step in run_steps], [self.COMMAND, f"{self.COMMAND} --fleet"])

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_it_runs_every_night_and_the_fleet_step_still_runs_after_a_failure(self) -> None:
        doc = workflows()["nightly-compliance.yml"]
        self.assertIn("every night", self.text)
        crons = [entry["cron"] for entry in triggers(doc)["schedule"]]
        self.assertEqual(len(crons), 1)
        self.assertEqual(crons[0].split()[2:], ["*", "*", "*"], "the schedule is no longer daily")
        fleet = doc["jobs"][self.JOB]["steps"][-1]
        self.assertEqual(fleet["if"], "${{ !cancelled() }}")

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_the_job_uses_the_runs_own_token_and_no_admin_secret(self) -> None:
        doc = workflows()["nightly-compliance.yml"]
        self.assertIn("needs only `contents: read`", self.text)
        self.assertIn("the job uses the run's own token and no admin secret", self.text)
        self.assertEqual(doc["permissions"], {"contents": "read"})
        job = doc["jobs"][self.JOB]
        self.assertNotIn("permissions", job)
        self.assertNotIn("secrets.", json.dumps(job))
        for step in (step for step in job["steps"] if "run" in step):
            self.assertEqual(step["env"], {"GITHUB_TOKEN": "${{ github.token }}"})

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_a_failed_run_is_watched_by_the_workflow_the_page_names(self) -> None:
        self.assertIn("(../.github/workflows/auto-issues.yml)", self.body)
        name = workflows()["nightly-compliance.yml"]["name"]
        self.assertIn(name, triggers(workflows()["auto-issues.yml"])["workflow_run"]["workflows"])


class TheDocsThatRouteAChangeNameIt(unittest.TestCase):
    def test_risk_tiers_puts_the_ruleset_in_tier_3(self) -> None:
        text = RISK_TIERS.read_text(encoding="utf-8")
        tier_3 = text.split("### Tier 3", 1)[1].split("### Tier 2", 1)[0]
        self.assertIn("`.github/rulesets/`", tier_3)

    def test_security_ai_points_at_the_page(self) -> None:
        self.assertIn("(branch-protection.md)", SECURITY.read_text(encoding="utf-8"))

    def test_no_document_says_main_is_unprotected(self) -> None:
        tracked = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertGreater(len(tracked), 10)
        hits = []
        for rel in tracked:
            text = squash((REPO_ROOT / rel).read_text(encoding="utf-8"))
            for pattern in STALE:
                for match in re.finditer(pattern, text):
                    hits.append(f"{rel}: ...{text[max(0, match.start() - 40) : match.end() + 20]}...")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
