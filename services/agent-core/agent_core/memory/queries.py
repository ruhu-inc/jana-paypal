"""The fixed read queries of the learner model and the episodic layer (MEM-04).

Every read of memory for a prompt, a tool or a view goes through one of these, with parameters set by code: a model
never writes query text (MEM-05). Content labels (KC statements, topic names, misconception names) come from the
verified installed pack through `ContentLabels` (`agent_core.store.pack_index.PackIndex` implements it).

A note's owner and version are read from the event log, not a column: the first `note_written` says who asked for
it (envelope source `student` for a note saved at the student's request, MEM-11), a `correction` on the note by the
student or the device adult makes it theirs (MEM-09), and the newest event touching the note is the version a review
is pinned to. Every query is ordered on stable keys, so the same tables always give the same answer (LRN-05).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from agent_core.config import Settings
from agent_core.store.moments import first_moment

HintUse = Literal["low", "medium", "high"]
# A KC's state (LRN-09); its student-facing label is the string catalogue's (G99), never a probability.
StateLabel = Literal["not_started", "learning", "ready_to_check", "mastered", "needs_review", "moved_on"]
NoteOrigin = Literal["reflector", "student_request", "corrected"]
OWNER_SOURCES = frozenset({"student", "adult"})


class ContentLabels(Protocol):
    """What the queries need from the installed packs to name things in the student's words."""

    def kc_statement(self, kc_id: str) -> str | None: ...
    def topic_of(self, kc_id: str) -> str | None: ...
    def kcs_in_topic(self, topic_id: str) -> Sequence[str]: ...
    def misconception_name(self, misconception_id: str) -> str | None: ...
    def prerequisites(self) -> Mapping[str, Sequence[str]]: ...


@dataclass(frozen=True)
class ProfileSummary:
    level: str
    subjects: tuple[str, ...]
    exam_distance: str | None  # "in N days" to the nearest exam from the session date, or "past"; never the date
    reading_level: str | None
    support_level: str | None
    solution_policy: str


@dataclass(frozen=True)
class KcLine:
    kc_id: str
    statement: str
    state: StateLabel
    hint_use: HintUse


@dataclass(frozen=True)
class MisconceptionLine:
    misconception_id: str
    name: str  # the pack's student-facing name (the id when no installed pack names it)
    status: Literal["active", "resolving"]
    evidence: int


@dataclass(frozen=True)
class NoteRecord:
    """A stored note as the readers see it. `origin` decides who may change it (MEM-09); `last_event_id` pins a
    review to the version shown."""

    note_id: str
    text: str
    status: Literal["active", "pending_review"]
    subject: str | None
    kc_ids: tuple[str, ...]
    created_at: int
    origin: NoteOrigin
    last_event_id: str
    updated_at: int = 0  # the last write of its text (an update keeps the note id and its created_at)
    source_event_ids: tuple[str, ...] = ()

    @property
    def day(self) -> str:
        """The date the note's current text was written (UTC; G95)."""
        return datetime.fromtimestamp((self.updated_at or self.created_at) / 1000, UTC).date().isoformat()


@dataclass(frozen=True)
class Preference:
    key: str
    value: str
    origin: Literal["override", "inferred"]
    confidence: float | None = None


@dataclass(frozen=True)
class DueReview:
    kc_id: str
    due_at: int


@dataclass(frozen=True)
class TranscriptLine:
    turn_ref: int  # the turn's position in the session, 1-based
    source: Literal["student", "tutor", "board"]
    text: str


# --- profile -----------------------------------------------------------------------------------------------------


