"""Pack versions, KC migrations and size budgets (CUR-09, CUR-10, CUR-34).

**Versions (CUR-09).** A release compares its tables with the previous release's, record by record. The smallest
version bump the change allows is

- major: a KC removed, retired, split or merged (a new KC migration), or marking semantics changed: an item's type,
  marks, answer, mark points, error carried forward, follow-through recipe (it changes the marks a carried error
  earns, ASM-06) or part dependencies, a template's item type, parameters, constraints or answer tool, or a
  convention's value;
- minor: a record added;
- patch: anything else (corrections, a withdrawn record, an item's `quantities`, which name a misconception in
  feedback and award no marks).

A release with a smaller bump, or a version that does not increase, is refused.

**KC migrations (CUR-10).** A migration maps an old KC to new KCs with weights summing to one. The pack carries every
migration it has ever had, so a device that skipped versions can still map its oldest evidence. Each migration's
targets are active KCs of the pack (or KCs that migrate further), chains end, and no active KC is migrated away. A
retired KC has a migration; so does every KC the previous release had and this one drops or retires.

**Size (CUR-34).** The pack and the update diff are measured against their budgets per release, with the tables that
dominate size (diagrams, then search vectors) listed first. The budgets are a "should": exceeding one is reported, not
refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_core.config import Settings
from agent_core.store import packs
from agent_core.store.packs import canonical, row_id

BUMPS = ("patch", "minor", "major")
WEIGHT_TOLERANCE = 1e-6  # floating-point slack on "weights sum to 1", not a policy value
MB = 1024 * 1024
# Tables measured first: they dominate a pack's size (CUR-34). The search tables join with CUR-30.
SIZE_FIRST = ("diagrams", "search_vectors", "search_fts")

Tables = Mapping[str, Sequence[Mapping[str, Any]]]


def version_tuple(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in str(text).split("."))
    return major, minor, patch


def bump_of(old: str, new: str) -> str | None:
    """The bump from `old` to `new` (major, minor or patch), or None when `new` is not greater."""
    a, b = version_tuple(old), version_tuple(new)
    if b <= a:
        return None
    if b[0] != a[0]:
        return "major"
    return "minor" if b[1] != a[1] else "patch"


# The fields of an item part that decide the marks it awards (CUR-09); `follow_through` is the recipe that awards
# marks for a correct method on a carried error (ASM-06, D90).
MARKING_PART_FIELDS = ("kc_ids", "id", "marks", "answer", "mark_points", "ecf", "follow_through", "uses_parts")


def _marking(table: str, record: Mapping[str, Any]) -> str | None:
    """The part of a record that decides marking, canonical; None for tables without marking semantics."""
    if table == "items":
        parts = [{k: p.get(k) for k in MARKING_PART_FIELDS} for p in record.get("parts", [])]
        return canonical({"type": record.get("type"), "parts": parts,
                          "conventions_override": record.get("conventions_override")})  # fmt: skip
    if table == "item_templates":
        return canonical({k: record.get(k) for k in ("item_type", "parameters", "constraints", "answer_tool")})
    if table == "conventions":
        return canonical({k: record.get(k) for k in ("value", "unit")})
    return None


@dataclass
class SemanticDiff:
    added: dict[str, list[str]] = field(default_factory=dict)
    changed: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    kcs_dropped: list[str] = field(default_factory=list)  # removed or newly retired
    kcs_migrated: list[str] = field(default_factory=list)  # source KCs of migrations new in this release
    marking_changed: list[str] = field(default_factory=list)

    def required_bump(self) -> str:
        if self.kcs_dropped or self.kcs_migrated or self.marking_changed:
            return "major"
        if any(self.added.values()):
            return "minor"
        return "patch"

    def summary(self) -> dict[str, Any]:
        """What changed, for the release record and release notes (AUT-34 builds the notes from this)."""
        return {"required_bump": self.required_bump(),
                "added": {t: ids for t, ids in self.added.items() if ids},
                "changed": {t: ids for t, ids in self.changed.items() if ids},
                "removed": {t: ids for t, ids in self.removed.items() if ids},
                "kcs_dropped": self.kcs_dropped, "kcs_migrated": self.kcs_migrated,
                "marking_changed": self.marking_changed}  # fmt: skip


def semantic_diff(old: Tables, new: Tables) -> SemanticDiff:
    diff = SemanticDiff()
    for table in packs.TABLES:
        before = {row_id(table, r): r for r in old.get(table, [])}
        after = {row_id(table, r): r for r in new.get(table, [])}
        diff.added[table] = sorted(k for k in after if k not in before)
        diff.removed[table] = sorted(k for k in before if k not in after)
        diff.changed[table] = sorted(k for k in after if k in before and canonical(after[k]) != canonical(before[k]))
        for key in diff.changed[table]:
            if _marking(table, before[key]) != _marking(table, after[key]):
                diff.marking_changed.append(key)
        for key in diff.removed[table] if table in ("conventions", "item_templates") else []:
            diff.marking_changed.append(key)  # a convention or template gone changes how answers are marked
    old_kcs = {str(k["id"]): k for k in old.get("kcs", [])}
    new_kcs = {str(k["id"]): k for k in new.get("kcs", [])}
    for kc_id, record in sorted(old_kcs.items()):
        if record.get("status") != "active":
            continue
        if kc_id not in new_kcs or new_kcs[kc_id].get("status") != "active":
            diff.kcs_dropped.append(kc_id)
    old_rows = {row_id("kc_migrations", r) for r in old.get("kc_migrations", [])}
    diff.kcs_migrated = sorted({str(r["from_kc_id"]) for r in new.get("kc_migrations", [])
                                if row_id("kc_migrations", r) not in old_rows})  # fmt: skip
    diff.marking_changed = sorted(set(diff.marking_changed))
    return diff


def version_problems(old_manifest: Mapping[str, Any], new_manifest: Mapping[str, Any], diff: SemanticDiff
                     ) -> list[str]:  # fmt: skip
    """CUR-09: the version increases by at least the bump the change needs."""
    old, new = str(old_manifest["version"]), str(new_manifest["version"])
    bump = bump_of(old, new)
    if bump is None:
        return [f"version {new} is not greater than the released {old}"]
    needed = diff.required_bump()
    if BUMPS.index(bump) < BUMPS.index(needed):
        why = {"major": "a KC is removed, retired, split or merged, or marking semantics changed",
               "minor": "records are added"}[needed]  # fmt: skip
        return [f"{old} → {new} is a {bump} bump, but {why}: it needs a {needed} version (CUR-09)"]
    return []


def _weights_problem(row: Mapping[str, Any]) -> str | None:
    total = sum(float(t["weight"]) for t in row.get("to", []))
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        return f"{row_id('kc_migrations', row)}: weights sum to {total:g}, not 1 (CUR-10)"
    targets = [t["kc_id"] for t in row.get("to", [])]
    if len(set(targets)) != len(targets):
        return f"{row_id('kc_migrations', row)}: a target KC is listed twice"
    return None


def migration_problems(tables: Tables, pack_version: str, previous: Tables | None = None) -> list[str]:
    """CUR-10: the pack's KC migrations are complete and consistent (with the previous release when given)."""
    problems: list[str] = []
    rows = list(tables.get("kc_migrations", []))
    kcs = {str(k["id"]): k for k in tables.get("kcs", [])}
    active = {k for k, r in kcs.items() if r.get("status") == "active"}
    by_source: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        source = str(row["from_kc_id"])
        rid = row_id("kc_migrations", row)
        if source in by_source:
            problems.append(f"{rid}: {source} already has a migration")
        by_source[source] = row
        weights = _weights_problem(row)
        if weights:
            problems.append(weights)
        if source in active:
            problems.append(f"{rid}: {source} is still active in this pack")
        if bump_of(str(row["from_version"]), str(row["to_version"])) is None:
            problems.append(f"{rid}: to_version {row['to_version']} is not after from_version {row['from_version']}")
        elif bump_of(str(row["from_version"]), str(row["to_version"])) != "major":
            problems.append(f"{rid}: a KC migration comes with a major version (CUR-09)")
        if version_tuple(str(row["to_version"])) > version_tuple(pack_version):
            problems.append(f"{rid}: to_version {row['to_version']} is after this pack's version {pack_version}")
    for row in rows:
        for target in row.get("to", []):
            kc = str(target["kc_id"])
            if kc not in active and kc not in by_source:
                problems.append(f"{row_id('kc_migrations', row)}: target {kc} is not an active KC of this pack")
    for source in sorted(by_source):  # every chain ends in active KCs
        seen, frontier = {source}, [source]
        while frontier:
            current = frontier.pop()
            for target in by_source[current].get("to", []) if current in by_source else []:
                kc = str(target["kc_id"])
                if kc in seen:
                    problems.append(f"{source}: its migrations form a cycle through {kc}")
                    frontier = []
                    break
                seen.add(kc)
                if kc in by_source:
                    frontier.append(kc)
    for kc_id, record in sorted(kcs.items()):
        if record.get("status") == "retired" and kc_id not in by_source:
            problems.append(f"{kc_id}: retired without a KC migration (CUR-10)")
    if previous is not None:
        before = {row_id("kc_migrations", r): canonical(r) for r in previous.get("kc_migrations", [])}
        now = {row_id("kc_migrations", r): canonical(r) for r in rows}
        for rid in sorted(before):
            if now.get(rid) != before[rid]:
                problems.append(f"{rid}: a released KC migration was dropped or changed")
        for kc_id in semantic_diff(previous, tables).kcs_dropped:
            if kc_id not in by_source:
                problems.append(f"{kc_id}: dropped from the previous release without a KC migration (CUR-10)")
    return problems


