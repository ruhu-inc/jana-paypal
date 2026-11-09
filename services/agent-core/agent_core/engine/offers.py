"""The offers the engine makes on its own during a problem (LRN-20, LRN-23), on the live turn loop.

- The unprompted hint offer (LRN-20, D112): after `offer_after_wrong` wrong answers in a row on a step, or the idle
  trigger, the engine says once, quietly, that a hint is one tap away: a `notice` with code `hint_offer` under the
  reply, never a hint and never a card to answer. The student pulls the hint with the hint chip above the composer,
  so there is nothing to accept or decline. At most once per step, never in a timed mode or with the student's
  `unprompted_help` preference off; a turn on which the engine already raised the hint rung offers none. The next hint
  the student pulls on that step records the trigger `offer_accepted`.
- The prerequisite detour (LRN-23): after `detour_failures` wrong answers on a KC whose prerequisite is under
  `detour_prereq_below`, a short detour to the weakest such prerequisite is offered: a short explanation and
  `detour_items` items on it, with the problem kept on a return stack at most `detour_max_depth` deep. When the
  detour's items are done the problem comes back at the same step with its board as it was. Declining is logged and
  the detour is not offered again for that KC in the session.

- The root gap: a wrong answer that matches a misconception the pack links to an earlier skill (its `root_gap`, a
  KC of any installed pack) gets that detour at once, without waiting for `detour_failures`: the card's prompt is
  the one line "This is really about {idea}.", and accepting it routes the next practice to that skill, then back.
  It takes the place of the policy's detour on that answer, and is offered once per misconception in a session.

The detour is one engine-written question card (TOL-20; at most one per wrong answer). Each offer raises the soft rule
signal its rule reads (`engine.idle_offer_due`, `engine.detour_eligible`, PED-14), so the Coach words the offer and
never takes it on itself. Every decision is the pure policy's (`agent_core.learner.policies`), logged in the turn trace
(LRN-15). The idle trigger (API 1.10, D104): the client reports a step's idle time and window focus with
`POST /v1/sessions/{session_id}/idle`; when the same policy offers on it, a short engine-written turn with no model call
carries the same quiet line on the session's stream (`Offers.idle_offer`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from agent_core.engine import slice as flows
from agent_core.engine import turn_scope
from agent_core.engine.events import new_id
from agent_core.engine.state import Problem, state_from_json, state_to_json
from agent_core.engine.ui import HandlerError, emit_part, open_board, question_card
from agent_core.learner import policies

if TYPE_CHECKING:
    from agent_core.engine.turn import TurnIds, TurnResult

# What a detour keeps of the open problem and puts back after it: the problem, its plan, step, hints and board.
SAVED = ("problem", "plan", "step", "rung", "attempts", "deflected", "board", "check_values", "implausible", "reviewed",
         "student_lines", "solved", "practice", "hint_step", "hint_at_ms", "gaming", "bottom_outs", "withheld_extra",
         "solution_shown", "wrong_streak", "offered_steps", "offer_pulled")  # fmt: skip
OFFER_NOTE = ("a card under your reply offers {what}; say in one short sentence that the student can choose it, and do "
              "not give a hint or start anything yourself")  # fmt: skip
HINT_OFFER_NOTE = ("the hint offer is the quiet line under your reply (a hint is one tap away when the student wants "
                   "it): do not repeat it, ask whether they want a hint, or give one yourself")  # fmt: skip
ROOT_GAP_NOTE = ("the card under your reply says this mistake is really about {idea} and offers practice on it "
                 "first: do not repeat that line or teach {idea} now; say in one short sentence that the student can "
                 "choose the practice")  # fmt: skip
HINT_OFFER_CODE = "hint_offer"  # the notice that carries the hint offer (LRN-20, D112)
DETOUR_NOTE = ("a short detour to an earlier idea the student chose: {idea}. Explain that idea in two or three plain "
               "sentences, then ask the short question on the board; say we will come back to the first problem "
               "after it")  # fmt: skip
NEXT_NOTE = "that answer is right; the next short question on the earlier idea is on the board: ask for an attempt"
RESUME_NOTE = ("the detour is done: we are back on the first problem at the same step, its board as it was; say so in "
               "one sentence and ask for the next attempt on that step")  # fmt: skip
NO_DETOUR_NOTE = ("the student wanted to go over an earlier idea ({idea}) but no short questions on it are "
                  "installed: explain it in two or three plain sentences, then go back to the problem at the same "
                  "step")  # fmt: skip


class Detours(Protocol):
    """What a detour reads, bound by the app to the open learner database and the installed packs."""

    def prerequisites(self, kc_ids: Sequence[str]) -> dict[str, dict[str, float]]:
        """Each KC's prerequisites with their P_j now."""
        ...

    def items(self, kc_id: str, count: int) -> list[dict[str, Any]]:
        """Up to `count` published practice items on the KC (pack records)."""
        ...

    def statement(self, kc_id: str) -> str | None: ...


