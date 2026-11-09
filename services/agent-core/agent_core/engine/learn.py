"""The subject map and the lesson player's reads, quick checks and saved position (UXS-22, UXS-23; LRN-09, LRN-34,
CUR-03, CUR-11, LRN-17 subset, ASM-02).

`LearnDesk` serves the open learner database with the installed, verified packs (`store.pack_index`):

- The subject map (`SubjectMap`): the subject's syllabus sections (the curriculum map's topic nodes, CUR-03) in
  order, each with its tiles: the nodes that hold KCs (its subtopics, and the topic itself when KCs hang from it
  directly). A tile lists its KCs, each with its TABLE LRN-09a state from the mastery projection and the three-step
  band of LRN-34, and the lessons that teach any of them. The reply carries codes only: the client picks a tile's
  label by TABLE UXS-22a and turns states into words. A subject with no installed pack says so (`not_installed`, or
  `unverified` when its pack is present but failed verification, CUR-08).
- Syllabus points on the map (CUR-06, D113): the board syllabuses the subject's map is mapped to, and each
  section's, tile's, KC's and lesson's points. A KC's are its own mappings, otherwise its node's; a lesson's are its
  KCs', in order, each once. Mapping facts only: scheme, code and coverage, never board text.
- The lesson (`LessonView`): its blocks in script order (CUR-11) with everything the player needs resolved from the
  pack, so a lesson plays with no model and no network (UXS-23, VSL-36): the worked example's checked board steps
  with the steps faded for this student (LRN-17: steps of the example's fading order by the mode its weakest KC's
  P_k picks, the full example, faded, or problems only, and the blanks the student earned, the lowest `fading_stage`
  over its KCs), check items as student views (`PracticeItem`, no keys), common mistakes with their
  student names and fix text, and the saved position.
- A step-shape lesson (TABLE CUR-11b, D110): each step with its action, lead-in and one item, and the closing
  three-question check. A step's why is never in the view; the check reply carries it.
- A quick check (`LessonCheckResult`): each answered part marked by the deterministic marker against the item's key
  (ASM-02); a model is never asked. On a step or check item of a step-shape lesson the reply's `explanation` (the
  step's why and named idea, the item's checked worked solution) comes with a correct answer, or as the step shown
  from the second missed try (`attempt`, UXS-23); after a first miss it is null, so a retry never sees the answer.
  Unit conversion and structured answers use the worker's checkers through `run_tool`; without it such a part is
  `not_checked`, never guessed. Misconception signatures are matched from the
  installed packs' library (CUR-18), and a misconception a part names (a probe choice on a diagnostic item, a
  matched signature elsewhere) is recorded as `misconception_evidence` (ASM-13); the check records no attempt.
- The position: the block the student was on, kept per lesson and pack version, in the learner database's
  `lesson_positions` (UXS-23) when the table exists, otherwise for the life of the core in `positions`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agent_core.assets.visuals import resolved_of
from agent_core.config import Settings
from agent_core.engine.marking import Converted, Verdict, mark
from agent_core.engine.signatures import evidence, pack_context, verdict_source
from agent_core.learner import policies
from agent_core.learner.service import NewEvent
from agent_core.memory.render import BANDS
from agent_core.store.pack_index import PackIndex, PackRef
from agent_core.store.sqlite import transaction

# The profile's subject names and the curriculum map's subject codes (CUR-02).
SUBJECT_CODES = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}
SUBJECT_NAMES = {code: name for name, code in SUBJECT_CODES.items()}
SUBJECT_ID = re.compile(r"^cur\.(mth|phy|chm|bio|acc)\.(o|a)$")
LESSON_ID = re.compile(r"^les\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+$")
ITEM_ID = re.compile(r"^itm\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[0-9]{4}$")
STATES = ("not_started", "learning", "ready_to_check", "mastered", "needs_review", "moved_on")
# The marker's outcomes as the check reply names them (UXS-16 copy keys on the client).
VERDICTS = {"correct": "correct", "incorrect": "incorrect", "partial": "partially_correct",
            "unverifiable": "not_checked"}  # fmt: skip
# Item types a lesson can show as a student view: their input needs nothing a key would give away.
SHOWN_TYPES = frozenset(
    {
        "mcq",
        "mcq_diagnostic",
        "multi_select",
        "numeric",
        "ordering",
        "algebraic",
        "equation_solutions",
        "chemical_equation",
        "journal_entry",
        "accounting_table",
        "diagram_label",
        "short_text",
        "structured",
    }
)
POSITIONS_TABLE = "lesson_positions"
# Board names by a scheme id's board part (`cambridge_4024`: Cambridge, syllabus 4024; CUR-06). A board not listed
# shows its id.
BOARDS = {"cambridge": "Cambridge"}

RunTool = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any] | None, str | None]]]


class CheckRefused(ValueError):
    """A quick-check body that does not fit the item: an unknown or repeated part, or an input of the wrong kind."""


def _active(record: Mapping[str, Any]) -> bool:
    return bool(record.get("status", "active") == "active")


def _order(node: Mapping[str, Any]) -> tuple[int, str]:
    return int(node.get("order") or 0), str(node["id"])


def _english(text: Any) -> str:
    """A pack's localised text in the base locale (LOC-01: English is the only catalogue at S2)."""
    if isinstance(text, Mapping):
        return str(text.get("en") or "")
    return str(text or "")


