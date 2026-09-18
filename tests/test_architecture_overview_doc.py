"""
Script: tests/test_architecture_overview_doc.py
What: Holds docs/architecture-overview.md to the machine it describes.
Doing: Extracts the document's tables, numbered step lists and inline literals, then joins each one to the workflow, action, helper or defaults file it restates -- executing the real scheduled-build gate for every row of its reason table.
Why: This document is where a reader learns why the pipeline is shaped this way, so a stale claim here is read as authoritative; two were already stale when this file was written.
Goal: Make a change to the build machine that contradicts the overview fail in CI rather than in review, or not at all.

The document is 644 lines of prose about code that lives elsewhere. Every
number, tag shape, label name, reason code and step list in it is a hand copy,
and the two tests that read the file before this one covered three claims
between them: tests/test_renovate_config.py's `DocumentationJoinTests` (the
named Chunkah version, the dependencies it says Renovate owns, the pin it says
must stay unannotated) and tests/test_docs_consistency.py (link and anchor
resolution over every tracked markdown file, never content). Those three stay
where they are; this file does not restate them.

Two claims were false when this was written, both fixed in the same change:

  * "It does four important things:" headed a five-item list. `test_the_
    containerfile_list_count_word_matches_its_length` is the general form of
    that defect, so the next stale count fails here instead of misleading a
    reader.
  * The buildah section named `oci: false`, an input of
    `redhat-actions/buildah-build`. This repository calls buildah directly
    with `--format docker`, so nothing in the tree carried the name the doc
    told a reader to look for.

Standard library only, and no PyYAML, for the reason
tests/test_docs_consistency.py gives: the CI job installs pytest, pytest-cov
and ruff, so a third-party parser would depend on the runner image and skip
silently the day that changed. The workflow and action assertions are
therefore text assertions over the YAML, which is what the other workflow
tests in this tree do.
"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import patch

from ci_tools.check_stable_signal import (
    STABLE_SIGNAL_DIGEST_LABEL,
    STABLE_SIGNAL_IMAGE_LABEL,
    ZFS_VERSION_LABEL,
    _bypass_decision,
    evaluate_stable_signal_gate,
)
from ci_tools.common import CiToolError
from ci_tools.pin_akmods_cache import akmods_cache_image_tag, pin_akmods_cache_image
from ci_tools.promote_stable import main as promote_main
from ci_tools.tagging_context import (
    build_branch_image_tag,
    build_branch_metadata,
    build_candidate_tag,
)
from tests.test_docs_consistency import REPO_ROOT

DOC_PATH = REPO_ROOT / "docs" / "architecture-overview.md"
GATE_SOURCE = REPO_ROOT / "ci_tools" / "check_stable_signal.py"
DEFAULTS_PATH = REPO_ROOT / "ci" / "defaults.json"


def read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def defaults() -> dict:
    return json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))


def numbered_list_after(needle: str) -> list[str]:
    """
    Return the numbered list items that follow `needle` in the document.

    The document writes its step lists as `1. text`, one per line, wrapped at
    the column limit with continuation lines indented. Collection stops at the
    first line that is neither a new item nor a continuation of one, so the
    paragraph after a list is never swallowed into it.
    """

    text = doc()
    start = text.index(needle) + len(needle)
    items: list[str] = []
    for line in text[start:].splitlines():
        if not line.strip():
            if items:
                # A blank line inside a list ends it: the document separates
                # consecutive lists by one blank line and nothing else.
                break
            continue
        opener = re.match(r"^(\d+)\. (.*)$", line)
        if opener:
            self_number = int(opener.group(1))
            if self_number != len(items) + 1:
                raise AssertionError(f"list after {needle!r} is misnumbered at {line!r}")
            items.append(opener.group(2))
            continue
        if items and line.startswith(("   ", "\t")):
            items[-1] += " " + line.strip()
            continue
        break
    if not items:
        raise AssertionError(f"no numbered list follows {needle!r}")
    return items


def gate_table() -> list[tuple[str, str]]:
    """
    Return the (reason, builds) cells of the scheduled-build gate's table.

    `builds` is the raw cell text, not a boolean: the test that reads it is the
    one that decides what `yes` and `no` mean, so a third value in that column
    fails there instead of being coerced into one of the two.
    """

    rows: list[tuple[str, str]] = []
    in_table = False
    for line in doc().splitlines():
        if line.startswith("| Reason "):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if set("".join(cells)) <= {"-", ":"}:
            continue
        rows.append((cells[0].strip("`"), cells[-1]))
    return rows


SIGNAL_IMAGE = "quay.io/fedora-ostree-desktops/kinoite:44"
SIGNAL_REF = f"docker://{SIGNAL_IMAGE}"
LATEST_REF = "docker://ghcr.io/danathar/zfs-kinoite-complex:latest"
CURRENT_ZFS = "2.4.3"
CURRENT_DIGEST = "sha256:current"


def _latest_inspect(
    *,
    signal_image: str = SIGNAL_IMAGE,
    signal_digest: str = CURRENT_DIGEST,
    zfs_version: str = CURRENT_ZFS,
    labels: dict | None = None,
) -> dict:
    if labels is None:
        labels = {
            STABLE_SIGNAL_IMAGE_LABEL: signal_image,
            STABLE_SIGNAL_DIGEST_LABEL: signal_digest,
        }
        if zfs_version:
            labels[ZFS_VERSION_LABEL] = zfs_version
    return {"Digest": "sha256:repo-latest", "Labels": labels}


def run_gate(latest, *, signal_digest: str = CURRENT_DIGEST):
    """
    Run the real gate against one fake registry.

    `latest` is the inspect payload for this repo's own `:latest`, or an
    exception to raise for it. The upstream signal image always answers with
    `signal_digest`, so a scenario only has to say what is different about the
    promoted image it is compared against.
    """

    def inspect(image_ref: str, *, creds: str | None = None) -> dict:
        if image_ref == SIGNAL_REF:
            return {"Digest": signal_digest, "Labels": {}}
        if image_ref == LATEST_REF:
            if isinstance(latest, Exception):
                raise latest
            return latest
        raise AssertionError(f"unexpected inspect of {image_ref}")

    with (
        patch("ci_tools.check_stable_signal.skopeo_inspect_json", side_effect=inspect),
        patch("ci_tools.common.skopeo_inspect_json", side_effect=inspect),
        patch(
            "ci_tools.check_stable_signal.resolve_latest_zfs_version",
            return_value=CURRENT_ZFS,
        ),
    ):
        return evaluate_stable_signal_gate(
            image_org="danathar",
            image_name="zfs-kinoite-complex",
            stable_signal_image=SIGNAL_IMAGE,
            zfs_minor_version="2.4",
            creds="actor:token",
        )


# One scenario per documented reason. The gate itself decides which reason each
# one produces; nothing here asserts a reason string, so a scenario cannot
# quietly agree with the document while the gate does something else.
GATE_SCENARIOS = {
    "stable-signal-unchanged": lambda: run_gate(_latest_inspect()),
    "stable-signal-advanced": lambda: run_gate(_latest_inspect(signal_digest="sha256:older")),
    "zfs-version-advanced": lambda: run_gate(_latest_inspect(zfs_version="2.4.2")),
    "stable-signal-image-changed": lambda: run_gate(
        _latest_inspect(signal_image="quay.io/fedora-ostree-desktops/kinoite:43")
    ),
    "current-latest-missing": lambda: run_gate(CiToolError("manifest unknown")),
    "current-latest-missing-stable-signal-labels": lambda: run_gate(_latest_inspect(labels={})),
    "current-latest-missing-zfs-version-label": lambda: run_gate(_latest_inspect(zfs_version="")),
    "not-schedule-event": lambda: _bypass_decision_offline(),
}


def _bypass_decision_offline():
    """The push/manual path, with its best-effort digest lookup failing."""

    with patch(
        "ci_tools.check_stable_signal.skopeo_inspect_json_optional",
        side_effect=CiToolError("manifest unknown"),
    ):
        return _bypass_decision(SIGNAL_IMAGE)


class ScheduledBuildGateTableTests(unittest.TestCase):
    """Section "0. Scheduled-Build Gate" tabulates ci_tools/check_stable_signal.py."""

    def test_the_table_scan_finds_every_row(self) -> None:
        # Guard the guard: every assertion below iterates these rows, and an
        # empty list passes a for-loop silently.
        rows = gate_table()
        self.assertEqual(len(rows), 8, rows)
        self.assertEqual(len({reason for reason, _ in rows}), len(rows))

    def test_the_documented_reasons_are_exactly_the_gate_source_reasons(self) -> None:
        documented = {reason for reason, _ in gate_table()}
        implemented = set(re.findall(r'reason="([a-z0-9-]+)"', GATE_SOURCE.read_text("utf-8")))
        self.assertEqual(documented, implemented)

    def test_every_documented_reason_has_a_scenario(self) -> None:
        self.assertEqual({reason for reason, _ in gate_table()}, set(GATE_SCENARIOS))

    def test_the_gate_produces_each_documented_reason_and_outcome(self) -> None:
        for reason, builds in gate_table():
            with self.subTest(reason=reason):
                self.assertIn(builds, {"yes", "no"}, "Builds? column is yes/no")
                decision = GATE_SCENARIOS[reason]()
                self.assertEqual(decision.reason, reason)
                self.assertEqual(decision.should_build, builds == "yes")

    def test_exactly_one_documented_reason_skips_the_build(self) -> None:
        # The section's whole claim is that a scheduled run skips only when
        # nothing moved. A second `no` row would be a different gate.
        skipping = [reason for reason, builds in gate_table() if builds == "no"]
        self.assertEqual(skipping, ["stable-signal-unchanged"])

    def test_the_gate_fails_closed_on_unknown_state(self) -> None:
        """The section says "we couldn't tell" must never read as "nothing changed"."""
        self.assertIn('"we couldn\'t tell" must never be silently treated as', doc())
        # An upstream signal-image failure always raises, missing or not.
        for message in ("manifest unknown", "unauthorized: authentication required"):
            with self.subTest(signal_error=message), self.assertRaises(CiToolError):
                run_gate_with_signal_error(CiToolError(message))
        # The repo's own `:latest` raises for everything except a real absence.
        with self.assertRaises(CiToolError):
            run_gate(CiToolError("unauthorized: authentication required"))
        self.assertEqual(run_gate(CiToolError("manifest unknown")).should_build, True)

    def test_a_failed_zfs_release_lookup_raises_instead_of_skipping(self) -> None:
        """The same paragraph extends fail-closed to the OpenZFS releases API."""
        self.assertIn("same applies to the OpenZFS releases API call", doc())

        def inspect(image_ref: str, *, creds: str | None = None) -> dict:
            return {"Digest": CURRENT_DIGEST, "Labels": {}}

        with (
            patch("ci_tools.check_stable_signal.skopeo_inspect_json", side_effect=inspect),
            patch("ci_tools.common.skopeo_inspect_json", side_effect=inspect),
            patch(
                "ci_tools.check_stable_signal.resolve_latest_zfs_version",
                side_effect=CiToolError("releases API unreachable"),
            ),
            self.assertRaises(CiToolError),
        ):
            evaluate_stable_signal_gate(
                image_org="danathar",
                image_name="zfs-kinoite-complex",
                stable_signal_image=SIGNAL_IMAGE,
                zfs_minor_version="2.4",
                creds="actor:token",
            )


def run_gate_with_signal_error(error: Exception):
    """Fail the upstream signal-image inspect, which the doc says always raises."""

    def inspect(image_ref: str, *, creds: str | None = None) -> dict:
        if image_ref == SIGNAL_REF:
            raise error
        raise AssertionError(f"the gate reached {image_ref} after the signal image failed")

    with (
        patch("ci_tools.check_stable_signal.skopeo_inspect_json", side_effect=inspect),
        patch("ci_tools.common.skopeo_inspect_json", side_effect=inspect),
        patch(
            "ci_tools.check_stable_signal.resolve_latest_zfs_version",
            return_value=CURRENT_ZFS,
        ),
    ):
        return evaluate_stable_signal_gate(
            image_org="danathar",
            image_name="zfs-kinoite-complex",
            stable_signal_image=SIGNAL_IMAGE,
            zfs_minor_version="2.4",
            creds="actor:token",
        )


class ProvenanceLabelTests(unittest.TestCase):
    """The three labels the section names have to be the three the build writes."""

    DOCUMENTED = (
        ("org.zfs-kinoite-complex.stable-signal-image", STABLE_SIGNAL_IMAGE_LABEL),
        ("org.zfs-kinoite-complex.stable-signal-digest", STABLE_SIGNAL_DIGEST_LABEL),
        ("org.zfs-kinoite-complex.zfs-version", ZFS_VERSION_LABEL),
    )

    def test_the_document_names_all_three(self) -> None:
        text = doc()
        self.assertIn("every\nbuild writes three OCI labels onto the candidate", text)
        for name, _ in self.DOCUMENTED:
            with self.subTest(label=name):
                self.assertIn(f"- `{name}`:", text)

    def test_each_documented_label_is_the_constant_the_gate_reads(self) -> None:
        for name, constant in self.DOCUMENTED:
            with self.subTest(label=name):
                self.assertEqual(name, constant)

    def test_each_documented_label_is_written_onto_the_candidate(self) -> None:
        action = read(".github/actions/build-native-image/action.yml")
        for name, _ in self.DOCUMENTED:
            with self.subTest(label=name):
                self.assertRegex(action, rf'--label "{re.escape(name)}=\$\{{\w+\}}"')

    def test_the_zfs_version_label_is_not_sourced_from_the_gate(self) -> None:
        """The doc says the label comes from the akmods job's own resolution."""
        self.assertIn("It comes from the real\n`build-zfs-akmods` job's own resolution", doc())
        # The gate's decision carries a zfs_version, and the build does not read
        # it: nothing in build.yml passes the preflight output into the label.
        workflow = read(".github/workflows/build.yml")
        self.assertNotRegex(
            workflow,
            r"zfs_version:\s*\$\{\{\s*needs\.preflight\.outputs\.zfs_version",
        )
        self.assertRegex(workflow, r"zfs_version:\s*\$\{\{\s*needs\.build-zfs-akmods\.outputs\.")


