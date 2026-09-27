"""
Script: tests/test_akmods_rebuild_path_docs.py
What: Joins the two pages that describe the shared akmods cache's rebuild path --
docs/architecture-overview.md and docs/zfs-kinoite-testing.md -- to the composite action
that runs it, `.github/actions/prepare-main-akmods/action.yml`, and to the strict mode of
`ci_tools/check_akmods_cache.py`.
Doing: Reads the action's step names in order and maps each one to exactly one item of the
overview's "That action does N things" list, both ways. Then holds the final item of each
page's rebuild list to the verification step it describes: the command it runs, the env it
passes, the step it takes the pinned digest from, what `main()` does with that env, and the
build.yml jobs that sign the cache afterwards and wait for that signing.
Why: #279 changed the post-rebuild verification to check the digest the pin step published
instead of re-reading the mutable `main-<fedora>` tag. Neither page mentioned that
verification at all. The overview's step list stopped at the pin and its rebuild list
stopped at "build", and zfs-kinoite-testing.md's rebuild list did the same. A reader was
never told that a rebuilt cache is content-checked at the pinned digest, or that the
signature is skipped at that point because the cache is signed by a later job.
Goal: A step added to the action that neither page's list accounts for, or a verification
that stops checking the pinned digest, fails here instead of leaving both pages describing
a rebuild path that ends one step early.

No PyYAML, for the reason tests/test_docs_consistency.py gives. The action and workflow
are read as text, with comments stripped where a comment could satisfy a search.
"""

from __future__ import annotations

import os
import re
import unittest
from unittest.mock import patch

from ci_tools.check_akmods_cache import AkmodsCacheStatus
from ci_tools.check_akmods_cache import main as check_cache_main
from tests.test_architecture_overview_doc import doc as overview_text
from tests.test_architecture_overview_doc import numbered_list_after
from tests.test_zfs_kinoite_testing_doc import (
    REPO_ROOT,
    normalized,
    numbered_items,
    section,
    strip_yaml_comments,
)
from tests.test_zfs_kinoite_testing_doc import (
    doc_text as kinoite_page_text,
)

ACTION = REPO_ROOT / ".github" / "actions" / "prepare-main-akmods" / "action.yml"
BUILD_MAIN = REPO_ROOT / ".github" / "workflows" / "build.yml"

