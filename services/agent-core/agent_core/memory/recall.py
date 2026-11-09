"""The two recall reads behind the memory tools (MEM-05, MEM-18, TOL-23).

`memory_recall` answers one of five fixed queries for the session's own profile, live, with integer refs expandable
only for `recent_notes`; its result carries a fixed records-not-instructions marker. `session_recall` scans the
current session's stored transcript (student text, tutor text, board LaTeX) with a literal, case-insensitive token
match where `*` is the only wildcard, never a regular expression built from model input. The engine's tool handlers
call these; the handlers bind the connection to the session's profile and map a refusal to `invalid_args`.

Note text in a result is escaped as in the learner block and re-checked: a note that fails the render check is
left out of a result just as it is left out of the prompt (MEM-27).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agent_core.config import Settings
from agent_core.memory.capture import CaptureFilter, default_filter
from agent_core.memory.policy import render_check
from agent_core.memory.queries import (
    ContentLabels,
    NoteRecord,
    active_misconceptions,
    notes_by_id,
    preferences,
    profile_summary,
    session_history,
    subject_notes,
    top_kcs,
)
from agent_core.memory.refs import RefOutOfRange, RefTable
from agent_core.memory.render import (
    BANDS,
    NOTES_CLOSE,
    NOTES_OPEN,
    RECORDS_LINE,
    STATE_WORDS,
    escape_markup,
    escape_note,
    wrap_notes,
)
from agent_core.memory.scrub import scrub_for_egress

QueryId = Literal["profile_summary", "top_kcs", "active_misconceptions", "recent_notes", "preferences"]
QUERY_IDS: tuple[QueryId, ...] = ("profile_summary", "top_kcs", "active_misconceptions", "recent_notes", "preferences")
MAX_REFS = 5  # refs per memory_recall call (design §4)
RECALL_MARKER = f"{NOTES_OPEN} {RECORDS_LINE} {NOTES_CLOSE}"


class RecallRefused(ValueError):
    """A recall the rules forbid: refs with a query other than recent_notes, too many refs, a ref outside the
    session's table, or another profile."""


@dataclass(frozen=True)
class RecallResult:
    query_id: QueryId
    items: tuple[dict[str, object], ...]
    marker: str  # the fixed records-not-instructions line

    def lines(self) -> list[str]:
        """One line per record, in words, every value escaped as note text is; repeated lines dropped."""
        out = [_line(self.query_id, item) for item in self.items]
        return list(dict.fromkeys(out))

    @property
    def records(self) -> str:
        """The records as the model reads them: inside the records wrapper with its fixed records-not-instructions
        line (MEM-27), one escaped line per record."""
        return wrap_notes(self.lines() or ["(none)"])

    def as_tool_result(self) -> dict[str, object]:
        """The tool result's JSON form: the query, how many records it returned (for the memory-use line, MEM-28)
        and the records as one wrapped string. No record text sits in any field outside the wrapper."""
        return {"query_id": self.query_id, "count": len(self.items), "records": self.records}


@dataclass(frozen=True)
class RecallMatch:
    turn_ref: int
    source: Literal["student", "tutor", "board"]
    text: str  # at most `session_recall_match_chars`, student text escaped


def _line(query_id: QueryId, item: dict[str, object]) -> str:
    """One record of a recall in words. Text values were escaped when the item was built; the rest is ours."""
    if query_id == "recent_notes":
        ref = item.get("ref")
        return f"[{ref}] {item['date']} · {item['text']}" if ref else f"{item['date']} · {item['text']}"
    if query_id == "top_kcs":
        return f"skill: {item['skill']}; state: {item['state']}; hint use: {item['hint_use']}"
    if query_id == "active_misconceptions":
        return f"mistake: {item['mistake']} ({item['status']})"
    if query_id == "preferences":
        owner = "set by the student" if item.get("set_by_student") else "inferred"
        return f"preference {item['preference']}: {item['value']} ({owner})"
    parts = [f"{key.replace('_', ' ')}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
             for key, v in item.items() if v not in (None, "", [])]  # fmt: skip
    return escape_note("; ".join(parts))


def _own_profile(conn: sqlite3.Connection, expect_profile: str | None) -> None:
    if expect_profile is None:
        return
    row = conn.execute("SELECT profile_id FROM meta").fetchone()
    if row is None or row[0] != expect_profile:
        raise RecallRefused("the session's profile does not own this store")


def _note_item(note: NoteRecord, ref: int) -> dict[str, object]:
    return {"ref": ref, "date": note.day, "text": escape_note(note.text)}


