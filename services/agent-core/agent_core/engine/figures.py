"""Checked figures on the live turn loop: a pack question's figure on screen, and a reason for the Coach to draw one.

- A pack item or lesson block that carries a figure (content `visual`, resolved and checked at pack build) shows it
  as soon as the problem or block opens: the engine emits the pre-resolved spec as a `visual` part and records it
  among the visuals on screen, so the Coach can point at its parts (`vis_<id>:<part>`, VSL-45). No model draws it.
  A predict-first simulation (VSL-47) asks for the learner's prediction before anything else.
- A new problem on a KC whose pack has a checked figure pattern (a worked example's figure) gets one more line in
  the Coach's opening note: draw a figure of that kind with this problem's own numbers. The pattern's numbers are
  masked, so the Coach can copy its shape but never another problem's values, and the givens it may cite are listed
  by their ids. The validator still traces every number the Coach's figure claims (VSL-12); anything it cannot trace
  is refused and the reply goes on in words.

Engine code over engine state (AGT-03): which figure and when is never a model's choice."""

from __future__ import annotations

import json
import re
from fractions import Fraction
from typing import Any

from agent_core.assets.visuals import advertised_kinds, resolved_of
from agent_core.engine.state import numbers_in

SUBJECT_CODES = {"mathematics": "mth", "physics": "phy", "chemistry": "chm", "biology": "bio", "accounting": "acc"}
DIGITS = re.compile(r"\d+(?:\.\d+)?(?:/\d+)?")
MASK = "?"
KEEP = frozenset({"id", "from", "to", "of", "graph", "type", "object", "optic", "kind", "layout_hint", "mode",
                  "predict", "arrangement", "between", "rays", "show", "marks", "scale", "axes"})  # fmt: skip
# The whole request to copy, not only its params: left to write the rest, the Coach adds a `ref` of its own, which the
# contract refuses (a `ref` names a library diagram) and the decoding schema cannot stop (its pattern is not sent).
FIGURE_NOTE = (
    "a checked figure fits this topic: in `visual`, show one {kind} drawn with this problem's own numbers, written "
    "like this request (fill each ? from the question; never label the answer on it): {request}. It takes no `ref`. "
    "Add its numbers_from: as strings, the ids of the givens you draw ({givens}) and of any check you use; a number "
    "you cannot cite stays off. Then ask about one of its parts ({parts}), pointing at it"
)
SHOWN_NOTE = "the question's figure is on screen ({visual_id}, parts {parts}): point at a part when you ask about it"
PREDICT_NOTE = ("a predict-first simulation is on screen ({visual_id}): ask the student to write their prediction "
                "in it and lock it in before anything else; never say or hint at what it will show")  # fmt: skip


def masked(value: Any, key: str | None = None) -> Any:
    """A pattern's params with every number replaced by `?`: its shape, never another problem's values. Names that
    only identify (ids, the ends of a connection, enum words) are kept as they are."""
    if isinstance(value, dict):
        return {k: masked(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v, key) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return MASK
    if isinstance(value, str) and key not in KEEP:
        return DIGITS.sub(MASK, value)
    return value


def knowns(engine: Any) -> dict[str, Fraction]:
    """The givens a visual may cite (VSL-12 `kn_`): the knowns panel's values the question states, and zero, which
    the question gives in words ("from rest"), as the board counts it."""
    out: dict[str, Fraction] = {"kn_zero": Fraction(0)}
    for known in engine.state.board.knowns or []:
        found = numbers_in(str(known.get("value", "")))
        if len(found) == 1 and re.fullmatch(r"kn_[A-Za-z0-9]{2,32}", str(known.get("known_id", ""))):
            out[str(known["known_id"])] = found[0]
    return out


def _pattern(engine: Any, kc_ids: list[str]) -> dict[str, Any] | None:
    sources = engine.deps.visual_sources
    if sources is None:
        return None
    figures = sources(SUBJECT_CODES.get(engine.state.subject, engine.state.subject)).get("figures") or {}
    allowed = set(advertised_kinds(engine.state.subject, engine.state.level, engine.deps.stage))
    for kc_id in kc_ids:
        pattern = figures.get(kc_id)
        if pattern is not None and pattern.get("type") in allowed:
            return dict(pattern)
    return None


def opening_note(engine: Any) -> str:
    """The Coach's figure line for a new problem ("" when its KCs have no checked pattern, or the session cannot show
    one: lean hardware, a text-only reply)."""
    state, settings = engine.state, engine.deps.settings
    if state.problem is None or engine.diagnosis is None or state.hardware_class in settings.lean_classes:
        return ""
    pattern = _pattern(engine, [str(k) for k in engine.diagnosis.kc_ids])
    if pattern is None:
        return ""
    given = knowns(engine)
    listed = ", ".join(f"{ref} = {value}" for ref, value in given.items())
    params = json.dumps(masked(pattern["params"]), ensure_ascii=False, separators=(",", ":"))
    request = f'{{"spec_version":1,"type":"{pattern["type"]}","params":{params[:600]}}}'
    parts = ", ".join(p for p in pattern["parts"] if not p.startswith("seg_"))[:200]
    return FIGURE_NOTE.format(kind=pattern["type"], request=request, givens=listed, parts=parts)


def show(engine: Any, turn: Any, record: Any) -> str:
    """A pack question's figure on screen when it has one; returns the Coach's note ("" when there is none)."""
    resolved = resolved_of(record)
    if resolved is None or engine.state.hardware_class in engine.deps.settings.lean_classes:
        return ""
    engine._emit_visual(turn, resolved)
    shown = next((v for v in engine.state.shown_visuals if v["visual_id"] == resolved["visual_id"]), None)
    if (resolved.get("params") or {}).get("predict"):  # VSL-47: the prediction comes first, then the run
        return PREDICT_NOTE.format(visual_id=resolved["visual_id"])
    parts = ", ".join(str(p) for p in (shown or {}).get("parts", []) if not str(p).startswith("seg_"))[:200]
    return SHOWN_NOTE.format(visual_id=resolved["visual_id"], parts=parts)