OVERVIEW_LIST_RE = re.compile(r"That action does (\w+) things in one place:")
NUMBER_WORDS = {
    "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

VERIFY_STEP = "Verify the rebuilt cache matches the resolved ZFS version"
PIN_STEP = "Resolve shared akmods cache digest"

# Which item of the overview's list accounts for each action step, and a phrase that item
# must carry so the mapping cannot drift onto an unrelated item. Items are 1-based.
STEP_ITEMS: dict[str, tuple[int, str]] = {
    "Resolve build inputs for this run": (1, "resolve and record build inputs"),
    "Write build inputs manifest": (1, "resolve and record build inputs"),
    "Upload build inputs manifest": (2, "upload the build-input manifest"),
    "Check for existing shared akmods cache": (3, "can be reused"),
    "Refuse to refresh the shared akmods cache from a restricted run": (
        4, "only when required"),
    "Clone resolved upstream akmods tooling": (4, "rebuild and republish"),
    "Configure shared ZFS target in self-hosted namespace": (4, "rebuild and republish"),
    "Build and publish shared self-hosted ZFS akmods image": (4, "rebuild and republish"),
    PIN_STEP: (5, "digest-pinned ref"),
    VERIFY_STEP: (6, "pinned digest carries the resolved ZFS version"),
}

# Steps the list does not describe, by name, with the reason. Both only run after the
# rebuild has already failed, to report it; they prepare nothing the image build consumes.
EXEMPT_STEPS = {
    "Classify akmods build failure": "failure reporting, not preparation",
    "Upload akmods failure payload": "failure reporting, not preparation",
}


# What the last item of each rebuild list must say. Every needle is a claim
# RebuildVerificationStepTests holds to the action, `main()` or build.yml.
REBUILD_CLAIMS = {
    "the command": "`check-akmods-cache`",
    "strict mode": "`REQUIRE_MATCH=true`",
    "the pinned digest env": "`AKMODS_IMAGE_PINNED`",
    "no re-read of the tag": "never re-reads the mutable tag",
    "the pin comes first": "freshly published `main-<fedora>` tag to a digest",
    "the signing job": "`sign-akmods-cache` job signs that digest",
    "promotion waits": "`promote-stable` waits for it",
}


def action_steps() -> list[tuple[str, str]]:
    """
    Return `(name, body)` for every step of the action, in order.

    A step starts at a `    - ` line under `  steps:`. Its name is the first `name:` at the
    step's own indent, so the `with: name:` of an upload step is never mistaken for it.
    """

    text = ACTION.read_text(encoding="utf-8")
    body = text.split("\n  steps:\n", 1)[1]
    chunks = re.split(r"(?m)^(?=    - )", body)
    steps = []
    for chunk in chunks:
        if not chunk.startswith("    - "):
            continue
        match = re.search(r"(?m)^(?:    - |      )name: (.+)$", chunk)
        if match is None:
            raise AssertionError(f"action step without a name: {chunk[:80]!r}")
        steps.append((match.group(1).strip(), chunk))
    return steps


def step_body(name: str) -> str:
    for step_name, body in action_steps():
        if step_name == name:
            return body
    raise AssertionError(f"no action step named {name!r}")


def step_if(name: str) -> str:
    match = re.search(r"(?m)^      if: (.+)$", step_body(name))
    if match is None:
        raise AssertionError(f"step {name!r} has no if:")
    return match.group(1).strip()


def overview_action_items() -> tuple[str, list[str]]:
    match = OVERVIEW_LIST_RE.search(overview_text())
    if match is None:
        raise AssertionError("the overview no longer introduces the action's list")
    return match.group(1), [normalized(i) for i in numbered_list_after(match.group(0))]


def rebuild_lists() -> dict[str, list[str]]:
    """The rebuild-path list of each page, keyed by page."""

    return {
        "architecture-overview.md": [normalized(i) for i in numbered_list_after("If no:")],
        "zfs-kinoite-testing.md": numbered_items(
            section(kinoite_page_text(), "### 3. Build Shared Akmods Cache When Required")
        ),
    }


class OverviewActionListTests(unittest.TestCase):
    """The overview's "That action does N things" list, joined to the action's steps."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.word, cls.items = overview_action_items()
        cls.steps = [name for name, _ in action_steps()]

    def test_the_count_word_is_the_length_of_the_list(self) -> None:
        self.assertEqual(NUMBER_WORDS.get(self.word.lower()), len(self.items))

    def test_every_action_step_is_accounted_for_or_exempt(self) -> None:
        unaccounted = [s for s in self.steps if s not in STEP_ITEMS and s not in EXEMPT_STEPS]
        self.assertEqual(unaccounted, [])

    def test_every_mapped_and_exempt_step_still_exists(self) -> None:
        self.assertEqual(set(STEP_ITEMS) - set(self.steps), set())
        self.assertEqual(set(EXEMPT_STEPS) - set(self.steps), set())

    def test_every_item_accounts_for_at_least_one_step(self) -> None:
        self.assertEqual({i for i, _ in STEP_ITEMS.values()}, set(range(1, len(self.items) + 1)))

    def test_each_mapped_item_says_what_its_steps_do(self) -> None:
        for step, (index, phrase) in STEP_ITEMS.items():
            with self.subTest(step=step):
                self.assertLessEqual(index, len(self.items))
                self.assertIn(phrase, self.items[index - 1])

    def test_the_list_is_in_the_order_the_steps_run(self) -> None:
        indexes = [STEP_ITEMS[s][0] for s in self.steps if s in STEP_ITEMS]
        self.assertEqual(indexes, sorted(indexes))


class RebuildVerificationStepTests(unittest.TestCase):
    """The machine side of what both rebuild lists now end with."""

    def test_the_verification_runs_check_akmods_cache_in_strict_mode(self) -> None:
        body = strip_yaml_comments(step_body(VERIFY_STEP))
        self.assertIn("python3 -m ci_tools.cli check-akmods-cache", body)
        self.assertRegex(body, r'(?m)^\s+REQUIRE_MATCH: "true"$')

    def test_the_verification_is_handed_the_pin_steps_digest(self) -> None:
        body = strip_yaml_comments(step_body(VERIFY_STEP))
        self.assertRegex(
            body,
            r"(?m)^\s+AKMODS_IMAGE_PINNED: \$\{\{ steps\.pin_akmods\.outputs\.akmods_image_pinned \}\}$",
        )
        pin = strip_yaml_comments(step_body(PIN_STEP))
        self.assertRegex(pin, r"(?m)^    - id: pin_akmods$")
        self.assertIn("python3 -m ci_tools.cli pin-akmods-cache", pin)
        names = [name for name, _ in action_steps()]
        self.assertLess(names.index(PIN_STEP), names.index(VERIFY_STEP))

    def test_the_verification_runs_exactly_when_the_pin_does(self) -> None:
        # "after a rebuild": both steps are gated on the rebuild path and nothing else.
        self.assertEqual(step_if(VERIFY_STEP), step_if(PIN_STEP))
        self.assertIn("inputs.rebuild_akmods == 'true'", step_if(VERIFY_STEP))

    def test_strict_mode_checks_the_pinned_digest_and_not_the_signature(self) -> None:
        pinned = "ghcr.io/danathar/zfs-kinoite-complex-akmods@sha256:" + "a" * 64
        env = {
            "GITHUB_REPOSITORY_OWNER": "Danathar",
            "FEDORA_VERSION": "43",
            "KERNEL_RELEASE": "6.18.16-200.fc43.x86_64",
            "AKMODS_REPO": "zfs-kinoite-complex-akmods",
            "ZFS_VERSION": "2.4.4",
            "REQUIRE_MATCH": "true",
            "AKMODS_IMAGE_PINNED": pinned,
        }
        matched = AkmodsCacheStatus(
            source_image="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-43",
            image_exists=True,
            source_image_pinned=pinned,
            required_zfs_version="2.4.4",
        )
        with patch.dict(os.environ, env, clear=False), patch(
            "ci_tools.check_akmods_cache.inspect_akmods_cache", return_value=matched
        ) as inspect, patch("builtins.print"):
            check_cache_main()
        kwargs = inspect.call_args.kwargs
        self.assertEqual(kwargs["pinned_image"], pinned)
        self.assertIs(kwargs["verify_signature"], False)

    def test_the_cache_is_signed_by_a_later_job_that_promotion_waits_for(self) -> None:
        workflow = strip_yaml_comments(BUILD_MAIN.read_text(encoding="utf-8"))
        jobs = dict(re.findall(r"(?ms)^  ([a-z][a-z-]*):\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)",
                               workflow))
        self.assertIn("sign-akmods-cache", jobs)
        self.assertRegex(jobs["sign-akmods-cache"], r"(?m)^    needs: build-zfs-akmods$")
        self.assertRegex(jobs["promote-stable"], r"(?m)^      - sign-akmods-cache$")


class RebuildListTests(unittest.TestCase):
    """
    Each page's rebuild list ends with the verification, stated the way the machine runs it.

    Every needle in REBUILD_CLAIMS is a claim the tests above hold to the action, `main()`
    or build.yml, so a page that names them is naming things that are true.
    """


    def test_each_rebuild_list_ends_with_the_build_then_the_verification(self) -> None:
        for page, items in rebuild_lists().items():
            with self.subTest(page=page):
                self.assertGreaterEqual(len(items), 2)
                self.assertRegex(items[-2], r"^builds? the shared cache image")
                for label, needle in REBUILD_CLAIMS.items():
                    with self.subTest(claim=label):
                        self.assertIn(needle, items[-1])

    def test_each_page_says_the_signature_is_not_checked_at_that_point(self) -> None:
        for page, items in rebuild_lists().items():
            with self.subTest(page=page):
                self.assertRegex(
                    items[-1],
                    r"(does not check the signature|signature is not checked)",
                )
                self.assertIn("nothing has signed the new cache yet", items[-1])

    def test_no_other_rebuild_item_claims_the_verification(self) -> None:
        # One copy per page, in the last item, so the checks above read the right one.
        for page, items in rebuild_lists().items():
            with self.subTest(page=page):
                for item in items[:-1]:
                    self.assertNotIn("REQUIRE_MATCH", item)


if __name__ == "__main__":
    unittest.main()