def consecutive_wrong(engine: Any) -> int:
    """Wrong answers in a row on the open step: a practice item's last verdicts, else the problem's own count."""
    practice = engine.state.practice
    if practice is None:
        return int(engine.state.wrong_streak)
    run = 0
    for outcome in reversed(practice.get("verdicts") or []):
        if outcome != "incorrect":
            break
        run += 1
    return run


def failures(engine: Any) -> int:
    """Wrong answers on the open problem's KCs in this problem (LRN-23)."""
    practice = engine.state.practice
    if practice is None:
        return int(engine.state.wrong_streak)
    return sum(1 for outcome in practice.get("verdicts") or [] if outcome == "incorrect")


def problem_kcs(engine: Any) -> list[str]:
    kcs = engine._item_kcs()
    if not kcs and engine.diagnosis is not None:
        kcs = [str(k) for k in engine.diagnosis.kc_ids]
    return list(kcs)


def _positions(stack: Sequence[Mapping[str, Any]]) -> list[policies.SavedPosition]:
    return [policies.SavedPosition(str(p["kc_id"]), str(p["problem_ref"]), int(p["step_index"])) for p in stack]


def after_wrong(engine: Any, turn: Any, rung_before: int, misconception: str | None = None) -> str:
    """After a wrong answer (S2): the hint offer and the detour offer the policies allow, as one question card; a
    `misconception` the answer matched whose pack links it to an earlier skill offers that skill instead (the root
    gap). Returns the Coach's note ("" when nothing is offered)."""
    state, s = engine.state, engine.deps.settings
    if engine.deps.stage < "S2" or state.mode in policies.TIMED_MODES or state.mock_running or state.problem is None:
        return ""
    on = True if engine.unprompted_help is None else bool(engine.unprompted_help())
    attempted = state.attempts.get(state.step, 0) > 0
    hinted = policies.unprompted_offer(idle_s=0.0, focused=False, consecutive_wrong=consecutive_wrong(engine),
                                       offered_on_step=state.step in state.offered_steps, mode=state.mode,
                                       preference_on=on, has_attempt=attempted, s=s)  # fmt: skip
    turn.trace.policy_decision(hinted.entry())
    hint = bool(hinted.output["offer"]) and state.rung == rung_before  # a hint the engine just showed is not offered
    gap = root_gap(engine, misconception)
    detour = gap if gap is not None else _detour(engine, turn)
    if not hint and detour is None:
        return ""
    return offer_card(engine, turn, hint, detour)


def idle_decision(engine: Any, idle_s: float, focused: bool) -> policies.Decision | None:
    """LRN-20's idle trigger: the policy's offer for a step that has had no input for `idle_s` with the window focused
    all that time, or None when nothing is offered (no open problem, a timed mode or a mock, the preference off, or
    already offered on this step). Wrong answers are not counted here: their trigger runs on the answer's own turn."""
    state = engine.state
    if engine.deps.stage < "S2" or state.mode in policies.TIMED_MODES or state.mock_running or state.problem is None:
        return None
    on = True if engine.unprompted_help is None else bool(engine.unprompted_help())
    decision = policies.unprompted_offer(idle_s=idle_s, focused=focused, consecutive_wrong=0,
                                         offered_on_step=state.step in state.offered_steps, mode=state.mode,
                                         preference_on=on, has_attempt=state.attempts.get(state.step, 0) > 0,
                                         s=engine.deps.settings)  # fmt: skip
    return decision if decision.output["offer"] else None


