"""
Script: tests/test_zfs_release.py
What: Tests for the OpenZFS minor-line release resolver.
Doing: Feeds synthetic release lists (including a same-batch, multi-line
scenario recreated from the real OpenZFS release feed) and checks which
version is selected. Also covers the fetcher's pagination and authentication
without making a live API call.
Why: A resolver that picks the wrong line can silently downgrade ZFS on pools
that have already activated newer on-disk feature flags.
Goal: Guarantee the resolver never returns a version from a different minor
line than the one requested, regardless of publish order.
"""

from __future__ import annotations

import http.client
import json
import os
import unittest
from typing import ClassVar
from unittest.mock import patch
from urllib.request import Request

from ci_tools.common import CiToolError
from ci_tools.zfs_release import (
    MAX_RELEASE_PAGES,
    RELEASES_PER_PAGE,
    ZfsSelection,
    fetch_openzfs_meta,
    fetch_openzfs_releases,
    release_supports_kernel,
    resolve_latest_zfs_version,
    resolve_linux_maximum,
    resolve_linux_minimum,
    select_zfs_version,
    zfs_line_key,
    zfs_line_of,
)

# Recreates the real batch observed on 2026-06-12: three minor lines published
# within 30 seconds of each other, newest-line-first in API order.
SAME_BATCH_RELEASES = [
    {"tag_name": "zfs-2.4.3", "draft": False, "prerelease": False},
    {"tag_name": "zfs-2.3.8", "draft": False, "prerelease": False},
    {"tag_name": "zfs-2.2.10", "draft": False, "prerelease": False},
    {"tag_name": "zfs-2.4.2", "draft": False, "prerelease": False},
    {"tag_name": "zfs-2.3.7", "draft": False, "prerelease": False},
]


class ResolveLatestZfsVersionTests(unittest.TestCase):
    def test_resolves_newest_patch_on_the_requested_line(self) -> None:
        version = resolve_latest_zfs_version(
            "2.4", releases_fetcher=lambda: SAME_BATCH_RELEASES
        )
        self.assertEqual(version, "2.4.3")

    def test_same_batch_different_lines_never_cross_contaminate(self) -> None:
        # This is the exact hazard the module docstring describes: if a
        # resolver ever compared "publish order" instead of filtering to one
        # line first, requesting 2.4 here could return 2.3.8 or 2.2.10.
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: SAME_BATCH_RELEASES),
            "2.4.3",
        )
        self.assertEqual(
            resolve_latest_zfs_version("2.3", releases_fetcher=lambda: SAME_BATCH_RELEASES),
            "2.3.8",
        )
        self.assertEqual(
            resolve_latest_zfs_version("2.2", releases_fetcher=lambda: SAME_BATCH_RELEASES),
            "2.2.10",
        )

    def test_a_later_published_older_line_release_is_never_selected(self) -> None:
        # zfs-2.3.9 publishes after zfs-2.4.4 (API returns newest-created-first).
        # Requesting the 2.4 line must still resolve 2.4.4, not 2.3.9.
        releases = [
            {"tag_name": "zfs-2.3.9", "draft": False, "prerelease": False},
            {"tag_name": "zfs-2.4.4", "draft": False, "prerelease": False},
        ]
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases), "2.4.4"
        )

    def test_ignores_prerelease_releases(self) -> None:
        releases = [
            {"tag_name": "zfs-2.4.4", "draft": False, "prerelease": True},
            {"tag_name": "zfs-2.4.3", "draft": False, "prerelease": False},
        ]
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases), "2.4.3"
        )

    def test_ignores_draft_releases(self) -> None:
        releases = [
            {"tag_name": "zfs-2.4.4", "draft": True, "prerelease": False},
            {"tag_name": "zfs-2.4.3", "draft": False, "prerelease": False},
        ]
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases), "2.4.3"
        )

    def test_ignores_tags_that_do_not_match_the_zfs_release_pattern(self) -> None:
        releases = [
            {"tag_name": "some-other-tag", "draft": False, "prerelease": False},
            {"tag_name": "zfs-2.4.3", "draft": False, "prerelease": False},
        ]
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases), "2.4.3"
        )

    def test_raises_when_no_release_matches_the_requested_line(self) -> None:
        releases = [{"tag_name": "zfs-2.3.8", "draft": False, "prerelease": False}]
        with self.assertRaises(CiToolError):
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases)

    def test_raises_on_malformed_minor_version(self) -> None:
        with self.assertRaises(CiToolError):
            resolve_latest_zfs_version("2", releases_fetcher=lambda: SAME_BATCH_RELEASES)

    def test_raises_on_minor_version_with_patch_component(self) -> None:
        with self.assertRaises(CiToolError):
            resolve_latest_zfs_version("2.4.3", releases_fetcher=lambda: SAME_BATCH_RELEASES)