class StableSignalDefaultsTests(unittest.TestCase):
    """The section pins two ci/defaults.json values and requires them to agree."""

    def test_the_documented_default_signal_image_is_the_configured_one(self) -> None:
        stated = re.search(
            r"The upstream `STABLE_SIGNAL_IMAGE` \(`([^`]+)` by\ndefault", doc()
        )
        self.assertIsNotNone(stated, "the overview stopped naming a default signal image")
        self.assertEqual(stated.group(1), defaults()["STABLE_SIGNAL_IMAGE"])

    def test_the_signal_image_and_the_base_image_still_point_at_one_stream(self) -> None:
        """The doc makes this an instruction, so a split between them is a defect."""
        self.assertIn("Keep these two values pointed at the\nsame Fedora Kinoite major tag.", doc())
        configured = defaults()
        self.assertEqual(configured["STABLE_SIGNAL_IMAGE"], configured["DEFAULT_BASE_IMAGE"])

    def test_both_defaults_carry_the_documented_fedora_major_tag(self) -> None:
        stated = re.search(
            r"Both default to the explicit Fedora major tag `kinoite:(\d+)`", doc()
        )
        self.assertIsNotNone(stated, "the overview stopped naming the Fedora major tag")
        configured = defaults()
        for key in ("STABLE_SIGNAL_IMAGE", "DEFAULT_BASE_IMAGE"):
            with self.subTest(key=key):
                self.assertTrue(
                    configured[key].endswith(f"/kinoite:{stated.group(1)}"),
                    f"{key}={configured[key]} is not the documented major tag",
                )


