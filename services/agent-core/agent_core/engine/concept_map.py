"""The concept map of one subject (UXS-22 map view): the curated skill backbone with the learner's state on it, the
gap path a root-gap card points along, and a reasoned "what next".

The backbone is the installed pack's own records, nothing generated:

- Nodes: every active KC of the subject, with its topic, exam weight and the learner's TABLE LRN-09a state (and
  `explaining` while a mastery check waits for the learner's explanation, explain-back), plus each KC of another
  subject that a root-gap link of this subject reaches (`linked`), so the link has both ends on the map.
- Edges: the pack's prerequisite links (`prerequisite`, strength `required` or `helpful`), drawn from the earlier
  skill to the one that builds on it, and each misconception's `root_gap` (`root_gap`, from the earlier skill the
  mistake really comes from to the KC the misconception belongs to). A root-gap link is `reviewed` only when its
  misconception record names a reviewer: one with none was drafted and never checked, and the map says so.
- What next: the KCs not yet secure, ranked like the planner's main block (exam weight x (1 - P)) with the ones
  whose required prerequisites are all secure first; each with a reason code and its parts (the exam-weight band and
  the unmet prerequisites), so the client words it. Codes only, no probability (the map never shows P).
- The gap path: the shortest chain of map edges from a root-gap skill to the KC the mistake was on.

The learner's questions and mistakes (`questions`, `mistakes`) are attached by `agent_core.engine.concept_questions`."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from agent_core.engine.learn import SUBJECT_ID, kc_states, subject_parts
from agent_core.memory.render import BANDS
from agent_core.store.pack_index import PackIndex

SECURE = ("mastered", "needs_review")
NEXT_MAX = 5
# Exam-weight bands of the reason line: the pack's exam_weight is 0 to 1 within its subject.
WEIGHT_BANDS = ((0.5, "high"), (0.25, "medium"), (0.0, "low"))


def _active(record: Mapping[str, Any]) -> bool:
    return bool(record.get("status", "active") == "active")


def _english(text: Any) -> str:
    return str(text.get("en", "")) if isinstance(text, Mapping) else str(text or "")


def weight_band(weight: float) -> str:
    return next(band for floor, band in WEIGHT_BANDS if weight >= floor)


def explaining_kcs(conn: sqlite3.Connection) -> set[str]:
    """KCs whose mastery check is waiting for the learner's explanation (explain-back): the run's body says
    `explaining` while its storage state stays `in_progress`. A database without assessment runs has none."""
    try:
        rows = conn.execute("SELECT body FROM assessment_runs WHERE kind = 'mastery_check' AND state = 'in_progress'")
        bodies = [json.loads(body) for (body,) in rows]
    except (sqlite3.OperationalError, json.JSONDecodeError):
        return set()
    return {
        str(b["kc_id"]) for b in bodies if isinstance(b, dict) and b.get("state") == "explaining" and b.get("kc_id")
    }


def p_mastery(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        return {str(k): float(p) for k, p in conn.execute("SELECT kc_id, p_mastery FROM kc_mastery") if p is not None}
    except sqlite3.OperationalError:
        return {}


def _node(content: PackIndex, kc: Mapping[str, Any], state: str, *, linked: bool) -> dict[str, Any]:
    node_id = str(kc["node_id"])
    topic = content.nodes.get(node_id) or {}
    subject, level = str(kc["id"]).split(".")[1:3]
    return {
        "kc_id": str(kc["id"]),
        "code": str(kc["code"]),
        "statement": str(kc["statement"]),
        "subject_id": f"cur.{subject}.{level}",
        "topic_id": node_id,
        "topic_title": str(topic.get("title") or ""),
        "exam_weight": round(float(kc.get("exam_weight") or 0.0), 3),
        "state": state,
        "band": "learning" if state == "explaining" else BANDS[state],
        "linked": linked,
    }


def backbone(
    content: PackIndex,
    conn: sqlite3.Connection,
    subject_id: str,
    *,
    states: Mapping[str, str] | None = None,
    live: bool = True,
    questions_from: sqlite3.Connection | None = None,
    until: int | None = None,
) -> dict[str, Any] | None:
    """The `ConceptMap` reply's backbone for a subject node id (nodes, edges, next), or None when the id is not a
    subject's. A subject with no installed pack has an empty map and says so (`not_installed`).

    `states` are the KCs' states (the learner model's at now, SPEC §21.4), else the mastery projection's stored ones.
    A dated map (§21, `live` false) is read from a replayed learner model: no mastery check is waiting in it, and the
    questions are the live database's (`questions_from`) asked by `until`."""
    parts = subject_parts(subject_id)
    if parts is None or not SUBJECT_ID.match(subject_id):
        return None
    subject, level = parts
    pack = content.subject_packs.get(subject_id)
    reply: dict[str, Any] = {
        "subject_id": subject_id,
        "subject": subject,
        "level": level,
        "status": "ready" if pack is not None else "not_installed",
        "pack": {"pack_id": pack.pack_id, "version": pack.version} if pack else None,
        "nodes": [],
        "edges": [],
        "next": [],
        "questions": [],
        "mistakes": [],
    }
    if pack is None:
        return reply
    states = kc_states(conn) if states is None else states
    explaining = explaining_kcs(conn) if live else set()

    def state_of(kc_id: str) -> str:
        return "explaining" if kc_id in explaining else states.get(kc_id, "not_started")

    prefix = subject_id + "."
    own = {str(k["id"]): k for k in content.table("kcs") if _active(k) and str(k["node_id"]).startswith(prefix)}
    edges: list[dict[str, Any]] = []
    for kc_id in sorted(own):
        for need in own[kc_id].get("prerequisites") or []:
            need_id = str(need["kc_id"]) if isinstance(need, dict) else str(need)
            strength = str(need.get("strength", "required")) if isinstance(need, dict) else "required"
            if need_id in own:
                edges.append({"from": need_id, "to": kc_id, "kind": "prerequisite", "strength": strength})
    linked: dict[str, Mapping[str, Any]] = {}
    for mc in content.table("misconceptions"):
        gap = mc.get("root_gap") if isinstance(mc.get("root_gap"), dict) else None
        if not gap or not gap.get("kc_id"):
            continue
        root = str(gap["kc_id"])
        root_record = content.record("kcs", root)
        if root_record is None:
            continue
        for on in sorted(str(k) for k in mc.get("kc_ids") or []):
            if on not in own and root not in own:
                continue
            on_record = content.record("kcs", on)
            if on_record is None:
                continue
            for end, record in ((root, root_record), (on, on_record)):
                if end not in own:
                    linked[end] = record
            reviewers = (mc.get("review") or {}).get("reviewers") or []
            edges.append(
                {
                    "from": root,
                    "to": on,
                    "kind": "root_gap",
                    "misconception_id": str(mc["id"]),
                    "misconception_name": str(mc.get("student_name") or ""),
                    "idea": _english(gap.get("idea")),
                    "reviewed": bool(reviewers),
                }
            )
    nodes = [_node(content, own[k], state_of(k), linked=False) for k in sorted(own)]
    nodes += [_node(content, linked[k], state_of(k), linked=True) for k in sorted(linked)]
    reply["nodes"] = nodes
    reply["edges"] = sorted(edges, key=lambda e: (e["kind"], e["from"], e["to"], e.get("misconception_id", "")))
    reply["next"] = what_next(own, {k: state_of(k) for k in own}, p_mastery(conn))
    from agent_core.engine import concept_questions  # it reads this module's gap path

    on_map = {n["kc_id"] for n in nodes}
    reply["questions"] = concept_questions.questions(questions_from or conn, subject_id, on_map, until=until)
    reply["mistakes"] = concept_questions.mistakes(conn, content, on_map, reply["edges"])
    return reply


