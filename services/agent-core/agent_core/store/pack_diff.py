"""Row-level pack diffs between two signed versions (CUR-09).

A diff carries, per changed table, the row ids to delete and the records to insert or replace, plus the new version's
signed manifest. It names the version it applies to by the sha256 of that version's canonical signed manifest, so it
applies to exactly one installed pack. The whole diff is signed by a content key (TABLE SEC-12a); after applying it the
device verifies the result against the new manifest like any pack (signature and every table hash), so a diff can
never produce a pack other than the one the signing service signed.

The format is ours (`FORMAT`): the specification names no diff contract yet.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from agent_core.store.packs import EMBEDDED_TABLE, TABLES, canonical, row_id, table_sql

FORMAT = "jana.pack-diff.1"
ARTEFACT = "content_diff"


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(manifest).encode()).hexdigest()


def make_diff(old_manifest: Mapping[str, Any], old_tables: Mapping[str, Sequence[Mapping[str, Any]]],
              new_manifest: Mapping[str, Any], new_tables: Mapping[str, Sequence[Mapping[str, Any]]],
              created_at: str, signed_files: Mapping[str, Mapping[str, Any]] | None = None
              ) -> dict[str, Any]:  # fmt: skip
    """The unsigned diff from one signed version to the next (both manifests already signed). `signed_files` are the
    new version's embedded signed files (SEC-16, OPS-02): they replace the old ones whole, as they are small."""
    if old_manifest["pack_id"] != new_manifest["pack_id"]:
        raise ValueError("a diff joins two versions of the same pack")
    changes = []
    for table in TABLES:
        before = {row_id(table, r): canonical(r) for r in old_tables.get(table, [])}
        after = {row_id(table, r): r for r in new_tables.get(table, [])}
        delete = sorted(k for k in before if k not in after)
        upsert = [after[k] for k in sorted(after) if before.get(k) != canonical(after[k])]
        if delete or upsert:
            changes.append({"table": table, "delete": delete, "upsert": upsert})
    diff = {
        "format": FORMAT,
        "pack_id": new_manifest["pack_id"],
        "from_version": old_manifest["version"],
        "to_version": new_manifest["version"],
        "from_manifest_sha256": manifest_sha256(old_manifest),
        "created_at": created_at,
        "manifest": dict(new_manifest),
        "changes": changes,
    }
    if signed_files:
        diff["signed_files"] = {name: dict(doc) for name, doc in sorted(signed_files.items())}
    return diff


def diff_bytes(diff: Mapping[str, Any]) -> bytes:
    """The diff as it ships: canonical JSON."""
    return canonical(diff).encode()


def read_diff(data: bytes) -> dict[str, Any]:
    document = json.loads(data)
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError("not a pack diff")
    return document


def apply_rows(conn: sqlite3.Connection, diff: Mapping[str, Any]) -> None:
    """Apply the diff's rows and manifest inside the caller's transaction on a copy of the installed pack."""
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    for change in diff["changes"]:
        table = change["table"]
        if table not in TABLES:
            raise ValueError(f"the diff changes an unknown table {table!r}")
        if table not in present:
            conn.execute(table_sql(table))
            present.add(table)
        conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(i,) for i in change["delete"]])  # noqa: S608
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} VALUES (?, ?)",  # noqa: S608 - the table is one of TABLES
            [(row_id(table, r), canonical(r)) for r in change["upsert"]],
        )
    conn.execute("UPDATE manifest SET record = ? WHERE id = 1", (canonical(diff["manifest"]),))
    if "signed_files" in diff or EMBEDDED_TABLE in present:
        if EMBEDDED_TABLE not in present:
            conn.execute(table_sql(EMBEDDED_TABLE))
        conn.execute(f"DELETE FROM {EMBEDDED_TABLE}")  # noqa: S608 - a fixed name
        rows = sorted((name, canonical(doc)) for name, doc in diff.get("signed_files", {}).items())
        conn.executemany(f"INSERT INTO {EMBEDDED_TABLE} VALUES (?, ?)", rows)  # noqa: S608 - a fixed name
