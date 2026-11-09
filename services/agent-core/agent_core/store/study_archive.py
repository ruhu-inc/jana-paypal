"""Explicit encrypted-archive contents for DAT-22. No jobs, grants, credentials or automatic cloud replay."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from agent_core.contracts import errors
from agent_core.store.exports import ExportError

# Fixed identifiers only; archive input never supplies a SQL identifier.
TABLES = {
    "study_sources": ("source_id", "current_revision", "private", "body", "created_at", "updated_at"),
    "study_revisions": ("source_id", "revision", "digest", "blob_ref", "byte_length", "extracted_at"),
    "study_chunks": ("chunk_id", "source_id", "revision", "ordinal", "body"),
    "study_aids": ("aid_id", "body", "created_at"),
    "study_aid_sources": ("aid_id", "source_id", "revision"),
    "study_decks": ("deck_id", "aid_id", "source_id", "revision", "scheduler"),
    "study_flashcards": ("card_id", "deck_id", "revision", "body", "schedule", "due_at"),
    "study_reviews": ("review_id", "card_id", "body", "reviewed_at"),
    "study_quizzes": ("quiz_id", "aid_id", "revision", "body"),
    "study_attempts": ("attempt_id", "quiz_id", "body", "submitted_at"),
    "study_tombstones": ("source_id", "deleted_at"),
}
MODELS = {
    "study_sources": "Source",
    "study_chunks": "Chunk",
    "study_aids": "Aid",
    "study_flashcards": "Flashcard",
    "study_reviews": "ReviewResult",
    "study_quizzes": "QuizStored",
    "study_attempts": "QuizResult",
}


def snapshot(conn: sqlite3.Connection, folder: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    rows = {
        table: [
            dict(zip(columns, row, strict=True))
            for row in conn.execute(f"SELECT {','.join(columns)} FROM {table}")  # noqa: S608 - fixed TABLES
        ]
        for table, columns in TABLES.items()
    }
    pending = {(r[0], r[1]) for r in conn.execute("SELECT source_id,revision FROM study_pending_revisions")}
    rows["study_revisions"] = [r for r in rows["study_revisions"] if (r["source_id"], r["revision"]) not in pending]
    if any(json.loads(r["body"])["state"] == "uploading" for r in rows["study_sources"]):
        raise ExportError("archive_invalid", "finish uploading study sources before exporting them")
    blobs = {}
    for row in rows["study_revisions"]:
        if row["blob_ref"] is not None:
            key = row["blob_ref"]
            data = (folder / "study" / "blobs" / key).read_bytes()
            if hashlib.sha256(data).hexdigest() != key:
                raise ExportError("archive_invalid", "a study source file is damaged")
            blobs[key] = data
    validate(rows, blobs)
    return rows, blobs


def validate(rows: dict[str, Any], blobs: dict[str, bytes]) -> None:
    """Validate before creating a profile or writing a single file. No path from the archive is trusted."""

    def reject() -> None:
        raise ExportError("archive_invalid", "invalid study workspace")

    if not isinstance(rows, dict) or set(rows) != set(TABLES):
        reject()
    for table, columns in TABLES.items():
        if not isinstance(rows[table], list):
            reject()
        for row in rows[table]:
            if not isinstance(row, dict) or set(row) != set(columns):
                reject()
            for key, value in row.items():
                if key in ("body", "schedule"):
                    if not isinstance(value, str):
                        reject()
                elif key.endswith("_at") or key in (
                    "revision",
                    "current_revision",
                    "ordinal",
                    "private",
                    "byte_length",
                ):
                    if value is not None and (type(value) is not int or value < 0):
                        reject()
                elif value is not None and not isinstance(value, str):
                    reject()
            if table in MODELS:
                try:
                    body = json.loads(row["body"])
                except (TypeError, ValueError):
                    reject()
                if errors("study/workspace#/$defs/" + MODELS[table], body):
                    reject()
                for key in ("source_id", "chunk_id", "aid_id", "card_id", "attempt_id", "revision", "ordinal"):
                    if key in row and key in body and body[key] != row[key]:
                        reject()
                if table == "study_sources" and (
                    body["revision"] != row["current_revision"] or body["private"] != bool(row["private"])
                ):
                    reject()
            if table == "study_flashcards":
                try:
                    schedule = json.loads(row["schedule"])
                except (TypeError, ValueError):
                    reject()
                if errors("study/workspace#/$defs/CardSchedule", schedule):
                    reject()
    sources = {r["source_id"]: r for r in rows["study_sources"]}
    revisions = {(r["source_id"], r["revision"]): r for r in rows["study_revisions"]}
    chunks = {r["chunk_id"]: json.loads(r["body"]) for r in rows["study_chunks"]}
    if (
        len(sources) != len(rows["study_sources"])
        or len(revisions) != len(rows["study_revisions"])
        or len(chunks) != len(rows["study_chunks"])
    ):
        reject()
    for row in rows["study_tombstones"]:
        if errors("study/workspace#/$defs/SourceId", row["source_id"]) or row["source_id"] in sources:
            reject()
    refs = set()
    for (sid, _), row in revisions.items():
        if sid not in sources:
            reject()
        ref = row["blob_ref"]
        if ref is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", ref) or ref not in blobs:
                reject()
            data = blobs[ref]
            if hashlib.sha256(data).hexdigest() != ref or ref != row["digest"] or len(data) != row["byte_length"]:
                reject()
            refs.add(ref)
    if set(blobs) != refs or any((sid, r["current_revision"]) not in revisions for sid, r in sources.items()):
        reject()
    for c in chunks.values():
        if (c["source_id"], c["revision"]) not in revisions or hashlib.sha256(c["text"].encode()).hexdigest() != c[
            "sha256"
        ]:
            reject()
    links = {(r["aid_id"], r["source_id"], r["revision"]) for r in rows["study_aid_sources"]}
    aids = {r["aid_id"]: json.loads(r["body"]) for r in rows["study_aids"]}
    for aid, sid, rev in links:
        if aid not in aids or (sid, rev) not in revisions:
            reject()

    def citations(value: Any, allowed: set[tuple[str, int]]) -> None:
        if isinstance(value, dict):
            if "citations" in value:
                for c in value["citations"]:
                    chunk = chunks.get(c["chunk_id"])
                    if (
                        chunk is None
                        or (c["source_id"], c["revision"]) not in allowed
                        or (chunk["source_id"], chunk["revision"]) != (c["source_id"], c["revision"])
                        or not (0 <= c["start"] < c["end"] <= len(chunk["text"]))
                        or chunk["text"][c["start"] : c["end"]] != c["quote"]
                    ):
                        reject()
            for child in value.values():
                citations(child, allowed)
        elif isinstance(value, list):
            for child in value:
                citations(child, allowed)

    for aid_id, aid in aids.items():
        allowed = {(s["source_id"], s["revision"]) for s in aid["sources"]}
        if {(aid_id, sid, rev) for sid, rev in allowed} != {r for r in links if r[0] == aid_id}:
            reject()
        citations(aid, allowed)
    decks = {r["deck_id"]: r for r in rows["study_decks"]}
    quizzes = {r["quiz_id"]: r for r in rows["study_quizzes"]}
    cards = {r["card_id"] for r in rows["study_flashcards"]}
    for row in [*decks.values(), *quizzes.values()]:
        if row["aid_id"] not in aids:
            reject()
        aid = aids[row["aid_id"]]
        if row.get("deck_id", row.get("quiz_id")) not in (aid.get("deck_id"), aid.get("quiz_id")):
            reject()
    for row in rows["study_flashcards"]:
        deck = decks.get(row["deck_id"])
        if deck is None:
            reject()
        assert deck is not None
        citations(json.loads(row["body"]), {(deck["source_id"], deck["revision"])})
    for row in rows["study_quizzes"]:
        body = json.loads(row["body"])
        if body["view"]["quiz_id"] != row["quiz_id"] or body["view"]["aid_id"] != row["aid_id"]:
            reject()
        citations(body, {(s["source_id"], s["revision"]) for s in aids[row["aid_id"]]["sources"]})
    if any(r["card_id"] not in cards for r in rows["study_reviews"]) or any(
        r["quiz_id"] not in quizzes for r in rows["study_attempts"]
    ):
        reject()

    # Exercise the actual immutable/FK/uniqueness/storage constraints before import creates a profile.
    from agent_core.store.migration_v10 import LEARNER

    scratch = sqlite3.connect(":memory:")
    try:
        scratch.execute("PRAGMA foreign_keys=ON")
        scratch.executescript(LEARNER)
        for table, columns in TABLES.items():
            for row in rows[table]:
                scratch.execute(  # fixed TABLES, never archive identifiers
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # noqa: S608
                    tuple(row[k] for k in columns),
                )
    except sqlite3.Error:
        reject()
    finally:
        scratch.close()


def restore(
    conn: sqlite3.Connection, folder: Path, rows: dict[str, Any], blobs: dict[str, bytes], deleted: dict[str, int]
) -> None:
    validate(rows, blobs)
    # Deletion wins even over a previously exported snapshot. Source ids are random globally unique identities.
    dead = {**deleted, **{r["source_id"]: r["deleted_at"] for r in rows["study_tombstones"]}}
    for sid, at in dead.items():
        conn.execute("INSERT OR IGNORE INTO study_tombstones VALUES (?,?)", (sid, at))
    excluded_aids = {r["aid_id"] for r in rows["study_aid_sources"] if r["source_id"] in dead}
    excluded_decks = {r["deck_id"] for r in rows["study_decks"] if r["aid_id"] in excluded_aids}
    excluded_cards = {r["card_id"] for r in rows["study_flashcards"] if r["deck_id"] in excluded_decks}
    excluded_quizzes = {r["quiz_id"] for r in rows["study_quizzes"] if r["aid_id"] in excluded_aids}
    refs = set()
    for table, columns in TABLES.items():
        if table == "study_tombstones":
            continue
        for row in rows[table]:
            if (
                row.get("source_id") in dead
                or row.get("aid_id") in excluded_aids
                or row.get("deck_id") in excluded_decks
                or row.get("card_id") in excluded_cards
                or row.get("quiz_id") in excluded_quizzes
            ):
                continue
            if table == "study_revisions" and row["blob_ref"]:
                refs.add(row["blob_ref"])
            if table == "study_sources":
                source = json.loads(row["body"])
                if source["state"] in ("uploading", "queued", "extracting"):
                    ref = next(
                        (
                            r["blob_ref"]
                            for r in rows["study_revisions"]
                            if r["source_id"] == row["source_id"] and r["revision"] == row["current_revision"]
                        ),
                        None,
                    )
                    source["state"] = "queued" if ref else "unreadable"
                    row = {**row, "body": json.dumps(source)}
            conn.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",  # noqa: S608 - fixed TABLES
                tuple(row[k] for k in columns),
            )
    for ref in refs:
        target = folder / "study" / "blobs" / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs[ref])