def offer_card(engine: Any, turn: Any, hint: bool, detour: Mapping[str, Any] | None) -> str:
    """The offers on this turn: the hint as the quiet `hint_offer` notice, the detour as one engine-written question
    card; returns the Coach's note ("" when nothing was offered)."""
    notes = []
    if hint:
        notes.append(hint_offer(engine))
    if detour is not None:
        notes.append(detour_card(engine, turn, detour))
    return "; ".join(n for n in notes if n)


def hint_offer(engine: Any) -> str:
    """LRN-20, D112: the hint offered once on the open step, as one quiet line (`notice`, code `hint_offer`) the
    student answers with the hint chip or not at all. The step is marked offered here, and only here, so neither
    trigger offers it again."""
    state = engine.state
    if state.step in state.offered_steps:
        return ""
    state.offered_steps.append(state.step)
    engine.flow_signals["engine.idle_offer_due"] = True
    engine.events.emit({"type": "notice", "notice_id": new_id("ntc"), "level": "info", "code": HINT_OFFER_CODE,
                        "message_key": "notice.hint_offer", "params": {}, "action": None})  # fmt: skip
    return HINT_OFFER_NOTE


def pulled(engine: Any) -> None:
    """The student pulled a hint (the chip, "I'm stuck"): on a step the engine offered one, that hint answers the
    offer, so its `hint` event records `offer_accepted` (LRN-20)."""
    state = engine.state
    if state.step in state.offered_steps and state.step not in state.offer_pulled:
        state.offer_pulled.append(state.step)
        engine.next_hint_trigger = "offer_accepted"


def detour_card(engine: Any, turn: Any, detour: Mapping[str, Any]) -> str:
    """The detour offer as one engine-written question card ("Go over an earlier idea first" or "Not now"); a root
    gap's card says in one line what the mistake is really about ("Practise {idea} first" or "Not now")."""
    state = engine.state
    cards = engine.receipt_texts.cards
    root = bool(detour.get("root_gap"))
    accept = cards["root_gap_practise" if root else "offer_detour"].format(idea=detour["idea"],
                                                                          minutes=detour["minutes"])  # fmt: skip
    options = [{"label": accept, "recommended": True}, {"label": cards["offer_not_now"]}]
    prompt = cards["root_gap_prompt" if root else "offer_detour_prompt"].format(idea=detour["idea"])
    try:
        body = question_card(prompt, options, False, False)
    except HandlerError:
        return ""
    request_id = new_id("req")
    opened: dict[str, Any] = {"type": "card.open", "request_id": request_id, "part_id": "", "call_id": None,
                              "card": body}  # fmt: skip
    if root and detour.get("misconception_id"):
        # What the card is about, so the concept map can light the gap path from the earlier skill.
        opened["about"] = {"kind": "root_gap", "kc_id": detour["kc_id"], "root_kc_id": detour["prerequisite_kc_id"],
                           "misconception_id": detour["misconception_id"]}  # fmt: skip
    part = emit_part(engine.events, "card", [opened])
    engine._persist(turn, part, "card", body)
    engine.open_cards[request_id] = "offer"
    state.offers[request_id] = {"hint": None, "detour": body["options"][0]["id"], "kc_id": detour["kc_id"],
                                "prerequisite_kc_id": detour["prerequisite_kc_id"], "idea": detour["idea"]}  # fmt: skip
    engine.flow_signals["engine.detour_eligible"] = True
    if root:
        return ROOT_GAP_NOTE.format(idea=detour["idea"])
    return OFFER_NOTE.format(what=f"a short detour to an earlier idea ({detour['idea']})")


