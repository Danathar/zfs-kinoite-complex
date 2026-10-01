"""The registry retention rule (#308): what is kept, what goes, and why."""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from ci_tools import prune_registry
from ci_tools.common import CiToolError
from ci_tools.prune_registry import KEEP_STABLE, Version, plan_versions

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)


def v(vid: int, *tags: str, age_days: float = 30, digest: str | None = None) -> Version:
    return Version(
        id=vid,
        digest=digest or f"sha256:{vid:064x}",
        created=NOW - timedelta(days=age_days),
        tags=tuple(tags),
    )


def sig_for(vid: int, subject: Version, age_days: float = 30) -> Version:
    return v(vid, f"sha256-{subject.digest.split(':', 1)[1]}.sig", age_days=age_days)


class RetentionRule(unittest.TestCase):
    def test_latest_is_never_deleted_however_old(self) -> None:
        plan = plan_versions([v(1, "latest", "candidate-abc123-44", age_days=400)], NOW)
        self.assertIn(1, plan.keep)

    def test_old_throwaway_tags_are_deleted(self) -> None:
        versions = [
            v(1, "candidate-4f4264c-44", "candidate-4f4264c-44-unsigned-33270846007"),
            v(2, "br-my-branch"),
            v(3, "latest-unsigned-123"),
        ]
        plan = plan_versions(versions, NOW)
        self.assertEqual(set(plan.delete), {1, 2, 3})

    def test_anything_younger_than_two_weeks_is_kept(self) -> None:
        plan = plan_versions([v(1, "br-x", age_days=13.9), v(2, "br-y", age_days=14.1)], NOW)
        self.assertIn(1, plan.keep)
        self.assertIn(2, plan.delete)

    def test_only_the_newest_stable_tags_survive(self) -> None:
        stables = [v(i, f"stable-{100 + i}-abc{i:04x}", age_days=200 - i) for i in range(KEEP_STABLE + 3)]
        plan = plan_versions(stables, NOW)
        kept = {s.id for s in stables if s.id in plan.keep}
        # The newest are the ones with the highest i (smallest age).
        self.assertEqual(kept, set(range(3, KEEP_STABLE + 3)))
        self.assertEqual(set(plan.delete), {0, 1, 2})

    def test_an_unknown_tag_keeps_a_version(self) -> None:
        plan = plan_versions([v(1, "candidate-abc-44", "something-new")], NOW)
        self.assertIn(1, plan.keep)

    def test_untagged_versions_are_kept(self) -> None:
        plan = plan_versions([v(1)], NOW)
        self.assertIn(1, plan.keep)


class Signatures(unittest.TestCase):
    def test_a_signature_follows_its_image(self) -> None:
        kept = v(1, "latest")
        gone = v(2, "br-old")
        plan = plan_versions([kept, gone, sig_for(3, kept), sig_for(4, gone)], NOW)
        self.assertIn(3, plan.keep)
        self.assertIn(4, plan.delete)

    def test_an_old_orphan_signature_goes_and_a_young_one_stays(self) -> None:
        missing = v(99, "latest")  # not passed in: its image is gone
        plan = plan_versions([sig_for(1, missing), sig_for(2, missing, age_days=1), v(3, "latest")], NOW)
        self.assertIn(1, plan.delete)
        self.assertIn(2, plan.keep)

    def test_a_signature_is_never_treated_as_an_image_tag(self) -> None:
        # A version carrying only a .sig tag must not reach the throwaway check.
        kept = v(1, "stable-1-abc")
        plan = plan_versions([kept, sig_for(2, kept, age_days=500)], NOW)
        self.assertIn(2, plan.keep)




class UntaggedPruning(unittest.TestCase):
    def test_untagged_versions_are_kept_unless_the_package_allows_pruning(self) -> None:
        self.assertIn(1, plan_versions([v(1)], NOW).keep)
        self.assertIn(1, plan_versions([v(1)], NOW, prune_untagged=True).delete)

    def test_a_young_untagged_version_is_kept(self) -> None:
        self.assertIn(1, plan_versions([v(1, age_days=3)], NOW, prune_untagged=True).keep)

    def test_an_untagged_child_of_a_tagged_index_is_kept(self) -> None:
        child = v(2)
        plan = plan_versions([v(1, "main-44"), child], NOW, True, frozenset({child.digest}))
        self.assertIn(2, plan.keep)
        self.assertIn(1, plan.keep)  # an unknown tag keeps the index itself

    def test_a_pruned_untagged_versions_signature_goes_with_it(self) -> None:
        old = v(1)
        plan = plan_versions([old, sig_for(2, old)], NOW, prune_untagged=True)
        self.assertEqual(set(plan.delete), {1, 2})


def api_item(vid: int, *tags: str, age_days: float = 30) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": vid, "name": f"sha256:{vid:064x}", "created_at": created, "metadata": {"container": {"tags": list(tags)}}}


