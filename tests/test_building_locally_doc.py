"""
Script: tests/test_building_locally_doc.py
What: Joins docs/building-locally.md -- the page README sends a reader to when they want to
"build or fork it myself" -- to the files it describes: the build arguments
`.github/actions/build-native-image/action.yml` passes, the `ARG` defaults in `Containerfile`,
the values in `ci/defaults.json`, the keys of `ci/inputs.lock.json`, and the files that
carry the base image by value.
Doing: Parses the page's `podman build` example and recomputes every value in it from the
file that owns it, counts the build arguments CI passes and compares that to the page's
number word, and compares the "Changing The Base Image" list to the set of tracked files and
`ci/defaults.json` keys that really carry the base image -- in both directions.
Why: Every claim on this page is a hand-copy of something that lives in the machine, and the
page was read by no test as a subject. Its only references under `tests/` were a comment in
tests/test_docs_consistency.py recalling a link it once got wrong and a docstring sentence in
tests/test_issue_templates.py. Four claims had already gone stale when this file was written.
Goal: Make a new build argument, a moved default, a lock-file key nobody documented, or a new
file carrying the base image fail here, instead of leaving a forker following a list that
leaves part of the build pointed at the old image.

The four stale claims this file was written against:

  * "CI uses `.github/actions/build-native-image`, which calls `buildah build` directly with
    the same flags shown below" -- the example showed two `--build-arg`s and CI passes five.
    `BREW_IMAGE` was named nowhere on the page, although `Containerfile` defaults it to the
    floating `ghcr.io/ublue-os/brew:latest` and the build fails on any path that tag adds
    beyond `build_files/brew-payload.manifest`. Brew is the image this repository's docs keep
    forgetting: docs/glossary.md and docs/zfs-kinoite-testing.md had both dropped it too.
  * "`AKMODS_IMAGE` is the only build argument that is genuinely required outside CI" --
    `Containerfile` declares `ARG AKMODS_IMAGE=""` and the install helper falls back to
    `AKMODS_IMAGE_TEMPLATE`, which the page's own note 2 said two paragraphs later.
  * "Changing The Base Image" listed `DEFAULT_BASE_IMAGE` as the `ci/defaults.json` key to
    change and omitted `STABLE_SIGNAL_IMAGE`, which carries the same value and which
    docs/architecture-overview.md says must keep naming the image the build consumes. A fork
    following the list would build from its new base while the scheduled-build gate kept
    watching Kinoite.
  * The same list sent the reader to `README.md` to "update example `BASE_IMAGE` arguments".
    README carries no base image and no `BASE_IMAGE`; the files that do are this page,
    docs/architecture-overview.md and docs/glossary.md.

No PyYAML, for the reason tests/test_docs_consistency.py gives. Assertions about the
composite action read comment-stripped text, because a search a comment satisfies is a
search that cannot fail.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import shlex
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "building-locally.md"
DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
LOCK = REPO_ROOT / "ci" / "inputs.lock.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD_ACTION = REPO_ROOT / ".github" / "actions" / "build-native-image" / "action.yml"
INSTALL_HELPER = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
BREW_MANIFEST = REPO_ROOT / "build_files" / "brew-payload.manifest"

# The registry owner is lower-cased before it reaches any ref, so every ref the page prints
# carries this spelling rather than the `Danathar` of the repository URL.
IMAGE_ORG = "danathar"

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BACKTICKED_RE = re.compile(r"`([^`]+)`")
NUMBERED_RE = re.compile(r"^(\d+)\. (.*)$")
CI_BUILD_ARG_RE = re.compile(r'--build-arg "([A-Z_]+)=')
CONTAINERFILE_ARG_RE = re.compile(r'^ARG ([A-Z_]+)="([^"]*)"$', re.MULTILINE)
JQ_RE = re.compile(r"^\$\(jq -r \.([A-Z_]+) (\S+)\)$")

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}

# Each phrase the lock-file paragraph uses, and the `ci/inputs.lock.json` keys it stands for.
# The union of the values has to be every key the lock file carries besides its own
# `version` and `description`, so a new pinned input nobody wrote down fails, and so does a
# phrase left behind after its key was removed.
LOCK_PHRASES = {
    "the base image ref": {"base_image"},
    "the build container ref": {"build_container"},
    "the Homebrew payload image ref": {"brew_image"},
    "the OpenZFS version (line plus, if set, the exact patch)": {"zfs_minor_version", "zfs_version"},
}
LOCK_METADATA_KEYS = {"version", "description"}


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def normalized(text: str) -> str:
    """Squash whitespace so a phrase still matches across a Markdown line wrap."""

    return re.sub(r"\s+", " ", text)


def section(heading: str) -> str:
    """Return one section's body, from its heading to the next heading of any level."""

    lines = doc_text().splitlines()
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