class FetchOpenzfsReleasesTests(unittest.TestCase):
    """Covers the HTTP behaviour of the fetcher itself, without a live call."""

    def _fake_urlopen(self, pages: list[list[dict]], seen: list[Request]):
        """Return a urlopen stand-in serving `pages` in order and recording requests."""

        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        def fake_urlopen(request, timeout=None):
            del timeout
            seen.append(request)
            index = len(seen) - 1
            payload = pages[index] if index < len(pages) else []
            return FakeResponse(json.dumps(payload).encode("utf-8"))

        return fake_urlopen

    def test_follows_pagination_until_a_short_page(self) -> None:
        # A line that is not the newest can have its latest release pushed off
        # the API's first page by newer releases on other lines.
        first_page = [
            {"tag_name": f"zfs-2.9.{n}", "draft": False, "prerelease": False}
            for n in range(RELEASES_PER_PAGE)
        ]
        second_page = [{"tag_name": "zfs-2.4.9", "draft": False, "prerelease": False}]
        seen: list[Request] = []
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            self._fake_urlopen([first_page, second_page], seen),
        ):
            releases = fetch_openzfs_releases()

        self.assertEqual(len(seen), 2)
        self.assertIn("page=1", seen[0].full_url)
        self.assertIn("page=2", seen[1].full_url)
        self.assertEqual(
            resolve_latest_zfs_version("2.4", releases_fetcher=lambda: releases), "2.4.9"
        )

    def test_stops_at_the_page_cap(self) -> None:
        full_page = [
            {"tag_name": f"zfs-2.9.{n}", "draft": False, "prerelease": False}
            for n in range(RELEASES_PER_PAGE)
        ]
        seen: list[Request] = []
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            self._fake_urlopen([full_page] * (MAX_RELEASE_PAGES + 3), seen),
        ):
            fetch_openzfs_releases()

        self.assertEqual(len(seen), MAX_RELEASE_PAGES)

    def test_sends_the_github_token_when_one_is_available(self) -> None:
        # Unauthenticated api.github.com calls are rate limited per IP, and
        # hosted runners share IPs, so the token must actually be sent.
        seen: list[Request] = []
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret-token"}, clear=False), patch(
            "ci_tools.zfs_release.urllib.request.urlopen", self._fake_urlopen([[]], seen)
        ):
            fetch_openzfs_releases()

        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret-token")

    def test_omits_the_authorization_header_when_no_token_is_set(self) -> None:
        seen: list[Request] = []
        env_without_token = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with patch.dict(os.environ, env_without_token, clear=True), patch(
            "ci_tools.zfs_release.urllib.request.urlopen", self._fake_urlopen([[]], seen)
        ):
            fetch_openzfs_releases()

        self.assertIsNone(seen[0].get_header("Authorization"))

    def test_wraps_network_failures_as_ci_tool_error(self) -> None:
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            side_effect=OSError("HTTP Error 403: rate limit exceeded"),
        ), self.assertRaises(CiToolError) as context:
            fetch_openzfs_releases()

        self.assertIn("rate limit exceeded", str(context.exception))

    def test_a_connection_dropped_mid_read_is_a_named_error(self) -> None:
        # `IncompleteRead` is an `http.client.HTTPException`, not an `OSError`.
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial"),
        ), self.assertRaises(CiToolError) as context:
            fetch_openzfs_releases()

        self.assertIn("Failed to fetch OpenZFS releases", str(context.exception))


