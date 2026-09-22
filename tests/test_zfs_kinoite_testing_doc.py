"""
Script: tests/test_zfs_kinoite_testing_doc.py
What: Joins docs/zfs-kinoite-testing.md -- the deep design document for this repository's
build -- to the tree it describes: the image refs and tag formats, the build arguments the
composite action passes, the ordered cache-reuse checks, the read-only rules for branch and
pull-request validation, and the signing and promotion paths.
Doing: Recomputes each claim from the thing that owns it -- `ci_tools/tagging_context.py`,
`ci_tools/pin_akmods_cache.py` and `ci_tools/promote_stable.py` for the tag shapes,
`ci/defaults.json` for the repository names and the akmods pin,
`ci_tools/check_akmods_cache.py` for the four cache checks and their order,
`.github/actions/build-native-image/action.yml` and `Containerfile` for the build
arguments, and the three build workflows for what each one is allowed to do.
Why: This page is where a reader is sent to learn what the build actually does. Every
statement in it is a hand-copy of something that lives in the machine, and the page was read
by no test as a subject, so a hand-copy could go stale with nothing to catch it.
Goal: Make a renamed build argument, a new one nobody documented, a moved tag format, a
reordered cache check or a branch workflow that stops being read-only fail here, instead of
leaving this page describing a build this repository no longer has.

Before this file, `docs/zfs-kinoite-testing.md` was opened by no test as a subject. The two
occurrences of the path under `tests/` read it as a *source*:
tests/test_image_payload_manifest.py checks that the page links
`files/usr/lib/modules-load.d/zfs.conf` and mentions `systemd-modules-load`, and
tests/test_production_boundary_docs.py quotes one sentence of it back as evidence for an
open finding recorded in `docs/maintenance-watchlist.md`. One link, three phrases and one
quoted sentence were pinned. The rest of the 187 lines were not, and one claim had already
gone stale when this file was written:

  * `### 4. Build Candidate Or Branch Image` listed the build arguments the workflow passes
    into `Containerfile` as `BASE_IMAGE`, `AKMODS_IMAGE`, `IMAGE_REPO` and
    `SIGNING_KEY_FILENAME`. `.github/actions/build-native-image/action.yml` passes five:
    `BREW_IMAGE` is missing from the page. Brew is the third digest-pinned image in this
    build -- it has a required composite-action input, a `Containerfile` `ARG`, an
    `org.zfs-kinoite-complex.brew-image` provenance label, a payload manifest and its own
    three tests -- and the same omission had already been found on the page beside this one
    (`docs/glossary.md` defined neither `BREW_IMAGE_REF`, `BREW_IMAGE_PINNED` nor
    `BREW_IMAGE_DIGEST`). A reader following this section to learn what reaches the image
    build would conclude brew does not.

One sentence on this page is deliberately, knowingly false and is NOT asserted here as
truth: "inside the `production-signing` environment that only `main` refs can reach". That
restriction is not configured, `docs/maintenance-watchlist.md` records it as an open
finding, and tests/test_production_boundary_docs.py already pins the sentence so the finding
cannot quote a document that no longer says it. This file checks the *machine-side* half --
that the two signing jobs really do declare that environment -- and leaves the truth of the
reachability claim to the test that owns it.

No PyYAML, for the reason tests/test_docs_consistency.py gives: the CI job installs only
pytest, pytest-cov and ruff, so a third-party parser would depend on the runner image and
skip silently the day that changed. Workflow assertions read comment-stripped text, because
a search a comment satisfies is a search that cannot fail.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from ci_tools.pin_akmods_cache import akmods_cache_image_tag
from ci_tools.tagging_context import (
    build_branch_image_tag,
    build_branch_metadata,
    build_candidate_tag,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "zfs-kinoite-testing.md"
DOCS_DIR = REPO_ROOT / "docs"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions"

DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD_ACTION = ACTION_DIR / "build-native-image" / "action.yml"
CHECK_CACHE = REPO_ROOT / "ci_tools" / "check_akmods_cache.py"
PROMOTE = REPO_ROOT / "ci_tools" / "promote_stable.py"
SIGN = REPO_ROOT / "ci_tools" / "sign_image.py"
RESOLVE = REPO_ROOT / "ci_tools" / "resolve_build_inputs.py"
IN_IMAGE_INSTALL = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"

BUILD_MAIN = WORKFLOW_DIR / "build.yml"
BUILD_BRANCH = WORKFLOW_DIR / "build-branch.yml"
BUILD_PR = WORKFLOW_DIR / "build-pr.yml"

# The registry owner is lower-cased by `normalize_owner` before it reaches any ref, so every
# image ref the page prints carries this spelling rather than the `Danathar` of the repo URL.
IMAGE_ORG = "danathar"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
NUMBERED_RE = re.compile(r"^\d+\. (.*)$")
BACKTICKED_RE = re.compile(r"`([^`]+)`")
PLACEHOLDER_RE = re.compile(r"<[a-z]+>")
BUILD_ARG_RE = re.compile(r'--build-arg "([A-Z_]+)=')
CONTAINERFILE_ARG_RE = re.compile(r"^ARG ([A-Z_]+)", re.MULTILINE)


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def normalized(text: str) -> str:
    """Squash whitespace so a phrase still matches across a Markdown line wrap."""

    return re.sub(r"\s+", " ", text)


def section(text: str, heading: str) -> str:
    """Return one section's body, from its heading to the next heading of any level."""

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != heading:
            continue
        body = []
        for following in lines[index + 1 :]:
            if following.startswith("#"):
                break
            body.append(following)
        return "\n".join(body)
    raise AssertionError(f"heading not found in {DOC.name}: {heading!r}")


