"""
Script: tests/test_akmods_fork_maintenance_doc.py
What: Joins docs/akmods-fork-maintenance.md to the code, defaults file, action and workflow
whose behaviour it restates by hand.
Doing: Extracts the document's own claims -- its three numbered ref-resolution modes, the
default tracking ref it quotes, the two OCI label names, the `/tmp/akmods` clone path, the job
name and workflow input it tells a maintainer to run -- and recomputes each one against the
tree, executing the real cascade, the real clone helper and the real cache-status property
rather than grepping for the strings.
Why: This is the page a maintainer reads during an upstream outage, when deciding whether to
freeze the build to a known-good SHA. Its cascade is a hand copy of `_resolve_default_akmods_ref`,
its provenance section is a hand copy of the labels `.github/actions/build-native-image` passes
to buildah, and its WARNING block exists to stop a reader from trusting `akmods-ref` as proof of
what shipped. No test opened the file, so any of that could drift -- or be fixed -- and the
document would keep reading exactly the same.
Goal: Make the document fail here when the machine moves under it, in both directions.

Parses Markdown and YAML by hand. CI installs pytest, pytest-cov and ruff and nothing else
(see .github/workflows/test.yml), so a PyYAML import here would skip in exactly the place these
assertions are meant to run. `_section`, `_numbered_items` and `_step_with` are the whole
parser and carry their own case table in `ParserTests` below, because a hand-rolled parser that
is never wrong about a fixture is the only thing keeping the assertions built on it honest.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from ci_tools import akmods_clone_pinned
from ci_tools.check_akmods_cache import AkmodsCacheStatus, _has_kernel_matching_rpm
from ci_tools.common import CiToolError, load_repo_defaults
from ci_tools.pin_akmods_cache import pin_akmods_cache_image
from ci_tools.resolve_build_inputs import (
    _resolve_default_akmods_ref,
    resolve_configured_inputs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "akmods-fork-maintenance.md"
DEFAULTS_FILE = REPO_ROOT / "ci" / "defaults.json"
LOCK_FILE = REPO_ROOT / "ci" / "inputs.lock.json"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
BUILD_ACTION = REPO_ROOT / ".github" / "actions" / "build-native-image" / "action.yml"
BUILD_ACTION_USES = "./.github/actions/build-native-image"

DOC_TEXT = DOC.read_text(encoding="utf-8")

# Every environment variable the cascade reads. Wiped before each execution so a value
# inherited from the surrounding process cannot decide the result.
CASCADE_ENV = (
    "DEFAULT_AKMODS_REF",
    "AKMODS_UPSTREAM_REF",
    "AKMODS_UPSTREAM_TRACK",
    "AKMODS_UPSTREAM_REPO",
)

ENV_SHA = "a" * 40
PIN_SHA = "b" * 40
TRACK_SHA = "c" * 40


def _section(text: str, heading: str) -> list[str]:
    """
    Return the lines under `heading`, up to the next heading of the same or higher level.

    `heading` is the full heading line including its `#` markers, so asking for `## Purpose`
    can never accidentally match `### Purpose`. Raises when the heading is absent or the
    section is empty: a renamed or emptied section must fail loudly here rather than quietly
    hand every assertion below an empty list to pass against.
    """

    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise AssertionError(f"{DOC.name} has no heading {heading!r}") from exc

    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        body.append(line)

    if not any(line.strip() for line in body):
        raise AssertionError(f"{DOC.name} section {heading!r} is empty")
    return body


def _numbered_items(lines: list[str]) -> list[str]:
    """
    Return the first top-level ordered list in `lines`, each item flattened to one string.

    A line that is neither blank nor a new `N. ` item continues the current item, because the
    document hard-wraps its longer steps. A blank line ends the list -- several sections here
    hold more than one ordered list, and merging them would renumber items that are not part
    of the same sequence. Numbering must run 1..n within that list: a document that skips or
    repeats a number is a parse this file refuses rather than guesses at.
    """

    items: list[str] = []
    open_item = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if items:
                break
            open_item = False
            continue
        match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if match and not line.startswith("   "):
            expected = len(items) + 1
            if int(match.group(1)) != expected:
                raise AssertionError(f"ordered list is not sequential at item {match.group(1)!r}")
            items.append(match.group(2))
            open_item = True
        elif open_item:
            items[-1] = f"{items[-1]} {stripped}"
    return items


def _step_with(workflow_text: str, uses: str) -> dict[str, str]:
    """
    Return the `with:` mapping of the step whose `uses:` is `uses`.

    Indentation-bound: the mapping ends at the first line indented no further than `with:`
    itself, so a reindent that moves an input out of the block changes the result here
    instead of being silently tolerated.
    """

    lines = workflow_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"uses: {uses}":
            continue
        for offset in range(index + 1, len(lines)):
            candidate = lines[offset]
            if not candidate.strip():
                continue
            if candidate.strip() != "with:":
                break
            indent = len(candidate) - len(candidate.lstrip())
            mapping: dict[str, str] = {}
            for entry in lines[offset + 1 :]:
                if not entry.strip():
                    continue
                if len(entry) - len(entry.lstrip()) <= indent:
                    break
                key, _, value = entry.strip().partition(":")
                mapping[key.strip()] = value.strip()
            return mapping
    raise AssertionError(f"no step in the workflow uses {uses!r} with a `with:` block")


def _buildah_labels(action_text: str) -> dict[str, str]:
    """
    Return `{label name: environment variable}` for every `--label` the build action passes.

    The action writes them as `--label "name=${VAR}"`, one per line. Reading the flags rather
    than the env block means a label that stops being passed to buildah disappears from this
    mapping even while the env var that fed it survives.
    """

    pattern = re.compile(r'--label "([^"=]+)=\$\{([A-Z0-9_]+)\}"')
    return {name: var for name, var in pattern.findall(action_text)}


def _wiped_env(**overrides: str) -> dict[str, str]:
    env = {name: "" for name in CASCADE_ENV}
    env.update(overrides)
    return env


class ParserTests(unittest.TestCase):
    """The parsers above, against fixtures whose right answer is written out by hand."""

    FIXTURE = textwrap.dedent(
        """\
        # Title

        ## Alpha

        1. first item
        2. second item that
           wraps onto another line

        prose after the list

        ### Alpha Nested

        nested body

        ## Beta

        beta body
        """
    )

    def test_section_stops_at_next_same_level_heading(self) -> None:
        body = "\n".join(_section(self.FIXTURE, "## Alpha"))
        self.assertIn("nested body", body)
        self.assertNotIn("beta body", body)

    def test_section_of_deeper_heading_excludes_its_parent(self) -> None:
        body = "\n".join(_section(self.FIXTURE, "### Alpha Nested"))
        self.assertEqual(body.strip(), "nested body")

    def test_section_rejects_missing_heading(self) -> None:
        with self.assertRaises(AssertionError):
            _section(self.FIXTURE, "## Gamma")

    def test_section_rejects_empty_section(self) -> None:
        with self.assertRaises(AssertionError):
            _section("## Alpha\n\n## Beta\n\nbody\n", "## Alpha")

    def test_numbered_items_join_wrapped_lines_and_stop_at_blank(self) -> None:
        items = _numbered_items(_section(self.FIXTURE, "## Alpha"))
        self.assertEqual(
            items,
            ["first item", "second item that wraps onto another line"],
        )

    def test_numbered_items_do_not_merge_a_second_list_in_the_same_section(self) -> None:
        lines = ["1. one", "2. two", "", "prose between the lists", "", "1. other list"]
        self.assertEqual(_numbered_items(lines), ["one", "two"])

    def test_numbered_items_reject_a_gap_in_the_numbering(self) -> None:
        with self.assertRaises(AssertionError):
            _numbered_items(["1. one", "3. three"])

    def test_step_with_reads_only_its_own_block(self) -> None:
        workflow = textwrap.dedent(
            """\
            - name: Something else
              uses: ./.github/actions/other
              with:
                akmods_image: wrong
            - name: Target
              uses: ./.github/actions/target
              with:
                akmods_image: right
                zfs_version: 2.4.3
            - name: After
              run: true
            """
        )
        self.assertEqual(
            _step_with(workflow, "./.github/actions/target"),
            {"akmods_image": "right", "zfs_version": "2.4.3"},
        )

    def test_step_with_rejects_a_step_that_has_no_with_block(self) -> None:
        workflow = textwrap.dedent(
            """\
            - name: Target
              uses: ./.github/actions/target
            - name: After
              run: true
            """
        )
        with self.assertRaises(AssertionError):
            _step_with(workflow, "./.github/actions/target")

    def test_buildah_labels_map_each_label_to_its_variable(self) -> None:
        action = '          --label "org.example.one=${ONE}" \\\n          --label "org.example.two=${TWO}" \\\n'
        self.assertEqual(
            _buildah_labels(action),
            {"org.example.one": "ONE", "org.example.two": "TWO"},
        )


class LinkTargetTests(unittest.TestCase):
    """Every relative link the document offers still points at a file that exists."""

    def test_relative_links_resolve(self) -> None:
        targets = re.findall(r"\]\((?!https?://)([^)#]+)(?:#[^)]*)?\)", DOC_TEXT)
        self.assertGreaterEqual(
            len(targets),
            6,
            "the document stopped linking to the files it describes",
        )
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(
                    (DOC.parent / target).resolve().exists(),
                    f"{DOC.name} links to {target}, which does not exist",
                )


class RefCascadeTests(unittest.TestCase):
    """
    "How The Akmods Ref Is Chosen" against `_resolve_default_akmods_ref`.

    The document numbers three modes and says they are "checked in order". Each test below
    executes the real cascade in that mode, against the committed `ci/defaults.json` wherever
    the mode is about what this repository is configured to do.
    """

    def setUp(self) -> None:
        self.items = _numbered_items(_section(DOC_TEXT, "## How The Akmods Ref Is Chosen"))
        self.defaults = load_repo_defaults()

    def test_document_lists_exactly_the_three_modes_the_cascade_implements(self) -> None:
        self.assertEqual(len(self.items), 3, "the cascade has three modes; the document must too")
        first, second, third = self.items
        self.assertIn("AKMODS_UPSTREAM_REF", first)
        self.assertIn("DEFAULT_AKMODS_REF", first)
        self.assertIn("process environment", first)
        self.assertIn("ci/defaults.json", second)
        self.assertIn("AKMODS_UPSTREAM_REF", second)
        self.assertIn("non-empty", second)
        self.assertIn("AKMODS_UPSTREAM_TRACK", third)
        self.assertIn("git ls-remote", third)
        self.assertIn("AKMODS_UPSTREAM_REPO", third)

    def test_mode_one_env_override_beats_a_pin_in_the_defaults_file(self) -> None:
        pinned = dict(self.defaults, AKMODS_UPSTREAM_REF=PIN_SHA)
        for variable in ("AKMODS_UPSTREAM_REF", "DEFAULT_AKMODS_REF"):
            with self.subTest(variable=variable):
                with (
                    patch.dict(os.environ, _wiped_env(**{variable: ENV_SHA}), clear=False),
                    patch(
                        "ci_tools.resolve_build_inputs.load_repo_defaults",
                        return_value=pinned,
                    ),
                    patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
                ):
                    resolved = _resolve_default_akmods_ref()
                self.assertEqual(resolved, ENV_SHA)
                ls_remote.assert_not_called()

    def test_mode_two_non_empty_pin_beats_the_tracking_ref(self) -> None:
        pinned = dict(self.defaults, AKMODS_UPSTREAM_REF=PIN_SHA)
        with (
            patch.dict(os.environ, _wiped_env(), clear=False),
            patch("ci_tools.resolve_build_inputs.load_repo_defaults", return_value=pinned),
            patch("ci_tools.resolve_build_inputs.git_ls_remote_resolve") as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()
        self.assertEqual(resolved, PIN_SHA)
        ls_remote.assert_not_called()

    def test_mode_three_floats_this_repository_onto_the_track_the_document_quotes(self) -> None:
        match = re.search(
            r"`AKMODS_UPSTREAM_TRACK` \(default `\"([^\"]+)\"`\)",
            self.items[2],
        )
        self.assertIsNotNone(match, "the document no longer quotes a default tracking ref")
        documented_track = match.group(1)

        with (
            patch.dict(os.environ, _wiped_env(), clear=False),
            patch(
                "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                return_value=TRACK_SHA,
            ) as ls_remote,
        ):
            resolved = _resolve_default_akmods_ref()

        self.assertEqual(resolved, TRACK_SHA)
        ls_remote.assert_called_once_with(
            self.defaults["AKMODS_UPSTREAM_REPO"],
            documented_track,
        )

    def test_the_checked_in_pin_is_empty_so_the_floating_default_is_live(self) -> None:
        """
        "Update Process" step 6: clearing the pin resumes floating.

        The committed file is the unfrozen state that step describes, so the assertion is
        both that the document still says `""` and that the file still holds it. A pin left
        behind in `ci/defaults.json` would make every other claim on this page describe a
        repository that is not this one.
        """

        steps = _numbered_items(_section(DOC_TEXT, "## Update Process"))
        unfreeze = steps[-1]
        self.assertIn("AKMODS_UPSTREAM_REF", unfreeze)
        self.assertIn('`""`', unfreeze)
        self.assertEqual(json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))["AKMODS_UPSTREAM_REF"], "")

    def test_plain_language_model_names_the_configured_fork(self) -> None:
        pieces = _numbered_items(_section(DOC_TEXT, "## Plain-Language Model"))
        fork = re.search(r"configured fork repository: `([^`]+)`", pieces[0])
        self.assertIsNotNone(fork, "the document no longer names the configured fork")
        self.assertIn(fork.group(1), self.defaults["AKMODS_UPSTREAM_REPO"])


class ProvenanceRecordTests(unittest.TestCase):
    """"Every build records the resolved commit SHA in two places" -- both places."""

    def setUp(self) -> None:
        body = _section(DOC_TEXT, "## How The Akmods Ref Is Chosen")
        marker = "Every build records the resolved commit SHA in two places:"
        start = next(index for index, line in enumerate(body) if line.strip() == marker)
        self.records = _numbered_items(body[start + 1 :])
        self.labels = _buildah_labels(BUILD_ACTION.read_text(encoding="utf-8"))

    def test_document_lists_two_records(self) -> None:
        self.assertEqual(len(self.records), 2)
        self.assertIn("build-inputs", self.records[0])
        self.assertIn("manifest artifact", self.records[0])

    def test_the_documented_label_is_one_buildah_writes_from_the_resolved_ref(self) -> None:
        label = re.search(r"`(org\.[a-z0-9.-]+)`", self.records[1])
        self.assertIsNotNone(label, "the document no longer names the provenance label")
        self.assertEqual(self.labels.get(label.group(1)), "AKMODS_UPSTREAM_REF")

    def test_the_build_inputs_manifest_really_records_the_resolved_ref(self) -> None:
        env = {
            "GITHUB_REPOSITORY": "Danathar/zfs-kinoite-complex",
            "GITHUB_WORKFLOW": "build",
            "GITHUB_RUN_ID": "1",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_NUMBER": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "f" * 40,
            "GITHUB_ACTOR": "someone",
            "USE_INPUT_LOCK": "false",
            "LOCK_FILE_PATH": "ci/inputs.lock.json",
            "FEDORA_VERSION": "44",
            "KERNEL_RELEASE": "7.1.4-204.fc44.x86_64",
            "DETECTED_KERNEL_RELEASES": "7.1.4-204.fc44.x86_64",
            "BASE_IMAGE_REF": "quay.io/example/kinoite:44",
            "BASE_IMAGE_NAME": "quay.io/example/kinoite",
            "BASE_IMAGE_TAG": "44",
            "BASE_IMAGE_PINNED": "quay.io/example/kinoite@sha256:" + "0" * 64,
            "BASE_IMAGE_DIGEST": "sha256:" + "0" * 64,
            "BUILD_CONTAINER_REF": "ghcr.io/example/devcontainer@sha256:" + "1" * 64,
            "BUILD_CONTAINER_PINNED": "ghcr.io/example/devcontainer@sha256:" + "1" * 64,
            "BUILD_CONTAINER_DIGEST": "sha256:" + "1" * 64,
            "BREW_IMAGE_REF": "ghcr.io/example/brew@sha256:" + "2" * 64,
            "BREW_IMAGE_PINNED": "ghcr.io/example/brew@sha256:" + "2" * 64,
            "BREW_IMAGE_DIGEST": "sha256:" + "2" * 64,
            "ZFS_MINOR_VERSION": "2.4",
            "ZFS_VERSION": "2.4.3",
            "AKMODS_UPSTREAM_REF": TRACK_SHA,
        }
        # Imported here: the module writes to a path relative to the process working
        # directory, so it must be driven from inside the temporary directory below.
        from ci_tools import write_build_inputs_manifest

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                with patch.dict(os.environ, env, clear=False):
                    write_build_inputs_manifest.main()
                document = json.loads(
                    (Path(temp_dir) / write_build_inputs_manifest.ARTIFACT_PATH).read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                os.chdir(previous)

        self.assertEqual(document["inputs"]["akmods_upstream_ref"], TRACK_SHA)


class AkmodsImageCaveatTests(unittest.TestCase):
    """
    The WARNING block: what it tells a reader to trust instead of `akmods-ref`.

    Three separate machine claims live in one sentence -- that the other label carries a
    digest-pinned ref, that it is the cache the build actually consumed, and that
    `check_akmods_cache.py` verified both the `kmod-zfs` RPM and a signature before it was
    reused. Each is recomputed below.
    """

    def setUp(self) -> None:
        body = _section(DOC_TEXT, "## How The Akmods Ref Is Chosen")
        self.warning = " ".join(
            line.lstrip("> ").strip() for line in body if line.startswith(">")
        )
        self.assertIn("[!WARNING]", self.warning)
        label = re.search(r"`(org\.[a-z0-9.-]+akmods-image)`", self.warning)
        self.assertIsNotNone(label, "the WARNING no longer names the akmods-image label")
        self.documented_label = label.group(1)

    def test_the_trusted_label_is_written_from_the_cache_image_the_build_consumed(self) -> None:
        labels = _buildah_labels(BUILD_ACTION.read_text(encoding="utf-8"))
        self.assertEqual(labels.get(self.documented_label), "AKMODS_IMAGE")

    def test_the_build_step_feeds_that_input_the_digest_pinned_cache_ref(self) -> None:
        inputs = _step_with(BUILD_WORKFLOW.read_text(encoding="utf-8"), BUILD_ACTION_USES)
        self.assertRegex(
            inputs["akmods_image"],
            r"outputs\.akmods_image_pinned\s*\}\}$",
            "the candidate build no longer receives the digest-pinned cache ref",
        )

    def test_pinning_produces_a_digest_ref_rather_than_the_mutable_tag(self) -> None:
        digest = "sha256:" + "3" * 64
        with patch(
            "ci_tools.pin_akmods_cache.skopeo_inspect_digest",
            return_value=digest,
        ):
            pinned, returned = pin_akmods_cache_image("ghcr.io/example/akmods:main-44")
        self.assertEqual(pinned, f"ghcr.io/example/akmods@{digest}")
        self.assertEqual(returned, digest)

    def test_reuse_requires_both_the_rpm_and_the_signature(self) -> None:
        """The document says "verified ... to contain the required `kmod-zfs` RPM *and* to
        carry a valid signature"; `reusable` is where that conjunction is enforced."""

        self.assertIn("kmod-zfs", self.warning)
        self.assertIn("signature", self.warning)
        for missing_release, signed, expected in (
            ("", True, True),
            ("", False, False),
            ("7.1.4-204.fc44.x86_64", True, False),
            ("7.1.4-204.fc44.x86_64", False, False),
        ):
            with self.subTest(missing_release=missing_release, signature_verified=signed):
                status = AkmodsCacheStatus(
                    source_image="ghcr.io/example/akmods:main-44",
                    image_exists=True,
                    missing_release=missing_release,
                    signature_verified=signed,
                )
                self.assertEqual(status.reusable, expected)

        self.assertFalse(
            AkmodsCacheStatus(
                source_image="ghcr.io/example/akmods:main-44",
                image_exists=False,
                signature_verified=True,
            ).reusable
        )

    def test_the_required_rpm_is_matched_on_the_exact_kernel_and_zfs_version(self) -> None:
        kernel = "7.1.4-204.fc44.x86_64"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_dir = root / "rpms" / "kmods" / "zfs"
            rpm_dir.mkdir(parents=True)
            (rpm_dir / f"kmod-zfs-{kernel}-2.4.3-1.fc44.x86_64.rpm").touch()
            self.assertTrue(_has_kernel_matching_rpm(root, kernel, "2.4.3"))
            self.assertFalse(_has_kernel_matching_rpm(root, kernel, "2.4.4"))
            self.assertFalse(_has_kernel_matching_rpm(root, "7.1.5-205.fc44.x86_64", "2.4.3"))


