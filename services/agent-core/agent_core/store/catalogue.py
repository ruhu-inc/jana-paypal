"""Which subject-level pairs students see (CUR-01, CUR-31).

The catalogue for a stage lists the pairs TABLE CUR-01a produces by that stage. A listed pair is available only when
an installed, verified pack for it meets the stage's coverage target (TABLE CUR-31a), counting published records only.
Targets are defined for S1 and S2; the S3 targets arrive with the content of S3.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_core.store.generated.availability import AVAILABILITY

STAGES = ("S1", "S2", "S3", "S4", "S5")


@dataclass(frozen=True)
class Listing:
    subject: str
    level: str
    stage: str  # the first stage it is produced in
    scope: str | None  # a scope note such as "one topic"


def catalogue(stage: str) -> list[Listing]:
    rank = STAGES.index(stage)
    out = []
    for subject, levels in sorted(AVAILABILITY.items()):
        for level, stages in sorted(levels.items()):
            reached = [(s, note) for s, note in stages if s is not None and STAGES.index(s) <= rank]
            if reached:  # the scope note of the latest stage reached applies (S1 physics: one topic; S2: all)
                out.append(Listing(subject, level, str(reached[0][0]), reached[-1][1]))
    return out


def first_stage(subject: str, level: str) -> str | None:
    """The first stage TABLE CUR-01a produces a subject-level in (None when it is not produced)."""
    stages = [str(s) for s, _ in AVAILABILITY.get(subject, {}).get(level, []) if s is not None]
    return min(stages, key=STAGES.index) if stages else None


def _published(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if (r.get("review") or {}).get("status") == "published"]


def _per_kc(kc_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if kc_id in r.get("kc_ids", [])]


def diagnostic(item: Mapping[str, Any]) -> bool:
    """A diagnostic item (CUR-16): the `mcq_diagnostic` type, or a diagnostic multiple-choice answer."""
    if item.get("type") == "mcq_diagnostic":
        return True
    return any((p.get("answer") or {}).get("diagnostic") is True for p in item.get("parts", []))


def practice_items(tables: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Mock questions stay reserved across papers; they cannot fill a practice coverage target (ASM-22, CUR-31)."""
    reserved = {
        q["item_id"]
        for paper in tables.get("mock_papers", [])
        for section in paper.get("sections", [])
        for q in section.get("questions", [])
    }
    return [item for item in tables.get("items", []) if item["id"] not in reserved]


def coverage_gaps(stage: str, tables: Mapping[str, list[dict[str, Any]]], settings: Any) -> list[str]:
    """What the pack still lacks for the stage's target (an empty list means the target is met)."""
    target = TARGETS.get(stage)
    if target is None:
        return [f"no coverage target is defined for {stage} yet"]
    kcs = [kc for kc in tables.get("kcs", []) if kc["status"] == "active"]
    if not kcs:
        return ["the pack has no knowledge components"]
    return target(kcs, tables, settings)


def _s1_gaps(kcs: list[dict[str, Any]], tables: Mapping[str, list[dict[str, Any]]], settings: Any) -> list[str]:
    items = _published(tables.get("items", []))
    examples = _published(tables.get("worked_examples", []))
    misconceptions = _published(tables.get("misconceptions", []))
    gaps = []
    for kc in kcs:
        counts = {
            "items": (len(_per_kc(kc["id"], items)), settings.coverage_s1_items_per_kc),
            "worked examples": (len(_per_kc(kc["id"], examples)), settings.coverage_s1_worked_examples_per_kc),
            "misconceptions": (len(_per_kc(kc["id"], misconceptions)), settings.coverage_s1_misconceptions_per_kc),
        }
        for what, (have, need) in counts.items():
            if have < need:
                gaps.append(f"{kc['id']}: {have} published {what}, needs {need}")
    lessons = len(_published(tables.get("lessons", [])))
    if lessons < settings.coverage_s1_lessons:
        gaps.append(f"{lessons} published lessons, needs {settings.coverage_s1_lessons}")
    return gaps


