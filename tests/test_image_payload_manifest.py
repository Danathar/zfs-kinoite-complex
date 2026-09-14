"""
Script: tests/test_image_payload_manifest.py
What: Tests that every file under files/ reaches the image, and that the two configuration payloads nothing else opens -- the modules-load.d and tmpfiles.d declarations -- say what the build says they say.
Doing: Parses build-image.sh's install commands, joins them to the tracked contents of files/ and to the Containerfile's build-context stage, and parses both declarations in the format systemd reads them.
Why: files/ is shipped configuration, not code: no --cov path measures it, build-image.sh is recorded as unmeasured in .coverage-thresholds.json, and a file that stops being installed -- or arrives and is never installed at all -- produces a green suite and an image missing the behaviour.
Goal: Make "this file ships" and "this file says X" failures the suite can see on the host, without a container build.

`files/etc/profile.d/brew-path.sh` and the `brew-setup.service` drop-in already
have tests that assert their own install lines. What no test held is the
manifest itself: the set. Adding a sixth file under `files/` and forgetting the
`install` line was a silent no-op, and so was deleting an install line for a
file that stayed in the tree. Both are single-line edits, and the image build
that would notice runs after the merge, if at all -- `bootc container lint`
cannot know a file was meant to be there.

The two declarations tested here were opened by no test at all. Their content is
read by `systemd-modules-load` and `systemd-tmpfiles` on a booted machine, so a
typo in either is discovered by a user, not by CI. They are parsed here the way
those two programs parse them rather than grepped, so a line that no longer
means what it meant fails even when the words survive.

No PyYAML and no third-party parser, for the reason
tests/test_docs_consistency.py gives: the CI job installs only pytest,
pytest-cov and ruff.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_IMAGE = REPO_ROOT / "build_files" / "build-image.sh"
CONTAINERFILE = REPO_ROOT / "Containerfile"
PAYLOAD_DIR = REPO_ROOT / "files"

MODULES_LOAD_RELATIVE = "usr/lib/modules-load.d/zfs.conf"
TMPFILES_RELATIVE = "usr/lib/tmpfiles.d/zfs-kinoite-complex.conf"

TESTING_DOC = REPO_ROOT / "docs" / "zfs-kinoite-testing.md"

# Payload files the build runs rather than installs, with the invocation that runs
# each one. A file here ships nothing into the image, so it needs a reason on the
# record instead of being quietly exempt from the manifest join below.
EXECUTED_NOT_INSTALLED = {
    "scripts/configure_signing_policy.py": "python3 /ctx/files/scripts/configure_signing_policy.py",
}

# Every payload file is installed read-only. `install -D` creates the parent
# directories, which matters because none of these paths exist in the base image.
EXPECTED_MODE = "0644"

CTX_MOUNT = "/ctx"


def build_image_text() -> str:
    return BUILD_IMAGE.read_text(encoding="utf-8")


def containerfile_text() -> str:
    return CONTAINERFILE.read_text(encoding="utf-8")


def tracked_payload_files() -> list[str]:
    """Paths under files/, relative to it, as the repository would ship them.

    `--others --exclude-standard` is deliberate: a file added to the tree and not
    yet committed is exactly the case this join exists to catch, and plain
    `ls-files` would not see it.
    """

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        line[len("files/") :] for line in result.stdout.splitlines() if line.startswith("files/")
    )


def shell_commands(text: str) -> list[list[str]]:
    """Split build-image.sh into argv lists, joining backslash continuations.

    Comments are dropped first: the script explains itself at length, and several
    of those explanations quote command lines that the build does not run.
    """

    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    joined = re.sub(r"\\\n\s*", " ", code)
    commands = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            commands.append(shlex.split(stripped))
        except ValueError:  # pragma: no cover - an unbalanced quote is a syntax error
            continue
    return commands


class InstallCommand:
    """One `install` invocation, parsed the way install(1) reads its arguments."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.flags: set[str] = set()
        self.mode: str | None = None
        self.operands: list[str] = []
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "-m":
                self.mode = argv[index + 1]
                index += 2
                continue
            if token.startswith("-") and len(token) > 1:
                self.flags.update(token[1:])
                index += 1
                continue
            self.operands.append(token)
            index += 1

    @property
    def creates_directories_only(self) -> bool:
        return "d" in self.flags

    @property
    def source(self) -> str:
        return self.operands[0]

    @property
    def destination(self) -> str:
        return self.operands[-1]


def install_commands() -> list[InstallCommand]:
    return [
        InstallCommand(argv)
        for argv in shell_commands(build_image_text())
        if argv and argv[0] == "install"
    ]


def file_installs() -> dict[str, InstallCommand]:
    """Installs that copy a file, keyed by source path."""

    return {
        command.source: command
        for command in install_commands()
        if not command.creates_directories_only
    }