def table_bytes(tables: Tables) -> dict[str, int]:
    return {t: sum(len(canonical(r).encode()) for r in tables.get(t, [])) for t in packs.TABLES}


def size_report(pack_bytes: int, diff_bytes: int | None, tables: Tables, settings: Settings | None = None
                ) -> dict[str, Any]:  # fmt: skip
    """CUR-34: the release's pack and diff sizes against their budgets, the size-dominant tables first."""
    settings = settings or Settings()
    sizes = table_bytes(tables)
    order = [t for t in SIZE_FIRST if t in sizes] + sorted((t for t in sizes if t not in SIZE_FIRST),
                                                           key=lambda t: -sizes[t])  # fmt: skip
    over = []
    if pack_bytes > settings.pack_size_budget_mb * MB:
        over.append(f"the pack is {pack_bytes / MB:.1f} MB (budget {settings.pack_size_budget_mb:g} MB)")
    if diff_bytes is not None and diff_bytes > settings.pack_diff_budget_mb * MB:
        over.append(f"the update diff is {diff_bytes / MB:.1f} MB (budget {settings.pack_diff_budget_mb:g} MB)")
    return {"pack_bytes": pack_bytes, "diff_bytes": diff_bytes,
            "pack_budget_bytes": int(settings.pack_size_budget_mb * MB),
            "diff_budget_bytes": int(settings.pack_diff_budget_mb * MB),
            "tables": [{"name": t, "bytes": sizes[t]} for t in order], "over_budget": over}  # fmt: skip
