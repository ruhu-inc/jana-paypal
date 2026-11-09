"""Exports that leave the device (DAT-10, SEC-18, API-30) and profile archive import (API-28).

Every export is sealed before it is written: a profile archive under the adult's passphrase (Argon2id + AES-256-GCM,
whose tag is the integrity check, so one changed byte is refused); a diagnostic bundle or research export to the
recipient's X25519 public key (an ephemeral key agreement, HKDF-SHA256, AES-256-GCM). The learner data on the device is
not encrypted at rest (D68); only what leaves it is. Passphrases are never stored, logged or traced.

A profile archive holds the profile's fields, its whole event log, settings, consents and notes, and transcripts or
attachments only for the sessions a student authorization names (API-30). Import always makes a new profile: the
events are re-keyed to the new id, keeping their ids and stamps, and the projections rebuilt from them."""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import struct
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PASSPHRASE_MAGIC = b"JANA-ARCHIVE-1\n"
RECIPIENT_MAGIC = b"JANA-SEALED-1\n"
EXTENSIONS = {"profile_archive": ".janaprofile", "diagnostic_bundle": ".janadiag", "research_export": ".janaresearch"}
FORMAT = "jana.profile_archive"
TRANSCRIPT_SQL = {
    "sessions": "SELECT * FROM sessions WHERE session_id = ?",
    "turns": "SELECT * FROM turns WHERE session_id = ? ORDER BY idx",
    "parts": "SELECT * FROM parts WHERE turn_id IN (SELECT turn_id FROM turns WHERE session_id = ?) ORDER BY part_id",
    "board_steps": "SELECT * FROM board_steps WHERE session_id = ? ORDER BY idx",
    "checks": "SELECT * FROM checks WHERE session_id = ? ORDER BY check_id",
}


class ExportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --- sealing ---------------------------------------------------------------------------------------------------------


def _passphrase_key(passphrase: str, salt: bytes, memory_kib: int, iterations: int) -> bytes:
    return Argon2id(salt=salt, length=32, iterations=iterations, lanes=1, memory_cost=memory_kib).derive(
        passphrase.encode()
    )


def seal_with_passphrase(data: bytes, passphrase: str, memory_kib: int, iterations: int) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = PASSPHRASE_MAGIC + struct.pack(">IB", memory_kib, iterations) + salt + nonce
    key = _passphrase_key(passphrase, salt, memory_kib, iterations)
    return header + AESGCM(key).encrypt(nonce, data, header)


def _kdf_bounds() -> tuple[tuple[int, int], tuple[int, int]]:
    """The Argon2id cost an archive may ask for: the declared range of the app's own keys (SEC-18), so a crafted
    header cannot make opening it take unbounded memory or time."""
    from agent_core.config.keys import Settings

    def bounds(name: str) -> tuple[int, int]:
        meta = Settings.model_fields[name].metadata
        low = next(int(m.ge) for m in meta if getattr(m, "ge", None) is not None)
        high = next(int(m.le) for m in meta if getattr(m, "le", None) is not None)
        return low, high

    return bounds("passphrase_argon2_memory_kib"), bounds("passphrase_argon2_iterations")


