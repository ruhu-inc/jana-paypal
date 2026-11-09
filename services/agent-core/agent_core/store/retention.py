"""The retention job: TABLE DAT-08a as data, applied to one profile's learner database, the device database and the
profile's attachment and spill folders (DAT-08, MEM-22).

`RULES` has one entry per row of the table, in the order the job applies them. A rule without a `purge` is never
touched by the job: its rows go only by profile deletion (DAT-09), uninstall (PLT-24) or the feature that owns them.
Every period is a configuration key (D53: fixed defaults, never a user setting). A row is removed when its expiry is
at or before `now_ms`, and never before. Purging a transcript keeps the session row, its title and summary, and every
event, so the projections derived from events do not change.

The runtime calls `run_retention` at app start and every `retention_job_interval_h` hours. It walks the rules in
the order of the registry of profile stores (`store.profile_stores`), the same registry profile deletion walks.
"""

from __future__ import annotations

import datetime
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_core.config import Settings
from agent_core.store.events import EventLog, StoredEvent
from agent_core.store.ids import new_ulid
from agent_core.store.profile_stores import retention_order
from agent_core.store.sqlite import transaction

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS

# Learner tables marked PROJECTION in the storage contract (DAT-05); `notes_fts` is the full-text index the contract
# keeps in step with `notes` by trigger, so it lives and goes with the projections.
PROJECTION_TABLES = (
    "kc_mastery",
    "review_cards",
    "learner_misconceptions",
    "attempts",
    "engagement_daily",
    "engagement_state",
    "subject_ability",
    "preferences",
    "notes",
    "study_plans",
)
NOTES_INDEX = "notes_fts"


@dataclass
class Job:
    """One run of the job: the open stores, the configuration and the simulated or real time."""

    learner: sqlite3.Connection
    device: sqlite3.Connection
    settings: Settings
    now_ms: int
    profile_dir: Path
    log: EventLog
    fold: Callable[[StoredEvent], None]
    prune_traces: Callable[[int], None] | None = None

    def cutoff(self, key: str, unit_ms: int) -> int:
        """Rows stamped at or before this moment have outlived the period held by configuration key `key`."""
        return self.now_ms - int(getattr(self.settings, key) * unit_ms)


Purge = Callable[[Job], int]


@dataclass(frozen=True)
class Rule:
    """One row of TABLE DAT-08a. `tables` are the tables the row governs (each table is governed by one rule);
    `acts_on` are other rules' tables it changes; `keys` are the configuration keys holding its periods."""

    row_class: str
    database: str  # "learner", "device", or "files" for data outside the databases
    tables: tuple[str, ...]
    purge: Purge | None = None
    keys: tuple[str, ...] = ()
    removed_by: str = ""  # for a rule without a purge: what removes its rows
    acts_on: tuple[str, ...] = field(default=())


# --- learner database --------------------------------------------------------------------------------------------


def _assessment_runs(job: Job) -> int:
    # A completed or failed run is finished at its last update; a stopped run's last update is its last activity.
    return job.learner.execute(
        "DELETE FROM assessment_runs WHERE state IN ('completed','failed','stopped') AND updated_at <= ?",
        (job.cutoff("retention_assessment_run_days", DAY_MS),),
    ).rowcount


def _transcripts(job: Job) -> int:
    ended = [
        row[0]
        for row in job.learner.execute(
            "SELECT session_id FROM sessions WHERE transcript_purged_at IS NULL AND ended_at IS NOT NULL"
            " AND ended_at <= ? ORDER BY session_id",
            (job.cutoff("retention_transcript_days", DAY_MS),),
        )
    ]
    for session_id in ended:
        # Steps first (they reference checks and parts), then checks, then parts with their turns.
        job.learner.execute("DELETE FROM board_steps WHERE session_id = ?", (session_id,))
        job.learner.execute("DELETE FROM checks WHERE session_id = ?", (session_id,))
        job.learner.execute(
            "DELETE FROM parts WHERE turn_id IN (SELECT turn_id FROM turns WHERE session_id = ?)", (session_id,)
        )
        job.learner.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        job.learner.execute(
            "UPDATE sessions SET transcript_purged_at = ? WHERE session_id = ?", (job.now_ms, session_id)
        )
    return len(ended)


