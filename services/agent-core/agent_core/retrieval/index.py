"""The on-device search index over the installed content packs (CUR-30, MEM-21, TOL-21, TOL-29).

`KnowledgeIndex` is built only from packs that verify now (signature and every table hash, CUR-08); a pack that does
not verify is left out and named in `rejected`. It reads each pack's `search_fts` rows (or derives the same passages
from the verified records of a pack built before that table, `retrieval.passages`) into an in-memory SQLite FTS5
index, so the pack file itself never holds a virtual table (SEC-13). Curriculum nodes and KCs are indexed beside the
passages for `curriculum_lookup` queries and topic classification.

Ranking is BM25 first. When an embedding model is installed (`retrieval.embedding.Embedder`) and a pack's vectors were
built with exactly that model, the BM25 and vector rankings of that pack's passages are fused by reciprocal rank;
a pack whose model differs is searched by keyword only and the mismatch is logged (CUR-30). No call reaches the
network: everything here reads the verified pack files and memory.

Retrieval for a topic (`scope`, `grounding`) returns only passages of that topic, or passages about a KC listed as a
prerequisite of one of its KCs (MEM-21).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.config.environments import Environment
from agent_core.config.trust import TrustStore
from agent_core.retrieval.embedding import Embedder, EmbeddingModel, cosine, decode, unit
from agent_core.retrieval.passages import Passage, derive, topic_of_node
from agent_core.store.packs import PackRejectedError, read_pack, verify

log = logging.getLogger(__name__)

SUBJECT_SLUGS = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}
SUBJECT_NAMES = {slug: name for name, slug in SUBJECT_SLUGS.items()}
_WORD = re.compile(r"[^\W_]+")
MAX_QUERY_TERMS = 40
# BM25 column weights: (title, text).
_WEIGHTS = (0.5, 1.0)


@dataclass(frozen=True)
class PackRef:
    pack_id: str
    version: str
    subject: str  # the pack's subject code (`phy`)
    level: str  # `o` or `a`
    embedding_model: EmbeddingModel | None
    vectors_used: bool


@dataclass(frozen=True)
class NodeRecord:
    id: str
    code: str
    title: str
    kind: str
    parent_id: str | None
    topic_id: str | None
    description: str
    pack_id: str
    version: str


@dataclass(frozen=True)
class KcRecord:
    id: str
    code: str
    statement: str
    kind: str
    node_id: str
    topic_id: str | None
    prerequisites: tuple[tuple[str, str], ...]  # (prerequisite KC id, strength)
    misconception_ids: tuple[str, ...]
    pack_id: str
    version: str


@dataclass(frozen=True)
class MisconceptionRecord:
    id: str
    student_name: str
    signature_kinds: tuple[str, ...]
    kc_ids: tuple[str, ...]
    pack_id: str
    version: str
    root_kc_id: str | None = None  # the earlier skill this misconception really comes from (pack `root_gap`)
    root_idea: str | None = None  # that skill as a short phrase: "This is really about {root_idea}."


@dataclass(frozen=True)
class Hit:
    passage: Passage
    rank: int  # 1 is the best
    score: float


def terms(query: str) -> list[str]:
    """The query's distinct words, lower case, in order, at most `MAX_QUERY_TERMS`."""
    out: list[str] = []
    for word in _WORD.findall(query.lower()):
        if word not in out:
            out.append(word)
    return out[:MAX_QUERY_TERMS]


def match_expression(query: str) -> str | None:
    """An FTS5 query matching any of the words, each quoted so no word is read as syntax; None for no words."""
    words = terms(query)
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words) if words else None