# The META file at zfs-2.4.4, as published.
META_2_4_4 = """Meta:          1
Name:          zfs
Branch:        1.0
Version:       2.4.4
Release:       1
Release-Tags:  relext
License:       CDDL
Author:        OpenZFS
Linux-Maximum: 7.2
Linux-Minimum: 4.18
"""


class ResolveLinuxMaximumTests(unittest.TestCase):
    def test_reads_linux_maximum_from_the_release_meta(self) -> None:
        asked: list[str] = []

        def fetcher(version: str) -> str:
            asked.append(version)
            return META_2_4_4

        self.assertEqual(resolve_linux_maximum("2.4.4", meta_fetcher=fetcher), "7.2")
        self.assertEqual(asked, ["2.4.4"])

    def test_a_missing_or_malformed_line_raises(self) -> None:
        for meta in (
            META_2_4_4.replace("Linux-Maximum: 7.2\n", ""),
            META_2_4_4.replace("Linux-Maximum: 7.2", "Linux-Maximum: 7"),
            META_2_4_4.replace("Linux-Maximum: 7.2", "Linux-Maximum: 7.2.1"),
            META_2_4_4.replace("Linux-Maximum: 7.2", "Linux-Maximum: latest"),
            "",
        ):
            with self.subTest(meta=meta[-40:]), self.assertRaises(CiToolError):
                resolve_linux_maximum("2.4.4", meta_fetcher=lambda version, meta=meta: meta)

    def test_only_an_exact_release_is_looked_up(self) -> None:
        def fetcher(version: str) -> str:
            raise AssertionError(f"fetched META for {version!r}")

        for version in ("2.4", "2.4.4.1", "zfs-2.4.4", "2.4.4/../../x", ""):
            with self.subTest(version=version), self.assertRaises(CiToolError):
                resolve_linux_maximum(version, meta_fetcher=fetcher)


class ResolveLinuxMinimumTests(unittest.TestCase):
    def test_reads_linux_minimum_from_the_release_meta(self) -> None:
        self.assertEqual(
            resolve_linux_minimum("2.4.4", meta_fetcher=lambda version: META_2_4_4), "4.18"
        )

    def test_a_missing_or_malformed_line_raises(self) -> None:
        for meta in (
            META_2_4_4.replace("Linux-Minimum: 4.18\n", ""),
            META_2_4_4.replace("Linux-Minimum: 4.18", "Linux-Minimum: 4"),
            META_2_4_4.replace("Linux-Minimum: 4.18", "Linux-Minimum: any"),
        ):
            with self.subTest(meta=meta[-30:]), self.assertRaises(CiToolError):
                resolve_linux_minimum("2.4.4", meta_fetcher=lambda version, meta=meta: meta)

    def test_only_an_exact_release_is_looked_up(self) -> None:
        def fetcher(version: str) -> str:
            raise AssertionError(f"fetched META for {version!r}")

        with self.assertRaises(CiToolError):
            resolve_linux_minimum("2.4", meta_fetcher=fetcher)


