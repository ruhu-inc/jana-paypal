"""What this video teaches (SPEC §20.3, §20.4, JM-D33): the `teaching_outline` aid's device-side half.

The model writes the cited part (objectives, worked examples, concepts, what the lecture assumes), each item citing
a transcript line; the gateway's verifier keeps the supported items. On the device, by code and never by a model:
`at_ms` from the cited line's moment, each concept's `kc_ids` through `study_topics.kc_tags` over its name and cited
text (`outside_packs` when none), `skips` as the skills of the tagged topics that `kc_tags` over the whole transcript
never tags, and `for_you` as the learner's bands joined from the learner database. Nothing here is sent anywhere."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent_core.engine.study_links import line_moment, line_text, moment_at
from agent_core.engine.study_topics import kc_tags, matching_topics

SECTIONS = ("objectives", "examples", "concepts", "assumes")
LIMITS = {"objectives": 12, "examples": 8, "concepts": 16, "assumes": 6}
SKIPS_MAX = 6
FOR_YOU_MAX = 48
# A concept maps to a skill only when their words overlap enough (its name counts twice: one name word and one cited
# word, or two name words), and to a second skill only when that one scores within a tenth of the best (live-run fix
# 2026-11-01: "displacement time graph" mapped to three skills that say "time" and not to the graphs skill).
CONCEPT_MIN_OVERLAP = 3
CONCEPT_CLOSE = 0.10


def flatten(outline: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The outline's items as claims, in order (objectives, examples, concepts, assumes): the verifier's per-claim
    support and the aid's `claims` map to them one to one."""
    claims = []
    for section in SECTIONS:
        for item in outline.get(section, []):
            claims.append({"text": item.get("text") or item.get("name") or "", "citations": item["citations"]})
    return claims


def rebuild(outline: Mapping[str, Any], keep: Sequence[bool]) -> dict[str, Any] | None:
    """The outline with only the items `keep` marks (one flag per flattened item), or None when nothing is left."""
    flags = iter(keep)
    kept: dict[str, Any] = {section: [] for section in SECTIONS}
    for section in SECTIONS:
        for item in outline.get(section, []):
            if next(flags, False):
                kept[section].append(item)
    return kept if any(kept[s] for s in SECTIONS) else None


def merge(outlines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One outline from every batch's, in order, within each section's bound."""
    merged: dict[str, Any] = {section: [] for section in SECTIONS}
    for outline in outlines:
        for section in SECTIONS:
            for item in outline.get(section, []):
                if len(merged[section]) < LIMITS[section]:
                    merged[section].append(dict(item))
    return merged


_WORD = re.compile(r"[a-z0-9]{4,}")


def _cited_line_start(text: str, start: int, end: int, said: str) -> int:
    """Where, in a citation's span, the line this item is about begins. A citation that spans several timed lines (the
    model quoted a whole passage for an item that one line of it states) is read at the line sharing most content words
    with the item, the earliest on a tie; a span of one line, or words that match none, keeps the line `start` is in
    (live-run fix 2026-11-01: three objectives at 0:21, 0:29 and 0:35 opened at 0:00)."""
    lines, at = [], text.rfind("\n", 0, start) + 1
    while at < min(end, len(text)):
        stop = text.find("\n", at)
        stop = len(text) if stop < 0 else stop
        if line_moment(text[at:stop]) is not None:
            lines.append((at, text[at:stop]))
        at = stop + 1
    wanted = set(_WORD.findall(said.lower()))
    scored = [
        (len(wanted & set(_WORD.findall(line_text(line).lower()))), -n, pos) for n, (pos, line) in enumerate(lines)
    ]
    best = max(scored, default=None)
    return start if best is None or best[0] == 0 else best[2]


def _moment(item: Mapping[str, Any], chunks: Mapping[str, Mapping[str, Any]]) -> int | None:
    said = str(item.get("text") or item.get("name") or "")
    for citation in item.get("citations", []):
        chunk = chunks.get(citation["chunk_id"])
        if chunk is None:
            continue
        text, start = str(chunk["text"]), int(citation["start"])
        found = moment_at(text, _cited_line_start(text, start, int(citation["end"]), said))
        if found is not None:
            return found
        locator = chunk["locator"]
        if locator.get("kind") == "time":
            return int(locator["start_ms"])
        if locator.get("kind") == "frame":
            return int(locator["at_ms"])
    return None


def fill(
    outline: Mapping[str, Any],
    chunks: Sequence[Mapping[str, Any]],
    content: Any,
    band_of: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """The outline with its code-written part filled (SPEC §20.4): `at_ms` from the cited line, `kc_ids` and
    `outside_packs` per concept, `skips` and `for_you`. `content` is the installed packs' index (None: everything is
    outside the packs); `band_of` reads a skill's band from the learner database (None: not started)."""
    by_id = {str(c["chunk_id"]): c for c in chunks}
    out: dict[str, Any] = {section: [dict(i) for i in outline.get(section, [])] for section in SECTIONS}
    for section in ("objectives", "examples", "concepts"):
        for item in out[section]:
            moment = _moment(item, by_id)
            if moment is not None:
                item["at_ms"] = moment
            elif "at_ms" in item and not isinstance(item["at_ms"], int):
                del item["at_ms"]
    for item in out["assumes"]:
        item.pop("at_ms", None)
    tagged: list[str] = []
    whole = "\n".join(str(c["text"]) for c in chunks)
    # The topics the whole transcript names (study_topics.matching_topics): a concept is scored within them by its
    # name (twice) and cited text, keeping the best skill and any within a tenth of it, none under the least overlap.
    named = matching_topics(content, whole)["items"] if content is not None else []
    for concept in out["concepts"]:
        quotes = " ".join(str(c.get("quote", "")) for c in concept.get("citations", []))
        name = str(concept.get("name", ""))
        tags = kc_tags(content, quotes, focus=name, within=named or None, min_overlap=CONCEPT_MIN_OVERLAP,
                       close=CONCEPT_CLOSE) if content is not None else []  # fmt: skip
        concept["kc_ids"] = [t["kc_id"] for t in tags][:3]
        concept["outside_packs"] = not concept["kc_ids"]
        tagged += [k for k in concept["kc_ids"] if k not in tagged]
    out["skips"] = _skips(tagged, chunks, content) if content is not None and tagged else []
    out["for_you"] = [
        {"kc_id": kc, "band": (band_of(kc) if band_of is not None else None) or "not_started"}
        for kc in tagged[:FOR_YOU_MAX]
    ]
    return out


def _skips(tagged: Sequence[str], chunks: Sequence[Mapping[str, Any]], content: Any) -> list[dict[str, str]]:
    """The skills of the tagged topics the whole transcript never reaches: the topic's KCs minus those `kc_tags`
    over the whole transcript tags and those a concept is tagged with (review non-blocking 7's rule)."""
    whole = "\n".join(str(c["text"]) for c in chunks)
    reached = {t["kc_id"] for t in kc_tags(content, whole)} | set(tagged)
    topics: list[str] = []
    for kc in tagged:
        info = content.kcs.get(kc)
        if info is not None and info.node_id not in topics:
            topics.append(info.node_id)
    skips: list[dict[str, str]] = []
    for node in topics:
        members = sorted(k.kc_id for k in content.kcs.values() if k.node_id == node)
        for kc in members:
            if kc in reached or any(s["kc_id"] == kc for s in skips):
                continue
            skips.append({"kc_id": kc, "statement": str(content.kc_statement(kc) or kc)[:200]})
            if len(skips) == SKIPS_MAX:
                return skips
    return skips
