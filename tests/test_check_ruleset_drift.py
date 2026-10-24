"""
Script: tests/test_check_ruleset_drift.py
What: Tests for `ci_tools/check_ruleset_drift.py`, the nightly comparison of the committed
ruleset with the rules GitHub enforces on the default branch.
Doing: Feeds `find_weaker_rules` a committed ruleset and a list shaped like the effective-rules
endpoint's response, and runs `main()` with the API calls replaced by a recording fake, both for
this repository and with `--fleet` across several fake ones.
Why: The check is asymmetric. It must fail when GitHub enforces less than the file and must pass
when GitHub enforces as much or more. Either direction going wrong is silent: a check that never
fails looks the same as a check that is green for a good reason (#488).
Goal: Each weaker shape fails here, each equal or stricter shape passes, the real committed
file is the one the job reads, and in `--fleet` mode a repository whose file cannot be read fails
the run rather than counting as checked (#490).
"""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

from ci_tools import check_ruleset_drift
from ci_tools.check_ruleset_drift import (
    FLEET,
    RAW_MEDIA_TYPE,
    RULESET_FILE,
    fetch_committed,
    fetch_live_rules,
    find_weaker_rules,
    fleet_section,
    render_report,
    ruleset_path,
)
from ci_tools.common import CiToolError

REPO_ROOT = Path(__file__).resolve().parent.parent

COMMITTED = {
    "name": "protect main",
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 1,
                "require_code_owner_review": True,
                "dismiss_stale_reviews_on_push": True,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "Python Unit Tests", "integration_id": 15368},
                    {"context": "Lint"},
                ],
            },
        },
    ],
}


def live_from(committed: dict, source: str = "Danathar/zfs-kinoite-complex") -> list[dict]:
    """The effective-rules response GitHub returns once `committed` has been applied as-is."""

    live = []
    for rule in committed["rules"]:
        entry = {
            "type": rule["type"],
            "ruleset_source_type": "Repository",
            "ruleset_source": source,
            "ruleset_id": 23959550,
        }
        if "parameters" in rule:
            entry["parameters"] = copy.deepcopy(rule["parameters"])
        live.append(entry)
    return live


def live_rule(live: list[dict], rule_type: str) -> dict:
    return next(rule for rule in live if rule["type"] == rule_type)


