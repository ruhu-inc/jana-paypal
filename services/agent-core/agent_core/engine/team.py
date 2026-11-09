"""Delegation order from S2 (AGT-15, TABLE AGT-15a) and the Diagnoser's failure path (AGT-27).

| Situation | Order |
| --- | --- |
| New problem | Diagnoser, then Solver, then the Checker only for a value no worker check covers (AGT-13) |
| Bank item with a stored, verified solution | Diagnoser only; the plan is the item's; the Solver is never called |
| Student answer or line the code could not confirm | Checker and Diagnoser in parallel, both joined before the Coach |

"Parallel" is `asyncio.gather`: on cloud candidates the two calls overlap; on a local runtime they queue on its slots
in priority order (MDL-22). A Diagnoser that fails twice is replaced by the curriculum keyword classifier, and the
student picks the topic from a card. The engine, never a model, decides each of these steps (AGT-03)."""

from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass, field, replace
from typing import Any

from agent_core.engine.events import new_id
from agent_core.engine.slice import requested_unknown
from agent_core.engine.state import numbers_in
from agent_core.engine.ui import HandlerError, emit_part, open_board, question_card, step_error, subject_card

SUBJECT_CODES = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}
CHECK_ID = re.compile(r"chk_[A-Za-z0-9]{3,64}")


@dataclass
class Diagnosed:
    """What the Diagnoser said about the open problem, with its numbered refs resolved to ids (engine state)."""

    output: dict[str, Any]
    topic_id: str | None
    kc_ids: list[str] = field(default_factory=list)
    misconception_id: str | None = None
    topics: list[str] = field(default_factory=list)  # the candidate topics the installed packs offered for it


def candidates(engine: Any, question: str) -> tuple[list[str], list[str], list[str]]:
    """Numbered candidates for the Diagnoser: topics, their knowledge components and misconceptions (from the
    installed packs' index; none without one)."""
    knowledge = engine.knowledge
    if knowledge is None:
        return [], [], []
    index = knowledge.index
    code = SUBJECT_CODES.get(engine.state.subject)
    settings = engine.deps.settings
    # The first question of a session started by asking may move it to the question's subject (D94): its candidates
    # come from every installed subject, so the Diagnoser is not shown only the session's default subject.
    scope = None if first_asked(engine) or not code else [code]
    lean = engine.state.hardware_class in settings.lean_classes  # each candidate line costs reading time (D128)
    topics = [t for t, _ in index.topic_scores(question, subjects=scope,
                                               limit=settings.topic_chooser_options + (0 if lean else 2))]  # fmt: skip
    kcs = ranked_kcs(index, question, topics,
                     settings.diagnoser_kc_candidates_lean if lean else settings.diagnoser_kc_candidates)  # fmt: skip
    misconceptions = sorted({m for k in kcs for m in index.kcs[k].misconception_ids if m in index.misconceptions})
    return topics, kcs, misconceptions[:12]


def ranked_kcs(index: Any, question: str, topics: list[str], cap: int) -> list[str]:
    """The candidate topics' KCs for the Diagnoser, the ones the question's words match best first, then the rest in
    topic order, up to `cap` (AGT-01, LRN-06): a long first topic never crowds out the KC the question is about."""
    in_topics = [k for t in topics for k in index.kcs_in_topic(t)]
    allowed = set(in_topics)
    k = index.settings.retrieval_rrf_k
    pool = index.settings.retrieval_candidate_pool
    scores: dict[str, float] = {}
    # The KCs of the pack passages the question's words find (lessons, worked items), and the KC records themselves.
    for position, hit in enumerate(index.search(question, limit=pool), start=1):
        for kc in hit.passage.kc_ids:
            if kc in allowed:
                scores[kc] = scores.get(kc, 0.0) + 1.0 / (k + position)
    for position, (kind, record_id, _) in enumerate(index.curriculum_search(question, limit=pool), start=1):
        if kind == "kc" and record_id in allowed:
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (k + position)
    matched = sorted(scores, key=lambda kc: (-scores[kc], kc))
    return list(dict.fromkeys([*matched, *in_topics]))[:cap]


def topic_title(engine: Any, topic_id: str) -> str:
    knowledge = engine.knowledge
    node = knowledge.index.nodes.get(topic_id) if knowledge is not None else None
    return node.title if node is not None else topic_id.rsplit(".", 1)[-1].replace("_", " ")


