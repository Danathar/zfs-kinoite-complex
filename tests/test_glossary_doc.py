"""
Script: tests/test_glossary_doc.py
What: Joins docs/glossary.md to the values it defines -- environment-variable names, image
refs, tag formats, paths, step orders and tool invocations -- by recomputing each one from
the tree.
Doing: Parses every bullet on the page into (terms, definition), then checks each claim
against the thing that owns it: `ci/defaults.json` for the image refs and the ZFS minor
line, every `require_env`/`optional_env`/`require_env_or_default`/`os.environ` read in
`ci_tools/` and `shared/` for the variable names the helpers actually consume (and, in the
other direction, those reads plus `write_github_env` exports, `Containerfile` `ARG`s and
the workflows' own `env:` blocks for whether an entry still names anything), the real
`build_candidate_tag` and `export_registry_context_values` for the two worked examples,
module constants for the paths, `.github/workflows/build.yml` for the rechunk ordering,
and the composite actions and shell scripts for the commands the page says this
repository runs.
Why: A glossary is the page a reader is sent to when a name in a log, a workflow input or a
build-inputs manifest means nothing to them. Every line of it is a hand-copy of something
that lives in the machine, and a hand-copy that nothing checks drifts by *omission* -- the
reader looks a name up, does not find it, and concludes the name is not part of this system.
Goal: Make a renamed variable, a moved default, a changed tag format, a reordered build or a
dropped tool fail here, instead of leaving the glossary describing a pipeline this repository
no longer has.

Before this file, `docs/glossary.md` was opened by no test as a subject. The three
occurrences of the path under `tests/` read it as a *source*: tests/test_licensing_doc.py
asserts `docs/licensing.md` links to it and that the file exists,
tests/test_editorconfig.py uses the path as a fixture for the `*` glob and for
`trim_trailing_whitespace`, and tests/test_issue_templates.py names it in a docstring
sentence. Existence, one inbound link and the editor settings were pinned. The 112 terms
were not, and five names had already gone missing when this file was written:

  * `Resolved Run Inputs` defined the five `BASE_IMAGE_*` values and the three
    `BUILD_CONTAINER_*` values but not `BREW_IMAGE_REF`, `BREW_IMAGE_PINNED` or
    `BREW_IMAGE_DIGEST`. Brew is the third digest-pinned image in the build:
    `ci_tools/write_build_inputs_manifest.py` requires all three, so three keys of
    `artifacts/build-inputs.json` were undefined by the page a reader opens beside it.
  * `Signing And Registry` defined `IMAGE_ORG` but not `IMAGE_REGISTRY` or `ACTOR_IS_BOT`,
    the other two values the same `write_github_env` call in
    `ci_tools/tagging_context.py` exports.
  * `zfs_version` was documented in its workflow-input spelling only. The helpers read the
    value as `ZFS_VERSION`, which the page never named, so a grep for the variable a failing
    helper reports found nothing.

What this file does not restate: link and anchor resolution (owned by
tests/test_docs_consistency.py), the `.editorconfig` treatment of this path (owned by
tests/test_editorconfig.py), and the behaviour of the helpers imported below, each of which
has its own test module. They are imported here to compute an expected value, never to be
tested through this page.

No PyYAML, for the reason tests/test_docs_consistency.py gives: the CI job installs pytest,
pytest-cov and ruff by name, so an optional parser import would start skipping silently the
day that changed. Workflow and action facts are read with the small helpers below, which
carry their own fixture cases in `ParserTests`. Comments are stripped before any workflow
assertion: several of these values appear in prose beside the line that sets them, and a
comment that satisfies a search is a test that cannot fail.

Each assertion was falsified before it was trusted: 38 mutants were applied to the committed
tree one at a time -- each of the five added entries deleted again, the stable-signal ref
moved off the base image, the candidate tag widened to eight SHA characters, the akmods
worktree path changed, `bootc container lint` removed from the Containerfile, the rechunk
step renamed so the ordering no longer resolves, the Chunkah pin repointed at another owner,
`DEFAULT_AKMODS_REF` wired in as a real `workflow_dispatch` input, a `just` call stripped of
its working directory, and the rest. All 38 were caught.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from ci_tools.tagging_context import (
    actor_is_bot,
    build_candidate_tag,
    export_registry_context_values,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "glossary.md"
DEFAULTS = REPO_ROOT / "ci" / "defaults.json"
CONTAINERFILE = REPO_ROOT / "Containerfile"
BUILD_IMAGE_SH = REPO_ROOT / "build_files" / "build-image.sh"
CI_TOOLS = REPO_ROOT / "ci_tools"
SHARED = REPO_ROOT / "shared"
GITHUB_DIR = REPO_ROOT / ".github"
WORKFLOW_DIR = GITHUB_DIR / "workflows"
ACTION_DIR = GITHUB_DIR / "actions"
BUILD_YML = WORKFLOW_DIR / "build.yml"
RECHUNK_ACTION = ACTION_DIR / "rechunk-native-image" / "action.yml"
PREPARE_AKMODS_ACTION = ACTION_DIR / "prepare-main-akmods" / "action.yml"
MANIFEST_WRITER = CI_TOOLS / "write_build_inputs_manifest.py"
TAGGING_CONTEXT = CI_TOOLS / "tagging_context.py"
RESOLVE_BUILD_INPUTS = CI_TOOLS / "resolve_build_inputs.py"
PROMOTE_STABLE = CI_TOOLS / "promote_stable.py"
CONFIGURE_ZFS_TARGET = CI_TOOLS / "akmods_configure_zfs_target.py"
CLONE_PINNED = CI_TOOLS / "akmods_clone_pinned.py"
ZFS_INSTALLER = REPO_ROOT / "containerfiles" / "zfs-akmods" / "install_zfs_from_akmods_cache.py"
TMPFILES_DIR = REPO_ROOT / "files" / "usr" / "lib" / "tmpfiles.d"

# A bullet is one or more backticked terms, joined by ` / ` when the page gives a term more
# than one spelling, then `: ` and the definition. Definitions contain colons of their own,
# so the term half has to be matched by shape rather than by splitting on the first colon.
BULLET_RE = re.compile(r"^- ((?:`[^`]+`)(?: / `[^`]+`)*): (.*)$")
BACKTICKED_RE = re.compile(r"`([^`]+)`")
UPPER_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
UPPER_WORD_RE = re.compile(r"\b[A-Z][A-Z0-9_]*\b")

# The three helpers in `ci_tools/common.py` that read one named environment variable.
ENV_READERS = frozenset({"require_env", "optional_env", "require_env_or_default"})

ENV_SECTION = "Configuration And Environment Variables"
COMMAND_SECTION = "Command Glossary"

# The three variables GitHub itself sets. They appear in the manifest writer because a run
# record names the run; the page is about this repository's own vocabulary, so it does not
# define them and should not have to.
GITHUB_PROVIDED = frozenset(
    {
        "GITHUB_ACTOR",
        "GITHUB_ENV",
        "GITHUB_EVENT_NAME",
        "GITHUB_OUTPUT",
        "GITHUB_REF",
        "GITHUB_REF_NAME",
        "GITHUB_REPOSITORY",
        "GITHUB_REPOSITORY_OWNER",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_SERVER_URL",
        "GITHUB_SHA",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_WORKFLOW",
    }
)


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def defaults() -> dict[str, str]:
    return json.loads(DEFAULTS.read_text(encoding="utf-8"))


def strip_yaml_comments(text: str) -> str:
    """
    Drop whole-line `#` comments and trailing ` #` comments from YAML.

    Several of the values asserted below are also discussed in a comment beside the line
    that sets them -- `AKMODS_KERNEL`, the Chunkah pin, the rechunk ordering. A search that
    a comment satisfies is a search that cannot fail, so every workflow assertion reads the
    stripped text.
    """

    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def strip_shell_comments(text: str) -> list[str]:
    """Return the non-comment, non-blank lines of a shell script."""

    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def bullets(text: str) -> list[tuple[list[str], str]]:
    """Return `(terms, definition)` for every top-level bullet on the page."""

    parsed = []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        match = BULLET_RE.match(line)
        if match is None:
            raise AssertionError(f"bullet does not parse: {line!r}")
        parsed.append((BACKTICKED_RE.findall(match.group(1)), match.group(2)))
    return parsed


def section(text: str, heading: str) -> str:
    """
    Return the body of the `##` section whose title is `heading`, including its `###`
    subsections, up to the next `##` heading or the end of the page.
    """

    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = index + 1
            break
    if start is None:
        raise AssertionError(f"no section titled {heading!r}")
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end])


def definition_of(text: str, term: str) -> str:
    """Return the definition the page gives for one term."""

    for terms, body in bullets(text):
        if term in terms:
            return body
    raise AssertionError(f"no bullet defines {term!r}")


def defined_variables(text: str) -> set[str]:
    """
    Return every variable name the environment-variable sections define as a bullet of its
    own, in both the spelling the page uses and its uppercase form.

    Only a bullet term counts. A name that merely appears inside someone else's definition
    is a cross-reference, not an entry, and accepting one would let the page satisfy this
    join while the reader still cannot look the name up. The uppercase form is added because
    `zfs_version` is defined in its workflow-input spelling and reaches the helpers as
    `ZFS_VERSION`; the page says so in that entry, which
    `test_the_zfs_version_entry_names_the_environment_spelling` checks separately.
    """

    body = section(text, ENV_SECTION)
    found = set()
    for terms, _ in bullets(body):
        for term in terms:
            found.add(term)
            found.add(term.upper())
    return {name for name in found if UPPER_NAME_RE.match(name)}


def tracked_files() -> list[Path]:
    """Every tracked file, read from the checkout rather than from git."""

    skip = {".git", "__pycache__"}
    return [
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file() and not any(part in skip for part in path.parts)
    ]


def implementation_text() -> str:
    """The tracked tree outside `docs/` and `tests/`, concatenated."""

    chunks = []
    for path in tracked_files():
        relative = path.relative_to(REPO_ROOT)
        if relative.parts[0] in {"docs", "tests"}:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(chunks)


def assign_constant(path: Path, name: str) -> str:
    """Return the string a module assigns to `name` at module level."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Constant):
                    return str(value.value)
                # `Path("/tmp/akmods")` and friends: the literal is the first argument.
                if isinstance(value, ast.Call) and value.args:
                    first = value.args[0]
                    if isinstance(first, ast.Constant):
                        return str(first.value)
                if isinstance(value, ast.Tuple) and value.elts:
                    first = value.elts[0]
                    if isinstance(first, ast.Constant):
                        return str(first.value)
    raise AssertionError(f"{path.name} assigns no module-level {name}")