class FindWeakerRulesTests(unittest.TestCase):
    def test_identical_rules_pass(self) -> None:
        self.assertEqual(find_weaker_rules(COMMITTED, live_from(COMMITTED)), [])

    def test_a_missing_required_check_fails(self) -> None:
        live = live_from(COMMITTED)
        checks = live_rule(live, "required_status_checks")["parameters"]["required_status_checks"]
        checks.pop(0)
        findings = find_weaker_rules(COMMITTED, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("`Python Unit Tests` (integration_id 15368)", findings[0])

    def test_a_required_check_from_another_integration_fails(self) -> None:
        # The integration pin is what stops a status posted by anything else counting.
        live = live_from(COMMITTED)
        checks = live_rule(live, "required_status_checks")["parameters"]["required_status_checks"]
        checks[0] = {"context": "Python Unit Tests"}
        findings = find_weaker_rules(COMMITTED, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("Python Unit Tests", findings[0])

    def test_a_committed_check_without_an_integration_matches_any_source(self) -> None:
        live = live_from(COMMITTED)
        checks = live_rule(live, "required_status_checks")["parameters"]["required_status_checks"]
        checks[1] = {"context": "Lint", "integration_id": 99}
        self.assertEqual(find_weaker_rules(COMMITTED, live), [])

    def test_code_owner_review_off_live_fails(self) -> None:
        # The state #454 left behind: the file asks for code owner review, the live ruleset not.
        live = live_from(COMMITTED)
        live_rule(live, "pull_request")["parameters"]["require_code_owner_review"] = False
        findings = find_weaker_rules(COMMITTED, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("require_code_owner_review", findings[0])

    def test_strict_mode_off_live_fails(self) -> None:
        live = live_from(COMMITTED)
        del live_rule(live, "required_status_checks")["parameters"][
            "strict_required_status_checks_policy"
        ]
        findings = find_weaker_rules(COMMITTED, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("strict_required_status_checks_policy", findings[0])

    def test_a_lower_approval_count_live_fails(self) -> None:
        live = live_from(COMMITTED)
        live_rule(live, "pull_request")["parameters"]["required_approving_review_count"] = 0
        findings = find_weaker_rules(COMMITTED, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("is 1 in the file but 0 live", findings[0])

    def test_a_committed_rule_type_absent_live_fails(self) -> None:
        for rule_type in ("deletion", "non_fast_forward", "pull_request", "required_status_checks"):
            with self.subTest(rule_type=rule_type):
                live = [rule for rule in live_from(COMMITTED) if rule["type"] != rule_type]
                findings = find_weaker_rules(COMMITTED, live)
                self.assertEqual(len(findings), 1, findings)
                self.assertIn(f"rule `{rule_type}`", findings[0])

    def test_no_live_rules_at_all_fails_once_per_committed_rule(self) -> None:
        self.assertEqual(len(find_weaker_rules(COMMITTED, [])), len(COMMITTED["rules"]))

    def test_extra_and_stricter_live_rules_pass(self) -> None:
        live = live_from(COMMITTED)
        live_rule(live, "pull_request")["parameters"]["required_approving_review_count"] = 2
        live_rule(live, "pull_request")["parameters"]["require_last_push_approval"] = True
        live_rule(live, "required_status_checks")["parameters"]["required_status_checks"].append(
            {"context": "Build PR Image (No Push)", "integration_id": 15368}
        )
        live.append({"type": "required_linear_history", "ruleset_id": 1})
        live.append({"type": "required_signatures", "ruleset_id": 1})
        self.assertEqual(find_weaker_rules(COMMITTED, live), [])

    def test_duplicate_live_rules_from_two_rulesets_take_the_strongest(self) -> None:
        # A second ruleset can carry the same rule type. GitHub enforces both, so a weak copy
        # next to a strong one is not drift; the strong one decides.
        weak = live_from(COMMITTED, source="Danathar")
        live_rule(weak, "pull_request")["parameters"].update(
            {"required_approving_review_count": 0, "require_code_owner_review": False}
        )
        live_rule(weak, "required_status_checks")["parameters"] = {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [],
        }
        strong = live_from(COMMITTED)
        self.assertEqual(find_weaker_rules(COMMITTED, weak + strong), [])
        self.assertEqual(find_weaker_rules(COMMITTED, strong + weak), [])
        # On its own the weak copy misses code owner review, the approval count, strict mode
        # and both checks.
        self.assertEqual(len(find_weaker_rules(COMMITTED, weak)), 5)

    def test_required_checks_split_across_two_rulesets_still_count(self) -> None:
        live = [rule for rule in live_from(COMMITTED) if rule["type"] != "required_status_checks"]
        live.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "Lint"}],
                },
            }
        )
        live.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "Python Unit Tests", "integration_id": 15368}
                    ],
                },
            }
        )
        self.assertEqual(find_weaker_rules(COMMITTED, live), [])


class CommittedFileTests(unittest.TestCase):
    """The file the job reads is the real one, and it parses into rules this module compares."""

    def setUp(self) -> None:
        self.committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))

    def test_the_job_reads_the_committed_ruleset(self) -> None:
        self.assertEqual(RULESET_FILE, REPO_ROOT / ".github" / "rulesets" / "main.json")

    def test_the_committed_file_applied_as_is_passes(self) -> None:
        self.assertTrue(self.committed["rules"])
        self.assertEqual(find_weaker_rules(self.committed, live_from(self.committed)), [])

    def test_the_live_state_issue_454_left_fails(self) -> None:
        # What GitHub enforced on main when #488 was filed: everything in the file, except
        # code owner review.
        live = live_from(self.committed)
        live_rule(live, "pull_request")["parameters"]["require_code_owner_review"] = False
        findings = find_weaker_rules(self.committed, live)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("require_code_owner_review", findings[0])


class RenderReportTests(unittest.TestCase):
    def test_a_clean_report_says_so(self) -> None:
        report = render_report("main", COMMITTED, live_from(COMMITTED), [])
        self.assertIn("Branch: `main`", report)
        self.assertIn("at least as strictly", report)
        self.assertNotIn("weaker", report)

    def test_a_failing_report_lists_each_finding_and_the_fix(self) -> None:
        report = render_report("main", COMMITTED, [], ["one", "two"])
        self.assertIn("**weaker**", report)
        self.assertIn("- one\n- two\n", report)
        self.assertIn("Live rule types: none", report)
        self.assertIn("docs/branch-protection.md", report)

    def test_the_header_names_the_file_it_compared(self) -> None:
        self.assertIn("(`.github/rulesets/main.json`)", render_report("main", COMMITTED, [], []))
        report = render_report("master", COMMITTED, [], [], ruleset_file=ruleset_path("master"))
        self.assertIn("Committed rules (`.github/rulesets/master.json`):", report)

    def test_the_fix_the_report_points_at_is_on_the_page(self) -> None:
        page = (REPO_ROOT / "docs" / "branch-protection.md").read_text(encoding="utf-8")
        self.assertIn("## Applying and changing it", page)
        self.assertIn("--method PUT", page)
        self.assertIn('"Applying and changing it"', render_report("main", COMMITTED, [], ["x"]))