def _saved_content(job: Job) -> int:
    # The earlier of the period or the source transcript's purge; the row is the whole record, including any
    # support-export permission stored in its content.
    count = 0
    cutoff = job.cutoff("retention_saved_content_days", DAY_MS)
    for table in ("saved_questions", "problem_reports"):
        count += job.learner.execute(
            f"DELETE FROM {table} WHERE expires_at <= ? OR created_at <= ?"  # noqa: S608 - fixed table names
            " OR session_id IN (SELECT session_id FROM sessions WHERE transcript_purged_at IS NOT NULL)",
            (job.now_ms, cutoff),
        ).rowcount
    return count


def _archive_authorizations(job: Job) -> int:
    # Expired, used, or issued under an earlier auth epoch (a lock or PIN reset raises the epoch).
    row = job.device.execute(
        "SELECT profile_auth_epoch FROM profiles_index WHERE profile_id = ?", (job.log.profile_id,)
    ).fetchone()
    epoch = int(row[0]) if row else None
    return job.learner.execute(
        "DELETE FROM archive_authorizations WHERE expires_at <= ? OR consumed_at <= ?"
        " OR (? IS NOT NULL AND profile_auth_epoch <> ?)",
        (job.now_ms, job.now_ms, epoch, epoch),
    ).rowcount


def _exam_answers(job: Job) -> int:
    # Graded answers lose their values with the transcript period counted from grading (the later of the answer's
    # last write, which sets its marks, and the submission); the marks stay for the life of the profile.
    return job.learner.execute(
        "UPDATE exam_answers SET answer = NULL WHERE answer IS NOT NULL AND marks_awarded IS NOT NULL"
        " AND max(updated_at, coalesce((SELECT submitted_at FROM exams WHERE exams.exam_id = exam_answers.exam_id),"
        " 0)) <= ?",
        (job.cutoff("retention_transcript_days", DAY_MS),),
    ).rowcount


def _learner_idempotency(job: Job) -> int:
    return job.learner.execute(
        "DELETE FROM idempotency_responses WHERE created_at <= ?", (job.cutoff("api_idempotency_hours", HOUR_MS),)
    ).rowcount


