"""The S2 flows on the live turn loop (AGT-19, PED-22, PED-24, PRV-13 `beyond_level`, LRN-26).

`tutor_flows` holds the flows as state machines; this module is where the running engine takes their edges. From S2
(the build stage, `Settings.build_stage`) a new problem in guided mode gets its own `GuidedFlow`, and every point of
the turn path the diagram names moves it: the input guard, the Diagnoser (and its clarifying question), the plan's
checks, the student's reply (answer, "I'm stuck" or a side question), the code's verdict and the wrap-up. A wrong
answer is where the engine raises the rung or targets a misconception; a matched misconception runs the PED-22 flow,
whose evidence goes to the learner model; and the saved-question triggers of PED-24 that the engine's signals reach
(the stuck cap, a misconception that survives its fixes, a question outside the installed content) save one local
question each. A node the diagram has no edge to is logged and skipped: the flow never fails a turn.

Everything here is engine code over engine state (AGT-03): no model chooses an edge."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_core.assets.visuals import scenarios_for
from agent_core.engine.answer_policy import paced_rung
from agent_core.engine.tutor_flows import MODE_CONTRACTS, GuidedFlow, MisconceptionFlow, contract, rung_source
from agent_core.retrieval.index import terms

SUBJECT_CODES = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}
MISCONCEPTION_NOTES = {
    "probe": "a possible misconception: {name}. Ask one probing question that would show whether the student thinks "
    "this; do not name it yet",
    "name_and_fix": "the student shows a misconception: {name}. Name it gently, without blame, and give this fix in "
    "your own words: {fix} Then ask one short question that tells the right idea from the wrong one",
    "switch_representation": "the misconception {name} is still there after the fix. Explain the idea again with a "
    "different representation from before (a diagram, a table or a concrete example)",
    "continue": "the probe did not confirm the misconception; carry on with the step",
    "resolving": "the student now answers the check correctly; say what they did right in one sentence",
}

# A predict scenario built around this misconception's usual guess (VSL-47) is the different representation to use.
SCENARIO_NOTE = (
    '. A predict-first simulation is built for this: show {kind} with params {{"predict": "{id}"}}; the student '
    "writes a prediction before it runs, so do not tell them the result first"
)


def guided(engine: Any) -> bool:
    """The guided flow drives this session's turns: S2 or later, in guided mode."""
    return bool(engine.deps.stage >= "S2" and engine.state.mode == "guided")


def _take(engine: Any, edge: Callable[[GuidedFlow], Any], *at: str) -> None:
    """Take one edge of the guided flow when it is at one of the nodes `at` (any node when none is named); a missing
    edge is logged, never raised."""
    flow: GuidedFlow | None = engine.guided
    if flow is None or (at and flow.node not in at):
        return
    try:
        edge(flow)
    except ValueError as error:
        engine._log("warning", "engine", "flow.edge_refused", node=flow.node, detail=str(error)[:80])


# --- the guided flow (AGT-19) ---------------------------------------------------------------------------------------


def out_of_scope(engine: Any) -> None:
    """A new question the input guard answered with a template: the flow's template edge."""
    if guided(engine) and engine.state.problem is None:
        engine.guided = GuidedFlow()
        engine.guided.guard(False)


def new_problem(engine: Any) -> None:
    """A new problem passed the input guard: a fresh flow at the Diagnoser (or none outside guided S2)."""
    engine.guided = None
    if guided(engine):
        engine.guided = GuidedFlow()
        engine.guided.guard(True)


def diagnosed(engine: Any, needs_clarification: bool, conceptual: bool = False) -> None:
    _take(engine, lambda f: f.diagnosed(needs_clarification, conceptual), "diagnoser")


def plan_checked(engine: Any, passed: bool) -> None:
    """One verification of the Solver's plan: passed, or failed (twice is the honest blocker)."""
    _take(engine, lambda f: f.plan_checked(passed), "solver", "checker_plan")


def reply(engine: Any, kind: str) -> None:
    """The student's reply to a rung: an answer or line, "I'm stuck", or a side question."""
    _take(engine, lambda f: f.reply(kind), "coach_rung")  # type: ignore[arg-type]


def verdict(engine: Any, status: str) -> None:
    """The code's verdict on the reply: correct (the last step wraps up), incorrect (the rung or the misconception),
    or a line no check could settle, which goes back to the step's rung."""
    last = bool(engine.state.solved)
    if status == "correct":
        _take(engine, lambda f: f.checked(True, last), "checker_diagnoser")
    elif status == "incorrect":
        _take(engine, lambda f: f.checked(False, False), "checker_diagnoser")
    else:
        _take(engine, lambda f: f._go("coach_rung"), "checker_diagnoser")