def context_copies() -> dict[str, Path]:
    """Map each path visible under /ctx to the repository path it comes from.

    The build context is its own `FROM scratch AS ctx` stage, bind-mounted at
    /ctx. A `COPY <src> /` lands the *contents* of src at the root of that stage,
    so it contributes one entry per child; any other destination contributes one
    entry for the tree itself.
    """

    stage = containerfile_text().split("FROM scratch AS ctx", 1)[-1]
    stage = stage.split("\nFROM ", 1)[0]
    mapping: dict[str, Path] = {}
    for line in stage.splitlines():
        argv = shlex.split(line.strip()) if line.strip() else []
        if len(argv) != 3 or argv[0] != "COPY":
            continue
        _, source, destination = argv
        origin = REPO_ROOT / source
        if destination == "/":
            for child in sorted(origin.iterdir()):
                mapping[f"{CTX_MOUNT}/{child.name}"] = child
            continue
        mapping[f"{CTX_MOUNT}{destination}"] = origin
    return mapping


def resolve_in_context(path: str, mapping: dict[str, Path]) -> Path | None:
    """Resolve a /ctx path to its repository path, or None if nothing supplies it."""

    for prefix, origin in mapping.items():
        if path == prefix:
            return origin
        if path.startswith(prefix + "/"):
            return origin / path[len(prefix) + 1 :]
    return None


def declaration_lines(path: Path) -> list[str]:
    """Content lines of a systemd configuration file: comments and blanks dropped.

    systemd treats both `#` and `;` as comment introducers in these files.
    """

    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        lines.append(stripped)
    return lines


class PremiseTests(unittest.TestCase):
    """What the rest of this file rests on."""

    def test_the_payload_directory_holds_the_files_this_module_names(self) -> None:
        # Every join below is over a computed set. If a rename emptied that set the
        # joins would all pass while asserting nothing, so name the two files whose
        # content this module is the only reader of.
        payload = tracked_payload_files()
        self.assertIn(MODULES_LOAD_RELATIVE, payload)
        self.assertIn(TMPFILES_RELATIVE, payload)

    def test_the_build_runs_the_script_out_of_the_build_context(self) -> None:
        self.assertRegex(containerfile_text(), r"(?m)^\s+/ctx/build-image\.sh$")

    def test_the_image_is_still_linted(self) -> None:
        # The tmpfiles.d declaration exists for `bootc container lint`'s var-tmpfiles
        # rule. Without that step it is inert, and this file should be revisited.
        self.assertRegex(containerfile_text(), r"(?m)^RUN bootc container lint$")


class PayloadManifestTests(unittest.TestCase):
    """Every file under files/ either ships or runs, and the build says which."""

    def test_every_payload_file_is_installed_or_executed(self) -> None:
        installs = file_installs()
        text = build_image_text()
        for relative in tracked_payload_files():
            with self.subTest(payload=relative):
                if relative in EXECUTED_NOT_INSTALLED:
                    self.assertIn(
                        EXECUTED_NOT_INSTALLED[relative],
                        text,
                        f"files/{relative} is recorded as executed, but the build does not run it",
                    )
                    continue
                source = f"{CTX_MOUNT}/files/{relative}"
                self.assertIn(
                    source,
                    installs,
                    f"files/{relative} ships nothing: no install command reads {source}",
                )
                self.assertEqual(installs[source].destination, f"/{relative}")

    def test_every_installed_payload_file_lands_read_only_with_its_parents(self) -> None:
        installs = file_installs()
        for relative in tracked_payload_files():
            if relative in EXECUTED_NOT_INSTALLED:
                continue
            command = installs.get(f"{CTX_MOUNT}/files/{relative}")
            if command is None:
                continue  # reported by the test above
            with self.subTest(payload=relative):
                self.assertEqual(command.mode, EXPECTED_MODE)
                self.assertIn(
                    "D",
                    command.flags,
                    "install -D creates the parent directories; none of these exist in the base",
                )

    def test_no_install_reads_a_payload_file_that_is_not_there(self) -> None:
        payload = set(tracked_payload_files())
        prefix = f"{CTX_MOUNT}/files/"
        for source in file_installs():
            if not source.startswith(prefix):
                continue
            relative = source[len(prefix) :]
            with self.subTest(source=source):
                self.assertIn(
                    relative,
                    payload,
                    f"the build installs {source}, which files/ does not contain",
                )

    def test_every_context_path_the_script_reads_is_copied_into_the_context_stage(self) -> None:
        mapping = context_copies()
        referenced = sorted(set(re.findall(r"/ctx/[A-Za-z0-9._/-]+", build_image_text())))
        self.assertIn(f"{CTX_MOUNT}/files/{MODULES_LOAD_RELATIVE}", referenced)
        for path in referenced:
            with self.subTest(path=path):
                origin = resolve_in_context(path, mapping)
                self.assertIsNotNone(
                    origin,
                    f"{path} is read by the build, but no COPY in the ctx stage supplies it",
                )
                self.assertTrue(origin.exists(), f"{path} resolves to {origin}, which is absent")


