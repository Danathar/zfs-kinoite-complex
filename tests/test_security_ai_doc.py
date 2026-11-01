"""
Script: tests/test_security_ai_doc.py
What: Joins the "Secret inventory" table in docs/SECURITY-AI.md to the secrets the workflows
actually read.
Doing: Reads the first and second column of every row of that table, collects every
`secrets.NAME` a workflow references outside a comment, and checks the two lists against each
other: every secret a workflow reads has a row, every stored secret a row names is read by a
workflow the row's "Used by" cell names, and every `REGISTRY_TOKEN` is `${{ github.token }}`,
as the `GITHUB_TOKEN` row says.
Why: The table ends "Nothing else in CI is secret." and it is where a reader decides how bad a
leak would be. No test read it, so swapping a workflow's `github.token` for a new stored
secret passed the whole suite while the page went on saying the token is not a stored
credential.
Goal: A secret added to, renamed in or dropped from a workflow fails here until the inventory
says the same.

The "Signing keys and secrets" section is joined to build.yml the same way: the jobs it names
are the jobs the `production-signing` environment gates, and the key it says "exists only in
the signing steps" of those jobs is read as a value nowhere else -- not in a job-level `env:`,
not in another step, not in a `with:` input -- and only by a step that ends in the isolated
signer. tests/test_workflow_build_container.py holds sign-candidate-image's step env exactly;
nothing held sign-akmods-cache's, so moving the key up to that job's `env:`, where the
third-party login action and every other step would see it, passed the suite.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML; see test.yml
    yaml = None

from tests.test_docs_consistency import workflow_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
SECURITY_AI = REPO_ROOT / "docs" / "SECURITY-AI.md"

# `${{ secrets.NAME }}` and any expression that reads one, e.g. `secrets.NAME != ''`.
SECRET_REF_RE = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
CODE_SPAN_RE = re.compile(r"`([^`]+)`")
REGISTRY_TOKEN_RE = re.compile(r"^\s*REGISTRY_TOKEN:\s*(.+?)\s*$")
# Always present, minted per job; its row describes scope, not a stored credential.
MINTED = {"GITHUB_TOKEN"}

BUILD = REPO_ROOT / ".github" / "workflows" / "build.yml"
SIGNING_ENV = "production-signing"
SIGNER = "python3 -I ci_tools/sign_image.py"
KEY_VALUE = "${{ secrets.SIGNING_SECRET }}"
# A job may test that the secret is set; that yields a boolean, not the key.
PRESENCE_CHECK = "${{ secrets.SIGNING_SECRET != '' }}"


def inventory(text: str) -> dict[str, str]:
    """Each secret name the table's first column spells in backticks -> that row's "Used by" cell."""

    section = text.split("### Secret inventory", 1)[1]
    section = re.split(r"^#{2,3} ", section, maxsplit=1, flags=re.MULTILINE)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith(("| ---", "| Secret ")):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for name in CODE_SPAN_RE.findall(cells[0]):
            rows[name] = cells[1]
    return rows


def workflow_secrets() -> dict[str, set[str]]:
    """Each secret name a workflow reads -> the workflow files that read it. Comment lines are skipped."""

    found: dict[str, set[str]] = {}
    for path in workflow_paths():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name in SECRET_REF_RE.findall(line):
                found.setdefault(name, set()).add(path.name)
    return found


class InventoryReaderTests(unittest.TestCase):
    def test_reads_every_name_in_the_first_column_and_stops_at_the_next_heading(self) -> None:
        text = (
            "### Secret inventory\n\n| Secret | Used by | If it leaks |\n| --- | --- | --- |\n"
            "| `A` | `a.yml` | x |\n| Pair (`B` / `C`) | `b.yml` | y |\n\nNothing else.\n\n"
            "## Next\n\n| `D` | d | z |\n"
        )
        self.assertEqual(inventory(text), {"A": "`a.yml`", "B": "`b.yml`", "C": "`b.yml`"})


class SecretInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SECURITY_AI.read_text(encoding="utf-8")
        cls.rows = inventory(cls.text)
        cls.used = workflow_secrets()

    def test_the_page_still_says_nothing_else_is_secret(self) -> None:
        self.assertTrue(
            "Nothing else in CI is secret." in self.text,
            "the sentence this file backs is gone from docs/SECURITY-AI.md; update or drop these tests",
        )

    def test_every_secret_a_workflow_reads_has_a_row(self) -> None:
        missing = {name: sorted(files) for name, files in self.used.items() if name not in self.rows}
        self.assertEqual(
            missing,
            {},
            "workflows read secrets docs/SECURITY-AI.md's Secret inventory does not list; "
            "add a row saying what a leak of each would allow",
        )

    def test_every_stored_secret_row_is_read_by_the_workflows_it_names(self) -> None:
        for name, used_by in self.rows.items():
            if name in MINTED:
                continue
            with self.subTest(secret=name):
                files = self.used.get(name, set())
                self.assertTrue(files, f"{name} has a row but no workflow reads secrets.{name}")
                for file in sorted(files):
                    self.assertIn(file, used_by, f"{file} reads secrets.{name}; the row's Used by cell does not name it")

    def test_the_registry_token_is_the_job_token(self) -> None:
        self.assertTrue(
            "`REGISTRY_TOKEN` is `${{ github.token }}`" in self.text,
            "the GITHUB_TOKEN row no longer says what REGISTRY_TOKEN is; update this test with it",
        )
        values = {}
        for path in workflow_paths():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = REGISTRY_TOKEN_RE.match(line)
                if match:
                    values[f"{path.name}:{number}"] = match.group(1)
        self.assertTrue(values, "no workflow sets REGISTRY_TOKEN; the GITHUB_TOKEN row describes one that does")
        self.assertEqual({where: v for where, v in values.items() if v != "${{ github.token }}"}, {})



def signing_section(text: str) -> str:
    """The "Signing keys and secrets" prose, up to its "Secret inventory" subsection."""

    return text.split("## Signing keys and secrets\n", 1)[1].split("### Secret inventory", 1)[0]


def key_readers(where: object) -> bool:
    """Whether a parsed workflow value mentions the signing secret anywhere inside it."""

    return "secrets.SIGNING_SECRET" in repr(where)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class SigningKeySectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SECURITY_AI.read_text(encoding="utf-8")
        cls.section = signing_section(cls.text)
        cls.jobs = yaml.safe_load(BUILD.read_text(encoding="utf-8"))["jobs"]
        cls.gated = {name for name, job in cls.jobs.items() if job.get("environment") == SIGNING_ENV}

    def test_the_section_names_exactly_the_jobs_the_signing_environment_gates(self) -> None:
        self.assertTrue(self.gated, f"no build.yml job uses the {SIGNING_ENV} environment")
        named = {name for name in self.jobs if f"`{name}`" in self.section}
        self.assertEqual(named, self.gated)
        self.assertIn("`SIGNING_SECRET` environment secret", self.section)
        self.assertIn(f"`{SIGNER}`", self.section)

    def test_the_inventory_row_names_the_same_jobs(self) -> None:
        used_by = inventory(self.text)["SIGNING_SECRET"]
        self.assertEqual({name for name in self.jobs if f"`{name}`" in used_by}, self.gated)

    def test_no_job_level_env_holds_the_key(self) -> None:
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                env = {key: value for key, value in (job.get("env") or {}).items() if key_readers(value)}
                if name in self.gated:
                    self.assertEqual(env, {"HAS_SIGNING_SECRET": PRESENCE_CHECK})
                else:
                    self.assertEqual(env, {})

    def test_the_key_reaches_one_step_per_signing_job_and_that_step_runs_the_signer(self) -> None:
        for name, job in self.jobs.items():
            steps = job.get("steps") or []
            readers = [step for step in steps if key_readers(step)]
            with self.subTest(job=name):
                if name not in self.gated:
                    self.assertEqual(readers, [])
                    continue
                self.assertEqual(len(readers), 1, f"{name}: {[step.get('name') for step in readers]}")
                step = readers[0]
                self.assertNotIn("uses", step)
                self.assertNotIn("with", step)
                self.assertEqual(
                    {key for key, value in step["env"].items() if key_readers(value)}, {"COSIGN_PRIVATE_KEY"}
                )
                self.assertEqual(step["env"]["COSIGN_PRIVATE_KEY"], KEY_VALUE)
                self.assertNotIn("SIGNING_SECRET", step["run"])
                self.assertTrue(
                    step["run"].rstrip().endswith(SIGNER),
                    f"{name}: the step holding the key does not end in the isolated signer",
                )


if __name__ == "__main__":
    unittest.main()
