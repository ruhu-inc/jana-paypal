"""A pushed skill's `answer_policy`: guided pacing only, never a gate on a request (SKL-14, with PED-08, PED-09, D87).

In guided mode the engine combines the skill's `answer_policy` with the mode and the guided withholding rule (PED-08)
to decide what the tutor volunteers, and applies the strictest: a skill can slow the pace (`min_attempts: 2` keeps the
worked-step rungs back until two attempts) but never loosen the guided rule. No skill setting holds back the student's
own request for the full worked solution: that is the solution policy's alone (PED-09, D87), and a skill adds nothing
to it. A skill's `escalation` is advice to the engine's adaptation rules, never a policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_core.engine.pedagogy import policy_template
from agent_core.engine.state import SessionState

# What the tutor may volunteer, strictest first.
LEVELS = ("never", "earned", "on_request")
SKILL_LEVEL = {
    "never_final_answer": "never",
    "withhold_until_earned": "earned",
    "show_on_request": "on_request",
    "not_applicable": "on_request",
}
WORKED_RUNG = 4  # rungs 4 and 5 show a step (PED-03): the first rung a slower pace holds back
GUIDED_MODES = ("guided",)


@dataclass(frozen=True)
class Pacing:
    """What the tutor volunteers unasked: `level`, and the attempts and rung it waits for first."""

    level: str
    min_attempts: int = 0
    min_rung: int = 0
    sources: tuple[str, ...] = ()


def guided_pacing(mode: str, skill_policy: dict[str, Any] | None = None) -> Pacing:
    """SKL-14: the strictest of the guided rule and the skill's policy, in guided mode; elsewhere the mode decides."""
    candidates: list[tuple[str, str]] = [("guided_rule", "earned")] if mode in GUIDED_MODES else []
    attempts = rung = 0
    if skill_policy and mode in GUIDED_MODES:
        candidates.append(("skill", SKILL_LEVEL.get(str(skill_policy.get("mode")), "on_request")))
        attempts = int(skill_policy.get("min_attempts") or 0)
        rung = int(skill_policy.get("min_rung") or 0)
    if not candidates:
        return Pacing("on_request")
    level = min((lv for _, lv in candidates), key=LEVELS.index)
    return Pacing(level, attempts, rung, tuple(src for src, lv in candidates if lv == level))


def paced_rung(state: SessionState, rung: int, skill: Any = None) -> int:
    """The hint rung the engine may volunteer now: a skill's slower pace keeps the worked-step rungs back until its
    attempts are made. It never raises a rung, and it never touches a request for the solution."""
    meta = getattr(skill, "meta", None) or {}
    pacing = guided_pacing(state.mode, meta.get("answer_policy"))
    if rung >= WORKED_RUNG and state.attempts.get(state.step, 0) < pacing.min_attempts:
        return WORKED_RUNG - 1
    return rung


def policy_template_with_skill(state: SessionState, text: str, skill: Any = None) -> str | None:
    """The engine's answer to a request for the solution: the solution policy and the mock lock decide (H4, H7,
    PED-09); the pushed skill adds no gate of its own (SKL-14, D87)."""
    del skill  # a skill's answer_policy paces what is volunteered; it never holds back a request
    return policy_template(state, text)
