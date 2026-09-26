"""
Script: tests/test_workflow_env_contract.py
What: Joins every `python3 -m ci_tools.cli <command>` step to the environment that command reads.
Doing: Walks each command's `main()` through the `ci_tools`/`shared` call graph with `ast`,
collecting every `require_env("NAME")` it can reach and every `registry_creds_from_env` call,
then checks each workflow and composite-action step that runs the command. A required name must
be set by the step, its job, its workflow, an earlier `>> "$GITHUB_ENV"` line in the same job,
or be one of the variables the runner sets itself. A step whose command reads the registry
credential pair must set both halves, and a step that sets them must run a command that reads
them.
Why: The unit tests for each helper set the variables they need with `patch.dict`, so they pass
whatever a workflow hands the real command. Nothing joined the two. Dropping `REGISTRY_TOKEN`
from the `promote-stable` step, or adding a `require_env` to a helper without adding the
variable to its step, stayed green here and failed only when that job next ran on `main`. The
optional credential is quieter: `registry_creds_from_env()` returns `None` when either half is
unset, so a step that loses one half pulls anonymously, and against the private akmods cache a
failed `cosign verify` is reported as an unsigned cache rather than as an error.
Goal: A step and the command it runs cannot disagree about which variables exist.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("ci_tools", "shared")
CREDENTIAL_PAIR = frozenset({"REGISTRY_ACTOR", "REGISTRY_TOKEN"})
CLI_COMMAND = re.compile(r"python3 -m ci_tools\.cli ([a-z0-9-]+)")
GITHUB_ENV_WRITE = re.compile(r"""\b([A-Z][A-Z0-9_]*)=[^\n]*>>\s*"?\$\{?GITHUB_ENV\b""")

# Default variables the runner sets for every step, from GitHub's "Variables" reference.
# Only names some command here reads are listed; `GITHUB_TOKEN` is deliberately absent,
# because the runner does NOT export it -- a step has to map it from `github.token`.
RUNNER_DEFAULTS = frozenset(
    {
        "GITHUB_ACTOR",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
        "GITHUB_REF",
        "GITHUB_REF_NAME",
        "GITHUB_REPOSITORY",
        "GITHUB_REPOSITORY_OWNER",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW",
    }
)

# Steps that run a credential-reading command without setting the pair, each with the step
# that established registry login before it. Held in both directions below, so an entry whose
# step starts passing credentials, or stops following the login, fails instead of going dead.
LOGGED_IN_BY_EARLIER_STEP = {
    (
        ".github/actions/prepare-main-akmods/action.yml",
        "Verify the rebuilt cache matches the resolved ZFS version",
    ): "Build and publish shared self-hosted ZFS akmods image",
}

# Commands that receive the pair only to hand it to upstream tooling in a subprocess, so no
# `ci_tools` code reads it. Each must still visibly run that tooling's login.
PASSES_CREDENTIALS_TO_SUBPROCESS = {
    "akmods-build-and-publish": ("ci_tools/akmods_build_and_publish.py", '["just", "login"]'),
}


def _modules() -> dict[str, ast.Module]:
    modules = {}
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).glob("*.py")):
            modules[f"{package}.{path.stem}"] = ast.parse(path.read_text(encoding="utf-8"))
    return modules


MODULES = _modules()


def _functions() -> dict[tuple[str, str], ast.FunctionDef]:
    return {
        (module, node.name): node
        for module, tree in MODULES.items()
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


FUNCTIONS = _functions()


def _imported_names(tree: ast.AST) -> dict[str, tuple[str, str]]:
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in MODULES:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)
    return names


IMPORTS = {module: _imported_names(tree) for module, tree in MODULES.items()}


def _command_entry_points() -> dict[str, tuple[str, str]]:
    """Read `command_map()`'s dict literal: command text -> (module, function)."""

    command_map = FUNCTIONS[("ci_tools.cli", "command_map")]
    local = _imported_names(command_map)
    entries = {}
    for node in ast.walk(command_map):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                entries[key.value] = local[value.id]
    return entries


ENTRY_POINTS = _command_entry_points()


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


class _Reads:
    def __init__(self) -> None:
        self.required: set[str] = set()
        self.reads_credentials = False


def _reads(command: str) -> _Reads:
    """
    Everything `command` reads from the environment, over its reachable call graph.

    `registry_creds_from_env` is not descended into: its body names both halves under
    `require_env` for the `required=True` form only, so walking it would make every optional
    caller look like a required one. Its call site says which form is used.
    """

    reads = _Reads()
    seen: set[tuple[str, str]] = set()
    pending = [ENTRY_POINTS[command]]
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        module = key[0]
        for node in ast.walk(FUNCTIONS[key]):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name == "require_env":
                reads.required.add(node.args[0].value)
                continue
            if name == "registry_creds_from_env":
                reads.reads_credentials = True
                if any(kw.arg == "required" and _is_true(kw.value) for kw in node.keywords):
                    reads.required |= CREDENTIAL_PAIR
                continue
            target = (module, name) if (module, name) in FUNCTIONS else IMPORTS[module].get(name)
            if target in FUNCTIONS:
                pending.append(target)
    return reads


class _Step:
    def __init__(self, path: str, job: str, index: int, step: dict, inherited: set[str]):
        self.path = path
        self.job = job
        self.index = index
        self.name = step.get("name") or f"step {index}"
        self.run = step.get("run") or ""
        self.own_env = set(step.get("env") or {})
        self.env = inherited | self.own_env
        self.commands = CLI_COMMAND.findall(self.run)

    def __str__(self) -> str:
        return f"{self.path} [{self.job}] {self.name!r}"