def _setting(conn: sqlite3.Connection, name: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (name,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def memory_saving(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Whether the student has memory saving on (MEM-06; a student preference, on by default)."""
    stored = _setting(conn, "memory_saving")
    return bool(stored) if isinstance(stored, bool) else bool(settings.memory_saving)


def _exam_distance(dates: Iterable[str], today: str | None) -> str | None:
    if today is None:
        return None
    try:
        now = date.fromisoformat(today)
    except ValueError:
        return None
    parsed = []
    for text in dates:
        try:
            parsed.append(date.fromisoformat(text))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return None
    ahead = sorted(d for d in parsed if d >= now)
    return f"in {(ahead[0] - now).days} days" if ahead else "past"


def profile_summary(
    conn: sqlite3.Connection, *, today: str | None = None, settings: Settings | None = None
) -> ProfileSummary:
    """Level, subjects, exam distance from `today` (the session date), reading and support level, and the adult's
    answer policy (tier 6, never cut). An empty profile reads as blanks rather than failing."""
    policy = "on_request"
    row = conn.execute("SELECT level, subjects, exam_date, reading_level, support_level FROM profile").fetchone()
    if row is None:
        return ProfileSummary("", (), None, None, None, str(policy))
    subjects: list[str] = []
    exam_dates: list[str] = [row[2]] if row[2] else []
    for entry in json.loads(row[1] or "[]"):
        if isinstance(entry, str):
            subjects.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("subject"), str):
            subjects.append(entry["subject"])
            if isinstance(entry.get("exam_date"), str):
                exam_dates.append(entry["exam_date"])
    return ProfileSummary(str(row[0]), tuple(subjects), _exam_distance(exam_dates, today), row[3], row[4], str(policy))


# --- knowledge components and misconceptions -------------------------------------------------------------------------


def hint_use(hints: int, attempts: int, settings: Settings) -> HintUse:
    """How much the student leans on hints for a KC: hints per closed attempt against the two thresholds."""
    if attempts <= 0:
        return "low"
    share = hints / attempts
    if share >= settings.snapshot_hint_use_high:
        return "high"
    return "medium" if share >= settings.snapshot_hint_use_medium else "low"


def _kc_rows(conn: sqlite3.Connection) -> dict[str, tuple[str, int, int, int]]:
    """Per KC: state, hints, closed attempts, and when the student last attempted it directly (a neighbour's
    evidence moves the mastery row's own time, so the attempts table is the measure of "recently practised")."""
    practised = dict(
        conn.execute(
            "SELECT json_extract(value, '$.kc_id'), max(occurred_at) FROM attempts, json_each(attempts.kc_ids)"
            " GROUP BY 1"
        ).fetchall()
    )
    rows = conn.execute("SELECT kc_id, status, hints_used, attempts FROM kc_mastery")
    return {r[0]: (r[1], r[2], r[3], int(practised.get(r[0]) or 0)) for r in rows}


def kc_line(kc_id: str, row: tuple[str, int, int, int] | None, content: ContentLabels, settings: Settings) -> KcLine:
    statement = content.kc_statement(kc_id) or kc_id
    if row is None:
        return KcLine(kc_id, statement, "not_started", "low")
    return KcLine(kc_id, statement, cast(StateLabel, row[0]), hint_use(row[1], row[2], settings))


def top_kcs(
    conn: sqlite3.Connection,
    content: ContentLabels,
    topic_id: str | None,
    n: int,
    settings: Settings,
    *,
    focus: Sequence[str] = (),
) -> list[KcLine]:
    """The n most relevant KCs of the topic with state label and hint-use band (`snapshot_hint_use_*`).

    Relevance, in order: the KCs of the current problem (`focus`), then the topic's KCs the student has practised,
    most recently seen first, then the rest of the topic's KCs (not started, even where a neighbour's evidence has
    reached them) in id order. Without a topic, every KC with a row is a candidate."""
    if n <= 0:
        return []
    rows = _kc_rows(conn)
    pool = list(content.kcs_in_topic(topic_id)) if topic_id is not None else list(rows)
    in_pool = set(pool)
    chosen: list[str] = [kc for kc in dict.fromkeys(focus) if topic_id is None or kc in in_pool]
    seen = set(chosen)
    practised = {kc for kc, row in rows.items() if row[2] > 0}
    with_evidence = sorted((kc for kc in pool if kc in practised and kc not in seen), key=lambda k: (-rows[k][3], k))
    fresh = sorted(kc for kc in pool if kc not in practised and kc not in seen)
    chosen += with_evidence + fresh
    return [kc_line(kc, rows.get(kc), content, settings) for kc in chosen[:n]]


def all_kcs(conn: sqlite3.Connection, content: ContentLabels, settings: Settings) -> list[KcLine]:
    """Every KC with evidence, in id order (the knows page groups them by state)."""
    rows = _kc_rows(conn)
    return [kc_line(kc, rows[kc], content, settings) for kc in sorted(rows)]


def active_misconceptions(
    conn: sqlite3.Connection, content: ContentLabels, topic_id: str | None, n: int
) -> list[MisconceptionLine]:
    """Active and resolving misconceptions of the topic, with evidence counts and student-facing names: active ones
    first, then by evidence, then most recently seen."""
    if n <= 0:
        return []
    rows = conn.execute(
        "SELECT misconception_id, kc_id, status, evidence_count FROM learner_misconceptions"
        " WHERE status IN ('active', 'resolving')"
        " ORDER BY status = 'resolving', evidence_count DESC, last_seen_at DESC, misconception_id"
    ).fetchall()
    out: list[MisconceptionLine] = []
    for mc_id, kc_id, status, evidence in rows:
        if topic_id is not None and content.topic_of(kc_id) != topic_id:
            continue
        name = content.misconception_name(mc_id) or mc_id
        out.append(MisconceptionLine(mc_id, name, status, int(evidence)))
    return out[:n]


def weak_prerequisites(conn: sqlite3.Connection, content: ContentLabels, kc_id: str, settings: Settings) -> list[str]:
    """Prerequisite KCs of `kc_id` below the mastery threshold (the engine's detour decision): those with evidence
    that are not confirmed and whose probability is under `mastery_ready_p`. A prerequisite never practised has no
    evidence either way and is not called weak."""
    needs = list(dict.fromkeys(content.prerequisites().get(kc_id, ())))
    out = []
    for need in needs:
        row = conn.execute("SELECT status, p_mastery, attempts FROM kc_mastery WHERE kc_id = ?", (need,)).fetchone()
        if row is not None and row[2] > 0 and row[0] != "mastered" and row[1] < settings.mastery_ready_p:
            out.append(need)
    return out


# --- notes -------------------------------------------------------------------------------------------------------


@dataclass
class NoteHistory:
    """What the event log says about one note: who first wrote it, whether its owner corrected it, its newest
    event, and the review links that point at it."""

    first_source: str | None = None
    corrected: bool = False
    last_seq: int = 0
    last_event_id: str = ""
    events: list[tuple[int, str, str, str, dict[str, Any]]] = field(default_factory=list)  # seq, id, type, source, p


def note_histories(conn: sqlite3.Connection, note_ids: Collection[str] | None = None) -> dict[str, NoteHistory]:
    """The note events of the log grouped by note: writes, deletions and corrections aimed at a note (MEM-09)."""
    out: dict[str, NoteHistory] = {}
    rows = conn.execute(
        "SELECT seq, event_id, type, source, payload FROM events WHERE erased_at IS NULL"
        " AND (type IN ('note_written', 'note_deleted')"
        " OR (type = 'correction' AND json_extract(payload, '$.target') = 'note')) ORDER BY seq"
    )
    for seq, event_id, type_, source, raw in rows:
        payload = json.loads(raw)
        note_id = payload.get("note_id") if type_ != "correction" else payload.get("target_id")
        if not isinstance(note_id, str) or (note_ids is not None and note_id not in note_ids):
            continue
        history = out.setdefault(note_id, NoteHistory())
        if type_ == "note_written" and history.first_source is None:
            history.first_source = source
        if type_ == "correction" and source in OWNER_SOURCES:
            history.corrected = True
        history.last_seq, history.last_event_id = seq, event_id
        history.events.append((seq, event_id, type_, source, payload))
    return out


def origin_of(history: NoteHistory | None) -> NoteOrigin:
    if history is not None and history.corrected:
        return "corrected"
    if history is not None and history.first_source == "student":
        return "student_request"
    return "reflector"


def last_event_id(conn: sqlite3.Connection, note_id: str) -> str | None:
    """The newest event touching a note: the version a review item is pinned to (MEM-09)."""
    history = note_histories(conn, {note_id}).get(note_id)
    return history.last_event_id if history is not None else None


_NOTE_COLUMNS = "note_id, text, status, subject, kc_ids, created_at, updated_at, source_event_ids"


def _records(conn: sqlite3.Connection, rows: Sequence[Sequence[Any]]) -> list[NoteRecord]:
    histories = note_histories(conn, {r[0] for r in rows})
    out = []
    for note_id, text, status, subject, kc_ids, created_at, updated_at, sources in rows:
        history = histories.get(note_id)
        out.append(
            NoteRecord(
                note_id=note_id,
                text=text,
                status=status,
                subject=subject,
                kc_ids=tuple(json.loads(kc_ids)) if kc_ids else (),
                created_at=int(created_at),
                origin=origin_of(history),
                last_event_id=history.last_event_id if history is not None else "",
                updated_at=int(updated_at),
                source_event_ids=tuple(json.loads(sources)),
            )
        )
    return out


def notes_by_id(conn: sqlite3.Connection, note_ids: Collection[str]) -> dict[str, NoteRecord]:
    """Active and pending notes by id (deleted notes are gone)."""
    if not note_ids:
        return {}
    marks = ", ".join("?" for _ in note_ids)
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM notes WHERE status <> 'deleted' AND note_id IN ({marks})",  # noqa: S608
        tuple(note_ids),
    ).fetchall()
    return {r.note_id: r for r in _records(conn, rows)}


def _subject_clause(subject: str | None) -> tuple[str, tuple[Any, ...]]:
    # A note with no subject (from onboarding, say) belongs to every subject.
    return (" AND (subject = ? OR subject IS NULL)", (subject,)) if subject is not None else ("", ())


def recent_notes(conn: sqlite3.Connection, subject: str | None, n: int) -> list[NoteRecord]:
    """The n newest active notes of the subject (pending notes never reach a prompt, MEM-09)."""
    if n <= 0:
        return []
    return subject_notes(conn, subject)[:n]


def subject_notes(conn: sqlite3.Connection, subject: str | None) -> list[NoteRecord]:
    """Every active note of the subject, newest first: the source of index mode (MEM-13)."""
    clause, params = _subject_clause(subject)
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM notes WHERE status = 'active'{clause}"  # noqa: S608 - fixed clause
        " ORDER BY updated_at DESC, note_id DESC",
        params,
    ).fetchall()
    return _records(conn, rows)


def stored_notes(
    conn: sqlite3.Connection, statuses: Collection[str] = ("active", "pending_review")
) -> list[NoteRecord]:
    """Notes in the given statuses across subjects, newest first (bounds, the knows page, the snapshot)."""
    marks = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM notes WHERE status IN ({marks})"  # noqa: S608 - placeholders only
        " ORDER BY updated_at DESC, note_id DESC",
        tuple(statuses),
    ).fetchall()
    return _records(conn, rows)


# --- review items (MEM-09) ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewItem:
    """A change the Reflector may not make on its own, waiting for the student or the device adult.

    `replace`: a pending note that would supersede `target` (a contradiction, or an update of a note the student
    owns); its id is the pending note's id. `drop`: a proposal to remove a note the student owns; its id is the
    proposal's event id and it has no pending note. A resolution must quote both versions shown
    (`pending_event_id`, `target_event_id`) and is refused if either has moved since."""

    review_id: str
    kind: Literal["replace", "drop"]
    pending: NoteRecord | None
    target: NoteRecord | None
    pending_event_id: str
    target_event_id: str | None


def review_items(conn: sqlite3.Connection) -> list[ReviewItem]:
    """Every open review item, oldest first. A replace item stays open while its note is pending; a drop item while
    its note exists and neither its owner has answered nor a newer drop proposal for the same note replaced it."""
    histories = note_histories(conn)
    live = {n.note_id: n for n in stored_notes(conn)}
    items: list[tuple[int, ReviewItem]] = []
    linked: set[str] = set()
    for target_id, history in histories.items():
        drop: tuple[int, str] | None = None
        for seq, event_id, type_, source, payload in history.events:
            if type_ == "correction" and source in OWNER_SOURCES:
                drop = None  # the owner answered every earlier proposal for this note
            if type_ != "correction" or source != "reflector" or payload.get("action") != "dispute":
                continue
            raw_value = payload.get("value")
            value: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
            pending_id = value.get("pending_note_id")
            if isinstance(pending_id, str):
                linked.add(pending_id)
                pending = live.get(pending_id)
                if pending is not None and pending.status == "pending_review":
                    target = live.get(target_id)
                    items.append((seq, ReviewItem(pending_id, "replace", pending, target, pending.last_event_id,
                                                  target.last_event_id if target else None)))  # fmt: skip
            elif value.get("proposal") == "drop":
                drop = (seq, event_id)
        target = live.get(target_id)
        if drop is not None and target is not None and target.status == "active":
            items.append((drop[0], ReviewItem(drop[1], "drop", None, target, drop[1], target.last_event_id)))
    for note in live.values():  # a pending note with no link (written by an older build) is still reviewable
        if note.status == "pending_review" and note.note_id not in linked:
            items.append((histories[note.note_id].last_seq if note.note_id in histories else 0,
                          ReviewItem(note.note_id, "replace", note, None, note.last_event_id, None)))  # fmt: skip
    items.sort(key=lambda item: (item[0], item[1].review_id))
    return [item for _, item in items]


# --- preferences, reviews, the transcript ----------------------------------------------------------------------------


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def preferences(conn: sqlite3.Connection) -> list[Preference]:
    """Overrides first, then inferences, most confident first."""
    overrides: list[Preference] = []
    inferred: list[Preference] = []
    for key, raw, confidence in conn.execute("SELECT key, value, confidence FROM preferences ORDER BY key"):
        value = json.loads(raw)
        if isinstance(value, dict) and "override" in value:
            overrides.append(Preference(key, _shown(value["override"]), "override"))
        elif isinstance(value, dict) and "inferred" in value:
            inferred.append(Preference(key, _shown(value["inferred"]), "inferred", float(confidence)))
    inferred.sort(key=lambda p: (-(p.confidence or 0.0), p.key))
    return overrides + inferred


def due_reviews(conn: sqlite3.Connection, date: str) -> list[DueReview]:
    """Reviews due on or before `date` (YYYY-MM-DD, through the end of that UTC day), for the study planner."""
    day = datetime.fromisoformat(date).replace(tzinfo=UTC) + timedelta(days=1)
    end = int(day.timestamp() * 1000)
    rows = conn.execute("SELECT kc_id, due_at FROM review_cards WHERE due_at < ? ORDER BY due_at, kc_id", (end,))
    return [DueReview(r[0], int(r[1])) for r in rows]


def session_history(conn: sqlite3.Connection, session_id: str) -> list[TranscriptLine]:
    """The current session's stored transcript: student text, tutor text and board LaTeX (MEM-18), in order. A board
    step belongs to the turn of the part that drew it, or else to the last turn started before it."""
    sid = session_id.removeprefix("ses_")
    from agent_core.memory.visibility import cutoff

    after = cutoff(conn)
    turns = conn.execute(
        "SELECT turn_id, idx, speaker, student_text, started_at FROM turns"
        " WHERE session_id = ? AND started_at > ? ORDER BY idx",
        (sid, after),
    ).fetchall()
    ref_of = {turn_id: idx + 1 for turn_id, idx, *_ in turns}
    out: list[tuple[int, int, TranscriptLine]] = []
    for turn_id, idx, speaker, student_text, _ in turns:
        if speaker == "student" and student_text:
            out.append((idx + 1, 0, TranscriptLine(idx + 1, "student", student_text)))
        elif speaker == "tutor":
            parts = conn.execute(
                "SELECT idx, content FROM parts WHERE turn_id = ? AND kind = 'text' AND status <> 'retracted'"
                " ORDER BY idx",
                (turn_id,),
            )
            for part_idx, content in parts:
                text = json.loads(content).get("text")
                if isinstance(text, str) and text:
                    out.append((idx + 1, 1 + part_idx, TranscriptLine(idx + 1, "tutor", text)))
    starts = [(started, idx + 1) for _, idx, _, _, started in turns]
    steps = conn.execute(
        "SELECT s.latex, p.turn_id, s.created_at, s.idx FROM board_steps s LEFT JOIN parts p ON p.part_id = s.part_id"
        " WHERE s.session_id = ? AND s.created_at > ? ORDER BY s.created_at, s.idx, s.step_id",
        (sid, after),
    )
    for latex, turn_id, created_at, step_idx in steps:
        ref = ref_of.get(turn_id) if turn_id is not None else None
        if ref is None:
            earlier = [r for started, r in starts if started <= created_at]
            ref = earlier[-1] if earlier else 1
        out.append((ref, 10_000 + step_idx, TranscriptLine(ref, "board", latex)))
    out.sort(key=lambda item: (item[0], item[1]))
    return [line for _, _, line in out]


# --- Study activity (SPEC §19.3.2, W8): what the learner did in Study, as the knows view and Progress show it ---------

STUDY_SHOWN = ("quiz_item", "card_review", "aid_made")


def recent_study_activity(conn: sqlite3.Connection, content: ContentLabels, n: int = 8) -> list[dict[str, Any]]:
    """The newest `n` Study activities in the knows view's words: what kind, the pack's statement of its first topic
    tag (never a title or the learner's text), the result word, the aid's kind and size, and the UTC day."""
    rows = conn.execute(
        "SELECT payload, occurred_at FROM events WHERE type = 'study_activity' AND erased_at IS NULL"
        " AND json_extract(payload, '$.activity') IN ('quiz_item', 'card_review', 'aid_made')"
        " ORDER BY occurred_at DESC, seq DESC LIMIT ?",
        (max(n, 0),),
    ).fetchall()
    out = []
    for raw, at in rows:
        payload = json.loads(raw)
        tags = [t["kc_id"] for t in payload.get("kc_tags", [])[:1]]
        result = payload.get("outcome") if payload["activity"] == "quiz_item" else payload.get("rating")
        out.append({
            "activity": payload["activity"],
            "kc_id": tags[0] if tags else None,
            "statement": (content.kc_statement(tags[0]) or None) if tags else None,
            "result": result,
            "aid_kind": payload.get("aid_kind"),
            "count": payload.get("count"),
            "day": datetime.fromtimestamp(int(at) / 1000, UTC).date().isoformat(),
            "from_pack": from_research_pack(conn, str(payload.get("ref") or "")),
        })  # fmt: skip
    return out


def from_research_pack(conn: sqlite3.Connection, ref: str) -> bool:
    """The activity's material came from a study pack the research agent made (§19 fix, 2026-10-29): its aid's
    source is a research pack's. A ref is `att:<attempt>:<item>`, `card:<card>` or `aid:<aid>`."""
    kind, _, rest = ref.partition(":")
    try:
        if kind == "att":
            row = conn.execute("SELECT q.aid_id FROM study_attempts a JOIN study_quizzes q ON q.quiz_id = a.quiz_id"
                               " WHERE a.attempt_id = ?", (rest.rsplit(":", 1)[0],)).fetchone()  # fmt: skip
        elif kind == "card":
            row = conn.execute("SELECT d.aid_id FROM study_flashcards f JOIN study_decks d ON d.deck_id = f.deck_id"
                               " WHERE f.card_id = ?", (rest,)).fetchone()  # fmt: skip
        elif kind == "aid":
            row = (rest,)
        else:
            return False
        if row is None:
            return False
        found = conn.execute(
            "SELECT 1 FROM study_aid_sources s JOIN study_research r ON json_extract(r.body, '$.source_id') ="
            " s.source_id WHERE s.aid_id = ? LIMIT 1",
            (row[0],),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return found is not None


def studied_kcs(conn: sqlite3.Connection, since_ms: int) -> list[str]:
    """The ideas Study practice touched since `since_ms` (quiz questions answered and cards reviewed, through their
    topic tags), newest first. Activity only: nothing here changes mastery (JM-D26)."""
    rows = conn.execute(
        "SELECT json_extract(payload, '$.kc_tags') FROM events WHERE type = 'study_activity' AND erased_at IS NULL"
        " AND occurred_at >= ? AND json_extract(payload, '$.activity') IN ('quiz_item', 'card_review')"
        " AND coalesce(json_extract(payload, '$.outcome'), '') <> 'skipped' ORDER BY occurred_at DESC, seq DESC",
        (since_ms,),
    ).fetchall()
    found: dict[str, None] = {}
    for (raw,) in rows:
        try:
            tags = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for tag in tags if isinstance(tags, list) else []:
            kc = tag.get("kc_id") if isinstance(tag, dict) else tag  # stored as {kc_id, weight}
            if isinstance(kc, str):
                found[kc] = None
    return list(found)


def study_activity_counts(conn: sqlite3.Connection, since_ms: int) -> dict[str, int]:
    """Study activity since `since_ms` for Progress's "Study activity" line: quiz questions answered (a skipped one
    is not), how many were right, cards reviewed and aids made."""
    rows = conn.execute(
        "SELECT json_extract(payload, '$.activity'), json_extract(payload, '$.outcome'), count(*) FROM events"
        " WHERE type = 'study_activity' AND erased_at IS NULL AND occurred_at >= ? GROUP BY 1, 2",
        (since_ms,),
    ).fetchall()
    quiz = [(outcome, n) for kind, outcome, n in rows if kind == "quiz_item" and outcome != "skipped"]
    return {
        "quiz_items": sum(n for _, n in quiz),
        "quiz_correct": sum(n for outcome, n in quiz if outcome == "correct"),
        "cards_reviewed": sum(n for kind, _, n in rows if kind == "card_review"),
        "aids_made": sum(n for kind, _, n in rows if kind == "aid_made"),
    }


# --- the companion's fixed queries (SPEC §19.3.3, W8 phase 2) -------------------------------------------------------

STUDY_EXCERPTS_MAX = 6
STUDY_EXCERPT_CHARS = 400
STUDY_LABEL_CHARS = 80


@dataclass(frozen=True)
class StudyExcerpt:
    """One literal match in the learner's own materials: where it is (a source's revision or a session note) and the
    excerpt. The ids stay on the device's side of the tool: the model sees an integer ref."""

    kind: Literal["source", "note"]
    record_id: str
    revision: int
    label: str
    text: str
    # SPEC §20.3 (W9): where in the material the excerpt is (the chunk's locator; a time locator narrowed to the
    # cited line's moment), so the companion can offer to open the moment. None for a note.
    locator: dict[str, Any] | None = None


def moment_locator(locator: Any, text: str) -> dict[str, Any] | None:
    """A chunk's locator for an excerpt of it: a time locator starts at the excerpt's own line when the line carries
    its `[m:ss]` moment (SPEC §20.3), else the chunk's own span."""
    if not isinstance(locator, dict):
        return None
    if locator.get("kind") == "time":
        moment = first_moment(text)
        if moment is not None and int(locator.get("start_ms", 0)) <= moment < int(locator.get("end_ms", 0)):
            return {"kind": "time", "start_ms": moment, "end_ms": int(locator["end_ms"])}
    return dict(locator)


def study_excerpts(
    conn: sqlite3.Connection, segments: Sequence[str], n: int = STUDY_EXCERPTS_MAX
) -> list[StudyExcerpt]:
    """`study_search`'s fixed query: the literal pieces (`memory.recall._segments`, `*` the only wildcard, never a
    regular expression) found in order in one sentence of a ready, non-private source's current revision or of a
    ready, non-private session note; at most `n`, each at most 400 characters with its 80-character label."""
    from agent_core.memory.recall import _find, _window

    if not segments:
        return []
    out: list[StudyExcerpt] = []
    rows = conn.execute(
        "SELECT s.source_id, s.current_revision, s.body, c.body FROM study_sources s JOIN study_chunks c"
        " ON c.source_id = s.source_id AND c.revision = s.current_revision WHERE s.private = 0 ORDER BY s.source_id,"
        " c.ordinal"
    ).fetchall()
    for source_id, revision, source_body, chunk_body in rows:
        source, chunk = json.loads(source_body), json.loads(chunk_body)
        if source.get("state") not in ("ready", "partial"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(chunk.get("text", ""))):
            flat = " ".join(sentence.split())
            hit = _find(flat, segments)
            if hit is None:
                continue
            label = str(source.get("title", ""))[:STUDY_LABEL_CHARS]
            excerpt = _window(flat, hit[0], hit[1], STUDY_EXCERPT_CHARS)
            out.append(StudyExcerpt("source", str(source_id), int(revision), label, excerpt,
                                    moment_locator(chunk.get("locator"), flat)))  # fmt: skip
            if len(out) >= n:
                return out
    try:
        notes = conn.execute(
            "SELECT note_id, title, statements FROM session_notes WHERE state = 'ready' AND private = 0"
            " ORDER BY note_id"
        ).fetchall()
    except sqlite3.Error:
        notes = []
    for note_id, title, statements in notes:
        for statement in json.loads(statements):
            text = statement.get("text") if isinstance(statement, dict) else None
            if not isinstance(text, str):
                continue
            flat = " ".join(text.split())
            hit = _find(flat, segments)
            if hit is None:
                continue
            out.append(StudyExcerpt("note", str(note_id), 0, str(title or "")[:STUDY_LABEL_CHARS],
                                    _window(flat, hit[0], hit[1], STUDY_EXCERPT_CHARS)))  # fmt: skip
            if len(out) >= n:
                return out
    return out


@dataclass(frozen=True)
class ProgressOverview:
    """`progress_read`'s fixed query: bands (most recently practised first), cards due in both schedulers, the
    nearest exam's distance and the streak. Never a probability."""

    bands: tuple[tuple[str, str, str], ...]  # (kc_id, statement, band)
    learner_cards: int
    study_cards: int
    exam_distance_days: int | None
    streak_days: int


def progress_overview(conn: sqlite3.Connection, content: ContentLabels, settings: Settings, today: str,
                      end_of_today_ms: int, n: int = 12) -> ProgressOverview:  # fmt: skip
    from agent_core.memory.render import BANDS

    kcs = top_kcs(conn, content, None, n, settings)
    bands = tuple((k.kc_id, k.statement, BANDS.get(k.state, "learning")) for k in kcs[:n])
    learner_cards = len(due_reviews(conn, today))
    study = conn.execute(
        "SELECT count(*) FROM study_flashcards f JOIN study_decks d ON d.deck_id = f.deck_id JOIN study_revisions r"
        " ON r.source_id = d.source_id AND r.revision = d.revision WHERE f.due_at IS NOT NULL AND f.due_at < ?"
        " AND json_extract(f.body, '$.suspended') = 0",
        (end_of_today_ms,),
    ).fetchone()[0]
    summary = profile_summary(conn, today=today, settings=settings)
    distance = int(summary.exam_distance.split()[1]) if summary.exam_distance and summary.exam_distance.startswith(
        "in ") else None  # fmt: skip
    row = conn.execute("SELECT streak_days FROM engagement_state").fetchone()
    return ProgressOverview(bands, learner_cards, int(study or 0), distance, int(row[0]) if row else 0)
