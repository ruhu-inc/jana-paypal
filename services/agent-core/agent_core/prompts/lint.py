"""The prompt linter's writing techniques (PRM-21, LIST PRM-21a): the marked techniques, checked over role bodies and
guidance blocks.

- Technique 1, mandatory tools for computation: the Solver's and the Checker's method says never to work out
  arithmetic or algebra itself, but to use the checked values or a tool.
- Technique 8, handoff contract: every team role's output section says its output goes to the engine, not the student.
- Technique 10, few-shot examples only for tool routing: at most two, each under 300 characters.
- Technique 11, declarative rules without shouting: no "IMPORTANT", no capital-letter commands."""

from __future__ import annotations

import re

from agent_core.assets.agent import AgentAssets

MANDATORY_TOOLS = re.compile(r"never work out arithmetic or algebra\s+yourself; use the checked values or a tool", re.I)
HANDOFF = "Your output goes to the engine, not the student"
SHOUTING = re.compile(r"\b(IMPORTANT|CRITICAL|MUST|NEVER|ALWAYS|DO NOT|DON'T|WARNING)\b")
EXAMPLE = re.compile(r"(?im)^(example|e\.g\.)\b[^\n]*(\n(?!\n)[^\n]*)*")
COMPUTING_ROLES = ("solver", "checker")
MAX_EXAMPLES = 2
EXAMPLE_MAX_CHARS = 300


def _section(body: str, title: str) -> str:
    found = re.search(rf"^## {re.escape(title)}\n((?:(?!^## ).*\n?)*)", body, re.M)
    return found.group(1) if found else ""


def technique_findings(assets: AgentAssets) -> list[str]:
    """`<file>: technique <n>: <what>` for every marked technique a role body or guidance block breaks."""
    found = []
    for role, manifest in sorted(assets.roles.items()):
        body = manifest.body
        if role in COMPUTING_ROLES and not MANDATORY_TOOLS.search(" ".join(_section(body, "Method").split())):
            found.append(f"{role}: technique 1: the method does not require tools for every computation")
        if manifest.meta.get("audience") == "team" and HANDOFF not in " ".join(_section(body, "Output").split()):
            found.append(f"{role}: technique 8: the output section lacks the handoff line")
        found += [f"{role}: {f}" for f in _common(body)]
    for block in (*assets.guidance, *assets.families):
        found += [f"{block.id}: {f}" for f in _common(block.body)]
    return found


def _common(text: str) -> list[str]:
    out = []
    shout = SHOUTING.search(text)
    if shout:
        out.append(f"technique 11: capital-letter shouting ({shout.group(0)})")
    examples = [m.group(0) for m in EXAMPLE.finditer(text)]
    if len(examples) > MAX_EXAMPLES or any(len(e) >= EXAMPLE_MAX_CHARS for e in examples):
        out.append(f"technique 10: at most {MAX_EXAMPLES} examples, each under {EXAMPLE_MAX_CHARS} characters")
    return out
