"""What the learner model and memory read from the installed content packs (LRN-05, LRN-10, MEM-04, CUR-08).

A `PackIndex` is built only from packs that verify (signature and every table hash, `store.packs.verify`); a pack
that does not verify is left out and named in `rejected`, so a damaged pack never labels anything. It answers the
projections' `ContentIndex` questions (item and KC difficulty, the prerequisite graph) and the memory queries' label
questions: a KC's statement and topic node, the KCs of a topic, and a misconception's student-facing name. It also
keeps the records the subject map and the lesson player read (UXS-22, UXS-23): every curriculum node, and the KC,
lesson, worked-example, item and misconception records, with the pack each subject came from.

Its `fingerprint` names the exact pack versions it was built from: the projections fold it into their parameter hash
(LRN-02), so installing a different pack version rebuilds them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from agent_core.config.environments import Environment
from agent_core.config.trust import TrustSource, current_trust
from agent_core.store import mastery_params
from agent_core.store.packs import PackRejectedError, read_pack, read_signed_files, verify


@dataclass(frozen=True)
class KcInfo:
    kc_id: str
    statement: str
    node_id: str
    prerequisites: tuple[str, ...]
    misconception_ids: tuple[str, ...]
    difficulty_prior: float | None


@dataclass(frozen=True)
class TopicInfo:
    node_id: str
    title: str
    kc_ids: tuple[str, ...]


@dataclass(frozen=True)
class MisconceptionInfo:
    misconception_id: str
    student_name: str
    kc_ids: tuple[str, ...]


@dataclass(frozen=True)
class PackRef:
    """The installed pack a subject's records came from (UXS-22, UXS-23)."""

    pack_id: str
    version: str


# The convention naming a subject's chart of accounts (CUR-28; `content/curriculum/ref/acc-o/conventions.yaml`).
CHART_CONVENTION = "acc.chart_of_accounts"

# The record tables kept whole for the subject map and the lesson player (UXS-22, UXS-23).
RECORD_TABLES = ("kcs", "lessons", "worked_examples", "items", "misconceptions", "mock_papers", "item_templates")


