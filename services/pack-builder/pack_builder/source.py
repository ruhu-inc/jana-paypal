"""The content source tree (AUT-03): one record per file, in canonical form, as the single source of truth.

A pack's sources live in `content/curriculum/<subject>/<level>/`:

    pack.yaml                         the pack manifest fields an author sets
    map.yaml                          curriculum nodes (a list)
    kcs/<topic>.yaml                  the knowledge components of one topic, with their prerequisite edges (a list)
    items/<id>.yaml                   one record per file; likewise lessons/, worked-examples/, misconceptions/,
                                      templates/, diagrams/ (with their SVG drawings), presets/ (VSL-29)
                                      and mock-papers/ (ASM-15)
and the pack's conventions in `content/curriculum/ref/<subject>-<level>/conventions.yaml` (a list), with its chart of
accounts beside them in `chart-of-accounts.yaml` (a list of charts; the `pack.ref` chart, CUR-07).

Canonical form makes diffs meaningful and saves byte-identical whoever writes the file (the CLI now, the studio later):
keys in the order of the content contract (source-only keys `checks` and `authoring` last), Unicode NFC, LF line ends,
block-style YAML with no line folding. The content sha256 of a record, to which review approvals are bound (AUT-22),
is computed over the canonical record without its `review` summary and `authoring` block.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PER_RECORD: dict[str, str] = {
    "items": "items",
    "lessons": "lessons",
    "worked_examples": "worked-examples",
    "misconceptions": "misconceptions",
    "item_templates": "templates",
    "diagrams": "diagrams",
    "visual_presets": "presets",
    "mock_papers": "mock-papers",
}
DEFINITION = {
    "curriculum_nodes": "CurriculumNode",
    "kcs": "KnowledgeComponent",
    "lessons": "Lesson",
    "worked_examples": "WorkedExample",
    "items": "Item",
    "item_templates": "ItemTemplate",
    "misconceptions": "Misconception",
    "diagrams": "DiagramRef",
    "visual_presets": "VisualPreset",
    "conventions": "Convention",
    "kc_migrations": "KcMigration",
    "mock_papers": "MockPaper",
    "charts_of_accounts": "ChartOfAccounts",  # the pack.ref chart of accounts (CUR-07)
}
CHART_FILE = "chart-of-accounts.yaml"  # beside conventions.yaml in the pack's ref directory
RECORD_TYPE = {
    "curriculum_nodes": "curriculum_node",
    "kcs": "kc",
    "lessons": "lesson",
    "worked_examples": "worked_example",
    "items": "item",
    "item_templates": "item_template",
    "misconceptions": "misconception",
    "diagrams": "diagram",
    "visual_presets": "visual_preset",
    "kc_migrations": "kc_migration",
    "mock_papers": "mock_paper",
}
SOURCE_ONLY = ("checks", "authoring")
NOT_CONTENT = ("review", "authoring")  # excluded from the content sha256 approvals are bound to


class SourceError(ValueError):
    """A source file that is not where the layout says, or not one record."""


# --- canonical form -------------------------------------------------------------------------------------------------


@cache
def _schema() -> dict[str, Any]:
    from agent_core.contracts import document

    return document("content/content-pack")


def _resolve(node: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    seen = 0
    while node is not None and "$ref" in node and seen < 8:
        ref = str(node["$ref"])
        node = _schema()["$defs"].get(ref.rsplit("/", 1)[-1]) if ref.startswith("#/$defs/") else None
        seen += 1
    return node


def _branches(node: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The node and, for a oneOf, anyOf or allOf, each resolved branch, in order."""
    node = _resolve(node)
    if node is None:
        return []
    found = [node]
    for keyword in ("allOf", "oneOf", "anyOf"):
        for branch in node.get(keyword, []):
            found += _branches(branch)
    return found


def _properties(node: Mapping[str, Any] | None) -> list[str]:
    keys: list[str] = []
    for branch in _branches(node):
        keys += [k for k in branch.get("properties", {}) if k not in keys]
    return keys


