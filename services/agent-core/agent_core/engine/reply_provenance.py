"""Numeric references for text-only replies, derived from existing evidence rather than generated again.

This never creates a value or receipt, or adds anything to SessionState.provenance(). The normal numeric, claim,
verdict and answer-leak gates still decide whether the words can be shown.
"""

from fractions import Fraction
from typing import Any

from agent_core.engine.spoken import to_written
from agent_core.engine.state import SessionState, numbers_in


def references(draft: dict[str, Any], state: SessionState) -> list[dict[str, str]]:
    sources: list[tuple[set[Fraction], str, str]] = []
    if state.problem:
        sources.append((set(numbers_in(state.problem.statement)), "question", state.problem.statement_ref))
    sources.extend((set(numbers_in(s.latex)), "student", s.step_id) for s in state.board.steps if s.owner == "student")
    sources.extend(
        (set(numbers_in(line)), "student", f"student-line:{index}") for index, line in enumerate(state.student_lines, 1)
    )
    flagged = state.unreviewed_implausible()
    sources.extend(
        (set(values), "check", check) for check, values in state.check_values.items() if check not in flagged
    )
    found: list[dict[str, str]] = []
    for name in ("say", "say_spoken"):
        for value in numbers_in(to_written(str(draft.get(name) or ""))):
            source = next(({"source": kind, "ref": ref} for values, kind, ref in sources if value in values), None)
            if source is not None and source not in found:
                found.append(source)
    return found