def raise_rung(engine: Any, turn: Any) -> str:
    """A wrong answer with no misconception matched: the engine raises the rung by one (to the start rung of LRN-18
    on the first), never past `wrong_answer_max_rung` (rung 5 is only ever asked for, PED-07). The rung shown is a
    `hint` event, its content authored (PED-04). Returns the Coach's note."""
    state, settings = engine.state, engine.deps.settings
    allowed = contract(state.mode if state.mode in MODE_CONTRACTS else "guided", settings).rungs
    cap = min(settings.wrong_answer_max_rung, max(allowed)) if allowed else 0
    if state.hint_step != state.step:  # the rung resets when the step changes (PED-06)
        state.rung, state.hint_step, state.hint_at_ms = 0, state.step, None
    if cap == 0 or state.rung >= cap:
        return ""
    start = engine.first_rung(turn) if state.rung == 0 else 1
    wanted = min(max(start, 1) if state.rung == 0 else state.rung + 1, cap)
    rung = paced_rung(state, wanted, engine._teaching_skill())  # a skill may slow the pace (SKL-14)
    if rung <= state.rung:
        return ""
    state.rung = rung
    practice = state.practice
    source = rung_source(rung, item=practice["item"] if practice else None,
                         part_id=practice["part"] if practice else None, step_index=state.step,
                         skill=engine._teaching_skill(), generic=engine.deps.templates.hints)  # fmt: skip
    payload = engine._hint_payload(rung, source.event_source == "skill_ladder")
    payload["trigger"] = "auto_start"  # raised by the engine after the answer, not asked for
    engine._learn(turn, [("hint", payload, "engine")])
    note = f"the answer was wrong, so the hint rises to rung {rung}"
    if source.source != "generic_template":
        note += f"; rephrase this authored hint: {source.text}"
    return note


# --- misconceptions (PED-22) ----------------------------------------------------------------------------------------


def _misconception(engine: Any, misconception_id: str) -> tuple[str, str, str]:
    """(KC id, student name, fix text) of a misconception from the installed packs' index."""
    knowledge = engine.knowledge
    record = knowledge.index.misconceptions.get(misconception_id) if knowledge is not None else None
    if record is None:
        return "", misconception_id.rsplit(".", 1)[-1].replace("_", " "), ""
    passage = knowledge.index.passage(record.pack_id, f"{misconception_id}#1")
    fix = passage.text.split("\n\n", 1)[-1] if passage is not None else ""
    return (record.kc_ids[0] if record.kc_ids else ""), record.student_name, fix[:400]


def misconception_fix(engine: Any, misconception_id: str) -> str:
    """A misconception's fix from the installed packs' index ("" when it has none)."""
    return _misconception(engine, misconception_id)[2]


def misconception_step(engine: Any, turn: Any, status: str, matched: str | None,
                       source: str = "signature", recorded: bool = False) -> str:  # fmt: skip
    """One answer through the misconception flows: an open probe or check is answered by it, and a misconception it
    matched starts or moves its flow. Evidence events go to the learner model, once per match: `recorded` says the
    marker already recorded this match (ASM-13), so the flow moves without a second event. A misconception that
    survives its fixes saves the question (PED-24). Returns the Coach's note ("" when no flow moved)."""
    flows: dict[str, MisconceptionFlow] = engine.misconception_flows
    notes = []
    for mc_id, flow in list(flows.items()):
        if mc_id == matched:
            continue
        if flow.node == "probe":
            flow.probe_answered(False)
        elif flow.node == "discriminating_check":
            flow.check_answered(status == "correct")
        else:
            continue
        notes.append(_note(engine, flow))
    if matched is not None:
        kc_id, _, _ = _misconception(engine, matched)
        held = flows.get(matched)
        flow = (
            held
            if held is not None and held.node not in ("continue", "resolving")
            else MisconceptionFlow(
                matched, kc_id, engine.deps.settings.misconception_active_evidence, fixes=held.fixes if held else 0
            )
        )
        flows[matched] = flow
        seen = engine.misconception_seen[matched] = engine.misconception_seen.get(matched, 0) + 1
        # The learner model's evidence so far (it may already hold this session's earlier matches) and this match.
        prior = engine.misconception_evidence(matched) if engine.misconception_evidence is not None else 0
        prior -= 1 if recorded and prior > 0 else 0  # the marker's event for this match is already in the model
        before = len(flow.events)
        if flow.node == "probe":
            flow.probe_answered(True)
        elif flow.node == "discriminating_check":
            flow.check_answered(False)
        else:
            flow.matched(max(prior + 1, seen), source)  # type: ignore[arg-type]
        if flow.node == "name_and_fix":
            flow.check_item()  # the discriminating question comes with the fix; the next answer settles it
        if kc_id and not recorded:
            engine._learn(turn, [(kind, payload, "checker") for kind, payload in flow.events[before:]])
        notes.append(_note(engine, flow))
        if flow.survived(engine.deps.settings.misconception_fixes_before_save):
            engine._saved("repeated_error")
    return "; ".join(n for n in notes if n)


