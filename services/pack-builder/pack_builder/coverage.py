"""The coverage report (CUR-31): each column of TABLE CUR-31a per subject-level, from the pack.

The report counts published records, as the app's catalogue does when it decides availability (CUR-01), and the
authoring counts beside them so authors see what is in the pipeline. A subject-level whose target is unmet is not
available: the app hides it, and the signing service refuses to publish it to the pilot or stable channel. The stage
defaults to the first one TABLE CUR-01a produces the subject-level in (S1 for O level Physics, S2 for the others).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_core.store.catalogue import (
    coverage_gaps,
    diagnostic,
    first_stage,
    highest_weight_topics,
    map_gaps,
    misconceptions_needed,
    practice_items,
)

COLUMNS = {
    "S1": (("items", "coverage_s1_items_per_kc"), ("misconceptions", "coverage_s1_misconceptions_per_kc"),
           ("worked_examples", "coverage_s1_worked_examples_per_kc")),
    "S2": (("items", "coverage_s2_items_per_kc"), ("misconceptions", "coverage_s2_misconceptions_per_kc"),
           ("worked_examples", "coverage_s2_worked_examples_per_kc")),
}  # fmt: skip


def _status(record: Mapping[str, Any]) -> str:
    return str((record.get("review") or {}).get("status", "drafted"))


def _counts(records: list[dict[str, Any]], needed: int) -> dict[str, int]:
    return {"published": sum(_status(r) == "published" for r in records), "authored": len(records), "needed": needed}


def default_stage(pack: Mapping[str, Any]) -> str:
    return first_stage(str(pack.get("subject")), str(pack.get("level"))) or "S1"


def report(pack: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]], settings: Any,
           stage: str | None = None) -> dict[str, Any]:  # fmt: skip
    stage = stage or default_stage(pack)
    kcs = [kc for kc in tables.get("kcs", []) if kc.get("status") == "active"]
    nodes = tables.get("curriculum_nodes", [])
    node_ids = {n["id"] for n in nodes}
    practice = practice_items(tables) if stage == "S2" else tables.get("items", [])
    per_kc = []
    for kc in kcs:
        row: dict[str, Any] = {"kc_id": kc["id"]}
        for table, key in COLUMNS.get(stage, ()):
            needed = misconceptions_needed(kc, settings) if (stage, table) == ("S2", "misconceptions") else getattr(
                settings, key)  # fmt: skip
            records = practice if table == "items" else tables.get(table, [])
            row[table] = _counts([r for r in records if kc["id"] in r.get("kc_ids", [])], needed)
        if stage == "S2":
            assessed = [i for i in practice if kc["id"] in i.get("kc_ids", []) and diagnostic(i)]
            row["diagnostic_items"] = _counts(assessed, settings.coverage_s2_diagnostic_items_per_kc)
        per_kc.append(row)
    lessons = tables.get("lessons", [])
    papers = tables.get("mock_papers", [])
    out: dict[str, Any] = {
        "pack_id": pack["pack_id"],
        "stage": stage,
        "map": {"active_kcs": len(kcs), "kcs_on_map": sum(kc.get("node_id") in node_ids for kc in kcs),
                "met": bool(kcs) and all(kc.get("node_id") in node_ids for kc in kcs)},
        "per_kc": per_kc,
    }  # fmt: skip
    if stage == "S2":
        out["map"]["met"] = bool(kcs) and not map_gaps(kcs, nodes)
        topics = [t["id"] for t in highest_weight_topics(nodes, settings.coverage_s2_lesson_topics)]
        out["lessons"] = {**_counts(lessons, len(topics)), "topics": topics}
        out["mock_papers"] = _counts(papers, settings.coverage_s2_mock_papers)
    else:
        out["lessons"] = _counts(lessons, settings.coverage_s1_lessons)
        out["mock_papers"] = {"needed": 0, "note": "no mock papers in S1 (TABLE CUR-31a)"}
    gaps = coverage_gaps(stage, tables, settings)
    return {**out, "gaps": gaps, "available": not gaps}
