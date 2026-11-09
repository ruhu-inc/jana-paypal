"""KC migrations in the learner model (CUR-10, LRN-02, DAT-05).

A content pack's major version may split, merge or retire knowledge components; its `kc_migrations` table maps each
old KC to new KCs with weights that sum to one. The event log is never rewritten: the projections read every event
through the migrations, so a rebuild folds old evidence onto the new KCs and no student loses any of it.

How an event's KC references are read through a migration:

- weighted tags (an attempt's `kc_ids`): each old KC's weight is shared among its targets by the migration weights,
  and tags that land on the same KC are added;
- KC lists (`kc_ids` of a hint or a note): every target, in order, once;
- per-KC state events (a mastery check, a review, a diagnostic item): the event counts for each target KC;
- a single KC an event blames or names (a verdict's `first_error_kc`, a misconception match, a detour): the target
  with the largest weight (ties: the smallest id), so one error is never counted twice.

Chains (A split into B and C, B later split into D and E) are followed to the KCs that do not migrate further. The
migrations' fingerprint joins the projections' parameter hash, so new migrations rebuild the projections.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from agent_core.store.events import StoredEvent, kc_ids_of

FAN_OUT = frozenset({"mastery_check", "review", "diagnostic_item"})
HEAVIEST = {"verdict": ("first_error_kc",), "misconception_evidence": ("kc_id",),
            "detour": ("kc_id", "prerequisite_kc_id")}  # fmt: skip
LISTS = frozenset({"hint", "note_written"})


class MigrationError(ValueError):
    """The migration table is inconsistent: a cycle, or weights that do not sum to one."""


@dataclass(frozen=True)
class KcMigrations:
    targets: Mapping[str, tuple[tuple[str, float], ...]]  # source KC -> final KCs with weights summing to 1
    fingerprint: str

    @staticmethod
    def from_rows(rows: Iterable[Mapping[str, Any]]) -> KcMigrations:
        rows = list(rows)
        direct: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            source = str(row["from_kc_id"])
            weights = [(str(t["kc_id"]), float(t["weight"])) for t in row["to"]]
            total = sum(w for _, w in weights)
            if not weights or abs(total - 1.0) > 1e-6:
                raise MigrationError(f"{source}: migration weights sum to {total:g}, not 1")
            direct[source] = weights
        resolved: dict[str, tuple[tuple[str, float], ...]] = {}

        def resolve(kc: str, path: tuple[str, ...]) -> list[tuple[str, float]]:
            if kc not in direct:
                return [(kc, 1.0)]
            if kc in path:
                raise MigrationError(f"KC migrations form a cycle through {kc}")
            if kc not in resolved:
                merged: dict[str, float] = {}
                for target, weight in direct[kc]:
                    for final, share in resolve(target, (*path, kc)):
                        merged[final] = merged.get(final, 0.0) + weight * share
                resolved[kc] = tuple(sorted(merged.items()))
            return list(resolved[kc])

        for source in direct:
            resolve(source, ())
        canonical = json.dumps(sorted(rows, key=lambda r: (str(r["from_kc_id"]), str(r["from_version"]))),
                               sort_keys=True, separators=(",", ":"))  # fmt: skip
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest() if rows else ""
        return KcMigrations(resolved, fingerprint)

    def __bool__(self) -> bool:
        return bool(self.targets)

    # --- one KC reference ---------------------------------------------------------------------------------------

    def fan_out(self, kc_id: str) -> list[str]:
        return [kc for kc, _ in self.targets.get(kc_id, ((kc_id, 1.0),))]

    def heaviest(self, kc_id: str) -> str:
        targets = self.targets.get(kc_id)
        if not targets:
            return kc_id
        return min(targets, key=lambda t: (-t[1], t[0]))[0]

    def weighted(self, tags: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, float] = {}
        for tag in tags:
            for kc, share in self.targets.get(str(tag["kc_id"]), ((str(tag["kc_id"]), 1.0),)):
                merged[kc] = merged.get(kc, 0.0) + float(tag["weight"]) * share
        return [{"kc_id": kc, "weight": weight} for kc, weight in merged.items()]

    def listed(self, kc_ids: Sequence[str]) -> list[str]:
        out: list[str] = []
        for kc in kc_ids:
            out += [k for k in self.fan_out(str(kc)) if k not in out]
        return out

    # --- events ---------------------------------------------------------------------------------------------------

    def touches(self, payload: Mapping[str, Any]) -> bool:
        found = [payload.get(k) for k in ("kc_id", "first_error_kc", "prerequisite_kc_id")]
        for value in payload.get("kc_ids") or []:
            found.append(value.get("kc_id") if isinstance(value, Mapping) else value)
        return any(isinstance(kc, str) and kc in self.targets for kc in found)

    def event(self, event: StoredEvent) -> list[StoredEvent]:
        """The event as the projections read it: unchanged when it names no migrated KC."""
        payload = event.payload
        if not self.targets or event.erased or not self.touches(payload):
            return [event]
        if event.type in FAN_OUT and isinstance(payload.get("kc_id"), str):
            return [self._with(event, {**payload, "kc_id": kc}) for kc in self.fan_out(payload["kc_id"])]
        changed = dict(payload)
        if event.type == "attempt" and isinstance(payload.get("kc_ids"), list):
            changed["kc_ids"] = self.weighted(payload["kc_ids"])
        elif event.type in LISTS and isinstance(payload.get("kc_ids"), list):
            changed["kc_ids"] = self.listed(payload["kc_ids"])
        for key in HEAVIEST.get(event.type, ()):
            if isinstance(payload.get(key), str):
                changed[key] = self.heaviest(payload[key])
        return [self._with(event, changed)]

    @staticmethod
    def _with(event: StoredEvent, payload: dict[str, Any]) -> StoredEvent:
        return replace(event, payload=payload, kc_ids=kc_ids_of(payload))


NO_MIGRATIONS = KcMigrations({}, "")


def migrations_of(content: object) -> KcMigrations:
    """The migrations a content index carries (`kc_migrations` rows, as the pack index collects them), if any."""
    rows = getattr(content, "kc_migrations", None)
    return KcMigrations.from_rows(rows) if rows else NO_MIGRATIONS