class FakeApi:
    """Stands in for `_get`: answers the repository call and pages the rules call."""

    def __init__(self, pages: list[list[dict]], default_branch: str = "main") -> None:
        self.pages = pages
        self.default_branch = default_branch
        self.paths: list[str] = []
        self.tokens: set[str] = set()

    def __call__(self, path: str, token: str) -> tuple[object, str]:
        self.paths.append(path)
        self.tokens.add(token)
        if "/rules/branches/" not in path:
            return {"default_branch": self.default_branch}, ""
        page = int(path.rsplit("page=", 1)[1])
        link = '<https://api.github.com/next>; rel="next"' if page < len(self.pages) else ""
        return self.pages[page - 1], link


class FetchLiveRulesTests(unittest.TestCase):
    def test_reads_the_default_branch_and_every_page(self) -> None:
        api = FakeApi([[{"type": "deletion"}], [{"type": "non_fast_forward"}]], "trunk")
        with mock.patch.object(check_ruleset_drift, "_get", api):
            branch, rules = fetch_live_rules("Danathar/zfs-kinoite-complex", "t0ken")
        self.assertEqual(branch, "trunk")
        self.assertEqual([rule["type"] for rule in rules], ["deletion", "non_fast_forward"])
        self.assertEqual(api.paths[0], "/repos/Danathar/zfs-kinoite-complex")
        rules_path = "/repos/Danathar/zfs-kinoite-complex/rules/branches/trunk?"
        self.assertTrue(all(path.startswith(rules_path) for path in api.paths[1:]), api.paths)
        self.assertEqual(len(api.paths), 3)

    def test_uses_the_effective_rules_endpoint_not_the_admin_only_ruleset(self) -> None:
        api = FakeApi([[]])
        with mock.patch.object(check_ruleset_drift, "_get", api):
            fetch_live_rules("o/r", "t")
        self.assertFalse(any("/rulesets" in path for path in api.paths), api.paths)

    def test_get_sends_the_token_and_parses_json(self) -> None:
        class Response(io.BytesIO):
            headers: ClassVar[dict[str, str]] = {"Link": 'rel="next"'}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(
            check_ruleset_drift.urllib.request, "urlopen", return_value=Response(b'[{"type": "x"}]')
        ) as urlopen:
            data, link = check_ruleset_drift._get("/repos/o/r", "t0ken")
        self.assertEqual(data, [{"type": "x"}])
        self.assertEqual(link, 'rel="next"')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/o/r")
        self.assertEqual(request.get_header("Authorization"), "Bearer t0ken")


class MainTests(unittest.TestCase):
    def _run(self, live: list[dict], *, summary: bool = True) -> tuple[str, str, Exception | None]:
        committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))
        api = FakeApi([live])
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            env = {"GITHUB_TOKEN": "t0ken", "GITHUB_REPOSITORY": "Danathar/zfs-kinoite-complex"}
            if summary:
                env["GITHUB_STEP_SUMMARY"] = str(summary_path)
            out = io.StringIO()
            error = None
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(check_ruleset_drift, "_get", api),
                redirect_stdout(out),
            ):
                try:
                    check_ruleset_drift.main()
                except CiToolError as exc:
                    error = exc
            written = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        self.assertEqual(api.tokens, {"t0ken"})
        self.assertIn(committed["rules"][0]["type"], out.getvalue())
        return out.getvalue(), written, error

    def test_matching_live_rules_pass_and_reach_the_summary(self) -> None:
        committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))
        stdout, summary, error = self._run(live_from(committed))
        self.assertIsNone(error)
        self.assertIn("at least as strictly", stdout)
        self.assertEqual(summary, stdout.rstrip("\n") + "\n")

    def test_weaker_live_rules_fail_the_run(self) -> None:
        committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))
        live = live_from(committed)
        live_rule(live, "pull_request")["parameters"]["require_code_owner_review"] = False
        stdout, summary, error = self._run(live)
        self.assertIsInstance(error, CiToolError)
        self.assertIn("weaker", str(error))
        self.assertIn("require_code_owner_review", stdout)
        self.assertIn("require_code_owner_review", summary)

    def test_without_a_step_summary_it_still_prints(self) -> None:
        stdout, summary, error = self._run([], summary=False)
        self.assertIsInstance(error, CiToolError)
        self.assertIn("**weaker**", stdout)
        self.assertEqual(summary, "")

    def test_a_missing_token_is_refused(self) -> None:
        with (
            mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r"}, clear=True),
            self.assertRaisesRegex(CiToolError, "GITHUB_TOKEN"),
        ):
            check_ruleset_drift.main()