class PublishedTagShapeTests(unittest.TestCase):
    """The "Outputs" section lists every tag this repo publishes."""

    def documented_tags(self) -> dict[str, str]:
        tags = {}
        for line in doc().splitlines():
            match = re.match(r"- (.+ tag): `([^`]+)`$", line)
            if match:
                tags[match.group(1)] = match.group(2)
        return tags

    def test_the_documented_tag_list_is_the_one_this_test_checks(self) -> None:
        self.assertEqual(
            set(self.documented_tags()),
            {"candidate tag", "stable tag", "stable audit tag", "branch tag"},
        )

    def test_the_candidate_tag_shape_is_the_one_the_helper_builds(self) -> None:
        stated = self.documented_tags()["candidate tag"]
        self.assertEqual(
            stated,
            "ghcr.io/danathar/zfs-kinoite-complex:"
            + build_candidate_tag(github_sha="<sha>", fedora_version="<fedora>"),
        )

    def test_the_branch_tag_shape_is_the_one_the_helper_builds(self) -> None:
        stated = self.documented_tags()["branch tag"]
        built = build_branch_image_tag(
            branch_tag_prefix=build_branch_metadata("<branch>"),
            fedora_version="<fedora>",
        )
        # `<branch>` is not a registry-safe branch name, so the prefix helper
        # rewrites the angle brackets; compare the shape, not the placeholder.
        self.assertEqual(
            stated,
            "ghcr.io/danathar/zfs-kinoite-complex:"
            + built.replace("br-branch-", "br-<branch>-"),
        )
        self.assertTrue(built.startswith("br-"), built)

    def test_promotion_moves_exactly_the_two_documented_stable_tags(self) -> None:
        """Run the real promotion and compare the tags it writes to the doc."""
        copied: list[str] = []

        def fake_copy(source, destination, **kwargs) -> None:
            copied.append(destination)

        def fake_digest(ref: str, **kwargs) -> str:
            return "sha256:candidate"

        env = {
            "IMAGE_NAME": "zfs-kinoite-complex",
            "GITHUB_REPOSITORY_OWNER": "Danathar",
            "GITHUB_SHA": "deadbeefcafe",
            "GITHUB_RUN_NUMBER": "<run>",
            "REGISTRY_ACTOR": "actor",
            "REGISTRY_TOKEN": "token",
            "FEDORA_VERSION": "<fedora>",
        }
        with (
            patch.dict("os.environ", env, clear=False),
            patch("ci_tools.promote_stable.skopeo_copy", side_effect=fake_copy),
            patch("ci_tools.promote_stable.skopeo_inspect_digest", side_effect=fake_digest),
            patch("ci_tools.promote_stable.verify_candidate_signature"),
        ):
            promote_main()

        prefix = "docker://ghcr.io/danathar/zfs-kinoite-complex:"
        stated = self.documented_tags()
        audit = stated["stable audit tag"].replace("<sha>", "deadbee")
        self.assertEqual(
            copied,
            [
                prefix + audit.split(":", 2)[-1],
                prefix + stated["stable tag"].split(":", 2)[-1],
            ],
            "promotion writes the audit tag first, then latest",
        )

    def test_the_akmods_cache_repository_is_the_configured_one(self) -> None:
        text = doc()
        configured = defaults()
        repository = f"ghcr.io/danathar/{configured['AKMODS_REPO']}"
        self.assertIn(f"- `{repository}:main-<fedora>`\n", text)
        self.assertIn(f"- `{repository}:main-<fedora>-x86_64`\n", text)
        self.assertIn(f"- `{repository}@sha256:<digest>`\n", text)
        # The readable tag the workflow jobs check or publish, from the helper
        # that builds it rather than from a second copy of the format string.
        self.assertEqual(
            akmods_cache_image_tag(
                image_org="danathar",
                source_repo=configured["AKMODS_REPO"],
                fedora_version="<fedora>",
            ),
            f"{repository}:main-<fedora>",
        )
        # And the digest-pinned ref the final image build actually consumes.
        with patch(
            "ci_tools.pin_akmods_cache.skopeo_inspect_digest",
            return_value="sha256:<digest>",
        ):
            pinned, digest = pin_akmods_cache_image(f"{repository}:main-<fedora>")
        self.assertEqual(pinned, f"{repository}@sha256:<digest>")
        self.assertEqual(digest, "sha256:<digest>")

    def test_the_documented_image_repository_is_the_one_the_build_signs_for(self) -> None:
        self.assertIn("ghcr.io/danathar/zfs-kinoite-complex", doc())
        self.assertIn(
            'ARG IMAGE_REPO="ghcr.io/danathar/zfs-kinoite-complex"',
            read("Containerfile"),
        )