def numbered_items(body: str) -> list[str]:
    """
    Return the numbered-list items of a section, in order, each one line of text.

    Markdown wraps, and several items here run to three or four source lines. Taking only
    the line that carries the digit would silently truncate every claim to its first
    fragment, so continuation lines are folded back in before the text is squashed.
    """

    items: list[str] = []
    for line in body.splitlines():
        match = NUMBERED_RE.match(line)
        if match:
            items.append(match.group(1).strip())
        elif items and line.strip() and line.startswith((" ", "\t")):
            items[-1] = f"{items[-1]} {line.strip()}"
    return [normalized(item) for item in items]


def strip_yaml_comments(text: str) -> str:
    """Drop whole-line `#` comments and trailing ` #` comments from YAML."""

    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def defaults() -> dict[str, str]:
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def skeleton(value: str) -> str:
    """
    Replace every `<placeholder>` in a documented ref with `{}`.

    The page writes `stable-<run>-<sha>` while `ci_tools/promote_stable.py` writes
    `stable-{run_number}-{sha_short}`. The field *names* are free to differ -- they are
    prose on one side and locals on the other -- but the literal text around them is the
    format, and that is what has to match.
    """

    return PLACEHOLDER_RE.sub("{}", value)


def fstring_skeletons(source: str, function_name: str) -> set[str]:
    """
    Return the literal skeleton of every f-string built inside `function_name`.

    Reading the formats out of the module's syntax tree, rather than grepping for a
    finished string, means a changed separator or an added field is a changed skeleton
    rather than a grep that still matches something else in the file.
    """

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        found = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.JoinedStr):
                continue
            parts = [
                str(value.value) if isinstance(value, ast.Constant) else "{}"
                for value in inner.values
            ]
            found.add("".join(parts))
        return found
    raise AssertionError(f"no function named {function_name}() in the parsed source")