def _steps() -> list[_Step]:
    """Every step of every workflow and composite action, with the env visible to it."""

    steps = []
    for path in sorted((REPO_ROOT / ".github").rglob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if "jobs" in document:
            jobs = [
                (name, set(document.get("env") or {}) | set(job.get("env") or {}), job)
                for name, job in document["jobs"].items()
            ]
            jobs = [(name, env, job.get("steps") or []) for name, env, job in jobs]
        elif isinstance(document.get("runs"), dict) and "steps" in document["runs"]:
            jobs = [("composite", set(), document["runs"]["steps"])]
        else:
            continue
        for job, inherited, raw_steps in jobs:
            exported: set[str] = set()
            for index, raw in enumerate(raw_steps):
                step = _Step(relative, job, index, raw, inherited | exported)
                steps.append(step)
                exported |= set(GITHUB_ENV_WRITE.findall(step.run))
    return steps


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class WorkflowEnvContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.steps = _steps()
        cls.cli_steps = [step for step in cls.steps if step.commands]

    def test_every_invoked_command_exists(self) -> None:
        """A step naming a command `cli.py` does not map would fail argparse at run time."""

        self.assertGreater(len(self.cli_steps), 15)
        for step in self.cli_steps:
            for command in step.commands:
                with self.subTest(step=str(step)):
                    self.assertIn(command, ENTRY_POINTS)

    def test_every_command_the_call_graph_reads_is_resolvable(self) -> None:
        """
        The walk only sees `require_env("LITERAL")` called by bare name.

        A computed name or a `common.require_env(...)` attribute call would drop out of the
        required set silently, so either shape fails here and has to be taught to the walk.
        """

        for (module, name), function in FUNCTIONS.items():
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "require_env",
                    "registry_creds_from_env",
                }:
                    self.fail(f"{module}.{name} calls {node.func.attr} through an attribute")
                if isinstance(node.func, ast.Name) and node.func.id == "require_env":
                    if (module, name) == ("ci_tools.common", "registry_creds_from_env"):
                        continue
                    with self.subTest(function=f"{module}.{name}"):
                        self.assertEqual(len(node.args), 1)
                        self.assertIsInstance(node.args[0], ast.Constant)
                        self.assertIsInstance(node.args[0].value, str)

    def test_every_required_variable_reaches_the_step(self) -> None:
        for step in self.cli_steps:
            for command in step.commands:
                missing = _reads(command).required - step.env - RUNNER_DEFAULTS
                with self.subTest(step=str(step), command=command):
                    self.assertEqual(
                        missing,
                        set(),
                        f"`{command}` calls require_env for these, but {step} does not set "
                        "them and the runner does not either",
                    )

    def test_credential_readers_are_given_both_halves(self) -> None:
        """
        Half a credential is read as none, so a step missing one half pulls anonymously.

        That is required-and-fatal only for `check-stable-signal` and `promote-stable`. The
        other readers fall back to `creds=None` without a word, which is why the pair is
        checked per step and not only through `require_env`.
        """

        for step in self.cli_steps:
            if not any(_reads(command).reads_credentials for command in step.commands):
                continue
            if (step.path, step.name) in LOGGED_IN_BY_EARLIER_STEP:
                continue
            with self.subTest(step=str(step)):
                self.assertLessEqual(CREDENTIAL_PAIR, step.env)

    def test_credentials_are_set_only_where_a_command_reads_them(self) -> None:
        for step in self.steps:
            given = CREDENTIAL_PAIR & step.own_env
            if not given:
                continue
            with self.subTest(step=str(step)):
                self.assertEqual(given, CREDENTIAL_PAIR, "a step sets one half of the pair")
                readers = [
                    command
                    for command in step.commands
                    if _reads(command).reads_credentials
                    or command in PASSES_CREDENTIALS_TO_SUBPROCESS
                ]
                self.assertTrue(readers, f"{step} sets the pair but runs nothing that reads it")

    def test_subprocess_credential_exemption_still_runs_the_login(self) -> None:
        for command, (path, login) in PASSES_CREDENTIALS_TO_SUBPROCESS.items():
            with self.subTest(command=command):
                self.assertIn(command, ENTRY_POINTS)
                self.assertFalse(_reads(command).reads_credentials)
                self.assertIn(login, (REPO_ROOT / path).read_text(encoding="utf-8"))

    def test_logged_in_exemptions_still_describe_the_workflow(self) -> None:
        """Each exempt step still reads credentials, sets none, and follows its login step."""

        by_key = {(step.path, step.name): step for step in self.steps}
        for key, login_name in LOGGED_IN_BY_EARLIER_STEP.items():
            with self.subTest(step=key):
                step = by_key[key]
                self.assertTrue(any(_reads(c).reads_credentials for c in step.commands))
                self.assertFalse(CREDENTIAL_PAIR & step.env)
                login = by_key[(key[0], login_name)]
                self.assertEqual(login.job, step.job)
                self.assertLess(login.index, step.index)
                self.assertTrue(any(c in PASSES_CREDENTIALS_TO_SUBPROCESS for c in login.commands))

    def test_the_walk_sees_the_known_credential_readers(self) -> None:
        """Pins the call-graph walk to the callers of `registry_creds_from_env`, in both forms."""

        readers = {command for command in ENTRY_POINTS if _reads(command).reads_credentials}
        self.assertEqual(
            readers,
            {
                "check-akmods-cache",
                "check-stable-signal",
                "prepare-validation-build",
                "promote-stable",
                "write-last-good-build-badge",
            },
        )
        required = {
            command for command in ENTRY_POINTS if CREDENTIAL_PAIR <= _reads(command).required
        }
        self.assertEqual(required, {"check-stable-signal", "promote-stable"})


if __name__ == "__main__":
    unittest.main()