class ContainerfileSectionTests(unittest.TestCase):
    """Section "3. Native Final Image Build" enumerates the Containerfile."""

    def test_the_containerfile_list_count_word_matches_its_length(self) -> None:
        # The defect this file was opened for: the opener said four and the
        # list had five items, all five of them real Containerfile steps.
        opener = re.search(r"It does (\w+) important things:", doc())
        self.assertIsNotNone(opener, "the Containerfile section stopped opening its list")
        words = {"three": 3, "four": 4, "five": 5, "six": 6}
        self.assertIn(opener.group(1), words, "unhandled count word")
        self.assertEqual(
            words[opener.group(1)],
            len(numbered_list_after(opener.group(0))),
        )

    def test_each_documented_containerfile_step_names_something_in_it(self) -> None:
        containerfile = read("Containerfile")
        needles = {
            1: 'ARG BASE_IMAGE="quay.io/fedora-ostree-desktops/kinoite',
            2: "FROM ${BREW_IMAGE} AS brew",
            3: "/ctx/check-brew-payload-inventory.sh",
            4: "/ctx/build-image.sh",
            5: "RUN bootc container lint",
        }
        items = numbered_list_after(re.search(r"It does \w+ important things:", doc()).group(0))
        self.assertEqual(len(items), len(needles))
        for number, needle in needles.items():
            with self.subTest(step=number):
                self.assertIn(needle, containerfile)

    def test_the_inventory_check_runs_above_the_payload_copy(self) -> None:
        """The doc calls this ordering load-bearing, twice, at length."""
        containerfile = read("Containerfile")
        inventory = containerfile.index("/ctx/check-brew-payload-inventory.sh")
        copy_in = containerfile.index("COPY --from=brew /system_files /")
        build = containerfile.index("/ctx/build-image.sh")
        lint = containerfile.index("RUN bootc container lint")
        self.assertLess(inventory, copy_in)
        self.assertLess(copy_in, build)
        self.assertLess(build, lint)
        self.assertIn("that ordering is load-bearing", doc())

    def test_the_payload_is_bind_mounted_rather_than_copied_twice(self) -> None:
        self.assertIn("The payload is bind-mounted from the `brew` stage", doc())
        self.assertIn(
            "--mount=type=bind,from=brew,source=/system_files,target=/brew-payload",
            read("Containerfile"),
        )

    def test_the_documented_manifest_format_flag_is_the_one_buildah_gets(self) -> None:
        """
        The doc used to name `oci: false`, an input of
        `redhat-actions/buildah-build`. This repository calls buildah itself,
        so that name appeared nowhere in the tree a reader would grep.
        """
        stated = re.search(
            r"uses Docker v2s2 manifest format \(`([^`]+)`\)", doc()
        )
        self.assertIsNotNone(stated, "the overview stopped naming a manifest-format flag")
        action = read(".github/actions/build-native-image/action.yml")
        self.assertIn(stated.group(1), action)
        self.assertIn("--format docker \\", action)

    def test_the_documented_inventory_comparison_is_paths_not_hashes(self) -> None:
        self.assertIn("It\ncompares paths and not hashes on purpose", doc())
        script = read("build_files/check-brew-payload-inventory.sh")
        self.assertIn("brew-payload.manifest", script)
        self.assertNotIn("sha256sum", script)

    def test_the_signing_policy_step_is_the_documented_python_helper(self) -> None:
        self.assertIn("files/scripts/configure_signing_policy.py", doc())
        self.assertIn(
            "python3 /ctx/files/scripts/configure_signing_policy.py",
            read("build_files/build-image.sh"),
        )