@dataclass
class KnowledgeIndex:
    settings: Settings = field(default_factory=Settings)
    embedder: Embedder | None = None
    packs: dict[str, PackRef] = field(default_factory=dict)
    passages: list[Passage] = field(default_factory=list)  # rowid = position + 1
    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    kcs: dict[str, KcRecord] = field(default_factory=dict)
    misconceptions: dict[str, MisconceptionRecord] = field(default_factory=dict)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (file name, reason)
    mismatches: list[str] = field(default_factory=list)  # packs searched by keyword only (CUR-30)
    _db: sqlite3.Connection = field(init=False, repr=False)
    _entries: list[tuple[str, str]] = field(default_factory=list, init=False, repr=False)  # (kind, id) per rowid
    _vectors: dict[int, tuple[float, ...]] = field(default_factory=dict, init=False, repr=False)
    _by_id: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _facets: dict[tuple[str, str], frozenset[int]] = field(default_factory=dict, init=False, repr=False)
    _by_topic: dict[str, set[int]] = field(default_factory=dict, init=False, repr=False)
    _by_kc: dict[str, set[int]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        tokenizer = "porter unicode61 remove_diacritics 2"
        self._db.execute(f"CREATE VIRTUAL TABLE passages_fts USING fts5(title, text, tokenize='{tokenizer}')")
        self._db.execute(f"CREATE VIRTUAL TABLE curriculum_fts USING fts5(title, text, tokenize='{tokenizer}')")

    # --- building -------------------------------------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        paths: Iterable[Path],
        environment: Environment,
        trust: TrustStore | None = None,
        settings: Settings | None = None,
        embedder: Embedder | None = None,
    ) -> KnowledgeIndex:
        """An index over every pack in `paths` that verifies now (CUR-08); the others are recorded in `rejected`."""
        index = cls(settings or Settings(), embedder)
        for path in sorted(paths):
            try:
                verify(path, environment, trust)
                manifest, tables = read_pack(path)
            except PackRejectedError as error:
                index.rejected.append((path.name, str(error)))
                continue
            index.add(manifest, tables)
        return index

    @classmethod
    def from_tables(
        cls,
        packs: Sequence[tuple[Mapping[str, Any], Mapping[str, Sequence[Mapping[str, Any]]]]],
        settings: Settings | None = None,
        embedder: Embedder | None = None,
    ) -> KnowledgeIndex:
        """An index over packs already verified: (manifest, record tables) pairs as `read_pack` returns them."""
        index = cls(settings or Settings(), embedder)
        for manifest, tables in packs:
            index.add(manifest, tables)
        return index

    def add(self, manifest: Mapping[str, Any], tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        """One verified pack. A record id already indexed from another pack keeps the first pack's record."""
        pack_id, version = str(manifest["pack_id"]), str(manifest.get("version", ""))
        if pack_id in self.packs:
            return
        model = EmbeddingModel.from_manifest(manifest.get("embedding_model"))
        rows = tables.get("search_fts")
        found = (
            [Passage.from_record(r, pack_id, version) for r in rows]
            if rows is not None
            else [replace(p, pack_id=pack_id, pack_version=version) for p in derive(tables)]
        )
        vectors = self._pack_vectors(pack_id, model, tables.get("search_vectors"))  # fmt: skip
        self.packs[pack_id] = PackRef(pack_id, version, str(manifest.get("subject") or ""),
                                      str(manifest.get("level") or ""), model, bool(vectors))  # fmt: skip
        for passage in found:
            rowid = len(self.passages) + 1
            self.passages.append(passage)
            self._by_id[(pack_id, passage.id)] = rowid
            for topic in passage.topic_ids:
                self._by_topic.setdefault(topic, set()).add(rowid)
            for kc_id in passage.kc_ids:
                self._by_kc.setdefault(kc_id, set()).add(rowid)
            self._db.execute("INSERT INTO passages_fts (rowid, title, text) VALUES (?, ?, ?)",
                             (rowid, passage.title, passage.text))  # fmt: skip
            if passage.id in vectors:
                self._vectors[rowid] = vectors[passage.id]
        self._add_curriculum(pack_id, version, tables)
        self._facets.clear()

    def _pack_vectors(self, pack_id: str, model: EmbeddingModel | None,
                      rows: Sequence[Mapping[str, Any]] | None) -> dict[str, tuple[float, ...]]:  # fmt: skip
        """The pack's vectors, when its embedding model is the installed one; otherwise none, and a mismatch is
        logged (CUR-30)."""
        if rows is None or model is None:
            return {}
        installed = self.embedder.model if self.embedder is not None else None
        if installed != model:
            have = f"{installed.id}@{installed.version}" if installed else "none"
            reason = (f"{pack_id}: embedding model {model.id}@{model.version} differs from the installed model {have}; "
                      "searching by keyword only")  # fmt: skip
            self.mismatches.append(reason)
            log.warning("pack search: %s", reason)
            return {}
        out = {}
        for row in rows:
            vector = decode(str(row.get("vector", "")), model.dimensions)
            if vector is not None:
                out[str(row["id"])] = unit(vector)
        return out

    def _add_curriculum(self, pack_id: str, version: str, tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        nodes = {str(n["id"]): n for n in tables.get("curriculum_nodes", [])}
        for node_id, node in sorted(nodes.items()):
            if node_id in self.nodes:
                continue
            parent = node.get("parent_id")
            record = NodeRecord(node_id, str(node.get("code", "")), str(node.get("title", "")), str(node["kind"]),
                                str(parent) if parent else None, topic_of_node(nodes, node_id),
                                str(node.get("description") or ""), pack_id, version)  # fmt: skip
            self.nodes[node_id] = record
            self._entry("node", node_id, record.title, record.description)
        for kc in sorted(tables.get("kcs", []), key=lambda r: str(r["id"])):
            kc_id = str(kc["id"])
            if kc_id in self.kcs:
                continue
            needs = tuple((str(p["kc_id"]), str(p.get("strength", ""))) if isinstance(p, Mapping) else (str(p), "")
                          for p in kc.get("prerequisites", []))  # fmt: skip
            record_kc = KcRecord(kc_id, str(kc.get("code", "")), str(kc.get("statement", "")), str(kc.get("kind", "")),
                                 str(kc["node_id"]), topic_of_node(nodes, str(kc["node_id"])), needs,
                                 tuple(str(m) for m in kc.get("misconception_ids", [])), pack_id, version)  # fmt: skip
            self.kcs[kc_id] = record_kc
            self._entry("kc", kc_id, record_kc.code, record_kc.statement)
        for found in tables.get("misconceptions", []):
            mc_id = str(found["id"])
            if mc_id not in self.misconceptions:
                kinds = tuple(sorted({str(s.get("kind", "")) for s in found.get("signatures", []) if s.get("kind")}))
                root_gap = found.get("root_gap")
                gap = root_gap if isinstance(root_gap, dict) else {}
                idea = gap.get("idea") or {}
                self.misconceptions[mc_id] = MisconceptionRecord(
                    mc_id, str(found.get("student_name", "")), kinds, tuple(str(k) for k in found.get("kc_ids", [])),
                    pack_id, version, str(gap["kc_id"]) if gap.get("kc_id") else None,
                    str(idea.get("en", "")).strip() or None if isinstance(idea, dict) else None,
                )  # fmt: skip

    def _entry(self, kind: str, record_id: str, title: str, text: str) -> None:
        self._entries.append((kind, record_id))
        self._db.execute("INSERT INTO curriculum_fts (rowid, title, text) VALUES (?, ?, ?)",
                         (len(self._entries), title, text))  # fmt: skip

    # --- lookups ------------------------------------------------------------------------------------------------

    def passage(self, pack_id: str, passage_id: str) -> Passage | None:
        """The passage a citation names, from the installed pack of that id."""
        rowid = self._by_id.get((pack_id, passage_id))
        return self.passages[rowid - 1] if rowid is not None else None

    def topic_of_kc(self, kc_id: str) -> str | None:
        kc = self.kcs.get(kc_id)
        return kc.topic_id if kc is not None else None

    def topics(self, subject: str | None = None) -> list[str]:
        """Topic ids that have at least one KC, in id order; of one subject code (`phy`) when given."""
        found = {k.topic_id for k in self.kcs.values() if k.topic_id}
        return sorted(t for t in found if subject is None or t.split(".")[1] == subject)

    def kcs_in_topic(self, topic_id: str) -> list[str]:
        return sorted(k.id for k in self.kcs.values() if k.topic_id == topic_id)

    def prerequisite_kcs(self, topic_id: str) -> set[str]:
        """KCs listed as a prerequisite of a KC of the topic and outside it (MEM-21)."""
        mine = set(self.kcs_in_topic(topic_id))
        return {need for k in mine for need, _ in self.kcs[k].prerequisites} - mine

    def scope(self, topic_ids: Collection[str]) -> set[int]:
        """Rows retrieval for these topics may return: their own passages and passages about a listed prerequisite."""
        topics = set(topic_ids)
        needs = set().union(*(self.prerequisite_kcs(t) for t in topics)) if topics else set()
        rows: set[int] = set()
        for topic in topics:
            rows |= self._by_topic.get(topic, set())
        for kc_id in needs:
            rows |= self._by_kc.get(kc_id, set())
        return rows

    # --- search -------------------------------------------------------------------------------------------------

    def _facet(self, name: str, value: str) -> frozenset[int]:
        """Rows by pack subject, pack level or passage kind, computed once per value."""
        key = (name, value)
        if key not in self._facets:

            def of(p: Passage) -> str:
                return {"subject": self.packs[p.pack_id].subject, "level": self.packs[p.pack_id].level,
                        "kind": p.kind}[name]  # fmt: skip

            self._facets[key] = frozenset(n for n, p in enumerate(self.passages, start=1) if of(p) == value)
        return self._facets[key]

    def _rows(self, subjects: Collection[str] | None, levels: Collection[str] | None,
              kinds: Collection[str] | None, scope: set[int] | None) -> set[int] | None:  # fmt: skip
        out = scope
        for name, values in (("subject", subjects), ("level", levels), ("kind", kinds)):
            if values is None:
                continue
            allowed = frozenset().union(*(self._facet(name, v) for v in values))
            out = set(allowed) if out is None else out & allowed
        return out

    def _bm25(self, table: str, query: str, rows: set[int] | None, limit: int) -> list[tuple[int, float]]:
        expression = match_expression(query)
        if expression is None or (rows is not None and not rows):
            return []
        weights = ", ".join(str(w) for w in _WEIGHTS)
        sql = f"SELECT rowid, bm25({table}, {weights}) AS s FROM {table} WHERE {table} MATCH ? ORDER BY s, rowid"  # noqa: S608
        out: list[tuple[int, float]] = []
        for rowid, score in self._db.execute(sql, (expression,)):
            if rows is None or rowid in rows:
                out.append((int(rowid), -float(score)))
                if len(out) >= limit:
                    break
        return out

    def _vector_ranking(self, query: str, rows: set[int] | None, limit: int) -> list[tuple[int, float]]:
        if self.embedder is None or not self._vectors or not query.strip():
            return []
        wanted = unit(self.embedder.embed([query])[0])
        if len(wanted) != self.embedder.model.dimensions:
            return []
        scored = [(rowid, cosine(wanted, vector)) for rowid, vector in self._vectors.items()
                  if rows is None or rowid in rows]  # fmt: skip
        scored.sort(key=lambda r: (-r[1], r[0]))
        return scored[:limit]

    def search(
        self,
        query: str,
        *,
        subjects: Collection[str] | None = None,
        levels: Collection[str] | None = None,
        kinds: Collection[str] | None = None,
        topic_ids: Collection[str] | None = None,
        limit: int = 5,
    ) -> list[Hit]:
        """Passages for `query`, best first: BM25 over the allowed rows, fused with the vector ranking of packs whose
        embedding model is installed. `topic_ids` scopes the search to those topics first (MEM-21)."""
        scope = self.scope(topic_ids) if topic_ids is not None else None
        rows = self._rows(subjects, levels, kinds, scope)
        pool = max(limit, self.settings.retrieval_candidate_pool)
        keyword = self._bm25("passages_fts", query, rows, pool)
        vector = self._vector_ranking(query, rows, pool)
        if not vector:
            ranked = keyword
        else:
            k = self.settings.retrieval_rrf_k
            fused: dict[int, float] = {}
            for ranking in (keyword, vector):
                for position, (rowid, _) in enumerate(ranking, start=1):
                    fused[rowid] = fused.get(rowid, 0.0) + 1.0 / (k + position)
            ranked = sorted(fused.items(), key=lambda r: (-r[1], r[0]))
        return [Hit(self.passages[rowid - 1], n, score) for n, (rowid, score) in enumerate(ranked[:limit], start=1)]

    def grounding(self, topic_ids: Collection[str], query: str, max_chars: int) -> list[Hit]:
        """Grounding for the Coach's topic context (MEM-21): the topic's lesson passages and misconception fixes,
        ranked within the topic by the query, up to `max_chars` of passage text (the tier-5 allowance)."""
        kinds = ("lesson", "misconception")
        scope = self.scope(topic_ids)
        rows = self._rows(None, None, kinds, scope) or set()
        ranked = [h.passage for h in self.search(query, kinds=kinds, topic_ids=topic_ids, limit=len(rows))]
        if not ranked:  # no query words: the topic's passages in pack order, lessons first
            ranked = [self.passages[r - 1] for r in sorted(rows, key=lambda r: (self.passages[r - 1].kind != "lesson",
                                                                                 r))]  # fmt: skip
        out: list[Hit] = []
        used = 0
        for passage in ranked:
            if used + len(passage.text) > max_chars and out:
                break
            used += len(passage.text)
            out.append(Hit(passage, len(out) + 1, 0.0))
        return out

    def curriculum_search(
        self, query: str, *, subjects: Collection[str] | None = None, levels: Collection[str] | None = None,
        limit: int = 8,
    ) -> list[tuple[str, str, float]]:  # fmt: skip
        """Curriculum nodes and KCs for a free-text query, best first: (kind, id, score)."""
        rows = None
        if subjects is not None or levels is not None:
            rows = set()
            for rowid, (_, record_id) in enumerate(self._entries, start=1):
                parts = record_id.split(".")
                if (subjects is None or parts[1] in subjects) and (levels is None or parts[2] in levels):
                    rows.add(rowid)
        return [(*self._entries[rowid - 1], score) for rowid, score in self._bm25("curriculum_fts", query, rows, limit)]

    def topic_scores(
        self, query: str, *, subjects: Collection[str] | None = None, limit: int = 8
    ) -> list[tuple[str, float]]:
        """Topics for a free-text question, best first (EVL-13 candidates): the reciprocal-rank sum of the topics of
        the best-matching passages and curriculum entries."""
        k = self.settings.retrieval_rrf_k
        pool = self.settings.retrieval_candidate_pool
        scores: dict[str, float] = {}
        for position, hit in enumerate(self.search(query, subjects=subjects, limit=pool), start=1):
            for topic in hit.passage.topic_ids:
                scores[topic] = scores.get(topic, 0.0) + 1.0 / (k + position)
        for position, (kind, record_id, _) in enumerate(self.curriculum_search(query, subjects=subjects, limit=pool),
                                                        start=1):  # fmt: skip
            owner = self.nodes[record_id].topic_id if kind == "node" else self.kcs[record_id].topic_id
            if owner:
                scores[owner] = scores.get(owner, 0.0) + 1.0 / (k + position)
        return sorted(scores.items(), key=lambda r: (-r[1], r[0]))[:limit]
