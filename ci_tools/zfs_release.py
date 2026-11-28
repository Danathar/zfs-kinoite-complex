"""
Script: ci_tools/zfs_release.py
What: Resolves the newest published OpenZFS release on one configured minor line.
Doing: Queries the OpenZFS GitHub releases API and picks the highest patch number
whose (major, minor) matches the requested line exactly.
Why: OpenZFS ships maintenance releases for several minor lines in one batch (for
example zfs-2.4.3, zfs-2.3.8, and zfs-2.2.10 have been published within seconds
of each other). Trusting API result order, or matching by string prefix across
lines, can select an older line's release that merely happened to publish
later -- silently downgrading ZFS. Filtering to the exact requested line before
comparing patch numbers makes that impossible.
Goal: Give both the scheduled-build gate and the akmods cache-reuse check one
trustworthy answer to "what is the newest release on this line right now".

It also reads the kernel range one exact release declares support for
(`Linux-Minimum` and `Linux-Maximum` in the release's `META` file). Input
resolution uses the maximum to hold the base image on a kernel that release can
build against.

`select_zfs_version` is the one place that decides which OpenZFS release a run
builds. It stays on one line, the newer of the configured line and the line
`:latest` runs, unless that line cannot support the newest upstream kernel.
Only then does it move to the oldest newer line whose newest `x.y.1`-or-later
release does. It never picks a newer line's `x.y.0`, and never a line older
than `:latest`'s. Resolution, the scheduled gate and the kernel hold all call
it, so they cannot disagree.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ci_tools.classify_akmods_failure import kernel_major_minor
from ci_tools.common import CiToolError

OPENZFS_RELEASES_URL = "https://api.github.com/repos/openzfs/zfs/releases"
MINOR_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
RELEASE_TAG_RE = re.compile(r"^zfs-(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
OPENZFS_META_URL = "https://api.github.com/repos/openzfs/zfs/contents/META?ref=zfs-{version}"
# `Linux-Maximum:       7.2` in the META file at a release tag. Only a plain
# `<major>.<minor>` counts; anything else is treated as missing.
LINUX_MAXIMUM_RE = re.compile(r"^Linux-Maximum:[ \t]*(\d+\.\d+)[ \t]*$", re.MULTILINE)
# `Linux-Minimum:       4.18`, read the same way.
LINUX_MINIMUM_RE = re.compile(r"^Linux-Minimum:[ \t]*(\d+\.\d+)[ \t]*$", re.MULTILINE)

# The API caps `per_page` at 100. Five pages covers OpenZFS's entire release
# history several times over, and bounds this to at most five requests.
RELEASES_PER_PAGE = 100
MAX_RELEASE_PAGES = 5


def fetch_openzfs_releases() -> list[dict]:
    """
    Fetch the OpenZFS releases list from the GitHub API.

    Paginates rather than reading only the API's default first page. One page
    is 30 releases, and OpenZFS publishes maintenance releases for several
    minor lines at once, so a line that is not the newest can have its most
    recent release pushed past a single page by newer releases on other lines.
    Reading one page would then either find nothing for that line or -- worse --
    silently settle for an older patch that happened to fall inside the window.

    Sends the GitHub token when one is available. Unauthenticated calls to
    api.github.com are limited to 60 per hour *per IP address*, and hosted
    runners share IPs, so an unauthenticated call here can fail for reasons
    that have nothing to do with this repository. Authenticated calls get a
    far higher per-repository limit. The token is optional: local runs without
    one still work, they just use the lower shared limit.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "zfs-kinoite-complex",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    releases: list[dict] = []
    for page in range(1, MAX_RELEASE_PAGES + 1):
        url = f"{OPENZFS_RELEASES_URL}?per_page={RELEASES_PER_PAGE}&page={page}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page_items = json.loads(response.read())
        # `http.client.HTTPException` (for example `IncompleteRead`, a connection
        # dropped mid-body) is not an `OSError`, and would otherwise escape as a
        # bare traceback instead of saying which call failed.
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise CiToolError(f"Failed to fetch OpenZFS releases from {url}: {exc}") from exc

        if not isinstance(page_items, list):
            raise CiToolError(f"Unexpected OpenZFS releases payload from {url}")

        releases.extend(page_items)
        # A short page is the last page.
        if len(page_items) < RELEASES_PER_PAGE:
            break

    return releases