class BuildImageStepOrderTests(unittest.TestCase):
    """The nine-step list for build_files/build-image.sh, in the script's order."""

    # One anchor per documented step, in the documented order. The anchors are
    # what the step acts on, not its wording: the doc is allowed to rephrase a
    # step, but not to reorder one, because the section immediately below the
    # list argues from that order ("steps 2, 3 and 4 act on that payload").
    # Anchors are the lines that run, never a mention in a comment: every one
    # of these names also appears in this script's prose, and an anchor that
    # matched the explanation instead of the step would report an order the
    # script does not have.
    ANCHORS = (
        ("installs the committed `cosign.pub`", 'install -m 0644 /ctx/cosign.pub "/etc/pki'),
        ("enables brew setup/update services", "\n/usr/bin/systemctl preset brew-setup.service"),
        ("removes the brew payload's own login-shell fragments", "\ncheck_brew_login_fragments\n"),
        (
            "installs a `PrivateTmp=yes` drop-in",
            "\n  /usr/lib/systemd/system/brew-setup.service.d/10-private-tmp.conf\n",
        ),
        ("keeps Distrobox", "\n# Distrobox is already included by Fedora Kinoite"),
        (
            "runs the ZFS install helper",
            "\npython3 /ctx/containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py\n",
        ),
        (
            "writes repository-specific signing policy",
            "\npython3 /ctx/files/scripts/configure_signing_policy.py\n",
        ),
        (
            "installs the local `tmpfiles.d` declaration",
            "\n  /usr/lib/tmpfiles.d/zfs-kinoite-complex.conf\n",
        ),
        ("removes build-only runtime/container state", "\nrm -rf /var/lib/containers\n"),
    )

    def setUp(self) -> None:
        self.items = numbered_list_after("`build-image.sh` then:\n")
        self.script = read("build_files/build-image.sh")

    def test_the_documented_list_is_the_length_this_test_covers(self) -> None:
        self.assertEqual(len(self.items), len(self.ANCHORS))

    def test_every_documented_step_says_what_this_test_thinks_it_says(self) -> None:
        for item, (phrase, _) in zip(self.items, self.ANCHORS):
            with self.subTest(step=phrase):
                self.assertIn(phrase, item)

    def test_the_script_performs_the_documented_steps_in_the_documented_order(self) -> None:
        positions = []
        for phrase, anchor in self.ANCHORS:
            with self.subTest(step=phrase):
                self.assertIn(anchor, self.script)
                positions.append(self.script.index(anchor))
        self.assertEqual(positions, sorted(positions), "build-image.sh reordered its steps")

    def test_the_two_documented_payload_checks_run_after_the_copy(self) -> None:
        """
        Steps 3 and 4 "read the image root rather than the bind mount, which is
        also why they stay on this side of the copy" -- so both are functions of
        this script, not of the pre-copy inventory.
        """
        for function in ("check_brew_login_fragments", "check_brew_setup_staging"):
            with self.subTest(function=function):
                self.assertIn(f"{function}() {{", self.script)
                self.assertIn(f"\n{function}\n", self.script)
        self.assertNotIn(
            "check_brew_login_fragments",
            read("build_files/check-brew-payload-inventory.sh"),
        )


