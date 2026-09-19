"""
Script: tests/test_runtime_validation_proposal_doc.py
What: Joins docs/runtime-validation-proposal.md to the pipeline whose boundary it describes and
to the two operator documents that carry the commands its sequence abbreviates.
Doing: Extracts each section of the proposal and recomputes every claim it makes about the
machine -- what CI validates, what it never executes before `latest` moves, what kind of runner
it uses, and that no workflow gate has taken the operator's place.
Why: The document is 33 lines and almost all of them are statements of fact about this
repository, hand-copied from .github/workflows/, the Containerfile, ci_tools/ and
containerfiles/. Nothing opened it, so a boot test could be added to CI -- or a pool touched
from a runner -- and the file explaining why neither happens would keep reading exactly the
same. docs/install-and-verify.md restates the same boundary in its testing-only warning, so the
two drift together and neither notices.
Goal: Make the proposal fail here when the pipeline moves under it, in both directions. A
boundary that was widened and never written down is as much a drift as one that regressed --
and this document's closing sentence, "the operator remains the final runtime validation step",
is false the moment a runtime gate appears in promote-stable's `needs`.

Two scanners, both hand-rolled, both with their own case table below.

`_shell_hits()` reads line-oriented files -- workflow YAML, composite actions, the Containerfile
and build_files/*.sh -- and asks whether a boot or pool primitive appears in command position.
Every mention of `bootc upgrade` or `modprobe` in this tree today is inside a comment explaining
why it is *not* run, so a scan that matched anywhere would report nine hits and have to be
weakened until it reported none. Command position is the distinction that keeps it honest.

`_python_hits()` reads Python through `ast`, which drops comments for free, and skips
docstrings explicitly. ci_tools/promote_stable.py's own docstring mentions `bootc upgrade`.

Both are approximations with a stated limit: a command assembled at runtime, or spelled through
a variable, is invisible to either. That is the same limit .claude/hooks/gate-git-diff.sh
documents for itself, and it is acceptable here for the same reason -- the thing being watched
for is a boot step someone adds on purpose, in the open, not one smuggled past a grep.

No PyYAML, for the reason tests/test_production_boundary_docs.py gives: CI installs pytest,
pytest-cov and ruff and nothing else, so a third-party import here would skip in exactly the
place the assertions are supposed to run.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTION_DIR = REPO_ROOT / ".github" / "actions"

PROPOSAL = DOCS_DIR / "runtime-validation-proposal.md"
INSTALL_AND_VERIFY = DOCS_DIR / "install-and-verify.md"
SAFETY_MODEL = DOCS_DIR / "safety-model.md"
DOC_MAP = DOCS_DIR / "documentation-guide.md"

CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD = WORKFLOW_DIR / "build.yml"
PROMOTE_STABLE = REPO_ROOT / "ci_tools" / "promote_stable.py"
AKMODS_INSTALLER = (
    REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
)
MODULES_LOAD_FRAGMENT = REPO_ROOT / "files" / "usr" / "lib" / "modules-load.d" / "zfs.conf"

CURRENT_BOUNDARY = "## Current boundary"
SEQUENCE = "## Recommended validation sequence"
FUTURE_AUTOMATION = "## Future automation requirements"

# The primitives that would make the document's central claim false: booting a deployment, or
# touching a pool, from inside CI.
#
# `bootc` is restricted to the subcommands that act on a machine. The Containerfile's
# `RUN bootc container lint` is a composition check on a filesystem tree and is exactly the kind
# of validation the document says the pipeline *does* perform, so matching a bare `bootc` here
# would flag the proposal's own supporting evidence as its refutation.
#
# `zfs` is likewise restricted to subcommands. Unrestricted, it matches the akmods installer's
# filenames and the shipped modules-load.d fragment, neither of which runs anything.
BOOT_OR_POOL = (
    r"zpool(?=\s|$)",
    r"zfs\s+(?:create|destroy|import|list|load-key|mount|set|snapshot)\b",
    r"modprobe(?=\s|$)",
    r"insmod(?=\s|$)",
    r"losetup(?=\s|$)",
    r"bootc\s+(?:switch|upgrade|rollback|install)\b",
    r"qemu-system-\w+",
    r"systemd-nspawn(?=\s|$)",
)

# Start of line, or after a separator that ends the previous command. `sudo`, `then`, `do` and
# `exec` are the words that can sit between a separator and the command itself in this tree.
COMMAND_POSITION = r"(?:^|[;&|(]|\$\(|\bsudo\b|\bthen\b|\bdo\b|\bexec\b)\s*"

SHELL_PRIMITIVE_RE = re.compile(COMMAND_POSITION + "(?:" + "|".join(BOOT_OR_POOL) + ")")
PYTHON_PRIMITIVE_RE = re.compile("^(?:" + "|".join(BOOT_OR_POOL) + ")")

# A subprocess argument list spells the command as one bare string with its arguments beside it,
# so a start-of-string match never sees the subcommand. These names are unambiguous alone.
BARE_COMMANDS = frozenset({"zpool", "modprobe", "insmod", "losetup", "systemd-nspawn"})


def _line_oriented_pipeline_files() -> list[Path]:
    """
    Every file the pipeline executes that is read line by line.

    Deliberately excludes `files/`: those are the scripts and units this image *ships*, which run
    on a booted machine and are outside the boundary this document draws. Including them would
    make `files/usr/lib/modules-load.d/zfs.conf` -- whose whole job is to load the module after
    boot -- read as CI loading a module.
    """

    return sorted(
        [
            *WORKFLOW_DIR.glob("*.yml"),
            *ACTION_DIR.glob("*/action.yml"),
            CONTAINERFILE,
            *(REPO_ROOT / "build_files").glob("*.sh"),
        ]
    )


def _python_pipeline_files() -> list[Path]:
    """Every Python module a workflow step or an image build runs."""

    return sorted(
        [
            *(REPO_ROOT / "ci_tools").glob("*.py"),
            *(REPO_ROOT / "shared").glob("*.py"),
            *(REPO_ROOT / "containerfiles").rglob("*.py"),
        ]
    )


def _shell_hits(text: str) -> list[str]:
    """Return the lines of `text` that invoke a boot or pool primitive in command position."""

    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if SHELL_PRIMITIVE_RE.search(stripped):
            hits.append(stripped)
    return hits


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The identities of every docstring constant, so prose about a command is not a command."""

    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if not body or not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            ids.add(id(first))
    return ids


