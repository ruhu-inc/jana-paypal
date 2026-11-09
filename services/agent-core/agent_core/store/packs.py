"""Content packs on the device: the SQLite format, table hashes and verification before load (CUR-07, CUR-08,
SEC-13).

A pack is a read-only SQLite file: a one-row `manifest` table and one table per record kind, each row a record id and
the record's canonical JSON. A table's hash is the sha256 of its canonical dump (rows by id, one canonical JSON line
each), so the same content always hashes the same whether it came from SQLite or from the JSON export. The manifest
lists every table's hash and row count and carries an Ed25519 signature over the canonical manifest without the
signature. A pack loads only when the signature verifies with a content key chained to a root this environment
trusts (SEC-12, OPS-35) and every table matches its hash; otherwise the previously installed version stays active.

Pack files are data, never code (SEC-13): they are opened read-only and immutable, with extension loading off and
`trusted_schema=OFF`, and their schema must be exactly the tables the builder writes. A trigger, a view, an index or
a table the builder does not write (or schema text that loads an extension) refuses the pack before any record is
read. At install the whole pack is also checked against the content-pack contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.trust import SignatureRefused, TrustStore, bundled_trust
from agent_core.store import mastery_params

# Record tables in contract order, then the search index tables (CUR-30), each written once the content-pack contract
# has its record. The search tables are derived from the records by the build (`agent_core.retrieval.passages`), never
# authored: a pack carries `search_vectors` only when it was built with an embedding model, named in its manifest.
SEARCH_TABLES = ("search_fts", "search_vectors")
RECORD_TABLES = (
    "curriculum_nodes",
    "kcs",
    "lessons",
    "worked_examples",
    "items",
    "item_templates",
    "misconceptions",
    "diagrams",
    "visual_presets",  # VSL-29: written once the content-pack contract has the record
    "conventions",
    "kc_migrations",
    "mock_papers",  # ASM-15, CUR-31: written once the content-pack contract has the record
    "charts_of_accounts",  # CUR-07 (`pack.ref`), CUR-18
    *SEARCH_TABLES,
)
DAMAGED_MESSAGE_KEY = "pack.damaged"
CONTRACT = "content/content-pack"
# Signed files that ride inside a pack (the manifest's `embedded_signed_files`: SEC-16 revocation list, OPS-02
# compatibility matrix, OPS-10 safety configuration): one row per file, its id the manifest's `signed/...` path and its
# record the signed document. Each file keeps its own signature; the manifest's sha256 covers its canonical JSON.
EMBEDDED_TABLE = "signed_files"


def contract_tables() -> tuple[str, ...]:
    """The record tables the bundled content-pack contract has: one table per top-level array of the contract, so a
    table the contract does not know is never written, read or listed in a manifest."""
    from agent_core.contracts import document

    properties = document(CONTRACT)["properties"]
    return tuple(t for t in RECORD_TABLES if t in properties)


TABLES = contract_tables()
ARTEFACT = "content_pack"  # signed by a content intermediate (TABLE SEC-12a)


class PackRejectedError(ValueError):
    """The pack is damaged, changed, not a plain pack file, or signed by a key this build does not trust."""

    message_key = DAMAGED_MESSAGE_KEY


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_id(table: str, record: Mapping[str, Any]) -> str:
    if table == "conventions":
        scope = record.get("scope", {})
        return "|".join((record["key"], scope.get("subject", ""), scope.get("level", ""), scope.get("node_id", "")))
    if table == "kc_migrations":
        return f"{record['from_kc_id']}@{record['from_version']}"
    return str(record["id"])


def table_dump(table: str, records: Iterable[Mapping[str, Any]]) -> bytes:
    rows = sorted((row_id(table, r), canonical(r)) for r in records)
    return b"".join(f"{key}\t{text}\n".encode() for key, text in rows)


def table_sha256(table: str, records: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(table_dump(table, records)).hexdigest()


def signing_bytes(manifest: Mapping[str, Any]) -> bytes:
    return canonical({k: v for k, v in manifest.items() if k != "signature"}).encode()


# --- the SQLite file ------------------------------------------------------------------------------------------------

MANIFEST_SQL = (
    "CREATE TABLE manifest (id INTEGER PRIMARY KEY CHECK (id = 1), record TEXT NOT NULL CHECK (json_valid(record)))"
    " STRICT"
)


def table_sql(table: str) -> str:
    return f"CREATE TABLE {table} (id TEXT PRIMARY KEY, record TEXT NOT NULL CHECK (json_valid(record))) STRICT"


def embedded_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(document).encode()).hexdigest()


def write_pack(path: Path, manifest: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]],
               signed_files: Mapping[str, Mapping[str, Any]] | None = None) -> None:  # fmt: skip
    """Write a pack file; `signed_files` maps each `embedded_signed_files` path to its signed document."""
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(MANIFEST_SQL)
        conn.execute("INSERT INTO manifest VALUES (1, ?)", (canonical(manifest),))
        for table in TABLES:
            if table not in tables:
                continue
            conn.execute(table_sql(table))
            conn.executemany(
                f"INSERT INTO {table} VALUES (?, ?)",  # noqa: S608 - table names are the fixed list above
                sorted((row_id(table, r), canonical(r)) for r in tables[table]),
            )
        if signed_files:
            conn.execute(table_sql(EMBEDDED_TABLE))
            conn.executemany(f"INSERT INTO {EMBEDDED_TABLE} VALUES (?, ?)",  # noqa: S608 - a fixed name
                             sorted((name, canonical(doc)) for name, doc in signed_files.items()))  # fmt: skip
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _normal(sql: str | None) -> str:
    return " ".join((sql or "").split())


def schema_problems(conn: sqlite3.Connection) -> list[str]:
    """Everything in a pack's schema the builder does not write (SEC-13): triggers, views, virtual tables, explicit
    indexes, unknown tables, or a known table whose definition was changed."""
    known = (*TABLES, EMBEDDED_TABLE)
    expected = {"manifest": MANIFEST_SQL, **{t: table_sql(t) for t in known}}
    problems = []
    for kind, name, owner, sql in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name"):
        if kind == "table" and name in expected and _normal(sql) == _normal(expected[name]):
            continue
        if kind == "index" and sql is None and owner in known and name == f"sqlite_autoindex_{owner}_1":
            continue
        reason = "loads an extension" if "load_extension" in (sql or "").lower() else "not written by the pack builder"
        problems.append(f"{kind} {name}: {reason}")
    return problems


def open_pack(path: Path) -> sqlite3.Connection:
    """A read-only, immutable connection with extension loading off and `trusted_schema=OFF` (SEC-13)."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    if hasattr(conn, "enable_load_extension"):
        conn.enable_load_extension(False)
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def read_pack(path: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The manifest and every record table, read without changing the file. A schema the builder does not write is
    refused before any record is read (SEC-13)."""
    conn = open_pack(path)
    try:
        problems = schema_problems(conn)
        if problems:
            raise PackRejectedError(f"{path.name}: {'; '.join(problems)}")
        manifest = json.loads(conn.execute("SELECT record FROM manifest WHERE id = 1").fetchone()[0])
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        tables = {
            table: [json.loads(r[0]) for r in conn.execute(f"SELECT record FROM {table} ORDER BY id")]  # noqa: S608
            for table in TABLES
            if table in present
        }
        return manifest, tables
    finally:
        conn.close()


def read_signed_files(path: Path) -> dict[str, dict[str, Any]]:
    """The signed files embedded in a pack, by path (`{kind, version, document}`), each matched against the manifest's
    sha256 for it. Raises `PackRejectedError` when a listed file is missing or changed, or an unlisted one is present
    (the pack is damaged). Each document's own signature is checked by whoever adopts it."""
    conn = open_pack(path)
    try:
        manifest = json.loads(conn.execute("SELECT record FROM manifest WHERE id = 1").fetchone()[0])
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        rows: dict[str, Any] = {}
        if EMBEDDED_TABLE in present:
            query = f"SELECT id, record FROM {EMBEDDED_TABLE}"  # noqa: S608 - a fixed name
            rows = {str(r[0]): json.loads(r[1]) for r in conn.execute(query)}
    finally:
        conn.close()
    found: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("embedded_signed_files", []):
        document = rows.get(entry["path"])
        if document is None or embedded_sha256(document) != entry["sha256"]:
            raise PackRejectedError(f"{path.name}: the embedded file {entry['path']} does not match the manifest")
        found[entry["path"]] = {"kind": entry["kind"], "version": entry["version"], "document": document}
    if set(rows) - set(found):
        raise PackRejectedError(f"{path.name}: an embedded file is not listed in the manifest")
    return found


def to_document(manifest: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The JSON export: the content-pack contract's shape."""
    doc: dict[str, Any] = {"manifest": dict(manifest)}
    for table in TABLES:
        if table in tables:
            doc[table] = sorted(tables[table], key=lambda r: row_id(table, r))
    return doc


def from_document(doc: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    return dict(doc["manifest"]), {table: list(doc[table]) for table in TABLES if table in doc}


# --- verification before load ---------------------------------------------------------------------------------------


def verify_manifest(manifest: Mapping[str, Any], environment: Environment, trust: TrustStore | None = None,
                    name: str = "pack") -> None:  # fmt: skip
    """The manifest's signature, by a content key that was valid when the pack was made (SEC-12)."""
    created = manifest.get("created_at")
    try:
        (trust or bundled_trust()).verify(ARTEFACT, manifest, environment,
                                          signed_at=created if isinstance(created, str) else None)  # fmt: skip
    except SignatureRefused as error:
        raise PackRejectedError(f"{name}: {error}") from error


def verify_tables(manifest: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]], name: str = "pack") -> None:
    listed = {t["name"]: t for t in manifest.get("tables", [])}
    if set(listed) != set(tables):
        raise PackRejectedError(f"{name}: the tables differ from the manifest")
    for table, records in tables.items():
        if table_sha256(table, records) != listed[table]["sha256"] or len(records) != listed[table]["rows"]:
            raise PackRejectedError(f"{name}: table {table} does not match its hash")


def read_manifest(path: Path) -> dict[str, Any]:
    """The manifest alone, after the schema check: what the signature is checked on before any record is read."""
    conn = open_pack(path)
    try:
        problems = schema_problems(conn)
        if problems:
            raise PackRejectedError(f"{path.name}: {'; '.join(problems)}")
        manifest = json.loads(conn.execute("SELECT record FROM manifest WHERE id = 1").fetchone()[0])
    finally:
        conn.close()
    if not isinstance(manifest, dict):
        raise PackRejectedError(f"{path.name}: the manifest is not an object")
    return manifest


def _size_problem(path: Path) -> str | None:
    from agent_core.config.keys import Settings

    limit = int(float(Settings.model_fields["pack_file_max_mb"].default) * 1024 * 1024)
    return f"{path.name}: larger than any pack this app opens" if path.stat().st_size > limit else None


def verify(path: Path, environment: Environment, trust: TrustStore | None = None) -> dict[str, Any]:
    """Verify the size, the schema, the signature and every table hash; returns the manifest. An untrusted file is
    refused by its size before it is opened, and by its manifest's signature before any record is read (SEC-13).
    Offline: nothing is fetched."""
    try:
        too_large = _size_problem(path)
        if too_large:
            raise PackRejectedError(too_large)
        manifest = read_manifest(path)
        verify_manifest(manifest, environment, trust, name=path.name)
        _, tables = read_pack(path)
    except PackRejectedError:
        raise
    except (sqlite3.Error, ValueError, TypeError, OSError) as error:
        raise PackRejectedError(f"{path.name}: unreadable ({type(error).__name__})") from error
    verify_tables(manifest, tables, name=path.name)
    verify_drawings(tables, name=path.name)
    # An embedded file that differs from its manifest entry makes the pack damaged; a mastery-params file that is not
    # valid rejects the pack, so the parameters already active stay in force (LRN-11).
    for entry in read_signed_files(path).values():
        if entry["kind"] == mastery_params.KIND:
            found = mastery_params.problems(entry["document"])
            if found:
                raise PackRejectedError(f"{path.name}: the mastery-params file is not valid ({found[0]})")
    return manifest


def verify_drawings(tables: Mapping[str, list[dict[str, Any]]], name: str = "pack") -> None:
    """Every library drawing is sanitised again at load (VSL-23, SEC-13): a pack whose SVG carries a script, an event
    handler or an outside reference is refused even though it is signed."""
    from agent_core.config.keys import Settings
    from agent_core.store.svg import sanitise_problems

    limit = int(Settings.model_fields["visual_library_svg_max_kb"].default) * 1024
    for record in tables.get("diagrams", []):
        svg = record.get("svg")
        if svg is None:
            continue
        problems = sanitise_problems(str(svg), limit)
        if problems:
            raise PackRejectedError(f"{name}: diagram {record.get('id')}: {problems[0]}")


def verify_for_install(path: Path, environment: Environment, trust: TrustStore | None = None) -> dict[str, Any]:
    """`verify`, then the whole pack against the content-pack contract: pack data passes the same schema validation as
    any other input before it is used (SEC-13). Run once, at install and after a diff, not at every load."""
    from agent_core.contracts import errors as contract_errors

    manifest = verify(path, environment, trust)
    _, tables = read_pack(path)
    problems = contract_errors(CONTRACT, to_document(manifest, tables))
    if problems:
        raise PackRejectedError(f"{path.name}: not a valid content pack ({problems[0]})")
    return manifest


@dataclass
class PackShelf:
    """Installed packs by id: a new version replaces the active one only after it verifies (CUR-08)."""

    environment: Environment
    trust: TrustStore | None = None
    active: dict[str, tuple[Path, dict[str, Any]]] = field(default_factory=dict)
    rejected: list[tuple[Path, str]] = field(default_factory=list)

    def install(self, path: Path) -> bool:
        try:
            manifest = verify_for_install(path, self.environment, self.trust)
        except PackRejectedError as error:
            self.rejected.append((path, str(error)))
            return False
        self.active[manifest["pack_id"]] = (path, manifest)
        return True

    def manifest(self, pack_id: str) -> dict[str, Any] | None:
        entry = self.active.get(pack_id)
        return entry[1] if entry else None


def list_packs(folder: Path, environment: Environment, trust: TrustStore | None = None) -> list[dict[str, Any]]:
    """The packs in `folder` as the pack list shows them (API-29): each verified now, so a damaged or untrusted file is
    listed with `signature_ok` false and its screen shows `pack.damaged` (CUR-08, G51). A file too damaged to read
    names itself from its file name (`pack.phy.o.pack`)."""
    listed = []
    for path in sorted(folder.glob("*.pack")) if folder.is_dir() else []:
        try:
            manifest, ok = verify(path, environment, trust), True
        except PackRejectedError:
            ok = False
            try:
                manifest = read_pack(path)[0]
            except (sqlite3.Error, ValueError, TypeError, IndexError):
                manifest = {}
        pack_id = str(manifest.get("pack_id") or path.stem)[:96]
        parts = pack_id.split(".")
        level = str(manifest.get("level") or (parts[2] if len(parts) > 2 else "o")).upper()
        listed.append({"pack_id": pack_id, "subject": str(manifest.get("subject") or (parts[1] if len(parts) > 1
                       else ""))[:32], "level": level if level in ("O", "A") else "O",
                       "version": str(manifest.get("version") or "")[:32], "signature_ok": ok,
                       "size_bytes": path.stat().st_size})  # fmt: skip
    return listed
