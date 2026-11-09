"""A scripted model for development builds only: UI work and browser checks without a model runtime (M5).

It plays a guided solve of one synthetic S1 problem through the shipped engine (the worker computes and the gates
run as usual): the Solver calls `math_solve` and plans from the check it gets back; the Coach asks short questions
that carry no numbers. Any other problem gets a blocked plan, so the engine shows its honest-blocker template. The S2
roles answer too, so the build's stage runs end to end (AGT-15, AGT-02): the Diagnoser finds the question in scope
with nothing to clarify, the Checker cannot verify an answer the code did not, and the input guard, rule matcher,
title and compaction roles give their plainest valid answers. Release builds refuse it (the sidecar checks)."""

from __future__ import annotations

import json
from itertools import count
from typing import Any

from agent_core.router import ScriptedProvider

DEMO = "thrown straight up at 14 m/s"
COACH_LINES = (
    "Which equation links the starting speed, the acceleration and the height?",
    "Good. At the highest point, what is the ball's speed?",
    "Now put the known values into your equation. What do you get for s?",
    "Check the sign of the acceleration you used. Is up positive?",
    "Well done. Can you say in one sentence why the speed is zero at the top?",
)


def _reply(content: str = "", calls: list[tuple[str, str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish = "stop"
    if calls:
        message["tool_calls"] = [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, n, a in calls]  # fmt: skip
        finish = "tool_calls"
    return {"reply": {"model": "dev-scripted", "choices": [{"message": message, "finish_reason": finish}],
                      "usage": {"prompt_tokens": 50, "completion_tokens": 20}}}  # fmt: skip


def _last_check(body: dict[str, Any]) -> str | None:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            try:
                return str(json.loads(message["content"])["check_id"])
            except (ValueError, KeyError, TypeError):
                return None
    return None


def _solver(body: dict[str, Any]) -> dict[str, Any]:
    task = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
    if DEMO not in task:
        blocked: dict[str, Any] = {"status": "blocked", "method_summary": "", "steps": [], "final_answer": None,
                                   "blocked_step": 1,
                                   "blocked_reason": "the development script only knows one problem"}  # fmt: skip
        return _reply(json.dumps(blocked))
    check = _last_check(body)
    if check is None:
        return _reply("", [("c0", "math_solve", {"equations": ["0 = 14^2 - 2*9.8*s"], "unknowns": ["s"]})])
    steps = [
        {"index": 1, "latex": "v^2 = u^2 + 2as", "reason": "links u, v, a and s", "check_id": None, "kc_ref": None,
         "value": None, "unit": None},
        {"index": 2, "latex": "0 = 14^2 - 2(9.8)s", "reason": "substitute with up positive", "check_id": check,
         "kc_ref": None, "value": "10", "unit": "m"},
    ]  # fmt: skip
    return _reply(json.dumps({"status": "complete", "method_summary": "Use v^2 = u^2 + 2as with v = 0 at the top.",
                              "steps": steps, "final_answer": {"latex": "s = 10\\,m", "value": "10", "unit": "m",
                                                               "check_id": check, "plausible": True},
                              "blocked_step": None, "blocked_reason": None}))  # fmt: skip


# The S2 roles' plainest valid structured answers (their contracts' required fields only).
S2_REPLIES: dict[str, dict[str, Any]] = {
    "diagnoser": {"in_scope": True, "subject": "physics", "topic_ref": None, "kc_refs": [],
                  "question_kind": "calculation", "knowns": [], "unknowns": [], "clarifying_question": None,
                  "misconception_ref": None, "evidence": [], "inference": "", "prerequisite_gap_refs": [],
                  "confidence": "medium"},
    "checker": {"subject_of_check": "student_answer", "verdict": "cannot_verify", "first_wrong_step": None,
                "error_category": "none", "why": "", "counterexample": None, "misconception_ref": None,
                "method": "substitute_back", "step_marks": [], "check_ids": []},
    "input_guard": {"class": "in_scope", "confidence": 0.9},
    "rule_matcher": {"applies": []},
    "title": {"title": "A ball thrown upward"},
    "compaction": {"agreements": ""},
}  # fmt: skip


class _Endless(dict[str, list[Any]]):
    """Role-keyed steps that never run out: each call gets a fresh step for its role."""

    def __init__(self) -> None:
        super().__init__()
        self.turns = count()

    def get(self, role: str, default: Any = None) -> list[Any]:
        if role == "solver":
            return [_solver]
        if role == "reflector":

            def reflect(body: dict[str, Any]) -> dict[str, Any]:
                if any(
                    "Write a session note by selecting evidence numbers only." in str(m.get("content", ""))
                    for m in body.get("messages", [])
                ):
                    return _reply(json.dumps({"statements": [{"evidence": 0, "section": "method"}]}))
                return _reply(json.dumps({"notes": [], "summary_for_student": "", "nothing_new": True}))

            return [reflect]
        if role in S2_REPLIES:
            return [_reply(json.dumps(S2_REPLIES[role]))]
        line = COACH_LINES[next(self.turns) % len(COACH_LINES)]
        tutor: dict[str, Any] = {"say_spoken": None, "say": line, "board": [], "visual": [], "card": None,
                                 "hint_rung": 0, "expects": "student_answer", "numbers_from": [], "cites": [],
                                 "reveals": "none", "confidence": "medium"}  # fmt: skip
        return [_reply(json.dumps(tutor))]


def dev_scripted_provider() -> ScriptedProvider:
    provider = ScriptedProvider(_Endless(), kind="local", installed_models=["qwen3-vl-8b", "gemma-4-12b"])
    provider.requests = _Discard()
    return provider


class _Discard(list[dict[str, Any]]):
    """Request bodies are not kept in a long-running development session."""

    def append(self, _: dict[str, Any]) -> None:
        return None