class PrimaryKernelZfsInstallTests(unittest.TestCase):
    """Section "4. Primary-Kernel ZFS Install Logic" enumerates the helper."""

    HELPER = "containerfiles/zfs-akmods/install_zfs_from_akmods_cache.py"

    def test_the_documented_step_list_matches_the_helper(self) -> None:
        items = numbered_list_after("Instead, the helper does this:\n")
        self.assertEqual(len(items), 6)
        helper = read(self.HELPER)
        for needle in ("/lib/modules", "newest", "kmod-zfs", "dnf5", "depmod"):
            with self.subTest(needle=needle):
                self.assertIn(needle, helper)
        self.assertIn("newest detected kernel as the supported primary kernel", items[1])

    def test_the_documented_module_forms_are_the_accepted_ones(self) -> None:
        """The doc lists the three spellings Fedora has shipped."""
        stated = re.findall(r"`(zfs\.ko(?:\.\w+)?)`", doc())
        self.assertEqual(sorted(set(stated)), ["zfs.ko", "zfs.ko.xz", "zfs.ko.zst"])
        helper = read(self.HELPER)
        for form in set(stated):
            with self.subTest(form=form):
                self.assertIn(form, helper)
        # A glob is how the helper accepts all three; a literal list of two
        # would satisfy the loop above and still miss the third.
        self.assertIn('ZFS_KO_DISK_GLOB = "zfs.ko*"', helper)

    def test_the_build_fails_when_the_primary_kernel_has_no_module(self) -> None:
        self.assertIn("fail the build if that supported kernel does not end up with", doc())
        self.assertIn("No ZFS module for supported primary kernel", read(self.HELPER))