class CloneContractTests(unittest.TestCase):
    """"Plain-Language Model" and "Important Current Assumption" against the clone helper."""

    def test_the_documented_clone_directory_is_the_one_the_helper_uses(self) -> None:
        pieces = _numbered_items(_section(DOC_TEXT, "## Plain-Language Model"))
        temporary = next(item for item in pieces if "temporary clone" in item)
        path = re.search(r"`(/[^`]+)`", temporary)
        self.assertIsNotNone(path, "the document no longer names the clone directory")
        self.assertEqual(akmods_clone_pinned.AKMODS_WORKTREE, Path(path.group(1)))

    def test_the_clone_verifies_the_commit_sha_and_never_patches_the_justfile(self) -> None:
        assumption = " ".join(_section(DOC_TEXT, "## Important Current Assumption"))
        self.assertIn("no longer patches the cloned akmods `Justfile` at runtime", assumption)

        with tempfile.TemporaryDirectory() as temp_dir:
            worktree = Path(temp_dir) / "akmods"
            with (
                patch.object(akmods_clone_pinned, "AKMODS_WORKTREE", worktree),
                patch(
                    "ci_tools.akmods_clone_pinned.run_cmd",
                    side_effect=["", "", "", "", f"{TRACK_SHA}\n"],
                ) as run_cmd,
            ):
                akmods_clone_pinned.clone_pinned("https://example.invalid/akmods.git", TRACK_SHA)

            argv = [call.args[0] for call in run_cmd.call_args_list]
            self.assertIn(["git", "rev-parse", "HEAD"], argv)
            for command in argv:
                self.assertNotIn("Justfile", " ".join(command))

            # A remote that resolved to some other commit must stop the run rather than
            # build from it: that is the whole of the document's "verify the commit SHA".
            with (
                patch.object(akmods_clone_pinned, "AKMODS_WORKTREE", worktree),
                patch(
                    "ci_tools.akmods_clone_pinned.run_cmd",
                    side_effect=["", "", "", "", f"{PIN_SHA}\n"],
                ),
                self.assertRaises(CiToolError),
            ):
                akmods_clone_pinned.clone_pinned("https://example.invalid/akmods.git", TRACK_SHA)


