"""Words that speak of a visual (VSL-16, PED-21): what the tutor says about a drawing must match what is on screen.

When a turn's visual is refused or dropped, the student sees the engine's "I can't draw this one" line and no drawing.
A reply that still says "I have drawn its velocity-time graph below" or asks "what does point A tell us?" is then
false. `refers_to_visual` finds such a sentence; `remove_visual_claims` takes those sentences out, as the critic takes
out claims of a save (`memory_session.remove_save_claims`). Engine code only: nothing here is model text."""

from __future__ import annotations

import re
from collections.abc import Iterable

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")  # "2.5 m/s" and "m/s." stay inside their sentence
# A drawing named by its kind, or the tutor saying it drew, plotted or shows something.
_DRAWING = re.compile(
    r"\b(?:graph|graphs|diagram|diagrams|figure|figures|sketch|sketches|chart|charts|plot|plots|drawing|drawings|"
    r"picture|pictures|visual|visuals|simulation|animation)\b"
    r"|\b(?:i(?:'ve|\s+have)?\s+|i've\s+|we(?:'ve|\s+have)?\s+)(?:drawn|drew|sketched|plotted|put\s+up|shown|marked)\b"
    r"|\b(?:shown|drawn|plotted|see|look\s+at)\s+(?:it\s+)?(?:below|above|here|on\s+(?:the|your)\s+screen)\b",
    re.IGNORECASE,
)


def _points(part_ids: Iterable[str]) -> re.Pattern[str] | None:
    """The refused visual's own labelled points ("point A", "points A and B"), which only that drawing shows."""
    ids = sorted({p for p in part_ids if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,15}", p)}, key=len, reverse=True)
    if not ids:
        return None
    names = "|".join(re.escape(p) for p in ids)
    return re.compile(rf"\bpoints?\s+(?:{names})\b(?:\s*(?:,|and|to)\s*(?:{names})\b)*")


def refers_to_visual(text: str, part_ids: Iterable[str] = ()) -> bool:
    """Whether any sentence of `text` speaks of a drawing, or of the refused visual's labelled points."""
    points = _points(part_ids)
    flat = " ".join((text or "").replace("\u2019", "'").split())
    return bool(_DRAWING.search(flat) or (points is not None and points.search(flat)))


def remove_visual_claims(text: str, part_ids: Iterable[str] = ()) -> tuple[str, bool]:
    """`text` without its sentences that speak of a drawing or of the refused visual's points; whether any went."""
    ids = list(part_ids)
    sentences = [s for s in _SENTENCE_END.split((text or "").strip()) if s]
    kept = [s for s in sentences if not refers_to_visual(s, ids)]
    removed = len(kept) < len(sentences)
    return (" ".join(kept) if removed else text), removed