def root_gap(engine: Any, misconception: str | None) -> dict[str, Any] | None:
    """The detour to the earlier skill a matched misconception really comes from (its pack `root_gap`), when it can
    run now: the skill is installed with practice on it, the student has not declined a detour on this problem's KC,
    the return stack has room, and it was not offered for this misconception already in the session."""
    knowledge, source, state, s = engine.knowledge, engine.detours, engine.state, engine.deps.settings
    record = knowledge.index.misconceptions.get(misconception) if knowledge is not None and misconception else None
    if record is None or not record.root_kc_id or not record.root_idea or source is None:
        return None
    offered: set[str] = engine.root_gaps_offered
    kcs = problem_kcs(engine) or list(record.kc_ids)
    if record.id in offered or not kcs or kcs[0] in state.detour_declined:
        return None
    if len(state.detour_stack) >= s.detour_max_depth or (state.detour or {}).get("kc_id") == record.root_kc_id:
        return None
    if source.statement(record.root_kc_id) is None or not source.items(record.root_kc_id, 1):
        engine._log("info", "engine", "root_gap.unavailable", misconception_id=record.id, kc_id=record.root_kc_id)
        return None
    offered.add(record.id)
    engine._log("info", "engine", "root_gap.offered", misconception_id=record.id, kc_id=record.root_kc_id)
    return {"kc_id": kcs[0], "prerequisite_kc_id": record.root_kc_id, "minutes": s.detour_minutes,
            "idea": record.root_idea[:40], "root_gap": True, "misconception_id": record.id}  # fmt: skip


def _detour(engine: Any, turn: Any) -> dict[str, Any] | None:
    """The detour the policy offers now, for the first KC of the problem that has one."""
    source: Detours | None = engine.detours
    state, s = engine.state, engine.deps.settings
    kcs = problem_kcs(engine)
    if source is None or not kcs or failures(engine) < s.detour_failures:
        return None
    prerequisites = source.prerequisites(kcs)
    stack = _positions(state.detour_stack)
    for kc in kcs:
        decision = policies.detour_offer(kc, failures(engine), prerequisites.get(kc, {}), state.detour_declined,
                                         stack, s)  # fmt: skip
        turn.trace.policy_decision(decision.entry())
        target = decision.output["prerequisite_kc_id"]
        if decision.output["offer"] and target:
            return {"kc_id": kc, "prerequisite_kc_id": target, "minutes": decision.output["minutes"],
                    "idea": (source.statement(target) or "an earlier idea")[:120]}  # fmt: skip
    return None


