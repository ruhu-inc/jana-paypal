"""What memory a session starts with (MEM-03, LRN-05) and the learner-model snapshot of the contract (LRN-03).

`freeze` runs the fixed queries once, at the session's first Coach prepare, and the result never changes for the
session except that a forgotten note is removed from it (`without_note`). It lives in memory only and is never
checkpointed. `learner_model_snapshot` is the contract form of the whole learner model, rebuilt from the
projections and the profile; it reads no clock, so the same tables always give the same document.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from agent_core.config import Settings
from agent_core.memory.queries import (
    ContentLabels,
    KcLine,
    MisconceptionLine,
    NoteRecord,
    Preference,
    ProfileSummary,
    active_misconceptions,
    preferences,
    profile_summary,
    stored_notes,
    subject_notes,
    top_kcs,
)
from agent_core.store.events import iso_ms

# The learner model's version in the contract's form; it moves with any projection's code version.
PROJECTION_VERSION = "1.16.0"  # 1.16: the plan projection completes Study-card warm-ups (W8)
MASTERY_MODEL = "pfa_elo"
BETA_PRIOR = {"alpha": 1.0, "beta": 1.0}  # LRN-22: every style starts at Beta(1, 1)
STUDENT_SETTINGS = ("low_power", "text_scale", "memory_saving", "notifications", "reduced_motion", "read_aloud",
                    "easy_read", "extra_time_percent", "speech_rate", "speech_voice", "theme",
                    "character_shortcuts", "tutor_name", "tutor_avatar", "faster_tutor", "wake_word",
                    "checkin_notifications")  # fmt: skip


@dataclass(frozen=True)
class FrozenMemory:
    """The fixed queries' answers at session start, in the order the learner block renders them."""

    session_date: str  # YYYY-MM-DD; the block has no clock
    profile: ProfileSummary
    kcs: tuple[KcLine, ...]
    misconceptions: tuple[MisconceptionLine, ...]
    preferences: tuple[Preference, ...]
    recent: tuple[NoteRecord, ...]  # shown in full
    older: tuple[NoteRecord, ...]  # shown as index lines (MEM-13)

    def without_note(self, note_id: str) -> FrozenMemory:
        """The same snapshot with one note removed: the only change a frozen snapshot allows (MEM-14)."""
        return replace(
            self,
            recent=tuple(n for n in self.recent if n.note_id != note_id),
            older=tuple(n for n in self.older if n.note_id != note_id),
        )

    @property
    def note_ids(self) -> tuple[str, ...]:
        return tuple(n.note_id for n in (*self.recent, *self.older))


def freeze(
    conn: sqlite3.Connection,
    content: ContentLabels,
    settings: Settings,
    *,
    subject: str | None,
    topic_id: str | None,
    session_date: str,
    focus: Sequence[str] = (),
    kcs_max: int | None = None,
    misconceptions_max: int | None = None,
) -> FrozenMemory:
    """Run the fixed queries with the session's parameters (`snapshot_top_kcs`, `snapshot_misconceptions_max`,
    `snapshot_recent_notes`). `focus` names the current problem's KCs, which come first among the topic's. A Study
    session's snapshot asks for more KCs and misconceptions than a tutoring prompt holds (`kcs_max`,
    `misconceptions_max`; SPEC §19.4): its block is structured data, not prompt text with an allowance."""
    notes = subject_notes(conn, subject)
    split = max(settings.snapshot_recent_notes, 0)
    n_kcs = settings.snapshot_top_kcs if kcs_max is None else kcs_max
    n_misconceptions = settings.snapshot_misconceptions_max if misconceptions_max is None else misconceptions_max
    return FrozenMemory(
        session_date=session_date,
        profile=profile_summary(conn, today=session_date, settings=settings),
        kcs=tuple(top_kcs(conn, content, topic_id, n_kcs, settings, focus=focus)),
        misconceptions=tuple(active_misconceptions(conn, content, topic_id, n_misconceptions)),
        preferences=tuple(preferences(conn)),
        recent=tuple(notes[:split]),
        older=tuple(notes[split:]),
    )


# --- the learner block on Study requests (SPEC §19.3.1, W8) -------------------------------------------------------

LEARNER_BLOCK_KCS = 24
LEARNER_BLOCK_MISCONCEPTIONS = 8
LEARNER_BLOCK_SUBJECTS = ("mathematics", "physics", "chemistry", "biology", "accounting")
_KC_ID = re.compile(r"^kc\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+$")
_MC_ID = re.compile(r"^mc\.[a-z0-9_.]+$")


