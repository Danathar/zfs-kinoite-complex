"""
Script: ci_tools/check_ruleset_drift.py
What: Compares the committed `.github/rulesets/main.json` (plus
`main-checks.json`, when there is one) with the rules GitHub actually enforces on
the default branch.
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

A repository may split its rules over two rulesets: `{branch}.json` and
`{branch}-checks.json`, which holds the required status checks in a ruleset of
their own. A bypass actor skips only the rules of the ruleset that lists it, so
the split lets one bot merge past code owner review while the checks still
apply to it. When the second file exists its rules are added to the first's
before the comparison; when it does not, nothing changes. GitHub's endpoint
already returns the rules of both rulesets in one list.

`--fleet` runs the same comparison for every repository in `FLEET` instead of
this one, with one report section each (#490). They are public, so the same
token reads each one's committed `.github/rulesets/{branch}.json`, and its
`{branch}-checks.json` when it has one, through the contents API and its effective rules through the endpoint above. One job here
covers all of them, instead of a copy of this script in each. The run fails when
any repository has a finding, or when its file or its rules cannot be read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence

from ci_tools.common import REPO_ROOT, CiToolError, require_env

API = "https://api.github.com"
RULESET_FILE = REPO_ROOT / ".github" / "rulesets" / "main.json"
# The required checks, in a ruleset of their own so that a bypass on the first one cannot skip them.
CHECKS_RULESET_FILE = REPO_ROOT / ".github" / "rulesets" / "main-checks.json"
JSON_MEDIA_TYPE = "application/vnd.github+json"
# The contents API returns the file itself under this media type, rather than base64 in JSON.
RAW_MEDIA_TYPE = "application/vnd.github.raw+json"

# Every other repository that commits a ruleset file. This one is checked by the plain run.
FLEET = (
    "Danathar/akmods",
    "Danathar/appbox",
    "Danathar/arch-bootc",
    "Danathar/atomic-image-builder",
    "Danathar/aurora-zfs-simple",
    "Danathar/dns-failover",
    "Danathar/exult",
    "Danathar/goodreads-mcp",
    "Danathar/openaudible-autoupdate",
    "Danathar/sensi",
    "Danathar/tududi",
    "Danathar/zfs-aurora-complex",
)


def ruleset_path(branch: str) -> str:
    """The committed ruleset for `branch`, named after it: `master.json` on a `master` repo."""

    return f".github/rulesets/{branch}.json"


def checks_ruleset_path(branch: str) -> str:
    """The optional second ruleset for `branch`, holding the required checks on their own."""

    return f".github/rulesets/{branch}-checks.json"


def merge_rulesets(ruleset: Mapping, checks: Mapping | None) -> dict:
    """
    `ruleset` with the rules of `checks` added, or `ruleset` unchanged when `checks` is `None`.

    GitHub enforces every ruleset that targets the branch, so the rules the two files ask for
    together are the rules the live branch has to meet.
    """

    merged = dict(ruleset)
    merged["rules"] = [*ruleset.get("rules", []), *((checks or {}).get("rules", []))]
    return merged


def load_committed() -> tuple[dict, list[str]]:
    """This repository's committed rules, both files merged, and the files they came from."""

    committed = json.loads(RULESET_FILE.read_text(encoding="utf-8"))
    files = [ruleset_path("main")]
    if CHECKS_RULESET_FILE.exists():
        checks = json.loads(CHECKS_RULESET_FILE.read_text(encoding="utf-8"))
        committed = merge_rulesets(committed, checks)
        files.append(checks_ruleset_path("main"))
    return committed, files


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
    branch: str,
    committed: Mapping,
    live_rules: Sequence[Mapping],
    findings: Sequence[str],
    ruleset_file: str | Sequence[str] = ".github/rulesets/main.json",
) -> str:
    """The Markdown the job prints and writes to the step summary."""

    files = [ruleset_file] if isinstance(ruleset_file, str) else list(ruleset_file)
    named = " + ".join(f"`{name}`" for name in files)
    committed_types = ", ".join(f"`{rule['type']}`" for rule in committed.get("rules", []))
    live_types = ", ".join(sorted({f"`{rule.get('type')}`" for rule in live_rules})) or "none"
    lines = [
        "### Ruleset drift",
        "",
        f"Branch: `{branch}`",
        f"Committed rules ({named}): {committed_types}",
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


def _get(path: str, token: str, accept: str = JSON_MEDIA_TYPE) -> tuple[object, str]:
    request = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
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


def fetch_committed(repository: str, branch: str, token: str) -> dict:
    """
    Return the ruleset `repository` commits for `branch`, read through the contents API.

    Raises `CiToolError` when the file parses but holds no rules, so an emptied file is reported
    as unreadable rather than passing with nothing to compare.
    """

    path = urllib.parse.quote(ruleset_path(branch))
    data, _ = _get(
        f"/repos/{repository}/contents/{path}?ref={urllib.parse.quote(branch)}",
        token,
        accept=RAW_MEDIA_TYPE,
    )
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise CiToolError(f"{ruleset_path(branch)} holds no `rules` list")
    return data


def fetch_committed_checks(repository: str, branch: str, token: str) -> dict | None:
    """
    Return the `{branch}-checks.json` ruleset `repository` commits, or `None` when it has none.

    Only a 404 means "none": a repository that keeps every rule in one file is not an error.
    Any other failure is raised, and a file that exists but holds no rules is refused the way
    `fetch_committed` refuses one.
    """

    path = urllib.parse.quote(checks_ruleset_path(branch))
    try:
        data, _ = _get(
            f"/repos/{repository}/contents/{path}?ref={urllib.parse.quote(branch)}",
            token,
            accept=RAW_MEDIA_TYPE,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise CiToolError(f"{checks_ruleset_path(branch)} holds no `rules` list")
    return data


def _put_command(
    repository: str,
    branch: str,
    live_rules: Sequence[Mapping],
    path: str | None = None,
    rule_types: set[str] | None = None,
) -> str:
    """
    The `PUT` an admin of `repository` runs, with the ruleset id when the live rules name one.

    `path` is the file to apply, `{branch}.json` by default. With `rule_types`, only live rules
    of those types are read for the id, so each of two files gets the id of its own ruleset.
    """

    ids = sorted(
        {
            str(rule["ruleset_id"])
            for rule in live_rules
            if rule.get("ruleset_source") == repository
            and "ruleset_id" in rule
            and (rule_types is None or rule.get("type") in rule_types)
        }
    )
    ruleset = ids[0] if len(ids) == 1 else "<id>"
    return (
        f"gh api --method PUT repos/{repository}/rulesets/{ruleset} "
        f"--input {path or ruleset_path(branch)}"
    )


def fleet_section(repository: str, token: str) -> tuple[str, bool]:
    """
    Return one repository's report section, and whether it fails the run.

    A file or rules that cannot be read fail it too: a repository the check cannot see is not one
    it has checked.
    """

    heading = f"## `{repository}`\n\n"
    try:
        branch, live_rules = fetch_live_rules(repository, token)
        main_file = fetch_committed(repository, branch, token)
        checks_file = fetch_committed_checks(repository, branch, token)
    except (OSError, ValueError, CiToolError) as exc:
        return heading + f"Could not read the committed ruleset or the live rules: {exc}\n", True

    committed = merge_rulesets(main_file, checks_file)
    files = [ruleset_path(branch)]
    if checks_file is not None:
        files.append(checks_ruleset_path(branch))
    findings = find_weaker_rules(committed, live_rules)
    section = heading + render_report(branch, committed, live_rules, findings, ruleset_file=files)
    if findings:
        if checks_file is None:
            puts = [_put_command(repository, branch, live_rules)]
        else:
            puts = [
                _put_command(
                    repository,
                    branch,
                    live_rules,
                    path,
                    {rule["type"] for rule in ruleset["rules"]},
                )
                for path, ruleset in ((files[0], main_file), (files[1], checks_file))
            ]
        commands = " and ".join(f"`{put}`" for put in puts)
        section += f"\nIn `{repository}`, from a checkout of `{branch}`: {commands}\n"
    return section, bool(findings)


def _publish(report: str) -> None:
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python3 -m ci_tools.cli check-ruleset-drift",
        description="Fail when GitHub's live branch rules are weaker than the committed ruleset.",
    )
    parser.add_argument(
        "--fleet",
        action="store_true",
        help="check every repository in FLEET instead of this one",
    )
    # Never sys.argv: through ci_tools.cli that still holds the command name.
    args = parser.parse_args(list(argv or []))
    token = require_env("GITHUB_TOKEN")

    if args.fleet:
        failed = []
        sections = []
        for repository in FLEET:
            section, fails = fleet_section(repository, token)
            sections.append(section)
            if fails:
                failed.append(repository)
        _publish("\n".join(sections))
        if failed:
            raise CiToolError(
                "Live rules are weaker than the committed ruleset, or could not be read, in "
                f"{len(failed)} of {len(FLEET)} repositories: {', '.join(failed)}"
            )
        return

    repository = require_env("GITHUB_REPOSITORY")
    committed, files = load_committed()

    branch, live_rules = fetch_live_rules(repository, token)
    findings = find_weaker_rules(committed, live_rules)
    report = render_report(branch, committed, live_rules, findings, ruleset_file=files)
    _publish(report)

    if findings:
        raise CiToolError(
            f"Live rules on {branch} are weaker than {' + '.join(files)} "
            f"({len(findings)} finding(s))"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
