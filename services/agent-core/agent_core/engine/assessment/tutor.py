"""Load pinned assessment context into a tutor session (UXS-24, ASM-20, AGT-24)."""

from __future__ import annotations

import copy
from typing import Any

from agent_core.engine.assessment.items import response
from agent_core.engine.slice import plan_from_item
from agent_core.engine.state import Problem, SessionState


def load_context(state: SessionState, loaded: dict[str, Any]) -> None:
    item = copy.deepcopy(loaded["item"])
    part = item["parts"][0]
    statement = str(item["stem"]) + " " + str(part["prompt"])
    state.problem = Problem(item["id"], statement, item["id"] + "#" + part["id"])
    state.practice = {"item": item, "part": part["id"], "shown_ms": 0}
    if loaded.get("misconception_id"):
        state.practice["misconception_id"] = loaded["misconception_id"]
        for option in part["answer"].get("options", []):
            if option.get("misconception_id") == loaded["misconception_id"]:
                option["feedback"] = loaded.get("fix", "")
    outcomes = loaded.get("outcomes") or (["incorrect"] if loaded.get("misconception_id") else [])
    if outcomes:
        state.practice["verdicts"] = list(outcomes)
        state.attempts[1] = len(outcomes)
    for answered in (loaded.get("answer") or {}).get("parts", []):
        state.student_lines.append(str(response(answered)))
    state.plan, values = plan_from_item(item, part["id"])
    state.check_values.update(values)
    state.assessment_origin = loaded["origin"]["kind"]
    if loaded.get("reason") == "why_right":  # "Why is this right?" (UXS-62): self-explanation first, no marking
        state.practice["why_right"] = "asking"
    if state.assessment_origin == "exam_review":
        state.mode = "guided"
        state.mock_answer_submitted = True
        state.transcript.append({"role": "user", "content": statement, "kind": "problem"})
        if state.student_lines:
            state.transcript.append({"role": "user", "content": "\n".join(state.student_lines), "kind": "attempt"})
