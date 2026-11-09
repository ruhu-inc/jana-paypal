"""Identifiers, the curriculum map, knowledge components and the prerequisite graph (CUR-02 to CUR-05).

The contract's patterns check each id's form; these checks add what a schema cannot: ids belong to the pack's subject
and level, no id appears twice or reuses a retired one, a corrected item names a retired id it replaces (never its
own, CUR-36), the map is a tree with KCs only under topics or subtopics and
topic weights summing to one per subject-level, KC statements start with an observable verb, published KCs have an
item and a worked example, prerequisites form an acyclic graph over known KCs, with cross-pack edges declared, and
board mappings are codes only, consistent down the tree (CUR-06, D113).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from agent_core.store import packs
from agent_core.store.packs import row_id

# Observable-ability verbs a KC statement may start with (our authoring list; extended with review).
VERBS = frozenset(
    {
        "Apply", "Calculate", "Classify", "Compare", "Construct", "Convert", "Define", "Derive", "Describe",
        "Determine", "Distinguish", "Draw", "Estimate", "Evaluate", "Explain", "Find", "Identify", "Interpret",
        "Label", "Measure", "Plot", "Predict", "Read", "Rearrange", "Recall", "Recognise", "Record", "Relate",
        "Select", "Sketch", "Solve", "State", "Use",
    }
)  # fmt: skip
# A board syllabus code (CUR-06): letters, digits and `.`, `(`, `)`, `-`, so no board text fits.
MAPPING_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.()-]{0,39}$")
PREFIX = {"curriculum_nodes": "cur", "kcs": "kc", "lessons": "les", "worked_examples": "wex", "items": "itm",
          "item_templates": "itpl", "misconceptions": "mc", "mock_papers": "mck"}  # fmt: skip


def id_problems(
    pack: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]], retired: Iterable[str] = ()
) -> list[str]:
    problems: list[str] = []
    subject, level = pack.get("subject"), pack.get("level")
    seen: dict[str, str] = {}
    retired_ids = set(retired)
    for table in packs.TABLES:
        for record in tables.get(table, []):
            rid = row_id(table, record)
            if rid in seen:
                problems.append(f"{rid}: duplicate id (in {seen[rid]} and {table})")
            seen[rid] = table
            if rid in retired_ids:
                problems.append(f"{rid}: reuses a retired id")
            if table in PREFIX and subject is not None:
                parts = rid.split(".")
                if parts[0] != PREFIX[table] or parts[1:3] != [subject, level]:
                    problems.append(f"{rid}: not a {PREFIX[table]} id of {subject}.{level}")
    for item in tables.get("items", []):
        corrected = item.get("corrects")
        if corrected is None:
            continue
        if corrected == item.get("id"):
            problems.append(f"{item.get('id')}: corrects its own id (a corrected item has a new id, CUR-02, CUR-36)")
        elif corrected not in retired_ids:
            problems.append(f"{item.get('id')}: corrects {corrected}, which is not a retired id (CUR-02, CUR-36)")
    return problems


def map_problems(nodes: list[dict[str, Any]], kcs: list[dict[str, Any]], tolerance: float) -> list[str]:
    problems: list[str] = []
    by_id = {n["id"]: n for n in nodes}
    allowed_parent = {"subject": (None,), "topic": ("subject",), "subtopic": ("topic", "subtopic")}
    for node in nodes:
        parent = by_id.get(node["parent_id"]) if node["parent_id"] else None
        if node["parent_id"] is not None and parent is None:
            problems.append(f"{node['id']}: orphan (parent {node['parent_id']} is not in the map)")
            continue
        kind = parent["kind"] if parent else None
        if kind not in allowed_parent[node["kind"]]:
            problems.append(f"{node['id']}: a {node['kind']} cannot hang from {kind or 'nothing'}")
        # Walk up: a cycle never reaches a root, and the tree has at most four node levels above the KC.
        depth, visited = 0, set()
        cursor: dict[str, Any] | None = node
        while cursor is not None:
            if cursor["id"] in visited:
                problems.append(f"{node['id']}: the map has a cycle")
                break
            visited.add(cursor["id"])
            depth += 1
            cursor = by_id.get(cursor["parent_id"]) if cursor["parent_id"] else None
        if depth > 4:
            problems.append(f"{node['id']}: more than four node levels")
    for kc in kcs:
        home = by_id.get(kc["node_id"])
        if home is None:
            problems.append(f"{kc['id']}: node {kc['node_id']} is not in the map")
        elif home["kind"] not in ("topic", "subtopic"):
            problems.append(f"{kc['id']}: a KC hangs only from a topic or subtopic, not a {home['kind']}")
    weights: dict[tuple[str, str], float] = {}
    for node in nodes:
        if node["kind"] == "topic" and node.get("status", "active") == "active":
            key = (node["subject"], node["level"])
            weights[key] = weights.get(key, 0.0) + node["exam_weight"]
    for (subject, level), total in sorted(weights.items()):
        if abs(total - 1) > tolerance:
            problems.append(f"{subject}.{level}: topic exam weights sum to {total:.3f}, not 1 ± {tolerance}")
    return problems


def mapping_problems(nodes: list[dict[str, Any]], kcs: list[dict[str, Any]]) -> list[str]:
    """Board mappings are mapping facts only (CUR-06, D113): each code is code-like, no record names one point twice,
    a subtopic's code in a scheme is its topic's code or under it when the topic carries that scheme, and a KC's own
    mapping uses a scheme the map uses."""
    problems: list[str] = []
    by_id = {n["id"]: n for n in nodes}

    def points(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return list(record.get("external_mappings") or [])

    for record in [*nodes, *kcs]:
        seen: set[tuple[str, str]] = set()
        for point in points(record):
            key = (str(point["scheme"]), str(point["code"]))
            if not MAPPING_CODE.match(key[1]):
                problems.append(f"{record['id']}: {key[0]} code {key[1]!r} is not a code (CUR-06: no board text)")
            if key in seen:
                problems.append(f"{record['id']}: maps {key[0]} {key[1]} twice")
            seen.add(key)
    for node in nodes:
        if node["kind"] != "subtopic":
            continue
        # Up to the topic; a cycle (which map_problems reports) stops the walk.
        cursor, visited = by_id.get(node["parent_id"]), {node["id"]}
        while cursor is not None and cursor["kind"] == "subtopic" and cursor["id"] not in visited:
            visited.add(cursor["id"])
            cursor = by_id.get(cursor["parent_id"])
        if cursor is not None and cursor["kind"] == "subtopic":
            continue
        if cursor is None:
            continue
        above: dict[str, list[str]] = {}
        for point in points(cursor):
            above.setdefault(str(point["scheme"]), []).append(str(point["code"]))
        for point in points(node):
            scheme, code = str(point["scheme"]), str(point["code"])
            if scheme in above and not any(code == c or code.startswith(c + ".") for c in above[scheme]):
                topic = ", ".join(above[scheme])
                problems.append(f"{node['id']}: {scheme} code {code} is not under its topic's ({topic})")
    used = {str(p["scheme"]) for n in nodes for p in points(n)}
    for kc in kcs:
        for point in points(kc):
            if str(point["scheme"]) not in used:
                problems.append(f"{kc['id']}: maps {point['scheme']}, a scheme the map does not use")
    return problems


def kc_problems(
    kcs: list[dict[str, Any]],
    items: list[dict[str, Any]],
    examples: list[dict[str, Any]],
    *,
    max_chars: int,
    published: bool,
) -> list[str]:
    """Statements are verb-first and short. In a published pack (pilot or stable channel) every active KC also needs a
    published item and a published worked example; a development pack is a draft and may hold KCs without them."""
    problems = []

    for kc in kcs:
        if kc["status"] != "active":
            continue
        statement = kc["statement"]
        if statement.split(" ", 1)[0] not in VERBS:
            problems.append(f"{kc['id']}: the statement does not start with an observable verb")
        if len(statement) >= max_chars:
            problems.append(f"{kc['id']}: the statement has {len(statement)} characters (under {max_chars})")
        if not published:
            continue
        if not any(kc["id"] in i["kc_ids"] and i["review"]["status"] == "published" for i in items):
            problems.append(f"{kc['id']}: no published item assesses it")
        if not any(kc["id"] in e["kc_ids"] and e["review"]["status"] == "published" for e in examples):
            problems.append(f"{kc['id']}: no published worked example shows it")
    return problems


def root_gap_problems(misconceptions: list[dict[str, Any]], kcs: list[dict[str, Any]],
                      pack: Mapping[str, Any]) -> list[str]:  # fmt: skip
    """A misconception's root gap names an earlier skill: in this pack an active KC that is not the misconception's
    own; in another pack a soft link, which the engine follows only when that pack is installed (so it needs no
    declared dependency) and which the repository's tests check against that pack's sources."""
    here = f"{pack.get('subject')}.{pack.get('level')}"
    status = {kc["id"]: kc["status"] for kc in kcs}
    problems = []
    for record in misconceptions:
        gap = record.get("root_gap")
        if not gap:
            continue
        target = gap["kc_id"]
        if target in record["kc_ids"]:
            problems.append(f"{record['id']}: root gap {target} is one of its own KCs")
        elif ".".join(target.split(".")[1:3]) == here and status.get(target) != "active":
            problems.append(f"{record['id']}: root gap {target} is not an active KC of this pack")
    return problems


def prerequisite_problems(kcs: list[dict[str, Any]], pack: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    here = f"{pack.get('subject')}.{pack.get('level')}"
    status = {kc["id"]: kc["status"] for kc in kcs}
    declared = {d["pack_id"].removeprefix("pack.") for d in pack.get("depends_on_packs", [])}
    graph: dict[str, list[str]] = {kc["id"]: [] for kc in kcs}
    for kc in kcs:
        for edge in kc["prerequisites"]:
            target = edge["kc_id"]
            target_pack = ".".join(target.split(".")[1:3])
            if target_pack != here:
                if target_pack not in declared:
                    problems.append(f"{kc['id']}: prerequisite {target} is in pack.{target_pack}, not declared")
                continue
            if target not in status:
                problems.append(f"{kc['id']}: prerequisite {target} is not a known KC")
            elif status[target] == "retired":
                problems.append(f"{kc['id']}: prerequisite {target} is retired")
            else:
                graph[kc["id"]].append(target)
    # Depth-first search for a cycle (white, grey, black).
    colour = dict.fromkeys(graph, 0)

    def visit(node: str, path: list[str]) -> None:
        colour[node] = 1
        for nxt in graph[node]:
            if colour[nxt] == 1:
                cycle = [*path[path.index(nxt) :], nxt] if nxt in path else [node, nxt]
                problems.append("prerequisite cycle: " + " -> ".join(cycle))
            elif colour[nxt] == 0:
                visit(nxt, [*path, nxt])
        colour[node] = 2

    for node in sorted(graph):
        if colour[node] == 0:
            visit(node, [node])
    return problems
