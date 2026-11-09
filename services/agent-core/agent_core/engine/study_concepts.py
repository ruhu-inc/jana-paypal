"""Study concept maps (the third layer of the concept map): a document's concepts and typed relations, a typed
topic's map, the anchors to the curated skill backbone, and the learner's edits. No model is asked here.

- A document's map comes from Muse Spark through the gateway (`concept_map` jobs): every concept has a summary and
  every relation a claim, each cited to its source passage and verified on its own citations. Batches are merged by
  concept name, or a name that only adds a generic word ("… phases") (the first wins), relations re-pointed and
  de-duplicated.
- A study pack's map (SPEC §18) also gets its outline (`with_outline`): its topic, the questions its research
  answered, and each idea under the question whose pages it was cited from: structure, never a claim.
- Anchoring: a concept matches a curated KC of an installed pack when its name is one of the KC's glossary terms
  (`glossary`), else appears as a phrase in a KC's statement (`statement`), else names a pack topic (`topic`);
  among several KCs the one whose statement shares most words with the concept's text wins, then the lower code.
  An unmatched concept stays "from your document". An anchor links to the curated skill; it never makes the
  document's wording curated.
- A typed topic with no document: when it names a pack topic (`matching_topics`), the map is that topic's curated
  skills and their prerequisite links (`pack`). Otherwise Muse Spark drafts one with no source (`model`): concepts
  with plain notes, no citations, each labelled "from Muse Spark, no source" on screen, and anchored like any other.
- Edits (rename, remove, merge, add) are the learner's own and are kept: a merged concept keeps the first cited
  summary; an added one has none (`learner`)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent_core.engine.study_topics import matching_topics

CONCEPTS_MAX = 60
RELATIONS_MAX = 120
DRAFT_CONCEPTS_MAX = 24
DRAFT_RELATIONS_MAX = 48
PACK_CONCEPTS_MAX = 40
SYMMETRIC = ("related_to", "contrasts_with")
STOP = frozenset(
    "a an and are as at be by for from in into is it its of on or that the this to with how what when "
    "which use using find calculate describe state recall explain identify".split()
)


class ConceptEditError(ValueError):
    pass


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.casefold()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _phrase(text: str) -> str:
    return " " + " ".join(_words(text)) + " "


def _name_key(name: str) -> str:
    return " ".join(_words(name))


# A longer name that only adds one of these to another concept's name is the same idea ("Cardiac cycle phases" is
# "Cardiac cycle"). A plain containment is not: "heart" and "heart valve" are two ideas.
GENERIC = frozenset(
    "phase process function role structure type system overview definition mechanism stage part".split()
)


def _same_idea(short: str, long: str) -> bool:
    """`long` names the same idea as `short`, which has at least two words, plus generic words only."""
    a, b = short.split(), long.split()
    if len(a) < 2 or len(b) <= len(a):
        return False
    for start in range(len(b) - len(a) + 1):
        if b[start : start + len(a)] == a:
            rest = b[:start] + b[start + len(a) :]
            return all(w in GENERIC or w in STOP for w in rest)
    return False


# --- merging a document's batches ------------------------------------------------------------------------------------


def merge_drafts(drafts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One `DraftConceptMap` from each batch's: concepts by name (the first summary wins), ids renumbered c1.. in
    document order, relations re-pointed, without self links or repeats (a symmetric type either way)."""
    concepts: list[dict[str, Any]] = []
    by_name: dict[str, str] = {}
    relations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for draft in drafts:
        local: dict[str, str] = {}
        for c in draft["concepts"]:
            key = _name_key(c["name"])
            same = next((k for k in by_name if _same_idea(k, key) or _same_idea(key, k)), None)
            if key not in by_name and same is not None:
                by_name[key] = by_name[same]  # a near-duplicate from another batch: the first name and summary win
            if key not in by_name:
                if len(concepts) >= DRAFT_CONCEPTS_MAX:
                    continue
                by_name[key] = f"c{len(concepts) + 1}"
                concepts.append({**c, "concept_id": by_name[key]})
            local[c["concept_id"]] = by_name[key]
        for r in draft["relations"]:
            a, b = local.get(r["from"]), local.get(r["to"])
            if a is None or b is None or a == b:
                continue
            ident = (min(a, b), max(a, b), r["type"]) if r["type"] in SYMMETRIC else (a, b, r["type"])
            if ident in seen or len(relations) >= DRAFT_RELATIONS_MAX:
                continue
            seen.add(ident)
            relations.append({**r, "from": a, "to": b})
    return {"concepts": concepts, "relations": relations}