def resolve_latest_zfs_version(
    minor_version: str,
    *,
    releases_fetcher: Callable[[], list[dict]] = fetch_openzfs_releases,
) -> str:
    """
    Return the newest non-draft, non-prerelease patch version on one ZFS minor line.

    `minor_version` must be exactly `<major>.<minor>` (for example `2.4`). Every
    candidate release is required to match that (major, minor) pair before its
    patch number is even considered, so this can never return a version from a
    different line -- there is no cross-line comparison for a later publish
    timestamp to win.
    """
    minor_match = MINOR_VERSION_RE.match(minor_version)
    if not minor_match:
        raise CiToolError(f"Invalid ZFS minor version: {minor_version!r}")
    minor_key = (int(minor_match.group(1)), int(minor_match.group(2)))

    best_patch: int | None = None
    for release in releases_fetcher():
        if release.get("draft") or release.get("prerelease"):
            continue
        tag_match = RELEASE_TAG_RE.match(str(release.get("tag_name") or ""))
        if not tag_match:
            continue
        release_key = (int(tag_match.group(1)), int(tag_match.group(2)))
        if release_key != minor_key:
            continue
        patch = int(tag_match.group(3))
        if best_patch is None or patch > best_patch:
            best_patch = patch

    if best_patch is None:
        raise CiToolError(f"No published OpenZFS release found for minor line {minor_version}")

    return f"{minor_key[0]}.{minor_key[1]}.{best_patch}"


def fetch_openzfs_meta(zfs_version: str) -> str:
    """
    Return the text of OpenZFS's `META` file at the `zfs-<zfs_version>` tag.

    Read through the GitHub contents API with the raw media type, so it shares
    the authenticated rate limit `fetch_openzfs_releases` uses rather than the
    per-IP limit raw.githubusercontent.com applies to shared runners.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "User-Agent": "zfs-kinoite-complex",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = OPENZFS_META_URL.format(version=zfs_version)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise CiToolError(f"Failed to fetch OpenZFS META from {url}: {exc}") from exc


def _meta_kernel_bound(
    meta_text: str, pattern: re.Pattern[str], name: str, zfs_version: str
) -> str:
    """Return one `Linux-*` bound from a release's META text, or raise naming it."""
    match = pattern.search(meta_text)
    if not match:
        raise CiToolError(f"OpenZFS {zfs_version} META has no {name} line")
    return match.group(1)


def resolve_linux_maximum(
    zfs_version: str,
    *,
    meta_fetcher: Callable[[str], str] = fetch_openzfs_meta,
) -> str:
    """
    Return the newest kernel line (for example `7.2`) one OpenZFS release supports.

    `zfs_version` must be an exact `<major>.<minor>.<patch>` release. A missing
    or malformed `Linux-Maximum` line raises: the caller decides which base
    image to build on from this value, so an unknown limit must stop the run
    rather than be guessed.
    """
    if not VERSION_RE.match(zfs_version):
        raise CiToolError(f"Invalid ZFS version: {zfs_version!r}")
    return _meta_kernel_bound(
        meta_fetcher(zfs_version), LINUX_MAXIMUM_RE, "Linux-Maximum", zfs_version
    )


def resolve_linux_minimum(
    zfs_version: str,
    *,
    meta_fetcher: Callable[[str], str] = fetch_openzfs_meta,
) -> str:
    """
    Return the oldest kernel line (for example `4.18`) one OpenZFS release supports.

    The same rules as `resolve_linux_maximum`: an exact release only, and a
    missing or malformed `Linux-Minimum` line raises.
    """
    if not VERSION_RE.match(zfs_version):
        raise CiToolError(f"Invalid ZFS version: {zfs_version!r}")
    return _meta_kernel_bound(
        meta_fetcher(zfs_version), LINUX_MINIMUM_RE, "Linux-Minimum", zfs_version
    )


def release_supports_kernel(
    zfs_version: str,
    kernel_release: str,
    *,
    meta_fetcher: Callable[[str], str] | None = None,
) -> bool:
    """
    True when `Linux-Minimum <= kernel <= Linux-Maximum` in one release's META.

    Compared on `<major>.<minor>`. Reads META once. A META that cannot be read
    or has no usable bound, or a kernel that cannot be parsed, raises: the
    answer decides which OpenZFS line ships, so it is never "supported" and
    never "unsupported" by default.
    """
    if not VERSION_RE.match(zfs_version):
        raise CiToolError(f"Invalid ZFS version: {zfs_version!r}")
    kernel = kernel_major_minor(kernel_release)
    if kernel is None:
        raise CiToolError(f"Cannot read a kernel version from {kernel_release!r}")
    meta_text = (meta_fetcher or fetch_openzfs_meta)(zfs_version)
    # Both patterns match only `<major>.<minor>`, so these always parse.
    minimum = kernel_major_minor(
        _meta_kernel_bound(meta_text, LINUX_MINIMUM_RE, "Linux-Minimum", zfs_version)
    )
    maximum = kernel_major_minor(
        _meta_kernel_bound(meta_text, LINUX_MAXIMUM_RE, "Linux-Maximum", zfs_version)
    )
    return minimum <= kernel <= maximum