def _note(engine: Any, flow: MisconceptionFlow) -> str:
    _, name, fix = _misconception(engine, flow.misconception_id)
    node = "name_and_fix" if flow.node == "discriminating_check" else flow.node
    template = MISCONCEPTION_NOTES.get(node)
    note = template.format(name=name, fix=fix) if template else ""
    if node == "switch_representation":
        note += "".join(SCENARIO_NOTE.format(kind=s["kind"], id=s["id"]) for s in scenarios_for(flow.misconception_id))
    return note


# --- saved questions and the signals for soft rules (PED-24, LRN-26, PED-14) ----------------------------------------


def stuck(engine: Any) -> bool:
    """The stuck cap (LRN-26) on the KCs in play: the question is saved for human help and the soft rules see
    `engine.stuck_cap_reached`."""
    kcs = engine._item_kcs() or engine._session_kcs()
    if engine.stuck_cap is None or not kcs or not engine.stuck_cap(kcs):
        return False
    engine.flow_signals["engine.stuck_cap_reached"] = True
    engine._saved("stuck")
    return True


def outside_content(engine: Any, topics: list[str], diagnosis: dict[str, Any] | None,
                    topic_id: str | None = None) -> bool:  # fmt: skip
    """A question the installed packs do not cover (no topic matches it, the Diagnoser found it outside the
    syllabus, or placed it in a subject with no pack installed at the level): saved for human help, and
    `content.out_of_pack` for the soft rules (PED-24, PED-14, D94)."""
    from agent_core.engine import team

    if engine.knowledge is None:
        return False
    other = team.other_subject(engine, diagnosis, topic_id)
    if other is not None and team.installed(engine, other):
        return False  # another installed subject's question: offered there, not saved (D94)
    if topics and other is None and (diagnosis is None or diagnosis.get("in_scope") is not False):
        return False
    engine.flow_signals["content.out_of_pack"] = True
    engine._saved("outside_content")
    engine.unsure_lesson = nearest_lesson(engine)  # the pack record the not-sure reply cites (PRD-05, TOL-21)
    return True


def nearest_lesson(engine: Any) -> dict[str, str] | None:
    """The installed lesson closest to the open question at the student's level (TABLE PED-16a `not_sure_cite`,
    UXS-20 "Tutor unsure"): its lesson id, title and TOL-21 pack citation; None without packs or a match."""
    knowledge, problem = engine.knowledge, engine.state.problem
    if knowledge is None or problem is None:
        return None
    # A related lesson must belong to the question's subject, even when that subject has no installed pack.
    from agent_core.engine import team

    diagnosed = engine.diagnosis
    named = team.question_subject(diagnosed.output, diagnosed.topic_id) if diagnosed is not None else None
    subject = SUBJECT_CODES.get(named or engine.state.subject)
    if subject is None:
        return None
    # Common question/instruction words and bare quantities are not evidence that a lesson covers the topic.
    ignored = set(
        "a an and are as at be by can could do does for from give given guide has have how i in is it "
        "me my of on one or please question says show step take that the then this through to us use "
        "was we what when where which with would you your".split()
    )
    query = " ".join(
        word for word in terms(problem.statement) if len(word) > 1 and not word.isnumeric() and word not in ignored
    )
    if not query:
        return None
    hits = knowledge.index.search(
        query, subjects=[subject], kinds=("lesson",), levels=[engine.state.level.lower()], limit=1
    )
    if not hits:
        return None
    passage = hits[0].passage
    return {"lesson_id": passage.record_id, "title": passage.title,
            "cite": f"{passage.pack_id}@{passage.pack_version}:{passage.id}"}  # fmt: skip


# --- beyond the level (PRV-13) --------------------------------------------------------------------------------------


def nearest_topic_record(engine: Any, text: str) -> tuple[str, str] | None:
    """The closest topic at the student's level for a question beyond it (TABLE PRV-13a): the best-scoring topic of
    the installed packs in the session's subject and level, by its title; None without packs or a match."""
    knowledge = engine.knowledge
    if knowledge is None or not text.strip():
        return None
    code = SUBJECT_CODES.get(engine.state.subject)
    level = engine.state.level.lower()
    limit = engine.deps.settings.topic_chooser_options * 4
    for topic, _ in knowledge.index.topic_scores(text, subjects=[code] if code else None, limit=limit):
        parts = topic.split(".")
        if len(parts) > 2 and parts[2] == level:
            node = knowledge.index.nodes.get(topic)
            return (str(topic), str(node.title)) if node is not None else None
    return None


def nearest_topic(engine: Any, text: str) -> str | None:
    found = nearest_topic_record(engine, text)
    return found[1] if found is not None else None