@dataclass
class PackIndex:
    fingerprint: str = ""
    kcs: dict[str, KcInfo] = field(default_factory=dict)
    topics: dict[str, TopicInfo] = field(default_factory=dict)
    misconceptions: dict[str, MisconceptionInfo] = field(default_factory=dict)
    items: dict[str, float] = field(default_factory=dict)  # item id -> difficulty prior
    # KC migrations of every pack (CUR-10): the projections read old evidence through them (learner.kc_migrations).
    kc_migrations: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    # Every curriculum node by id, and the whole records of RECORD_TABLES by table and id (first pack wins).
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    records: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # The pack each subject node (`cur.<subj>.<lvl>`) came from.
    subject_packs: dict[str, PackRef] = field(default_factory=dict)
    # What visuals draw on, per subject code (VSL-13, VSL-22, VSL-29): library entries with their sanitised SVG,
    # template presets, the pack conventions (sign conventions, g) and pack reference values (N3) by reference.
    visuals: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Charts of accounts by id (`coa.<...>.v<n>`), from the packs' `charts_of_accounts` table (the `pack.ref` chart
    # each accounting pack carries, CUR-07): what an account's class is when a journal entry is read (CUR-18, VER-22).
    charts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The fitted parameters of the packs' mastery-params files (LRN-11): (beta, gamma, rho) per KC, and the
    # mastery model each pack records for its subject (`pfa_elo` or `bkt`, D25). First pack wins.
    kc_params: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    mastery_models: dict[str, str] = field(default_factory=dict)

    def visual_sources(self, subject_code: str) -> dict[str, Any]:
        return self.visuals.get(
            subject_code, {"diagrams": {}, "presets": {}, "conventions": {}, "pack_values": {}, "figures": {}}
        )

    # --- building -------------------------------------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        paths: Iterable[Path],
        environment: Environment,
        trust: TrustSource = None,
    ) -> PackIndex:
        """An index over every pack in `paths` that verifies now; the others are recorded in `rejected`."""
        packs: list[tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]] = []
        documents: list[list[dict[str, Any]]] = []
        rejected: list[tuple[str, str]] = []
        anchors = current_trust(trust)
        for path in sorted(paths):
            try:
                verify(path, environment, anchors)
            except PackRejectedError as error:
                rejected.append((path.name, str(error)))
                continue
            packs.append(read_pack(path))
            documents.append(
                [e["document"] for e in read_signed_files(path).values() if e["kind"] == mastery_params.KIND]
            )
        index = cls.from_tables(packs)
        index.rejected = rejected
        for (manifest, _), docs in zip(packs, documents, strict=True):
            for doc in docs:  # each was checked by `verify`
                index.mastery_models.setdefault(str(manifest.get("pack_id")), mastery_params.model_of(doc))
                for kc, values in mastery_params.kc_overrides(doc).items():
                    index.kc_params.setdefault(kc, values)
        return index

    def kc_overrides(self) -> tuple[tuple[str, float, float, float], ...]:
        """The fitted per-KC values for the learner's mastery parameters (`MasteryParams.kc_overrides`)."""
        return tuple((kc, *values) for kc, values in sorted(self.kc_params.items()))

    @classmethod
    def from_tables(cls, packs: Sequence[tuple[Mapping[str, Any], Mapping[str, list[dict[str, Any]]]]]) -> PackIndex:
        """An index over packs already verified: (manifest, record tables) pairs as `read_pack` returns them. A
        record id present in two packs keeps the first pack's record (packs are ordered by file name)."""
        index = cls()
        versions = []
        for manifest, tables in packs:
            versions.append(f"{manifest.get('pack_id')}@{manifest.get('version')}")
            for record in tables.get("kcs", []):
                kc_id = str(record["id"])
                if kc_id in index.kcs:
                    continue  # a retired KC keeps its record: past evidence still names it
                prior = record.get("difficulty_prior")
                # A prerequisite is a {kc_id, strength} record in the pack; older sources list bare ids.
                needs = tuple(
                    str(p["kc_id"]) if isinstance(p, dict) else str(p) for p in record.get("prerequisites", [])
                )
                index.kcs[kc_id] = KcInfo(kc_id, str(record["statement"]), str(record["node_id"]), needs,
                                          tuple(str(m) for m in record.get("misconception_ids", [])),
                                          float(prior) if isinstance(prior, int | float) else None)  # fmt: skip
            ref = PackRef(str(manifest.get("pack_id") or ""), str(manifest.get("version") or ""))
            for node in tables.get("curriculum_nodes", []):
                index.nodes.setdefault(str(node["id"]), dict(node))
                if node.get("kind") == "subject":
                    index.subject_packs.setdefault(str(node["id"]), ref)
            for table in RECORD_TABLES:
                kept = index.records.setdefault(table, {})
                for record in tables.get(table, []):
                    kept.setdefault(str(record["id"]), dict(record))
            titles = {str(n["id"]): str(n["title"]) for n in tables.get("curriculum_nodes", [])}
            members: dict[str, list[str]] = {}
            for kc in index.kcs.values():
                if kc.node_id in titles:
                    members.setdefault(kc.node_id, []).append(kc.kc_id)
            for node_id, kc_ids in members.items():
                if node_id not in index.topics:
                    index.topics[node_id] = TopicInfo(node_id, titles[node_id], tuple(sorted(kc_ids)))
            for record in tables.get("misconceptions", []):
                mc_id = str(record["id"])
                if mc_id not in index.misconceptions:
                    linked = tuple(str(k) for k in record["kc_ids"])
                    index.misconceptions[mc_id] = MisconceptionInfo(mc_id, str(record["student_name"]), linked)
            index.kc_migrations += [dict(r) for r in tables.get("kc_migrations", [])]
            for record in tables.get("items", []):
                prior = (record.get("difficulty") or {}).get("prior")
                if isinstance(prior, int | float):
                    index.items.setdefault(str(record["id"]), float(prior))
            index._add_visuals(manifest, tables)
            for chart in tables.get("charts_of_accounts", []):
                index.charts.setdefault(str(chart["id"]), dict(chart))
        if versions:
            index.fingerprint = hashlib.sha256("\n".join(sorted(versions)).encode()).hexdigest()
        return index

    def _add_visuals(self, manifest: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]]) -> None:
        pack_id = str(manifest.get("pack_id"))
        found = self.visuals.setdefault(
            str(manifest.get("subject")),
            {"diagrams": {}, "presets": {}, "conventions": {}, "pack_values": {}, "figures": {}},
        )
        for record in tables.get("diagrams", []):
            if "svg" in record:  # a drawable library entry (a bare reference has nothing to draw)
                found["diagrams"].setdefault(str(record["id"]), dict(record))
        for record in tables.get("visual_presets", []):
            found["presets"].setdefault(str(record["id"]), {**record, "pack_id": pack_id})
        # A worked example's checked figure is the pattern for that KC's figures (kind, params and parts), which the
        # Coach is shown so a new problem on the KC can be drawn with its own numbers (VSL-12 still checks them).
        for record in tables.get("worked_examples", []):
            resolved = (record.get("visual") or {}).get("resolved")
            if not isinstance(resolved, dict):
                continue
            parts = [str(p["id"]) for p in resolved.get("parts", []) if p.get("focusable", True)]
            pattern = {"source": str(record["id"]), "type": str(resolved["type"]),
                       "params": resolved.get("params", {}), "parts": parts}  # fmt: skip
            for kc_id in record.get("kc_ids", []):
                found["figures"].setdefault(str(kc_id), pattern)
        for record in tables.get("conventions", []):
            key, value = str(record["key"]), str(record["value"])
            found["conventions"].setdefault(key, value)
            try:
                found["pack_values"][f"pack:{pack_id}/conventions/{key}"] = Fraction(value)
            except (ValueError, ZeroDivisionError):
                continue

    # --- the projections' ContentIndex (learner.projections) ------------------------------------------------------

    def item_difficulty(self, item_id: str | None) -> float:
        return self.items.get(item_id, 0.0) if item_id is not None else 0.0

    def reference_difficulty(self, kc_id: str) -> float:
        kc = self.kcs.get(kc_id)
        return kc.difficulty_prior if kc is not None and kc.difficulty_prior is not None else 0.0

    def prerequisites(self) -> Mapping[str, Sequence[str]]:
        return {kc_id: list(kc.prerequisites) for kc_id, kc in self.kcs.items() if kc.prerequisites}

    def dependents(self) -> Mapping[str, Sequence[str]]:
        out: dict[str, list[str]] = {}
        for kc_id, kc in sorted(self.kcs.items()):
            for need in kc.prerequisites:
                out.setdefault(need, []).append(kc_id)
        return out

    def item_median_ms(self, item_id: str | None) -> int | None:
        """The item's median response time for the LRN-06 time modifiers: the pack's `estimated_seconds` until item
        statistics exist (CUR-26); None when the item has no estimate."""
        record = self.record("items", item_id) if item_id is not None else None
        seconds = (record or {}).get("estimated_seconds")
        return int(seconds * 1000) if isinstance(seconds, int | float) and seconds > 0 else None

    def item_options(self, item_id: str | None) -> int | None:
        """The number of options of a multiple-choice item (the LRN-06 MCQ modifier); None for any other item."""
        record = self.record("items", item_id) if item_id is not None else None
        if not record or record.get("type") not in ("mcq", "mcq_diagnostic"):
            return None
        parts = record.get("parts") or [{}]
        options = ((parts[0].get("answer") or {}).get("options")) or []
        return len(options) or None

    # --- what the planner and the adaptation policies read (LRN-24, LRN-28) -------------------------------------------

    def kc_record(self, kc_id: str) -> dict[str, Any]:
        return self.record("kcs", kc_id) or {}

    def exam_weight(self, kc_id: str) -> float:
        """A KC's exam weight (the pack's `exam_weight`, 0 to 1); 0 when the pack does not have the KC."""
        weight = self.kc_record(kc_id).get("exam_weight")
        return float(weight) if isinstance(weight, int | float) else 0.0

    def confusable_with(self, kc_id: str) -> tuple[str, ...]:
        """KCs the pack marks confusable with this one, in both directions (CUR-04, LRN-24)."""
        own = {str(k) for k in self.kc_record(kc_id).get("confusable_with", [])}
        back = {k for k, r in self.records.get("kcs", {}).items() if kc_id in r.get("confusable_with", [])}
        return tuple(sorted((own | back) - {kc_id}))

    def kc_kind(self, kc_id: str) -> str | None:
        kind = self.kc_record(kc_id).get("kind")
        return str(kind) if kind is not None else None

    # --- labels for the memory queries (memory.queries.ContentLabels) ----------------------------------------------

    def kc_statement(self, kc_id: str) -> str | None:
        kc = self.kcs.get(kc_id)
        return kc.statement if kc is not None else None

    def topic_of(self, kc_id: str) -> str | None:
        """The KC's curriculum node id (its topic), when that node is in an installed pack."""
        kc = self.kcs.get(kc_id)
        return kc.node_id if kc is not None and kc.node_id in self.topics else None

    def topic_title(self, node_id: str) -> str | None:
        topic = self.topics.get(node_id)
        return topic.title if topic is not None else None

    def kcs_in_topic(self, topic_id: str) -> Sequence[str]:
        topic = self.topics.get(topic_id)
        return topic.kc_ids if topic is not None else ()

    def chart_of_accounts(self, subject_code: str, chart_id: str | None = None) -> dict[str, Any] | None:
        """A chart of accounts as the accounting checkers read it (`{"id", "accounts"}`): the one named, else the one
        the subject's pack conventions name (`acc.chart_of_accounts`, CUR-28); None when no installed pack has it."""
        named = chart_id or self.visual_sources(subject_code)["conventions"].get(CHART_CONVENTION)
        return self.charts.get(str(named)) if named else None

    def misconception_name(self, misconception_id: str) -> str | None:
        found = self.misconceptions.get(misconception_id)
        return found.student_name if found is not None else None

    # --- records for the subject map and the lesson player (UXS-22, UXS-23) -----------------------------------------

    def record(self, table: str, record_id: str) -> dict[str, Any] | None:
        """One whole record of a RECORD_TABLES table, or None when no installed pack has it."""
        return self.records.get(table, {}).get(record_id)

    def table(self, table: str) -> list[dict[str, Any]]:
        """Every record of one RECORD_TABLES table, in id order."""
        return [self.records.get(table, {})[k] for k in sorted(self.records.get(table, {}))]