def _task(engine: Any, question: str, answer: str | None, topics: list[str], kcs: list[str],
          misconceptions: list[str]) -> str:  # fmt: skip
    from agent_core.engine.turn import _escape

    lines = [f"<student-message>\n{_escape(question)}\n</student-message>"]
    if answer is not None:
        lines.append(f"The student's answer:\n<student-message>\n{_escape(answer)}\n</student-message>")
    lines.append(f"Subject: {engine.state.subject}; level: {engine.state.level}")
    if topics:
        lines.append("Topics:\n" + "\n".join(f"{n}. {topic_title(engine, t)}" for n, t in enumerate(topics, 1)))
    if kcs:
        index = engine.knowledge.index
        listed = [f"{n}. {index.kcs[k].statement[:120]}" for n, k in enumerate(kcs, 1)]
        lines.append("Knowledge components:\n" + "\n".join(listed))
    if misconceptions:
        index = engine.knowledge.index
        lines.append("Misconceptions:\n" + "\n".join(f"{n}. {index.misconceptions[m].student_name}"
                                                     for n, m in enumerate(misconceptions, 1)))  # fmt: skip
    return "\n\n".join(lines)


def _resolve(output: dict[str, Any], topics: list[str], kcs: list[str], misconceptions: list[str]) -> Diagnosed:
    def pick(items: list[str], ref: Any) -> str | None:
        return items[ref - 1] if isinstance(ref, int) and 1 <= ref <= len(items) else None

    return Diagnosed(output, pick(topics, output.get("topic_ref")),
                     [k for k in (pick(kcs, r) for r in output.get("kc_refs") or []) if k is not None],
                     pick(misconceptions, output.get("misconception_ref")), list(topics))  # fmt: skip


AT_REST = re.compile(r"\b(?:from|at) rest\b|\bstationary\b", re.IGNORECASE)


def photo_knowns_valid(output: dict[str, Any], question: str) -> bool:
    """A photo's given quantities must quote the student's confirmed text, including each written value/unit.
    An inconsistent diagnosis is retried; it cannot open a Solver task with missing or invented givens.
    """
    confirmed = " ".join(question.split()).casefold()
    for known in output.get("knowns") or []:
        quote = " ".join(str(known["quote"]).split())
        value, unit = str(known["value"]).strip(), str(known["unit"]).strip()
        if not quote or quote.casefold() not in confirmed:
            return False
        wanted, quoted = numbers_in(value), numbers_in(quote)
        if wanted == [0] and AT_REST.search(quote):  # "dropped from rest" gives a zero speed it never writes
            continue
        if not value or (wanted and any(number not in quoted for number in wanted)):
            return False
        if not wanted and value.casefold() not in quote.casefold():
            return False
        if unit and re.search(rf"(?<![A-Za-zµμ]){re.escape(unit)}(?![A-Za-zµμ])", quote) is None:
            return False
    return True


def intake_schema(full: dict[str, Any], topics: int, kcs: int, misconceptions: int,
                  goal: str | None = None, *, scalar_syntax: bool = False
                  ) -> tuple[dict[str, Any], dict[str, Any]]:  # fmt: skip
    """Engine-owned typed-intake projection; photos keep their full quotation contract."""
    from agent_core.engine import lean

    schema = copy.deepcopy(lean.decode_schema("agents/diagnosis", full))
    props = schema["properties"]
    if scalar_syntax:
        props["knowns"]["items"]["properties"]["symbol"] = {
            **props["knowns"]["items"]["properties"]["symbol"], "pattern": r"^[A-Za-z][A-Za-z0-9_]{0,19}$"
        }
        value = props["knowns"]["items"]["properties"]["value"]
        props["knowns"]["items"]["properties"]["value"] = {
            **value, "pattern": r"^[A-Za-z0-9_ +*/().,%?:'=-]+$"
        }
    props["topic_ref"]["enum"] = [None, *range(1, topics + 1)]
    if kcs:
        props["kc_refs"]["items"]["enum"] = list(range(1, kcs + 1))
    else:
        props["kc_refs"]["maxItems"] = 0
    neutral: dict[str, Any] = {}
    if misconceptions:
        props["misconception_ref"]["enum"] = [None, *range(1, misconceptions + 1)]
    else:
        neutral = {"misconception_ref": None, "evidence": []}
        # Optional neutral fields retain compatibility with earlier lean runtimes. The prompt asks only for
        # required fields; non-neutral claims still fail validation and cannot create a misconception.
        props["misconception_ref"] = {"type": "null", "const": None}
        props["evidence"] = {"type": "array", "items": {"type": "string"}, "maxItems": 0}
        schema["required"] = [f for f in schema["required"] if f not in neutral]
    # Goal ownership is checked after decoding. A model may put an explicit unknown placeholder in
    # knowns; that is normalized separately, while an actual value under the goal is always rejected.
    return schema, neutral