def draft_claims(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every cited claim of a draft: concept summaries, then relation claims."""
    return [c["summary"] for c in draft["concepts"]] + [r["claim"] for r in draft["relations"]]


# --- anchoring to the curated backbone --------------------------------------------------------------------------------


def _active_kcs(content: Any) -> list[dict[str, Any]]:
    return [k for k in content.table("kcs") if k.get("status", "active") == "active"]


def anchor(content: Any, name: str, text: str = "") -> dict[str, Any] | None:
    """The curated KC a concept matches (`ConceptAnchor`), or None. Deterministic: no model, pack records only."""
    if content is None:
        return None
    key = _name_key(name)
    if not key:
        return None
    kcs = _active_kcs(content)
    context = set(_words(f"{name} {text}")) - STOP

    def best(found: list[dict[str, Any]], how: str) -> dict[str, Any] | None:
        if not found:
            return None
        pick = min(found, key=lambda k: (-len(context & (set(_words(str(k["statement"]))) - STOP)), str(k["code"])))
        return {"kc_id": str(pick["id"]), "statement": str(pick["statement"])[:400], "how": how}

    glossary = [k for k in kcs if any(_name_key(str(t)) == key for t in k.get("glossary") or [])]
    if glossary:
        return best(glossary, "glossary")
    if len(key) >= 5 or " " in key:
        phrase = f" {key} "
        stated = [k for k in kcs if phrase in _phrase(str(k["statement"]))]
        if stated:
            return best(stated, "statement")
    topics = matching_topics(content, name).get("items", [])
    if topics:
        ids = set(topics[0]["kc_ids"]) if "kc_ids" in topics[0] else set()
        under = [k for k in kcs if str(k["id"]) in ids] or [k for k in kcs if k.get("node_id") == topics[0]["node_id"]]
        return best(under, "topic")
    return None


# --- the stored map ---------------------------------------------------------------------------------------------------


def document_map(draft: Mapping[str, Any], content: Any) -> dict[str, Any]:
    """The `StudyConceptMap` of a verified document draft: every concept and relation from the document, with its
    citation, each concept anchored where the backbone has it."""
    concepts = [
        {
            "concept_id": c["concept_id"],
            "name": c["name"],
            "summary": c["summary"],
            "note": None,
            "origin": "document",
            "anchor": anchor(content, c["name"], c["summary"]["text"]),
        }
        for c in draft["concepts"]
    ]
    relations = [
        {"from": r["from"], "to": r["to"], "type": r["type"], "claim": r["claim"], "origin": "document"}
        for r in draft["relations"]
    ]
    return {"concepts": concepts, "relations": relations, "edited": False}


def with_outline(current: Mapping[str, Any], topic: str, branches: Sequence[str],
                 branch_of: Mapping[str, int]) -> dict[str, Any]:  # fmt: skip
    """The map with a study pack's outline as its tree: the topic (`t1`) includes each question its research answered
    (`q1`..), and each question includes the ideas cited from its pages (`branch_of`: concept id -> question index);
    an idea from no known question hangs from the topic. Outline concepts carry the words as a note and no claim, and
    outline relations are `includes` with no claim: the pack's organisation, never something the pages said. A map
    that already has an outline is returned as it is."""
    concepts = [dict(c) for c in current["concepts"]]
    if any(c["origin"] == "outline" for c in concepts):
        return dict(current)
    used = sorted({i for i in branch_of.values() if 0 <= i < len(branches)})
    outline = [{"concept_id": "t1", "name": topic[:80], "summary": None, "note": topic[:400], "origin": "outline",
                "anchor": None}]  # fmt: skip
    outline += [{"concept_id": f"q{i + 1}", "name": branches[i][:80], "summary": None, "note": branches[i][:400],
                 "origin": "outline", "anchor": None} for i in used]  # fmt: skip
    links = [{"from": "t1", "to": f"q{i + 1}", "type": "includes", "claim": None, "origin": "outline"} for i in used]
    for c in concepts:
        if c["origin"] == "outline":
            continue
        index = branch_of.get(c["concept_id"])
        parent = f"q{index + 1}" if index in used else "t1"
        links.append({"from": parent, "to": c["concept_id"], "type": "includes", "claim": None, "origin": "outline"})
    room = CONCEPTS_MAX - len(outline)
    return {**current, "concepts": outline + concepts[:room],
            "relations": (links + list(current["relations"]))[:RELATIONS_MAX]}  # fmt: skip


def pack_map(content: Any, topic: str) -> tuple[str, dict[str, Any]] | None:
    """A typed topic's map from the pack when it names a pack topic: (node id, map) of that topic's curated skills
    and the skills they need directly, with the pack's prerequisite links; None when no pack topic matches."""
    if content is None:
        return None
    found = matching_topics(content, topic).get("items", [])
    if not found:
        return None
    node_id = str(found[0]["node_id"])
    kcs = {str(k["id"]): k for k in _active_kcs(content)}
    own = [k for k in sorted(kcs) if str(kcs[k].get("node_id")) == node_id][:PACK_CONCEPTS_MAX]
    needed = []
    for kc_id in own:
        for need in kcs[kc_id].get("prerequisites") or []:
            n = str(need["kc_id"]) if isinstance(need, dict) else str(need)
            if n in kcs and n not in own and n not in needed:
                needed.append(n)
    chosen = (own + needed)[:PACK_CONCEPTS_MAX]
    ids = {kc_id: f"k{i + 1}" for i, kc_id in enumerate(chosen)}
    concepts = []
    for kc_id in chosen:
        statement = str(kcs[kc_id]["statement"])
        # The skill's own statement names it (cut at a word to fit); the note keeps it whole.
        name = statement if len(statement) <= 80 else statement[:79].rsplit(" ", 1)[0] + "\u2026"
        concepts.append(
            {
                "concept_id": ids[kc_id],
                "name": name,
                "summary": None,
                "note": statement[:400],
                "origin": "pack",
                "anchor": {"kc_id": kc_id, "statement": statement[:400], "how": "topic"},
            }
        )
    relations = []
    for kc_id in chosen:
        for need in kcs[kc_id].get("prerequisites") or []:
            n = str(need["kc_id"]) if isinstance(need, dict) else str(need)
            if n in ids:
                relations.append(
                    {"from": ids[n], "to": ids[kc_id], "type": "builds_on", "claim": None, "origin": "pack"}
                )
    return node_id, {"concepts": concepts, "relations": relations[:RELATIONS_MAX], "edited": False}


def model_map(draft: Mapping[str, Any], content: Any) -> dict[str, Any]:
    """A no-source topic map from Muse Spark's `ConceptTopicDraft`: plain notes, no citations, each concept anchored
    to the backbone where it matches."""
    ids = {c["concept_id"]: f"s{i + 1}" for i, c in enumerate(draft["concepts"])}
    concepts = [
        {
            "concept_id": ids[c["concept_id"]],
            "name": c["name"],
            "summary": None,
            "note": c["note"],
            "origin": "model",
            "anchor": anchor(content, c["name"], c["note"]),
        }
        for c in draft["concepts"]
    ]
    relations = [
        {"from": ids[r["from"]], "to": ids[r["to"]], "type": r["type"], "claim": None, "origin": "model"}
        for r in draft["relations"]
        if r["from"] in ids and r["to"] in ids and r["from"] != r["to"]
    ]
    return {"concepts": concepts, "relations": relations, "edited": False}


# --- the learner's edits ----------------------------------------------------------------------------------------------


def _next_id(concepts: Sequence[Mapping[str, Any]], prefix: str) -> str:
    used = {c["concept_id"] for c in concepts}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def apply_edits(current: Mapping[str, Any], ops: Sequence[Mapping[str, Any]], content: Any = None) -> dict[str, Any]:
    """The map after the learner's edits, in order. An unknown concept, a merge of fewer than two known concepts or
    a map past its bounds refuses the whole edit (`ConceptEditError`), so nothing is half applied."""
    concepts = [dict(c) for c in current["concepts"]]
    relations = [dict(r) for r in current["relations"]]

    def find(concept_id: str) -> dict[str, Any]:
        found = next((c for c in concepts if c["concept_id"] == concept_id), None)
        if found is None:
            raise ConceptEditError(f"no concept {concept_id}")
        return found

    for op in ops:
        kind = op["op"]
        if kind == "rename":
            find(op["concept_id"])["name"] = op["name"].strip()[:80] or find(op["concept_id"])["name"]
        elif kind == "remove":
            find(op["concept_id"])
            concepts = [c for c in concepts if c["concept_id"] != op["concept_id"]]
            relations = [r for r in relations if op["concept_id"] not in (r["from"], r["to"])]
        elif kind == "merge":
            parts = [find(i) for i in op["concept_ids"]]
            if len(parts) < 2:
                raise ConceptEditError("a merge needs two concepts")
            merged_id = _next_id(concepts, "m")
            first_cited = next((p for p in parts if p.get("summary")), parts[0])
            origins = {p["origin"] for p in parts}
            merged = {
                "concept_id": merged_id,
                "name": op["name"].strip()[:80],
                "summary": first_cited.get("summary"),
                "note": next((p["note"] for p in parts if p.get("note")), None),
                "origin": origins.pop() if len(origins) == 1 else "learner",
                "anchor": next((p["anchor"] for p in parts if p.get("anchor")), None),
                "merged_from": sorted({*op["concept_ids"], *(m for p in parts for m in p.get("merged_from", []))})[:24],
            }
            gone = set(op["concept_ids"])
            at = min(i for i, c in enumerate(concepts) if c["concept_id"] in gone)
            concepts = [c for c in concepts if c["concept_id"] not in gone]
            concepts.insert(at, merged)
            kept, seen = [], set()
            for r in relations:
                r = {
                    **r,
                    "from": merged_id if r["from"] in gone else r["from"],
                    "to": merged_id if r["to"] in gone else r["to"],
                }
                ident = (r["from"], r["to"], r["type"])
                if r["from"] != r["to"] and ident not in seen:
                    seen.add(ident)
                    kept.append(r)
            relations = kept
        elif kind == "add":
            new_id = _next_id(concepts, "u")
            name = op["name"].strip()[:80]
            concepts.append(
                {
                    "concept_id": new_id,
                    "name": name,
                    "summary": None,
                    "note": None,
                    "origin": "learner",
                    "anchor": anchor(content, name),
                }
            )
            if op.get("link_to"):
                find(op["link_to"])
                relations.append(
                    {"from": new_id, "to": op["link_to"], "type": "related_to", "claim": None, "origin": "learner"}
                )
        else:
            raise ConceptEditError(f"unknown edit {kind}")
        if len(concepts) > CONCEPTS_MAX or len(relations) > RELATIONS_MAX:
            raise ConceptEditError("the map is full")
    return {"concepts": concepts, "relations": relations, "edited": True}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