def open_with_passphrase(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(PASSPHRASE_MAGIC) or len(blob) < len(PASSPHRASE_MAGIC) + 33:
        raise ExportError("archive_unsupported_version", "not a profile archive this app can read")
    at = len(PASSPHRASE_MAGIC)
    memory, iterations = struct.unpack(">IB", blob[at : at + 5])
    (memory_low, memory_high), (passes_low, passes_high) = _kdf_bounds()
    if not memory_low <= memory <= memory_high or not passes_low <= iterations <= passes_high:
        raise ExportError("archive_invalid", "the archive asks for a key derivation this app does not use")
    salt, nonce = blob[at + 5 : at + 21], blob[at + 21 : at + 33]
    header = blob[: at + 33]
    try:
        return AESGCM(_passphrase_key(passphrase, salt, memory, iterations)).decrypt(nonce, blob[at + 33 :], header)
    except InvalidTag:
        raise ExportError("archive_invalid", "the passphrase is wrong or the archive was changed") from None


def _shared_key(secret: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"jana export v1").derive(secret)


def seal_to_recipient(data: bytes, recipient_public_b64: str) -> bytes:
    recipient = X25519PublicKey.from_public_bytes(base64.b64decode(recipient_public_b64))
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    nonce = os.urandom(12)
    header = RECIPIENT_MAGIC + ephemeral_public + nonce
    key = _shared_key(ephemeral.exchange(recipient), ephemeral_public)
    return header + AESGCM(key).encrypt(nonce, data, header)


def open_as_recipient(blob: bytes, private_key: X25519PrivateKey) -> bytes:
    if not blob.startswith(RECIPIENT_MAGIC):
        raise ExportError("archive_unsupported_version", "not a sealed export")
    at = len(RECIPIENT_MAGIC)
    ephemeral_public, nonce = blob[at : at + 32], blob[at + 32 : at + 44]
    key = _shared_key(private_key.exchange(X25519PublicKey.from_public_bytes(ephemeral_public)), ephemeral_public)
    try:
        return AESGCM(key).decrypt(nonce, blob[at + 44 :], blob[: at + 44])
    except InvalidTag:
        raise ExportError("archive_invalid", "the export was changed or is for another recipient") from None


# --- contents --------------------------------------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(args))
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from agent_core.store.events import EventLog

    log = EventLog(conn, "", "", None)  # type: ignore[arg-type]  # reading needs no clock
    return [e.envelope() for e in log.by_seq() if not e.erased]


@dataclass
class Archive:
    manifest: dict[str, Any]
    events: list[dict[str, Any]]
    settings: list[dict[str, Any]]
    consents: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    sessions: dict[str, dict[str, list[dict[str, Any]]]]
    attachments: dict[str, bytes]
    study_notes: list[dict[str, Any]] = field(default_factory=list)
    session_notes: list[dict[str, Any]] = field(default_factory=list)
    study_workspace: dict[str, Any] | None = None
    study_blobs: dict[str, bytes] = field(default_factory=dict)
    companion: dict[str, Any] | None = None


def build_profile_archive(
    conn: sqlite3.Connection,
    profile_dir: Path,
    app_version: str,
    created_at: str,
    session_ids: list[str],
    include_attachments: bool,
    include_study_sources: bool = False,
) -> bytes:
    """The archive's zip: everything of the profile except transcripts and attachments, which come only for the
    authorized sessions."""
    profile = _rows(conn, "SELECT display_name, avatar, age_band, level, subjects, exam_date, locale FROM profile")[0]
    profile["subjects"] = json.loads(profile["subjects"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        sessions: dict[str, Any] = {}
        attached: list[str] = []
        for session_id in session_ids:
            session = {table: _rows(conn, sql, (session_id,)) for table, sql in TRANSCRIPT_SQL.items()}
            sessions[session_id] = session
            if include_attachments:
                for row in _rows(conn, "SELECT attachment_id, file_path FROM attachments WHERE session_id = ?",
                                 (session_id,)):  # fmt: skip
                    path = profile_dir / str(row["file_path"])
                    if path.is_file():
                        archive.writestr(f"attachments/{row['attachment_id']}", path.read_bytes())
                        attached.append(str(row["attachment_id"]))
        session_notes = _rows(conn, "SELECT * FROM session_notes")
        from agent_core.store.companion_archive import snapshot as companion_snapshot

        companion = companion_snapshot(conn, session_ids)
        has_companion = bool(companion["revision"]["id"] or any(companion[k] for k in companion if k != "revision"))
        manifest = {
            "format": FORMAT,
            "version": 4 if has_companion else 3 if include_study_sources else 2 if session_notes else 1,
            "app_version": app_version,
            "created_at": created_at,
            "profile": profile,
            "transcript_sessions": sorted(sessions),
            "attachments": attached,
        }
        if has_companion:
            archive.writestr("companion.json", json.dumps(companion))
        if include_study_sources:
            from agent_core.store.study_archive import snapshot

            workspace, study_blobs = snapshot(conn, profile_dir)
            archive.writestr("study_workspace.json", json.dumps(workspace))
            for ref, data in study_blobs.items():
                archive.writestr(f"study_blobs/{ref}", data)
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        archive.writestr("events.jsonl", "".join(json.dumps(e, sort_keys=True) + "\n" for e in _events(conn)))
        archive.writestr("settings.json", json.dumps(_rows(conn, "SELECT * FROM settings")))
        archive.writestr("consents.json", json.dumps(_rows(conn, "SELECT * FROM consents")))
        archive.writestr("notes.json", json.dumps(_rows(conn, "SELECT * FROM notes WHERE status <> 'deleted'")))
        # Each note in the study_note contract's shape: `private` only on a note the student marked (D104).
        notes = [{**n, "private": True} if n.pop("private", 0) else n for n in _rows(conn, "SELECT * FROM study_notes")]
        archive.writestr("study_notes.json", json.dumps(notes))
        archive.writestr("session_notes.json", json.dumps(session_notes))
        for session_id, session in sessions.items():
            archive.writestr(f"sessions/{session_id}.json", json.dumps(session, sort_keys=True))
    return out.getvalue()


def read_profile_archive(data: bytes, max_bytes: int | None = None, max_entries: int | None = None) -> Archive:
    """The archive's parts. Its declared sizes are checked before anything is unpacked: more than `max_entries`
    entries or `max_bytes` unpacked in total is refused (DAT-10), and so is an entry whose name could leave a folder."""
    from agent_core.config.keys import Settings

    defaults = Settings()
    max_bytes = max_bytes if max_bytes is not None else int(defaults.archive_import_max_mb * 1024 * 1024)
    max_entries = max_entries if max_entries is not None else defaults.archive_import_max_entries
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            listed = archive.infolist()
            if len(listed) > max_entries or sum(info.file_size for info in listed) > max_bytes:
                raise ExportError("archive_invalid", "the archive is larger than a profile archive can be")
            if any(info.filename.startswith("/") or ".." in info.filename.split("/") for info in listed):
                raise ExportError("archive_invalid", "the archive is damaged")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != FORMAT or manifest.get("version") not in (1, 2, 3, 4):
                raise ExportError("archive_unsupported_version", "this archive comes from a different app version")
            if manifest.get("version") in (2, 3, 4) and "session_notes.json" not in archive.namelist():
                raise ExportError("archive_invalid", "session notes are missing from this archive")
            workspace = None
            study_blobs = {}
            if manifest["version"] == 3 or (manifest["version"] == 4 and "study_workspace.json" in archive.namelist()):
                from agent_core.store.study_archive import validate

                workspace = json.loads(archive.read("study_workspace.json"))
                study_blobs = {
                    name.removeprefix("study_blobs/"): archive.read(name)
                    for name in archive.namelist()
                    if name.startswith("study_blobs/")
                }
                validate(workspace, study_blobs)
            elif any(name == "study_workspace.json" or name.startswith("study_blobs/") for name in archive.namelist()):
                raise ExportError("archive_invalid", "unexpected study workspace in legacy archive")
            if len({info.filename for info in listed}) != len(listed):
                raise ExportError("archive_invalid", "duplicate archive entries")
            events = [json.loads(line) for line in archive.read("events.jsonl").decode().splitlines() if line]
            sessions = {name[len("sessions/") : -5]: json.loads(archive.read(name)) for name in archive.namelist()
                        if name.startswith("sessions/")}  # fmt: skip
            attachments = {name[len("attachments/") :]: archive.read(name) for name in archive.namelist()
                           if name.startswith("attachments/")}  # fmt: skip
            companion = None
            if manifest["version"] == 4:
                from agent_core.store.companion_archive import validate as validate_companion

                companion = json.loads(archive.read("companion.json"))
                validate_companion(companion, sessions)
            elif "companion.json" in archive.namelist():
                raise ExportError("archive_invalid", "unexpected companion state in legacy archive")
            return Archive(
                manifest,
                events,
                json.loads(archive.read("settings.json")),
                json.loads(archive.read("consents.json")),
                json.loads(archive.read("notes.json")),
                sessions,
                attachments,
                json.loads(archive.read("study_notes.json")) if "study_notes.json" in archive.namelist() else [],
                json.loads(archive.read("session_notes.json")) if "session_notes.json" in archive.namelist() else [],
                workspace,
                study_blobs,
                companion,
            )
    except (KeyError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, ExportError):
            raise
        raise ExportError("archive_invalid", "the archive is damaged") from None


def research_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    """De-identified events for a learning-gain study (EVL-22): no display name, transcript, note or photo, and no
    session or turn ids that could join back to a transcript. A re-marked verdict is exported as its latest re-mark,
    where it stands, and a wrong key's evidence not at all (ASM-23, CUR-36); the profile archive keeps the log whole."""
    from agent_core.store.events import EventLog
    from agent_core.store.supersession import read_log

    log = EventLog(conn, "", "", None)  # type: ignore[arg-type]  # reading needs no clock
    keep = []
    read = list(read_log(log.merged()))
    for event in (e.envelope() for e in read):
        event = {k: v for k, v in event.items() if k not in ("session_id", "turn_id", "profile_id", "device_id")}
        keep.append(event)
    attempts = _rows(conn, "SELECT * FROM attempts ORDER BY occurred_at, attempt_id")
    return {
        "events": keep,
        "kc_mastery": _rows(conn, "SELECT * FROM kc_mastery"),
        "item_responses": _item_responses(read, attempts),
        "skill_sessions": _skill_sessions(read, attempts),
    }


# The modes whose first attempt at a pack item is a response to that item (CUR-26, ASM-21): not guided tutoring, where
# the tutor shapes the answer.
RESPONSE_MODES = frozenset({"practice", "diagnostic", "mastery_check", "mock", "review", "revision"})
GAIN_WINDOW = 5  # checked attempts on the session's KCs before and after a session (SKL-28)


def _kcs(row: dict[str, Any]) -> list[str]:
    """An attempts row's KC ids, in the attempt's order."""
    return [str(k["kc_id"]) for k in json.loads(row["kc_ids"])]


def _item_responses(read: list[Any], attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each first attempt at a pack item in an assessment mode, with its latest verdict from the attempts projection
    (CUR-26, ASM-21): the item, the option when the attempt recorded one, and the misconception the marker matched,
    which names the distractor of a diagnostic choice item; never the answer the student wrote (the kept answer is
    transcript, MEM-26). The session and attempt ids stay on the device; the join happens here."""
    asked = {e.payload.get("attempt_id"): e.payload for e in read if e.type == "attempt"}
    rows = []
    for row in attempts:
        attempt = asked.get(row["attempt_id"])
        if attempt is None or not attempt.get("item_id") or attempt.get("attempt_index") != 1:
            continue
        if row["mode"] not in RESPONSE_MODES or row["outcome"] not in ("correct", "incorrect", "partial"):
            continue
        kcs = _kcs(row)
        rows.append({"item_id": attempt["item_id"], "outcome": row["outcome"], "time_ms": row["time_ms"],
                     "choice_id": attempt.get("choice_id"), "misconception_id": row["misconception_id"],
                     "kc_id": kcs[0] if kcs else None})  # fmt: skip
    return rows


def _skill_sessions(read: list[Any], attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per session that delivered skills, the skills' (name, version), the hints used and the mastery gain on the
    session's KCs (SKL-28): the share correct of the student's next `GAIN_WINDOW` checked attempts on those KCs after
    the session, less that of the last ones before it; None without both. The session itself is not exported."""
    delivered: dict[str, set[tuple[str, str]]] = {}
    for event in read:
        if event.type == "skills_delivered" and event.session_id:
            for skill in event.payload.get("skills", []):
                delivered.setdefault(event.session_id, set()).add((str(skill["name"]), str(skill["version"])))
    checked = [r for r in attempts if r["outcome"] in ("correct", "incorrect", "partial")]
    rows = []
    for session, skills in delivered.items():
        mine = [r for r in checked if r["session_id"] == session]
        kcs = {kc for r in mine for kc in _kcs(r)}
        gain = None
        if mine:
            start, end = mine[0]["occurred_at"], mine[-1]["occurred_at"]
            on_kcs = [r for r in checked if r["session_id"] != session and kcs & set(_kcs(r))]
            before = [r["outcome"] == "correct" for r in on_kcs if r["occurred_at"] < start][-GAIN_WINDOW:]
            after = [r["outcome"] == "correct" for r in on_kcs if r["occurred_at"] > end][:GAIN_WINDOW]
            if before and after:
                gain = sum(after) / len(after) - sum(before) / len(before)
        rows.append({"skills": [{"name": n, "version": v} for n, v in sorted(skills)],
                     "hints": sum(r["hints_used"] for r in attempts if r["session_id"] == session),
                     "mastery_gain": gain})  # fmt: skip
    return rows