async def diagnose(engine: Any, turn: Any, question: str, answer: str | None = None,
                   attempts: int | None = None) -> Diagnosed | None:  # fmt: skip
    """The Diagnoser on the question (and the student's answer), up to `attempts` times; None when every attempt
    failed (no valid diagnosis)."""
    topics, kcs, misconceptions = candidates(engine, question)
    prior: Diagnosed | None = engine.diagnosis if answer is not None else None
    if prior is not None:
        assert answer is not None
        # The open problem is already parsed and classified. Read the new evidence without regenerating its
        # givens, unknowns and curriculum classification, which otherwise costs another full local decode.
        from agent_core.contracts import validator

        if not misconceptions:
            # No authored label is eligible, so another inference could only return null. Preserve the
            # established problem and clear any older label exactly as a null classification would.
            return replace(prior, output={**prior.output, "misconception_ref": None, "evidence": [],
                                          "inference": ""}, misconception_id=None)
        full = engine.deps.bundled_schemas["agents/diagnosis"]
        fields = ("misconception_ref",)
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": list(fields),
            "properties": {"misconception_ref": {
                **full["properties"]["misconception_ref"], "enum": [None, *range(1, len(misconceptions) + 1)]
            }},
        }
        task = _task(engine, question, answer, [], [], misconceptions)
        task += (
            "\n\nThis is feedback on the existing problem, not a new question. Classify only the student's "
            "new answer against the numbered misconceptions; do not solve or reparse the problem. "
            "Use null when the answer does not support a listed misconception. "
            "The engine retains the exact student answer as evidence; do not repeat it."
        )
        envelope = await engine.delegate("diagnoser", task, turn, tools=[], decode_schema=schema)
        output = envelope.get("output")
        if engine.cancelled.is_set() or not envelope["schema_valid"] or not isinstance(output, dict):
            return None
        ref = output.get("misconception_ref")
        if ref is not None and (type(ref) is not int or not 1 <= ref <= len(misconceptions)):
            return None  # a legacy full-shape reply must respect the same eligible references
        combined = {
            **prior.output,
            "misconception_ref": output["misconception_ref"],
            "evidence": [answer[:200]] if output["misconception_ref"] is not None else [],
            "inference": "",
        }
        if list(validator("agents/diagnosis").iter_errors(combined)):
            return None
        ref = output.get("misconception_ref")
        match = misconceptions[ref - 1] if type(ref) is int and 1 <= ref <= len(misconceptions) else None
        return replace(prior, output=combined, misconception_id=match)
    if answer is None and engine.state.hardware_class in engine.deps.settings.lean_classes:
        misconceptions = []  # only a student answer can show one (D128)
    task = _task(engine, question, answer, topics, kcs, misconceptions)
    goal = requested_unknown(question)
    if goal:
        task += (
            f"\n\nThe student explicitly uses {goal} for the requested unknown. Reserve that symbol for the "
            "unknown; use different symbols for the given quantities. Keep the given values unchanged."
        )
    # Typed givens need no copied quotation or free-form diagnostic metadata. Keep the same bounded
    # projection on online devices, then restore neutral fields and validate the full contract below.
    # Photos retain their quotes and the original provenance check.
    compact = engine._input_source == "student_text"
    compact_schema = None
    neutral: dict[str, Any] = {}
    if compact:
        from agent_core.engine import lean

        compact_schema, neutral = intake_schema(
            engine.deps.bundled_schemas["agents/diagnosis"], len(topics), len(kcs), len(misconceptions), goal,
            scalar_syntax=bool(goal and question.isascii() and numbers_in(question)),
        )
    for _ in range(attempts or engine.deps.settings.diagnoser_attempts):
        # The numbered candidates already contain the relevant pack records. Decode the diagnosis directly
        # instead of spending the short role budget on a redundant lookup and an unconstrained first reply.
        envelope = await engine.delegate(
            "diagnoser", task, turn, tools=[] if topics else None, decode_schema=compact_schema
        )
        if engine.cancelled.is_set():
            return None
        if not envelope["schema_valid"]:
            from agent_core.engine.turn import _escape

            rejected = str(envelope.get("raw_text") or "")[:1600]
            task += (
                "\n\nRepair the previous diagnosis to the current contract. Do not repeat the same invalid shape. "
                "The requested unknown is not a given. A missing value is not a given value.\n"
                f"<previous-diagnosis>\n{_escape(rejected)}\n</previous-diagnosis>"
            )
        if envelope["schema_valid"] and isinstance(envelope["output"], dict):
            if compact:
                from agent_core.contracts import validator

                envelope["output"] = {**lean.completed("agents/diagnosis", envelope["output"]), **neutral}
                envelope["output"]["knowns"] = [
                    k for k in envelope["output"].get("knowns", [])
                    if str(k.get("value", "")).strip().casefold() not in ("", ".", "?", "unknown")
                ]
                if list(validator("agents/diagnosis").iter_errors(envelope["output"])):
                    continue
            if engine._input_source == "ocr_text" and not photo_knowns_valid(envelope["output"], question):
                continue
            output = envelope["output"]
            if compact:
                invalid = [k for k in output.get("knowns") or []
                           if not engine._given(str(k.get("value") or ""))
                           and str(k.get("value") or "").strip().casefold() not in question.casefold()]
                if invalid:
                    task += ("\n\nA previous given was not copied from the question. Reread the original values "
                             "and units. Do not invent punctuation, compute a value, or ask for facts already stated.")
                    continue
            if (output.get("topic_ref") not in [None, *range(1, len(topics) + 1)]
                    or any(k not in range(1, len(kcs) + 1) for k in output.get("kc_refs") or [])
                    or output.get("misconception_ref") not in [None, *range(1, len(misconceptions) + 1)]):
                task += ("\n\nUse only reference numbers actually supplied in the candidate lists; "
                         "use null if none fits.")
                continue
            if goal and any(str(k.get("symbol", "")).strip() == goal for k in output.get("knowns") or []):
                engine._log("info", "engine", "diagnosis.goal_given_conflict")
                task += (
                    f"\n\nThe previous diagnosis put {goal} among the givens. It is the requested unknown. "
                    "Correct the notation without changing the given values."
                )
                continue  # never publish a contradictory Given panel or plan against it
            if goal and len(output.get("unknowns") or []) <= 1:
                output = {**output, "unknowns": [goal]}  # source-owned notation, not a computed value
            return _resolve(output, topics, kcs, misconceptions)
    return None