def teaching_example(content: Any, lesson: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Reuse an installed worked example for the lesson's first objective, in a stable order."""
    kcs = lesson.get("kc_ids") or []
    if not kcs:
        return None
    return next(
        iter(
            sorted(
                (
                    example
                    for example in content.table("worked_examples")
                    if _active(example) and kcs[0] in example.get("kc_ids", [])
                ),
                key=lambda example: str(example["id"]),
            )
        ),
        None,
    )


def syllabus_points(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """A record's own board mappings as syllabus points (CUR-06)."""
    return [{"scheme": str(m["scheme"]), "code": str(m["code"]), "coverage": str(m["coverage"])}
            for m in record.get("external_mappings") or []]  # fmt: skip


def syllabus(scheme: str) -> dict[str, str]:
    """A scheme id as the map names it: the board's name and the syllabus's own code (`<board>_<code>`)."""
    board, _, code = scheme.partition("_")
    return {"scheme": scheme, "board": BOARDS.get(board, board), "syllabus_code": code or scheme}


def subject_parts(subject_id: str) -> tuple[str, str] | None:
    """The profile's subject name and level of a subject node id (`cur.phy.o` -> physics, O)."""
    found = SUBJECT_ID.match(subject_id)
    return (SUBJECT_NAMES[found[1]], found[2].upper()) if found else None


def kc_states(conn: sqlite3.Connection) -> dict[str, str]:
    """Each KC's TABLE LRN-09a state from the mastery projection; a KC with no row is `not_started`."""
    return {str(k): str(s) for k, s in conn.execute("SELECT kc_id, status FROM kc_mastery") if s in STATES}


@dataclass(frozen=True)
class Fading:
    """What fades a worked example for this student (LRN-17): each KC's earned blanks (`fading_stage`) and P_k, and
    whether extra support raises the example threshold (LRN-27)."""

    stages: Mapping[str, int]
    p_mastery: Mapping[str, float]
    support_extra: bool = False

    @classmethod
    def read(cls, conn: sqlite3.Connection) -> Fading:
        """From the learner database; a bare projection with no student (the studio's preview) fades nothing."""
        try:
            rows = conn.execute("SELECT kc_id, fading_stage, p_mastery FROM kc_mastery").fetchall()
            support = conn.execute("SELECT support_level FROM profile").fetchone()
        except sqlite3.OperationalError:
            return cls({}, {})
        return cls({str(k): int(s) for k, s, _ in rows if s is not None}, {str(k): float(p) for k, _, p in rows},
                   bool(support and support[0] == "extra"))  # fmt: skip

    def faded(self, kc_ids: Sequence[str], order: Sequence[int], settings: Settings) -> list[int]:
        """The steps blanked for the student, from the example's fading order. The weakest of its KCs picks the mode
        (`worked_example_mode`): below the example threshold the full example, with only the blanks the student
        earned; in the faded band at least one blank; from `example_problems_from` every step of the fading order
        (problems only, the full example on request)."""
        known = [self.stages[k] for k in kc_ids if k in self.stages]
        stage = min(known) if known else 0
        p_k = min((self.p_mastery.get(k, 0.0) for k in kc_ids), default=0.0)
        mode = policies.worked_example_mode(p_k, settings, support_extra=self.support_extra).output["mode"]
        count = len(order) if mode == "problem" else max(1, stage) if mode == "faded_example" else stage
        return [int(n) for n in list(order)[:count]]


# --- the student's view of a pack item (no keys, no solutions) -------------------------------------------------------


def _options(options: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [{"id": str(o["id"]), "label": str(o["text"])} for o in options]


def _input(item_type: str, answer: Mapping[str, Any], item_id: str = "") -> dict[str, Any] | None:
    if item_type in ("mcq", "mcq_diagnostic"):
        return {"kind": "mcq", "options": _options(answer["options"])}
    if item_type == "multi_select":
        return {"kind": "multi_select", "options": _options(answer["options"])}
    if item_type == "numeric":
        # The unit is named only when the answer must be given in exactly that unit.
        exact = answer.get("unit_policy") == "exact" and answer.get("unit")
        # Whether the answer has a unit at all: the client shows a unit field only then (SPEC §10.7).
        expected = bool(answer.get("unit")) and answer.get("unit_policy") != "none"
        return {"kind": "numeric", "unit_prompt": str(answer["unit"]) if exact else None, "unit_expected": expected}
    if item_type == "ordering":
        # A fixed order that is never the key's: by a hash of the item and element ids.
        def mixed(e: Mapping[str, Any]) -> str:
            return hashlib.sha256(f"{item_id}:{e['id']}".encode()).hexdigest()

        elements = sorted(answer.get("elements") or [], key=mixed)
        if [str(e["id"]) for e in elements] == [str(i) for i in answer.get("correct_order") or []]:
            elements = elements[1:] + elements[:1]
        return {"kind": "ordering", "elements": [{"id": str(e["id"]), "label": str(e["text"])} for e in elements]}
    return None


def student_item(
    item: Mapping[str, Any], pack: PackRef | None, content: PackIndex | None = None, settings: Settings | None = None
) -> dict[str, Any] | None:
    """A pack item as the student sees it (`PracticeItem` of the command contract), or None for a type the lesson
    player cannot show yet."""
    if content is not None:
        from agent_core.engine.assessment.items import present, ready

        options = settings or Settings()
        return present(item, content, options) if ready(item, content, options) else None
    if item.get("type") not in SHOWN_TYPES:
        return None
    parts = []
    for part in item["parts"]:
        shown = _input(str(item["type"]), part["answer"], str(item["id"]))
        if shown is None:
            return None
        parts.append({"part_id": str(part["id"]), "prompt": str(part["prompt"]), "marks": int(part["marks"]),
                      "uses_parts": [str(p) for p in part.get("uses_parts") or []], "input": shown})  # fmt: skip
    return {
        "item_id": str(item["id"]),
        "pack_id": pack.pack_id if pack is not None else "",
        "pack_version": pack.version if pack is not None else "",
        "kc_ids": [str(k) for k in item["kc_ids"]],
        "kind": str(item["type"]),
        "stem": str(item["stem"]),
        "parts": parts,
        "visual": None,
        "marks": sum(int(p["marks"]) for p in item["parts"]),
    }


def _response(part_answer: Mapping[str, Any]) -> Any:
    """What the marker reads for one answered part (ASM-02)."""
    kind = part_answer["kind"]
    if kind == "mcq":
        return str(part_answer["option_id"])
    if kind == "multi_select":
        return [str(o) for o in part_answer["option_ids"]]
    if kind == "numeric":
        unit = part_answer.get("unit")
        return f"{part_answer['value']} {unit}".strip() if unit else str(part_answer["value"])
    if kind == "ordering":
        return [str(e) for e in part_answer["element_ids"]]
    raise CheckRefused(f"a lesson check has no {kind} answers")


# --- the desk --------------------------------------------------------------------------------------------------------


@dataclass
class LearnDesk:
    content: PackIndex
    conn: sqlite3.Connection
    settings: Settings
    # Installed packs as `GET /v1/packs` lists them, so a subject whose pack failed verification says so (CUR-08).
    listed: Callable[[], list[dict[str, Any]]] = lambda: []
    run_tool: RunTool | None = None
    # Positions kept for the life of the core while the learner database has no `lesson_positions` table.
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Writes learning events to the open learner database (`Learner.record_batch`); without it a check records
    # nothing. A quick check records only the `misconception_evidence` of a matched misconception (ASM-13).
    record: Callable[[list[NewEvent]], Any] | None = None
    # The learner's map (SPEC §21): the open learner's projector (its mastery parameters, pinned content and
    # projections, for states at now and the dated replays), the clock, the device database (`app_settings`, where
    # the last open is kept, JM-D45), the profile, and what the profile's map keeps for the life of the core. Without
    # a projector the map shows the stored states and has no dated view.
    projector: Any = None
    clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000))
    device: sqlite3.Connection | None = None
    profile_id: str = ""
    map_state: Any = None

    # --- the subject map (UXS-22) ---------------------------------------------------------------------------------

    def subject_map(self, subject_id: str) -> dict[str, Any] | None:
        """The `SubjectMap` reply for a subject node id, or None when the id is not a subject's."""
        parts = subject_parts(subject_id)
        if parts is None:
            return None
        subject, level = parts
        pack = self.content.subject_packs.get(subject_id)
        node = self.content.nodes.get(subject_id)
        reply: dict[str, Any] = {"subject_id": subject_id, "subject": subject, "level": level,
                                 "title": str(node["title"]) if node else None, "status": "ready",
                                 "pack": {"pack_id": pack.pack_id, "version": pack.version} if pack else None,
                                 "syllabuses": [], "sections": []}  # fmt: skip
        if node is None or pack is None:
            code, lvl = subject_id.split(".")[1:]
            damaged = any(p.get("subject") == code and str(p.get("level")).lower() == lvl and not p.get("signature_ok")
                          for p in self.listed())  # fmt: skip
            reply["status"] = "unverified" if damaged else "not_installed"
            return reply
        states = self.states_now(self.conn, self.clock())
        prefix = subject_id + "."
        kcs: dict[str, list[dict[str, Any]]] = {}
        for kc in self.content.table("kcs"):
            if _active(kc) and str(kc["node_id"]).startswith(prefix):
                kcs.setdefault(str(kc["node_id"]), []).append(kc)
        lessons = [les for les in self.content.table("lessons") if str(les["node_id"]).startswith(prefix)]
        nodes = [n for n in self.content.nodes.values() if str(n["id"]).startswith(prefix) and _active(n)]
        schemes = {p["scheme"] for n in nodes for p in syllabus_points(n)}
        reply["syllabuses"] = [syllabus(s) for s in sorted(schemes)]
        for topic in sorted((n for n in nodes if n["kind"] == "topic"), key=_order):
            under = sorted((n for n in nodes if n["kind"] == "subtopic" and n["parent_id"] == topic["id"]), key=_order)
            holding = [n for n in [topic, *under] if kcs.get(str(n["id"]))]
            tiles = [self._tile(n, kcs[str(n["id"])], states, lessons) for n in holding]
            if tiles:
                reply["sections"].append({"node_id": str(topic["id"]), "code": str(topic["code"]),
                                          "title": str(topic["title"]), "syllabus_points": syllabus_points(topic),
                                          "topics": tiles})  # fmt: skip
        return reply

    def concept_map(self, subject_id: str, as_of: str | None = None, *, opening: bool = True) -> dict[str, Any] | None:
        """The `ConceptMap` reply for a subject node id (the map view of UXS-22), or None when the id is not a
        subject's: the pack's skill backbone with this learner's states, questions and mistakes on it, and the
        learner's map layers (SPEC §21). With `as_of` (a local date) the map as the learner model stood at the end of
        that day, replayed in memory and read-only (JM-D37); a date that is not one, in the future or more than 26
        weeks back raises `learner_map.AsOfRefused`. A live read with `opening` records the open (JM-D45)."""
        from agent_core.engine import learner_map
        from agent_core.engine.concept_map import backbone

        now = self.clock()
        params = self._params()
        if as_of is not None:
            day = learner_map.check_as_of(params, now, as_of)
            if params is None:
                raise learner_map.AsOfRefused("this map has no dated view")
            mem, ts = learner_map.snapshot(self._map_state(), self.profile_id, day, self.conn, params,
                                           self.projector.ctx.content, self.projector.projections)  # fmt: skip
            reply = backbone(self.content, mem, subject_id, states=learner_map.states_now(mem, params, ts), live=False,
                             questions_from=self.conn, until=ts)  # fmt: skip
            layers = learner_map.Layers.read(mem, self.conn, self.content, params, ts, None)
        else:
            reply = backbone(self.content, self.conn, subject_id, states=self.states_now(self.conn, now))
            layers = learner_map.Layers.read(self.conn, self.conn, self.content, params, now, self._baseline())
            day = None
        if reply is not None and reply["status"] == "not_installed":
            code, lvl = subject_id.split(".")[1:]
            damaged = any(p.get("subject") == code and str(p.get("level")).lower() == lvl and not p.get("signature_ok")
                          for p in self.listed())  # fmt: skip
            reply["status"] = "unverified" if damaged else "not_installed"
        if reply is None:
            return None
        reply["nodes"] = [layers.node(n) for n in reply["nodes"]]
        reply["as_of"] = day
        reply["first_day"] = learner_map.first_day(self.conn, params)
        if day is None and opening:
            self.mark_open(now)
        return reply

    # --- the learner's map (SPEC §21) --------------------------------------------------------------------------

    def _params(self) -> Any:
        return self.projector.ctx.params if self.projector is not None else None

    def _map_state(self) -> Any:
        from agent_core.engine.learner_map import MapState

        if self.map_state is None:
            self.map_state = MapState()
        return self.map_state

    def states_now(self, conn: sqlite3.Connection, now: int) -> dict[str, str]:
        """Each KC's TABLE LRN-09a state at `now` (`kc_views`), so the map's state and refresh due agree."""
        from agent_core.engine.learner_map import states_now

        return states_now(conn, self._params(), now)

    def _baseline(self) -> dict[str, str] | None:
        """The bands this visit compares against: those kept at the last open, read once per core (JM-D45)."""
        from agent_core.engine.learner_map import opened

        state = self._map_state()
        if not state.loaded:
            state.baseline, state.loaded = opened(self.device, self.profile_id), True
        baseline: dict[str, str] | None = state.baseline
        return baseline

    def mark_open(self, now: int) -> None:
        """Keep this open's bands in the device database's `app_settings` (never a learning event, JM-D45)."""
        from agent_core.engine.learner_map import write_opened

        self._baseline()
        states = self.states_now(self.conn, now)
        bands = {k: BANDS[s] for k, s in states.items()}
        write_opened(self.device, self.profile_id, {k: b for k, b in bands.items() if b != "not_started"}, now)

    def overview(self) -> dict[str, Any]:
        """The `CurriculumOverview` reply: every installed subject as its subject map's tiles, each with TABLE
        UXS-22a's rule, bands and the layers' summaries (SPEC §21.1). A read records the open (JM-D45)."""
        from agent_core.engine import learner_map

        now = self.clock()
        layers = learner_map.Layers.read(self.conn, self.conn, self.content, self._params(), now, self._baseline())
        watched = learner_map.watched(self.conn)
        subjects = []
        for subject_id in sorted(self.content.subject_packs):
            shown = self.subject_map(subject_id) if SUBJECT_ID.match(subject_id) else None
            if shown is None or shown["status"] != "ready":
                continue
            tiles = [tile for section in shown["sections"] for tile in section["topics"]]
            nodes = {k["kc_id"]: layers.node({"kc_id": k["kc_id"], "state": k["state"], "band": k["band"]})
                     for tile in tiles for k in tile["kcs"]}  # fmt: skip
            subjects.append({"subject_id": subject_id, "subject": shown["subject"], "level": shown["level"],
                             "topics": [learner_map.overview_topic(t, nodes, watched, learner_map.topic_rule)
                                        for t in tiles]})  # fmt: skip
        self.mark_open(now)
        return {"subjects": subjects}

    def kc_links(self, kc_id: str) -> dict[str, Any] | None:
        """The `KcLinks` reply for an installed KC, or None: its Study materials, lecture moments, notes, whether the
        tutor is tracking it, and its misconceptions. Device-only (JM-D39)."""
        from agent_core.engine import learner_map
        from agent_core.engine.concept_map import backbone

        record = self.content.record("kcs", kc_id)
        if record is None or not _active(record):
            return None
        now = self.clock()
        mapped = backbone(self.content, self.conn, ".".join(["cur", *kc_id.split(".")[1:3]]),
                          states=self.states_now(self.conn, now))  # fmt: skip
        mistakes = mapped["mistakes"] if mapped is not None else []
        return learner_map.links(self.conn, self.conn, self.content, kc_id, now, mistakes)

    def _tile(self, node: Mapping[str, Any], kcs: list[dict[str, Any]], states: Mapping[str, str],
              lessons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:  # fmt: skip
        ids = {str(k["id"]) for k in kcs}
        shown = []
        for kc in sorted(kcs, key=lambda k: str(k["code"])):
            state = states.get(str(kc["id"]), "not_started")
            shown.append({"kc_id": str(kc["id"]), "code": str(kc["code"]), "statement": str(kc["statement"]),
                          "state": state, "band": BANDS[state], "syllabus_points": self.kc_points(kc)})  # fmt: skip
        teaching = [{"lesson_id": str(les["id"]), "title": str(les["title"]),
                     "estimated_minutes": int(les["estimated_minutes"]),
                     "kc_ids": [k for k in les["kc_ids"] if k in ids], "syllabus_points": self.lesson_points(les)}
                    for les in lessons if ids & set(les["kc_ids"])]  # fmt: skip
        return {"node_id": str(node["id"]), "code": str(node["code"]), "title": str(node["title"]),
                "syllabus_points": syllabus_points(node), "kcs": shown, "lessons": teaching}  # fmt: skip

    def kc_points(self, kc: Mapping[str, Any]) -> list[dict[str, str]]:
        """A KC's syllabus points: its own mappings, otherwise those of the node it hangs from (CUR-06, D113)."""
        own = syllabus_points(kc)
        return own if own else syllabus_points(self.content.nodes.get(str(kc.get("node_id")), {}))

    def lesson_points(self, lesson: Mapping[str, Any]) -> list[dict[str, str]]:
        """A lesson's syllabus points: those of the KCs it teaches, in order, each once (CUR-06, D113)."""
        out: list[dict[str, str]] = []
        for kc_id in lesson.get("kc_ids") or []:
            for point in self.kc_points(self.content.kc_record(str(kc_id))):
                if point not in out:
                    out.append(point)
        return out

    # --- the lesson (UXS-23) ----------------------------------------------------------------------------------------

    def _pack_of(self, record_id: str) -> PackRef | None:
        parts = record_id.split(".")
        return self.content.subject_packs.get(f"cur.{parts[1]}.{parts[2]}") if len(parts) > 2 else None

    def _items(self, item_ids: Sequence[str]) -> list[dict[str, Any]]:
        out = []
        for item_id in item_ids:
            record = self.content.record("items", item_id)
            shown = (
                student_item(record, self._pack_of(item_id), self.content, self.settings)
                if record is not None
                else None
            )
            if shown is not None:
                out.append(shown)
        return out

    def _worked_example(self, example_id: str, fading: Fading) -> dict[str, Any] | None:
        example = self.content.record("worked_examples", example_id)
        if example is None:
            return None
        steps = [{"n": int(s["n"]), "latex": str(s["latex"]), "reason": str(s["reason"]),
                  "check_id": s.get("check_id")} for s in example["steps"]]  # fmt: skip
        return {"worked_example_id": example_id, "problem": str(example["problem"]), "steps": steps,
                "final_answer": {"latex": str(example["final_answer"]["latex"]),
                                 "check_id": str(example["final_answer"]["check_id"])},
                "faded_steps": fading.faded(example["kc_ids"], example.get("fading_order") or [], self.settings),
                "visual": resolved_of(example)}  # its figure, resolved and checked at build (or None)  # fmt: skip

    def _mistakes(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        out = []
        for mc_id in ids:
            record = self.content.record("misconceptions", mc_id)
            if record is not None:
                out.append({"misconception_id": mc_id, "name": str(record["student_name"]),
                            "fix": _english(record["fix"]["text"])})  # fmt: skip
        return out

    def _block(self, block: Mapping[str, Any], stages: Fading) -> dict[str, Any] | None:
        kind = block["type"]
        if kind in ("prior_knowledge", "quick_check", "exit_check"):
            return {"type": kind, "items": self._items(block["item_ids"])}
        if kind == "guided_step":
            return {"type": kind, "items": self._items([block["item_id"]])}
        if kind == "idea":
            return {"type": kind, "text": _english(block["text"]), "visual": block.get("visual")}
        if kind == "worked_example":
            example = self._worked_example(str(block["worked_example_id"]), stages)
            return {"type": kind, "worked_example": example} if example is not None else None
        if kind == "common_mistakes":
            text = _english(block.get("text")) or None
            return {"type": kind, "text": text, "mistakes": self._mistakes(block["misconception_ids"])}
        if kind == "visual":
            return {"type": kind, "visual": block["visual"], "caption": block.get("caption")}
        if kind == "summary":
            return {"type": kind, "text": _english(block["text"])}
        if kind == "step":
            items = self._items([block["item_id"]])
            if not items:
                return None  # a step is its action: without an item it has nothing to ask
            return {"type": kind, "action": str(block["action"]), "text": _english(block.get("text")) or None,
                    "visual": block.get("visual"), "items": items}  # fmt: skip
        if kind == "check":
            return {"type": kind, "items": self._items(block["item_ids"])}
        return None

    def lesson(self, lesson_id: str, *, teaching: bool = False) -> dict[str, Any] | None:
        """The `LessonView` reply, or None when no installed pack has the lesson."""
        record = self.content.record("lessons", lesson_id) if LESSON_ID.match(lesson_id) else None
        if record is None:
            return None
        pack = self._pack_of(lesson_id)
        stages = Fading.read(self.conn)
        blocks = [b for b in (self._block(b, stages) for b in record["blocks"]) if b is not None]
        version = str(record["version"])
        position = self._position(lesson_id, version, len(blocks))
        if teaching and any(block["type"] == "step" for block in blocks):
            example = teaching_example(self.content, record)
            if example is not None:
                resolved = self._worked_example(str(example["id"]), stages)
                if resolved is not None:
                    resolved["faded_steps"] = []  # Teaching explicitly requests the complete worked example.
                    idea = next(
                        (_english(block.get("names")) for block in record["blocks"] if _english(block.get("names"))), ""
                    )
                    # Append presentation blocks: saved step indices and all check items keep their identity.
                    if idea:
                        blocks.append({"type": "idea", "text": idea, "visual": None})
                    blocks.append({"type": "worked_example", "worked_example": resolved})
        return {"lesson_id": lesson_id, "node_id": str(record["node_id"]), "title": str(record["title"]),
                "estimated_minutes": int(record["estimated_minutes"]), "kc_ids": list(record["kc_ids"]),
                "version": version, "pack": {"pack_id": pack.pack_id, "version": pack.version} if pack else None,
                "blocks": blocks, "position": position}  # fmt: skip

    # --- the position ------------------------------------------------------------------------------------------------

    def _table(self) -> bool:
        found = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (POSITIONS_TABLE,))
        return found.fetchone() is not None

    def _position(self, lesson_id: str, version: str, blocks: int) -> dict[str, Any] | None:
        """The saved position, when it was saved on this lesson version and is still a block of it."""
        if self._table():
            row = self.conn.execute("SELECT block_index, lesson_version, updated_at FROM lesson_positions"
                                    " WHERE lesson_id = ?", (lesson_id,)).fetchone()  # fmt: skip
            saved = {"block_index": row[0], "lesson_version": row[1], "updated_ts": row[2]} if row else None
        else:
            saved = self.positions.get(lesson_id)
        if saved is None or saved["lesson_version"] != version or not 0 <= int(saved["block_index"]) < blocks:
            return None
        return {"block_index": int(saved["block_index"]), "updated_ts": int(saved["updated_ts"])}

    def save_position(self, lesson_id: str, block_index: int, version: str, now_ms: int) -> dict[str, Any] | None:
        """Keep the block the student is on; None when the lesson is not installed. A position for another version
        of the lesson, or past its last block, is refused with ValueError."""
        lesson = self.lesson(lesson_id)
        if lesson is None:
            return None
        if version != lesson["version"] or not 0 <= block_index < len(lesson["blocks"]):
            raise ValueError("that block is not in this version of the lesson")
        if self._table():
            with transaction(self.conn):
                self.conn.execute("INSERT OR REPLACE INTO lesson_positions (lesson_id, block_index, lesson_version,"
                                  " updated_at) VALUES (?, ?, ?, ?)",
                                  (lesson_id, block_index, version, now_ms))  # fmt: skip
        else:
            self.positions[lesson_id] = {"block_index": block_index, "lesson_version": version, "updated_ts": now_ms}
        return {"lesson_id": lesson_id, "block_index": block_index, "updated_ts": now_ms}

    # --- quick checks (ASM-02) ---------------------------------------------------------------------------------------

    def _check_items(self, lesson_id: str) -> dict[str, Mapping[str, Any]]:
        """The lesson's check items, each with the block that asks it."""
        record = self.content.record("lessons", lesson_id)
        if record is None:
            return {}
        out: dict[str, Mapping[str, Any]] = {}
        for block in record["blocks"]:
            for item_id in [*(block.get("item_ids") or []), *([block["item_id"]] if "item_id" in block else [])]:
                out.setdefault(str(item_id), block)
        return out

    async def check(self, lesson_id: str, item_id: str, parts: Sequence[Mapping[str, Any]],
                    attempt: int = 1) -> dict[str, Any] | None:  # fmt: skip
        """Mark a lesson item's answered parts; None when the lesson or the item is not one of its checks. `attempt`
        is the student's try at the item in this visit, from 1 (UXS-23)."""
        asked = self._check_items(lesson_id) if LESSON_ID.match(lesson_id) else {}
        if item_id not in asked:
            return None
        item = self.content.record("items", item_id)
        if item is None or item.get("type") not in SHOWN_TYPES:
            return None
        from agent_core.engine.assessment.items import ready, response, validate_answer

        if not ready(item, self.content, self.settings):
            return None
        known = {str(p["id"]): p for p in item["parts"]}
        try:
            validate_answer(item, {"parts": list(parts)})
        except ValueError as error:
            raise CheckRefused(str(error)) from None
        responses = {str(p["part_id"]): response(p, item) for p in parts}
        results = []
        context = pack_context(self.content, item)
        found: list[str] = []
        for answered in parts:
            verdict = await mark(
                item,
                str(answered["part_id"]),
                responses if item["type"] == "structured" else responses[str(answered["part_id"])],
                self._convert,
                self.settings,
                self.run_tool,
                signatures=context,
            )
            results.append(self._part(known[str(answered["part_id"])], verdict, answered))
            if verdict.misconception_id is not None:
                found.append(verdict.misconception_id)
        self._evidence(item, found, context.misconceptions)
        result = self._result(item_id, results)
        block = asked[item_id]
        if block.get("type") in ("step", "check"):
            result["explanation"] = self._explanation(item, block, result["verdict"], attempt)
        return result

    def _explanation(self, item: Mapping[str, Any], block: Mapping[str, Any], verdict: str,
                     attempt: int) -> dict[str, Any] | None:  # fmt: skip
        """The why and the checked working of a step-shape lesson's item (D110): with a correct answer, or as the step
        shown from the `lesson_step_misses_before_shown`th missed try; nothing after an earlier miss, so the retry
        never sees the answer. An answer the marker could not check is neither: the student may move on."""
        misses = self.settings.lesson_step_misses_before_shown
        shown = verdict in ("incorrect", "partially_correct") and attempt >= misses
        if verdict != "correct" and not shown:
            return None
        step = block["type"] == "step"
        working = [{"latex": str(s["latex"]), "reason": str(s.get("reason") or ""), "check_id": s.get("check_id")}
                   for s in (item.get("solution") or {}).get("steps") or []]  # fmt: skip
        return {"shown": shown, "why": _english(block.get("why")) or None if step else None,
                "names": _english(block.get("names")) or None if step else None, "working": working}  # fmt: skip

    def _evidence(self, item: Mapping[str, Any], found: Sequence[str], library: Sequence[Mapping[str, Any]]) -> None:
        """One `misconception_evidence` event per part whose answer named a misconception: a probe choice on a
        diagnostic item, a matched signature elsewhere (ASM-13, CUR-18). The check records no attempt."""
        source = verdict_source(item)
        payloads = [evidence(item, mc_id, library, source) for mc_id in found]
        events = [NewEvent("misconception_evidence", p, source="engine") for p in payloads if p is not None]
        if self.record is not None and events:
            self.record(events)

    async def _convert(self, quantity: str, to: str) -> Converted | None:
        if self.run_tool is None:
            return None
        value, check_id = await self.run_tool("units_convert", {"quantity": quantity, "to": to})
        decimal = ((value or {}).get("value") or {}).get("decimal")
        if decimal is None or check_id is None:
            return None
        return Converted(Decimal(str(decimal)), check_id)

    def _part(self, part: Mapping[str, Any], verdict: Verdict, answered: Mapping[str, Any]) -> dict[str, Any]:
        outcome = VERDICTS[verdict.outcome]
        feedback: str | None = None
        if answered["kind"] == "mcq":
            chosen = next((o for o in part["answer"]["options"] if o["id"] == answered["option_id"]), None)
            feedback = str(chosen["feedback"]) if chosen is not None and chosen.get("feedback") else None
        name = self.content.misconception_name(verdict.misconception_id) if verdict.misconception_id else None
        return {"part_id": str(part["id"]), "verdict": outcome, "marks_awarded": verdict.marks_awarded,
                "marks_total": verdict.marks_available, "category": verdict.error_category,
                "misconception": ({"misconception_id": verdict.misconception_id, "name": name}
                                  if verdict.misconception_id and name else None),
                "feedback": feedback, "check_ids": list(verdict.check_ids)}  # fmt: skip

    @staticmethod
    def _result(item_id: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
        verdicts = {p["verdict"] for p in parts}
        if verdicts == {"correct"}:
            overall = "correct"
        elif verdicts <= {"not_checked"}:
            overall = "not_checked"
        elif verdicts & {"correct", "partially_correct"}:
            overall = "partially_correct"
        else:
            overall = "incorrect" if "incorrect" in verdicts else "not_checked"
        return {"item_id": item_id, "verdict": overall, "parts": parts,
                "marks_awarded": sum(p["marks_awarded"] for p in parts),
                "marks_total": sum(p["marks_total"] for p in parts)}  # fmt: skip
