"""
Script: tests/test_install_and_verify_doc.py
What: Joins docs/install-and-verify.md -- and README.md's copy of its host bootstrap -- to the
build helper, workflows and files that its install, validation and branch-image steps describe.
Doing: Parses the fenced blocks an operator copies, runs the in-image signing-policy helper with
the inputs the build passes, and checks that the host bootstrap writes the same key path,
registries.d file, discovery YAML and policy rule the image writes for itself; then re-derives
the switch target, the post-boot checks, and the unsigned-branch and candidate-tag claims from
the code that implements them.
Why: The page is the one README sends an operator to before a first `bootc switch`, and no test
read it as a subject. Its references under `tests/` read one thing back out: the
`cosign verify` flags (tests/test_workflow_nightly_compliance.py, tests/test_quality_doc.py),
the testing-only boundary sentence (tests/test_runtime_validation_proposal_doc.py), and a
section heading (tests/test_signing_and_bootc_doc.py). Step 1 says the host commands are "the
same three artifacts the image itself writes at build time", and nothing compared them to
files/scripts/configure_signing_policy.py. If that helper renamed the discovery file, changed
the key filename rule or the rule's `signedIdentity`, the host would trust one thing and every
later `bootc upgrade` would read another, and the page would still read as correct.
Goal: Make a change to what the image trusts fail here until the copy an operator pastes on
the host says the same thing.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; see test.yml
    yaml = None

from ci_tools import tagging_context

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "install-and-verify.md"
README = REPO_ROOT / "README.md"
DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD_SCRIPT = REPO_ROOT / "build_files" / "build-image.sh"
POLICY_HELPER = REPO_ROOT / "files" / "scripts" / "configure_signing_policy.py"
BREW_PATH_FRAGMENT = REPO_ROOT / "files" / "etc" / "profile.d" / "brew-path.sh"
MODULES_LOAD = REPO_ROOT / "files" / "usr" / "lib" / "modules-load.d" / "zfs.conf"
ZFS_INSTALLER = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
PROMOTE = REPO_ROOT / "ci_tools" / "promote_stable.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BUILD = WORKFLOWS / "build.yml"
BUILD_BRANCH = WORKFLOWS / "build-branch.yml"
BUILD_WORKFLOWS = ("build.yml", "build-branch.yml", "build-pr.yml")

# Both files carry the host bootstrap an operator pastes before the first switch.
BOOTSTRAP_DOCS = (DOC, README)

FENCE_RE = re.compile(r"^```(\w*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
IMAGE_REF_RE = re.compile(r"\bghcr\.io/[a-z0-9._/-]+:[A-Za-z0-9._-]+")


def _load_policy_helper():
    spec = importlib.util.spec_from_file_location("configure_signing_policy", POLICY_HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POLICY = _load_policy_helper()


def _fences(text: str, lang: str) -> list[str]:
    return [body for tag, body in FENCE_RE.findall(text) if tag == lang]


def _section(text: str, heading: str) -> str:
    """Body of one heading, up to the next heading of the same or higher level.

    A `#` comment inside a fenced shell block is not a heading.
    """

    lines = text.splitlines(keepends=True)
    in_fence = False
    level = None
    body: list[str] = []
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else re.match(r"(#+) (.*)\n?$", line)
        if level is None:
            if match and match.group(2) == heading:
                level = len(match.group(1))
            continue
        if match and len(match.group(1)) <= level:
            break
        body.append(line)
    assert level is not None, f"no heading {heading!r}"
    return "".join(body)


def _containerfile_arg(name: str) -> str:
    match = re.search(rf'^ARG {name}="([^"]*)"$', CONTAINERFILE.read_text(encoding="utf-8"),
                      re.MULTILINE)
    assert match, f"Containerfile declares no ARG {name}"
    return match.group(1)


def _image_name() -> str:
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))["IMAGE_NAME"]


def _image_repo() -> str:
    return _containerfile_arg("IMAGE_REPO")


def _signing_key_filename() -> str:
    return _containerfile_arg("SIGNING_KEY_FILENAME")


class Bootstrap:
    """The commands of one doc's host bootstrap block, parsed into what they write."""

    def __init__(self, doc: Path) -> None:
        text = doc.read_text(encoding="utf-8")
        blocks = [b for b in _fences(text, "bash") if "registries.d" in b]
        assert len(blocks) == 1, f"{doc.name}: expected one bootstrap block, found {len(blocks)}"
        self.doc = doc
        self.block = blocks[0]
        self.installs: list[list[str]] = []
        self.dirs: list[list[str]] = []
        self.tees: list[tuple[str, str]] = []
        lines = iter(self.block.splitlines())
        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            heredoc = re.search(r"<<'(\w+)'$", line)
            argv = shlex.split(line.split("<<")[0].replace(">/dev/null", ""))
            assert argv[0] == "sudo", f"{doc.name}: unexpected bootstrap line {line!r}"
            argv = argv[1:]
            if heredoc:
                body = []
                for inner in lines:
                    if inner == heredoc.group(1):
                        break
                    body.append(inner)
                assert argv[0] == "tee", line
                self.tees.append((argv[1], "\n".join(body) + "\n"))
            elif argv[0] == "install" and "-d" in argv:
                self.dirs.append(argv)
            elif argv[0] == "install":
                self.installs.append(argv)
            else:
                raise AssertionError(f"{doc.name}: unclassified bootstrap command {line!r}")
        policy_blocks = _fences(text, "json")
        assert len(policy_blocks) == 1, f"{doc.name}: expected one policy.json snippet"
        self.policy_entry = json.loads("{" + policy_blocks[0] + "}")