class ReleaseSupportsKernelTests(unittest.TestCase):
    def supports(self, kernel: str, meta: str = META_2_4_4) -> bool:
        return release_supports_kernel("2.4.4", kernel, meta_fetcher=lambda version: meta)

    def test_inside_the_range_including_both_ends(self) -> None:
        for kernel in ("4.18.0-1.el8", "6.17.4-200.fc43.x86_64", "7.2.15-200.fc44.x86_64"):
            with self.subTest(kernel=kernel):
                self.assertTrue(self.supports(kernel))

    def test_outside_the_range(self) -> None:
        for kernel in ("4.17.9", "7.3.4-200.fc44.x86_64", "8.0.1"):
            with self.subTest(kernel=kernel):
                self.assertFalse(self.supports(kernel))

    def test_an_unreadable_kernel_or_meta_raises(self) -> None:
        with self.assertRaises(CiToolError):
            self.supports("not-a-kernel")
        for meta in ("", META_2_4_4.replace("Linux-Minimum: 4.18\n", "")):
            with self.subTest(meta=meta[-30:]), self.assertRaises(CiToolError):
                self.supports("7.2.1", meta)

    def test_meta_is_read_once(self) -> None:
        asked: list[str] = []

        def fetcher(version: str) -> str:
            asked.append(version)
            return META_2_4_4

        release_supports_kernel("2.4.4", "7.2.1", meta_fetcher=fetcher)
        self.assertEqual(asked, ["2.4.4"])


class ZfsLineHelperTests(unittest.TestCase):
    def test_line_of_an_exact_release(self) -> None:
        self.assertEqual(zfs_line_of("2.4.4"), "2.4")
        self.assertEqual(zfs_line_of("2.10.1"), "2.10")
        for value in ("", "2.4", "zfs-2.4.4", "2.4.4-rc1", "latest"):
            with self.subTest(value=value):
                self.assertEqual(zfs_line_of(value), "")

    def test_lines_compare_numerically(self) -> None:
        self.assertLess(zfs_line_key("2.9"), zfs_line_key("2.10"))
        self.assertIsNone(zfs_line_key("2.4.4"))


# The kernel the newest upstream base ships in these scenarios.
NEW_KERNEL = "7.3.5-200.fc44.x86_64"


def _meta(maximum: str, minimum: str = "4.18") -> str:
    return f"Linux-Maximum: {maximum}\nLinux-Minimum: {minimum}\n"


def _published(*tags: str, draft: tuple[str, ...] = (), prerelease: tuple[str, ...] = ()):
    return [
        {"tag_name": f"zfs-{tag}", "draft": tag in draft, "prerelease": tag in prerelease}
        for tag in tags
    ]