def answered(engine: Any, request_id: str, answer: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """An offer card answered: ("hint" | "detour" | "declined", the offer), or None when the card was no offer. A
    declined detour is logged and not offered again for that KC in this session (LRN-23)."""
    offer = engine.state.offers.pop(request_id, None)
    if offer is None:
        return None
    chosen = set(answer.get("option_ids") or [])
    if offer.get("hint") and offer["hint"] in chosen:
        return "hint", offer
    if offer.get("detour") and offer["detour"] in chosen:
        return "detour", offer
    if offer.get("kc_id"):
        engine.state.detour_declined.append(str(offer["kc_id"]))
        engine._log("info", "engine", "detour.declined", kc_id=offer["kc_id"])
    return "declined", offer


class Offers:
    """Mixed into `Engine`: taking an offer the student accepted (a new turn), and the idle trigger's offer."""

    next_hint_trigger: str | None

    async def idle_offer(self: Any, decision: policies.Decision, ids: TurnIds | None = None) -> TurnResult:
        """LRN-20, D104, D112: the hint offer the idle trigger's decision allows, as an engine-written turn whose only
        content is the quiet `hint_offer` line; no model is called, so the offer never waits for the tutor."""
        turn = self._begin("ask", ids)
        with turn_scope(turn.turn_id):
            try:
                turn.trace.policy_decision(decision.entry())
                hint_offer(self)
                return cast("TurnResult", self._end(turn, None))
            except Exception:
                self._finish_crashed(turn)
                raise

    async def accept_offer(self: Any, choice: str, offer: Mapping[str, Any], text: str,
                           ids: TurnIds | None = None) -> TurnResult:  # fmt: skip
        if choice == "hint":
            self.next_hint_trigger = "offer_accepted"  # the hint event's trigger (LRN-20)
            return cast("TurnResult", await self.student_turn(text, ids, stuck=True))
        turn = self._begin("answer", ids)
        with turn_scope(turn.turn_id):
            try:
                return cast("TurnResult", await start_detour(self, turn, offer))
            except Exception:
                self._finish_crashed(turn)
                raise


async def start_detour(engine: Any, turn: Any, offer: Mapping[str, Any]) -> Any:
    """The accepted detour: the problem's place pushed on the return stack, then the first item on the prerequisite
    with a short explanation of it. With no item installed on it, the explanation only, and the problem goes on."""
    state, s = engine.state, engine.deps.settings
    target, idea = str(offer["prerequisite_kc_id"]), str(offer.get("idea") or "an earlier idea")
    source: Detours | None = engine.detours
    items = source.items(target, s.detour_items) if source is not None else []
    if not items or state.problem is None:
        return await engine._coach_turn(turn, intent_note=NO_DETOUR_NOTE.format(idea=idea))
    problem_ref, _ = engine._opportunity()
    here = policies.SavedPosition(str(offer["kc_id"]), problem_ref, state.step, {"board_id": state.board.board_id})
    try:
        policies.detour_push(_positions(state.detour_stack), here, s)
    except ValueError:  # a third nested detour is refused (LRN-23)
        return await engine._coach_turn(turn, intent_note=NO_DETOUR_NOTE.format(idea=idea))
    saved = {k: v for k, v in state_to_json(state).items() if k in SAVED}
    state.detour_stack.append({"kc_id": here.kc_id, "problem_ref": here.problem_ref, "step_index": here.step_index,
                               "saved": saved, "detour": state.detour})  # fmt: skip
    state.detour = {"kc_id": target, "items": items, "index": 0}
    _load(engine, turn, items[0])
    return await engine._coach_turn(turn, intent_note=DETOUR_NOTE.format(idea=idea))


def after_correct(engine: Any, turn: Any) -> str:
    """A detour item answered correctly: the next item, or back to the problem the detour left. Returns the note."""
    detour = engine.state.detour
    if detour is None:
        return ""
    detour["index"] = int(detour["index"]) + 1
    if detour["index"] < len(detour["items"]):
        _load(engine, turn, detour["items"][detour["index"]])
        return NEXT_NOTE
    return resume(engine, turn)


def resume(engine: Any, turn: Any) -> str:
    """The innermost saved place back: the same problem, step and board (LRN-23), shown with `board.reset` (reason
    `resume`) and the board's steps as they were."""
    state = engine.state
    if not state.detour_stack:
        state.detour = None
        return ""
    position = state.detour_stack.pop()
    restored = state_from_json({**state_to_json(state), **position["saved"]})
    for name in SAVED:
        setattr(state, name, getattr(restored, name))
    state.detour = position.get("detour")
    board = state.board
    events: list[dict[str, Any]] = [{"type": "board.reset", "board_id": board.board_id, "part_id": "",
                                     "problem": {"text": (state.problem.statement if state.problem else "")[:4000],
                                                 "latex": None}, "reason": "resume"}]  # fmt: skip
    for step in board.steps:
        events.append({"type": "board.step", "board_id": board.board_id, "step_id": step.step_id, "index": step.index,
                       "latex": step.latex[:4000], "reason": step.reason, "owner": step.owner, "status": step.status,
                       "check_id": step.check_id, "error": None})  # fmt: skip
    part = emit_part(engine.events, "board", events)
    engine._persist(turn, part, "board", events)
    return RESUME_NOTE


def _load(engine: Any, turn: Any, item: Mapping[str, Any]) -> None:
    """A detour item as the open problem, its board opened at once so the student sees the question."""
    state = engine.state
    part_id = str(item["parts"][0]["id"])
    part = item["parts"][0]
    statement = " ".join([str(item.get("stem", "")), str(part["prompt"])]).strip()
    engine._new_problem(turn)
    state.problem = Problem(str(item["id"]), statement, f"{item['id']}#{part_id}")
    state.solved = False
    state.practice = {"item": dict(item), "part": part_id, "shown_ms": engine.events.clock_ms()}
    state.student_lines.append(statement)
    state.transcript.append({"role": "user", "content": statement, "kind": "problem"})
    plan, values = flows.plan_from_item(dict(item), part_id)
    state.check_values.update(values)
    state.plan, state.step, state.rung = plan, 1, 0
    state.attempts, state.deflected = {}, set()
    opened = open_board(state)
    if opened is not None:
        part_ref = emit_part(engine.events, "board", [opened])
        engine._persist(turn, part_ref, "board", [opened])