class ImageInputsTests(unittest.TestCase):
    """The build inputs the rest of this file feeds the helper."""

    def test_the_containerfile_defaults_are_what_ci_passes(self) -> None:
        # Every build workflow passes image_repo as ghcr.io/<image_org>/<image_name> and the key
        # filename as <image_name>.pub. The Containerfile defaults are what a local build uses and
        # what this file feeds the helper, so they must be the same values.
        for name in BUILD_WORKFLOWS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding="utf-8")
                self.assertIn(
                    "image_repo: ghcr.io/${{ steps.registry.outputs.image_org }}/"
                    "${{ steps.defaults.outputs.image_name }}",
                    text,
                )
                self.assertIn(
                    "signing_key_filename: ${{ steps.defaults.outputs.image_name }}.pub", text
                )
        org = tagging_context.export_registry_context_values(
            repository_owner=_image_repo().split("/")[1], actor_name="someone"
        )["image_org"]
        self.assertEqual(_image_repo(), f"ghcr.io/{org}/{_image_name()}")
        self.assertEqual(_signing_key_filename(), f"{_image_name()}.pub")

    def test_the_build_hands_the_helper_those_inputs(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('IMAGE_REPO="${IMAGE_REPO}"', script)
        self.assertIn('SIGNING_KEY_FILENAME="${SIGNING_KEY_FILENAME}"', script)
        self.assertIn("python3 /ctx/files/scripts/configure_signing_policy.py", script)


class HostBootstrapTests(unittest.TestCase):
    """Step 1: the host carries "the same three artifacts the image itself writes"."""

    def setUp(self) -> None:
        self.repo = _image_repo()
        self.key_path = POLICY.DEFAULT_KEYS_DIR / _signing_key_filename()

    def test_the_key_lands_where_the_image_installs_it(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'install -m 0644 /ctx/cosign.pub "/etc/pki/containers/${SIGNING_KEY_FILENAME}"', script
        )
        for doc in BOOTSTRAP_DOCS:
            with self.subTest(doc=doc.name):
                boot = Bootstrap(doc)
                self.assertEqual(len(boot.installs), 1, boot.installs)
                argv = boot.installs[0]
                self.assertEqual(argv[-2], "cosign.pub")
                self.assertTrue((REPO_ROOT / argv[-2]).is_file())
                self.assertEqual(argv[-1], str(self.key_path))
                mode = re.search(r"-\w*m(\d{4})", " ".join(argv[1:-2]))
                self.assertIsNotNone(mode, f"{doc.name}: key install sets no mode")
                self.assertEqual(mode.group(1), "0644")

    def test_the_discovery_directory_matches_the_image(self) -> None:
        script = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("install -d -m 0755 /etc/pki/containers /etc/containers/registries.d", script)
        for doc in BOOTSTRAP_DOCS:
            with self.subTest(doc=doc.name):
                boot = Bootstrap(doc)
                self.assertEqual(
                    boot.dirs, [["install", "-d", "-m", "0755", str(POLICY.DEFAULT_REGISTRIES_DIR)]]
                )

    def test_the_discovery_file_is_the_one_the_image_writes(self) -> None:
        expected_path = POLICY.registry_file_path(
            image_repo=self.repo, registries_dir=POLICY.DEFAULT_REGISTRIES_DIR
        )
        with tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp) / "discovery.yaml"
            POLICY.write_registry_discovery_file(image_repo=self.repo, registry_file=written)
            image_text = written.read_text(encoding="utf-8")
        image_lines = [line for line in image_text.splitlines() if not line.startswith("#")]
        for doc in BOOTSTRAP_DOCS:
            with self.subTest(doc=doc.name):
                boot = Bootstrap(doc)
                self.assertEqual(len(boot.tees), 1, boot.tees)
                path, body = boot.tees[0]
                self.assertEqual(path, str(expected_path))
                self.assertEqual(body.splitlines(), image_lines)
                if yaml is not None:
                    self.assertEqual(yaml.safe_load(body), yaml.safe_load(image_text))

    def test_the_policy_rule_is_the_one_the_image_writes(self) -> None:
        image_policy = POLICY.update_policy(policy_data={}, image_repo=self.repo,
                                            key_path=self.key_path)
        expected = {self.repo: image_policy["transports"]["docker"][self.repo]}
        for doc in BOOTSTRAP_DOCS:
            with self.subTest(doc=doc.name):
                self.assertEqual(Bootstrap(doc).policy_entry, expected)

    def test_the_policy_file_named_is_the_one_the_image_edits(self) -> None:
        for doc in BOOTSTRAP_DOCS:
            with self.subTest(doc=doc.name):
                prose = " ".join(doc.read_text(encoding="utf-8").split())
                self.assertIn(
                    f"`transports.docker` map in `{POLICY.DEFAULT_POLICY_FILE}`", prose
                )

    def test_three_artifacts_means_the_three_the_helper_and_build_write(self) -> None:
        # Key (build-image.sh), discovery file and policy rule (the helper). The doc's count and
        # the bootstrap's targets both have to match, so a fourth trust file in the image fails
        # here until the host step carries it too.
        text = " ".join(DOC.read_text(encoding="utf-8").split())
        self.assertIn("the same three artifacts the image itself writes at build time", text)
        boot = Bootstrap(DOC)
        targets = {boot.installs[0][-1], boot.tees[0][0], str(POLICY.DEFAULT_POLICY_FILE)}
        self.assertEqual(len(targets), 3)
        helper_writes = re.findall(r"\b(\w+)\.write_text\(", POLICY_HELPER.read_text(encoding="utf-8"))
        self.assertEqual(sorted(helper_writes), ["policy_path", "registry_file"])

    def test_the_fork_note_names_the_build_argument_that_sets_the_key_filename(self) -> None:
        text = " ".join(_section(DOC.read_text(encoding="utf-8"), "Step 1: Trust The Signing Key On The Host").split())
        self.assertIn("a key filename matching that fork's `SIGNING_KEY_FILENAME`", text)
        _containerfile_arg("SIGNING_KEY_FILENAME")
        self.assertIn(': "${SIGNING_KEY_FILENAME:?', BUILD_SCRIPT.read_text(encoding="utf-8"))