async def new_problem(engine: Any, turn: Any, question: str) -> Any | None:
    """S2: the Diagnoser before the Solver. Returns None to go on to the plan, or the finished turn when the
    Diagnoser failed twice and the student is asked to pick the topic (AGT-27), or when the question needs one
    clarifying question first (AGT-19: the Coach asks it and the turn ends). A question in another subject moves a
    new asked session there or is offered as a new question there (D94); one the installed packs do not cover is
    saved for human help and goes on (PED-24)."""
    from agent_core.engine import live_flows

    diagnosed = await diagnose(engine, turn, question)
    if engine.cancelled.is_set():
        return engine._finish_interrupted(turn)
    if diagnosed is None:
        return topic_chooser(engine, turn, question)
    other = other_subject(engine, diagnosed.output, diagnosed.topic_id)
    if other is not None and may_switch(engine, other):
        # The first question of a session started by asking decides its subject (AGT-15, D94): the candidates and
        # the Diagnoser run again on that subject before the Solver.
        engine.state.subject = other
        rerun = await diagnose(engine, turn, question)
        if engine.cancelled.is_set():
            return engine._finish_interrupted(turn)
        engine.state.topic_id = rerun.topic_id if rerun is not None else None
        engine.events.emit({"type": "session.updated", "subject": other, "topic_id": engine.state.topic_id})
        if rerun is None:
            return topic_chooser(engine, turn, question)
        diagnosed, other = rerun, None
    else:
        session_topic(engine, diagnosed)
    engine.diagnosis = diagnosed
    # The title is the session list's alone: it is named while the Solver plans and settled before the Coach replies
    # (`Engine._settle_title`), so it costs the student no wait (SPEC §12).
    engine.title_pending = asyncio.ensure_future(name_session(engine, diagnosed))
    if other is not None:
        offer_subject(engine, turn, other)
    live_flows.outside_content(engine, diagnosed.topics, diagnosed.output, diagnosed.topic_id)
    if diagnosed.output.get("clarifying_question"):
        live_flows.diagnosed(engine, True)
        engine.clarifying = question  # the student's reply completes the question (`Engine._clarified`)
        # The Diagnoser's own words never reach tutor-context (PRM-15): the Coach is told only what to do.
        return await engine._coach_turn(turn, intent_note="the question is missing information or is ambiguous: ask "
                                                          "the student exactly one short question to clear it up; "
                                                          "do not start solving")  # fmt: skip
    if conceptual(engine, diagnosed.output):
        # A definition or explanation with no quantity to work from has nothing for the Solver to compute: its tools
        # need numbers, so planning only spends model calls on failed checks and ends in the honest blocker (D122).
        live_flows.diagnosed(engine, False, conceptual=True)
        return await engine._coach_turn(turn, intent_note=CONCEPT_NOTE)
    live_flows.diagnosed(engine, False)
    return None