def _python_hits(source: str) -> list[str]:
    """Return the string literals in `source` that name a boot or pool primitive."""

    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        value = node.value.strip()
        if value in BARE_COMMANDS or PYTHON_PRIMITIVE_RE.search(value):
            hits.append(value)
    return hits


def _normalized(text: str) -> str:
    """Collapse whitespace so a claim wrapped across two lines still matches."""

    return re.sub(r"\s+", " ", text)


def _section(path: Path, heading: str) -> str:
    """
    Return the body of the Markdown section introduced by `heading`.

    Scoped to the section so a claim that moves out of it -- or a section deleted outright --
    fails here rather than quietly matching the same words elsewhere in the file.
    """

    text = path.read_text(encoding="utf-8")
    if heading not in text:
        raise AssertionError(f"{path.name} no longer contains the heading {heading!r}")
    level = len(heading.split(" ", 1)[0])

    body: list[str] = []
    fenced = False
    for line in text.split(heading, 1)[1].splitlines()[1:]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(rf"#{{1,{level}}} ", line):
            break
        body.append(line)
    return "\n".join(body)


def _numbered_steps(section: str) -> list[str]:
    """The ordered-list items of `section`, in order, with their numbers dropped."""

    return [
        match.group("text").strip()
        for match in re.finditer(r"^\d+\.\s+(?P<text>.+)$", section, re.MULTILINE)
    ]