def memory_recall(
    conn: sqlite3.Connection,
    content: ContentLabels,
    query_id: QueryId,
    refs: Sequence[int],
    note_refs: RefTable,
    settings: Settings,
    *,
    subject: str | None,
    topic_id: str | None,
    session_date: str | None = None,
    expect_profile: str | None = None,
    capture: CaptureFilter | None = None,
    egress_terms: Sequence[str] | None = None,
    recalled_in: tuple[str, str] | None = None,
) -> RecallResult:
    """A live read of one fixed query. Refs (at most `MAX_REFS`, plain integers in the session's note table) are
    allowed only with `recent_notes`, where they name the notes to expand; without refs, `recent_notes` returns the
    newest `snapshot_recent_notes`. A note deleted since the snapshot is simply absent. `expect_profile`, when
    given, must be the profile the store belongs to.

    `egress_terms` (SPEC §19.6, W8 phase 3): the call is a cloud call under `tutor_memory_online`; the answer carries
    only what the cloud_notes variant may: notes that pass `scrub_for_egress` (with these Redactor terms), KC ids with
    bands, misconception ids, the cloud profile line's fields, and no preferences. `recalled_in` = (session id, day):
    one `note_recalls` row per note returned (§19.3.5)."""
    if query_id not in QUERY_IDS:
        raise RecallRefused(f"unknown query {query_id!r}")
    if not isinstance(refs, Sequence) or isinstance(refs, str | bytes):
        raise RecallRefused("refs must be a list of integers")
    if refs and query_id != "recent_notes":
        raise RecallRefused("refs are allowed only with recent_notes")
    if len(refs) > MAX_REFS:
        raise RecallRefused(f"at most {MAX_REFS} refs")
    try:
        wanted = note_refs.resolve_all(list(refs))
    except RefOutOfRange as error:
        raise RecallRefused(str(error)) from None
    _own_profile(conn, expect_profile)
    capture = capture or default_filter(settings)
    cloud = egress_terms is not None
    if cloud and query_id == "preferences":
        raise RecallRefused("preferences are not read for a cloud model")
    items: list[dict[str, object]]
    if query_id == "profile_summary" and cloud:
        p = profile_summary(conn, today=session_date, settings=settings)
        items = [{"level": p.level, "subjects": list(p.subjects), "exam": p.exam_distance,
                  "solution_policy": p.solution_policy}]  # fmt: skip
    elif query_id == "top_kcs" and cloud:
        kcs = top_kcs(conn, content, topic_id, settings.snapshot_top_kcs, settings)
        items = [{"skill": k.kc_id, "state": BANDS.get(k.state, k.state), "hint_use": k.hint_use} for k in kcs]
    elif query_id == "active_misconceptions" and cloud:
        found = active_misconceptions(conn, content, topic_id, settings.snapshot_misconceptions_max)
        items = [{"mistake": m.misconception_id, "status": m.status} for m in found]
    elif query_id == "profile_summary":
        p = profile_summary(conn, today=session_date, settings=settings)
        items = [{"level": p.level, "subjects": list(p.subjects), "exam": p.exam_distance,
                  "reading_level": p.reading_level, "support_level": p.support_level,
                  "solution_policy": p.solution_policy}]  # fmt: skip
    elif query_id == "top_kcs":
        kcs = top_kcs(conn, content, topic_id, settings.snapshot_top_kcs, settings)
        items = [{"skill": escape_note(k.statement), "state": STATE_WORDS[k.state], "hint_use": k.hint_use}
                 for k in kcs]  # fmt: skip
    elif query_id == "active_misconceptions":
        found = active_misconceptions(conn, content, topic_id, settings.snapshot_misconceptions_max)
        items = [{"mistake": escape_note(m.name), "status": m.status} for m in found]
    elif query_id == "preferences":
        items = [{"preference": p.key, "value": escape_note(p.value), "set_by_student": p.origin == "override"}
                 for p in preferences(conn)]  # fmt: skip
    else:
        if wanted:
            live = notes_by_id(conn, wanted)
            notes = [live[i] for i in dict.fromkeys(wanted) if i in live and live[i].status == "active"]
        else:
            notes = subject_notes(conn, subject)
        # A note that fails the render check is left out without taking a place; a note outside the session's table
        # (written after the snapshot) is shown without a ref (0).
        passing = [n for n in notes if not render_check(n.text, settings, capture)]
        if cloud:
            passing = [n for n in passing if scrub_for_egress(n.text, settings, capture, egress_terms or ()) is None]
        if not wanted:
            passing = passing[: settings.snapshot_recent_notes]
        items = [_note_item(n, note_refs.ref_of(n.note_id) if n.note_id in note_refs.ids else 0) for n in passing]
        if recalled_in is not None:
            record_recalls(conn, [n.note_id for n in passing], *recalled_in)
    return RecallResult(query_id, tuple(items), RECALL_MARKER)


def record_recalls(conn: sqlite3.Connection, note_ids: Sequence[str], session_id: str, day: str) -> None:
    """One `note_recalls` row per note, session and day (SPEC §19.3.5): device-local, never egress (JM-D27). A
    database without the table (before v21) records nothing."""
    session = session_id.removeprefix("ses_")
    try:
        conn.executemany("INSERT OR IGNORE INTO note_recalls (note_id, session_id, day) VALUES (?, ?, ?)",
                         [(note_id, session, day) for note_id in dict.fromkeys(note_ids)])  # fmt: skip
    except sqlite3.OperationalError:
        return