CONCEPTUAL_KINDS = frozenset({"definition", "explanation"})
CONCEPT_NOTE = (
    "a definition or explanation question with nothing to calculate: explain the idea briefly in your own words "
    "from the topic, naming any formula in words and symbols, then ask the student one short question to check "
    "they have it; do not introduce numerical values or worked calculations"
)


def conceptual(engine: Any, output: dict[str, Any]) -> bool:
    """The Diagnoser read a definition or explanation and the question states no quantity to work from (D122)."""
    if output.get("question_kind") not in CONCEPTUAL_KINDS:
        return False
    return not any(str(k.get("value") or "").strip() and engine._given(str(k.get("value")))
                   for k in output.get("knowns") or [] if isinstance(k, dict))  # fmt: skip


def show_knowns(engine: Any, turn: Any) -> None:
    """The knowns panel from the question's parse, before any model answers the student (UXS-14, API-12): Given is
    each quantity the Diagnoser read whose numbers the question itself states (a value the question does not give is
    left off: no unchecked number reaches the board, AGT-10), labelled by its quote when the question has those words;
    Find is what it asks for. Engine-written; nothing is shown without a diagnosis of this problem."""
    state, diagnosed = engine.state, engine.diagnosis
    if diagnosed is None or state.problem is None:
        return
    statement = state.problem.statement
    knowns: list[dict[str, Any]] = []
    for raw in diagnosed.output.get("knowns") or []:
        value, symbol = str(raw.get("value") or "").strip(), str(raw.get("symbol") or "").strip()
        if not value or not symbol or not engine._given(value):
            continue
        quote = str(raw.get("quote") or "").strip()
        label = quote if quote and quote.lower() in statement.lower() else symbol
        knowns.append({"known_id": f"kn_{len(knowns) + 1:02d}", "symbol": symbol[:32], "label": label[:80],
                       "value": value[:64], "unit": str(raw.get("unit") or "").strip()[:32] or None,
                       "source": "question", "check_id": None})  # fmt: skip
    unknowns = [{"symbol": str(u)[:32], "label": str(u)[:80], "unit": None}
                for u in (diagnosed.output.get("unknowns") or []) if str(u).strip()]  # fmt: skip
    if not knowns and not unknowns:
        return
    opened = open_board(state)
    state.board.knowns, state.board.unknowns = knowns[:30], unknowns[:10]
    event = {"type": "board.knowns", "board_id": state.board.board_id, "knowns": state.board.knowns,
             "unknowns": state.board.unknowns, "conventions": []}  # fmt: skip
    events = [opened, event] if opened else [event]
    part = emit_part(engine.events, "board", events)
    engine._persist(turn, part, "board", events)