class LinkTests(unittest.TestCase):
    """Every relative link on the page has to resolve, including the glossary pointer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = doc_text()

    def test_the_document_exists_and_is_not_empty(self) -> None:
        self.assertTrue(DOC.is_file())
        self.assertGreater(len(self.text.splitlines()), 100)

    def test_every_relative_link_resolves(self) -> None:
        for target in LINK_RE.findall(self.text):
            if target.startswith(("http://", "https://", "#")):
                continue
            with self.subTest(target=target):
                resolved = (DOC.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.exists(), f"broken link in {DOC.name}: {target}")

    def test_the_page_opens_by_pointing_at_the_shared_glossary(self) -> None:
        # The glossary is the page a reader is sent to for any unfamiliar name here, and
        # tests/test_glossary_doc.py is what keeps that page honest. A pointer that rots is
        # how a reader stops being sent anywhere.
        self.assertIn("[`docs/glossary.md`](./glossary.md)", self.text)
        self.assertTrue((DOCS_DIR / "glossary.md").is_file())

    def test_the_fork_cascade_pointer_names_the_document_that_owns_it(self) -> None:
        self.assertIn(
            "[`docs/akmods-fork-maintenance.md`](./akmods-fork-maintenance.md)",
            normalized(self.text),
        )
        self.assertTrue((DOCS_DIR / "akmods-fork-maintenance.md").is_file())


class MainArtifactTests(unittest.TestCase):
    """
    The four refs under `### Main Artifacts` are the names a reader greps a registry for.

    Each is recomputed from the helper that builds it, with the page's own `<placeholder>`
    spellings fed in where the helper takes a value, so the comparison is against the real
    format string rather than a second hand-copy of it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = numbered_items(section(doc_text(), "### Main Artifacts"))
        cls.values = defaults()
        cls.repo = f"ghcr.io/{IMAGE_ORG}/{cls.values['IMAGE_NAME']}"
        cls.published = fstring_skeletons(PROMOTE.read_text(encoding="utf-8"), "main")

    def test_the_section_lists_exactly_four_main_artifacts(self) -> None:
        self.assertEqual(len(self.items), 4)

    def test_the_candidate_ref_matches_build_candidate_tag(self) -> None:
        expected = build_candidate_tag(github_sha="<sha>", fedora_version="<fedora>")
        self.assertIn(f"`{self.repo}:{expected}`", self.items[0])

    def test_the_stable_ref_is_the_tag_promote_stable_moves(self) -> None:
        self.assertIn(f"`{self.repo}:latest`", self.items[1])
        # `image_org` and `image_name` are runtime values in the helper, so the repository
        # half of its ref is `{}/{}`; the page spells them out, and this pins the tag half.
        self.assertIn("docker://ghcr.io/{}/{}:latest", self.published)

    def test_the_audit_ref_matches_the_format_promote_stable_publishes(self) -> None:
        documented = BACKTICKED_RE.findall(self.items[2])[0]
        self.assertTrue(documented.startswith(f"{self.repo}:"))
        tag = skeleton(documented).rsplit(":", 1)[1]
        self.assertEqual(tag, "stable-{}-{}")
        self.assertIn(f"docker://ghcr.io/{{}}/{{}}:{tag}", self.published)

    def test_the_shared_cache_ref_matches_akmods_cache_image_tag(self) -> None:
        expected = akmods_cache_image_tag(
            image_org=IMAGE_ORG,
            source_repo=self.values["AKMODS_REPO"],
            fedora_version="<fedora>",
        )
        self.assertIn(f"`{expected}`", self.items[3])

    def test_the_cache_repository_is_the_one_ci_defaults_names(self) -> None:
        self.assertEqual(self.values["AKMODS_REPO"], f"{self.values['IMAGE_NAME']}-akmods")

    def test_the_digest_pinned_cache_form_is_what_the_final_build_consumes(self) -> None:
        body = section(doc_text(), "### Main Artifacts")
        documented = f"ghcr.io/{IMAGE_ORG}/{self.values['AKMODS_REPO']}@sha256:<digest>"
        self.assertIn(f"`{documented}`", body)
        # `pin_akmods_cache_image` builds exactly this by replacing `:tag` with `@digest`.
        tag = akmods_cache_image_tag(
            image_org=IMAGE_ORG, source_repo=self.values["AKMODS_REPO"], fedora_version="44"
        )
        self.assertEqual(f"{tag.rsplit(':', 1)[0]}@sha256:<digest>", documented)
        pin = (REPO_ROOT / "ci_tools" / "pin_akmods_cache.py").read_text(encoding="utf-8")
        self.assertIn('return f"{image_name}@{digest}", digest', pin)


