"""
Script: tests/test_build_image_brew_payload_inventory.py
What: Tests that the build compares the brew payload's complete file list against a committed manifest and stops on any difference.
Doing: Extracts the inventory check from build-image.sh and runs it against fixture payload trees, asserts the Containerfile mounts the payload for it, and cross-checks the manifest against the two narrower brew checks.
Why: `COPY --from=brew /system_files /` copies a third-party tree into the signed image; the digest pin in ci/defaults.json makes that payload reproducible but not reviewed, so a bump can add a file nobody read.
Goal: Make a new file in the payload fail the build rather than ship.

The payload is not in this tree at any revision, so nothing here can assert what it
contains. What the suite can pin is what this repository does about it: that the check
exists, that the Containerfile gives it something to look at, that the manifest and the
other two brew checks still agree about the same files, and -- by extracting the check
and running it -- what it does to a payload tree that has changed.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_IMAGE = REPO_ROOT / "build_files" / "build-image.sh"
CONTAINERFILE = REPO_ROOT / "Containerfile"
MANIFEST = REPO_ROOT / "build_files" / "brew-payload.manifest"

CHECK_FUNCTION = "check_brew_payload_inventory"

# Matches the check function from its opening line to the closing brace in column 1.
# build-image.sh defines no nested functions, so this is unambiguous.
CHECK_RE = re.compile(
    rf"^{CHECK_FUNCTION}\(\) \{{\n.*?^\}}\n",
    re.MULTILINE | re.DOTALL,
)

# The payload as it stands at the digest ci/defaults.json pins, read out of the
# registry rather than guessed from upstream's default branch. Both architectures
# ship the same eleven paths.
PAYLOAD_FILES = (
    "etc/profile.d/brew-bash-completion.sh",
    "etc/profile.d/brew.sh",
    "etc/security/limits.d/30-brew-limits.conf",
    "usr/lib/systemd/system/brew-setup.service",
    "usr/lib/systemd/system/brew-update.service",
    "usr/lib/systemd/system/brew-update.timer",
    "usr/lib/systemd/system/brew-upgrade.service",
    "usr/lib/systemd/system/brew-upgrade.timer",
    "usr/lib/systemd/system-preset/01-homebrew.preset",
    "usr/share/fish/vendor_conf.d/ublue-brew.fish",
    "usr/share/homebrew.tar.zst",
)


REMOVED_FRAGMENTS = (
    "etc/profile.d/brew.sh",
    "etc/profile.d/brew-bash-completion.sh",
    "usr/share/fish/vendor_conf.d/ublue-brew.fish",
)

# The `rm -f` continuation block that deletes the login-shell fragments, from its
# `rm -f \` line to the first line that does not end in a backslash.
REMOVAL_RE = re.compile(r"^rm -f \\\n(?:.*\\\n)*.*\n", re.MULTILINE)


def build_image_text() -> str:
    return BUILD_IMAGE.read_text(encoding="utf-8")


def removal_block() -> str:
    """Return the `rm -f` block that removes the payload's login-shell fragments."""

    match = REMOVAL_RE.search(build_image_text())
    assert match is not None, f"no `rm -f` continuation block in {BUILD_IMAGE}"
    return match.group(0)


def manifest_entries() -> list[str]:
    """Return the manifest's paths, with comments and blank lines dropped."""

    entries = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            entries.append(stripped)
    return entries


def check_source() -> str:
    """Return the inventory check exactly as build-image.sh defines it."""

    match = CHECK_RE.search(build_image_text())
    assert match is not None, f"{CHECK_FUNCTION} not found in {BUILD_IMAGE}"
    return match.group(0)