class ReplayClaimTests(unittest.TestCase):
    """
    "Why A Pin Still Exists At All": what a lock-file replay does with the akmods ref.

    The document tells a maintainer that replaying `ci/inputs.lock.json` will not reproduce
    the original upstream commit unless they set `AKMODS_UPSTREAM_REF` themselves. Both
    halves are executed here against the committed lock file's shape.
    """

    def setUp(self) -> None:
        self.claim = " ".join(_section(DOC_TEXT, "## Why A Pin Still Exists At All"))
        self.lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))

    def _replay(self, **env: str) -> str:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "inputs.lock.json"
            replayed = dict(self.lock)
            replayed["base_image"] = "quay.io/example/kinoite:44"
            replayed["build_container"] = ""
            lock_path.write_text(json.dumps(replayed), encoding="utf-8")
            base_env = _wiped_env(
                USE_INPUT_LOCK="true",
                LOCK_FILE=str(lock_path),
                BUILD_CONTAINER_REF="ghcr.io/example/devcontainer@sha256:" + "1" * 64,
            )
            base_env.update(env)
            with (
                patch.dict(os.environ, base_env, clear=False),
                patch(
                    "ci_tools.resolve_build_inputs.git_ls_remote_resolve",
                    return_value=TRACK_SHA,
                ),
            ):
                return resolve_configured_inputs().akmods_upstream_ref

    def test_document_says_the_lock_file_does_not_carry_the_akmods_commit(self) -> None:
        self.assertIn("lock file does *not* carry the akmods commit", self.claim)
        self.assertNotIn("akmods_upstream_ref", self.lock)

    def test_a_replay_without_an_explicit_ref_falls_back_to_the_tracking_ref(self) -> None:
        self.assertEqual(self._replay(), TRACK_SHA)

    def test_setting_the_ref_explicitly_replays_that_commit(self) -> None:
        self.assertEqual(self._replay(AKMODS_UPSTREAM_REF=ENV_SHA), ENV_SHA)


