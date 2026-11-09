"""Compaction of what is sent, never of what is stored (MEM-15, MEM-16, MEM-17, AGT-05).

The engine compacts when a built prompt exceeds the threshold share of the candidate's context for agents, or after a
`context_overflow`. Protected and never summarised: the first student message, the current problem statement and the
most recent exchanges; tool calls and their results are never split. The record's sections are built by code from
working state; only free dialogue is summarised (into Agreements) by the compaction slot, and that text is validated:
no number that working state does not hold, with one correction, else a fixed placeholder."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.config import Settings
from agent_core.engine.state import SessionState, numbers_in

PLACEHOLDER = "Earlier conversation summarised; ask to recall details."
SECTIONS = (
    "Goal",
    "Constraints",
    "Problem state",
    "Steps done",
    "Student errors seen",
    "Agreements",
    "Exact values",
    "Next step",
)
FLOW_KINDS = frozenset({"attempt", "hint", "problem", "tutor", "tool"})


@dataclass
class CompactionRecord:
    sections: dict[str, str]
    model_calls: int = 0
    before_chars: int = 0
    after_chars: int = 0
    merged_from_previous: bool = False

    def text(self) -> str:
        body = "\n".join(f"{name}: {self.sections.get(name, '')}" for name in SECTIONS)
        return f"<compaction-record>\n{body}\n</compaction-record>"


@dataclass
class Compactor:
    settings: Settings
    previous: CompactionRecord | None = None
    history: list[CompactionRecord] = field(default_factory=list)

    def needed(self, prompt_tokens: int, context_for_agents: int) -> bool:
        return prompt_tokens > self.settings.compaction_threshold * context_for_agents

    async def compact(
        self,
        state: SessionState,
        transcript: list[dict[str, Any]],
        summarise: Callable[[str], Awaitable[str]] | None = None,
    ) -> tuple[list[dict[str, Any]], CompactionRecord]:
        """The messages to send instead of `transcript`, and the record built from working state."""
        before = sum(len(str(m.get("content", ""))) for m in transcript)
        protected_head = [m for m in transcript[:1]]
        recent = _recent(transcript[1:], self.settings.compaction_recent_exchanges)
        middle = transcript[1 : len(transcript) - len(recent)] if recent else transcript[1:]
        free_dialogue = [m for m in middle if m.get("kind") not in FLOW_KINDS and m.get("role") == "user"]
        sections = self._sections(state)
        calls = 0
        if free_dialogue and summarise is not None:
            text = "\n".join(str(m["content"]) for m in free_dialogue)
            sections["Agreements"], calls = await self._agreements(text, state, summarise)
        elif self.previous is not None:
            sections["Agreements"] = self.previous.sections.get("Agreements", "")
        record = CompactionRecord(sections, calls, before, merged_from_previous=self.previous is not None)
        sent = [*protected_head, {"role": "user", "content": record.text(), "kind": "compaction"}, *recent]
        record.after_chars = sum(len(str(m.get("content", ""))) for m in sent)
        self.previous = record
        self.history.append(record)
        return sent, record

    def _sections(self, state: SessionState) -> dict[str, str]:
        """Every section but Agreements, from working state with no model call (MEM-16)."""
        problem = state.problem
        total = len(state.plan.steps) if state.plan else 0
        done = [s for s in state.board.steps if s.status in ("correct", "final") and s.latex]
        exact = [f"{s.latex} [{s.check_id}]" for s in state.board.steps if s.check_id]
        next_step = state.current_step()
        return {
            "Goal": f"solve problem {problem.problem_id}" if problem else "no open problem",
            "Constraints": f"mode {state.mode}",
            "Problem state": (
                f"{problem.statement_ref if problem else 'none'}; step {state.step} of {total}; hint rung {state.rung}"
            ),
            "Steps done": "; ".join(
                f"{n}. {s.latex} ({s.status}, {s.check_id or 'student line'})" for n, s in enumerate(done, 1)
            )
            or "none",
            "Student errors seen": "; ".join(s.latex for s in state.board.steps if s.status == "incorrect") or "none",
            "Agreements": "",
            "Exact values": "; ".join(exact) or "none",
            "Next step": f"step {next_step.index}" if next_step else "check the final answer",
        }

    async def _agreements(
        self, dialogue: str, state: SessionState, summarise: Callable[[str], Awaitable[str]]
    ) -> tuple[str, int]:
        allowed = state.provenance()
        prompt = dialogue
        for attempt in range(2):
            text = (await summarise(prompt)).strip()
            problems = validation_errors(text, allowed, self.settings)
            if text and not problems:  # an empty summary is a failed section (MEM-17, AGT-02)
                return text, attempt + 1
            prompt = f"{dialogue}\n\nFix these problems and summarise again: {'; '.join(problems)}"
        return PLACEHOLDER, 2


def validation_errors(text: str, allowed: set[Any], settings: Settings) -> list[str]:
    """A model-written section: within its length, and no number absent from working state (MEM-17)."""
    problems = []
    if len(text) > settings.compaction_section_max_chars:
        problems.append(f"longer than {settings.compaction_section_max_chars} characters")
    invented = [str(v) for v in numbers_in(text) if v not in allowed]
    if invented:
        problems.append(f"numbers not in working state: {', '.join(invented[:3])}")
    if re.search(r"<[a-z-]+>", text):
        problems.append("contains markup")
    return problems


def _recent(messages: list[dict[str, Any]], exchanges: int) -> list[dict[str, Any]]:
    """The last N exchanges (a student message and what followed), never splitting a tool call from its result."""
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("kind") != "compaction"]
    if len(starts) <= exchanges:
        return list(messages)
    cut = starts[-exchanges]
    while cut > 0 and messages[cut].get("role") == "tool":
        cut -= 1
    return list(messages[cut:])
