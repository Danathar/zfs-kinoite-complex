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
REGISTRY = "https://ghcr.io"
IMAGE_PACKAGE = "zfs-kinoite-complex"
AKMODS_PACKAGE = "zfs-kinoite-complex-akmods"
DEFAULT_PACKAGES = (IMAGE_PACKAGE, AKMODS_PACKAGE)
# Packages whose untagged versions may be pruned. Rebuilding the akmods cache
# moves its mutable `main-<fedora>` tags to the new build and leaves the old one
# untagged, so without this its history grows forever. The image package keeps
# untagged versions: they can be pieces of a multi-arch image.
PRUNE_UNTAGGED = frozenset({AKMODS_PACKAGE})
KEEP_STABLE = 20
MIN_AGE = timedelta(days=14)

# Tag families. A version is deleted only when every one of its tags is in a
# deletable family; one unknown tag keeps it. Unknown means "keep", never "go".
LATEST_RE = re.compile(r"^latest$")
STABLE_RE = re.compile(r"^stable-\d+-[0-9a-f]+$")
DELETABLE_RES = (
    re.compile(r"^candidate-[0-9a-f]+-\d+$"),  # per-commit candidates
    re.compile(r"^.+-unsigned-\d+$"),  # publish-native-image's pre-sign tag
    re.compile(r"^br-.+$"),  # unsigned throwaway branch images
    # The akmods cache's per-kernel tags, `main-<fedora>-<kernel>` on the index
    # and `.<arch>` on its child. Nothing reads them, and the build that still
    # matters also carries `main-<fedora>`/`main-<fedora>-<arch>`, which stay
    # unrecognised and so keep it. The arch tag starts with a letter, so this
    # never matches `main-<fedora>-<arch>`.
    re.compile(r"^main-\d+-\d+\.\d+[0-9A-Za-z._+~-]*$"),
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


def plan_versions(
    versions: list[Version],
    now: datetime,
    prune_untagged: bool = False,
    referenced: frozenset[str] = frozenset(),
) -> Plan:
    """
    Decide each version's fate. Pure: no network, so the rule is testable.

    `prune_untagged` lets an old untagged version go. No version whose digest
    is in `referenced` (a child some index points at) is deleted, tagged or
    not: once the index is gone, a later run can take the child.

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
            if not prune_untagged:
                plan.keep[v.id] = "untagged (may belong to a multi-arch image)"
            elif v.digest in referenced:
                plan.keep[v.id] = "untagged, but a tagged index points at it"
            elif now - v.created < MIN_AGE:
                plan.keep[v.id] = f"untagged, younger than {MIN_AGE.days} days"
            else:
                plan.delete[v.id] = "untagged: a rebuild moved its tag to a newer version"
        elif any(LATEST_RE.match(t) for t in image_tags):
            plan.keep[v.id] = "tagged latest"
        elif v.id in newest_stable:
            plan.keep[v.id] = f"one of the newest {KEEP_STABLE} stable-* tags"
        elif now - v.created < MIN_AGE:
            plan.keep[v.id] = f"younger than {MIN_AGE.days} days"
        elif not all(_deletable(t) for t in image_tags):
            plan.keep[v.id] = "has a tag this tool does not recognise"
        elif v.digest in referenced:
            plan.keep[v.id] = "only throwaway tags, but an index points at it"
        else:
            plan.delete[v.id] = "only throwaway tags: " + ", ".join(image_tags)

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


INDEX_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)


def _registry_get(url: str, accept: str = "", bearer: str = "") -> object:
    headers = {"Accept": accept} if accept else {}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.loads(resp.read())


def referenced_children(owner: str, package: str, versions: list[Version]) -> frozenset[str]:
    """
    Digests any version's index points at, tagged or not. An untagged version
    in this set may be the platform manifest of an index the plan keeps (a
    young untagged index, say), so it is never pruned in this run. Once the
    index pointing at it is gone, a later run can prune it.

    Reads the public registry anonymously. Any failure raises: if the
    references can't be read, the run stops rather than guess.
    """

    repo = f"{owner.lower()}/{package}"
    token = _registry_get(f"{REGISTRY}/token?scope=repository:{repo}:pull")
    assert isinstance(token, dict)
    bearer = str(token["token"])
    children: set[str] = set()
    for v in versions:
        if v.tags and all(SIG_RE.match(t) for t in v.tags):
            continue
        manifest = _registry_get(f"{REGISTRY}/v2/{repo}/manifests/{v.digest}", INDEX_ACCEPT, bearer)
        assert isinstance(manifest, dict)
        children.update(str(m["digest"]) for m in manifest.get("manifests", []))
    return frozenset(children)


def main() -> None:
    """List, plan and (only with PRUNE_DELETE=true) delete. Dry run otherwise."""

    token = os.environ.get("GITHUB_TOKEN", "")
    owner = os.environ.get("PACKAGE_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not token or not owner:
        raise CiToolError("GITHUB_TOKEN and PACKAGE_OWNER (or GITHUB_REPOSITORY_OWNER) are required")
    packages = tuple(p for p in os.environ.get("PACKAGES", " ".join(DEFAULT_PACKAGES)).split() if p)
    delete = os.environ.get("PRUNE_DELETE", "false") == "true"
    now = datetime.now(timezone.utc)

    # Phase 1: list and plan every package before touching any. A listing or
    # sanity failure on the second package must not leave the first one half
    # pruned by a run nobody reviewed in full.
    planned: list[tuple[str, list[Version], Plan]] = []
    for package in packages:
        versions = list_versions(owner, package, token)
        if package == DEFAULT_PACKAGES[0] and not any(LATEST_RE.match(t) for v in versions for t in v.tags):
            raise CiToolError(f"{package} has no version tagged latest; refusing to plan against it")
        if package in PRUNE_UNTAGGED:
            plan = plan_versions(versions, now, True, referenced_children(owner, package, versions))
        else:
            plan = plan_versions(versions, now)
        planned.append((package, versions, plan))

    for package, versions, plan in planned:
        print(f"== {package}: {len(versions)} versions, keep {len(plan.keep)}, delete {len(plan.delete)}")
        by_id = {v.id: v for v in versions}
        for vid, why in sorted(plan.delete.items(), key=lambda kv: by_id[kv[0]].created):
            v = by_id[vid]
            print(f"  delete {vid} {v.created:%Y-%m-%d} {v.digest[:19]} {','.join(v.tags) or '-'}  ({why})")

    # Phase 2: delete, only when asked, only the planned IDs. Images first; a
    # signature goes only once every image it signs is gone (deleted in this run
    # or already absent). A failed image delete must not leave that still
    # published image without its signature.
    failures = 0
    if delete:
        for package, versions, plan in planned:
            by_id = {v.id: v for v in versions}
            present = {v.digest for v in versions}
            sig_ids = {vid for vid in plan.delete if by_id[vid].tags and all(SIG_RE.match(t) for t in by_id[vid].tags)}
            gone: set[str] = set()
            for vid in [i for i in plan.delete if i not in sig_ids] + sorted(sig_ids):
                v = by_id[vid]
                if vid in sig_ids:
                    subjects = {"sha256:" + m.group("hex") for t in v.tags if (m := SIG_RE.match(t))}
                    if any(s in present and s not in gone for s in subjects):
                        print(f"  kept signature {vid}: the image it signs was not deleted")
                        continue
                try:
                    _request("DELETE", f"/users/{owner}/packages/container/{package}/versions/{vid}", token)
                    gone.add(v.digest)
                except Exception as exc:  # noqa: BLE001 - report every failure, keep going
                    failures += 1
                    print(f"  FAILED to delete {vid}: {exc}", file=sys.stderr)
    print("mode: " + ("delete" if delete else "dry run (nothing deleted)"))
    if failures:
        raise CiToolError(f"{failures} deletion(s) failed")