def map_gaps(kcs: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> list[str]:
    """The S2 map column, every KC with its prerequisites: each active KC hangs from an active topic or subtopic of
    the map, and each prerequisite in this pack is an active KC (an edge to another pack names that pack in the
    manifest, which the build checks)."""
    homes = {n["id"] for n in nodes if n.get("status", "active") == "active" and n["kind"] != "subject"}
    active = {kc["id"] for kc in kcs}
    gaps = []
    for kc in kcs:
        if kc.get("node_id") not in homes:
            gaps.append(f"{kc['id']}: not on the map")
        here = kc["id"].split(".")[1:3]
        for edge in kc.get("prerequisites", []):
            target = str(edge["kc_id"])
            if target.split(".")[1:3] == here and target not in active:
                gaps.append(f"{kc['id']}: prerequisite {target} is not an active KC of the pack")
    return gaps


def highest_weight_topics(nodes: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """The `count` active topics with the highest exam weight (ties by map order, then id)."""
    topics = [n for n in nodes if n["kind"] == "topic" and n.get("status", "active") == "active"]
    return sorted(topics, key=lambda n: (-float(n.get("exam_weight", 0)), n.get("order", 0), n["id"]))[:count]


def _under(node_id: str | None, topic_id: str, parents: Mapping[str, str | None]) -> bool:
    seen: set[str] = set()
    while node_id is not None and node_id not in seen:
        if node_id == topic_id:
            return True
        seen.add(node_id)
        node_id = parents.get(node_id)
    return False


def lesson_gaps(nodes: list[dict[str, Any]], lessons: list[dict[str, Any]], count: int) -> list[str]:
    """The S2 lessons column: each of the `count` highest-weight topics has a published lesson on it or below it."""
    parents = {n["id"]: n.get("parent_id") for n in nodes}
    published = _published(lessons)
    return [f"{t['id']}: no published lesson (one of the {count} highest-weight topics)"
            for t in highest_weight_topics(nodes, count)
            if not any(_under(lesson.get("node_id"), t["id"], parents) for lesson in published)]  # fmt: skip


def misconceptions_needed(kc: Mapping[str, Any], settings: Any) -> int:
    """S2 asks misconceptions only of KCs at or above the weight threshold (TABLE CUR-31a)."""
    heavy = float(kc.get("exam_weight", 0)) >= settings.coverage_s2_misconception_min_kc_weight
    return int(settings.coverage_s2_misconceptions_per_kc) if heavy else 0


def _s2_gaps(kcs: list[dict[str, Any]], tables: Mapping[str, list[dict[str, Any]]], settings: Any) -> list[str]:
    nodes = tables.get("curriculum_nodes", [])
    gaps = map_gaps(kcs, nodes)
    items = _published(practice_items(tables))
    examples = _published(tables.get("worked_examples", []))
    misconceptions = _published(tables.get("misconceptions", []))
    for kc in kcs:
        assessed = _per_kc(kc["id"], items)
        counts = {
            "items": (len(assessed), settings.coverage_s2_items_per_kc),
            "diagnostic items": (sum(diagnostic(i) for i in assessed), settings.coverage_s2_diagnostic_items_per_kc),
            "misconceptions": (len(_per_kc(kc["id"], misconceptions)), misconceptions_needed(kc, settings)),
            "worked examples": (len(_per_kc(kc["id"], examples)), settings.coverage_s2_worked_examples_per_kc),
        }
        for what, (have, need) in counts.items():
            if have < need:
                gaps.append(f"{kc['id']}: {have} published {what}, needs {need}")
    gaps += lesson_gaps(nodes, tables.get("lessons", []), settings.coverage_s2_lesson_topics)
    papers = len(_published(tables.get("mock_papers", [])))
    if papers < settings.coverage_s2_mock_papers:
        gaps.append(f"{papers} published mock papers, needs {settings.coverage_s2_mock_papers}")
    return gaps


Target = Callable[[list[dict[str, Any]], Mapping[str, list[dict[str, Any]]], Any], list[str]]
TARGETS: dict[str, Target] = {"S1": _s1_gaps, "S2": _s2_gaps}


def available(stage: str, packs: Mapping[str, Mapping[str, list[dict[str, Any]]]], settings: Any) -> list[Listing]:
    """The listed pairs whose installed pack (by pack id) meets the coverage target; the app hides the rest."""
    out = []
    for listing in catalogue(stage):
        tables = packs.get(f"pack.{listing.subject}.{listing.level}")
        if tables is not None and not coverage_gaps(stage, tables, settings):
            out.append(listing)
    return out