class BranchArtifactTests(unittest.TestCase):
    """`### Branch Artifacts` promises a branch run publishes one tag and refreshes nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = numbered_items(section(doc_text(), "### Branch Artifacts"))
        cls.branch_yaml = strip_yaml_comments(BUILD_BRANCH.read_text(encoding="utf-8"))

    def test_the_branch_ref_matches_the_two_helpers_that_build_it(self) -> None:
        prefix = build_branch_image_tag(branch_tag_prefix="br-<branch>", fedora_version="<fedora>")
        self.assertIn(f"`ghcr.io/{IMAGE_ORG}/{defaults()['IMAGE_NAME']}:{prefix}`", self.items[0])

    def test_the_br_prefix_is_the_one_build_branch_metadata_produces(self) -> None:
        self.assertEqual(build_branch_metadata("my-branch"), "br-my-branch")

    def test_a_bot_authored_run_stops_before_pushing_any_public_tag(self) -> None:
        self.assertIn("bot-authored branch runs stop after local validation", normalized(self.items[1]))
        self.assertIn("do not push any public tag", normalized(self.items[1]))
        # The gate is the `actor_is_bot` output, checked both ways in the workflow.
        self.assertIn("if: steps.registry.outputs.actor_is_bot != 'true'", self.branch_yaml)
        self.assertIn("if: steps.registry.outputs.actor_is_bot == 'true'", self.branch_yaml)

    def test_a_branch_run_never_refreshes_the_shared_cache(self) -> None:
        item = normalized(self.items[2])
        self.assertIn("never publish branch-specific cache tags", item)
        self.assertIn("never refresh the shared one", item)
        self.assertIn('rebuild_akmods: "false"', self.branch_yaml)
        self.assertNotIn('rebuild_akmods: "true"', self.branch_yaml)


class BuildInputResolutionTests(unittest.TestCase):
    """`### 1. Detect Base Kernel Stream` -- the two modes and the primary-kernel policy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = section(doc_text(), "### 1. Detect Base Kernel Stream")
        cls.main_yaml = strip_yaml_comments(BUILD_MAIN.read_text(encoding="utf-8"))

    def test_replay_mode_names_the_lock_file_the_dispatch_input_defaults_to(self) -> None:
        documented = LINK_RE.findall(self.body)
        self.assertIn("../ci/inputs.lock.json", documented)
        # The workflow's own default for `lock_file` is the same path, relative to the root.
        self.assertIn("default: ci/inputs.lock.json", self.main_yaml)

    def test_the_lock_file_the_page_links_is_committed(self) -> None:
        self.assertTrue((REPO_ROOT / "ci" / "inputs.lock.json").is_file())

    def test_the_workflow_reads_lib_modules_rather_than_one_metadata_label(self) -> None:
        self.assertIn("inspects `/lib/modules` inside the pinned base image", normalized(self.body))
        resolve = RESOLVE.read_text(encoding="utf-8")
        self.assertIn("find /lib/modules", resolve)

    def test_the_policy_choice_still_lists_four_decisions(self) -> None:
        # Two numbered lists live in this section: the two resolution modes, then the four
        # policy decisions. Together that is six items, and the second list is the last four.
        items = numbered_items(self.body)
        self.assertEqual(len(items), 6)
        self.assertIn("newest detected kernel", normalized(items[3]))
        self.assertIn("image rollback", normalized(items[5]))

    def test_the_shared_ordering_helper_is_the_one_the_page_links(self) -> None:
        self.assertIn("../shared/kernel_release.py", LINK_RE.findall(self.body))
        self.assertTrue((REPO_ROOT / "shared" / "kernel_release.py").is_file())

    def test_both_sides_the_page_names_reach_that_one_helper(self) -> None:
        # "both CI input resolution and the in-image ZFS install helper use it". CI input
        # resolution reaches it through `ci_tools.common.sort_kernel_releases`; the in-image
        # helper imports it directly. Either import disappearing makes the sentence false.
        self.assertIn("sort_kernel_releases", RESOLVE.read_text(encoding="utf-8"))
        common = (REPO_ROOT / "ci_tools" / "common.py").read_text(encoding="utf-8")
        self.assertIn("from shared.kernel_release import kernel_release_sort_key", common)
        self.assertIn(
            "from shared.kernel_release import kernel_release_sort_key",
            IN_IMAGE_INSTALL.read_text(encoding="utf-8"),
        )


