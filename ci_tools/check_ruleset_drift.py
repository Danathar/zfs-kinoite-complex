"""
Script: ci_tools/check_ruleset_drift.py
What: Compares the committed `.github/rulesets/main.json` with the rules GitHub
actually enforces on the default branch.
Doing: Reads the committed ruleset, reads the branch's effective rules from
`GET /repos/{owner}/{repo}/rules/branches/{branch}`, prints a report, and fails
when any live rule is weaker than the committed one.
Why: Applying the file to GitHub is a separate owner-only `PUT`, and nothing
noticed when it was skipped: the file asked for code owner review while the
live ruleset did not (#454, #488).
Goal: A live ruleset that has fallen behind the committed file turns the nightly
run red within a day, instead of being found by accident.

Asymmetric on purpose. Only a live rule that is missing or weaker than the
file's fails. An extra rule, or a stricter one, passes: GitHub enforcing more
than the file asks for is not a hole.

The effective-rules endpoint is used rather than `/rulesets/<id>` because it
needs no admin rights, so the default `GITHUB_TOKEN` with `contents: read` can
read it. It returns a flat list of rules from every ruleset that applies to the
branch, so one rule type can appear more than once. Each comparison takes the
strongest live entry of that type, which is what GitHub enforces.

What it does not compare: the bypass list, the target conditions and the
`pull_request` settings other than code owner review and the approval count.
The effective-rules endpoint does not return the first two.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence

from ci_tools.common import REPO_ROOT, CiToolError, require_env

API = "https://api.github.com"
RULESET_FILE = REPO_ROOT / ".github" / "rulesets" / "main.json"


def _live_parameters(live_rules: Sequence[Mapping], rule_type: str) -> list[Mapping]:
    """The `parameters` of every live rule of one type, `{}` for a rule that has none."""

    return [rule.get("parameters") or {} for rule in live_rules if rule.get("type") == rule_type]


def _check_label(check: Mapping) -> str:
    if check.get("integration_id") is None:
        return f"`{check['context']}`"
    return f"`{check['context']}` (integration_id {check['integration_id']})"


def find_weaker_rules(committed: Mapping, live_rules: Sequence[Mapping]) -> list[str]:
    """
    Return one sentence per way the live rules are weaker than `committed`.

    `committed` is the ruleset file as parsed JSON. `live_rules` is the list the
    effective-rules endpoint returns. An empty result means every committed rule
    is enforced at least as strictly as the file asks.
    """

    findings: list[str] = []
    for rule in committed.get("rules", []):
        rule_type = rule["type"]
        wanted = rule.get("parameters") or {}
        live = _live_parameters(live_rules, rule_type)
        if not live:
            findings.append(
                f"rule `{rule_type}` is in the committed file but GitHub does not enforce it"
            )
            continue

        if rule_type == "pull_request":
            if wanted.get("require_code_owner_review") and not any(
                params.get("require_code_owner_review") for params in live
            ):
                findings.append(
                    "`pull_request`: `require_code_owner_review` is true in the file but false live"
                )
            wanted_count = int(wanted.get("required_approving_review_count") or 0)
            live_count = max(
                int(params.get("required_approving_review_count") or 0) for params in live
            )
            if live_count < wanted_count:
                findings.append(
                    "`pull_request`: `required_approving_review_count` is "
                    f"{wanted_count} in the file but {live_count} live"
                )

        if rule_type == "required_status_checks":
            if wanted.get("strict_required_status_checks_policy") and not any(
                params.get("strict_required_status_checks_policy") for params in live
            ):
                findings.append(
                    "`required_status_checks`: `strict_required_status_checks_policy` is true "
                    "in the file but false live"
                )
            live_checks = [
                check for params in live for check in params.get("required_status_checks") or []
            ]
            for check in wanted.get("required_status_checks") or []:
                matched = any(
                    found.get("context") == check["context"]
                    and (
                        check.get("integration_id") is None
                        or found.get("integration_id") == check["integration_id"]
                    )
                    for found in live_checks
                )
                if not matched:
                    findings.append(
                        f"`required_status_checks`: {_check_label(check)} is required in the file "
                        "but not live"
                    )
    return findings


def render_report(
    branch: str, committed: Mapping, live_rules: Sequence[Mapping], findings: Sequence[str]
) -> str:
    """The Markdown the job prints and writes to the step summary."""

    committed_types = ", ".join(f"`{rule['type']}`" for rule in committed.get("rules", []))
    live_types = ", ".join(sorted({f"`{rule.get('type')}`" for rule in live_rules})) or "none"
    lines = [
        "### Ruleset drift",
        "",
        f"Branch: `{branch}`",
        f"Committed rules (`.github/rulesets/main.json`): {committed_types}",
        f"Live rule types: {live_types}",
        "",
    ]
    if not findings:
        lines.append("GitHub enforces every committed rule at least as strictly as the file asks.")
        return "\n".join(lines) + "\n"

    lines.append("GitHub's live rules are **weaker** than the committed file:")
    lines.append("")
    lines.extend(f"- {finding}" for finding in findings)
    lines.append("")
    lines.append(
        "A repository admin applies the file with the `PUT` command in docs/branch-protection.md, "
        "\"Applying and changing it\"."
    )
    return "\n".join(lines) + "\n"


def _get(path: str, token: str) -> tuple[object, str]:
    request = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read()), response.headers.get("Link", "")


def fetch_live_rules(repository: str, token: str) -> tuple[str, list[dict]]:
    """Return the default branch and every effective rule on it, all pages."""

    repo, _ = _get(f"/repos/{repository}", token)
    assert isinstance(repo, dict)
    branch = str(repo["default_branch"])

    rules: list[dict] = []
    page = 1
    while True:
        path = f"/repos/{repository}/rules/branches/{urllib.parse.quote(branch)}"
        data, link = _get(f"{path}?per_page=100&page={page}", token)
        assert isinstance(data, list)
        rules.extend(data)
        if 'rel="next"' not in link:
            return branch, rules
        page += 1


def main() -> None:
    token = require_env("GITHUB_TOKEN")
    repository = require_env("GITHUB_REPOSITORY")
    committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))

    branch, live_rules = fetch_live_rules(repository, token)
    findings = find_weaker_rules(committed, live_rules)
    report = render_report(branch, committed, live_rules, findings)

    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)

    if findings:
        raise CiToolError(
            f"Live rules on {branch} are weaker than .github/rulesets/main.json "
            f"({len(findings)} finding(s))"
        )


if __name__ == "__main__":
    main()