def prose(body: str) -> str:
    """Drop fenced code blocks, so a backtick scan cannot pair across a fence."""

    return re.sub(r"```.*?```", "", body, flags=re.DOTALL)


def backticked(body: str) -> set[str]:
    return set(BACKTICKED_RE.findall(prose(body)))


def numbered_items(body: str) -> dict[int, str]:
    """
    Return a section's top-level numbered items by number, continuation lines folded in.

    Items here carry indented `-` sub-bullets; those belong to the item above them.
    """

    items: dict[int, str] = {}
    current = None
    for line in prose(body).splitlines():
        match = NUMBERED_RE.match(line)
        if match:
            current = int(match.group(1))
            items[current] = match.group(2)
        elif current is not None and line.strip() and line.startswith((" ", "\t")):
            items[current] = f"{items[current]} {line.strip()}"
    return {number: normalized(text) for number, text in items.items()}


def podman_command() -> list[str]:
    """Return the page's `podman build` example as argv, backslash continuations folded."""

    blocks = re.findall(r"```bash\n(.*?)```", section("## Local Build"), flags=re.DOTALL)
    commands = [block for block in blocks if block.lstrip().startswith("podman build")]
    if len(commands) != 1:
        raise AssertionError(f"expected one `podman build` block, found {len(commands)}")
    return shlex.split(commands[0].replace("\\\n", " "))


def example_build_args() -> dict[str, str]:
    argv = podman_command()
    found: dict[str, str] = {}
    for flag, value in itertools.pairwise(argv):
        if flag == "--build-arg":
            name, _, arg_value = value.partition("=")
            if name in found:
                raise AssertionError(f"the example passes --build-arg {name} twice")
            found[name] = arg_value
    return found


def strip_yaml_comments(text: str) -> str:
    """Drop whole-line `#` comments and trailing ` #` comments from YAML."""

    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def ci_build_args() -> list[str]:
    return CI_BUILD_ARG_RE.findall(strip_yaml_comments(BUILD_ACTION.read_text(encoding="utf-8")))


def containerfile_args() -> dict[str, str]:
    return dict(CONTAINERFILE_ARG_RE.findall(CONTAINERFILE.read_text(encoding="utf-8")))


def defaults() -> dict[str, str]:
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def module_constant(path: Path, name: str) -> str:
    """Read one module-level string constant out of a file's syntax tree, without importing it."""

    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError(f"no module-level constant {name} in {path.relative_to(REPO_ROOT)}")


def repo_files_containing(needle: str) -> set[str]:
    """
    Every text file in the tree, outside tests/ and .git/, whose contents carry `needle`.

    tests/ is excluded because the fixtures that pin the default are a consequence of the
    value, not somewhere a forker is told to edit; they fail loudly on their own when it moves.
    """

    found = set()
    for path in REPO_ROOT.rglob("*"):
        relative = path.relative_to(REPO_ROOT)
        if not path.is_file() or relative.parts[0] in {".git", "tests"}:
            continue
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if needle in text:
            found.add(relative.as_posix())
    return found