def recall_sessions(conn: sqlite3.Connection) -> dict[str, int]:
    """How many sessions each note was recalled in (SPEC §19.3.5)."""
    try:
        rows = conn.execute("SELECT note_id, count(DISTINCT session_id) FROM note_recalls GROUP BY note_id")
        return {str(r[0]): int(r[1]) for r in rows}
    except sqlite3.OperationalError:
        return {}


# --- session recall (MEM-18) ---------------------------------------------------------------------------------------


def _segments(pattern: str) -> list[str]:
    """The literal pieces of a pattern between `*` wildcards, case-folded, with spacing folded."""
    folded = " ".join(pattern.split()).casefold()
    return [piece for piece in folded.split("*") if piece]


def _find(text: str, segments: Sequence[str]) -> tuple[int, int] | None:
    """Where the segments occur in order in `text` (case-insensitive), as (start, end); plain string search only."""
    folded = text.casefold()
    position, start = 0, -1
    for piece in segments:
        found = folded.find(piece, position)
        if found == -1:
            return None
        start = found if start == -1 else start
        position = found + len(piece)
    return start, position


def _window(text: str, start: int, end: int, limit: int) -> str:
    if len(text) <= limit:
        return text
    middle = (start + end) // 2
    left = max(0, min(middle - limit // 2, len(text) - limit))
    return text[left : left + limit]


def session_recall(conn: sqlite3.Connection, session_id: str, pattern: str, settings: Settings) -> list[RecallMatch]:
    """At most `session_recall_max_matches` matches for `pattern` (at most `session_recall_pattern_max_chars`).

    The match is a case-insensitive search for the pattern's literal pieces in order, `*` standing for anything in
    between, within one line of the session's transcript; nothing in the pattern is interpreted otherwise. Each
    match is the line, or a window of `session_recall_match_chars` around the match, escaped, with its turn ref."""
    if not isinstance(pattern, str) or len(pattern) > settings.session_recall_pattern_max_chars:
        raise RecallRefused(f"the pattern is at most {settings.session_recall_pattern_max_chars} characters")
    segments = _segments(pattern)
    if not segments or all(not piece.strip() for piece in segments):
        raise RecallRefused("the pattern has no text to look for")
    out: list[RecallMatch] = []
    for line in session_history(conn, session_id):
        flat = " ".join(line.text.split())
        hit = _find(flat, segments)
        if hit is None:
            continue
        window = _window(flat, hit[0], hit[1], settings.session_recall_match_chars)
        escaped = escape_markup(window)
        out.append(RecallMatch(line.turn_ref, line.source, escaped[: settings.session_recall_match_chars]))
        if len(out) >= settings.session_recall_max_matches:
            break
    return out


# --- past sessions (SPEC §19.3.3, W8 phase 2) ------------------------------------------------------------------

PAST_MATCH_CHARS = 200
PAST_MAX = 8


@dataclass(frozen=True)
class PastMatch:
    session_date: str  # YYYY-MM-DD of the session's start
    subject: str | None
    source: Literal["student", "tutor", "board"]
    text: str  # at most 200 characters, escaped


def session_recall_past(conn: sqlite3.Connection, session_id: str, pattern: str, settings: Settings,
                        limit: int = PAST_MAX) -> list[PastMatch]:  # fmt: skip
    """`session_recall` with `scope: "past"`: the same literal match (`_segments`, `_find`; `*` the only wildcard) over
    the profile's other sessions' stored transcripts, newest session first, at most `limit` (≤ 8) matches of at most
    200 characters, each with its session's date and subject. Purged transcripts hold nothing to find. Served to a
    local candidate only (§19.6): the handler refuses it on a cloud call."""
    if not isinstance(pattern, str) or len(pattern) > settings.session_recall_pattern_max_chars:
        raise RecallRefused(f"the pattern is at most {settings.session_recall_pattern_max_chars} characters")
    segments = _segments(pattern)
    if not segments or all(not piece.strip() for piece in segments):
        raise RecallRefused("the pattern has no text to look for")
    limit = max(1, min(int(limit), PAST_MAX))
    rows = conn.execute(
        "SELECT session_id, subject, started_at FROM sessions WHERE session_id <> ? AND transcript_purged_at IS NULL"
        " ORDER BY started_at DESC, session_id DESC",
        (session_id.removeprefix("ses_"),),
    ).fetchall()
    out: list[PastMatch] = []
    for sid, subject, started in rows:
        day = datetime.fromtimestamp(int(started) / 1000, UTC).date().isoformat()
        for line in session_history(conn, str(sid)):
            flat = " ".join(line.text.split())
            hit = _find(flat, segments)
            if hit is None:
                continue
            window = escape_markup(_window(flat, hit[0], hit[1], PAST_MATCH_CHARS))
            out.append(PastMatch(day, subject, line.source, window[:PAST_MATCH_CHARS]))
            if len(out) >= limit:
                return out
    return out