def _job_block(path: Path, job: str) -> str:
    """The indented body of one workflow job, read as text."""

    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line == f"  {job}:":
            body = []
            for candidate in lines[index + 1 :]:
                if candidate.strip() and not candidate.startswith("   "):
                    break
                body.append(candidate)
            return "\n".join(body)
    raise AssertionError(f"{path.name} no longer defines a `{job}` job")


def _defaults() -> dict[str, str]:
    return json.loads((REPO_ROOT / "ci" / "defaults.json").read_text(encoding="utf-8"))


def _tracked(pattern: str) -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", pattern],
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in listing.stdout.split()]


class ShellScannerTests(unittest.TestCase):
    """
    The case table for `_shell_hits()`.

    Every "the pipeline never does this" assertion below is a statement about what this scanner
    returned, so a scanner that silently matches nothing would turn them all green while checking
    nothing at all. The negative cases are not hypothetical: each is a line that exists in this
    tree today and must not be read as an executed command.
    """

    def test_a_bare_invocation_is_found(self) -> None:
        line = "zpool create testpool /dev/vdb"
        self.assertEqual(_shell_hits(line), [line])

    def test_a_sudo_invocation_is_found(self) -> None:
        self.assertTrue(_shell_hits("          sudo modprobe zfs"))

    def test_an_invocation_after_a_separator_is_found(self) -> None:
        self.assertTrue(_shell_hits("set -e && bootc switch ghcr.io/danathar/zfs:latest"))
        self.assertTrue(_shell_hits("losetup -f pool.img; zpool import -d . testpool"))

    def test_a_comment_about_a_command_is_not_a_command(self) -> None:
        # Both lines are in this tree: build_files/build-image.sh and build-branch.yml.
        self.assertEqual(_shell_hits("# without requiring a manual modprobe."), [])
        self.assertEqual(
            _shell_hits("      # switched to on fresh VMs with plain `bootc switch`"), []
        )

    def test_bootc_container_lint_is_not_a_boot(self) -> None:
        # The Containerfile's own composition check. Flagging it would make the document's
        # supporting evidence read as its refutation.
        self.assertEqual(_shell_hits("RUN bootc container lint"), [])

    def test_a_filename_containing_zfs_is_not_a_zfs_command(self) -> None:
        self.assertEqual(_shell_hits("  /ctx/files/usr/lib/modules-load.d/zfs.conf \\"), [])
        self.assertEqual(_shell_hits("python3 /ctx/containerfiles/zfs-akmods/install_zfs.py"), [])

    def test_a_mention_inside_a_sentence_is_not_a_command(self) -> None:
        self.assertEqual(
            _shell_hits("      about: Signature verification and bootc switch steps."), []
        )


class PythonScannerTests(unittest.TestCase):
    """The case table for `_python_hits()`."""

    def test_a_subprocess_argument_list_is_found(self) -> None:
        self.assertEqual(_python_hits('run(["zpool", "import", "tank"])'), ["zpool"])

    def test_a_command_string_is_found(self) -> None:
        self.assertEqual(_python_hits('run("bootc upgrade --apply")'), ["bootc upgrade --apply"])

    def test_a_module_docstring_is_not_a_command(self) -> None:
        # ci_tools/promote_stable.py's docstring mentions `bootc upgrade` time.
        source = '"""than on a user machine at bootc upgrade time."""\n'
        self.assertEqual(_python_hits(source), [])

    def test_a_function_docstring_is_not_a_command(self) -> None:
        source = 'def f():\n    """Runs before a zpool import elsewhere."""\n    return 1\n'
        self.assertEqual(_python_hits(source), [])

    def test_a_comment_is_not_a_command(self) -> None:
        self.assertEqual(_python_hits("# nothing here runs modprobe\nx = 1\n"), [])

    def test_an_unrelated_string_is_not_a_command(self) -> None:
        self.assertEqual(_python_hits('p = "/lib/modules/6.16.3/extra/zfs"\n'), [])


