"""Conservative local topic suggestions from verified installed pack titles; never mastery evidence.

`kc_tags` labels a Study activity with the pack topics its words name (SPEC §19.3.2): a hint for the Reflector and the
knows view, never which skills an item tests, so never mastery evidence (JM-D26)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


def matching_topics(content: Any, text: str) -> dict[str, Any]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    haystack, tokens = " " + " ".join(words) + " ", set(words)
    matches = []
    for node_id, node in sorted(content.nodes.items()):
        title = str(node.get("title", ""))
        terms = re.findall(r"[a-z0-9]+", title.lower())
        if not terms or node.get("status", "active") != "active":
            continue
        # Exact title phrases (including a meaningful single word) are an interpretable suggestion.
        # No semantic/model inference can invent a pack ID, and generic one-word titles are excluded.
        if " " + " ".join(terms) + " " not in haystack or (len(terms) == 1 and len(terms[0]) < 5):
            continue
        if not set(terms) <= tokens or node.get("kind") not in ("topic", "subtopic"):
            continue
        kcs = sorted(
            str(k["id"])
            for k in content.table("kcs")
            if k.get("node_id") == node_id and k.get("status", "active") == "active"
        )[:100]
        if not kcs:
            continue
        lessons = sorted(
            str(lesson["id"])
            for lesson in content.table("lessons")
            if set(lesson.get("kc_ids", [])) & set(kcs) and lesson.get("status", "active") == "active"
        )[:20]
        matches.append(
            {
                "node_id": node_id,
                "title": title,
                "subject": ".".join(node_id.split(".")[:3]),
                "kc_ids": kcs,
                "lesson_ids": lessons,
                "objectives": [{"kc_id": k, "statement": str(content.kc_statement(k) or k)} for k in kcs],
            }
        )
        if len(matches) == 10:
            break
    return {"items": matches}


KC_TAGS_MAX = 3


def kc_tags(
    content: Any,
    text: str,
    *,
    within: list[dict[str, Any]] | None = None,
    require_overlap: bool = False,
    focus: str | None = None,
    min_overlap: int = 0,
    close: float | None = None,
) -> list[dict[str, Any]]:
    """At most three `{kc_id, weight}` for a Study activity, the weights summing to 1 (none when no topic matches):
    the topics `matching_topics` finds in the text share the weight equally, and each topic's share goes to its KCs,
    more to a KC whose statement shares more words with the text. Topic-derived, never mastery evidence (JM-D26).

    `within` names the topics to score instead of finding them in `text` (SPEC §20.4: a lecture's concept is scored
    within the topics the whole transcript names); `require_overlap` keeps only KCs whose statement shares a content
    word with the text, so a concept that names no skill tags none. For a concept (live-run fix 2026-11-01):
    `focus` is its name, whose words count twice (a cited word once), plain plurals folded; `min_overlap` is the
    least overlap score a KC needs; `close` keeps, beside the best KC, only those scoring within that fraction of it,
    so "displacement time graph" maps to the trajectory-and-graphs skill alone, not to every skill that says "time"."""
    if content is None or not (text.strip() or (focus or "").strip()):
        return []
    found = within if within is not None else matching_topics(content, f"{focus or ''}\n{text}")["items"]
    if not found:
        return []
    fold = _fold if focus is not None else _same
    named = {fold(w) for w in re.findall(r"[a-z0-9]+", (focus or "").lower())}
    words = {fold(w) for w in re.findall(r"[a-z0-9]+", text.lower())}
    least = max(min_overlap, 1 if require_overlap else 0)
    share = 1 / len(found)
    scores: dict[str, float] = {}
    for topic in found:
        overlap = {}
        statements = {kc: _terms(str(content.kc_statement(kc) or ""), fold) for kc in topic["kc_ids"]}
        # A concept's name has to name a skill, not the topic: words the topic's own title or most of its skills say
        # ("projectile") say nothing of which skill, so they score nothing and a concept naming only such words
        # ("projectile motion", "air resistance") maps to none (live-run fix 2026-11-01).
        general = _topic_words(topic, statements, fold) if focus is not None else set()
        for kc, terms in statements.items():
            specific = terms - general
            overlap[kc] = len(specific & words) + 2 * len(specific & named)
            if focus is not None and not specific & named:
                overlap[kc] = 0
        overlap = {kc: n for kc, n in overlap.items() if n >= max(least, 1 if focus is not None else 0)}
        total = sum(1 + n for n in overlap.values())
        for kc, n in overlap.items():
            scores[kc] = scores.get(kc, 0.0) + share * (1 + n) / total
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if close is not None and ranked:
        ranked = [kv for kv in ranked if kv[1] >= ranked[0][1] * (1 - close)]
    top = ranked[:KC_TAGS_MAX]
    if not top:
        return []
    whole = sum(w for _, w in top)
    weights = [round(w / whole, 4) for _, w in top]
    weights[-1] = round(1 - sum(weights[:-1]), 4)  # the rounding never moves the sum
    return [{"kc_id": kc, "weight": w} for (kc, _), w in zip(top, weights, strict=True) if w > 0]


GENERAL_SHARE = 0.5  # a word in more than this share of a topic's skill statements is general to the topic


def _topic_words(topic: dict[str, Any], statements: dict[str, set[str]], fold: Callable[[str], str]) -> set[str]:
    """The content words that describe the topic as a whole: those of its title, and those most of its skills'
    statements say (when it has at least three skills, so a small topic's every word is not general)."""
    general = {fold(w) for w in re.findall(r"[a-z0-9]+", str(topic.get("title", "")).lower()) if len(w) >= 4}
    if len(statements) >= 3:
        counts: dict[str, int] = {}
        for terms in statements.values():
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
        general |= {term for term, n in counts.items() if n > GENERAL_SHARE * len(statements)}
    return general


def _terms(statement: str, fold: Callable[[str], str] = lambda w: w) -> set[str]:
    return {fold(w) for w in re.findall(r"[a-z0-9]+", statement.lower()) if len(w) >= 4 and w not in _STOP}


def _same(word: str) -> str:
    return word


def _fold(word: str) -> str:
    """A plain plural folded to its singular ("graphs" and "graph" are one word; "mass", "radius" stay)."""
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


# --- STU-21: topics from the material itself --------------------------------------------------------------------

_NUMBERING = re.compile(
    r"^\s*(?:#{1,6}\s+|(?:\d+(?:\.\d+)*|[IVXivx]+|[A-Za-z])[.)]\s+|(?:chapter|unit|section|topic)\s+\d+[:.]?\s*)",
    re.IGNORECASE,
)
_STOP = frozenset(
    "about above after again against also among another because been before being below between both could does "
    "doing during each either every from further have having here into itself just more most much must never only "
    "other others over same should since some such than that their them then there these they this those through "
    "under until upon very were what when where which while whose will with within without would your yours also "
    "used using uses called means example examples figure table page slide answer question questions".split()
)


def _heading(line: str) -> str | None:
    """A short heading-like line: a section title, never a sentence."""
    raw = line.strip()
    marked = bool(_NUMBERING.match(raw))
    text = _NUMBERING.sub("", raw).strip().rstrip(":").strip(" -\u2013\u2014*_#")
    words = text.split()
    if not 1 <= len(words) <= 9 or not 3 <= len(text) <= 80:
        return None
    if text[-1] in ".?!,;" or not text[0].isalpha():
        return None
    if not marked and not (raw.endswith(":") or text[0].isupper()):
        return None
    if sum(ch.isalpha() for ch in text) < len(text.replace(" ", "")) * 0.7:
        return None
    return text[0].upper() + text[1:]


def material_topics(chunks: list[dict[str, Any]], limit: int = 20) -> dict[str, Any]:
    """Topics a learner can pick for generation (STU-21), extracted deterministically from the material's own
    headings (a slide or a section title covers its chunks until the next heading); a source without headings falls
    back to its most repeated terms. Titles are the material's own words: no model call, no syllabus claim."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    headed = 0
    for chunk in chunks:
        lines = [line for line in chunk["text"].splitlines() if line.strip()]
        # A chunk opening with running text continues the section before it; every heading inside it names it.
        names = [] if not lines or _heading(lines[0]) else [current] if current else []
        for line in lines:
            title = _heading(line)
            # A one-line chunk of running text (a paragraph) is not a heading however it is capitalised.
            if title is None or (len(lines) == 1 and len(chunk["text"]) > 120):
                continue
            key = title.casefold()
            current = next((t for t in sections if t.casefold() == key), title)
            sections.setdefault(current, [])
            names.append(current)
            headed += 1
        for name in names:
            if chunk["chunk_id"] not in sections[name]:
                sections[name].append(chunk["chunk_id"])
    if headed >= 2:
        items = [{"title": t[:120], "chunk_ids": ids} for t, ids in sections.items() if ids]
        return {"items": items[:limit]}
    counts: dict[str, int] = {}
    where: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for chunk in chunks:
        for word in re.findall(r"[A-Za-z][A-Za-z-]{4,}", chunk["text"]):
            key = word.casefold()
            if key in _STOP:
                continue
            counts[key] = counts.get(key, 0) + 1
            shown.setdefault(key, word if word[0].isupper() else word.capitalize())
            ids = where.setdefault(key, [])
            if chunk["chunk_id"] not in ids:
                ids.append(chunk["chunk_id"])
    ranked = sorted((k for k, n in counts.items() if n >= 2), key=lambda k: (-counts[k], k))[: min(limit, 8)]
    order = {k: i for i, k in enumerate(counts)}
    return {"items": [{"title": shown[k], "chunk_ids": where[k]} for k in sorted(ranked, key=order.__getitem__)]}