def calls_named(path: Path, function: str) -> list[ast.Call]:
    """Every call to `function` in a module, by name."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function
    ]


def _is_environ(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ"


def env_reads(tree: ast.AST) -> set[str]:
    """
    Every literal variable name a module reads from the process environment: the first
    argument of `require_env`, `optional_env` and `require_env_or_default`, of
    `os.environ.get(...)`, and the key of an `os.environ[...]` read. A computed name is not
    a read this join can see, and an `os.environ[...] = ...` store is not a read at all.
    """

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            by_helper = isinstance(func, ast.Name) and func.id in ENV_READERS
            by_get = (
                isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value)
            )
            if (by_helper or by_get) and node.args and isinstance(node.args[0], ast.Constant):
                found.add(node.args[0].value)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if _is_environ(node.value) and isinstance(node.slice, ast.Constant):
                found.add(node.slice.value)
    return found


def read_variables() -> set[str]:
    """Every variable name `ci_tools/` and `shared/` read from the environment."""

    found = set()
    for package in (CI_TOOLS, SHARED):
        for path in sorted(package.glob("*.py")):
            found |= env_reads(ast.parse(path.read_text(encoding="utf-8")))
    return found


def exported_variables() -> set[str]:
    """Every key `ci_tools/` writes to `GITHUB_ENV` through `write_github_env`."""

    found = set()
    for path in sorted(CI_TOOLS.glob("*.py")):
        for call in calls_named(path, "write_github_env"):
            if call.args and isinstance(call.args[0], ast.Dict):
                found.update(
                    key.value for key in call.args[0].keys if isinstance(key, ast.Constant)
                )
    return found


def workflow_variables() -> set[str]:
    """
    Every upper-case name in the comment-stripped workflows and composite actions: an
    `env:` key, a `${{ secrets.NAME }}` or `env.NAME` expression, or a `$NAME` in a run
    block. Over-matching is harmless here -- the set is only ever asked whether a name the
    page defines is in it.
    """

    found = set()
    for path in sorted([*WORKFLOW_DIR.glob("*.yml"), *ACTION_DIR.glob("*/action.yml")]):
        found.update(UPPER_WORD_RE.findall(strip_yaml_comments(path.read_text(encoding="utf-8"))))
    return found


def argv_lists(path: Path) -> list[list[str]]:
    """Every literal argv list handed to `run_cmd` in a module."""

    lists = []
    for call in calls_named(path, "run_cmd"):
        if not call.args or not isinstance(call.args[0], ast.List):
            continue
        parts = [
            element.value
            for element in call.args[0].elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if parts:
            lists.append(parts)
    return lists


def containerfile_args() -> dict[str, str]:
    """The `ARG NAME="value"` defaults declared by the root Containerfile."""

    found = {}
    for line in CONTAINERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^ARG\s+([A-Z][A-Z0-9_]*)="?([^"]*)"?\s*$', line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def yaml_scalars(text: str, key: str) -> list[str]:
    """Every value assigned to `key` as a YAML mapping key, at any indentation."""

    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(\S.*?)\s*$")
    return [match.group(1) for line in text.splitlines() if (match := pattern.match(line))]


def step_names(text: str) -> list[str]:
    """The `- name:` step labels of a workflow or action, in file order."""

    pattern = re.compile(r"^\s*- name:\s*(\S.*?)\s*$")
    return [match.group(1) for line in text.splitlines() if (match := pattern.match(line))]


class ParserTests(unittest.TestCase):
    """The helpers above are hand-rolled, so each one carries its own fixture cases."""

    def test_bullets_splits_terms_from_definitions(self) -> None:
        parsed = bullets("- `a`: first: with a colon\n- `b` / `c`: second\nnot a bullet\n")
        self.assertEqual(parsed, [(["a"], "first: with a colon"), (["b", "c"], "second")])

    def test_bullets_rejects_a_bullet_whose_term_is_not_backticked(self) -> None:
        with self.assertRaises(AssertionError):
            bullets("- plain: definition\n")

    def test_section_returns_one_section_including_its_subsections(self) -> None:
        page = "## One\nbody one\n### Sub\nbody sub\n## Two\nbody two\n"
        self.assertEqual(section(page, "One"), "body one\n### Sub\nbody sub")
        self.assertEqual(section(page, "Two"), "body two")

    def test_section_refuses_a_heading_that_is_not_there(self) -> None:
        with self.assertRaises(AssertionError):
            section("## One\nbody\n", "Three")

    def test_strip_yaml_comments_drops_whole_line_and_trailing_comments(self) -> None:
        stripped = strip_yaml_comments("# lead\nkey: value # trailing\n  other: two\n")
        self.assertEqual(stripped, "key: value\n  other: two")

    def test_strip_shell_comments_keeps_only_real_lines(self) -> None:
        self.assertEqual(strip_shell_comments("# c\n\n  run me\n"), ["  run me"])

    def test_yaml_scalars_reads_a_key_at_any_indentation(self) -> None:
        self.assertEqual(yaml_scalars("a: 1\n    a: 2\nb: 3\n", "a"), ["1", "2"])

    def test_yaml_scalars_ignores_a_key_that_only_looks_like_one(self) -> None:
        self.assertEqual(yaml_scalars("prefix_a: 1\na:\n", "a"), [])

    def test_step_names_reads_labels_in_file_order(self) -> None:
        self.assertEqual(step_names("  - name: One\n    env:\n  - name: Two\n"), ["One", "Two"])

    def test_env_reads_sees_each_reader_shape_and_only_literal_names(self) -> None:
        source = (
            "a = require_env('A')\n"
            "b = optional_env('B', '')\n"
            "c = require_env_or_default('C')\n"
            "d = os.environ.get('D', '')\n"
            "e = os.environ['E']\n"
            "os.environ['STORED'] = 'x'\n"
            "f = require_env(name)\n"
            "g = other.get('G')\n"
        )
        self.assertEqual(env_reads(ast.parse(source)), {"A", "B", "C", "D", "E"})


class PageStructureTests(unittest.TestCase):
    def test_every_bullet_on_the_page_parses(self) -> None:
        self.assertGreater(len(bullets(doc_text())), 100)

    def test_no_term_is_defined_twice(self) -> None:
        terms = [term for names, _ in bullets(doc_text()) for term in names]
        duplicates = sorted({term for term in terms if terms.count(term) > 1})
        self.assertEqual(duplicates, [], f"terms defined more than once: {duplicates}")

    def test_the_page_carries_no_fenced_block(self) -> None:
        # Every backtick scan above pairs ticks across a whole line. A fence would make the
        # text between two claims look like one backticked token.
        self.assertNotIn("```", doc_text())


class EnvironmentVariableTests(unittest.TestCase):
    def test_every_variable_ci_tools_and_shared_read_is_documented(self) -> None:
        """
        A helper that starts reading a new variable has to write it down on this page,
        because the name shows up in a workflow `env:` block and in that helper's own
        "Missing required environment variable" error, and the glossary is where a reader
        is sent to look it up.
        """

        read = read_variables()
        # One name per reader shape, so a read moved to a form the walk cannot see fails
        # here instead of quietly narrowing the join.
        self.assertIn("IMAGE_TAG", read)  # require_env
        self.assertIn("KCPATH", read)  # optional_env
        self.assertIn("AKMODS_UPSTREAM_REF", read)  # require_env_or_default
        self.assertIn("COSIGN_PASSWORD", read)  # os.environ.get
        missing = sorted(read - defined_variables(doc_text()) - GITHUB_PROVIDED)
        self.assertEqual(missing, [], f"read from the environment, undefined on the page: {missing}")

    def test_every_defined_variable_is_still_read_exported_declared_or_set(self) -> None:
        """
        An entry has to name something the machine still uses: a name `ci_tools/` or
        `shared/` read, a value they export to `GITHUB_ENV`, an `ARG` of the root
        `Containerfile`, or a name a workflow or composite action sets or tests. Prose
        elsewhere in the tree does not count: a name that survives only in a README
        sentence or a YAML comment is exactly the stale entry this join exists to catch.
        """

        live = (
            read_variables()
            | exported_variables()
            | set(containerfile_args())
            | workflow_variables()
        )
        stale = sorted(defined_variables(doc_text()) - live)
        self.assertEqual(stale, [], f"defined by the page, used nowhere: {stale}")

    def test_the_build_inputs_manifest_names_only_documented_variables(self) -> None:
        """
        `artifacts/build-inputs.json` is the file a reader opens with this page beside them.
        Every value in its `inputs` block arrives as an environment variable, so every one
        of those names has to be a name the page writes down.
        """

        defined = defined_variables(doc_text())
        required = {
            call.args[0].value
            for call in calls_named(MANIFEST_WRITER, "require_env")
            if call.args and isinstance(call.args[0], ast.Constant)
        }
        self.assertIn("BREW_IMAGE_DIGEST", required)
        missing = sorted(required - defined - GITHUB_PROVIDED)
        self.assertEqual(missing, [], f"written to the manifest, undefined by the page: {missing}")

    def test_the_zfs_version_entry_names_the_environment_spelling(self) -> None:
        """
        `zfs_version` is the workflow input and the resolver output; the helpers read the
        same value as `ZFS_VERSION`. Only one of those spellings is a bullet term, so the
        entry has to carry the other or a reader greps for what a failing helper printed
        and finds nothing.
        """

        self.assertIn("`ZFS_VERSION`", definition_of(doc_text(), "zfs_version"))

    def test_every_value_ci_tools_exports_to_github_env_is_documented(self) -> None:
        defined = defined_variables(doc_text())
        exported = exported_variables()
        self.assertEqual(exported, {"IMAGE_ORG", "IMAGE_REGISTRY", "ACTOR_IS_BOT"})
        missing = sorted(exported - defined)
        self.assertEqual(missing, [], f"exported to GITHUB_ENV, undefined by the page: {missing}")

    def test_detected_kernel_releases_is_read_as_a_space_separated_list(self) -> None:
        self.assertIn("space-separated list", definition_of(doc_text(), "DETECTED_KERNEL_RELEASES"))
        tree = ast.parse(MANIFEST_WRITER.read_text(encoding="utf-8"))
        split_on = [
            node.func.value.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "split"
            and isinstance(node.func.value, ast.Call)
            and node.func.value.args
            and isinstance(node.func.value.args[0], ast.Constant)
        ]
        self.assertEqual(split_on, ["DETECTED_KERNEL_RELEASES"])


class CheckedInValueTests(unittest.TestCase):
    def test_the_stable_signal_entry_names_the_image_the_build_consumes(self) -> None:
        """
        The entry's own last sentence is the invariant: the gate must watch the image the
        build actually uses. So the ref it quotes has to be both defaults at once.
        """

        definition = definition_of(doc_text(), "stable signal")
        quoted = [token for token in BACKTICKED_RE.findall(definition) if "/" in token]
        self.assertEqual(len(quoted), 1, f"expected one image ref in the entry, got {quoted}")
        values = defaults()
        self.assertEqual(quoted[0], values["STABLE_SIGNAL_IMAGE"])
        self.assertEqual(quoted[0], values["DEFAULT_BASE_IMAGE"])
        self.assertIn("DEFAULT_BASE_IMAGE", definition)

    def test_checked_in_defaults_points_at_the_file_that_holds_them(self) -> None:
        definition = definition_of(doc_text(), "checked-in defaults")
        self.assertIn("`ci/defaults.json`", definition)
        self.assertIsInstance(defaults(), dict)

    def test_the_fork_entry_names_the_upstream_repository(self) -> None:
        definition = definition_of(doc_text(), "fork")
        quoted = [token for token in BACKTICKED_RE.findall(definition) if "/" in token]
        self.assertEqual(quoted, ["Danathar/akmods"])
        upstream = defaults()["AKMODS_UPSTREAM_REPO"]
        self.assertEqual(upstream, f"https://github.com/{quoted[0]}.git")

    def test_the_zfs_minor_version_example_is_the_checked_in_default(self) -> None:
        definition = definition_of(doc_text(), "ZFS_MINOR_VERSION")
        example = re.search(r"for example `([^`]+)`", definition)
        self.assertIsNotNone(example)
        self.assertEqual(example.group(1), defaults()["DEFAULT_ZFS_MINOR_VERSION"])

    def test_the_zfs_version_example_sits_on_the_documented_minor_line(self) -> None:
        definition = definition_of(doc_text(), "zfs_version")
        example = re.search(r"for example `([^`]+)`", definition)
        self.assertIsNotNone(example)
        minor = defaults()["DEFAULT_ZFS_MINOR_VERSION"]
        self.assertTrue(
            example.group(1).startswith(f"{minor}."),
            f"{example.group(1)} is not a patch release on the {minor} line",
        )
        self.assertIn("(../ci_tools/zfs_release.py)", definition)
        self.assertTrue((REPO_ROOT / "ci_tools" / "zfs_release.py").is_file())

    def test_the_akmods_image_template_carries_the_fedora_placeholder(self) -> None:
        definition = definition_of(doc_text(), "AKMODS_IMAGE_TEMPLATE")
        self.assertIn("`{fedora}`", definition)
        from_containerfile = containerfile_args()["AKMODS_IMAGE_TEMPLATE"]
        from_module = assign_constant(ZFS_INSTALLER, "DEFAULT_AKMODS_IMAGE_TEMPLATE")
        self.assertEqual(from_containerfile, from_module)
        self.assertIn("{fedora}", from_module)

    def test_the_brew_image_entry_matches_where_each_default_lives(self) -> None:
        definition = definition_of(doc_text(), "BREW_IMAGE")
        self.assertIn("`DEFAULT_BREW_IMAGE`", definition)
        self.assertIn("`ci/defaults.json`", definition)
        self.assertIn("@sha256:", defaults()["DEFAULT_BREW_IMAGE"])
        # "the Containerfile default is a local-build convenience only" -- a floating tag.
        self.assertNotIn("@sha256:", containerfile_args()["BREW_IMAGE"])

    def test_the_signing_key_filename_default_is_the_image_name(self) -> None:
        definition = definition_of(doc_text(), "SIGNING_KEY_FILENAME")
        self.assertIn("installed into the image", definition)
        self.assertEqual(
            containerfile_args()["SIGNING_KEY_FILENAME"], f"{defaults()['IMAGE_NAME']}.pub"
        )
        installs = [
            line
            for line in strip_shell_comments(BUILD_IMAGE_SH.read_text(encoding="utf-8"))
            if "SIGNING_KEY_FILENAME" in line and "cosign.pub" in line
        ]
        self.assertEqual(len(installs), 1, "build-image.sh installs cosign.pub exactly once")

    def test_the_temporary_checkout_path_is_the_akmods_worktree(self) -> None:
        definition = definition_of(doc_text(), "temporary checkout")
        quoted = [token for token in BACKTICKED_RE.findall(definition) if token.startswith("/")]
        self.assertEqual(quoted, ["/tmp/akmods"])
        for module in (CLONE_PINNED, CONFIGURE_ZFS_TARGET):
            self.assertEqual(assign_constant(module, "AKMODS_WORKTREE"), quoted[0])

    def test_the_akmods_kernel_and_target_values_are_the_ones_the_action_passes(self) -> None:
        action = strip_yaml_comments(PREPARE_AKMODS_ACTION.read_text(encoding="utf-8"))
        self.assertEqual(yaml_scalars(action, "AKMODS_KERNEL"), ["main"])
        self.assertEqual(yaml_scalars(action, "AKMODS_TARGET"), ["zfs"])
        self.assertIn("`main`", definition_of(doc_text(), "AKMODS_KERNEL"))
        self.assertIn("`zfs`", definition_of(doc_text(), "AKMODS_TARGET"))

    def test_default_akmods_ref_is_not_wired_as_a_workflow_input(self) -> None:
        """The entry makes a negative claim, so the test has to be the negative search."""

        definition = definition_of(doc_text(), "DEFAULT_AKMODS_REF")
        self.assertIn("not wired as a formal workflow-dispatch input", definition)
        offenders = [
            path.relative_to(REPO_ROOT)
            for path in sorted(GITHUB_DIR.rglob("*.yml"))
            if "DEFAULT_AKMODS_REF" in strip_yaml_comments(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], f"DEFAULT_AKMODS_REF is wired in {offenders}")
        read_by = {
            call.args[0].value
            for call in calls_named(RESOLVE_BUILD_INPUTS, "optional_env")
            if call.args and isinstance(call.args[0], ast.Constant)
        }
        self.assertIn("DEFAULT_AKMODS_REF", read_by)


class WorkedExampleTests(unittest.TestCase):
    def test_the_candidate_tag_example_is_what_the_helper_builds(self) -> None:
        definition = definition_of(doc_text(), "tag")
        example = re.search(r"`(candidate-[0-9a-f]+-\d+)`", definition)
        self.assertIsNotNone(example, f"no candidate tag example in: {definition}")
        short_sha, fedora_version = example.group(1).split("-")[1:]
        rebuilt = build_candidate_tag(
            github_sha=short_sha + "0" * (40 - len(short_sha)), fedora_version=fedora_version
        )
        self.assertEqual(rebuilt, example.group(1))

    def test_the_image_owner_example_is_what_the_registry_helper_builds(self) -> None:
        definition = definition_of(doc_text(), "image owner portion")
        owner = re.search(r"for example `([a-z0-9-]+)` in `(ghcr\.io/[^`]+)`", definition)
        self.assertIsNotNone(owner, f"no owner example in: {definition}")
        values = export_registry_context_values(
            repository_owner="Danathar", actor_name="a-person"
        )
        self.assertEqual(values["image_org"], owner.group(1))
        self.assertEqual(
            f"{values['image_registry']}/{defaults()['IMAGE_NAME']}", owner.group(2)
        )

    def test_the_automation_account_example_is_recognized_as_a_bot(self) -> None:
        definition = definition_of(doc_text(), "automation account")
        example = re.search(r"for example `([^`]+)`", definition)
        self.assertIsNotNone(example)
        self.assertTrue(actor_is_bot(example.group(1)))

    def test_the_stable_and_audit_tags_are_what_promotion_writes(self) -> None:
        source = PROMOTE_STABLE.read_text(encoding="utf-8")
        self.assertIn("`latest`", definition_of(doc_text(), "stable"))
        self.assertIn("immutable", definition_of(doc_text(), "audit tag"))
        self.assertIn('stable_ref = f"docker://ghcr.io/{image_org}/{image_name}:latest"', source)
        self.assertRegex(source, r'audit_ref = f"docker://ghcr\.io/\{image_org\}/\{image_name\}:stable-')
        # "written during promotion" -- and written first, so a cancelled job never leaves a
        # moved `latest` with no audit record behind it.
        self.assertLess(
            source.index("destination_ref=audit_ref"),
            source.index("destination_ref=stable_ref"),
        )


class PipelineClaimTests(unittest.TestCase):
    def test_rechunking_happens_after_the_lint_and_before_the_push(self) -> None:
        definition = definition_of(doc_text(), "rechunk")
        self.assertIn("after `bootc container lint`", definition)
        self.assertIn("before the image is pushed or signed", definition)
        names = step_names(strip_yaml_comments(BUILD_YML.read_text(encoding="utf-8")))
        for label in ("Build candidate image locally", "Rechunk candidate image with Chunkah"):
            self.assertEqual(names.count(label), 1, f"{label!r} is not a unique step name")
        self.assertEqual(names.count("Push and sign candidate image"), 1)
        self.assertLess(
            names.index("Build candidate image locally"),
            names.index("Rechunk candidate image with Chunkah"),
        )
        self.assertLess(
            names.index("Rechunk candidate image with Chunkah"),
            names.index("Push and sign candidate image"),
        )
        # The lint the entry names runs inside the build step, from the Containerfile.
        self.assertIn("RUN bootc container lint", CONTAINERFILE.read_text(encoding="utf-8"))

    def test_the_chunkah_link_is_the_repository_behind_the_pinned_image(self) -> None:
        definition = definition_of(doc_text(), "Chunkah")
        link = re.search(r"\[`([^`]+)`\]\(https://github\.com/([^)]+)\)", definition)
        self.assertIsNotNone(link, f"no linked repository in: {definition}")
        self.assertEqual(link.group(1), link.group(2))
        action = strip_yaml_comments(RECHUNK_ACTION.read_text(encoding="utf-8"))
        pins = [value for value in yaml_scalars(action, "default") if "chunkah" in value]
        self.assertEqual(len(pins), 1, f"expected one pinned Chunkah image, got {pins}")
        repository = pins[0].split("@", 1)[0].split(":", 1)[0].split("/", 1)[1]
        self.assertEqual(repository, link.group(2))

    def test_build_image_names_ostree_container_commit_only_in_comments(self) -> None:
        definition = definition_of(doc_text(), "ostree container commit")
        self.assertIn("no longer calls it directly", definition)
        live = [
            line
            for line in strip_shell_comments(BUILD_IMAGE_SH.read_text(encoding="utf-8"))
            if "ostree container commit" in line
        ]
        self.assertEqual(live, [], f"build-image.sh still runs it: {live}")

    def test_the_ci_entry_points_at_the_directory_that_holds_the_workflows(self) -> None:
        self.assertIn("`.github/workflows`", definition_of(doc_text(), "CI"))
        triggered = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in tracked_files()
            if path.suffix in {".yml", ".yaml"}
            and re.search(r"^on:", path.read_text(encoding="utf-8"), re.MULTILINE)
        }
        in_workflow_dir = {
            path.relative_to(REPO_ROOT).as_posix() for path in sorted(WORKFLOW_DIR.glob("*.yml"))
        }
        self.assertEqual(triggered, in_workflow_dir)

    def test_every_local_action_is_the_composite_kind_the_entry_describes(self) -> None:
        definition = definition_of(doc_text(), "composite action")
        self.assertIn("without moving logic out of version control", definition)
        actions = sorted(ACTION_DIR.glob("*/action.yml"))
        self.assertGreater(len(actions), 1)
        for action in actions:
            text = strip_yaml_comments(action.read_text(encoding="utf-8"))
            self.assertEqual(
                yaml_scalars(text, "using"), ["composite"], f"{action.parent.name} is not composite"
            )

    def test_the_tmpfiles_entry_creates_only_pcp_state_directories(self) -> None:
        definition = definition_of(doc_text(), "tmpfiles.d")
        self.assertIn("`pcp`", definition)
        configs = sorted(TMPFILES_DIR.glob("*.conf"))
        self.assertEqual(len(configs), 1, f"expected one shipped tmpfiles.d entry, got {configs}")
        paths = [
            line.split()[1]
            for line in strip_shell_comments(configs[0].read_text(encoding="utf-8"))
        ]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/var/lib/pcp"), f"{path} is not pcp state")


class CommandGlossaryTests(unittest.TestCase):
    def commands(self) -> list[str]:
        return [term for terms, _ in bullets(section(doc_text(), COMMAND_SECTION)) for term in terms]

    def test_every_command_the_page_defines_appears_in_the_tracked_tree(self) -> None:
        implementation = implementation_text()
        absent = sorted(
            command
            for command in self.commands()
            # A path prefix still counts as the command: build-image.sh reaches
            # `systemctl` as `/usr/bin/systemctl`, so `/` must not block the match.
            if not re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", implementation)
        )
        self.assertEqual(absent, [], f"defined by the page, absent from the tree: {absent}")

    def test_buildah_is_run_by_a_composite_action_as_the_entry_says(self) -> None:
        self.assertIn("used by the GitHub Action in this repo", definition_of(doc_text(), "buildah"))
        runners = [
            action.parent.name
            for action in sorted(ACTION_DIR.glob("*/action.yml"))
            if re.search(
                r"^\s*buildah build",
                strip_yaml_comments(action.read_text(encoding="utf-8")),
                re.MULTILINE,
            )
        ]
        self.assertEqual(runners, ["build-native-image"])

    def test_depmod_is_invoked_after_the_modules_are_installed(self) -> None:
        self.assertIn("Used after installing ZFS kernel modules", definition_of(doc_text(), "depmod"))
        self.assertIn("depmod", [argv[0] for argv in argv_lists(ZFS_INSTALLER)])

    def test_systemctl_preset_activates_the_brew_units_at_build_time(self) -> None:
        definition = definition_of(doc_text(), "systemctl preset")
        self.assertIn("Used to activate brew services at build time", definition)
        presets = [
            line.split()[-1]
            for line in strip_shell_comments(BUILD_IMAGE_SH.read_text(encoding="utf-8"))
            if "systemctl preset" in line
        ]
        self.assertTrue(presets, "build-image.sh runs no systemctl preset")
        for unit in presets:
            self.assertTrue(unit.startswith("brew"), f"{unit} is not a brew unit")

    def test_yq_is_what_writes_the_upstream_target_file(self) -> None:
        definition = definition_of(doc_text(), "yq")
        self.assertIn("used to update the upstream akmods target file", definition)
        self.assertEqual(assign_constant(CONFIGURE_ZFS_TARGET, "YQ"), "yq")
        self.assertIn("images.yaml", CONFIGURE_ZFS_TARGET.read_text(encoding="utf-8"))

    def test_just_only_ever_runs_inside_the_upstream_worktree(self) -> None:
        """
        The entry says `just` belongs to the *upstream* repository. That is only true while
        every invocation names the cloned worktree as its working directory.
        """

        self.assertIn("upstream akmods repository", definition_of(doc_text(), "just"))
        source = (CI_TOOLS / "akmods_build_and_publish.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        invocations = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "run_cmd" or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.List) or not first.elts:
                continue
            head = first.elts[0]
            if not (isinstance(head, ast.Constant) and head.value == "just"):
                continue
            invocations += 1
            cwd = [keyword for keyword in node.keywords if keyword.arg == "cwd"]
            self.assertEqual(len(cwd), 1, "a `just` invocation names no working directory")
            self.assertIn("AKMODS_WORKTREE", ast.get_source_segment(source, cwd[0].value) or "")
        self.assertGreater(invocations, 1)

    def test_bootc_container_lint_runs_from_the_containerfile(self) -> None:
        definition = definition_of(doc_text(), "bootc container lint")
        self.assertIn("bootc update system", definition)
        self.assertIn("RUN bootc container lint", CONTAINERFILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