class ModulesLoadTests(unittest.TestCase):
    """`systemd-modules-load` reads one module name per line, nothing else."""

    def setUp(self) -> None:
        self.path = PAYLOAD_DIR / MODULES_LOAD_RELATIVE

    def test_it_loads_the_zfs_module_and_only_that(self) -> None:
        # Every name here is modprobe'd as root at boot. The image installs exactly
        # one kernel module payload, so one name is the whole contract.
        self.assertEqual(declaration_lines(self.path), ["zfs"])

    def test_it_is_installed_where_systemd_looks(self) -> None:
        # systemd reads *.conf under modules-load.d and ignores every other name,
        # so a renamed destination is a file that is never read.
        destination = file_installs()[f"{CTX_MOUNT}/files/{MODULES_LOAD_RELATIVE}"].destination
        self.assertTrue(destination.startswith("/usr/lib/modules-load.d/"))
        self.assertTrue(destination.endswith(".conf"))

    def test_the_testing_guide_still_describes_this_file(self) -> None:
        # docs/zfs-kinoite-testing.md tells a reader why post-boot validation can
        # report a kernel-module version without a manual modprobe. That paragraph is
        # only true while this file is installed and names this module.
        text = TESTING_DOC.read_text(encoding="utf-8")
        self.assertIn(f"(../files/{MODULES_LOAD_RELATIVE})", text)
        self.assertIn("systemd-modules-load", text)
        self.assertIn("modprobe zfs", text)


class TmpfilesTests(unittest.TestCase):
    """`systemd-tmpfiles` reads seven whitespace-separated fields per line."""

    def setUp(self) -> None:
        self.path = PAYLOAD_DIR / TMPFILES_RELATIVE
        self.entries = [line.split() for line in declaration_lines(self.path)]

    def test_every_line_creates_a_root_owned_directory(self) -> None:
        self.assertTrue(self.entries, f"{self.path} declares nothing")
        for fields in self.entries:
            with self.subTest(line=" ".join(fields)):
                self.assertEqual(len(fields), 7, "type, path, mode, user, group, age, argument")
                kind, _, mode, user, group, age, argument = fields
                # `d` creates the directory and leaves an existing one alone. `D` and
                # `R` would empty it, which on /var is user data on an installed
                # machine, not build-time state.
                self.assertEqual(kind, "d")
                self.assertEqual(mode, "0755")
                self.assertEqual(user, "root")
                self.assertEqual(group, "root")
                self.assertEqual([age, argument], ["-", "-"], "no cleanup age, no argument")

    def test_every_declared_directory_is_state_under_var(self) -> None:
        # The rule this file answers is `bootc container lint`'s var-tmpfiles: content
        # baked into /var is applied at install and never refreshed by `bootc upgrade`.
        # A declaration outside /var answers a different rule and belongs elsewhere.
        for fields in self.entries:
            with self.subTest(path=fields[1]):
                self.assertTrue(fields[1].startswith("/var/"))

    def test_each_nested_directory_is_declared_after_its_parent(self) -> None:
        # systemd-tmpfiles applies the lines of one file in order, so a child listed
        # before its parent -- or with no parent line at all -- relies on ordering
        # this repository does not control. /var/lib is Fedora's, not ours.
        declared: list[str] = []
        for fields in self.entries:
            path = fields[1]
            parent = str(Path(path).parent)
            with self.subTest(path=path):
                if parent not in ("/var", "/var/lib"):
                    self.assertIn(
                        parent,
                        declared,
                        f"{path} is declared before (or without) its parent {parent}",
                    )
            declared.append(path)

    def test_it_is_installed_where_systemd_looks(self) -> None:
        destination = file_installs()[f"{CTX_MOUNT}/files/{TMPFILES_RELATIVE}"].destination
        self.assertTrue(destination.startswith("/usr/lib/tmpfiles.d/"))
        self.assertTrue(destination.endswith(".conf"))

    def test_the_declared_directories_are_the_ones_the_comment_explains(self) -> None:
        # The file says these come from `pcp`, pulled in by the zfs userspace chain.
        # If a later entry answers a different lint warning the reason above it stops
        # covering the file, which is how a declaration nobody can justify survives.
        for fields in self.entries:
            with self.subTest(path=fields[1]):
                self.assertTrue(fields[1].startswith("/var/lib/pcp"))
        self.assertIn("pcp", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
