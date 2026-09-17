"""
Script: tests/test_licensing_doc.py
What: Joins docs/licensing.md to the licence files, the README and CONTRIBUTING licence
statements, the install planner, the build inputs and the publish path whose facts it
restates by hand.
Doing: Extracts the page's own claims -- the licence it says the repository carries, the
package it says the build produces, the kernel it says that package is built against, the
"baked into a published container image" claim and the list of things it says the licence
does not relicense -- and recomputes each one from the tree: the licence identifier from
`LICENSE` itself, the package name by executing the real install planner, the kernel and
the not-relicensed names from ci/defaults.json and the Containerfile, the publish claim
from the Containerfile's RUN and the action's `podman push`.
Why: This is the page README.md sends a reader to before redistributing the image or
basing a downstream image on it, and no test at any tier opened it: `grep -rl licensing
tests/` returned nothing. tests/test_docs_consistency.py resolves its links and confirms
it is in the documentation map, which leaves every claim that is not a link unchecked, and
the coverage flags in .github/workflows/test.yml name four Python paths and no Markdown,
so no tier could notice. Five committed sentences were falsified one at a time (GPL-3.0 ->
MIT, kmod-zfs -> kmod-openzfs, `ublue-os/brew` -> `homebrew/core`, Fedora kernel -> OpenBSD
kernel, CDDL -> MPL) and the suite stayed green five times out of five.
Goal: Make the page fail here when the tree moves under it, in both directions.

Parses Markdown by hand. CI installs pytest, pytest-cov and ruff and nothing else (see
.github/workflows/test.yml), so a third-party Markdown parser here would skip in exactly the
place these assertions are meant to run. `_section`, `_paragraphs`, `_prose`, `_links`,
`_defaults_scalar` and `_containerfile_arg` are the whole parser and carry their own case
table in `ParserTests` below, because a hand-rolled parser that is never wrong about a
fixture is the only thing keeping the assertions built on it honest.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "licensing.md"
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
DOC_GUIDE = REPO_ROOT / "docs" / "documentation-guide.md"
RISK_TIERS = REPO_ROOT / "docs" / "risk-tiers.md"
LICENSE_FILE = REPO_ROOT / "LICENSE"
APACHE_FILE = REPO_ROOT / "LICENSE.APACHE-2.0"
DEFAULTS_FILE = REPO_ROOT / "ci" / "defaults.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD_IMAGE_SCRIPT = REPO_ROOT / "build_files" / "build-image.sh"
PUBLISH_ACTION = REPO_ROOT / ".github" / "actions" / "publish-native-image" / "action.yml"
INSTALL_MODULE = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
ZFS_RELEASE_MODULE = REPO_ROOT / "ci_tools" / "zfs_release.py"
KERNEL_RELEASE_MODULE = REPO_ROOT / "shared" / "kernel_release.py"

# The workflows whose `paths-ignore` is what makes editing this page Tier 0 in
# docs/risk-tiers.md: a documentation-only change starts no build at all.
BUILD_WORKFLOWS = (
    REPO_ROOT / ".github" / "workflows" / "build.yml",
    REPO_ROOT / ".github" / "workflows" / "build-pr.yml",
    REPO_ROOT / ".github" / "workflows" / "build-branch.yml",
)

# The pages that send a reader here. The note is only reachable because these link it, and
# both describe it as the CDDL/GPLv2 position -- a rename or a reframing that left them
# behind would strand the page this module exists to keep honest.
ENTRY_POINTS = (README, DOC_GUIDE)


def _load_install_module():
    """
    Load the installer from its file path: `containerfiles/zfs-akmods` is not an importable
    package name. Same idiom as tests/test_install_zfs_from_akmods_cache.py, under its own
    module name so the two test files never share one module object.
    """

    spec = importlib.util.spec_from_file_location("install_zfs_from_akmods_cache_licensing_doc", INSTALL_MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_install_plan = _load_install_module().build_install_plan

DOC_TEXT = DOC.read_text(encoding="utf-8")
README_TEXT = README.read_text(encoding="utf-8")
CONTRIBUTING_TEXT = CONTRIBUTING.read_text(encoding="utf-8")
DEFAULTS = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))

# Every heading on the page. Asserted exhaustive in SectionInventoryTests so a new section
# cannot arrive unread.
DOC_HEADINGS = ("# Licensing Note", "## Purpose")

# The identifier the page, the README badge, the README section and CONTRIBUTING all name.
# Recomputed from `LICENSE` in LicenceFilesTests -- this constant is what the recomputation
# is compared against, and every other file is then compared against the same one value
# rather than against each other.
GPL_SHORT = "GPL-3.0"
GPL_LONG = "GNU General Public License v3.0"

# The name the page says the build produces. Verified by executing the install planner: a
# lookup answering with this name must be selected as the kernel module, and one answering
# with anything else must not.
KMOD_NAME = "kmod-zfs"

# The Fedora-owned registry namespace the base image must resolve to for "compiled against a
# Fedora kernel" to be a statement about this build.
FEDORA_REGISTRY_PREFIX = "quay.io/fedora-ostree-desktops/"

# Each name the page says the licence does not relicense, against the build input that
# consumes it. A defaults key maps to the name(s) its value is an artifact of; the two
# non-registry inputs map to the module that fetches or selects them. Asserted exhaustive in
# both directions in NotRelicensedTests: a name on the page that this table cannot place is
# a dependency the build does not consume, and an input the table does not place is a
# third-party artifact the page does not account for.
INPUT_TABLE: dict[str, tuple[str, ...]] = {
    "DEFAULT_BASE_IMAGE": ("Fedora", "Kinoite"),
    "STABLE_SIGNAL_IMAGE": ("Fedora", "Kinoite"),
    "DEFAULT_BUILD_CONTAINER_IMAGE": ("Universal Blue components",),
    "DEFAULT_BREW_IMAGE": ("`ublue-os/brew`",),
    # Danathar/akmods is the repository's fork of ublue-os/akmods (see
    # docs/akmods-fork-maintenance.md); what it consumes is Universal Blue's.
    "AKMODS_UPSTREAM_REPO": ("Universal Blue components",),
    "ci_tools/zfs_release.py": ("OpenZFS",),
    "shared/kernel_release.py": ("the Linux kernel",),
}

# What each registry-shaped defaults value must point at for its table row to be true.
INPUT_HOSTS = {
    "DEFAULT_BASE_IMAGE": "quay.io/fedora-ostree-desktops/kinoite",
    "STABLE_SIGNAL_IMAGE": "quay.io/fedora-ostree-desktops/kinoite",
    "DEFAULT_BUILD_CONTAINER_IMAGE": "ghcr.io/ublue-os/devcontainer",
    "DEFAULT_BREW_IMAGE": "ghcr.io/ublue-os/brew",
    "AKMODS_UPSTREAM_REPO": "https://github.com/Danathar/akmods.git",
}


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
        raise AssertionError(f"no heading {heading!r}") from exc

    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        body.append(line)

    if not any(line.strip() for line in body):
        raise AssertionError(f"section {heading!r} is empty")
    return body


def _headings(text: str) -> list[str]:
    """Every ATX heading line in `text`, in document order, markers included."""

    return [line for line in text.splitlines() if re.match(r"^#{1,6} \S", line)]


def _paragraphs(lines: list[str]) -> list[str]:
    """
    Return the blank-line-separated paragraphs of `lines`, each collapsed to one string.

    The page hard-wraps some paragraphs and not others, so a claim is routinely split across
    a newline ("does not\\nrelicense"). Collapsing each paragraph means an assertion pins a
    sentence, not a line break. Heading lines are not paragraphs.
    """

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if current:
                paragraphs.append(_prose(current))
                current = []
            continue
        current.append(stripped)
    if current:
        paragraphs.append(_prose(current))
    return paragraphs


def _prose(lines: list[str]) -> str:
    """Return `lines` as one whitespace-collapsed string."""

    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _links(text: str) -> list[tuple[str, str]]:
    """
    Return every inline Markdown link in `text` as (label, target), in order.

    A badge is an image inside a link, `[![alt](img)](target)`; the outer link is what is
    returned for it, with the image markup as its label, so the badge's target is read from
    the same place as every other link's. Reference-style links are not used in the files
    read here and are not parsed.
    """

    found: list[tuple[str, str]] = []
    for match in re.finditer(r"\[(!\[[^\]]*\]\([^)]*\)|[^\]]*)\]\(([^)\s]+)\)", text):
        found.append((match.group(1), match.group(2)))
    return found


def _defaults_scalar(name: str) -> str:
    """The string value of one ci/defaults.json key; a missing or non-string key fails here."""

    value = DEFAULTS.get(name)
    if isinstance(value, str):
        return value
    raise AssertionError(f"ci/defaults.json has no string key {name!r}")


def _containerfile_arg(text: str, name: str) -> str:
    """
    Return the default of `ARG name="..."` in a Containerfile, unquoted.

    Only a top-of-line ARG with a double-quoted default is a default this file reads; an
    ARG with no default, or a mention of the name in a comment, is not one. Raises when the
    ARG is absent so a renamed build argument fails here rather than matching "".
    """

    match = re.search(rf'^ARG {re.escape(name)}="([^"]*)"\s*$', text, flags=re.MULTILINE)
    if match is None:
        raise AssertionError(f"Containerfile has no `ARG {name}=\"...\"` default")
    return match.group(1)


def _paths_ignore(workflow_text: str) -> list[str]:
    """
    Return the entries of the first `paths-ignore:` list in a workflow, unquoted.

    The list is the sequence of `- item` lines immediately under the key, at any deeper
    indentation; the first line that is not one ends it. Raises when there is no key or the
    list is empty, because "ignores nothing" must not read as "ignores everything asked".
    """

    lines = workflow_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "paths-ignore:"]
    if not starts:
        raise AssertionError("workflow has no `paths-ignore:` key")
    indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
    entries: list[str] = []
    for line in lines[starts[0] + 1 :]:
        stripped = line.strip()
        item_indent = len(line) - len(line.lstrip())
        if not stripped.startswith("- ") or item_indent <= indent:
            break
        entries.append(stripped[2:].strip().strip('"').strip("'"))
    if not entries:
        raise AssertionError("`paths-ignore:` list is empty")
    return entries


def _tracked(path: Path) -> bool:
    """Whether git tracks `path`: a licence file that exists but is not committed is not shipped."""

    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path.relative_to(REPO_ROOT))],
        capture_output=True,
        text=True,
        check=False,
    )
    return listing.returncode == 0


def _licence_identifier(licence_text: str) -> tuple[str, str]:
    """
    Recompute (short, long) licence names from the header of a licence text.

    Reads the first two non-blank lines -- the title and the version line -- and refuses
    anything but the two texts this repository ships, so a swapped-in licence fails here
    with its actual header in the message rather than being mislabelled.
    """

    header = [line.strip() for line in licence_text.splitlines() if line.strip()][:2]
    if header == ["GNU GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"]:
        return ("GPL-3.0", "GNU General Public License v3.0")
    if header[:1] == ["Apache License"] and header[1:2] and header[1].startswith("Version 2.0"):
        return ("Apache-2.0", "Apache License 2.0")
    raise AssertionError(f"unrecognised licence header {header!r}")


class SectionInventoryTests(unittest.TestCase):
    def test_every_heading_on_the_page_is_one_this_file_reads(self) -> None:
        self.assertEqual(tuple(_headings(DOC_TEXT)), DOC_HEADINGS)

    def test_the_page_is_in_git(self) -> None:
        self.assertTrue(_tracked(DOC))


class LicenceFilesTests(unittest.TestCase):
    """`LICENSE` really is the GPL v3 text, `LICENSE.APACHE-2.0` really is Apache 2.0."""

    def test_both_licence_files_are_tracked(self) -> None:
        for path in (LICENSE_FILE, APACHE_FILE):
            with self.subTest(path=path.name):
                self.assertTrue(_tracked(path), f"{path.name} is not tracked by git")

    def test_license_is_the_gnu_gpl_v3_text(self) -> None:
        self.assertEqual(_licence_identifier(LICENSE_FILE.read_text(encoding="utf-8")), (GPL_SHORT, GPL_LONG))

    def test_license_apache_is_the_apache_2_text(self) -> None:
        short, _ = _licence_identifier(APACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(short, "Apache-2.0")

    def test_the_two_licence_files_are_not_the_same_text(self) -> None:
        self.assertNotEqual(
            LICENSE_FILE.read_bytes(),
            APACHE_FILE.read_bytes(),
            "LICENSE and LICENSE.APACHE-2.0 carry identical text",
        )


class LicenceNameTests(unittest.TestCase):
    """One identifier, recomputed from `LICENSE`, named the same way on every surface."""

    def test_the_page_names_the_licence_the_license_file_carries(self) -> None:
        purpose = _prose(_section(DOC_TEXT, "## Purpose"))
        self.assertIn(f"The repository's own {GPL_SHORT} licensing", purpose)
        # No other licence identifier may stand in for the repository's own.
        for wrong in ("MIT", "Apache-2.0 licensing", "GPL-2.0 licensing", "BSD"):
            self.assertNotIn(wrong, purpose)

    def test_the_readme_badge_names_the_same_licence(self) -> None:
        # shields.io escapes a literal dash as `--`, so the badge carries `GPL--3.0`.
        badge_label = GPL_SHORT.replace("-", "--")
        badges = [label for label, _ in _links(README_TEXT) if label.startswith("![License:")]
        self.assertEqual(len(badges), 1, "README must carry exactly one licence badge")
        self.assertIn(f"badge/license-{badge_label}-", badges[0])
        self.assertIn(f"[License: {GPL_SHORT}]", badges[0])

    def test_the_readme_license_section_names_the_same_licence(self) -> None:
        section = _prose(_section(README_TEXT, "## License"))
        self.assertIn(f"distributed under the {GPL_LONG}.", section)

    def test_contributing_names_the_same_licence(self) -> None:
        self.assertIn(f"[{GPL_LONG}](./LICENSE)", CONTRIBUTING_TEXT)
        self.assertIn(f"distributed under {GPL_SHORT}.", _prose(CONTRIBUTING_TEXT.splitlines()))


class LicenceLinkTests(unittest.TestCase):
    """The licence links point at the licence files they name, and Apache is accounted for."""

    def test_the_readme_badge_links_to_the_license_file(self) -> None:
        targets = [target for label, target in _links(README_TEXT) if label.startswith("![License:")]
        self.assertEqual(targets, [LICENSE_FILE.name])
        self.assertTrue((README.parent / targets[0]).is_file())

    def test_the_readme_license_section_links_both_licence_files(self) -> None:
        section_links = dict(_links("\n".join(_section(README_TEXT, "## License"))))
        self.assertEqual(section_links.get(f"`{LICENSE_FILE.name}`"), LICENSE_FILE.name)
        self.assertEqual(section_links.get(f"`{APACHE_FILE.name}`"), APACHE_FILE.name)
        for target in section_links.values():
            self.assertTrue((README.parent / target).is_file(), f"README links {target} which does not exist")

    def test_the_readme_license_section_explains_why_the_apache_file_exists(self) -> None:
        section = _prose(_section(README_TEXT, "## License"))
        self.assertIn("Material previously distributed under the Apache License 2.0", section)
        self.assertIn(f"See [`{APACHE_FILE.name}`]({APACHE_FILE.name}).", section)

    def test_contributing_links_the_license_file(self) -> None:
        targets = [target for _, target in _links(CONTRIBUTING_TEXT) if target.endswith("LICENSE")]
        self.assertEqual(targets, ["./LICENSE"])
        self.assertTrue((CONTRIBUTING.parent / "LICENSE").is_file())
        # CONTRIBUTING's own Apache sentence points at the README section that explains it.
        self.assertIn("(./README.md#license)", CONTRIBUTING_TEXT)
        self.assertIn("## License", _headings(README_TEXT))


class ArtifactClaimTests(unittest.TestCase):
    """The binary the page describes is the binary this build produces and publishes."""

    def _binary_sentence(self) -> str:
        for paragraph in _paragraphs(_section(DOC_TEXT, "## Purpose")):
            if "This repository produces exactly such a binary" in paragraph:
                return paragraph
        raise AssertionError("the page no longer says what binary the repository produces")

    def test_the_package_name_is_the_one_the_install_planner_selects(self) -> None:
        sentence = self._binary_sentence()
        self.assertIn(f"a `{KMOD_NAME}` package compiled against a Fedora kernel", sentence)

        # Execute the real planner: the name on the page must be the one it picks out as the
        # kernel module, and a lookup answering with any other name must leave it with no
        # module to install. This reads the planner's decision, not the module's text.
        rpm = Path("/cache/x.rpm")
        plan = build_install_plan(
            ["6.18.0-100.fc44.x86_64"],
            [rpm],
            rpm_name_lookup=lambda _path: KMOD_NAME,
            kernel_release_lookup=lambda _path: "6.18.0-100.fc44.x86_64",
        )
        self.assertEqual(plan.supported_kmod_rpm, rpm)
        with self.assertRaises(RuntimeError):
            build_install_plan(
                ["6.18.0-100.fc44.x86_64"],
                [rpm],
                rpm_name_lookup=lambda _path: KMOD_NAME + "-other",
                kernel_release_lookup=lambda _path: "6.18.0-100.fc44.x86_64",
            )
        self.assertTrue(_tracked(INSTALL_MODULE))

    def test_fedora_kernel_is_the_kernel_of_the_base_image_the_build_resolves(self) -> None:
        defaults_base = _defaults_scalar("DEFAULT_BASE_IMAGE")
        containerfile_base = _containerfile_arg(CONTAINERFILE.read_text(encoding="utf-8"), "BASE_IMAGE")
        self.assertEqual(defaults_base, containerfile_base, "ci/defaults.json and the Containerfile disagree on the base image")
        self.assertTrue(
            defaults_base.startswith(FEDORA_REGISTRY_PREFIX),
            f"base image {defaults_base!r} is not Fedora-owned; the page says the module is built against a Fedora kernel",
        )
        # The kernel the module is selected for is read out of that image, by the shared
        # helper the page's "Fedora kernel" ultimately means.
        self.assertIn("Fedora kernel release strings", KERNEL_RELEASE_MODULE.read_text(encoding="utf-8"))

    def test_baked_into_a_published_container_image_is_what_the_build_does(self) -> None:
        sentence = self._binary_sentence()
        self.assertIn("baked into a published container image", sentence)

        # Baked: the Containerfile's RUN executes build-image.sh, and build-image.sh executes
        # the installer -- the module lands in the image's filesystem, not in a sidecar.
        containerfile = CONTAINERFILE.read_text(encoding="utf-8")
        self.assertRegex(containerfile, r"(?m)^RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\\n(?:    --mount=[^\n]*\\\n)*    /ctx/build-image\.sh$")
        self.assertRegex(
            BUILD_IMAGE_SCRIPT.read_text(encoding="utf-8"),
            r"(?m)^python3 /ctx/containerfiles/zfs-akmods/install_zfs_from_akmods_cache\.py",
        )
        # Published: the action pushes the tag it built. A comment mentioning `podman push`
        # is not a push; only an unindented-by-comment command line counts.
        push_lines = [
            line
            for line in PUBLISH_ACTION.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("podman push")
        ]
        self.assertTrue(push_lines, "publish-native-image no longer runs `podman push`")


class NotRelicensedTests(unittest.TestCase):
    """Every name on the not-relicensed list is a real build input, and vice versa."""

    def _sentence(self) -> str:
        for paragraph in _paragraphs(_section(DOC_TEXT, "## Purpose")):
            if "relicense" in paragraph:
                return paragraph
        raise AssertionError("the page no longer carries the not-relicensed sentence")

    def _listed_names(self) -> list[str]:
        match = re.search(r"It does not relicense (.+?), or any other third-party", self._sentence())
        if match is None:
            raise AssertionError("cannot find the not-relicensed list in the sentence")
        return match.group(1).split(", ")

    def test_the_negation_survives(self) -> None:
        sentence = self._sentence()
        self.assertIn("does not relicense", sentence)
        self.assertNotRegex(sentence, r"\b(?<!not )relicenses\b")
        self.assertNotIn("does relicense", sentence)
        self.assertIn("it neither resolves nor changes the CDDL/GPLv2 kernel-module redistribution question", sentence)

    def test_every_listed_name_resolves_to_a_build_input(self) -> None:
        placed = {name for names in INPUT_TABLE.values() for name in names}
        for name in self._listed_names():
            with self.subTest(name=name):
                self.assertIn(name, placed, f"{name!r} is on the page but no build input consumes it")

    def test_every_build_input_is_on_the_list(self) -> None:
        listed = set(self._listed_names())
        for key, names in INPUT_TABLE.items():
            for name in names:
                with self.subTest(input=key, name=name):
                    self.assertIn(name, listed, f"the build consumes {key} but the page does not name {name!r}")

    def test_the_input_table_is_exhaustive_over_the_defaults_file(self) -> None:
        # A defaults value that names a registry or a git remote is a third-party artifact the
        # build pulls. Every such key must be placed; a new dependency key fails here.
        external = {
            key for key, value in DEFAULTS.items()
            if isinstance(value, str) and re.match(r"^(https?://|[a-z0-9.-]+\.[a-z]{2,}/)", value)
        }
        self.assertEqual(external, {key for key in INPUT_TABLE if key in DEFAULTS})

    def test_each_registry_input_points_where_its_row_says(self) -> None:
        for key, prefix in INPUT_HOSTS.items():
            with self.subTest(key=key):
                self.assertTrue(
                    _defaults_scalar(key).startswith(prefix),
                    f"{key}={_defaults_scalar(key)!r} no longer points at {prefix!r}",
                )

    def test_the_non_registry_inputs_are_the_modules_the_table_names(self) -> None:
        self.assertIn(
            'OPENZFS_RELEASES_URL = "https://api.github.com/repos/openzfs/zfs/releases"',
            ZFS_RELEASE_MODULE.read_text(encoding="utf-8"),
        )
        self.assertTrue(_tracked(KERNEL_RELEASE_MODULE))
        self.assertTrue(_tracked(ZFS_RELEASE_MODULE))

    def test_brew_is_also_the_containerfile_brew_stage(self) -> None:
        # The page names `ublue-os/brew` by repository; the Containerfile's local default and
        # the CI digest pin must both be that repository's image.
        brew_default = _containerfile_arg(CONTAINERFILE.read_text(encoding="utf-8"), "BREW_IMAGE")
        self.assertTrue(brew_default.startswith("ghcr.io/ublue-os/brew"))
        self.assertTrue(_defaults_scalar("DEFAULT_BREW_IMAGE").startswith("ghcr.io/ublue-os/brew@sha256:"))


class EntryPointTests(unittest.TestCase):
    """Terms are defined on first use, and the pages that send a reader here still agree."""

    def test_cddl_and_gplv2_are_defined_on_first_use(self) -> None:
        purpose = _prose(_section(DOC_TEXT, "## Purpose"))
        for definition, acronym in (
            ("Common Development and Distribution License (CDDL)", "CDDL"),
            ("version 2 of the GNU General Public License (GPLv2)", "GPLv2"),
        ):
            with self.subTest(acronym=acronym):
                self.assertIn(definition, purpose)
                # The acronym's first occurrence on the page is the one inside its definition.
                self.assertEqual(purpose.index(acronym), purpose.index(definition) + definition.index(acronym))

    def test_the_page_defers_to_the_openzfs_faq(self) -> None:
        targets = [target for label, target in _links(DOC_TEXT) if "OpenZFS FAQ" in label]
        self.assertEqual(targets, ["https://openzfs.github.io/openzfs-docs/Project%20and%20Community/FAQ.html#licensing"])

    def test_every_entry_point_links_the_page_as_the_cddl_gplv2_position(self) -> None:
        for page in ENTRY_POINTS:
            text = page.read_text(encoding="utf-8")
            with self.subTest(page=page.name):
                targets = {target for _, target in _links(text)}
                self.assertTrue(
                    any(target.endswith("licensing.md") for target in targets),
                    f"{page.name} no longer links docs/licensing.md",
                )
                self.assertIn("CDDL/GPLv2", text)


class RiskTierTests(unittest.TestCase):
    """Editing this page starts no build, which is what makes it Tier 0."""

    def test_every_build_workflow_ignores_markdown_and_docs(self) -> None:
        for workflow in BUILD_WORKFLOWS:
            ignored = _paths_ignore(workflow.read_text(encoding="utf-8"))
            with self.subTest(workflow=workflow.name):
                self.assertIn("**/*.md", ignored)
                self.assertIn("docs/**", ignored)

    def test_build_yml_additionally_ignores_the_license_file(self) -> None:
        ignored = _paths_ignore(BUILD_WORKFLOWS[0].read_text(encoding="utf-8"))
        self.assertIn(LICENSE_FILE.name, ignored)

    def test_risk_tiers_records_the_same_ignore_rule(self) -> None:
        tier0 = _prose(_section(RISK_TIERS.read_text(encoding="utf-8"), "### Tier 0 — cannot affect anything that runs"))
        self.assertIn("`LICENSE`", tier0)
        for workflow in BUILD_WORKFLOWS:
            self.assertIn(f"`{workflow.name}`", tier0)
        self.assertIn("`paths-ignore` for `**/*.md` and `docs/**`", tier0)


class ParserTests(unittest.TestCase):
    """The case table for the hand-rolled parser every assertion above is built on."""

    FIXTURE = textwrap.dedent(
        """\
        # Title

        ## Purpose

        First paragraph, split
        across a line.

        Second [`LICENSE`](LICENSE) paragraph with a
        [![License: X](https://img.shields.io/badge/license-X-blue)](LICENSE) badge.

        ### Purpose
        Not the same section.

        ## Other

        Other body.
        """
    )

    def test_section_stops_at_the_next_heading_of_the_same_level(self) -> None:
        body = _section(self.FIXTURE, "## Purpose")
        self.assertIn("### Purpose", body)
        self.assertNotIn("Other body.", body)

    def test_section_does_not_match_a_deeper_heading_with_the_same_words(self) -> None:
        self.assertEqual(_section(self.FIXTURE, "### Purpose"), ["Not the same section.", ""])

    def test_section_raises_on_a_missing_or_empty_heading(self) -> None:
        with self.assertRaises(AssertionError):
            _section(self.FIXTURE, "## Missing")
        with self.assertRaises(AssertionError):
            _section("## Empty\n\n\n", "## Empty")

    def test_headings_lists_every_heading_in_order(self) -> None:
        self.assertEqual(_headings(self.FIXTURE), ["# Title", "## Purpose", "### Purpose", "## Other"])

    def test_paragraphs_collapse_wrapped_lines_and_skip_headings(self) -> None:
        paragraphs = _paragraphs(_section(self.FIXTURE, "## Purpose"))
        self.assertEqual(paragraphs[0], "First paragraph, split across a line.")
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual(paragraphs[2], "Not the same section.")

    def test_links_returns_plain_links_and_badges_by_outer_target(self) -> None:
        links = _links(self.FIXTURE)
        self.assertIn(("`LICENSE`", "LICENSE"), links)
        self.assertIn(("![License: X](https://img.shields.io/badge/license-X-blue)", "LICENSE"), links)
        self.assertEqual(len(links), 2)

    def test_containerfile_arg_reads_only_a_quoted_top_of_line_default(self) -> None:
        text = '# ARG BASE_IMAGE="commented"\nARG BASE_IMAGE="quay.io/x/y:1"\nARG EMPTY=""\nARG BARE\n'
        self.assertEqual(_containerfile_arg(text, "BASE_IMAGE"), "quay.io/x/y:1")
        self.assertEqual(_containerfile_arg(text, "EMPTY"), "")
        with self.assertRaises(AssertionError):
            _containerfile_arg(text, "BARE")

    def test_paths_ignore_reads_the_list_and_stops_at_the_next_key(self) -> None:
        text = "on:\n  push:\n    paths-ignore:\n      - README.md\n      - \"**/*.md\"\n      - 'docs/**'\n  workflow_dispatch:\n"
        self.assertEqual(_paths_ignore(text), ["README.md", "**/*.md", "docs/**"])
        with self.assertRaises(AssertionError):
            _paths_ignore("on:\n  push:\n    branches: [main]\n")
        with self.assertRaises(AssertionError):
            _paths_ignore("on:\n  push:\n    paths-ignore:\n  workflow_dispatch:\n")

    def test_licence_identifier_recognises_both_texts_and_nothing_else(self) -> None:
        self.assertEqual(
            _licence_identifier("\n   GNU GENERAL PUBLIC LICENSE\n   Version 3, 29 June 2007\n"),
            ("GPL-3.0", "GNU General Public License v3.0"),
        )
        self.assertEqual(
            _licence_identifier("  Apache License\n  Version 2.0, January 2004\n")[0],
            "Apache-2.0",
        )
        with self.assertRaises(AssertionError):
            _licence_identifier("MIT License\n\nPermission is hereby granted")

    def test_defaults_scalar_refuses_a_missing_key(self) -> None:
        with self.assertRaises(AssertionError):
            _defaults_scalar("NO_SUCH_KEY")


if __name__ == "__main__":
    unittest.main()