class SwitchAndVerifyTargetTests(unittest.TestCase):
    """Every image an operator is told to switch to or verify is this repository's stable tag."""

    def test_every_documented_image_ref_is_the_published_stable_tag(self) -> None:
        promote = PROMOTE.read_text(encoding="utf-8")
        self.assertIn('stable_ref = f"docker://ghcr.io/{image_org}/{image_name}:latest"', promote)
        for doc in BOOTSTRAP_DOCS:
            text = doc.read_text(encoding="utf-8")
            refs = [ref for block in _fences(text, "bash") for ref in IMAGE_REF_RE.findall(block)]
            with self.subTest(doc=doc.name):
                self.assertTrue(refs, f"{doc.name} names no image to switch to")
                self.assertEqual(set(refs), {f"{_image_repo()}:latest"})

    def test_the_switch_enforces_the_policy_step_1_wrote(self) -> None:
        # Every copy an operator pastes; the plain switch belongs only to the unsigned
        # branch-image section, which gives it in prose and never as a block.
        for doc in BOOTSTRAP_DOCS:
            switches = [
                shlex.split(line)
                for block in _fences(doc.read_text(encoding="utf-8"), "bash")
                for line in block.splitlines()
                if "bootc switch" in line
            ]
            with self.subTest(doc=doc.name):
                self.assertEqual(len(switches), 1, switches)
                self.assertEqual(switches[0][:3], ["sudo", "bootc", "switch"])
                self.assertIn("--enforce-container-sigpolicy", switches[0])

    def test_the_in_page_anchor_resolves(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("(#signature-verification)", text)
        self.assertRegex(text, r"(?m)^## Signature Verification$")


class QuickValidationTests(unittest.TestCase):
    """The post-boot checks name what the image actually installs."""

    def setUp(self) -> None:
        blocks = _fences(_section(DOC.read_text(encoding="utf-8"), "Quick Validation After Boot"),
                         "bash")
        self.checks = blocks[0].splitlines()

    def test_the_rpm_query_names_the_package_the_installer_requires(self) -> None:
        self.assertIn("rpm -q kmod-zfs", self.checks)
        self.assertIn('rpm_name_lookup(rpm_path) == "kmod-zfs"',
                      ZFS_INSTALLER.read_text(encoding="utf-8"))

    def test_lsmod_can_see_zfs_because_the_image_loads_it_at_boot(self) -> None:
        self.assertIn("lsmod | grep '^zfs'", self.checks)
        entries = [line for line in MODULES_LOAD.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.startswith("#")]
        self.assertEqual(entries, ["zfs"])
        self.assertIn("/usr/lib/modules-load.d/zfs.conf", BUILD_SCRIPT.read_text(encoding="utf-8"))

    def test_the_brew_note_describes_the_fragment_the_image_installs(self) -> None:
        self.assertIn("brew --version", self.checks)
        text = " ".join(DOC.read_text(encoding="utf-8").split())
        fragment = BREW_PATH_FRAGMENT.read_text(encoding="utf-8")
        self.assertIn("`/etc/profile.d/brew-path.sh` puts it on `PATH` for that account only", text)
        self.assertIn(
            "install -D -m 0644 \\\n  /ctx/files/etc/profile.d/brew-path.sh \\\n"
            "  /etc/profile.d/brew-path.sh",
            BUILD_SCRIPT.read_text(encoding="utf-8"),
        )
        # "for that account only": the fragment is gated on owning the prefix.
        self.assertIn("if [ -O /home/linuxbrew/.linuxbrew ]", fragment)
        by_path = re.search(r"\(`(/[^`]+/brew)`\)", text)
        self.assertIsNotNone(by_path, "the note gives no by-path spelling for root")
        self.assertIn(str(Path(by_path.group(1)).parent), fragment)


class BranchAndCandidateImageTests(unittest.TestCase):
    """The unsigned `br-*` rules and the signed `candidate-*` alternative."""

    def setUp(self) -> None:
        self.section = " ".join(
            _section(DOC.read_text(encoding="utf-8"), "Testing An Unsigned Branch Image").split()
        )

    def test_branch_tags_are_br_prefixed(self) -> None:
        self.assertIn("`br-*` branch tags are **deliberately unsigned**", self.section)
        prefix = tagging_context.build_branch_metadata("feature/x")
        tag = tagging_context.build_branch_image_tag(branch_tag_prefix=prefix, fedora_version="44")
        self.assertTrue(tag.startswith("br-"), tag)

    def test_branch_images_are_published_through_the_unsigned_opt_in(self) -> None:
        self.assertIn("publish test images through an explicit unsigned opt-in", self.section)
        text = BUILD_BRANCH.read_text(encoding="utf-8")
        step = text[text.index("- name: Push unsigned branch test image"):]
        step = step[: step.index("\n      - name:", 1)] if "\n      - name:" in step else step
        self.assertIn('allow_unsigned: "true"', step)

    def test_a_signed_host_refuses_br_tags_because_they_share_its_repository(self) -> None:
        # The refusal in rule 1 holds only because br-* tags land in the SAME repository the
        # host's policy rule covers (matchRepository). A branch image pushed elsewhere would
        # fall through to the host's default and be accepted.
        self.assertIn("will refuse to pull an unsigned `br-*` tag", self.section)
        self.assertEqual(
            POLICY.update_policy(policy_data={}, image_repo="r", key_path=Path("k"))
            ["transports"]["docker"]["r"][0]["signedIdentity"],
            {"type": "matchRepository"},
        )
        for name in ("build.yml", "build-branch.yml"):
            with self.subTest(workflow=name):
                self.assertIn(
                    "image_repo: ghcr.io/${{ steps.registry.outputs.image_org }}/"
                    "${{ steps.defaults.outputs.image_name }}",
                    (WORKFLOWS / name).read_text(encoding="utf-8"),
                )

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_the_signed_alternative_is_a_dispatch_that_signs_but_does_not_promote(self) -> None:
        self.assertIn("`workflow_dispatch` and `promote_to_stable=false`", self.section)
        self.assertIn("signed `candidate-*` tag", self.section)
        workflow = yaml.safe_load(BUILD.read_text(encoding="utf-8"))
        on = workflow.get("on", workflow.get(True))
        self.assertEqual(on["workflow_dispatch"]["inputs"]["promote_to_stable"]["type"], "boolean")
        jobs = workflow["jobs"]
        # The candidate is built and signed whatever promote_to_stable says ...
        self.assertEqual(jobs["build-candidate-image"]["environment"], "production-signing")
        self.assertNotIn("promote_to_stable", str(jobs["build-candidate-image"].get("if", "")))
        # ... and only moving `latest` reads it.
        self.assertIn("github.event.inputs.promote_to_stable == 'true'", jobs["promote-stable"]["if"])
        tag = tagging_context.build_candidate_tag(github_sha="0" * 40, fedora_version="44")
        self.assertTrue(tag.startswith("candidate-"), tag)


if __name__ == "__main__":
    unittest.main()
