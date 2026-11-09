"""The learner's own layer of the concept map: the questions they asked and the mistakes the tutor diagnosed, each
attached to the KC (and misconception) the engine already assigned. No model is asked: everything here is read from
the learner database.

- A question is a learner message, typed or spoken (a voice turn's transcript), that opened a session or asked
  something (`?`). A card answer is never one. A session whose capture was a photo of the question says `photo`.
  It hangs from the KC of the learning events written on the tutor turn that answered it (an attempt's, a hint's or a
  misconception's KC ids), else from the KC its session's events name most (the earliest on a tie); with none it is
  an open question, which the learner can take to the tutor.
- The words are the transcript's (`turns.student_text`, already redacted, PRV-20), so they follow its retention:
  once a session's transcript is purged its questions are gone with it, and a session whose transcript was purged
  but whose events name a KC still counts as one question on that KC, with no words (`kept` false).
- A mistake is a misconception in the learner's projection (`learner_misconceptions`), in any of its five statuses
  (a `disputed` one is the learner's "This isn't right", LRN-37, and shows as such, SPEC §21), with its student name
  from the pack, how often its evidence was seen and, when the pack links it to an earlier skill, that root gap with
  the gap path on the map."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from agent_core.engine.concept_map import gap_path
from agent_core.engine.learn import SUBJECT_NAMES
from agent_core.store.pack_index import PackIndex

QUESTIONS_MAX = 200
TEXT_MAX = 160
PHOTO_SOURCES = ("photo_", "webcam", "pdf", "handwriting_photo")
KC_EVENTS = ("attempt", "hint", "misconception_evidence", "verdict")


def _rows(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    try:
        return conn.execute(sql, tuple(args)).fetchall()
    except sqlite3.OperationalError:
        return []


def _kc_list(raw: Any) -> list[str]:
    """The `events.kc_ids` column: plain ids, or `{kc_id, weight}` objects on attempts."""
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    out = []
    for item in found if isinstance(found, list) else []:
        kc = item.get("kc_id") if isinstance(item, dict) else item
        if isinstance(kc, str) and kc.startswith("kc."):
            out.append(kc)
    return out


def _excerpt(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= TEXT_MAX else one[: TEXT_MAX - 1].rstrip() + "…"


def _photo_sessions(conn: sqlite3.Connection) -> set[str]:
    out = set()
    for session_id, record in _rows(conn, "SELECT session_id, record FROM captures"):
        try:
            body = json.loads(record)
        except (TypeError, json.JSONDecodeError):
            continue
        if body.get("purpose", "question") == "question" and str(body.get("source", "")).startswith(PHOTO_SOURCES):
            out.add(str(session_id))
    return out


def questions(
    conn: sqlite3.Connection, subject_id: str, on_map: set[str], *, until: int | None = None
) -> list[dict[str, Any]]:
    """The learner's questions in this subject, newest first: each with the KC it hangs from (`kc_id`, null for an
    open question), its words while the transcript keeps them, how it was asked and when. A question belongs to the
    subject when its KC is on the subject's map, or, for an open question, when its session was in the subject.
    With `until` (a dated map, SPEC §21), only the questions asked by then."""
    subject = SUBJECT_NAMES.get(subject_id.split(".")[1], "")
    sessions = {
        str(s): (subj, purged)
        for s, subj, purged in _rows(conn, "SELECT session_id, subject, transcript_purged_at FROM sessions")
    }
    by_turn: dict[str, list[str]] = {}
    by_session: dict[str, Counter[str]] = {}
    first_seen: dict[str, dict[str, int]] = {}
    marks = ",".join("?" * len(KC_EVENTS))
    for seq, session_id, turn_id, raw in _rows(
        conn,
        f"SELECT seq, session_id, turn_id, kc_ids FROM events WHERE erased_at IS NULL AND session_id IS NOT NULL"  # noqa: S608
        f" AND kc_ids IS NOT NULL AND type IN ({marks}) ORDER BY seq",
        KC_EVENTS,
    ):
        kcs = _kc_list(raw)
        if turn_id:
            kept = by_turn.setdefault(str(turn_id), [])
            kept.extend(k for k in dict.fromkeys(kcs) if k not in kept)
        counts = by_session.setdefault(str(session_id), Counter())
        seen = first_seen.setdefault(str(session_id), {})
        for kc_id in kcs:
            counts[kc_id] += 1
            seen.setdefault(kc_id, int(seq))

    def session_kc(session_id: str) -> str | None:
        counts = by_session.get(session_id)
        if not counts:
            return None
        return min(counts, key=lambda k: (-counts[k], first_seen[session_id][k]))

    photos = _photo_sessions(conn)
    out: list[dict[str, Any]] = []
    asked = _rows(
        conn,
        "SELECT s.turn_id, s.session_id, s.idx, s.student_text, s.input_kind, s.started_at, t.turn_id"
        " FROM turns s LEFT JOIN turns t ON t.session_id = s.session_id AND t.idx = s.idx + 1"
        " AND t.speaker = 'tutor' WHERE s.speaker = 'student' AND s.student_text IS NOT NULL"
        " AND s.input_kind IN ('typed', 'voice') ORDER BY s.session_id, s.idx",
    )
    opened: set[str] = set()
    for turn_id, session_id, _idx, text, kind, started, tutor_turn in asked:
        session_id = str(session_id)
        first = session_id not in opened
        opened.add(session_id)
        words = str(text).strip()
        if not words or not (first or "?" in words):
            continue
        own = [k for k in by_turn.get(str(tutor_turn), []) if k in on_map]
        kc = own[0] if own else session_kc(session_id)
        if kc is not None and kc not in on_map:
            continue  # another subject's question
        if kc is None and sessions.get(session_id, (None, None))[0] != subject:
            continue
        how = "spoken" if kind == "voice" else "photo" if first and session_id in photos else "typed"
        out.append(
            {
                "question_id": str(turn_id),
                "session_id": f"ses_{session_id}",
                "kc_id": kc,
                "text": _excerpt(words),
                "input": how,
                "asked_at": int(started),
                "kept": True,
            }
        )
    for session_id, (_subj, purged) in sorted(sessions.items()):
        if purged is None or session_id in opened:
            continue
        kc = session_kc(session_id)
        if kc is not None and kc in on_map:
            out.append(
                {
                    "question_id": f"ses_{session_id}",
                    "session_id": f"ses_{session_id}",
                    "kc_id": kc,
                    "text": None,
                    "input": "typed",
                    "asked_at": int(purged),
                    "kept": False,
                }
            )
    if until is not None:
        out = [q for q in out if q["asked_at"] <= until]
    out.sort(key=lambda q: (-q["asked_at"], q["question_id"]))
    return out[:QUESTIONS_MAX]


def mistakes(
    conn: sqlite3.Connection, content: PackIndex, on_map: set[str], edges: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The learner's diagnosed mistakes on this map's KCs, most seen first: the misconception's student name, its
    status and evidence count, and its root gap (the earlier skill, whether a teacher reviewed the link, and the gap
    path from it to the KC)."""
    out: list[dict[str, Any]] = []
    rows = _rows(
        conn,
        "SELECT misconception_id, kc_id, status, evidence_count, last_seen_at FROM learner_misconceptions",
    )
    for mc_id, kc_id, status, count, last_seen in rows:
        if str(kc_id) not in on_map:
            continue
        record = content.record("misconceptions", str(mc_id)) or {}
        root_gap = record.get("root_gap")
        gap = root_gap if isinstance(root_gap, dict) else {}
        root = str(gap.get("kc_id")) if gap.get("kc_id") else None
        idea = gap.get("idea")
        reviewers = (record.get("review") or {}).get("reviewers") or []
        path = gap_path(edges, root, str(kc_id)) if root else []
        out.append(
            {
                "misconception_id": str(mc_id),
                "kc_id": str(kc_id),
                "name": str(record.get("student_name") or content.misconception_name(str(mc_id)) or ""),
                "status": str(status),
                "times": int(count),
                "last_seen_at": int(last_seen),
                "root_kc_id": root if path else None,
                "idea": (str(idea.get("en", "")) if isinstance(idea, Mapping) else str(idea or "")) if path else None,
                "reviewed": bool(reviewers) if path else None,
                "gap_path": path,
            }
        )
    # Watched ones first (most seen first), then the ones the learner disputed, then the resolved ones.
    later = {"disputed": 1, "resolved": 2}
    out.sort(key=lambda m: (later.get(m["status"], 0), -m["times"], -m["last_seen_at"], m["misconception_id"]))
    return out
