"""Student-owned board lines: validate first, preserve ids, recheck the edited suffix in order (INP-11/12)."""

from __future__ import annotations

from typing import Any

from agent_core.engine import slice as flows
from agent_core.engine.annotations import term_moves
from agent_core.engine.state import BoardStep
from agent_core.engine.ui import emit_part, error_of, open_board, step_error
from agent_core.router import ToolCall


def publish(engine: Any, turn: Any, steps: list[BoardStep], opened: Any = None,
            moves: dict[str, list[dict[str, Any]]] | None = None) -> None:  # fmt: skip
    """The steps' `board.step` events, a checked line with the term moves from the line it follows (UXS-61)."""
    events = [
        {
            "type": "board.step",
            "board_id": engine.state.board.board_id,
            "step_id": step.step_id,
            "index": step.index,
            "latex": step.latex,
            "reason": step.reason,
            "owner": step.owner,
            "status": step.status,
            "check_id": step.check_id,
            "error": step_error(step),
            **({"term_moves": moves[step.step_id]} if moves and moves.get(step.step_id) else {}),
        }
        for step in steps
    ]
    if opened:
        events.insert(0, opened)
    part = emit_part(engine.events, "board", events)
    engine._persist(turn, part, "board", events)


def highlight_on_photo(engine: Any, turn: Any, step: BoardStep) -> bool:
    """The first wrong line, lit on the board and, when it was read from the student's photo, on the photo too
    (JM-D10): one `board.highlight` naming the photo region beside the step. False (nothing emitted) otherwise."""
    region = engine.state.photo_lines.get(step.step_id)
    if region is None:
        return False  # typed working: the Coach's own board ops say what to light
    event = {"type": "board.highlight", "board_id": engine.state.board.board_id, "step_ids": [step.step_id],
             "ttl_ms": None, "photo_regions": [region]}  # fmt: skip
    part = emit_part(engine.events, "board", [event])
    engine._persist(turn, part, "board", [event])
    return True


async def apply(engine: Any, turn: Any, submitted: list[tuple[str, str]], source: str = "student_text") -> Any:
    state = engine.state
    existing = {step.step_id: step for step in state.board.steps}
    ids = [step_id for step_id, _ in submitted]
    if (
        state.problem is None
        or len(ids) != len(set(ids))
        or any(step_id in existing and existing[step_id].owner != "student" for step_id in ids)
    ):
        return engine._finish_text(turn, "Only your own working lines can be edited.", None, "completed")
    lines = []
    # One call for the message (AGT-02); a line may be the student's answer to the open numeric part.
    model = await engine._guard_model("\n".join(raw for _, raw in submitted), answer=True)
    for step_id, raw in submitted:
        stopped, line = engine._intake(turn, raw, source, model)  # the guard sees the whole line (PRV-13, PRM-18)
        if stopped is not None:
            return stopped
        if (source == "ocr_text" and "[?]" in line) or flows.worded(line):
            lines.append((step_id, line))  # a worded step is kept as it is, never parsed as algebra (VER-27)
            continue
        call = ToolCall(f"board_parse_{len(turn.tools.log)}", "math_parse", {"text": flows.for_tool(line)})
        outcomes = await turn.tools.run(
            [call], "checker", engine._tool_session("checker", "small"), ["math_parse"], False, engine.cancelled
        )
        if engine.cancelled.is_set():
            return engine._finish_interrupted(turn)
        if not outcomes or outcomes[0].result["status"] != "ok":
            # No invalid draft is committed or marked incorrect. The original line and all later lines remain.
            return engine._finish_text(
                turn,
                "I couldn't read this working line. Check its brackets and symbols, "
                "then try again. Your previous working is still here.",
                None,
                "completed",
                "input_not_parsed",
            )
        lines.append((step_id, line))
    if state.plan is None:
        state.plan = await engine._plan(turn)
        if state.plan is None:
            return engine._finish_template(turn, "tpl.honest_blocker", "completed", degraded_reason="plan_unverified")
    opened = open_board(state)
    own = [step for step in state.board.steps if step.owner == "student" and step.latex.strip()]
    old_lines = [step.latex for step in own]
    existing = {step.step_id: step for step in state.board.steps}
    affected = len(own)
    for step_id, line in lines:
        step = existing.get(step_id)
        if step is None:
            step = BoardStep(step_id, len(state.board.steps), line, None, "student", "checking")
            state.board.steps.append(step)
            own.append(step)
        else:
            step.latex = line
            if step not in own:
                own.append(step)
                own.sort(key=lambda s: s.index)
            affected = min(affected, own.index(step))
    suffix = own[affected:]
    for step in suffix:
        step.status, step.check_id, step.reason, step.error = "checking", None, None, None
    publish(engine, turn, suffix, opened)
    earlier = next((i for i, s in enumerate(own[:affected]) if s.status == "incorrect"), None)
    _, step_index = engine._opportunity()  # the working is one attempt at the problem (LRN-06, ASM-09)
    results = await flows.check_lines(
        engine, turn, [step.latex for step in own], state.plan, start_index=affected, first_error_at=earlier
    )
    if engine.cancelled.is_set():
        return engine._finish_interrupted(turn)
    moves: dict[str, list[dict[str, Any]]] = {}
    for step, result in zip(suffix, results, strict=True):
        step.status, step.check_id = result.status, result.check_id
        if result.before is not None:
            moves[step.step_id] = term_moves(own[result.before].step_id, own[result.before].latex, step.latex,
                                             result.moves)  # fmt: skip
        step.reason = flows.line_reason(result)
        # The first wrong line names its error (API-12, UXS-16); a line carrying it forward names that line instead.
        step.error = error_of(result.category) if result.status == "incorrect" and not result.carried_forward else None
    publish(engine, turn, suffix, moves=moves)
    # Previous edits must not keep granting derived-value permission to the answer-leak gate.
    for old in old_lines:
        if old in state.student_lines:
            state.student_lines.remove(old)
    state.student_lines.extend(step.latex for step in own)
    state.transcript.append(
        {"role": "user", "content": "\n".join(line for _, line in lines), "kind": "attempt", "source": source}
    )
    system = flows.system_of(engine, state.plan)
    state.solved = flows.solved(state.plan, system, [(step.latex, step.status) for step in own])
    state.rung = 0
    if not state.solved:
        state.step = 1
    first = next((i for i, step in enumerate(own) if step.status == "incorrect"), None)
    await engine.working_evidence(turn, own, first, step_index)
    note = "working checked: explain the statuses and ask one question; never give a corrected line"
    if first is not None:
        note += f"; first wrong student line is {first + 1}"
        if highlight_on_photo(engine, turn, own[first]):
            note += " (it is outlined on the student's own photo: ask about that line)"
    return await engine._coach_turn(turn, intent_note=note)