def run_check(payload: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    """Run the real check body against a fixture payload tree and manifest."""

    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{check_source()}\n"
        f'{CHECK_FUNCTION} "$1" "$2"\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", encoding="utf-8") as handle:
        handle.write(script)
        handle.flush()
        return subprocess.run(
            ["bash", handle.name, str(payload), str(manifest)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def fixture_payload(root: Path, relative_paths: tuple[str, ...]) -> Path:
    """Build a payload tree holding exactly `relative_paths`."""

    payload = root / "payload"
    for relative in relative_paths:
        path = payload / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    payload.mkdir(parents=True, exist_ok=True)
    return payload


class PremiseTests(unittest.TestCase):
    """What the rest of this file rests on."""

    def test_the_brew_payload_is_still_copied_wholesale(self) -> None:
        # A COPY narrowed to named paths would make this check redundant, and this
        # suite should be revisited rather than left asserting something that
        # protects nothing.
        self.assertIn(
            "COPY --from=brew /system_files /",
            CONTAINERFILE.read_text(encoding="utf-8"),
        )

    def test_the_containerfile_mounts_the_payload_for_inspection(self) -> None:
        # Without the mount the check cannot see anything, and build-image.sh would
        # fail every build rather than pass a build it did not check.
        self.assertIn(
            "--mount=type=bind,from=brew,source=/system_files,target=/brew-payload",
            CONTAINERFILE.read_text(encoding="utf-8"),
        )

    def test_the_payload_is_mounted_rather_than_copied_again(self) -> None:
        # A second COPY would add the 154MB tarball to the image a second time just
        # to read a list of file names.
        self.assertNotIn(
            "COPY --from=brew /system_files /brew-payload",
            CONTAINERFILE.read_text(encoding="utf-8"),
        )


class ManifestTests(unittest.TestCase):
    def test_the_manifest_lists_the_pinned_payload(self) -> None:
        self.assertEqual(sorted(manifest_entries()), sorted(PAYLOAD_FILES))

    def test_the_manifest_has_no_duplicate_entries(self) -> None:
        entries = manifest_entries()
        self.assertEqual(len(entries), len(set(entries)))

    def test_every_entry_is_a_relative_path(self) -> None:
        # The check compares against `find -printf '%P'` output, which is relative to
        # the payload root. A leading slash would silently never match.
        for entry in manifest_entries():
            self.assertFalse(entry.startswith("/"), entry)

    def test_the_fragments_the_build_removes_are_listed(self) -> None:
        # The removals in build-image.sh and this manifest describe the same files.
        # If the payload stopped shipping one, the inventory check should be what
        # says so, not a silent no-op `rm -f`.
        removed = {
            line.strip().rstrip("\\").strip().lstrip("/")
            for line in removal_block().splitlines()[1:]
        }
        self.assertEqual(removed, set(fragment for fragment in REMOVED_FRAGMENTS))
        for fragment in REMOVED_FRAGMENTS:
            self.assertIn(fragment, manifest_entries())

    def test_the_unit_the_staging_check_reads_is_listed(self) -> None:
        self.assertIn("usr/lib/systemd/system/brew-setup.service", manifest_entries())


class InventoryCheckTests(unittest.TestCase):
    def test_the_pinned_payload_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, PAYLOAD_FILES)
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_added_file_fails_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(
                root,
                PAYLOAD_FILES + ("usr/lib/systemd/system/sshd.service.d/10-brew.conf",),
            )
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no longer matches", result.stderr)
            # The reviewer is told which file, so they do not have to go unpack the
            # payload out of a registry to find out.
            self.assertIn("sshd.service.d/10-brew.conf", result.stderr)

    def test_an_added_file_in_a_directory_no_other_check_sweeps_fails(self) -> None:
        # check_brew_login_fragments greps three directories and
        # check_brew_setup_staging reads one unit. Neither sees these.
        for added in (
            "usr/lib/tmpfiles.d/brew.conf",
            "etc/profile",
            "etc/sudoers.d/brew",
            "usr/bin/sudo",
        ):
            with self.subTest(added=added):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    payload = fixture_payload(root, PAYLOAD_FILES + (added,))
                    result = run_check(payload, MANIFEST)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(added, result.stderr)

    def test_an_added_symlink_fails_the_build(self) -> None:
        # A symlink into the Homebrew prefix placed somewhere root reads is the same
        # exposure as a file there, so `find` matches both types.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, PAYLOAD_FILES)
            link = payload / "etc" / "profile.d" / "zz-brew.sh"
            link.symlink_to("/home/linuxbrew/.linuxbrew/profile.sh")
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 1)
            self.assertIn("zz-brew.sh", result.stderr)

    def test_a_removed_file_fails_the_build(self) -> None:
        # A payload that stopped shipping something is also a change worth reading:
        # the drop-in, the preset and the removals above are all aimed at named files.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, PAYLOAD_FILES[1:])
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 1)
            self.assertIn(PAYLOAD_FILES[0], result.stderr)

    def test_a_moved_file_fails_the_build(self) -> None:
        unit = "usr/lib/systemd/system/brew-setup.service"
        moved = tuple(
            "usr/lib/systemd/user/brew-setup.service" if path == unit else path
            for path in PAYLOAD_FILES
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, moved)
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 1)

    def test_an_empty_payload_fails_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, ())
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 1)

    def test_a_missing_mount_fails_the_build(self) -> None:
        # Not a pass: a check with nothing to look at must not read as "nothing
        # wrong", or dropping the mount from the Containerfile would disable it
        # silently.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = run_check(root / "absent", MANIFEST)
            self.assertEqual(result.returncode, 1)
            self.assertIn("not mounted", result.stderr)

    def test_directories_alone_do_not_count_as_entries(self) -> None:
        # The manifest lists files. An empty directory the payload happens to carry
        # must not have to be listed, or the manifest becomes noise.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, PAYLOAD_FILES)
            (payload / "usr" / "lib" / "modules-load.d").mkdir(parents=True)
            result = run_check(payload, MANIFEST)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_comments_and_blank_lines_in_the_manifest_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = fixture_payload(root, ("etc/only.conf",))
            manifest = root / "manifest"
            manifest.write_text(
                "# a comment\n"
                "\n"
                "etc/only.conf  # trailing comment\n"
                "   \n",
                encoding="utf-8",
            )
            result = run_check(payload, manifest)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_check_actually_runs_in_the_build(self) -> None:
        # A defined-but-never-called check is the guard that guards nothing.
        code = "\n".join(
            line for line in build_image_text().splitlines() if not line.lstrip().startswith("#")
        )
        self.assertRegex(code, rf"(?m)^{CHECK_FUNCTION}$")

    def test_the_check_runs_after_it_is_defined(self) -> None:
        text = build_image_text()
        definition = text.index(f"{CHECK_FUNCTION}() {{")
        call = text.index(f"\n{CHECK_FUNCTION}\n")
        self.assertLess(definition, call)

    def test_the_inventory_runs_before_anything_acts_on_the_payload(self) -> None:
        # An unknown file should stop the build before the presets enable units from
        # the same payload and before the two narrower checks look at parts of it.
        text = build_image_text()
        call = text.index(f"\n{CHECK_FUNCTION}\n")
        for later in (
            "systemctl preset brew-setup.service",
            "\ncheck_brew_login_fragments\n",
            "\ncheck_brew_setup_staging\n",
        ):
            self.assertLess(call, text.index(later), later)


if __name__ == "__main__":
    unittest.main()