def learner_block_data(frozen: FrozenMemory, variant: Literal["cloud"] = "cloud") -> dict[str, Any]:
    """The cloud variant of the learner block as data (`LearnerBlock`, MDL-25's set and nothing else): the profile's
    level and subjects, at most 24 KCs in three bands in the snapshot's order (the focus KCs first, then the most
    recently practised, then the rest), and at most 8 watched misconception ids. No notes, preferences, name, exam
    date or reading level: those never leave for a Study request. `cloud_notes` (with notes) is phase 3."""
    if variant != "cloud":
        raise ValueError(f"learner block variant {variant!r} is not available")
    from agent_core.memory.render import BANDS  # the render module imports this one

    profile = frozen.profile
    subjects = [s for s in dict.fromkeys(profile.subjects) if s in LEARNER_BLOCK_SUBJECTS][:5]
    kcs = [{"kc_id": k.kc_id, "band": BANDS.get(k.state, "learning")} for k in frozen.kcs if _KC_ID.match(k.kc_id)]
    misconceptions = [m.misconception_id for m in frozen.misconceptions if _MC_ID.match(m.misconception_id)]
    return {
        "level": profile.level if profile.level in ("O", "A") and profile.subjects else None,
        "subjects": subjects,
        "kcs": kcs[:LEARNER_BLOCK_KCS],
        "misconception_ids": list(dict.fromkeys(misconceptions))[:LEARNER_BLOCK_MISCONCEPTIONS],
    }


# --- the learner-model contract (LRN-03) -----------------------------------------------------------------------


def _iso(ms: int | None) -> str | None:
    return iso_ms(int(ms)) if ms is not None else None


def _json(raw: str | None) -> Any:
    return json.loads(raw) if raw else None


def _profile(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    row = conn.execute(
        "SELECT display_name, level, subjects, exam_date, reading_level, locale, support_level, goals,"
        " student_settings FROM profile"
    ).fetchone()
    if row is None:
        raise LookupError("the learner database has no profile")
    name, level, subjects_raw, exam_date, reading, locale, support, goals_raw, student_raw = row
    subjects: list[dict[str, Any]] = []
    for entry in _json(subjects_raw) or []:
        item = {"subject": entry, "level": level} if isinstance(entry, str) else dict(entry)
        item.setdefault("level", level)
        if exam_date and "exam_date" not in item:
            item["exam_date"] = exam_date
        subjects.append({k: v for k, v in item.items() if k in ("subject", "level", "exam_date")})
    student = {k: v for k, v in (_json(student_raw) or {}).items() if k in STUDENT_SETTINGS}
    stored = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    for key in STUDENT_SETTINGS:
        if key in stored:
            student[key] = json.loads(stored[key])
    out: dict[str, Any] = {"display_name": name, "level": level, "subjects": subjects, "locale": locale or "en",
                           "support_level": support or "standard", "student_settings": student}  # fmt: skip
    if reading:
        out["reading_level"] = reading
    goals = _json(goals_raw)
    if isinstance(goals, list):
        out["goals"] = [str(g) for g in goals][:5]
    return out


def _projection(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT params_hash, last_seq, last_hlc, last_device_id, last_origin_seq, rebuilt_at FROM projection_state"
        " ORDER BY last_seq DESC, name"
    ).fetchall()
    if not rows:
        return {"projection_version": PROJECTION_VERSION, "params_hash": "0" * 64, "last_seq": 0,
                "rebuilt_at": iso_ms(0), "mastery_model": MASTERY_MODEL, "merge_cursor": None}  # fmt: skip
    top = rows[0]
    rebuilt = max((r[5] for r in rows if r[5] is not None), default=0)
    cursor = {"hlc": top[2], "device_id": top[3], "origin_seq": top[4]} if top[2] is not None else None
    return {"projection_version": PROJECTION_VERSION, "params_hash": top[0], "last_seq": int(top[1]),
            "rebuilt_at": iso_ms(int(rebuilt)), "mastery_model": MASTERY_MODEL, "merge_cursor": cursor}  # fmt: skip


def _kc_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    rows = conn.execute(
        "SELECT kc_id, status, p_mastery, n_eff, succ_w, fail_w, implied_w, attempts, hints_used, bottom_outs,"
        " median_time_ms, fading_stage, stuck_count, target_offset, mastery_check, last_seen_at FROM kc_mastery"
        " ORDER BY kc_id"
    )
    for r in rows:
        item: dict[str, Any] = {"kc_id": r[0], "status": r[1], "p_mastery": r[2], "n_eff": r[3], "succ_w": r[4],
                                "fail_w": r[5], "attempts": r[7], "hints_used": r[8], "bottom_outs": r[9],
                                "stuck_count": r[12], "last_seen": _iso(r[15])}  # fmt: skip
        for key, value in (("implied_w", r[6]), ("median_time_ms", r[10]), ("fading_stage", r[11]),
                           ("target_offset", r[13])):  # fmt: skip
            if value is not None:
                item[key] = value
        check = _json(r[14])
        if isinstance(check, dict):
            keep = ("state", "items_done", "first_item_at", "retry_after")
            item["mastery_check"] = {k: v for k, v in check.items() if k in keep}
        out.append(item)
    return out


def _review_cards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT kc_id, state, stability, difficulty, due_at, reps, lapses, last_review_at, scheduler FROM review_cards"
        " ORDER BY kc_id"
    )
    out = []
    for r in rows:
        item = {"kc_id": r[0], "state": r[1], "stability": r[2], "difficulty": r[3], "due_at": iso_ms(r[4]),
                "reps": r[5], "lapses": r[6], "last_review": _iso(r[7])}  # fmt: skip
        if r[8] is not None:
            item["scheduler"] = r[8]
        out.append(item)
    return out


