"""Inspectable teaching evidence, derived from checked practice; never preferences or causal efficacy claims."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_core.memory.queries import ContentLabels, NoteRecord
from agent_core.store.events import iso_ms

DAY = 86_400_000


def explain_note(conn: sqlite3.Connection, note: NoteRecord) -> dict[str, Any]:
    sources = []
    for event_id in note.source_event_ids[:20]:
        row = conn.execute(
            "SELECT type,occurred_at,session_id FROM events WHERE event_id=? AND erased_at IS NULL", (event_id,)
        ).fetchone()
        if row:
            sources.append(
                {
                    "event_id": event_id,
                    "kind": row[0],
                    "at": iso_ms(row[1]),
                    "session_id": "ses_" + row[2] if row[2] else None,
                }
            )
    self_report = any(s["kind"] == "memory_requested" for s in sources)
    practice = any(s["kind"] in ("attempt", "verdict", "hint", "mastery_check", "review") for s in sources)
    basis = (
        "mixed"
        if self_report and practice
        else "self_report"
        if self_report
        else ("practice_observation" if practice else "activity" if sources else "unavailable")
    )
    meaning = {
        "self_report": "You asked to save this: a report, not assessed understanding or evidence of effectiveness.",
        "practice_observation": "Saved from practice. This does not establish that a method caused learning.",
        "mixed": "Your report and practice contributed. Their association does not establish teaching effectiveness.",
        "activity": "Saved from recorded activity. Activity alone does not establish understanding.",
        "unavailable": "The original evidence is no longer available; treat this memory as tentative.",
    }[basis]
    return {
        "basis": basis,
        "meaning": meaning,
        "sources": sources,
        "correction": "Use This isn't right or Forget to remove this memory; tell your tutor what to remember instead.",
    }


def teaching_observations(conn: sqlite3.Connection, content: ContentLabels) -> list[dict[str, Any]]:
    """Up to 12 recent delivered teaching skills with same-session and later independent checked attempts.

    The attempts projection includes re-marks and supersession. No generated Study quiz, self-report, model rating,
    or mere skill selection qualifies. Missing receipts and unrelated topics are excluded. Outcomes are association,
    not proof of causation. The bounded window makes absence mean 'not observed here', never 'cannot do it'.
    """
    cursor = conn.execute(
        "SELECT attempt_id,session_id,kc_ids,outcome,hints_used,max_rung,occurred_at,mode FROM attempts"
        " WHERE outcome IN ('correct','incorrect','partial') ORDER BY occurred_at DESC LIMIT 1000"
    )
    attempts = [dict(zip((c[0] for c in cursor.description), row, strict=True)) for row in cursor]
    attempts.reverse()
    for a in attempts:
        a["kcs"] = {k["kc_id"] for k in json.loads(a["kc_ids"])}
    out = []
    delivered = conn.execute(
        "SELECT event_id,session_id,occurred_at,payload FROM events WHERE type='skills_delivered'"
        " AND erased_at IS NULL ORDER BY seq DESC LIMIT 60"
    ).fetchall()
    for event_id, session_id, occurred, payload in delivered:
        related = [a for a in attempts if a["session_id"] == session_id]
        before = [a for a in related if a["occurred_at"] <= occurred]
        kcs = sorted(before[-1]["kcs"]) if before else []
        for skill in json.loads(payload).get("skills", []):
            if skill.get("kind") != "teaching" or skill.get("receipt") is not True:
                continue
            for kc in kcs[:3]:
                immediate = next((a for a in related if kc in a["kcs"] and a["occurred_at"] > occurred), None)
                delayed = next(
                    (
                        a
                        for a in attempts
                        if kc in a["kcs"]
                        and a["session_id"] != session_id
                        and a["occurred_at"] >= occurred + DAY
                        and a["hints_used"] == 0
                        and a["mode"] in ("practice", "review", "mastery_check", "diagnostic", "mock")
                    ),
                    None,
                )

                def result(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
                    if attempt is None:
                        return None
                    return {
                        "attempt_id": attempt["attempt_id"],
                        "outcome": attempt["outcome"],
                        "hints_used": attempt["hints_used"],
                        "max_rung": attempt["max_rung"],
                        "at": iso_ms(attempt["occurred_at"]),
                    }

                conflict = bool(immediate and delayed and immediate["outcome"] != delayed["outcome"])
                out.append(
                    {
                        "source_event_id": event_id,
                        "session_id": "ses_" + session_id,
                        "intervention": skill["name"],
                        "version": skill["version"],
                        "at": iso_ms(occurred),
                        "kc_id": kc,
                        "topic": content.kc_statement(kc) or kc,
                        "immediate": result(immediate),
                        "delayed": result(delayed),
                        "confidence": "tentative",
                        "conflicting_outcomes": conflict,
                        "interpretation": "Observed after teaching; not proof that the method caused learning.",
                    }
                )
                if len(out) == 12:
                    return out
    return out
