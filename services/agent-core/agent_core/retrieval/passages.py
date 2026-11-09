"""Search passages of a content pack (CUR-30, MEM-21).

A passage is one searchable span of a pack record: a text block of a lesson, a worked example (its problem and the
reasons of its steps), a misconception (what it is and its fix) or an item stem (the stem and its part prompts, never
hints, answers or solutions). Each carries the record it came from, its KCs and its topics, so retrieval can be scoped
to a topic first (MEM-21) and every result can be cited by pack id, version and passage id (TOL-21).

A topic is a curriculum node of kind `topic` (CUR-03): a KC hangs from a topic or from one of its subtopics, and its
topic is the nearest `topic` node at or above that node. Passages are derived the same way by the build, which writes
them as the pack's `search_fts` table, and on the device from a verified pack that predates that table, so both give
the same passages.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

KINDS = ("lesson", "worked_example", "misconception", "item")
SOURCE_TABLE = {"lesson": "lessons", "worked_example": "worked_examples", "misconception": "misconceptions",
                "item": "items"}  # fmt: skip
LANGUAGE = "en"  # the language passages are built in; packs carry their text per language


@dataclass(frozen=True)
class Passage:
    """One passage as the `search_fts` table stores it (`record`), with the pack it came from once installed."""

    id: str
    kind: str
    record_id: str
    title: str
    text: str
    kc_ids: tuple[str, ...]
    topic_ids: tuple[str, ...]
    pack_id: str = ""
    pack_version: str = ""

    def record(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "record_id": self.record_id, "title": self.title,
                "text": self.text, "kc_ids": list(self.kc_ids), "topic_ids": list(self.topic_ids)}  # fmt: skip

    @classmethod
    def from_record(cls, record: Mapping[str, Any], pack_id: str = "", pack_version: str = "") -> Passage:
        return cls(str(record["id"]), str(record["kind"]), str(record["record_id"]), str(record.get("title") or ""),
                   str(record["text"]), tuple(str(k) for k in record.get("kc_ids", [])),
                   tuple(str(t) for t in record.get("topic_ids", [])), pack_id, pack_version)  # fmt: skip


def topic_of_node(nodes: Mapping[str, Mapping[str, Any]], node_id: str) -> str | None:
    """The nearest node of kind `topic` at or above `node_id` (CUR-03); None when the chain has none."""
    seen: set[str] = set()
    current: str | None = node_id
    while current is not None and current in nodes and current not in seen:
        seen.add(current)
        node = nodes[current]
        if node.get("kind") == "topic":
            return current
        parent = node.get("parent_id")
        current = str(parent) if parent else None
    return None


def kc_topics(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, str]:
    """KC id -> topic id, for every KC of the pack whose node chain reaches a topic."""
    nodes = {str(n["id"]): n for n in tables.get("curriculum_nodes", [])}
    out = {}
    for kc in tables.get("kcs", []):
        topic = topic_of_node(nodes, str(kc["node_id"]))
        if topic is not None:
            out[str(kc["id"])] = topic
    return out


def _text(value: Any) -> str:
    """A localised text field (`{"en": ...}`) or a plain string, in the passage language."""
    if isinstance(value, Mapping):
        found = value.get(LANGUAGE)
        if found is None and value:
            found = next(iter(value.values()))
        return str(found or "")
    return str(value or "")


def _join(parts: Iterable[str]) -> str:
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _topics(kc_ids: Iterable[str], topics: Mapping[str, str], extra: Iterable[str] = ()) -> tuple[str, ...]:
    return tuple(sorted({*(topics[k] for k in kc_ids if k in topics), *extra}))


def derive(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[Passage]:
    """Every passage of a pack's record tables, ordered by passage id (the order the pack's table stores them)."""
    nodes = {str(n["id"]): n for n in tables.get("curriculum_nodes", [])}
    topics = kc_topics(tables)
    out: list[Passage] = []
    for lesson in sorted(tables.get("lessons", []), key=lambda r: str(r["id"])):
        kcs = tuple(str(k) for k in lesson.get("kc_ids", []))
        own = topic_of_node(nodes, str(lesson.get("node_id") or ""))
        scope = _topics(kcs, topics, [own] if own else [])
        for n, block in enumerate(lesson.get("blocks", []), start=1):
            text = _text(block.get("text"))
            if text.strip():
                out.append(Passage(f"{lesson['id']}#{n}", "lesson", str(lesson["id"]), str(lesson.get("title", "")),
                                   text.strip(), kcs, scope))  # fmt: skip
    for example in sorted(tables.get("worked_examples", []), key=lambda r: str(r["id"])):
        kcs = tuple(str(k) for k in example.get("kc_ids", []))
        steps = [f"{s.get('reason', '')}: {s.get('latex', '')}" for s in example.get("steps", [])]
        text = _join([_text(example.get("problem")), *steps])
        out.append(Passage(f"{example['id']}#1", "worked_example", str(example["id"]), "", text, kcs,
                           _topics(kcs, topics)))  # fmt: skip
    for found in sorted(tables.get("misconceptions", []), key=lambda r: str(r["id"])):
        kcs = tuple(str(k) for k in found.get("kc_ids", []))
        text = _join([_text(found.get("description")), _text((found.get("fix") or {}).get("text"))])
        out.append(Passage(f"{found['id']}#1", "misconception", str(found["id"]), str(found.get("student_name", "")),
                           text, kcs, _topics(kcs, topics)))  # fmt: skip
    for item in sorted(tables.get("items", []), key=lambda r: str(r["id"])):
        kcs = tuple(str(k) for k in item.get("kc_ids", []))
        text = _join([_text(item.get("stem")), *(_text(p.get("prompt")) for p in item.get("parts", []))])
        if text:
            out.append(Passage(f"{item['id']}#1", "item", str(item["id"]), "", text, kcs, _topics(kcs, topics)))
    return sorted(out, key=lambda p: p.id)


def search_fts_table(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """The pack's `search_fts` rows, as the build writes them (CUR-30)."""
    return [p.record() for p in derive(tables)]