class LocalBuildArgumentTests(unittest.TestCase):
    """The prose above the example has to account for every argument the build accepts."""

    def test_ci_passes_build_arguments_at_all(self) -> None:
        # Guard the guard: an empty list here would make every subset check below pass.
        self.assertGreaterEqual(len(ci_build_args()), 4)
        self.assertEqual(len(ci_build_args()), len(set(ci_build_args())))

    def test_the_count_the_page_gives_is_the_count_ci_passes(self) -> None:
        text = normalized(prose(section("## Local Build")))
        match = re.search(r"`buildah build` directly with (\w+) build arguments", text)
        self.assertIsNotNone(match, "the page no longer says how many arguments CI passes")
        self.assertEqual(NUMBER_WORDS.get(match.group(1).lower()), len(ci_build_args()))

    def test_the_sentence_listing_what_ci_passes_names_exactly_those_arguments(self) -> None:
        # Scoped to the sentence, not the section: note 4 also names `BREW_IMAGE`, so a
        # section-wide search stayed green when the list itself dropped it.
        text = normalized(prose(section("## Local Build")))
        match = re.search(r"directly with \w+ build arguments -- (.*?) --", text)
        self.assertIsNotNone(match, "the sentence listing CI's build arguments is gone")
        self.assertEqual(set(BACKTICKED_RE.findall(match.group(1))), set(ci_build_args()))

    def test_every_containerfile_arg_is_named_on_the_page(self) -> None:
        # Wider than the CI set: `AKMODS_IMAGE_TEMPLATE` is not passed by CI, but it is the
        # fallback note 2 describes, and a reader has to be able to find it.
        missing = sorted(set(containerfile_args()) - backticked(section("## Local Build")))
        self.assertEqual(missing, [], "Containerfile ARGs the page never names")

    def test_the_page_no_longer_claims_the_example_matches_ci(self) -> None:
        self.assertNotIn("same flags", normalized(section("## Local Build")))

    def test_the_page_does_not_call_any_argument_required(self) -> None:
        # Every ARG has a default, so "required" is false of each of them. Note 2 says
        # `AKMODS_IMAGE` can be omitted; the page once said, above it, that it could not.
        self.assertTrue(all(name in containerfile_args() for name in ci_build_args()))
        self.assertEqual(containerfile_args()["AKMODS_IMAGE"], "")
        self.assertNotIn("genuinely required", normalized(section("## Local Build")))
        self.assertIn("No build argument is strictly required", normalized(section("## Local Build")))


class LocalBuildExampleTests(unittest.TestCase):
    """Each value in the `podman build` example is recomputed from the file that owns it."""

    def test_the_example_passes_only_arguments_the_containerfile_declares(self) -> None:
        unknown = sorted(set(example_build_args()) - set(containerfile_args()))
        self.assertEqual(unknown, [])

    def test_the_example_passes_the_three_image_arguments(self) -> None:
        self.assertEqual(
            {"BASE_IMAGE", "AKMODS_IMAGE", "BREW_IMAGE"} - set(example_build_args()), set()
        )

    def test_the_example_builds_the_repository_root_under_a_local_tag(self) -> None:
        argv = podman_command()
        self.assertEqual(argv[:2], ["podman", "build"])
        self.assertEqual(argv[-1], ".")
        self.assertEqual(argv[argv.index("-t") + 1], f"{defaults()['IMAGE_NAME']}:local")

    def test_example_base_image_is_the_default_base_image(self) -> None:
        value = example_build_args()["BASE_IMAGE"]
        self.assertEqual(value, defaults()["DEFAULT_BASE_IMAGE"])
        self.assertEqual(value, containerfile_args()["BASE_IMAGE"])

    def test_example_akmods_image_is_what_the_fallback_would_render(self) -> None:
        # So passing it and omitting it (note 2) build the same thing, which is what makes
        # the example a safe starting point.
        base = example_build_args()["BASE_IMAGE"]
        fedora = base.rpartition(":")[2]
        self.assertRegex(fedora, r"^\d+$")
        template = containerfile_args()["AKMODS_IMAGE_TEMPLATE"]
        self.assertEqual(example_build_args()["AKMODS_IMAGE"], template.format(fedora=fedora))

    def test_the_akmods_template_names_this_repositorys_cache(self) -> None:
        template = containerfile_args()["AKMODS_IMAGE_TEMPLATE"]
        self.assertEqual(template, module_constant(INSTALL_HELPER, "DEFAULT_AKMODS_IMAGE_TEMPLATE"))
        self.assertEqual(template.partition(":")[0], f"ghcr.io/{IMAGE_ORG}/{defaults()['AKMODS_REPO']}")

    def test_example_brew_image_reads_the_pinned_default(self) -> None:
        match = JQ_RE.match(example_build_args()["BREW_IMAGE"])
        self.assertIsNotNone(match, "BREW_IMAGE is no longer read out of ci/defaults.json")
        key, path = match.groups()
        self.assertEqual(REPO_ROOT / path, DEFAULTS)
        self.assertEqual(key, "DEFAULT_BREW_IMAGE")
        self.assertIn("@sha256:", defaults()[key])