class FakeApi:
    """Stands in for _request: serves canned version pages and records every call."""

    def __init__(self, pages: dict[str, list[list[dict]]], fail_list: str | None = None) -> None:
        self.pages = pages
        self.fail_list = fail_list
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, token: str):
        self.calls.append((method, path))
        if method == "DELETE":
            return None, ""
        package = path.split("/container/", 1)[1].split("/", 1)[0]
        if package == self.fail_list:
            raise OSError("listing failed")
        page = int(path.rsplit("page=", 1)[1])
        pages = self.pages[package]
        link = '<next>; rel="next"' if page < len(pages) else ""
        return pages[page - 1], link

    def deletes(self) -> list[str]:
        return [path for method, path in self.calls if method == "DELETE"]


IMAGE = "zfs-kinoite-complex"
AKMODS = "zfs-kinoite-complex-akmods"


class FakeRegistry:
    """Stands in for _registry_get: a pull token, then manifests by digest."""

    def __init__(self, children: dict[str, list[str]] | None = None, fail: bool = False) -> None:
        self.children = children or {}
        self.fail = fail

    def __call__(self, url: str, accept: str = "", bearer: str = ""):
        if self.fail:
            raise OSError("registry unreachable")
        if "/token?" in url:
            return {"token": "anon"}
        digest = url.rsplit("/manifests/", 1)[1]
        kids = self.children.get(digest)
        return {"manifests": [{"digest": d} for d in kids]} if kids else {"layers": []}


def run_main(api: FakeApi, delete: bool, registry: FakeRegistry | None = None) -> str:
    env = {"GITHUB_TOKEN": "t", "PACKAGE_OWNER": "o", "PRUNE_DELETE": "true" if delete else "false", "PACKAGES": f"{IMAGE} {AKMODS}"}
    out = io.StringIO()
    with (
        mock.patch.dict(os.environ, env, clear=False),
        mock.patch.object(prune_registry, "_request", api),
        mock.patch.object(prune_registry, "_registry_get", registry or FakeRegistry()),
        redirect_stdout(out),
    ):
        prune_registry.main()
    return out.getvalue()


class Main(unittest.TestCase):
    def pages(self) -> dict[str, list[list[dict]]]:
        # Two pages for the image, so pagination is exercised too.
        return {
            IMAGE: [[api_item(1, "latest"), api_item(2, "br-old")], [api_item(3, "candidate-abc123-44")]],
            AKMODS: [[api_item(10, "main-44")]],
        }

    def test_a_dry_run_lists_both_packages_and_deletes_nothing(self) -> None:
        api = FakeApi(self.pages())
        output = run_main(api, delete=False)
        self.assertEqual(api.deletes(), [])
        self.assertIn("delete 2", output)
        self.assertIn("delete 3", output)
        self.assertIn("mode: dry run (nothing deleted)", output)

    def test_a_delete_run_removes_exactly_the_planned_versions(self) -> None:
        api = FakeApi(self.pages())
        run_main(api, delete=True)
        self.assertEqual(
            sorted(api.deletes()),
            [f"/users/o/packages/container/{IMAGE}/versions/2", f"/users/o/packages/container/{IMAGE}/versions/3"],
        )

    def test_every_package_is_planned_before_anything_is_deleted(self) -> None:
        api = FakeApi(self.pages(), fail_list=AKMODS)
        with self.assertRaises(OSError):
            run_main(api, delete=True)
        self.assertEqual(api.deletes(), [], "a failure listing the second package must leave the first untouched")

    def test_no_latest_in_the_image_package_refuses_before_deleting(self) -> None:
        pages = {IMAGE: [[api_item(2, "br-old")]], AKMODS: [[]]}
        api = FakeApi(pages)
        with self.assertRaises(CiToolError):
            run_main(api, delete=True)
        self.assertEqual(api.deletes(), [])


    def test_old_untagged_akmods_versions_go_but_an_index_child_stays(self) -> None:
        pages = {
            IMAGE: [[api_item(1, "latest")]],
            # 13 is the signature of the kept index 10, so it stays too.
            AKMODS: [[api_item(10, "main-44"), api_item(11), api_item(12), api_item(13, f"sha256-{10:064x}.sig")]],
        }
        child = f"sha256:{11:064x}"
        api = FakeApi(pages)
        run_main(api, delete=True, registry=FakeRegistry({f"sha256:{10:064x}": [child]}))
        self.assertEqual(api.deletes(), [f"/users/o/packages/container/{AKMODS}/versions/12"])

    def test_an_unreadable_registry_stops_the_run_before_any_delete(self) -> None:
        api = FakeApi(self.pages())
        with self.assertRaises(OSError):
            run_main(api, delete=True, registry=FakeRegistry(fail=True))
        self.assertEqual(api.deletes(), [])


    def test_the_old_child_of_a_young_untagged_index_is_kept(self) -> None:
        # A rebuild can leave a young untagged index that the plan keeps. Its
        # platform manifest may be older; deleting that would break the index.
        pages = {
            IMAGE: [[api_item(1, "latest")]],
            AKMODS: [[api_item(20, age_days=3), api_item(21, age_days=30)]],
        }
        api = FakeApi(pages)
        run_main(api, delete=True, registry=FakeRegistry({f"sha256:{20:064x}": [f"sha256:{21:064x}"]}))
        self.assertEqual(api.deletes(), [])


if __name__ == "__main__":
    unittest.main()