class SectionExtractorTests(unittest.TestCase):
    def test_every_section_the_file_reads_is_non_empty(self) -> None:
        # An extractor that returned "" would make every `assertIn` below vacuous.
        for heading in (CURRENT_BOUNDARY, SEQUENCE, FUTURE_AUTOMATION):
            self.assertGreater(len(_section(PROPOSAL, heading).strip()), 150, heading)

    def test_a_section_stops_at_the_next_heading(self) -> None:
        self.assertNotIn(SEQUENCE, _section(PROPOSAL, CURRENT_BOUNDARY))

    def test_a_missing_heading_is_an_error_rather_than_an_empty_string(self) -> None:
        with self.assertRaises(AssertionError):
            _section(PROPOSAL, "## A section nobody wrote")

    def test_the_numbered_steps_are_read_in_order(self) -> None:
        self.assertEqual(
            _numbered_steps("intro\n\n1. first\n2. second\n\ntrailing prose\n"),
            ["first", "second"],
        )


class CurrentBoundaryTests(unittest.TestCase):
    """
    The first section states what CI does and what it refuses to do. Both halves are recomputed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.section = _normalized(_section(PROPOSAL, CURRENT_BOUNDARY))

    def test_the_section_still_names_the_three_validations(self) -> None:
        for claim in ("composition", "signatures", "package and module invariants"):
            self.assertIn(claim, self.section)

    def test_composition_is_validated_where_the_document_implies(self) -> None:
        containerfile = CONTAINERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "RUN bootc container lint",
            containerfile,
            "The proposal says the pipeline validates composition. `bootc container lint` is "
            "the check that makes that true; it is gone from the Containerfile.",
        )
        inventory = REPO_ROOT / "build_files" / "check-brew-payload-inventory.sh"
        self.assertIn(inventory.name, containerfile)
        self.assertTrue(inventory.is_file())

    def test_signatures_are_verified_before_latest_moves(self) -> None:
        source = PROMOTE_STABLE.read_text(encoding="utf-8")
        self.assertIn("def verify_candidate_signature(", source)
        call = source.index("    verify_candidate_signature(")
        stable = source.index(':latest"')
        self.assertLess(
            call,
            stable,
            "The proposal's boundary is that signatures are validated before `latest` moves. "
            "promote_stable.py now resolves the stable reference before verifying the candidate.",
        )
        self.assertIn(
            "Verify the published :latest signature",
            (WORKFLOW_DIR / "nightly-compliance.yml").read_text(encoding="utf-8"),
        )

    def test_package_and_module_invariants_are_enforced_by_the_installer(self) -> None:
        tree = ast.parse(AKMODS_INSTALLER.read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        for name in ("build_install_plan", "validate_installed_modules"):
            self.assertIn(
                name,
                functions,
                "The proposal credits the pipeline with package and module invariants. "
                f"{AKMODS_INSTALLER.name} no longer defines `{name}`, which is where they live.",
            )
            raises = [
                node
                for node in ast.walk(functions[name])
                if isinstance(node, ast.Raise)
            ]
            self.assertTrue(
                raises,
                f"`{name}` no longer refuses anything, so it is not an invariant any more.",
            )

    def test_the_base_image_the_document_names_is_the_one_configured(self) -> None:
        self.assertIn("Fedora Kinoite", self.section)
        self.assertIn("kinoite", _defaults()["DEFAULT_BASE_IMAGE"])

    def test_no_pipeline_file_boots_a_deployment_or_touches_a_pool(self) -> None:
        self.assertIn(
            "does not boot a Fedora Kinoite deployment or import a real ZFS pool", self.section
        )
        hits = {}
        for path in _line_oriented_pipeline_files():
            found = _shell_hits(path.read_text(encoding="utf-8"))
            if found:
                hits[str(path.relative_to(REPO_ROOT))] = found
        for path in _python_pipeline_files():
            found = _python_hits(path.read_text(encoding="utf-8"))
            if found:
                hits[str(path.relative_to(REPO_ROOT))] = found
        self.assertEqual(
            hits,
            {},
            "docs/runtime-validation-proposal.md says the pipeline neither boots a deployment "
            "nor imports a pool before `latest` moves, and docs/install-and-verify.md repeats "
            "that as a testing-only warning. Something in CI now runs one of those primitives. "
            "If that is deliberate, both documents have to be rewritten in the same change.",
        )

    def test_the_scan_covers_the_files_it_claims_to(self) -> None:
        # A scan over an empty list is a scan that proves nothing.
        line_oriented = _line_oriented_pipeline_files()
        self.assertGreaterEqual(len(line_oriented), 15)
        self.assertIn(BUILD, line_oriented)
        self.assertIn(CONTAINERFILE, line_oriented)
        python_files = _python_pipeline_files()
        self.assertGreaterEqual(len(python_files), 15)
        self.assertIn(PROMOTE_STABLE, python_files)
        self.assertIn(AKMODS_INSTALLER, python_files)

    def test_the_shipped_image_scripts_are_outside_the_scan_by_design(self) -> None:
        # `files/` runs on a booted machine, which is the far side of the boundary. If it were
        # scanned, the modules-load.d fragment would read as CI loading the ZFS module.
        scanned = {*_line_oriented_pipeline_files(), *_python_pipeline_files()}
        for path in _tracked("files/*"):
            self.assertNotIn(path, scanned)
        self.assertTrue(MODULES_LOAD_FRAGMENT.is_file())

    def test_every_runner_is_github_hosted(self) -> None:
        self.assertIn("a hosted runner is not a safe place", self.section)
        labels = set()
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            labels.update(
                re.findall(
                    r"^\s*runs-on:\s*(?P<label>\S+)",
                    path.read_text(encoding="utf-8"),
                    re.MULTILINE,
                )
            )
        self.assertTrue(labels)
        self.assertEqual(
            sorted(label for label in labels if "self-hosted" in label),
            [],
            "The proposal's reasoning rests on CI running on hosted runners. A self-hosted "
            "runner appeared, which is exactly the case the section says would need separate "
            "hardware, device isolation and credential boundaries decided first.",
        )


class RecommendedSequenceTests(unittest.TestCase):
    """
    The six steps, each joined to the document that actually carries its command.

    The proposal abbreviates; docs/install-and-verify.md and docs/safety-model.md are what an
    operator runs. A step whose command has been renamed or dropped at the source leaves the
    proposal telling someone to perform a procedure this repository no longer documents.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.section = _section(PROPOSAL, SEQUENCE)
        cls.steps = _numbered_steps(cls.section)
        cls.install = INSTALL_AND_VERIFY.read_text(encoding="utf-8")
        cls.safety = SAFETY_MODEL.read_text(encoding="utf-8")

    def test_the_sequence_is_still_six_ordered_steps(self) -> None:
        self.assertEqual(len(self.steps), 6, self.steps)

    def test_step_one_verify_is_the_command_install_and_verify_documents(self) -> None:
        self.assertIn("signature", self.steps[0])
        self.assertIn("cosign verify", self.install)
        self.assertIn("--new-bundle-format=false", self.install)

    def test_step_two_switches_with_the_enforcement_flag(self) -> None:
        self.assertIn("enforced signature policy", self.steps[1])
        self.assertIn("bootc switch --enforce-container-sigpolicy", self.install)

    def test_step_three_reboots(self) -> None:
        self.assertIn("reboot", self.steps[2])
        self.assertIn("systemctl reboot", self.install)

    def test_step_four_checks_the_versions_install_and_verify_prints(self) -> None:
        self.assertIn("kernel", self.steps[3])
        for command in ("rpm -q kmod-zfs", "modinfo zfs", "zpool --version", "zfs --version"):
            self.assertIn(
                command,
                self.install,
                "The proposal's step 4 is the quick validation block in "
                "docs/install-and-verify.md; that block no longer runs this command.",
            )

    def test_step_five_loads_the_module_and_creates_a_disposable_pool(self) -> None:
        self.assertIn("disposable", _normalized(self.section))
        self.assertIn("zpool create", self.install)
        # The module loads itself on a booted machine, which is why the step reads as one action.
        self.assertTrue(MODULES_LOAD_FRAGMENT.is_file())
        self.assertIn(
            "disposable VM, disposable pool",
            _normalized(self.install),
            "Both documents have to keep saying the pool is disposable. That agreement is the "
            "whole safety argument for running step 5 at all.",
        )

    def test_step_six_rolls_back_with_the_command_safety_model_documents(self) -> None:
        self.assertIn("rollback", self.steps[5])
        self.assertIn("bootc rollback", self.safety)

    def test_the_two_documents_name_different_device_models(self) -> None:
        """
        The proposal recommends a loopback-backed pool; the only pool procedure in the tree uses
        a secondary block device. Neither is wrong, and nothing here prefers one. This pins the
        disagreement so that closing it -- by adding a loopback procedure, or by rewording the
        step -- is a deliberate edit rather than something a reader discovers mid-validation.
        """

        self.assertIn("loopback-backed pool", self.section)
        self.assertIn("/dev/vdb", self.install)
        loopback = sorted(
            path.name
            for path in DOCS_DIR.glob("*.md")
            if "losetup" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            loopback,
            [],
            "A document now describes a loopback device. If that is the proposal's step 5 "
            "being written up, docs/install-and-verify.md's secondary-disk procedure and this "
            "test should say which one an operator is meant to follow.",
        )

    def test_the_production_pool_prohibition_is_stated(self) -> None:
        self.assertIn(
            "Do not point automated tests at production pool devices", _normalized(self.section)
        )

    def test_the_self_hosted_caveat_is_stated_alongside_the_sequence(self) -> None:
        # The caveat sits under this heading rather than the boundary section, because it is
        # about the machine an operator would have to build to automate these six steps.
        self.assertIn("self-hosted runner would require", _normalized(self.section))