class FleetApi:
    """
    Stands in for `_get` across several repositories.

    `repos` maps a repository to its default branch, its committed ruleset (`None` answers the
    contents call with a 404, as GitHub does for a missing file) and its live rules.
    """

    def __init__(self, repos: dict[str, tuple[str, dict | None, list[dict]]]) -> None:
        self.repos = repos
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, path: str, token: str, accept: str = "application/vnd.github+json"
    ) -> tuple[object, str]:
        self.calls.append((path, accept))
        repository = "/".join(path.split("/")[2:4])
        branch, committed, live = self.repos[repository]
        if "/contents/" in path:
            if committed is None:
                raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
            return copy.deepcopy(committed), ""
        if "/rules/branches/" in path:
            return copy.deepcopy(live), ""
        return {"default_branch": branch}, ""


def weaker(committed: dict) -> list[dict]:
    """`committed` applied as-is, except code owner review, which is off: the #454 state."""

    live = live_from(committed, source="Danathar/weaker")
    live_rule(live, "pull_request")["parameters"]["require_code_owner_review"] = False
    return live


class FleetListTests(unittest.TestCase):
    def test_the_fleet_is_every_other_repository_once(self) -> None:
        self.assertEqual(len(FLEET), 11)
        self.assertEqual(len(set(FLEET)), len(FLEET))
        self.assertNotIn("Danathar/zfs-kinoite-complex", FLEET)
        self.assertTrue(all(repository.startswith("Danathar/") for repository in FLEET))
        self.assertIn("Danathar/exult", FLEET)
        self.assertIn("Danathar/sensi", FLEET)

    def test_the_nightly_job_runs_the_fleet_check(self) -> None:
        workflow = REPO_ROOT / ".github" / "workflows" / "nightly-compliance.yml"
        self.assertIn(
            "python3 -m ci_tools.cli check-ruleset-drift --fleet",
            workflow.read_text(encoding="utf-8"),
        )


class FetchCommittedTests(unittest.TestCase):
    def test_a_master_repository_reads_master_json_from_master(self) -> None:
        api = FleetApi({"Danathar/sensi": ("master", COMMITTED, [])})
        with mock.patch.object(check_ruleset_drift, "_get", api):
            committed = fetch_committed("Danathar/sensi", "master", "t")
        self.assertEqual(committed, COMMITTED)
        self.assertEqual(
            api.calls,
            [
                (
                    "/repos/Danathar/sensi/contents/.github/rulesets/master.json?ref=master",
                    RAW_MEDIA_TYPE,
                )
            ],
        )

    def test_a_main_repository_reads_main_json(self) -> None:
        self.assertEqual(ruleset_path("main"), ".github/rulesets/main.json")
        self.assertEqual(ruleset_path("master"), ".github/rulesets/master.json")

    def test_a_file_with_no_rules_is_refused(self) -> None:
        for body in ({"name": "x"}, {"rules": []}, {"rules": "deletion"}, []):
            with self.subTest(body=body):
                api = FleetApi({"o/r": ("main", body, [])})
                with (
                    mock.patch.object(check_ruleset_drift, "_get", api),
                    self.assertRaisesRegex(CiToolError, "no `rules` list"),
                ):
                    fetch_committed("o/r", "main", "t")

    def test_get_sends_the_raw_media_type_when_asked(self) -> None:
        class Response(io.BytesIO):
            headers: ClassVar[dict[str, str]] = {}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(
            check_ruleset_drift.urllib.request, "urlopen", return_value=Response(b'{"rules": []}')
        ) as urlopen:
            check_ruleset_drift._get("/repos/o/r/contents/x", "t", accept=RAW_MEDIA_TYPE)
        self.assertEqual(urlopen.call_args.args[0].get_header("Accept"), RAW_MEDIA_TYPE)


