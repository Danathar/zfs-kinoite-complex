"""
Script: tests/test_ai_fix_stop_list.py
What: Joins the "Stop and explain" list in the prompt of .github/workflows/ai-fix.yml to
the "Requires a human decision first" list in docs/SECURITY-AI.md.
Doing: Reads each bold-led item of the SECURITY-AI.md list, maps it through a ledger to
the phrases the workflow's prompt must carry for it, and set-compares the ledger to the
list so a new item fails until it is mapped.
Why: The prompt is the only text the unattended agent is certain to have in front of it,
and its list reads as complete ("Those are Tier 3 in docs/risk-tiers.md and need a human
first"). It was written as a copy of the SECURITY-AI.md list, and nothing joined the
two: #285 added "Changing what an agent may run without a prompt" to SECURITY-AI.md and
the prompt's copy did not move.
Goal: Make an item added to SECURITY-AI.md's human-decision list, or a phrase dropped
from the prompt's copy of it, fail here.

PENDING records an item the prompt does not carry yet. The comparison there runs the
other way -- it asserts the phrase is still missing -- so the change that adds it to the
workflow fails this file until the entry moves from PENDING to STOP_LIST_PHRASES. The
ledger can only shrink by someone doing the work.

PyYAML is guarded the way tests/test_workflow_ai_fix_preflight.py guards it: CI installs
it by name, and the suite still runs under `python3 -m unittest discover -s tests` with
nothing installed.
"""

from __future__ import annotations

import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None

from tests.test_docs_consistency import REPO_ROOT

SECURITY_AI = REPO_ROOT / "docs" / "SECURITY-AI.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ai-fix.yml"

LIST_INTRO = "Requires a human decision first"
BOLD_LEAD_RE = re.compile(r"^- \*\*(.+?)\*\*")
STOP_BULLET_LEAD = "- Stop and explain"

# Each bold lead of the SECURITY-AI.md list, with the phrases the prompt must carry
# for it. `where` is "stop" for the "Stop and explain" bullet, or "prompt" for an item
# the prompt states elsewhere as a refusal rather than a stop.
STOP_LIST_PHRASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Anything that weakens a fail-closed check.": (
        "prompt",
        ("Do not relax the check", "AGENTS.md section 0 rule 1"),
    ),
    "Changing the ZFS line, the kernel it builds against, or anything pool-facing.": (
        "stop",
        ("change the ZFS line or anything pool-facing",),
    ),
    # These are the seven files; the prompt names the rule that lists them.
    "Changing what gets signed, how tags propagate, or the promotion path": (
        "stop",
        ("seven files in AGENTS.md section 0 rule 2",),
    ),
    "Widening any workflow's `permissions:` block": (
        "stop",
        ("widen any workflow's `permissions:` block", "add a secret to a job that had none"),
    ),
    "Lowering a coverage floor": (
        "stop",
        ("lower a floor in .coverage-thresholds.json",),
    ),
    "Adding a runtime dependency.": (
        "stop",
        ("add a runtime dependency",),
    ),
}

# Items the prompt's stop list does not carry yet, with the phrases it will need.
# Adding them is a workflow change the bot that wrote this file cannot push; see #290.
PENDING: dict[str, tuple[str, ...]] = {
    "Changing what an agent may run without a prompt": (
        ".claude/settings.json",
        ".claude/hooks/",
        ".claude/commands/",
        "tests/run_tests.py",
    ),
}


def squash(text: str) -> str:
    """Collapse runs of whitespace, so a phrase matches across a wrapped line."""
    return re.sub(r"\s+", " ", text).strip()


def human_decision_leads() -> list[str]:
    """Return the bold lead of every item in SECURITY-AI.md's human-decision list."""
    lines = SECURITY_AI.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(LIST_INTRO)]
    if len(starts) != 1:
        raise AssertionError(f"expected one {LIST_INTRO!r} line, found {len(starts)}")
    items: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("- "):
            items.append(line)
        elif items and line.startswith("  "):
            items[-1] += "\n" + line
        elif items:
            break
    leads: list[str] = []
    for item in items:
        match = BOLD_LEAD_RE.match(squash(item))
        if not match:
            raise AssertionError(f"item without a bold lead: {item!r}")
        leads.append(match.group(1))
    return leads


def agent_prompt() -> str:
    """Return the `prompt:` input of the one step in ai-fix.yml that has one."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    prompts = [
        step["with"]["prompt"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "prompt" in step.get("with", {})
    ]
    if len(prompts) != 1:
        raise AssertionError(f"expected one step with a prompt, found {len(prompts)}")
    return prompts[0]


def stop_bullet(prompt: str) -> str:
    """Return the prompt's "Stop and explain" bullet through its continuation lines."""
    lines = prompt.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith(STOP_BULLET_LEAD)]
    if len(starts) != 1:
        raise AssertionError(f"expected one {STOP_BULLET_LEAD!r} bullet, found {len(starts)}")
    first = lines[starts[0]]
    indent = len(first) - len(first.lstrip())
    body = [first]
    for line in lines[starts[0] + 1 :]:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("- ") or len(line) - len(stripped) <= indent:
            break
        body.append(line)
    return squash("\n".join(body))


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class AiFixStopListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = squash(agent_prompt())
        self.stop = stop_bullet(agent_prompt())

    def text_for(self, where: str) -> str:
        return self.stop if where == "stop" else self.prompt

    def test_every_human_decision_item_is_in_the_ledger(self) -> None:
        leads = human_decision_leads()
        self.assertEqual(len(leads), len(set(leads)), leads)
        ledger = set(STOP_LIST_PHRASES) | set(PENDING)
        self.assertEqual(
            set(leads),
            ledger,
            "docs/SECURITY-AI.md's human-decision list and this file's ledger disagree. "
            "An item added there needs the prompt in .github/workflows/ai-fix.yml to carry "
            "it, and an entry here naming the phrases that do.",
        )
        self.assertFalse(set(STOP_LIST_PHRASES) & set(PENDING))

    def test_the_prompt_carries_each_item(self) -> None:
        for lead, (where, phrases) in STOP_LIST_PHRASES.items():
            for phrase in phrases:
                with self.subTest(item=lead, phrase=phrase):
                    self.assertIn(phrase, self.text_for(where))

    def test_pending_items_are_still_missing_from_the_stop_list(self) -> None:
        for lead, phrases in PENDING.items():
            with self.subTest(item=lead):
                missing = [p for p in phrases if p not in self.stop]
                self.assertTrue(
                    missing,
                    f"the stop list now carries {lead!r}: move it from PENDING to "
                    "STOP_LIST_PHRASES in the same pull request",
                )

    def test_the_stop_bullet_claims_its_items_need_a_human(self) -> None:
        # The claim that makes the list read as complete, and so the one that
        # makes a missing item matter.
        self.assertIn("Those are Tier 3 in docs/risk-tiers.md and need a human first", self.stop)
        self.assertIn("docs/SECURITY-AI.md", self.prompt)


if __name__ == "__main__":
    unittest.main()
