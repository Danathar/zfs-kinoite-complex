"""
Script: tests/test_codeowners.py
What: Joins `.github/CODEOWNERS` to the owner-merge paths, the list the hive's `auto_merge.human_merge_paths` gate holds for this repository, to the `area/safety-critical` globs in `.github/labeler.yml`, and to the ruleset.
Doing: Parses each CODEOWNERS line, resolves it against the tracked tree, and checks that it owns exactly the files the owner-merge paths match, and only files the safety-critical label also matches.
Why: Since the owner's decision of 2026-11-23 GitHub no longer requires code-owner review. The hive's human_merge_paths gate is what keeps the owner on the merge button for image signing, the workflows, the rulesets and this file, and CODEOWNERS is how GitHub asks for his review on those pull requests. A path missing here gets no review request, an extra path asks for a review nothing waits on, and a catch-all line asks on every pull request.
Goal: Make a dropped line, a stray `*`, or a list that no longer matches the owner-merge paths fail here, in the pull request that does it.

The hive's setting lives on the hive, not in this repository, so OWNER_MERGE_PATHS below is
the committed copy of it. Change both together.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_labeler_config import SAFETY_LABEL, glob_to_regex, label_globs, tracked_files, yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
LABELER = REPO_ROOT / ".github" / "labeler.yml"
RULESET = REPO_ROOT / ".github" / "rulesets" / "main.json"

OWNER = "@Danathar"

# The hive's auto_merge.human_merge_paths for this repository, as set on 2026-11-23, in the
# hive's glob form. A pull request touching any of them is labelled `needs-owner-merge` and
# waits for the owner.
OWNER_MERGE_PATHS = (
    "cosign.pub",
    "ci_tools/sign_image.py",
    "ci_tools/promote_stable.py",
    "files/scripts/configure_signing_policy.py",
    ".github/actions/install-signing-tools/**",
    ".github/actions/publish-native-image/**",
    ".github/workflows/**",
    ".github/rulesets/**",
    ".github/CODEOWNERS",
)


def entries() -> list[tuple[str, list[str]]]:
    """(pattern, owners) for every rule line, in file order."""
    found = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pattern, *owners = line.split()
            found.append((pattern, owners))
    return found


def owned(pattern: str, tracked: list[str]) -> list[str]:
    """Tracked files an anchored CODEOWNERS pattern covers.

    Only the two anchored forms this file uses: `/dir/` owns everything under
    it, `/file` owns that file. test_every_pattern_is_anchored keeps it so.
    """
    path = pattern.lstrip("/")
    if path.endswith("/"):
        return [f for f in tracked if f.startswith(path)]
    return [f for f in tracked if f == path]


class CodeownersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entries = entries()
        cls.tracked = tracked_files()

    def test_the_file_has_one_rule_per_owner_merge_path(self) -> None:
        self.assertEqual(len(self.entries), len(OWNER_MERGE_PATHS))

    def test_there_is_no_catch_all_line(self) -> None:
        # A catch-all asks for the owner's review on every pull request, though only the
        # owner-merge paths wait for him.
        for pattern, _ in self.entries:
            with self.subTest(pattern=pattern):
                self.assertNotIn("*", pattern)
                self.assertNotIn(pattern, ("/", "/**"))

    def test_every_pattern_is_anchored(self) -> None:
        # An unanchored pattern matches at any depth, which owned() does not model.
        for pattern, _ in self.entries:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.startswith("/"), pattern)

    def test_every_line_is_owned_by_the_owner(self) -> None:
        # A line with no owner un-owns its path, so GitHub stops asking the owner to review it.
        for pattern, owners in self.entries:
            with self.subTest(pattern=pattern):
                self.assertEqual(owners, [OWNER])

    def test_every_pattern_matches_a_tracked_file(self) -> None:
        for pattern, _ in self.entries:
            with self.subTest(pattern=pattern):
                self.assertNotEqual(owned(pattern, self.tracked), [], f"{pattern} owns nothing")

    def test_the_file_owns_itself(self) -> None:
        # The hive holds this file for the owner too, and a change to it changes who is asked.
        self.assertIn("/.github/CODEOWNERS", [pattern for pattern, _ in self.entries])

    def test_the_file_owns_exactly_the_owner_merge_paths(self) -> None:
        # The hive gate and this file are two copies of one list. A path only the hive holds
        # waits for an owner nobody asked; a path only this file holds asks for a review
        # nothing waits on.
        regexes = [glob_to_regex(glob) for glob in OWNER_MERGE_PATHS]
        held = {f for f in self.tracked if any(r.match(f) for r in regexes)}
        owned_files = {f for pattern, _ in self.entries for f in owned(pattern, self.tracked)}
        self.assertEqual(sorted(owned_files), sorted(held))
        for glob, regex in zip(OWNER_MERGE_PATHS, regexes):
            with self.subTest(glob=glob):
                self.assertTrue(any(regex.match(f) for f in self.tracked), f"{glob} matches nothing")

    def test_the_ruleset_no_longer_requires_code_owner_review(self) -> None:
        # The owner's decision of 2026-11-23. The docs say GitHub does not require the review;
        # turning it back on puts every owned path behind an approval the owner cannot give
        # his own pull request, so it needs the docs changed in the same pull request.
        rules = json.loads(RULESET.read_text(encoding="utf-8"))["rules"]
        (pull_request,) = [rule for rule in rules if rule["type"] == "pull_request"]
        self.assertIs(pull_request["parameters"]["require_code_owner_review"], False)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class CodeownersAreInsideTheSafetyLabel(unittest.TestCase):
    """Every owner-merge path is Tier 3. Most Tier 3 paths are not owner-merge paths any more."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tracked = tracked_files()
        cls.owned = {f for pattern, _ in entries() for f in owned(pattern, cls.tracked)}
        globs = label_globs(yaml.safe_load(LABELER.read_text(encoding="utf-8")))[SAFETY_LABEL]
        regexes = [glob_to_regex(g) for g in globs]
        cls.labelled = {f for f in cls.tracked if any(r.match(f) for r in regexes)}

    def test_every_owned_file_is_safety_critical(self) -> None:
        self.assertGreater(len(self.labelled), len(self.owned))
        self.assertEqual(
            sorted(self.owned - self.labelled),
            [],
            "CODEOWNERS owns files that are not Tier 3",
        )


if __name__ == "__main__":
    unittest.main()