class LocalBuildNotesTests(unittest.TestCase):
    """The numbered notes under the example state facts about `Containerfile` defaults."""

    def notes(self) -> dict[int, str]:
        return numbered_items(section("## Local Build"))

    def test_note_2_names_the_template_the_helper_falls_back_to(self) -> None:
        note = self.notes()[2]
        self.assertIn("`AKMODS_IMAGE` can be omitted", note)
        self.assertIn("falls back to `AKMODS_IMAGE_TEMPLATE`", note)

    def test_note_4_quotes_the_floating_brew_default(self) -> None:
        note = self.notes()[4]
        default = containerfile_args()["BREW_IMAGE"]
        self.assertNotIn("@sha256:", default, "the brew default is pinned now; note 4 is stale")
        self.assertIn(f"`BREW_IMAGE` defaults to the floating `{default}`", note)

    def test_note_4_names_the_manifest_the_build_checks(self) -> None:
        note = self.notes()[4]
        linked = {target for target in LINK_RE.findall(note)}
        self.assertIn("../build_files/brew-payload.manifest", linked)
        self.assertTrue(BREW_MANIFEST.is_file())
        self.assertIn("`DEFAULT_BREW_IMAGE`", note)

    def test_the_brew_check_runs_before_the_payload_lands(self) -> None:
        # Note 4 says a new path "stops a local build at that check"; that is only true while
        # the check is its own RUN above the COPY that installs the payload.
        text = CONTAINERFILE.read_text(encoding="utf-8")
        check = text.index("/ctx/check-brew-payload-inventory.sh")
        copy = text.index("COPY --from=brew /system_files /")
        self.assertLess(check, copy)

    def test_note_5_defaults_are_this_repositorys_own(self) -> None:
        note = self.notes()[5]
        self.assertIn("`IMAGE_REPO` and `SIGNING_KEY_FILENAME` default", note)
        image_name = defaults()["IMAGE_NAME"]
        args = containerfile_args()
        self.assertEqual(args["IMAGE_REPO"], f"ghcr.io/{IMAGE_ORG}/{image_name}")
        self.assertEqual(args["SIGNING_KEY_FILENAME"], f"{image_name}.pub")


class NativeBuildFlowTests(unittest.TestCase):
    def test_the_named_base_is_the_default_base_without_its_tag(self) -> None:
        text = normalized(prose(section("## Native Build Flow")))
        match = re.search(r"`Containerfile` starts from `([^`]+)`", text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), defaults()["DEFAULT_BASE_IMAGE"].rpartition(":")[0])

    def test_the_final_stage_is_the_base_image_and_is_linted(self) -> None:
        text = CONTAINERFILE.read_text(encoding="utf-8")
        stages = re.findall(r"^FROM (\S+)", text, flags=re.MULTILINE)
        self.assertEqual(stages[-1], "${BASE_IMAGE}")
        self.assertIn("RUN bootc container lint", text.splitlines())