class RechunkBandTests(unittest.TestCase):
    """The Chunkah subsection describes three actions and one skopeo flag."""

    def test_the_documented_host_preparation_is_what_the_action_installs(self) -> None:
        action = read(".github/actions/prepare-rechunk-host/action.yml")
        self.assertIn("unconditionally from Ubuntu's `resolute` apt suite", doc())
        for tool in ("crun", "buildah", "podman", "skopeo"):
            with self.subTest(tool=tool):
                self.assertIn(f"{tool}/resolute", action)

    def test_the_documented_podman_floor_is_asserted_by_the_action(self) -> None:
        stated = re.search(r"asserts podman `>= (\d+)` after installing", doc())
        self.assertIsNotNone(stated, "the overview stopped naming a podman floor")
        action = read(".github/actions/prepare-rechunk-host/action.yml")
        # The comparison the shell actually makes, not a mention of the floor:
        # the step's own comment says "podman < 5" two lines above it, so a
        # loose search here would pass while the test compared against 4.
        tests = re.findall(r"^\s*if \[ .+ -lt (\d+) \]; then$", action, re.MULTILINE)
        self.assertEqual(tests, [stated.group(1)], "the installed-podman floor moved")
        self.assertIn(
            f"Chunkah needs >= {stated.group(1)} or it loses OCI layer annotations.",
            action,
        )

    def test_the_documented_storage_relocation_is_in_the_action(self) -> None:
        self.assertIn("container storage relocated onto the runner's larger `/mnt` disk", doc())
        self.assertIn("/mnt", read(".github/actions/prepare-rechunk-host/action.yml"))

    def test_the_documented_rechunk_mechanism_is_the_one_the_action_uses(self) -> None:
        self.assertIn("`podman run --mount=type=image` against the Chunkah", doc())
        self.assertIn(
            "--mount=type=image",
            read(".github/actions/rechunk-native-image/action.yml"),
        )

    def test_both_documented_digest_preserving_copies_pass_the_flag(self) -> None:
        """
        The subsection's last point is that neither copy may re-encode the
        manifest, because that would drop Chunkah's layer annotations.
        """
        self.assertIn("`skopeo copy --preserve-digests`", doc())
        self.assertIn(
            "skopeo copy --multi-arch=all --preserve-digests",
            read(".github/actions/publish-native-image/action.yml"),
        )
        self.assertIn("preserve_digests=True", read("ci_tools/promote_stable.py"))


class PublicationAndPromotionTests(unittest.TestCase):
    """Section "5. Promotion And Signing" lists both step sequences."""

    def test_the_publish_action_takes_the_documented_four_steps(self) -> None:
        items = numbered_list_after("The publish action:\n")
        self.assertEqual(len(items), 4)
        action = read(".github/actions/publish-native-image/action.yml")
        self.assertIn("-unsigned-${{ github.run_id }}", action)
        self.assertIn("unsigned-<run_id>", items[0])
        self.assertIn("Sign transient image digest", action)

    def test_promotion_re_verifies_the_signature_before_moving_a_tag(self) -> None:
        items = numbered_list_after("It:\n")
        self.assertEqual(len(items), 4)
        self.assertIn("re-verifies that digest's cosign signature", items[1])
        source = read("ci_tools/promote_stable.py")
        verify = source.index("verify_candidate_signature(")
        self.assertLess(verify, source.index("_copy_and_verify_digest("))
        self.assertIn("cosign.pub", source + read("ci_tools/sign_image.py"))

    def test_the_documented_cancellation_reasoning_still_has_its_concurrency_block(self) -> None:
        self.assertIn("`build.yml` cancels an in-progress\npromotion", doc())
        workflow = read(".github/workflows/build.yml")
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)

    def test_latest_is_not_signed_a_second_time(self) -> None:
        self.assertIn("It does not sign `latest` again.", doc())
        self.assertNotIn("cosign sign", read("ci_tools/promote_stable.py"))


