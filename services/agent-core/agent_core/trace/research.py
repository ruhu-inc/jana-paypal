"""MEM-26, PRD-19, EVL-22: numerical learning evidence, never memory content."""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from agent_core.config import Settings

EVENTS = frozenset({"attempt", "verdict", "hint", "mastery_check", "session_ended", "diagnostic_item"})
NUMBERS = frozenset(
    {
        "active_ms",
        "duration_ms",
        "attempts",
        "hint_rung",
        "score",
        "max_score",
        "correct",
        "passed",
        "bottom_out",
        "newly_mastered",
        "time_ms",
        "rung",
        "marks_awarded",
        "marks_available",
        "closes_opportunity",
    }
)


def statistic(participants: int, total: float, settings: Settings | None = None) -> dict[str, Any]:
    suppressed = participants < (settings or Settings()).research_min_participants
    return {
        "sample_size": participants,
        "value": None if suppressed else total / participants,
        "suppressed": suppressed,
        "review_required": suppressed,
    }


def project(raw: dict[str, Any]) -> dict[str, Any]:
    events = []
    for event in raw.get("events", []):
        if event.get("type") not in EVENTS:
            continue
        try:
            day = date.fromisoformat(str(event["occurred_at"])[:10]).isoformat()
        except (KeyError, ValueError):
            continue
        payload = event.get("payload", {})
        entry = {
            key: value
            for key, value in payload.items()
            if key in NUMBERS and type(value) in (int, float, bool) and math.isfinite(value)
        }
        for key, allowed in {
            "item_result": {"pass", "fail"},
            "check_state": {"in_progress", "passed", "failed"},
            "outcome": {"correct", "incorrect", "partial", "unverifiable"},
            "mode": {
                "guided",
                "solution",
                "check_working",
                "learn",
                "practice",
                "mock",
                "revision",
                "diagnostic",
                "mastery_check",
                "review",
            },
        }.items():
            if payload.get(key) in allowed:
                entry[key] = payload[key]
        kc = payload.get("kc_id")
        if isinstance(kc, str) and re.fullmatch(r"kc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+", kc):
            entry["kc_id"] = kc
        events.append({"type": event["type"], "day": day, **entry})
    mastery = []
    for row in raw.get("kc_mastery", []):
        # KC identifiers stay out of this analysis projection; it cannot join to the device's learner record.
        kept = {
            key: value
            for key, value in row.items()
            if key in {"p_mastery", "attempt_count", "confirmed"}
            and type(value) in (int, float, bool)
            and math.isfinite(value)
        }
        if kept:
            mastery.append(kept)
    return {"events": events, "kc_mastery": mastery, "item_responses": item_responses(raw),
            "skill_sessions": skill_sessions(raw)}  # fmt: skip


ITEM_ID = re.compile(r"itm\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[0-9]{4}")
CHOICE_ID = re.compile(r"[A-Za-z0-9_]{1,16}")
KC_ID = re.compile(r"kc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+")
MISCONCEPTION_ID = re.compile(r"mc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+")
SKILL = re.compile(r"[a-z][a-z0-9-]*[a-z0-9]")
VERSION = re.compile(r"\d{1,4}\.\d{1,4}(\.\d{1,4})?")


def _whole(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and len(value) <= 96 and pattern.fullmatch(value) is not None


def item_responses(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The first responses to pack items (CUR-26, ASM-21): the item, the outcome, the time, the option chosen and the
    misconception matched, each checked against its form; anything else a row carries (the student's words, an id)
    is dropped."""
    rows = []
    for row in raw.get("item_responses", []):
        if not isinstance(row, dict):
            continue
        if not _whole(row.get("item_id"), ITEM_ID) or row.get("outcome") not in ("correct", "incorrect", "partial"):
            continue
        time_ms = row.get("time_ms")
        if type(time_ms) is not int or not 0 <= time_ms <= 86_400_000:
            continue
        kept = {"item_id": row["item_id"], "outcome": row["outcome"], "time_ms": time_ms}
        if _whole(row.get("choice_id"), CHOICE_ID):
            kept["choice_id"] = row["choice_id"]
        if _whole(row.get("misconception_id"), MISCONCEPTION_ID):
            kept["misconception_id"] = row["misconception_id"]
        if _whole(row.get("kc_id"), KC_ID):
            kept["kc_id"] = row["kc_id"]
        rows.append(kept)
    return rows


def skill_sessions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Per tutoring session, the skills delivered by (name, version), the hints used and the mastery gain (SKL-28)."""
    rows = []
    for row in raw.get("skill_sessions", []):
        if not isinstance(row, dict) or not isinstance(row.get("skills"), list):
            continue
        skills = [
            {"name": s["name"], "version": s["version"]}
            for s in row["skills"]
            if isinstance(s, dict) and _whole(s.get("name"), SKILL) and _whole(s.get("version"), VERSION)
        ]
        hints, gain = row.get("hints"), row.get("mastery_gain")
        if not skills or type(hints) is not int or hints < 0:
            continue
        if gain is not None and (type(gain) is not float or not math.isfinite(gain) or abs(gain) > 1):
            gain = None
        rows.append({"skills": skills, "hints": hints, "mastery_gain": gain})
    return rows


def projected(rows: dict[str, Any]) -> bool:
    """Whether `rows` are exactly what `project` emits, so nothing in them is what a device's projection would drop:
    the student's words, an identifier or a field outside the allow-list (AUT-37's intake reads this)."""
    events, mastery = rows.get("events", []), rows.get("kc_mastery", [])
    if not all(isinstance(v, list) and all(isinstance(e, dict) for e in v) for v in (events, mastery)):
        return False
    wrapped = [{"type": e.get("type"), "occurred_at": e.get("day"),
                "payload": {k: v for k, v in e.items() if k not in ("type", "day")}} for e in events]  # fmt: skip
    again = project({**rows, "events": wrapped})
    return all(again[key] == rows.get(key, []) for key in again)