def _inside(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def _attachments(job: Job) -> int:
    root = job.profile_dir / "attachments"
    rows = job.learner.execute(
        "SELECT attachment_id, file_path FROM attachments WHERE delete_after <= ? OR created_at <= ?"
        " OR session_id IN (SELECT session_id FROM sessions WHERE transcript_purged_at IS NOT NULL)",
        (job.now_ms, job.cutoff("retention_attachment_days", DAY_MS)),
    ).fetchall()
    for attachment_id, file_path in rows:
        path = _inside(root, file_path)
        if path is not None:
            path.unlink(missing_ok=True)  # the file first: a row never outlives knowledge of its file
        job.learner.execute("DELETE FROM attachments WHERE attachment_id = ?", (attachment_id,))
    return len(rows)


def _checkpoints(job: Job) -> int:
    ended = job.learner.execute(
        "DELETE FROM checkpoints WHERE session_id IN (SELECT session_id FROM sessions WHERE ended_at IS NOT NULL"
        " AND ended_at <= ?)",
        (job.cutoff("retention_checkpoint_days", DAY_MS),),
    ).rowcount
    beyond = job.learner.execute(
        "DELETE FROM checkpoints WHERE checkpoint_id IN (SELECT checkpoint_id FROM (SELECT checkpoint_id,"
        " row_number() OVER (PARTITION BY session_id ORDER BY created_at DESC, checkpoint_id DESC) AS n"
        " FROM checkpoints) WHERE n > ?)",
        (job.settings.retention_checkpoints_per_session,),
    ).rowcount
    return ended + beyond


def _spill(job: Job) -> int:
    # Each session's spill area is `spill/<session_id>`; it goes when the session ends, and at the latest the hard
    # maximum after the session started (or after the directory was last written, for a session with no row).
    root = job.profile_dir / "spill"
    if not root.is_dir():
        return 0
    cutoff = job.cutoff("retention_spill_max_h", HOUR_MS)
    removed = 0
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        row = job.learner.execute(
            "SELECT started_at, ended_at FROM sessions WHERE session_id = ?", (directory.name,)
        ).fetchone()
        if row is not None:
            expired = (row[1] is not None and row[1] <= job.now_ms) or row[0] <= cutoff
        else:
            expired = int(directory.stat().st_mtime * 1000) <= cutoff
        if expired:
            shutil.rmtree(directory, ignore_errors=True)
            removed += 1
    return removed


def _traces(job: Job) -> int:
    if job.prune_traces is not None:
        job.prune_traces(job.now_ms)  # age and size limits, oldest first (OBS-08)
    return 0


def _notes(job: Job) -> int:
    period = int(job.settings.retention_note_days * DAY_MS)
    expired = [
        row[0]
        for row in job.learner.execute(
            "SELECT note_id FROM notes WHERE status <> 'deleted' AND CASE WHEN expires_at IS NULL THEN created_at + ?"
            " ELSE min(expires_at, created_at + ?) END <= ? ORDER BY note_id",
            (period, period, job.now_ms),
        )
    ]
    for note_id in expired:
        # The deletion is an event the notes projection folds (status deleted, text cleared); the text the note was
        # written with is erased from the log, as for any deleted note (LRN-37).
        event = job.log.append(
            "note_deleted", {"note_id": note_id, "actor_role": "system_expiry"}, source="engine",
            occurred_at=job.now_ms,
        )  # fmt: skip
        job.fold(event)
        written = job.learner.execute(
            "SELECT event_id FROM events WHERE type = 'note_written' AND erased_at IS NULL"
            " AND json_extract(payload, '$.note_id') = ?",
            (note_id,),
        ).fetchall()
        for (event_id,) in written:
            job.log.erase(event_id, job.now_ms)
    return len(expired)


def _note_recalls(job: Job) -> int:
    """A note's recall rows go with the note (a forget deletes them at once) and at `retention_note_days` from their
    day, as notes do (SPEC §19.3.5)."""
    cutoff = datetime.datetime.fromtimestamp(job.cutoff("retention_note_days", DAY_MS) / 1000, datetime.UTC)
    return job.learner.execute(
        "DELETE FROM note_recalls WHERE day < ?"
        " OR note_id NOT IN (SELECT note_id FROM notes WHERE status <> 'deleted')",
        (cutoff.date().isoformat(),),
    ).rowcount


def _older(table: str, column: str, key: str, unit_ms: int, database: str = "learner") -> Purge:
    def purge(job: Job) -> int:
        conn = job.learner if database == "learner" else job.device
        return conn.execute(
            f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {column} <= ?",  # noqa: S608 - fixed names
            (job.cutoff(key, unit_ms),),
        ).rowcount

    return purge


# A re-mark's notice to the student waits in the audit log until shown (ASM-23, CUR-36; `learner.remark`): it is
# kept past `retention_audit_days` until its "shown" row exists, so a profile not opened within the period still
# hears about its corrected marking; once shown, both rows age out as any audit row. The row holds only a count.
PENDING_NOTICE = "remark.notice"
NOTICE_SHOWN = "remark.notice_shown"


def _audit_log(job: Job) -> int:
    return job.learner.execute(
        "DELETE FROM audit_log WHERE at IS NOT NULL AND at <= ?"
        " AND audit_id NOT IN (SELECT audit_id FROM audit_log WHERE action = 'memory_context_changed'"
        " ORDER BY rowid DESC LIMIT 1) AND NOT (action = ? AND NOT EXISTS"
        " (SELECT 1 FROM audit_log shown WHERE shown.action = ? AND shown.target = audit_log.audit_id))",
        (job.cutoff("retention_audit_days", DAY_MS), PENDING_NOTICE, NOTICE_SHOWN),
    ).rowcount


# --- device database ---------------------------------------------------------------------------------------------


def _outbox(job: Job) -> int:
    delivered = job.device.execute(
        "DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at <= ?", (job.now_ms,)
    ).rowcount
    expired = job.device.execute(
        "SELECT outbox_id, kind, profile_id FROM outbox WHERE delivered_at IS NULL AND (expires_at <= ?"
        " OR (kind = 'tombstone' AND created_at <= ?) OR (kind = 'telemetry' AND created_at <= ?))"
        " ORDER BY created_at, outbox_id",
        (
            job.now_ms,
            job.cutoff("retention_tombstone_days", DAY_MS),
            job.cutoff("retention_telemetry_days", DAY_MS),
        ),
    ).fetchall()
    for outbox_id, kind, profile_id in expired:
        if kind in ("tombstone", "consent_withdrawal"):
            job.device.execute(
                "INSERT INTO device_audit (audit_id, actor_role, action, profile_id, detail, at)"
                " VALUES (?, 'system', ?, ?, json_object('outbox_id', ?), ?)",
                (new_ulid(job.now_ms), f"{kind}.expired", profile_id, outbox_id, job.now_ms),
            )
        job.device.execute("DELETE FROM outbox WHERE outbox_id = ?", (outbox_id,))
    crash = job.device.execute(
        "DELETE FROM outbox WHERE outbox_id IN (SELECT outbox_id FROM (SELECT outbox_id, row_number() OVER"
        " (ORDER BY created_at DESC, outbox_id DESC) AS n FROM outbox WHERE kind = 'crash_report'"
        " AND delivered_at IS NULL) WHERE n > ?)",
        (job.settings.crash_reports_queued_max,),
    ).rowcount
    return delivered + len(expired) + crash


RULES: tuple[Rule, ...] = (
    # Learner database. Transcripts go before the rows that also expire "with the transcript".
    Rule(
        "life of the profile",
        "learner",
        ("events", "consents", "meta", "profile", "settings", "projection_state", "sessions", "lesson_positions"),
        removed_by="DAT-09",
    ),
    Rule("projections", "learner", (*PROJECTION_TABLES, NOTES_INDEX), removed_by="DAT-09"),
    Rule("exams", "learner", ("exams",), removed_by="DAT-09"),
    # ASM-27, LRN-44: imported past papers and exam dates stay until the student removes them or the profile goes.
    Rule("past papers", "learner", ("past_papers",), removed_by="student deletion or DAT-09"),
    Rule("exam dates", "learner", ("exam_dates",), removed_by="student deletion or DAT-09"),
    Rule("study notes", "learner", ("study_notes",), removed_by="student deletion or DAT-09"),
    # SPEC §19.4 (W8): the companion thread. An episode is a session's row and goes with it; its turns are purged at
    # `retention_transcript_days` like any session's (it ends daily). The rolling summary lasts while the profile does.
    Rule(
        "companion turn provenance", "learner", ("companion_turn_context",), removed_by="transcript cascade or DAT-09"
    ),
    Rule("companion episodes", "learner", ("companion_episodes",), removed_by="its session or DAT-09"),
    Rule(
        "companion summary", "learner", ("companion_summary",), removed_by="DAT-09; no free text, kept with the profile"
    ),
    # SPEC §19.3.4, §19.3.5 (W8 phase 3): operational, device-local rows (JM-D27).
    Rule(
        "check-ins",
        "learner",
        ("commitments",),
        _older("commitments", "resolved_at", "retention_commitment_days", DAY_MS),
        ("retention_commitment_days",),
    ),
    Rule("note recalls", "learner", ("note_recalls",), _note_recalls, ("retention_note_days",)),
    Rule(
        "source study workspace",
        "learner",
        (
            "study_sources",
            "study_revisions",
            "study_pending_revisions",
            "study_cloud_cleanup",
            "study_chunks",
            "study_jobs",
            "study_job_sources",
            "study_aids",
            "study_aid_sources",
            "study_decks",
            "study_flashcards",
            "study_reviews",
            "study_quizzes",
            "study_attempts",
            "study_aid_parents",
            "study_journeys",
            "study_quiz_submissions",
            "study_tombstones",
        ),
        removed_by="DAT-22 source erasure or DAT-09; tombstones last for the profile",
    ),
    Rule(
        "study concept maps of typed topics",
        "learner",
        ("study_concept_maps",),
        removed_by="DAT-09 profile deletion; the learner's own maps, no source material",
    ),
    # SPEC §20.3 (W9, JM-D34): a video file's frames as text, one row per still, never an image; they go with the
    # source revision they were read for.
    Rule(
        "video frames",
        "learner",
        ("study_frames",),
        removed_by="DAT-22 source erasure (the revision's cascade) or DAT-09",
    ),
    Rule(
        "study packs from the research agent",
        "learner",
        ("study_research",),
        removed_by="DAT-09 profile deletion; the researched pages are a Study source, erased with it (DAT-22)",
    ),
    Rule("support sharing confirmations", "learner", ("support_shares",), removed_by="session or profile deletion"),
    Rule("sync cursors", "learner", ("sync_cursors",), removed_by="DAT-19 (multi-device disabled or peer removed)"),
    Rule(
        "transcript",
        "learner",
        ("turns", "parts", "board_steps", "checks"),
        _transcripts,
        ("retention_transcript_days",),
        acts_on=("sessions",),
    ),
    Rule(
        "assessment runs",
        "learner",
        ("assessment_runs",),
        _assessment_runs,
        ("retention_assessment_run_days",),
    ),
    Rule(
        "saved questions and problem reports",
        "learner",
        ("saved_questions", "problem_reports"),
        _saved_content,
        ("retention_saved_content_days",),
    ),
    Rule(
        "archive authorizations",
        "learner",
        ("archive_authorizations",),
        _archive_authorizations,
        ("retention_archive_authorization_s",),
    ),
    Rule("exam answers", "learner", ("exam_answers",), _exam_answers, ("retention_transcript_days",)),
    Rule(
        "idempotency responses (learner)",
        "learner",
        ("idempotency_responses",),
        _learner_idempotency,
        ("api_idempotency_hours",),
    ),
    Rule(
        "captures",
        "learner",
        ("captures",),
        lambda job: (
            job.learner.execute(
                "DELETE FROM captures WHERE expires_at <= ? OR session_id IN "
                "(SELECT session_id FROM sessions WHERE transcript_purged_at IS NOT NULL)",
                (job.now_ms,),
            ).rowcount
        ),
        ("retention_attachment_days",),
    ),
    Rule(
        "editor_drafts",
        "learner",
        ("editor_drafts",),
        lambda job: (
            job.learner.execute(
                "DELETE FROM editor_drafts WHERE session_id IN "
                "(SELECT session_id FROM sessions WHERE transcript_purged_at IS NOT NULL)"
            ).rowcount
        ),
        ("retention_transcript_days",),
    ),
    Rule("attachments", "learner", ("attachments",), _attachments, ("retention_attachment_days",)),
    Rule(
        "checkpoints",
        "learner",
        ("checkpoints",),
        _checkpoints,
        ("retention_checkpoints_per_session", "retention_checkpoint_days"),
    ),
    Rule("spill files", "files", (), _spill, ("retention_spill_max_h",)),
    Rule("trace store", "files", (), _traces, ("trace_retention_days", "trace_store_max_bytes")),
    Rule("Reflector notes", "learner", (), _notes, ("retention_note_days",), acts_on=("notes", "events")),
    Rule(
        "digests",
        "learner",
        ("digests",),
        _older("digests", "generated_at", "retention_digest_weeks", WEEK_MS),
        ("retention_digest_weeks",),
    ),
    Rule(
        "alerts",
        "learner",
        ("alerts",),
        _older("alerts", "dismissed_at", "retention_alert_days", DAY_MS),
        ("retention_alert_days",),
    ),
    Rule(
        "audit log",
        "learner",
        ("audit_log",),
        _audit_log,
        ("retention_audit_days",),
    ),
    Rule(
        "exports",
        "learner",
        ("exports",),
        _older("exports", "created_at", "retention_audit_days", DAY_MS),
        ("retention_audit_days",),
    ),
    # Device database.
    Rule(
        "life of the installation",
        "device",
        ("device", "app_settings", "migrations", "online_features"),
        removed_by="PLT-24",
    ),
    Rule("profiles index", "device", ("profiles_index",), removed_by="DAT-09"),
    Rule(
        "egress ledger",
        "device",
        ("egress_ledger",),
        _older("egress_ledger", "at", "retention_egress_days", DAY_MS, "device"),
        ("retention_egress_days",),
    ),
    Rule(
        "idempotency responses (device)",
        "device",
        ("idempotency_responses",),
        _older("idempotency_responses", "created_at", "api_idempotency_hours", HOUR_MS, "device"),
        ("api_idempotency_hours",),
    ),
    Rule(
        "outbox",
        "device",
        ("outbox",),
        _outbox,
        ("retention_tombstone_days", "retention_telemetry_days", "crash_reports_queued_max"),
        acts_on=("device_audit",),
    ),
    Rule(
        "device audit",
        "device",
        ("device_audit",),
        _older("device_audit", "at", "retention_device_audit_days", DAY_MS, "device"),
        ("retention_device_audit_days",),
    ),
    Rule(
        "device consents",
        "device",
        ("device_consents",),
        _older("device_consents", "withdrawn_at", "retention_withdrawn_consent_days", DAY_MS, "device"),
        ("retention_withdrawn_consent_days",),
    ),
)


def ordered_rules() -> list[Rule]:
    """The rules in the order the registry of profile stores walks them (`store.profile_stores`). A rule no store
    claims still runs, last: retention is never skipped (the erasure test fails for it)."""
    by_class = {rule.row_class: rule for rule in RULES}
    walked = [by_class[row_class] for row_class in retention_order()]
    return walked + [rule for rule in RULES if rule not in walked]


def run_retention(
    learner: sqlite3.Connection,
    device: sqlite3.Connection,
    settings: Settings,
    *,
    now_ms: int,
    profile_dir: Path,
    log: EventLog,
    fold: Callable[[StoredEvent], None],
    prune_traces: Callable[[int], None] | None = None,
) -> dict[str, int]:
    """Apply TABLE DAT-08a at `now_ms`. `log` is the profile's event log on `learner` and `fold` folds a new event
    into the projections (the learner's projector); `prune_traces` applies the trace store's limits. Each rule runs
    in its own transaction. Returns the number of rows, files or folders each rule removed or cleared."""
    job = Job(learner, device, settings, now_ms, profile_dir, log, fold, prune_traces)
    counts: dict[str, int] = {}
    for rule in ordered_rules():
        if rule.purge is None:
            continue
        if rule.database == "device":
            with transaction(device):
                counts[rule.row_class] = rule.purge(job)
        else:
            with transaction(learner):
                counts[rule.row_class] = rule.purge(job)
    return counts