class SelectZfsVersionTests(unittest.TestCase):
    """
    The one decision of which OpenZFS release a run builds.

    Unless a scenario says otherwise, OpenZFS 2.4 stops at kernel 7.2 and the newest base
    ships 7.3.
    """

    METAS: ClassVar[dict[str, str]] = {
        "2.4.4": _meta("7.2"),
        "2.4.5": _meta("7.3"),
        "2.5.0": _meta("7.3", "5.10"),
        "2.5.1": _meta("7.3", "5.10"),
        "2.5.2": _meta("7.3", "5.10"),
        "2.6.1": _meta("7.4", "5.10"),
    }

    def select(
        self,
        releases: list[dict],
        *,
        configured: str = "2.4",
        latest: str = "2.4.4",
        kernel: str = NEW_KERNEL,
        metas: dict[str, str] | None = None,
    ) -> ZfsSelection:
        metas = self.METAS if metas is None else metas
        self.meta_reads: list[str] = []

        def meta_fetcher(version: str) -> str:
            self.meta_reads.append(version)
            if version not in metas:
                raise CiToolError(f"Failed to fetch OpenZFS META for {version}")
            return metas[version]

        return select_zfs_version(
            configured_line=configured,
            latest_zfs_version=latest,
            kernel_release=kernel,
            releases_fetcher=lambda: releases,
            meta_fetcher=meta_fetcher,
        )

    def test_the_floor_line_supports_the_kernel_so_nothing_changes(self) -> None:
        selection = self.select(
            _published("2.4.4", "2.5.1"), kernel="7.2.15-200.fc44.x86_64"
        )
        self.assertEqual(selection, ZfsSelection("2.4.4", "2.4", "2.4", "2.4"))
        self.assertFalse(selection.moved)
        # The newer line is never even looked at.
        self.assertEqual(self.meta_reads, ["2.4.4"])

    def test_a_newer_line_x_y_1_that_supports_the_kernel_is_chosen(self) -> None:
        selection = self.select(_published("2.4.4", "2.5.0", "2.5.1"))
        self.assertEqual(selection.version, "2.5.1")
        self.assertEqual(selection.line, "2.5")
        self.assertEqual(selection.floor_line, "2.4")
        self.assertTrue(selection.moved)

    def test_the_newest_patch_of_the_newer_line_is_the_candidate(self) -> None:
        selection = self.select(_published("2.4.4", "2.5.2", "2.5.0", "2.5.1"))
        self.assertEqual(selection.version, "2.5.2")

    def test_only_an_x_y_0_on_the_newer_line_moves_nothing(self) -> None:
        selection = self.select(_published("2.4.4", "2.5.0"))
        self.assertEqual(selection.version, "2.4.4")
        self.assertFalse(selection.moved)
        self.assertNotIn("2.5.0", self.meta_reads)

    def test_no_newer_line_supports_the_kernel_so_the_floor_stays(self) -> None:
        metas = self.METAS | {"2.5.1": _meta("7.2", "5.10"), "2.6.1": _meta("7.2", "5.10")}
        selection = self.select(_published("2.4.4", "2.5.1", "2.6.1"), metas=metas)
        self.assertEqual(selection.version, "2.4.4")
        self.assertFalse(selection.moved)
        self.assertEqual(self.meta_reads, ["2.4.4", "2.5.1", "2.6.1"])

    def test_newer_lines_are_tried_oldest_first(self) -> None:
        metas = self.METAS | {"2.6.1": _meta("7.3", "5.10")}
        selection = self.select(_published("2.6.1", "2.5.1", "2.4.4"), metas=metas)
        self.assertEqual(selection.version, "2.5.1")

    def test_a_newer_line_whose_minimum_is_above_the_kernel_is_skipped(self) -> None:
        metas = self.METAS | {"2.5.1": _meta("7.4", "7.4"), "2.6.1": _meta("7.4", "5.10")}
        selection = self.select(_published("2.4.4", "2.5.1", "2.6.1"), metas=metas)
        self.assertEqual(selection.version, "2.6.1")

    def test_latest_on_a_newer_line_is_never_left_even_when_the_old_line_catches_up(
        self,
    ) -> None:
        # `:latest` runs 2.5.1, the configuration still says 2.4, and 2.4.5 now supports the
        # kernel. The floor is 2.5, so 2.4 is never chosen again.
        selection = self.select(_published("2.4.4", "2.4.5", "2.5.1"), latest="2.5.1")
        self.assertEqual(selection, ZfsSelection("2.5.1", "2.5", "2.5", "2.5"))
        self.assertNotIn("2.4.5", self.meta_reads)

    def test_a_floor_set_by_latest_keeps_its_own_patches(self) -> None:
        selection = self.select(_published("2.4.5", "2.5.1", "2.5.2"), latest="2.5.1")
        self.assertEqual(selection.version, "2.5.2")
        self.assertFalse(selection.moved)

    def test_prerelease_and_draft_releases_are_ignored(self) -> None:
        releases = _published(
            "2.4.4", "2.5.1", "2.5.2", draft=("2.5.1",), prerelease=("2.5.2",)
        )
        selection = self.select(releases)
        self.assertEqual(selection.version, "2.4.4")
        self.assertFalse(selection.moved)

    def test_a_prerelease_on_the_newer_line_does_not_hide_its_real_release(self) -> None:
        releases = _published("2.4.4", "2.5.1", "2.5.2", prerelease=("2.5.2",))
        self.assertEqual(self.select(releases).version, "2.5.1")

    def test_an_unreadable_meta_fails(self) -> None:
        for metas in (
            {},
            self.METAS | {"2.5.1": "Linux-Maximum: 7.3\n"},
            self.METAS | {"2.4.4": "garbage"},
        ):
            with self.subTest(metas=sorted(metas)), self.assertRaises(CiToolError):
                self.select(_published("2.4.4", "2.5.1"), metas=metas)

    def test_a_missing_or_malformed_latest_label_uses_the_configured_line(self) -> None:
        for latest in ("", "2.5", "latest", "zfs-2.5.1"):
            with self.subTest(latest=latest):
                selection = self.select(
                    _published("2.4.4", "2.5.1"), latest=latest, kernel="7.2.1-200.fc44.x86_64"
                )
                self.assertEqual(selection.version, "2.4.4")
                self.assertEqual(selection.floor_line, "2.4")
                self.assertEqual(selection.latest_line, "")

    def test_latest_on_an_older_line_than_the_configuration_uses_the_configuration(self) -> None:
        selection = self.select(
            _published("2.4.4", "2.5.1"), configured="2.5", latest="2.4.4"
        )
        self.assertEqual(selection, ZfsSelection("2.5.1", "2.5", "2.5", "2.4"))

    def test_an_unknown_kernel_stays_on_the_floor_without_reading_meta(self) -> None:
        selection = self.select(_published("2.4.4", "2.5.1"), kernel="")
        self.assertEqual(selection.version, "2.4.4")
        self.assertEqual(self.meta_reads, [])

    def test_an_unparseable_kernel_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.select(_published("2.4.4", "2.5.1"), kernel="unknown")

    def test_an_invalid_configured_line_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.select(_published("2.4.4"), configured="2.4.4")

    def test_no_release_on_the_floor_line_fails(self) -> None:
        with self.assertRaises(CiToolError):
            self.select(_published("2.5.1"), latest="")

    def test_the_default_fetchers_are_looked_up_when_called(self) -> None:
        # The gate and resolution tests replace the module's fetchers; the selection must use
        # them rather than the ones bound when the module was imported.
        with (
            patch(
                "ci_tools.zfs_release.fetch_openzfs_releases",
                return_value=_published("2.4.4", "2.5.1"),
            ),
            patch(
                "ci_tools.zfs_release.fetch_openzfs_meta",
                side_effect=self.METAS.__getitem__,
            ),
        ):
            selection = select_zfs_version(
                configured_line="2.4", latest_zfs_version="2.4.4", kernel_release=NEW_KERNEL
            )
        self.assertEqual(selection.version, "2.5.1")