def _misconceptions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT misconception_id, kc_id, status, evidence_count, first_seen_at, last_seen_at, fixes_tried,"
        " resolving_correct FROM learner_misconceptions ORDER BY misconception_id"
    )
    out = []
    for r in rows:
        item = {"misconception_id": r[0], "kc_id": r[1], "status": r[2], "evidence_count": r[3],
                "first_seen": iso_ms(r[4]), "last_seen": iso_ms(r[5]), "fixes_tried": r[6]}  # fmt: skip
        if r[2] == "resolving" and r[7] is not None:
            item["resolving_correct"] = r[7]
        out.append(item)
    return out


def _preferences(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {
        "start_style": {"worked_example": dict(BETA_PRIOR), "hint": dict(BETA_PRIOR)},
        "representation": {k: dict(BETA_PRIOR) for k in ("diagram", "simpler_numbers", "analogy", "worked_example")},
        "unprompted_help": True,  # offers of help are on until the student switches them off
        "self_explanation_prompts": True,
    }
    overrides = []
    for key, raw in conn.execute("SELECT key, value FROM preferences ORDER BY key"):
        value = json.loads(raw)
        if isinstance(value, dict) and "override" in value:
            overrides.append(key)
            if key in ("pace", "unprompted_help", "self_explanation_prompts"):
                out[key] = value["override"]
    if overrides:
        out["student_overrides"] = overrides
    return out


def _engagement(conn: sqlite3.Connection) -> dict[str, Any]:
    state = conn.execute(
        "SELECT streak_days, freezes_left, last_active_date, frustration, confidence_bias FROM engagement_state"
    ).fetchone()
    days = conn.execute(
        "SELECT minutes_active, sessions, gaming_flags FROM engagement_daily ORDER BY day DESC LIMIT 7"
    ).fetchall()
    out: dict[str, Any] = {
        "streak_days": state[0] if state else 0,
        "freezes_left": state[1] if state else 0,
        "last_active_date": state[2] if state else None,
        "minutes_7d": sum(d[0] for d in days),
        "sessions_7d": sum(d[1] for d in days),
        "frustration": state[3] if state else 0.0,
        "gaming_flags_7d": sum(d[2] for d in days),
    }
    if state is not None and state[4] is not None:
        out["confidence_bias"] = state[4]
    return out


def _notes(conn: sqlite3.Connection, now_ms: int | None = None) -> list[dict[str, Any]]:
    out = []
    notes = sorted(stored_notes(conn), key=lambda n: (n.created_at, n.note_id))[:50]
    from agent_core.learner.projections import note_lifecycle

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    derived = note_lifecycle(conn, [n.note_id for n in notes], now)
    for note in notes:
        item: dict[str, Any] = {"note_id": note.note_id, "text": note.text,
                                "source_event_ids": list(note.source_event_ids),
                                "created_at": iso_ms(note.created_at), "status": note.status}  # fmt: skip
        if note.note_id in derived:  # SPEC §19.3.5: derived, never written by a model
            item["confidence"] = derived[note.note_id].confidence
            item["fading"] = derived[note.note_id].fading
        if note.subject is not None:
            item["subject"] = note.subject
        if note.kc_ids:
            item["kc_ids"] = list(note.kc_ids)[:5]
        expires = conn.execute("SELECT expires_at FROM notes WHERE note_id = ?", (note.note_id,)).fetchone()
        if expires is not None and expires[0] is not None:
            item["expires_at"] = iso_ms(expires[0])
        out.append(item)
    return out


def learner_model_snapshot(
    conn: sqlite3.Connection, profile_id: str, content: ContentLabels, settings: Settings | None = None
) -> dict[str, Any]:
    """The learner model as the learner-model contract shapes it (LRN-03), from the projections only."""
    settings = settings or Settings()
    abilities = {r[0]: {"theta": r[1], "n": r[2]} for r in conn.execute(
        "SELECT subject, theta, n FROM subject_ability ORDER BY subject")}  # fmt: skip
    return {
        "schema_version": "1.0",
        "profile_id": profile_id,
        "projection": _projection(conn),
        "profile": _profile(conn, settings),
        "subject_ability": abilities,
        "kc_states": _kc_states(conn),
        "review_cards": _review_cards(conn),
        "misconceptions": _misconceptions(conn),
        "preferences": _preferences(conn),
        "engagement": _engagement(conn),
        "notes": _notes(conn),
    }