def topic_chooser(engine: Any, turn: Any, question: str) -> Any:
    """The Diagnoser's fallback: the curriculum keyword classifier's best topics as a question card; the student
    picks one or types it. Engine-written, so nothing here is a model's text."""
    topics, _, _ = candidates(engine, question)
    shown = topics[: engine.deps.settings.topic_chooser_options]
    options = [{"label": topic_title(engine, t)[:60], "latex": None, "recommended": n == 0}
               for n, t in enumerate(shown)]  # fmt: skip
    prompt = engine.receipt_texts.cards["topic_chooser"]
    engine._ack(turn, engine.receipt_texts.cards["topic_chooser_ack"])
    try:
        body = question_card(prompt, options, True, False)
    except HandlerError:
        body = question_card(prompt, [], True, False)
    request_id = new_id("req")
    part = emit_part(engine.events, "card", [{"type": "card.open", "request_id": request_id, "part_id": "",
                                              "call_id": None, "card": body}])  # fmt: skip
    engine._persist(turn, part, "card", body)
    engine.open_cards[request_id] = "topic_chooser"
    engine.topic_choices[request_id] = shown
    engine.state.transcript.append({"role": "assistant", "content": prompt, "kind": "tutor"})
    turn.degraded, turn.degraded_reason = True, "diagnoser_failed"
    turn.template = "card.topic_chooser"
    return engine._end(turn, None)


def installed(engine: Any, subject: str) -> bool:
    """A content pack for `subject` is installed at the session's level (D94)."""
    knowledge = engine.knowledge
    code = SUBJECT_CODES.get(subject)
    if knowledge is None or code is None:
        return False
    level = engine.state.level.lower()
    return any(p.subject == code and p.level == level for p in knowledge.index.packs.values())


def question_subject(output: dict[str, Any] | None, topic_id: str | None) -> Any:
    """The subject the Diagnoser placed the question in: its chosen topic's subject, else its free `subject` field.
    The topic wins (SPEC §10.2): a Mathematics topic labelled "physics" is a Mathematics question."""
    code = topic_id.split(".")[1] if topic_id is not None and topic_id.count(".") >= 2 else None
    named = next((name for name, c in SUBJECT_CODES.items() if c == code), None)
    return named if named is not None else (output or {}).get("subject")


def other_subject(engine: Any, output: dict[str, Any] | None, topic_id: str | None = None) -> str | None:
    """The subject the Diagnoser placed the question in when it is one of the five and not the session's."""
    named = question_subject(output, topic_id)
    return named if named in SUBJECT_CODES and named != engine.state.subject else None


def first_asked(engine: Any) -> bool:
    """The session was started by asking and is on its first problem, before any board step (D94)."""
    state = engine.state
    problems = sum(1 for m in state.transcript if m.get("kind") == "problem")
    first = problems <= 1 and not state.board.steps and state.lesson_id is None and state.assessment_origin is None
    return state.origin == "ask" and first


def may_switch(engine: Any, subject: str) -> bool:
    """D94: only a session started by asking, on its first problem and before any board step, moves to the subject
    of its question, and only to one with a pack installed at the session's level."""
    return first_asked(engine) and installed(engine, subject)


def session_topic(engine: Any, diagnosed: Diagnosed) -> None:
    """A new problem's diagnosed topic is the session's: `session.updated` carries it when it changed (API-41)."""
    if diagnosed.topic_id is None or diagnosed.topic_id == engine.state.topic_id:
        return
    engine.state.topic_id = diagnosed.topic_id
    engine.events.emit({"type": "session.updated", "topic_id": diagnosed.topic_id})


def offer_subject(engine: Any, turn: Any, subject: str) -> None:
    """A question in another subject that does not move the session (D94): a confirm card naming that subject, whose
    confirmation the client turns into a new asked session there with the same question. The problem goes on here;
    engine-written, so nothing here is a model's text."""
    cards = engine.receipt_texts.cards
    names = {"subject": subject.capitalize(), "current": engine.state.subject.capitalize()}
    body = subject_card(cards["new_subject_title"].format(**names), cards["new_subject_body"].format(**names), subject)
    request_id = new_id("req")
    part = emit_part(engine.events, "card", [{"type": "card.open", "request_id": request_id, "part_id": "",
                                              "call_id": None, "card": body}])  # fmt: skip
    engine._persist(turn, part, "card", body)
    engine.open_cards[request_id] = "new_subject"


async def name_session(engine: Any, diagnosed: Diagnosed) -> None:
    """The title role names the session once, after the first problem is diagnosed; its fallback is the topic name."""
    if engine.titled or diagnosed.topic_id is None:
        return
    topic = topic_title(engine, diagnosed.topic_id)
    statement = engine.state.problem.statement if engine.state.problem is not None else ""
    if engine.state.hardware_class in engine.deps.settings.lean_classes:
        title = topic  # the title role's fallback, with no model call in the first turn (D128)
    else:
        title, _ = await engine.auxiliary.title(statement, topic)
    engine.titled = True
    engine.events.emit({"type": "session.updated", "title": title[:120]})