class OperationalModelTests(unittest.TestCase):
    """The operational list names workflows; the gate section names one job."""

    def documented_workflows(self) -> list[str]:
        return [
            match.group(1)
            for match in re.finditer(r"^\d+\. `([a-z-]+\.yml)`:", doc(), re.MULTILINE)
        ]

    def test_every_workflow_the_document_names_exists(self) -> None:
        named = self.documented_workflows()
        self.assertEqual(len(named), 5, named)
        for name in named:
            with self.subTest(workflow=name):
                self.assertTrue((REPO_ROOT / ".github" / "workflows" / name).is_file())

    def test_the_gate_lives_in_the_documented_job_and_only_gates_schedules(self) -> None:
        self.assertIn("the `preflight` job in `build.yml`", doc())
        self.assertIn(
            "Push and manual (`workflow_dispatch`)\nruns always build; only the daily "
            "`schedule` trigger is gated.",
            doc(),
        )
        workflow = read(".github/workflows/build.yml")
        self.assertIn("\n  preflight:\n", workflow)
        self.assertIn("python3 -m ci_tools.cli check-stable-signal", workflow)
        gated = re.findall(
            r"if: github\.event_name != 'schedule' \|\| needs\.preflight\.outputs\.should_build",
            workflow,
        )
        # Every job that consumes the gate bypasses it for non-schedule events.
        self.assertGreaterEqual(len(gated), 2)
        self.assertEqual(
            len(gated),
            workflow.count("needs.preflight.outputs.should_build"),
            "a job reads the gate output without the non-schedule bypass",
        )

    def test_branch_runs_never_rebuild_the_shared_cache(self) -> None:
        self.assertIn("branch runs pass `allow_cache_rebuild: \"false\"`", doc())
        self.assertIn('allow_cache_rebuild: "false"', read(".github/workflows/build-branch.yml"))
        self.assertNotIn(
            'allow_cache_rebuild: "false"',
            read(".github/workflows/build.yml"),
        )

    def test_the_cache_signing_job_runs_in_the_documented_environment(self) -> None:
        self.assertIn(
            "a dedicated `sign-akmods-cache` job (in `build.yml`, running in the\n"
            "`production-signing` environment)",
            doc(),
        )
        workflow = read(".github/workflows/build.yml")
        self.assertIn("\n  sign-akmods-cache:\n", workflow)
        job = workflow[workflow.index("\n  sign-akmods-cache:\n") :]
        self.assertIn("environment: production-signing", job[: job.index("\n  build-candidate")])

    def test_the_triage_workflow_keys_on_the_documented_artifact_prefix(self) -> None:
        self.assertIn("checks for a `build-inputs-<run_id>` artifact", doc())
        self.assertIn(
            "startsWith('build-inputs-')",
            read(".github/workflows/akmods-failure-triage.yml"),
        )

    def test_the_documented_defaults_file_is_where_the_workflows_read_from(self) -> None:
        self.assertIn("keep workflow defaults in one checked-in file", doc())
        self.assertTrue(DEFAULTS_PATH.is_file())
        self.assertIn("load-ci-defaults", read(".github/workflows/build.yml"))

    def test_the_documented_container_image_exception_is_still_one_literal(self) -> None:
        """
        The overview's closing exception: `jobs.<job>.container.image` cannot
        read the defaults file, so both akmods jobs carry one literal fallback.
        """
        self.assertIn("still carry one literal fallback build-container ref", doc())
        configured = defaults()["DEFAULT_BUILD_CONTAINER_IMAGE"]
        base = configured.split("@")[0]
        for name in ("build.yml", "build-branch.yml"):
            with self.subTest(workflow=name):
                workflow = read(f".github/workflows/{name}")
                literals = re.findall(rf"^\s+image: .*{re.escape(base)}.*$", workflow, re.MULTILINE)
                self.assertEqual(len(literals), 1, literals)
                self.assertIn("workflow_dispatch", workflow)


if __name__ == "__main__":
    unittest.main()
