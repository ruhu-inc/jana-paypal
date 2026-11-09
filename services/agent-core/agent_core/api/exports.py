"""Local exports require the open profile's token. Exact, single-use conversation authorizations expire after
five minutes or a profile close/switch. Archive import creates a separate profile (API-28, API-30, DAT-10, D87)."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from agent_core.api.core import CURRENT_PROFILE, CoreService, require
from agent_core.api.errors import ApiError
from agent_core.api.profiles import Mutating
from agent_core.engine.events import new_id
from agent_core.engine.research import export_rows
from agent_core.store.consents import PROFILE_SCOPES, active
from agent_core.store.events import EventLog, iso_ms
from agent_core.store.exports import (
    EXTENSIONS,
    TRANSCRIPT_SQL,
    ExportError,
    build_profile_archive,
    open_with_passphrase,
    read_profile_archive,
    seal_to_recipient,
    seal_with_passphrase,
)
from agent_core.store.ids import new_ulid
from agent_core.store.profiles import ProfileDirectory, ProfileError, internal_id, require_supported_age
from agent_core.store.settings import PREFERENCES, tutor_avatar
from agent_core.store.sqlite import transaction


def add_routes(app: FastAPI, core: CoreService, mutating: Mutating) -> None:
    def directory() -> ProfileDirectory:
        if not isinstance(core.directory, ProfileDirectory):
            raise ApiError("not_found", "profiles are not available")
        return core.directory

    def guarded(work: Callable[[], Any]) -> Any:
        try:
            return work()
        except (ProfileError, ExportError) as error:
            raise ApiError(error.code, error.message) from None

    def job(kind: str, result_ref: str | None, error: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = new_id("job")
        core.jobs[job_id] = {"job_id": job_id, "kind": kind, "state": "failed" if error else "done", "progress": None,
                             "result_ref": result_ref, "error": error}  # fmt: skip
        return {"job_id": job_id, "queued_until_online": False}

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        found = core.jobs.get(job_id)
        if found is None:
            raise ApiError("not_found", "no such job")
        return dict(found)

    # --- the student's authorization for conversations and photos (API-30) ------------------------------------------

    @app.post("/v1/profiles/{profile_id}/archive-authorizations")
    async def authorize(profile_id: str, request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            if CURRENT_PROFILE.get() != profile_id:
                raise ApiError("not_found", "no such profile")
            asked = require("ArchiveAuthorizationRequest", body)
            d = directory()
            now = core.clock_ms()
            lifetime = int(core.settings.retention_archive_authorization_s)  # five minutes (API-30, DAT-08)
            authorization_id = new_ulid(now)
            with d.open_learner(profile_id) as conn, transaction(conn):
                known = {r[0] for r in conn.execute("SELECT session_id FROM sessions")}
                chosen = [s.removeprefix("ses_") for s in asked["session_ids"]]  # storage keeps bare ids
                if set(chosen) - known:
                    raise ApiError("not_found", "no such session")
                conn.execute(
                    "INSERT INTO archive_authorizations (authorization_id, session_ids, include_attachments,"
                    " profile_auth_epoch, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (authorization_id, json.dumps(chosen), int(asked["include_attachments"]),
                     d.epoch(profile_id), now + lifetime * 1000),
                )  # fmt: skip
            return 200, {"authorization_id": authorization_id, "expires_in_s": lifetime}

        return await mutating(request, run)

    def consume(conn: Any, profile_id: str, authorization_id: str) -> tuple[list[str], bool]:
        """Single use, unexpired, for this profile's current auth epoch; consumed with the job (API-30)."""
        row = conn.execute(
            "SELECT session_ids, include_attachments, profile_auth_epoch, expires_at, consumed_at FROM"
            " archive_authorizations WHERE authorization_id = ?",
            (authorization_id,),
        ).fetchone()
        now = core.clock_ms()
        if row is None or row[4] is not None or row[3] <= now or row[2] != directory().epoch(profile_id):
            raise ApiError("validation_failed", "the student's authorization is missing, used or expired")
        conn.execute("UPDATE archive_authorizations SET consumed_at = ? WHERE authorization_id = ?",
                     (now, authorization_id))  # fmt: skip
        return list(json.loads(row[0])), bool(row[1])

    # --- exports (DAT-10, API-30) -------------------------------------------------------------------------------------

    def write(destination: dict[str, Any], kind: str, sealed: bytes) -> Path:
        folder = Path(destination["path"])
        if not folder.is_dir():
            raise ApiError("validation_failed", "the destination folder does not exist")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(core.clock_ms() / 1000))
        path = folder / f"jana-{kind.replace('_', '-')}-{stamp}{EXTENSIONS[kind]}"
        path.write_bytes(sealed)
        return path

    def record(
        conn: Any,
        kind: str,
        destination: str,
        sealed: bytes,
        recipient: str | None,
        approval: dict[str, Any] | None = None,
    ) -> str:
        now = core.clock_ms()
        export_id = new_ulid(now)
        seq = conn.execute("SELECT min(seq), max(seq) FROM events").fetchone()
        conn.execute(
            "INSERT INTO exports (export_id, kind, destination, recipient, event_seq_from, event_seq_to, sha256,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (export_id, kind, destination, recipient, seq[0], seq[1], hashlib.sha256(sealed).hexdigest(), now),
        )
        detail: dict[str, Any] = {"destination": destination}
        if kind == "research_export" and approval is not None:
            detail["research_approval"] = approval
        conn.execute(
            "INSERT INTO audit_log (audit_id, actor_role, action, target, detail, at)"
            " VALUES (?, 'student', ?, ?, ?, ?)",
            (new_ulid(now), f"export.{kind}", export_id, json.dumps(detail), now),
        )
        return export_id

    @app.post("/v1/exports")
    async def create_export(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            # The contract requires `research_approval` on a research export and refuses it on any other kind (EVL-22).
            asked = require("CreateExport", body)
            approval = asked.get("research_approval")
            kind, profile_id = asked["kind"], asked["profile_ids"][0]
            if CURRENT_PROFILE.get() != profile_id:
                raise ApiError("not_found", "no such profile")
            d = directory()
            guarded(lambda: d.epoch(profile_id) or d.profile(profile_id))
            settings = core.settings
            with d.open_learner(profile_id) as conn, transaction(conn):
                if kind == "profile_archive":
                    sessions: list[str] = []
                    attachments = False
                    if asked.get("include_transcripts") or asked.get("include_attachments"):
                        sessions, allowed = consume(conn, profile_id, asked["archive_authorization_id"])
                        attachments = allowed and bool(asked.get("include_attachments"))
                    archive = build_profile_archive(conn, d.learner_path(profile_id).parent, d.app_version,
                                                    iso_ms(core.clock_ms()), sessions, attachments,
                                                    bool(asked.get("include_study_sources", False)))  # fmt: skip
                    sealed = seal_with_passphrase(archive, asked["passphrase"], settings.passphrase_argon2_memory_kib,
                                                  settings.passphrase_argon2_iterations)  # fmt: skip
                    recipient = None
                elif kind == "research_export":
                    consent = active(conn, "research_use")
                    if consent is None or consent[5] is not None:
                        raise ApiError("validation_failed", "a research export needs active research_use consent")
                    recipient = settings.export_research_public_key
                    sealed = seal_to_recipient(json.dumps(export_rows(conn, approval)).encode(), recipient)
                elif kind == "diagnostic_bundle":
                    if asked.get("include_session_ids"):
                        raise ApiError("validation_failed", "conversation export requires a separate support bundle")
                    reports = []
                    for report_id in asked.get("report_ids", []):
                        row = conn.execute(
                            "SELECT content FROM problem_reports WHERE report_id = ? AND expires_at > ?",
                            (report_id, core.clock_ms()),
                        ).fetchone()
                        if row is None:
                            raise ApiError("not_found", "no such saved report")
                        content = json.loads(row[0])
                        if content.get("allow_support_export") is not True:
                            raise ApiError("validation_failed", "this report is not authorized for support export")
                        reports.append({"report_id": report_id, **content})
                    recipient = settings.export_support_public_key
                    traces = core.diagnostic_traces(profile_id)
                    sealed = seal_to_recipient(
                        json.dumps({"traces": traces, "problem_reports": reports}).encode(), recipient
                    )
                else:
                    raise ApiError("validation_failed", f"{kind} exports arrive in a later stage")
                path = write(asked["destination"], kind, sealed)
                record(conn, kind, asked["destination"]["kind"], sealed, recipient, approval)
            return 202, job("export", str(path))

        return await mutating(request, run)

    # --- import as a new profile (API-28) -----------------------------------------------------------------------------

    @app.post("/v1/profiles/import")
    async def import_archive(request: Request) -> Response:
        async def run(body: Any) -> tuple[int, Any]:
            asked = require("ImportArchive", body)
            path = Path(asked["source"]["path"])
            if not path.is_file():
                raise ApiError("validation_failed", "the archive file was not found")
            limit = int(core.settings.archive_import_max_mb * 1024 * 1024)
            if path.stat().st_size > limit:  # refused before it is read (DAT-10)
                raise ApiError("archive_invalid", "the archive is larger than a profile archive can be")
            entries = core.settings.archive_import_max_entries
            archive = guarded(lambda: read_profile_archive(open_with_passphrase(path.read_bytes(), asked["passphrase"]),
                                                           limit, entries))  # fmt: skip
            return 202, job("import", import_profile(archive, asked.get("display_name")))

        return await mutating(request, run)

    def import_profile(archive: Any, display_name: str | None) -> str:
        """A new profile with a new id and no PIN; events re-keyed to it and projections rebuilt; archived consents as
        withdrawn history; never touches an existing profile."""
        d = directory()
        fields = archive.manifest["profile"]
        guarded(lambda: require_supported_age(fields["age_band"]))
        taken = {p["display_name"] for p in d.summaries()["profiles"]}
        name = display_name or fields["display_name"]
        suffix = 2
        while name in taken:
            name, suffix = f"{(display_name or fields['display_name'])[:36]} ({suffix})", suffix + 1
        # Consult only content-free tombstones in the device's existing profiles. No source content is read.
        deleted: dict[str, int] = {}
        erased_memory: set[str] = set()
        for existing in d.summaries()["profiles"]:
            with d.open_learner(existing["profile_id"]) as old:
                erased_memory.update(
                    str(r[0])
                    for r in old.execute(
                        "SELECT event_id FROM events WHERE erased_at IS NOT NULL"
                        " AND type IN ('note_written','correction')",
                    )
                )
                if archive.study_workspace is not None:
                    deleted.update(old.execute("SELECT source_id,deleted_at FROM study_tombstones").fetchall())
        created = d.create({"display_name": name, "avatar": fields["avatar"], "level": fields["level"],
                            "subjects": fields["subjects"], "age_band": fields["age_band"],
                            "exam_date": fields.get("exam_date")})  # fmt: skip
        new_id_ = str(created["profile_id"])
        internal = internal_id(new_id_)
        now = core.clock_ms()

        @contextmanager
        def importing() -> Iterator[Any]:
            try:
                with d.open_learner(new_id_) as conn:
                    yield conn
            except BaseException:
                # Import owns this newly-created profile. A late disk/constraint failure must leave no
                # partial profile or original-file blobs behind, including when the DB transaction rolled back.
                d.delete(new_id_)
                raise

        with importing() as conn:
            events = []
            memory_removed = False
            for event in archive.events:
                if event["event_id"] in erased_memory:
                    memory_removed = True
                    continue
                if event["type"] in ("pin_changed", "solution_policy_changed"):
                    continue
                if (
                    event["type"] == "setting_change"
                    and isinstance(event["payload"], dict)
                    and event["payload"].get("setting") == "solution_policy"
                ):
                    continue
                event = {**event, "profile_id": internal}
                if event["source"] == "adult":
                    event["source"] = "student"
                payload = event["payload"]
                if isinstance(payload, dict):
                    payload = dict(payload)
                    payload.pop("pin_verified", None)
                    if payload.get("actor_role") == "adult":
                        payload["actor_role"] = "student"
                    event["payload"] = payload
                events.append(event)
            log = EventLog.open(
                conn,
                internal,
                d.device_id,
                lambda: int(time.time() * 1000),
                max(core.settings.sync_clock_skew_max_ms, 10**13),
            )  # archived stamps are in the past
            log.apply_remote(events)
            if core.rebuild_projections is not None:
                core.rebuild_projections(conn, internal)  # the runtime's learner model folds the imported events
            with transaction(conn):
                for row in archive.settings:
                    if row["key"] not in PREFERENCES:
                        continue
                    row = {**row, "source": "student" if row["source"] == "adult" else row["source"]}
                    if row["key"] == "checkin_notifications":
                        row["value"] = "false"
                    if row["key"] == "tutor_avatar":  # an archive from before the six characters (D103, G119)
                        row["value"] = json.dumps(tutor_avatar(json.loads(row["value"])))
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value, source, set_by_event_id, hlc, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        tuple(row[k] for k in ("key", "value", "source", "set_by_event_id", "hlc", "updated_at")),
                    )
                for row in archive.consents:
                    if row["scope"] not in PROFILE_SCOPES:
                        continue
                    row = dict(row)
                    row["method"] = (
                        "in_app_notice"
                        if row["granted_by_role"] == "student_18_plus"
                        else (
                            "in_app_guardian"
                            if row["scope"] in ("research_use", "model_training_research")
                            else "checkout_guardian"
                        )
                    )
                    row["dpia_ref"] = row.get("dpia_ref") or "legacy-unrecorded"
                    withdrawn = row["withdrawn_at"] or now
                    conn.execute(
                        "INSERT INTO consents (consent_id, scope, granted_by_role, granted_by_label, basis_ref,"
                        " notice_version, dpia_ref, method, granted_at, expires_at, withdrawn_at, withdrawn_by_role)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row["consent_id"], row["scope"], row["granted_by_role"], row["granted_by_label"],
                         row["basis_ref"], row["notice_version"], row["dpia_ref"], row["method"], row["granted_at"],
                         row["expires_at"], withdrawn, row["withdrawn_by_role"] or row["granted_by_role"]),
                    )  # fmt: skip
                for row in archive.study_notes:
                    from agent_core.contracts import errors

                    # A note carries `private` only as true (D104), absent in an archive from before the mark; a row
                    # copied as the column's 0 or 1 reads the same.
                    marked = isinstance(row, dict) and row.get("private") in (1, True)
                    note = {k: v for k, v in row.items() if k != "private"} if isinstance(row, dict) else row
                    if marked:
                        note["private"] = True
                    if errors("voice/activities#/$defs/study_note", note):
                        raise ApiError("archive_invalid", "invalid study note")
                    conn.execute(
                        "INSERT INTO study_notes (note_id, text, source, created_at, updated_at, private)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (*(note[k] for k in ("note_id", "text", "source", "created_at", "updated_at")), int(marked)),
                    )
                for row in archive.session_notes:
                    from agent_core.engine.session_notes import archive_note

                    try:
                        values = archive_note(row)
                    except (ValueError, TypeError, KeyError):
                        raise ApiError("archive_invalid", "invalid session note") from None
                    conn.execute(
                        "INSERT INTO session_notes (note_id,session_id,title,subject,kc_ids,statements,state,private,"
                        "source_revision,withheld,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                if archive.study_workspace is not None:
                    from agent_core.store.study_archive import restore

                    restore(conn, d.learner_path(new_id_).parent, archive.study_workspace, archive.study_blobs, deleted)
                for session in archive.sessions.values():
                    for table in TRANSCRIPT_SQL:
                        known = {str(c[1]) for c in conn.execute(f"PRAGMA table_info({table})")}  # fixed names
                        for row in session[table]:
                            if not isinstance(row, dict) or not row or set(row) - known:
                                # Column names come from the archive: only the table's own are ever written.
                                raise ApiError("archive_invalid", "the archive is damaged")
                            columns = ", ".join(row)
                            marks = ", ".join("?" for _ in row)
                            conn.execute(f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({marks})",  # noqa: S608
                                         tuple(row.values()))  # fmt: skip
                if archive.companion is not None:
                    from agent_core.store.companion_archive import restore as restore_companion

                    restore_companion(conn, archive.companion)
                if memory_removed:
                    from agent_core.engine.companion_import import exclude_removed_memory

                    # Old archives can contain paraphrases in dialogue/check-ins. Keep authorized history visible,
                    # exclude it from model recall, and do not revive reminders derived from removed memory.
                    exclude_removed_memory(conn, now)
            if archive.companion is not None:
                from agent_core.engine.companion_import import refold_summaries

                refold_summaries(conn, archive.companion, core.companion.content(), now_ms=now)
        d.audit("profile.import", internal)
        return new_id_