def needs_reading(text: str, status: str) -> bool:
    """An answer or line the code did not confirm: a wrong stated value, or working (a relation or a number) the code
    could not check. A question about the problem is not an answer and gets no reading."""
    if status == "incorrect":
        return True
    return status == "unchecked" and bool(re.search(r"\d|=", text))


async def read_answer(engine: Any, turn: Any, text: str, status: str) -> str:
    """A student answer the code could not confirm: the Checker and the Diagnoser read it in parallel and both are
    joined before the Coach. Returns a short note for the Coach's tutor-context (never a withheld value)."""
    assert engine.state.problem is not None
    from agent_core.engine.turn import _escape

    statement = engine.state.problem.statement
    checker_task = (f"<student-message>\n{_escape(statement)}\n</student-message>\n\n"
                    "Check this student answer (subject_of_check: student_answer). Verify it with the tools; cite "
                    f"the checks you used.\n<student-message>\n{_escape(text)}\n</student-message>")  # fmt: skip

    async def checked() -> dict[str, Any]:
        # A deterministic incorrect verdict already settles this line. A second model opinion cannot
        # override it; keep the Diagnoser for misconception coaching without another queued generation.
        return {} if status == "incorrect" else await engine.delegate("checker", checker_task, turn)

    verdict_envelope, diagnosed = await asyncio.gather(
        checked(), diagnose(engine, turn, statement, answer=text, attempts=1)
    )
    note = []
    output = verdict_envelope.get("output") if verdict_envelope.get("schema_valid") else None
    if isinstance(output, dict):
        cited = [c for c in output.get("check_ids") or [] if c in engine.state.check_values]
        verdict = output.get("verdict")
        proved = next((c for c in cited if proves_attempt(engine, text, c)), None)
        if status == "unchecked" and proved and verdict == "correct":
            restate(engine, turn, verdict, proved)
            note.append("the checker supplied a proved equivalence for this exact line")
        elif status == "unchecked":
            note.append("this working remains unchecked; a check of another expression cannot confirm it")
        elif output.get("error_category") not in (None, "none"):
            note.append(f"likely error: {output['error_category']}")
    engine.answer_misconception = diagnosed.misconception_id if diagnosed is not None else None  # PED-22
    if diagnosed is not None:
        engine.diagnosis = diagnosed
        # _after_answer advances the misconception flow and supplies its name, fix and probing question.
        # Repeating the name here overflows the small context once prior evidence calls for the full fix.
    return "; ".join(note)


def proves_attempt(engine: Any, text: str, check_id: str) -> bool:
    """Only a whole, proved equivalence to the current plan can settle an unresolved equation.

    A numeric result, a prior plan check, a tautology on another expression or a model's verdict is not
    evidence for this line. Worded explanations remain ungraded (VER-27).
    """
    from agent_core.engine import slice as flows
    from agent_core.engine.state import expression_key

    plan = engine.state.plan
    if plan is None or text.count("=") != 1 or flows.worded(text) or "\n" in text or ";" in text:
        return False
    if check_id in engine.state.unreviewed_implausible():
        return False
    proved = set(engine.state.check_expressions.get(check_id, []))
    original = expression_key(text)
    premises = {expression_key(s.latex) for s in plan.steps}
    if plan.final_latex:
        premises.add(expression_key(plan.final_latex))
    return original in proved and bool(premises & proved)


def restate(engine: Any, turn: Any, verdict: str, check_id: str) -> None:
    """A Checker verdict with a whole-expression proof settles a line the code could not: the board step the
    student wrote gets that status (engine-set, with the check id; never upgrades a code `incorrect`, AGT-14)."""
    steps = engine.state.board.steps
    if not steps or steps[-1].owner != "student" or steps[-1].status != "unchecked":
        return
    step = steps[-1]
    if verdict != "correct" or not proves_attempt(engine, step.latex, check_id):
        return
    step.status, step.check_id = verdict, check_id
    event = {"type": "board.step", "board_id": engine.state.board.board_id, "step_id": step.step_id,
             "index": step.index, "latex": step.latex, "reason": step.reason, "owner": step.owner,
             "status": step.status, "check_id": step.check_id if CHECK_ID.fullmatch(step.check_id) else None,
             "error": step_error(step)}  # fmt: skip
    part = emit_part(engine.events, "board", [event])
    engine._persist(turn, part, "board", [event])