class ValidationInstructionTests(unittest.TestCase):
    """The two things the document tells a maintainer to run still exist under those names."""

    def test_the_named_akmods_job_exists_in_build_yml(self) -> None:
        items = _numbered_items(_section(DOC_TEXT, "## What To Validate After Changing A Pin"))
        job = re.search(r"`([^`]+)` still succeeds", items[0])
        self.assertIsNotNone(job, "the document no longer names the akmods job to re-run")
        self.assertIn(
            f"name: {job.group(1)}",
            BUILD_WORKFLOW.read_text(encoding="utf-8"),
        )

    def test_the_named_workflow_and_dispatch_input_exist(self) -> None:
        items = _numbered_items(_section(DOC_TEXT, "### Validate The Sync"))
        after_push = next(item for item in items if "after pushing" in item)
        workflow = re.search(r"`(build-branch)` workflow", after_push)
        dispatch = re.search(r"`rebuild_akmods=(true|false)`", after_push)
        self.assertIsNotNone(workflow, "the document no longer names the branch validation workflow")
        self.assertIsNotNone(dispatch, "the document no longer names the rebuild input")
        self.assertTrue(
            (REPO_ROOT / ".github" / "workflows" / f"{workflow.group(1)}.yml").exists()
        )
        build_text = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("rebuild_akmods:", build_text)
        self.assertIn("github.event.inputs.rebuild_akmods", build_text)


if __name__ == "__main__":
    unittest.main()