class FutureAutomationTests(unittest.TestCase):
    """
    The closing sentence is a claim about `promote-stable`'s gates, and it is falsifiable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.section = _normalized(_section(PROPOSAL, FUTURE_AUTOMATION))

    def test_the_five_things_a_future_gate_must_document_are_still_named(self) -> None:
        for requirement in (
            "device model",
            "cleanup guarantees",
            "signature boundary",
            "rollback behavior",
            "failure handling",
        ):
            self.assertIn(requirement, self.section)

    def test_no_runtime_validation_gate_has_taken_the_operators_place(self) -> None:
        self.assertIn("the operator remains the final runtime validation step", self.section)
        needs = re.findall(
            r"^      - (?P<job>\S+)$", _job_block(BUILD, "promote-stable"), re.MULTILINE
        )
        self.assertEqual(
            sorted(needs),
            ["build-candidate-image", "build-zfs-akmods", "sign-akmods-cache"],
            "The proposal's last sentence says the operator is still the final runtime "
            "validation step, and 'Until then' makes that conditional on no workflow gate "
            "existing. promote-stable's gates changed. If one of them boots the image, this "
            "document's three sections all need rewriting.",
        )

    def test_the_document_map_still_describes_the_file_it_points_at(self) -> None:
        entry = [
            line
            for line in DOC_MAP.read_text(encoding="utf-8").splitlines()
            if PROPOSAL.name in line
        ]
        self.assertEqual(len(entry), 1, entry)
        self.assertIn("after it boots", entry[0])
        self.assertIn("reboot", _section(PROPOSAL, SEQUENCE))


if __name__ == "__main__":
    unittest.main()