class AkmodsSourceTests(unittest.TestCase):
    """The page's account of *which* akmods commit a run builds, and how it is pinned."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = normalized(section(doc_text(), "### 2. Validate Existing Shared Akmods Cache"))
        cls.values = defaults()

    def test_the_freeze_knob_is_named_correctly_and_is_empty_by_default(self) -> None:
        self.assertIn("`AKMODS_UPSTREAM_REF` in `ci/defaults.json` exists to freeze it", self.body)
        self.assertIn("is empty by default", self.body)
        self.assertIn("AKMODS_UPSTREAM_REF", self.values)
        self.assertEqual(self.values["AKMODS_UPSTREAM_REF"], "")

    def test_the_fork_the_page_names_is_the_configured_upstream(self) -> None:
        self.assertIn("clones the resolved `Danathar/akmods` commit", self.body)
        self.assertIn("Danathar/akmods", self.values["AKMODS_UPSTREAM_REPO"])

    def test_the_moving_tip_the_page_describes_is_the_configured_track(self) -> None:
        self.assertIn("resolving the moving `main` tip at the start of each run", self.body)
        self.assertEqual(self.values["AKMODS_UPSTREAM_TRACK"], "main")

    def test_the_clone_destination_is_the_one_the_page_prints(self) -> None:
        self.assertIn("clones that exact commit into `/tmp/akmods`", self.body)
        clone = (REPO_ROOT / "ci_tools" / "akmods_clone_pinned.py").read_text(encoding="utf-8")
        self.assertIn("/tmp/akmods", clone)

    def test_every_workflow_path_really_does_clone_that_commit(self) -> None:
        # "every workflow path also clones the resolved commit once" -- the main and branch
        # workflows through the prepare-main-akmods action, and pull-request validation
        # through `ci_tools/prepare_validation_build.py`, which calls the same helper.
        self.assertIn("Separate from cache reuse, every workflow path also clones", self.body)
        action = strip_yaml_comments(
            (ACTION_DIR / "prepare-main-akmods" / "action.yml").read_text(encoding="utf-8")
        )
        self.assertIn("ci_tools.cli akmods-clone-pinned", action)
        for workflow in (BUILD_MAIN, BUILD_BRANCH):
            with self.subTest(workflow=workflow.name):
                self.assertIn(
                    "./.github/actions/prepare-main-akmods",
                    strip_yaml_comments(workflow.read_text(encoding="utf-8")),
                )
        validation = (REPO_ROOT / "ci_tools" / "prepare_validation_build.py").read_text(encoding="utf-8")
        self.assertIn("from ci_tools.akmods_clone_pinned import clone_pinned", validation)
        self.assertIn("clone_pinned(", validation)
        self.assertIn(
            "ci_tools.cli prepare-validation-build",
            strip_yaml_comments(BUILD_PR.read_text(encoding="utf-8")),
        )


class CacheInspectionTests(unittest.TestCase):
    """
    The four numbered checks under `### 2.` are an ordered claim, not a set.

    `ci_tools/check_akmods_cache.py` deliberately confirms the RPM content *before* spending
    a cosign call on it, and says so in a comment. A page that listed the signature first
    would be describing a different trust decision, so the order is asserted here too.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = numbered_items(section(doc_text(), "### 2. Validate Existing Shared Akmods Cache"))
        cls.source = CHECK_CACHE.read_text(encoding="utf-8")

    def test_the_section_lists_four_inspection_steps_then_the_source_account(self) -> None:
        # Four inspection steps, two reasons the check exists, four facts about the commit.
        self.assertEqual(len(self.items), 10)

    def test_step_one_copies_the_image_into_a_local_oci_layout(self) -> None:
        self.assertIn("local Open Container Initiative (OCI) layout", normalized(self.items[0]))
        self.assertIn("skopeo_copy", self.source)
        self.assertIn('f"dir:{akmods_dir}"', self.source)

    def test_step_two_unpacks_the_layers_from_that_local_copy(self) -> None:
        self.assertIn("unpack the filesystem layers", normalized(self.items[1]))
        self.assertIn("load_layer_files_from_oci_layout", self.source)
        self.assertIn("unpack_layer_tarballs", self.source)

    def test_step_three_requires_the_kernel_and_the_exact_patch_version(self) -> None:
        item = normalized(self.items[2])
        self.assertIn("inspect the extracted RPM filenames directly", item)
        self.assertIn("`kmod-zfs` RPM", item)
        self.assertIn("exact OpenZFS patch version", item)
        self.assertIn('f"kmod-zfs-{kernel_release}-{zfs_version}-*.rpm"', self.source)

    def test_step_three_explains_why_the_minor_line_alone_is_not_enough(self) -> None:
        self.assertIn("matching the minor line alone would let an older patch satisfy a newer one",
                      normalized(self.items[2]))

    def test_step_four_verifies_against_the_committed_public_key(self) -> None:
        item = normalized(self.items[3])
        self.assertIn("cosign signature", item)
        self.assertIn("committed `cosign.pub`", item)
        self.assertIn('REPO_ROOT / "cosign.pub"', self.source)
        self.assertTrue((REPO_ROOT / "cosign.pub").is_file())

    def test_the_signature_check_really_does_come_last(self) -> None:
        self.assertIn("only once that content matches", normalized(self.items[3]))
        content_at = self.source.index("has_match = _has_kernel_matching_rpm(")
        signature_at = self.source.index("if verify_signature:")
        self.assertLess(content_at, signature_at)

    def test_a_failing_check_is_a_cache_miss_rather_than_a_trusted_cache(self) -> None:
        body = normalized(section(doc_text(), "### 2. Validate Existing Shared Akmods Cache"))
        self.assertIn("treated as a cache miss rather than trusted", body)
        self.assertIn("return self.content_matches and self.signature_verified", self.source)


