"""Mock criterion dependencies and local method adjudication (ASM-17), and "show that" parts (ASM-25); no item-marker
changes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

Equivalent = Callable[[str, str], Awaitable[tuple[bool | None, str | None]]]
Judge = Callable[[Sequence[str], Sequence[str]], Awaitable[bool | None]]
JudgeCriterion = Callable[[Mapping[str, Any], Sequence[str]], Awaitable[bool | None]]


async def decide_method(
    lines: Sequence[str],
    alternatives: Sequence[str],
    equivalent: Equivalent,
    *,
    judge_allowed: bool,
    judge: Judge | None = None,
) -> bool | None:
    complete = bool(lines and alternatives)
    for line in lines:
        for method in alternatives:
            result, check_id = await equivalent(line, method)
            if result is True and check_id:
                return True
            complete &= result is not None and check_id is not None
    if judge_allowed and judge is not None:
        first, second = await judge(lines, alternatives), await judge(lines, alternatives)
        return first if first is not None and first == second else None
    return False if complete else None


SameForm = Callable[[str, str], Awaitable[bool | None]]


async def drop_restatements(lines: Sequence[str], target: str, same_form: SameForm) -> list[str]:
    """ASM-25: a "show that" part gives its target, so writing the target is no step of a derivation. Every line of a
    derivation has the target's value, so equivalence cannot tell them apart: a line whose last side has the target's
    form as written ("dy/dx = 6x + 2" or "2 + 6x" for 6x + 2, not "3(2x) + 2") is set aside before method marks are
    decided. Restating the target then earns no method mark, and the A mark that needs it falls with it."""
    kept = []
    for line in lines:
        if await same_form(line.rsplit("=", 1)[-1].strip(), target) is not True:
            kept.append(line)
    return kept


def score_criteria(criteria: Sequence[Mapping[str, Any]], decisions: Mapping[str, bool | None]) -> list[dict[str, Any]]:
    by_id = {str(c["criterion_id"]): c for c in criteria}
    if len(by_id) != len(criteria):
        raise ValueError("duplicate criterion ids")
    resolved: dict[str, bool | None] = {}
    visiting: set[str] = set()

    def resolve(cid: str) -> bool | None:
        if cid in resolved:
            return resolved[cid]
        if cid in visiting or cid not in by_id:
            raise ValueError("invalid criterion dependencies")
        visiting.add(cid)
        c = by_id[cid]
        needs = list(c.get("requires") or [])
        if c["mark_type"] == "A":
            if not any(by_id.get(n, {}).get("mark_type") == "M" for n in needs):
                resolved[cid] = False
                visiting.remove(cid)
                return False
        value = decisions.get(cid)
        prerequisites = [resolve(n) for n in needs]
        if any(p is False for p in prerequisites):
            value = False
        elif any(p is None for p in prerequisites):
            value = None
        resolved[cid] = value
        visiting.remove(cid)
        return value

    out = []
    for c in criteria:
        value = resolve(str(c["criterion_id"]))
        marks = int(c["marks"])
        out.append(
            {
                "criterion_id": c["criterion_id"],
                "mark_type": c["mark_type"],
                "awarded": marks if value else 0,
                "available": 0 if value is None else marks,
                "resolved": value is not None,
                "scheme": str(c["pass_if"]),
                "notice_key": "assessment.not_marked_automatically" if value is None else None,
            }
        )
    return out