class FetchOpenzfsMetaTests(unittest.TestCase):
    """Covers the HTTP behaviour of the META fetcher, without a live call."""

    def _fake_urlopen(self, body: str, seen: list[Request]):
        class FakeResponse:
            def read(self) -> bytes:
                return body.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        def fake_urlopen(request, timeout=None):
            del timeout
            seen.append(request)
            return FakeResponse()

        return fake_urlopen

    def test_reads_meta_at_the_release_tag_as_raw_text_with_the_token(self) -> None:
        seen: list[Request] = []
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret-token"}, clear=False), patch(
            "ci_tools.zfs_release.urllib.request.urlopen", self._fake_urlopen(META_2_4_4, seen)
        ):
            text = fetch_openzfs_meta("2.4.4")

        self.assertEqual(text, META_2_4_4)
        self.assertEqual(
            seen[0].full_url,
            "https://api.github.com/repos/openzfs/zfs/contents/META?ref=zfs-2.4.4",
        )
        self.assertEqual(seen[0].get_header("Accept"), "application/vnd.github.raw+json")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret-token")

    def test_wraps_network_failures_as_ci_tool_error(self) -> None:
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            side_effect=OSError("HTTP Error 404: Not Found"),
        ), self.assertRaises(CiToolError) as context:
            fetch_openzfs_meta("2.4.4")

        self.assertIn("Not Found", str(context.exception))

    def test_a_connection_dropped_mid_read_is_a_named_error(self) -> None:
        with patch(
            "ci_tools.zfs_release.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial"),
        ), self.assertRaises(CiToolError) as context:
            fetch_openzfs_meta("2.4.4")

        self.assertIn("Failed to fetch OpenZFS META", str(context.exception))


if __name__ == "__main__":
    unittest.main()