class CacheRebuildTests(unittest.TestCase):
    """`### 3.` -- the rebuild path exists on `main` only, and the page says how to reach it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = section(doc_text(), "### 3. Build Shared Akmods Cache When Required")
        cls.items = numbered_items(cls.body)

    def test_the_rebuild_path_lists_four_steps(self) -> None:
        self.assertEqual(len(self.items), 4)

    def test_the_rebuild_target_repository_is_the_configured_one(self) -> None:
        self.assertIn(f"`{defaults()['AKMODS_REPO']}`", self.items[1])

    def test_the_dispatch_input_the_page_tells_a_reader_to_use_is_on_main_only(self) -> None:
        # `build-branch.yml` carries a `workflow_dispatch` trigger of its own, so the claim
        # is not "no other workflow can be dispatched" -- it is that no other workflow
        # offers this input, and the branch one hard-codes the refresh off.
        self.assertIn("`rebuild_akmods=true`", normalized(self.body))
        main_inputs = strip_yaml_comments(BUILD_MAIN.read_text(encoding="utf-8")).split("jobs:", 1)[0]
        self.assertIn("rebuild_akmods:", main_inputs)
        for workflow in (BUILD_BRANCH, BUILD_PR):
            with self.subTest(workflow=workflow.name):
                triggers = strip_yaml_comments(workflow.read_text(encoding="utf-8")).split("jobs:", 1)[0]
                self.assertNotIn("rebuild_akmods:", triggers)

    def test_pull_request_validation_is_read_only_and_never_publishes(self) -> None:
        self.assertIn("pull request validation is likewise read-only", normalized(self.body))
        pr_yaml = strip_yaml_comments(BUILD_PR.read_text(encoding="utf-8"))
        self.assertNotIn("podman push", pr_yaml)
        self.assertNotIn("skopeo copy", pr_yaml)
        self.assertNotIn("SIGNING_SECRET", pr_yaml)


class BuildArgumentTests(unittest.TestCase):
    """
    `### 4.` names the build arguments the workflow passes into `Containerfile`.

    This is the list that had already gone stale: the page named four of the five the
    composite action passes. Set equality both ways is the only form that catches either
    failure -- a dropped argument and an undocumented new one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = section(doc_text(), "### 4. Build Candidate Or Branch Image")
        cls.documented = [BACKTICKED_RE.findall(item)[0] for item in numbered_items(cls.body)]
        cls.passed = BUILD_ARG_RE.findall(BUILD_ACTION.read_text(encoding="utf-8"))

    def test_the_composite_action_passes_a_non_empty_set(self) -> None:
        self.assertTrue(self.passed)
        self.assertEqual(len(self.passed), len(set(self.passed)))

    def test_the_page_names_every_build_argument_the_action_passes(self) -> None:
        self.assertEqual(set(self.documented), set(self.passed))

    def test_the_page_lists_them_in_the_order_the_action_passes_them(self) -> None:
        self.assertEqual(self.documented, self.passed)

    def test_every_documented_build_argument_is_declared_in_the_containerfile(self) -> None:
        declared = set(CONTAINERFILE_ARG_RE.findall(CONTAINERFILE.read_text(encoding="utf-8")))
        for name in self.documented:
            with self.subTest(build_arg=name):
                self.assertIn(name, declared)

    def test_the_template_fallback_the_page_keeps_is_still_a_containerfile_arg(self) -> None:
        normalized_body = normalized(self.body)
        self.assertIn("`AKMODS_IMAGE_TEMPLATE` is still available as a `Containerfile` fallback",
                      normalized_body)
        declared = set(CONTAINERFILE_ARG_RE.findall(CONTAINERFILE.read_text(encoding="utf-8")))
        self.assertIn("AKMODS_IMAGE_TEMPLATE", declared)
        # It is a fallback precisely because it is not one of the five the action passes.
        self.assertNotIn("AKMODS_IMAGE_TEMPLATE", self.passed)

    def test_ci_passes_the_digest_pinned_cache_ref_the_page_promises(self) -> None:
        self.assertIn("CI passes the digest-pinned cache ref resolved by the earlier akmods job",
                      normalized(self.body))
        for workflow in (BUILD_MAIN, BUILD_BRANCH):
            with self.subTest(workflow=workflow.name):
                yaml_text = strip_yaml_comments(workflow.read_text(encoding="utf-8"))
                self.assertIn("akmods_image_pinned", yaml_text)

    def test_the_modules_load_drop_in_the_page_links_is_installed_by_the_build(self) -> None:
        self.assertIn("../files/usr/lib/modules-load.d/zfs.conf", LINK_RE.findall(self.body))
        self.assertTrue((REPO_ROOT / "files" / "usr" / "lib" / "modules-load.d" / "zfs.conf").is_file())