def what_next(
    kcs: Mapping[str, Mapping[str, Any]], states: Mapping[str, str], p: Mapping[str, float]
) -> list[dict[str, Any]]:
    """The subject's unmastered KCs ranked for "what next": those whose required prerequisites are all secure first,
    then by the planner's value (exam weight x (1 - P)), then by code. Each with its reason: `foundation` (it needs
    nothing earlier), `ready` (everything it needs is secure) or `needs_first` (the unmet required prerequisites,
    in code order); and the exam-weight band."""
    ranked = []
    for kc_id, kc in kcs.items():
        if states.get(kc_id, "not_started") in SECURE:
            continue
        needs = [
            (
                str(n["kc_id"]) if isinstance(n, dict) else str(n),
                str(n.get("strength", "required")) if isinstance(n, dict) else "required",
            )
            for n in kc.get("prerequisites") or []
        ]
        unmet = sorted(
            (
                n
                for n, strength in needs
                if strength == "required" and n in kcs and states.get(n, "not_started") not in SECURE
            ),
            key=lambda n: str(kcs[n]["code"]),
        )
        weight = float(kc.get("exam_weight") or 0.0)
        value = weight * (1 - p.get(kc_id, 0.0))
        reason = "needs_first" if unmet else "ready" if needs else "foundation"
        ranked.append(
            (
                (bool(unmet), len(unmet), -round(value, 12), str(kc["code"])),
                {"kc_id": kc_id, "reason": reason, "weight": weight_band(weight), "needs": unmet[:3]},
            )
        )
    return [entry for _, entry in sorted(ranked, key=lambda r: r[0])[:NEXT_MAX]]


def gap_path(edges: Sequence[Mapping[str, Any]], root_kc: str, on_kc: str) -> list[str]:
    """The shortest chain of map edges from `root_kc` to `on_kc` (both ends included), or [] when there is none.
    Ties go to the earlier edge in the map's edge order, so the path is the same on every read."""
    after: dict[str, list[str]] = {}
    for e in edges:
        after.setdefault(str(e["from"]), []).append(str(e["to"]))
    came: dict[str, str | None] = {root_kc: None}
    queue = deque([root_kc])
    while queue:
        at = queue.popleft()
        if at == on_kc:
            path = [at]
            while (back := came[path[-1]]) is not None:
                path.append(back)
            return path[::-1]
        for nxt in after.get(at, []):
            if nxt not in came:
                came[nxt] = at
                queue.append(nxt)
    return []