def zfs_line_of(zfs_version: str) -> str:
    """Return the `<major>.<minor>` line of an exact `X.Y.Z` version, or "" if it is not one."""
    if not VERSION_RE.match(zfs_version):
        return ""
    major, minor, _ = zfs_version.split(".")
    return f"{int(major)}.{int(minor)}"


def zfs_line_key(line: str) -> tuple[int, int] | None:
    """Return a comparable `(major, minor)` for a line like `2.4`, or None."""
    match = MINOR_VERSION_RE.match(line)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class ZfsSelection:
    """
    The OpenZFS release one run builds, and the lines it was chosen between.

    - `version`: the exact release to build (`2.5.1`).
    - `line`: its line (`2.5`). This is what the outputs and the manifest call
      `zfs_minor_version`.
    - `floor_line`: the newer of the configured line and `latest_line`. The
      selection is never older than this.
    - `latest_line`: the line `:latest` runs, from its `zfs-version` label, or
      "" when `:latest`, its label, or a readable value is missing.
    """

    version: str
    line: str
    floor_line: str
    latest_line: str

    @property
    def moved(self) -> bool:
        """True when the selection left the floor line for a newer one the kernel needs."""
        return self.line != self.floor_line


def select_zfs_version(
    *,
    configured_line: str,
    latest_zfs_version: str,
    kernel_release: str,
    releases_fetcher: Callable[[], list[dict]] | None = None,
    meta_fetcher: Callable[[str], str] | None = None,
) -> ZfsSelection:
    """
    Choose the OpenZFS release to build for `kernel_release`.

    - The floor is the newer of `configured_line` and the line of
      `latest_zfs_version` (`:latest`'s `zfs-version` label). A missing or
      malformed label leaves the floor on the configured line.
    - If the newest release on the floor line supports the kernel (by its
      META range), that release is chosen, exactly as before this function.
    - Otherwise newer lines are tried oldest first. Each is represented by its
      newest non-draft, non-prerelease release with a patch number of at least
      1; an `x.y.0` is never chosen. The first whose META range includes the
      kernel wins.
    - If none does, the floor line's newest release is returned, and the kernel
      hold or its failure applies as before.

    An empty `kernel_release` means the caller could not read the upstream
    kernel. Nothing can then be compared, so the floor line's newest release
    is returned without reading any META; input resolution fails on the
    missing label itself.

    A lock-file replay does not call this: it keeps its locked version.
    """
    configured_key = zfs_line_key(configured_line)
    if configured_key is None:
        raise CiToolError(f"Invalid ZFS minor version: {configured_line!r}")
    latest_line = zfs_line_of(latest_zfs_version)
    latest_key = zfs_line_key(latest_line) if latest_line else None
    floor_key = max(configured_key, latest_key) if latest_key else configured_key
    floor_line = f"{floor_key[0]}.{floor_key[1]}"

    releases = (releases_fetcher or fetch_openzfs_releases)()
    floor_version = resolve_latest_zfs_version(floor_line, releases_fetcher=lambda: releases)
    floor = ZfsSelection(
        version=floor_version, line=floor_line, floor_line=floor_line, latest_line=latest_line
    )
    if not kernel_release:
        return floor
    meta = meta_fetcher or fetch_openzfs_meta
    if release_supports_kernel(floor_version, kernel_release, meta_fetcher=meta):
        return floor

    newest_patch_per_line: dict[tuple[int, int], int] = {}
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag_match = RELEASE_TAG_RE.match(str(release.get("tag_name") or ""))
        if not tag_match:
            continue
        key = (int(tag_match.group(1)), int(tag_match.group(2)))
        patch = int(tag_match.group(3))
        if key <= floor_key or patch < 1:
            continue
        newest_patch_per_line[key] = max(patch, newest_patch_per_line.get(key, patch))

    for key in sorted(newest_patch_per_line):
        candidate = f"{key[0]}.{key[1]}.{newest_patch_per_line[key]}"
        if release_supports_kernel(candidate, kernel_release, meta_fetcher=meta):
            return ZfsSelection(
                version=candidate,
                line=f"{key[0]}.{key[1]}",
                floor_line=floor_line,
                latest_line=latest_line,
            )
    return floor