class LockFileParagraphTests(unittest.TestCase):
    """The replay paragraph lists what `ci/inputs.lock.json` pins and what it does not."""

    def paragraph(self) -> str:
        paragraphs = normalized(prose(section("## Local Build"))).split("For reproducing")
        self.assertEqual(len(paragraphs), 2, "the replay paragraph is gone or duplicated")
        return paragraphs[1]

    def lock_keys(self) -> set[str]:
        return set(json.loads(LOCK.read_text(encoding="utf-8"))) - LOCK_METADATA_KEYS

    def test_every_phrase_is_on_the_page(self) -> None:
        missing = [phrase for phrase in LOCK_PHRASES if phrase not in self.paragraph()]
        self.assertEqual(missing, [])

    def test_the_phrases_cover_exactly_the_keys_the_lock_file_carries(self) -> None:
        documented = set().union(*LOCK_PHRASES.values())
        self.assertEqual(documented, self.lock_keys())

    def test_the_akmods_commit_is_not_in_the_lock_file_but_is_in_defaults(self) -> None:
        self.assertIn("does **not** pin the akmods fork commit", self.paragraph())
        self.assertEqual([key for key in self.lock_keys() if "akmods" in key], [])
        self.assertIn("AKMODS_UPSTREAM_REF", defaults())

    def test_the_kernel_set_is_not_in_the_lock_file(self) -> None:
        self.assertIn("does not record the kernel set", self.paragraph())
        self.assertEqual([key for key in self.lock_keys() if "kernel" in key], [])

    def test_the_brew_fallback_names_a_real_default(self) -> None:
        self.assertIn("empty falls back to `DEFAULT_BREW_IMAGE` in `ci/defaults.json`", self.paragraph())
        self.assertIn("DEFAULT_BREW_IMAGE", defaults())


class ChangingTheBaseImageTests(unittest.TestCase):
    """The fork checklist has to name every place the base image lives, and nothing else."""

    def items(self) -> dict[int, str]:
        return numbered_items(section("## Changing The Base Image"))

    def test_every_defaults_key_carrying_the_base_image_is_listed(self) -> None:
        base = defaults()["DEFAULT_BASE_IMAGE"]
        carrying = {key for key, value in defaults().items() if value == base}
        # Guard the guard: at least DEFAULT_BASE_IMAGE itself.
        self.assertIn("DEFAULT_BASE_IMAGE", carrying)
        named = set(BACKTICKED_RE.findall(self.items()[1]))
        self.assertEqual(sorted(carrying - named), [], "keys a forker would leave on the old base")

    def test_every_key_item_1_names_is_a_defaults_key(self) -> None:
        named = {token for token in BACKTICKED_RE.findall(self.items()[1]) if token.isupper()}
        self.assertEqual(sorted(named - set(defaults())), [])

    def test_item_2_is_the_containerfile_fallback_and_it_matches_defaults(self) -> None:
        self.assertIn("update the fallback `ARG BASE_IMAGE`", self.items()[2])
        self.assertEqual(containerfile_args()["BASE_IMAGE"], defaults()["DEFAULT_BASE_IMAGE"])

    def listed_files(self) -> set[str]:
        """Repository-relative paths of every file the list links, plus this page for item 3."""

        found = set()
        for text in self.items().values():
            for target in LINK_RE.findall(text):
                found.add((DOC.parent / target).resolve().relative_to(REPO_ROOT).as_posix())
        self.assertIn("this page's `podman build` example", self.items()[3])
        found.add(DOC.relative_to(REPO_ROOT).as_posix())
        return found

    def test_every_listed_file_carries_the_base_image(self) -> None:
        # README.md was listed as holding "example `BASE_IMAGE` arguments" it did not have.
        base = defaults()["DEFAULT_BASE_IMAGE"]
        empty = sorted(
            path for path in self.listed_files() if base not in (REPO_ROOT / path).read_text(encoding="utf-8")
        )
        self.assertEqual(empty, [])

    def test_every_file_carrying_the_base_image_is_listed(self) -> None:
        carrying = repo_files_containing(defaults()["DEFAULT_BASE_IMAGE"])
        self.assertIn("ci/defaults.json", carrying)
        self.assertEqual(sorted(carrying - self.listed_files()), [], "files a forker would miss")

    def test_the_replay_note_names_the_lock_files_base_image_key(self) -> None:
        tail = normalized(prose(section("## Changing The Base Image"))).split("If you use workflow replay")
        self.assertEqual(len(tail), 2)
        self.assertIn("use_input_lock=true", tail[1])
        self.assertIn("base_image", json.loads(LOCK.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