class SigningTests(unittest.TestCase):
    """
    `### 5.` -- what signs, what cannot, and the environment the signing jobs declare.

    The page's claim that only `main` refs can *reach* that environment is knowingly false
    and is pinned by tests/test_production_boundary_docs.py as the quote an open finding in
    `docs/maintenance-watchlist.md` rests on. Asserting it as truth here would turn a tracked
    finding into a test that says the gap is closed, so this only checks the half the tree
    can settle: the two jobs really do declare the environment the page names.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = normalized(section(doc_text(), "### 5. Sign Published Tags"))
        cls.main_yaml = strip_yaml_comments(BUILD_MAIN.read_text(encoding="utf-8"))
        cls.branch_yaml = strip_yaml_comments(BUILD_BRANCH.read_text(encoding="utf-8"))

    def test_the_environment_named_here_is_the_one_the_signing_jobs_declare(self) -> None:
        self.assertIn("`production-signing` environment", self.body)
        self.assertEqual(self.main_yaml.count("environment: production-signing"), 2)

    def test_the_open_finding_that_contradicts_this_sentence_is_still_recorded(self) -> None:
        # The sentence stays on the page on purpose: `docs/maintenance-watchlist.md` quotes
        # it as evidence. If the restriction is ever configured, this test is the reminder
        # that the finding -- and this page's claim -- both need revisiting together.
        watchlist = (DOCS_DIR / "maintenance-watchlist.md").read_text(encoding="utf-8")
        self.assertIn("### Open: the `production-signing` environment is not branch-restricted",
                      watchlist)
        self.assertIn("only `main` refs can reach", self.body)

    def test_a_candidate_is_signed_by_digest_after_the_push(self) -> None:
        self.assertIn("resolving the pushed tag to a digest and then signing that digest", self.body)
        self.assertIn("skopeo_inspect_digest", SIGN.read_text(encoding="utf-8"))

    def test_stable_is_promoted_by_copying_rather_than_signed_twice(self) -> None:
        self.assertIn("by copying the already-signed candidate digest, not by signing a second time",
                      self.body)
        self.assertNotIn("cosign", strip_yaml_comments(PROMOTE.read_text(encoding="utf-8")).lower()
                         .split("def verify_candidate_signature")[0].split("import")[0])

    def test_a_branch_run_cannot_reach_the_key(self) -> None:
        self.assertIn("branch runs cannot sign", self.body)
        self.assertNotIn("SIGNING_SECRET", self.branch_yaml)
        self.assertNotIn("environment: production-signing", self.branch_yaml)

    def test_the_unsigned_branch_push_is_the_explicit_opt_in_the_page_names(self) -> None:
        self.assertIn("`allow_unsigned` opt-in", self.body)
        self.assertIn('allow_unsigned: "true"', self.branch_yaml)

    def test_automation_accounts_stop_before_the_push_entirely(self) -> None:
        self.assertIn("automation accounts such as Renovate stop before the push entirely", self.body)
        self.assertIn("actor_is_bot", self.branch_yaml)


class PromotionTests(unittest.TestCase):
    """`### 6.` -- promotion copies one digest onto two tags, and signs nothing new."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = section(doc_text(), "### 6. Promote Candidate To Stable")
        cls.items = numbered_items(cls.body)
        cls.source = PROMOTE.read_text(encoding="utf-8")

    def test_promotion_names_exactly_the_two_tags_promote_stable_writes(self) -> None:
        documented = {skeleton(BACKTICKED_RE.findall(item)[0]) for item in self.items}
        self.assertEqual(documented, {"latest", "stable-{}-{}"})

    def test_promotion_copies_a_digest_rather_than_rebuilding(self) -> None:
        self.assertIn("Promotion only copies the tested candidate digest", normalized(self.body))
        self.assertIn("_copy_and_verify_digest(", self.source)
        self.assertEqual(self.source.count("_copy_and_verify_digest(\n"), 2)

    def test_the_signature_carries_over_because_both_tags_are_one_digest(self) -> None:
        self.assertIn("both promoted tags resolve to the same digest", normalized(self.body))
        self.assertIn("source_digest=candidate_digest", self.source)

    def test_the_legacy_attachment_storage_the_page_names_is_what_the_signer_uses(self) -> None:
        self.assertIn("legacy cosign attachment storage", normalized(self.body))
        self.assertIn("--registry-referrers-mode=legacy", SIGN.read_text(encoding="utf-8"))

    def test_the_reason_the_page_gives_is_bootc_discovering_the_signature(self) -> None:
        self.assertIn("bootc's current containers/image policy path can discover the signature",
                      normalized(self.body))


