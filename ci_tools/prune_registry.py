"""
Script: ci_tools/prune_registry.py
What: Decides which GHCR package versions to keep, and optionally deletes the rest.
Doing: Lists every version of each container package, classifies each by its
tags and age, prints the plan, and deletes only when asked to.
Why: Nothing removed old images, so the registry grew by about 11 tags a day,
and the "transient" `-unsigned-<run>` tags were neither transient nor unsigned
(#308).
Goal: Bounded growth without ever deleting `latest`, a recent build, a recent
`stable-*` rollback target, or anything this tool does not recognise.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ci_tools.common import CiToolError

API = "https://api.github.com"
DEFAULT_PACKAGES = ("zfs-kinoite-complex", "zfs-kinoite-complex-akmods")
KEEP_STABLE = 10
MIN_AGE = timedelta(days=14)

# Tag families. A version is deleted only when every one of its tags is in a
# deletable family; one unknown tag keeps it. Unknown means "keep", never "go".
LATEST_RE = re.compile(r"^latest$")
STABLE_RE = re.compile(r"^stable-\d+-[0-9a-f]+$")
DELETABLE_RES = (
    re.compile(r"^candidate-[0-9a-f]+-\d+$"),  # per-commit candidates
    re.compile(r"^.+-unsigned-\d+$"),  # publish-native-image's pre-sign tag
    re.compile(r"^br-.+$"),  # unsigned throwaway branch images
)
SIG_RE = re.compile(r"^sha256-(?P<hex>[0-9a-f]{64})\.sig$")


@dataclass(frozen=True)
class Version:
    """One package version as the Packages API reports it."""

    id: int
    digest: str
    created: datetime
    tags: tuple[str, ...]


@dataclass
class Plan:
    """Which versions to keep and delete, with the reason for each."""

    keep: dict[int, str] = field(default_factory=dict)
    delete: dict[int, str] = field(default_factory=dict)


def plan_versions(versions: list[Version], now: datetime) -> Plan:
    """
    Decide each version's fate. Pure: no network, so the rule is testable.

    Order matters: images first, then signatures, because a signature is kept
    exactly when the image it signs is kept.
    """

    plan = Plan()
    newest_stable = {
        v.id
        for v in sorted(
            (v for v in versions if any(STABLE_RE.match(t) for t in v.tags)),
            key=lambda v: v.created,
            reverse=True,
        )[:KEEP_STABLE]
    }

    signatures: list[Version] = []
    for v in versions:
        image_tags = [t for t in v.tags if not SIG_RE.match(t)]
        if v.tags and not image_tags:
            signatures.append(v)
            continue
        if not v.tags:
            plan.keep[v.id] = "untagged (may belong to a multi-arch image)"
        elif any(LATEST_RE.match(t) for t in image_tags):
            plan.keep[v.id] = "tagged latest"
        elif v.id in newest_stable:
            plan.keep[v.id] = f"one of the newest {KEEP_STABLE} stable-* tags"
        elif now - v.created < MIN_AGE:
            plan.keep[v.id] = f"younger than {MIN_AGE.days} days"
        elif all(_deletable(t) for t in image_tags):
            plan.delete[v.id] = "only throwaway tags: " + ", ".join(image_tags)
        else:
            plan.keep[v.id] = "has a tag this tool does not recognise"

    by_digest = {v.digest: v for v in versions}
    for sig in signatures:
        subjects = [by_digest.get("sha256:" + m.group("hex")) for t in sig.tags if (m := SIG_RE.match(t))]
        live = [s for s in subjects if s is not None]
        if any(s.id in plan.keep for s in live):
            plan.keep[sig.id] = "signs a kept image"
        elif live:
            plan.delete[sig.id] = "signs only deleted images"
        elif now - sig.created < MIN_AGE:
            plan.keep[sig.id] = f"orphan signature, younger than {MIN_AGE.days} days"
        else:
            plan.delete[sig.id] = "orphan signature: the image it signs is gone"
    return plan


def _deletable(tag: str) -> bool:
    if STABLE_RE.match(tag):
        return True  # reached only for stable-* tags outside the newest KEEP_STABLE
    return any(r.match(tag) for r in DELETABLE_RES)


def _request(method: str, path: str, token: str) -> tuple[object, str]:
    req = urllib.request.Request(
        API + path,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return (json.loads(body) if body else None), resp.headers.get("Link", "")


def list_versions(owner: str, package: str, token: str) -> list[Version]:
    """Every version of a user-owned container package, all pages."""

    out: list[Version] = []
    page = 1
    while True:
        data, link = _request(
            "GET", f"/users/{owner}/packages/container/{package}/versions?per_page=100&page={page}", token
        )
        assert isinstance(data, list)
        for item in data:
            out.append(
                Version(
                    id=int(item["id"]),
                    digest=item["name"],
                    created=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
                    tags=tuple(item.get("metadata", {}).get("container", {}).get("tags", [])),
                )
            )
        if 'rel="next"' not in link:
            return out
        page += 1


def main() -> None:
    """List, plan and (only with PRUNE_DELETE=true) delete. Dry run otherwise."""

    token = os.environ.get("GITHUB_TOKEN", "")
    owner = os.environ.get("PACKAGE_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not token or not owner:
        raise CiToolError("GITHUB_TOKEN and PACKAGE_OWNER (or GITHUB_REPOSITORY_OWNER) are required")
    packages = tuple(p for p in os.environ.get("PACKAGES", " ".join(DEFAULT_PACKAGES)).split() if p)
    delete = os.environ.get("PRUNE_DELETE", "false") == "true"
    now = datetime.now(timezone.utc)

    failures = 0
    for package in packages:
        versions = list_versions(owner, package, token)
        plan = plan_versions(versions, now)
        if package == DEFAULT_PACKAGES[0] and not any(LATEST_RE.match(t) for v in versions for t in v.tags):
            raise CiToolError(f"{package} has no version tagged latest; refusing to plan against it")
        print(f"== {package}: {len(versions)} versions, keep {len(plan.keep)}, delete {len(plan.delete)}")
        by_id = {v.id: v for v in versions}
        for vid, why in sorted(plan.delete.items(), key=lambda kv: by_id[kv[0]].created):
            v = by_id[vid]
            print(f"  delete {vid} {v.created:%Y-%m-%d} {v.digest[:19]} {','.join(v.tags) or '-'}  ({why})")
        if not delete:
            continue
        for vid in plan.delete:
            try:
                _request("DELETE", f"/users/{owner}/packages/container/{package}/versions/{vid}", token)
            except Exception as exc:  # noqa: BLE001 - report every failure, keep going
                failures += 1
                print(f"  FAILED to delete {vid}: {exc}", file=sys.stderr)
    print("mode: " + ("delete" if delete else "dry run (nothing deleted)"))
    if failures:
        raise CiToolError(f"{failures} deletion(s) failed")