class FleetSectionTests(unittest.TestCase):
    def _section(self, repository: str, spec: tuple) -> tuple[str, bool]:
        with mock.patch.object(check_ruleset_drift, "_get", FleetApi({repository: spec})):
            return fleet_section(repository, "t")

    def test_a_clean_repository_passes_and_names_its_file(self) -> None:
        section, fails = self._section(
            "Danathar/exult", ("master", COMMITTED, live_from(COMMITTED, "Danathar/exult"))
        )
        self.assertFalse(fails)
        self.assertTrue(section.startswith("## `Danathar/exult`\n"), section)
        self.assertIn("Branch: `master`", section)
        self.assertIn("(`.github/rulesets/master.json`)", section)
        self.assertIn("at least as strictly", section)
        self.assertNotIn("PUT", section)

    def test_a_weaker_repository_fails_with_its_own_put(self) -> None:
        section, fails = self._section("Danathar/weaker", ("main", COMMITTED, weaker(COMMITTED)))
        self.assertTrue(fails)
        self.assertIn("require_code_owner_review", section)
        self.assertIn(
            "`gh api --method PUT repos/Danathar/weaker/rulesets/23959550 "
            "--input .github/rulesets/main.json`",
            section,
        )

    def test_a_put_without_one_ruleset_id_leaves_a_placeholder(self) -> None:
        # Rules from another source carry someone else's id; that is not the one to PUT.
        live = live_from(COMMITTED, source="Danathar")
        live_rule(live, "pull_request")["parameters"]["require_code_owner_review"] = False
        section, fails = self._section("Danathar/weaker", ("main", COMMITTED, live))
        self.assertTrue(fails)
        self.assertIn("repos/Danathar/weaker/rulesets/<id> ", section)

    def test_an_unreadable_file_fails(self) -> None:
        section, fails = self._section("Danathar/unread", ("main", None, []))
        self.assertTrue(fails)
        self.assertIn("## `Danathar/unread`", section)
        self.assertIn("Could not read", section)
        self.assertIn("404", section)


class FleetMainTests(unittest.TestCase):
    REPOS: ClassVar[dict[str, tuple]] = {
        "Danathar/clean": ("main", COMMITTED, live_from(COMMITTED, "Danathar/clean")),
        "Danathar/weaker": ("master", COMMITTED, weaker(COMMITTED)),
        "Danathar/unread": ("main", None, []),
    }

    def _run(self, repos: dict[str, tuple]) -> tuple[str, str, Exception | None]:
        api = FleetApi(repos)
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.md"
            # No GITHUB_REPOSITORY: the fleet run does not read this repository.
            env = {"GITHUB_TOKEN": "t0ken", "GITHUB_STEP_SUMMARY": str(summary_path)}
            out = io.StringIO()
            error = None
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(check_ruleset_drift, "_get", api),
                mock.patch.object(check_ruleset_drift, "FLEET", tuple(repos)),
                redirect_stdout(out),
            ):
                try:
                    check_ruleset_drift.main(["--fleet"])
                except CiToolError as exc:
                    error = exc
            written = summary_path.read_text(encoding="utf-8")
        return out.getvalue(), written, error

    def test_one_section_per_repository_and_the_failures_named(self) -> None:
        stdout, summary, error = self._run(self.REPOS)
        self.assertEqual(summary, stdout.rstrip("\n") + "\n")
        for repository in self.REPOS:
            self.assertEqual(stdout.count(f"## `{repository}`"), 1, repository)
        self.assertIsInstance(error, CiToolError)
        self.assertIn("in 2 of 3 repositories: Danathar/weaker, Danathar/unread", str(error))
        self.assertNotIn("Danathar/clean", str(error))
        self.assertIn("(`.github/rulesets/master.json`)", stdout)
        self.assertIn("--input .github/rulesets/master.json", stdout)

    def test_a_fleet_with_no_findings_passes(self) -> None:
        stdout, _, error = self._run({"Danathar/clean": self.REPOS["Danathar/clean"]})
        self.assertIsNone(error)
        self.assertIn("at least as strictly", stdout)

    def test_an_unreadable_repository_alone_fails_the_run(self) -> None:
        stdout, _, error = self._run(
            {
                "Danathar/clean": self.REPOS["Danathar/clean"],
                "Danathar/unread": self.REPOS["Danathar/unread"],
            }
        )
        self.assertIsInstance(error, CiToolError)
        self.assertIn("Danathar/unread", str(error))
        self.assertIn("Could not read", stdout)

    def test_an_unknown_option_is_refused(self) -> None:
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            check_ruleset_drift.main(["--fleat"])


if __name__ == "__main__":
    unittest.main()