class ConstraintsTests(unittest.TestCase):
    """`## Constraints And Context` -- the five rules the rest of the page is built on."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = numbered_items(section(doc_text(), "## Constraints And Context"))

    def test_the_section_still_states_five_constraints(self) -> None:
        self.assertEqual(len(self.items), 5)

    def test_branch_testing_must_not_overwrite_latest(self) -> None:
        self.assertIn("Branch testing must not overwrite `latest`", self.items[2])
        # `promote_stable` is the only thing that writes `:latest`, and only `build.yml`
        # runs it.
        self.assertIn(
            "ci_tools.cli promote-stable", strip_yaml_comments(BUILD_MAIN.read_text(encoding="utf-8"))
        )
        for workflow in (BUILD_BRANCH, BUILD_PR):
            with self.subTest(workflow=workflow.name):
                self.assertNotIn(
                    "promote-stable", strip_yaml_comments(workflow.read_text(encoding="utf-8"))
                )

    def test_seeding_a_missing_cache_is_a_main_workflow_action(self) -> None:
        item = normalized(self.items[4])
        self.assertIn("`workflow_dispatch` with `rebuild_akmods=true`", item)
        self.assertIn("never rebuild or republish it", item)


class PurposeTests(unittest.TestCase):
    """`## Purpose` -- the four things the build is meant to prove, and where they live."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.items = numbered_items(section(doc_text(), "## Purpose"))

    def test_the_objective_lists_four_steps(self) -> None:
        self.assertEqual(len(self.items), 4)

    def test_the_failure_step_names_the_workflow_run_as_the_gate(self) -> None:
        self.assertIn("fail in the GitHub Actions workflow run before a broken image replaces `latest`",
                      normalized(self.items[3]))

    def test_the_build_the_page_describes_is_the_native_containerfile_one(self) -> None:
        purpose = normalized(section(doc_text(), "## Purpose"))
        self.assertIn("native `Containerfile` build", purpose)
        self.assertTrue(CONTAINERFILE.is_file())
        self.assertIn("-f ./Containerfile", BUILD_ACTION.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