def _child(node: Mapping[str, Any] | None, key: str | None) -> Mapping[str, Any] | None:
    for branch in _branches(node):
        found = branch.get("items") if key is None else branch.get("properties", {}).get(key)
        if isinstance(found, Mapping) and (_resolve(found) or {}) != {}:
            return found
    return None


def _ordered(value: Any, node: Mapping[str, Any] | None) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, list):
        return [_ordered(v, _child(node, None)) for v in value]
    if isinstance(value, dict):
        known = [k for k in _properties(node) if k in value]
        tail_source = [k for k in SOURCE_ONLY if k in value and k not in known]
        rest = sorted(k for k in value if k not in known and k not in tail_source)
        return {k: _ordered(value[k], _child(node, k)) for k in [*known, *rest, *tail_source]}
    return value


def canonical(value: Any, table: str) -> str:
    """The canonical YAML text of one record (a dict) or one multi-record file (a list) of `table`."""
    node: Mapping[str, Any] = {"$ref": f"#/$defs/{DEFINITION[table]}"}
    ordered = _ordered(value, {"items": node} if isinstance(value, list) else node)
    text = str(yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=1_000_000, default_flow_style=False))
    return text if text.endswith("\n") else text + "\n"


def content_sha256(record: Mapping[str, Any]) -> str:
    """The hash review approvals are bound to: the canonical record without its review summary and authoring."""
    body = {k: v for k, v in record.items() if k not in NOT_CONTENT}
    normal = json.loads(json.dumps(_ordered(body, None), ensure_ascii=False))
    return hashlib.sha256(
        json.dumps(normal, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


# --- the tree -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Located:
    table: str
    record: dict[str, Any]
    path: Path


def ref_dir(pack_dir: Path) -> Path:
    return pack_dir.parent.parent / "ref" / f"{pack_dir.parent.name}-{pack_dir.name}"


def migrations_dir(pack_dir: Path, pack_id: str) -> Path:
    """A pack's KC migrations (CUR-10, AUT-36): `content/curriculum/migrations/<pack id>/<to version>.yaml`, each a
    list. Every migration the pack has ever had stays, so a device that skipped versions can still map old evidence."""
    return pack_dir.parent.parent / "migrations" / pack_id


def topic_of(kc_id: str) -> str:
    parts = kc_id.split(".")
    if len(parts) < 5:
        raise SourceError(f"{kc_id}: a KC id names subject, level, topic and KC")
    return parts[3]


def _load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if hasattr(yaml, "CSafeLoader"):
        return yaml.load(text, Loader=yaml.CSafeLoader)
    return yaml.safe_load(text)


def read_pack(pack_dir: Path) -> tuple[dict[str, Any], dict[str, list[Located]]]:
    """The pack fields and every record of the pack's source tree, by table, in file order."""
    pack = _load(pack_dir / "pack.yaml")
    found: dict[str, list[Located]] = {t: [] for t in DEFINITION}
    map_path = pack_dir / "map.yaml"
    for record in (_load(map_path) or []) if map_path.is_file() else []:
        found["curriculum_nodes"].append(Located("curriculum_nodes", record, map_path))
    for path in sorted((pack_dir / "kcs").glob("*.yaml")):
        for record in _load(path) or []:
            found["kcs"].append(Located("kcs", record, path))
    conventions = ref_dir(pack_dir) / "conventions.yaml"
    for record in (_load(conventions) or []) if conventions.is_file() else []:
        found["conventions"].append(Located("conventions", record, conventions))
    charts = ref_dir(pack_dir) / CHART_FILE
    for record in (_load(charts) or []) if charts.is_file() else []:
        found["charts_of_accounts"].append(Located("charts_of_accounts", record, charts))
    for table, directory in PER_RECORD.items():
        for path in sorted((pack_dir / directory).glob("*.yaml")):
            record = _load(path)
            if not isinstance(record, dict):
                raise SourceError(f"{path}: a {table} file holds exactly one record")
            if record.get("id") is not None and f"{record['id']}.yaml" != path.name:
                raise SourceError(f"{path}: the file name must be the record id ({record['id']}.yaml)")
            found[table].append(Located(table, record, path))
    for path in sorted(migrations_dir(pack_dir, str(pack.get("pack_id"))).glob("*.yaml")):
        for record in _load(path) or []:
            found["kc_migrations"].append(Located("kc_migrations", record, path))
    known = {"pack.yaml", "map.yaml", "README.md", "kcs", *PER_RECORD.values()}
    strays = (p.name for p in pack_dir.iterdir() if p.name not in known and not p.name.startswith(".studio-"))
    for stray in sorted(strays):
        raise SourceError(f"{pack_dir / stray}: not part of the source layout (AUT-03)")
    return pack, found


def record_path(pack_dir: Path, table: str, record: Mapping[str, Any]) -> Path:
    if table in PER_RECORD:
        return pack_dir / PER_RECORD[table] / f"{record['id']}.yaml"
    if table == "kcs":
        return pack_dir / "kcs" / f"{topic_of(str(record['id']))}.yaml"
    if table == "curriculum_nodes":
        return pack_dir / "map.yaml"
    if table == "conventions":
        return ref_dir(pack_dir) / "conventions.yaml"
    if table == "charts_of_accounts":
        return ref_dir(pack_dir) / CHART_FILE
    if table == "kc_migrations":
        return migrations_dir(pack_dir, str(_load(pack_dir / "pack.yaml")["pack_id"])) / f"{record['to_version']}.yaml"
    raise SourceError(f"{table}: no source location in S1")


def write_text(path: Path, text: str) -> None:
    from pack_builder.studio_io import atomic_write

    atomic_write(path, text)


def _identity(table: str, record: Mapping[str, Any]) -> Any:
    if table == "kc_migrations":
        return (record.get("from_kc_id"), record.get("from_version"))
    return record.get("id", record.get("key"))


def save_record(pack_dir: Path, table: str, record: dict[str, Any]) -> Path:
    """Write one record in canonical form; in a multi-record file the record replaces the one with its id."""
    path = record_path(pack_dir, table, record)
    if table in PER_RECORD:
        write_text(path, canonical(record, table))
        return path
    records = [r for r in (_load(path) or [])] if path.is_file() else []
    index = next((i for i, r in enumerate(records) if _identity(table, r) == _identity(table, record)), None)
    if index is None:
        records.append(record)
    else:
        records[index] = record
    write_text(path, canonical(records, table))
    return path


def write_table(pack_dir: Path, table: str, records: Iterable[dict[str, Any]]) -> None:
    """Replace a whole table: every file the table owns is rewritten and files of removed records deleted."""
    records = list(records)
    if table in PER_RECORD:
        directory = pack_dir / PER_RECORD[table]
        keep = set()
        for record in records:
            keep.add(save_record(pack_dir, table, record).name)
        for path in directory.glob("*.yaml") if directory.is_dir() else []:
            if path.name not in keep:
                path.unlink()
        return
    groups: dict[Path, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(record_path(pack_dir, table, record), []).append(record)
    owned = {p for p in (pack_dir / "kcs").glob("*.yaml")} if table == "kcs" else set()
    for path in owned - set(groups):
        path.unlink()
    for path, group in groups.items():
        write_text(path, canonical(group, table))


def format_tree(pack_dir: Path) -> list[Path]:
    """Rewrite every source file of a pack in canonical form; returns the files that changed."""
    changed = []
    _, found = read_pack(pack_dir)
    files: dict[Path, tuple[str, list[dict[str, Any]]]] = {}
    for table, located in found.items():
        for item in located:
            files.setdefault(item.path, (table, []))[1].append(item.record)
    for path, (table, records) in sorted(files.items()):
        text = canonical(records[0], table) if table in PER_RECORD else canonical(records, table)
        if path.read_bytes() != text.encode("utf-8"):
            write_text(path, text)
            changed.append(path)
    return changed
