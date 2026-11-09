"""Online sketches (VSL-43, VSL-44, D103, D106): the engine validates a sketch request, runs its checks and resolves it.

A sketch is a list of primitives from the closed vocabulary of the sketch contract in the visual's own coordinate frame,
never SVG, HTML, path data or pixels. Before anything is shown the request passes the schema (anything else, raw markup
included, is dropped whole and logged), then each element: every coordinate inside the frame, at most `sketch_max_
elements` elements and `sketch_max_labels` labels, label text through the output guard and every number in a label
equal to a verified value. A dropped element is named with its reason; the explanation goes on without it.

The checks of TABLE VSL-44a compare an element with a verified value (a check id or a known) or a curve of the shown
visual, within the claim's tolerance. A failing element is dropped. The sketch is `checked` only when at least one check
ran, every check passed and no checkable element (a point, a segment, an arrow, an arc, a label with a number) went
unchecked; otherwise it is `not_checked`, shown as "Rough sketch, not checked". Shapes, layout and pictures are never
checkable. The alt text is the engine's, built from the kept elements (VSL-14)."""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from agent_core.contracts import errors as contract_errors
from agent_core.engine.events import new_id
from agent_core.engine.state import numbers_in

MAX_ELEMENTS = 40
MAX_LABELS = 12
# Tolerances per claim (VSL-44), as a share of the frame's span or in degrees; configuration keys in the core.
POSITION_SHARE = 0.02
LENGTH_SHARE = 0.03
ANGLE_DEG = 3.0
# Markup in any text is refused outright: a sketch is never SVG or HTML (VSL-43).
MARKUP = re.compile(r"[<>]|&#?[A-Za-z0-9]+;|javascript:", re.I)
CHECKABLE = frozenset({"point", "line", "arrow", "arc"})
# The claims each element kind can carry (TABLE VSL-44a).
CLAIMS = {
    "point": {"point_value", "point_on_curve", "intersection"},
    "line": {"length"},
    "arrow": {"direction", "length"},
    "arc": {"angle"},
    "label": {"label_value"},
}

Tool = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any] | None, str | None]]]


@dataclass
class SketchContext:
    """What the engine knows when it resolves a sketch: verified values by check id or known id, the curves of the
    shown visual by part id (an expression in x), the guard and the numbers a label may carry."""

    values: dict[str, list[Fraction]] = field(default_factory=dict)
    curves: dict[str, str] = field(default_factory=dict)
    guard: Callable[[str], bool] = lambda text: True
    traced: Callable[[Fraction], bool] = lambda value: False
    max_elements: int = MAX_ELEMENTS
    max_labels: int = MAX_LABELS
    position_share: float = POSITION_SHARE
    length_share: float = LENGTH_SHARE
    angle_deg: float = ANGLE_DEG


class SketchRefused(Exception):
    """The request is not a sketch the contract allows: nothing is shown and a log record is written (EVL-28)."""


def _coords(element: dict[str, Any]) -> list[tuple[float, float]]:
    kind = element["kind"]
    if kind in ("line", "arrow"):
        return [tuple(element["from"]), tuple(element["to"])]
    if kind in ("circle", "arc"):
        (x, y), r = element["center"], element["radius"]
        return [(x - r, y - r), (x + r, y + r)]
    if kind == "polygon":
        return [tuple(p) for p in element["points"]]
    if kind in ("point", "label"):
        return [tuple(element["at"])]
    return []


def _inside(element: dict[str, Any], frame: dict[str, Any]) -> bool:
    if element["kind"] == "axis":
        low, high = (frame["x_min"], frame["x_max"]) if element["axis"] == "x" else (frame["y_min"], frame["y_max"])
        return all(low <= v <= high for v in (element["from"], element["to"], *element.get("ticks", [])))
    return all(frame["x_min"] <= x <= frame["x_max"] and frame["y_min"] <= y <= frame["y_max"]
               for x, y in _coords(element))  # fmt: skip


def _text_ok(text: str, context: SketchContext) -> str | None:
    """None when the text may be shown, else the drop reason."""
    if MARKUP.search(text) or not context.guard(text):
        return "guard_refused"
    if any(not context.traced(value) for value in numbers_in(text)):
        return "unverified_number"
    return None


def validate(request: Any, context: SketchContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The request with its elements kept or dropped (VSL-43). Raises SketchRefused when it is not a sketch at all."""
    if not isinstance(request, dict) or contract_errors("visuals/sketch", request):
        raise SketchRefused("schema")
    frame = request["frame"]
    if not (frame["x_min"] < frame["x_max"] and frame["y_min"] < frame["y_max"]):
        raise SketchRefused("frame")
    kept: dict[int, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    labels = 0
    for index, element in enumerate(request["elements"]):
        reason = None
        if index >= context.max_elements:
            reason = "over_cap"
        elif not _inside(element, frame):
            reason = "out_of_bounds"
        elif element["kind"] == "label":
            labels += 1
            reason = "over_cap" if labels > context.max_labels else _text_ok(element["text"], context)
        elif element["kind"] == "axis" and element.get("title"):
            reason = _text_ok(element["title"], context)
        if reason is None:
            kept[index] = element
        else:
            dropped.append({"element": index, "reason": reason})
    caption = request.get("caption")
    draft: dict[str, Any] = {"frame": frame, "kept": kept, "checks": request["checks"]}
    if caption and _text_ok(caption, context) is None:
        draft["caption"] = caption
    return draft, dropped


def _span(frame: dict[str, Any]) -> float:
    return math.hypot(frame["x_max"] - frame["x_min"], frame["y_max"] - frame["y_min"])


def _near(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


async def _check_one(check: dict[str, Any], element: dict[str, Any], frame: dict[str, Any], context: SketchContext,
                     run: Tool) -> tuple[str, str | None]:  # fmt: skip
    """pass, fail or not_checkable, with the check id that decided it."""
    claim, against = check["claim"], check["against"]
    if claim not in CLAIMS.get(element["kind"], set()):
        return "not_checkable", None
    span = _span(frame)
    if claim == "point_on_curve":
        expression = context.curves.get(against)
        if expression is None:
            return "not_checkable", None
        x, y = element["at"]
        value, check_id = await run("math_evaluate", {"expression": expression, "values": {"x": str(x)}})
        computed = _number((value or {}).get("exact") or (value or {}).get("value"))
        if computed is None or check_id is None:
            return "not_checkable", None
        return ("pass" if _near(y, computed, context.position_share * span) else "fail"), check_id
    found = context.values.get(against)
    if not found:
        return "not_checkable", None
    target = [float(v) for v in found]
    check_id = against if against.startswith("chk_") else None
    if claim in ("point_value", "intersection"):
        x, y = element["at"]
        tolerance = context.position_share * span
        ok = _near(x, target[0], tolerance) and (len(target) < 2 or _near(y, target[1], tolerance))
    elif claim == "length":
        (x1, y1), (x2, y2) = element["from"], element["to"]
        ok = _near(math.hypot(x2 - x1, y2 - y1), target[0], max(context.length_share * span, 1e-9))
    elif claim == "direction":
        if len(target) < 2:
            return "not_checkable", None
        (x1, y1), (x2, y2) = element["from"], element["to"]
        drawn, wanted = math.atan2(y2 - y1, x2 - x1), math.atan2(target[1], target[0])
        turn = abs((math.degrees(drawn - wanted) + 180) % 360 - 180)
        ok = turn <= context.angle_deg
    elif claim == "angle":
        ok = _near(abs(element["end_deg"] - element["start_deg"]), target[0], context.angle_deg)
    else:  # label_value
        values = numbers_in(element["text"])
        ok = bool(values) and all(any(abs(float(v) - t) <= 1e-9 * max(1, abs(t)) for t in target) for v in values)
    return ("pass" if ok else "fail"), check_id


def _number(text: Any) -> float | None:
    try:
        return float(Fraction(str(text))) if text is not None else None
    except (ValueError, ZeroDivisionError):
        values = numbers_in(str(text))
        return float(values[0]) if len(values) == 1 else None


def _checkable(element: dict[str, Any]) -> bool:
    return element["kind"] in CHECKABLE or (element["kind"] == "label" and bool(numbers_in(element["text"])))


def alt_text(elements: list[dict[str, Any]], caption: str | None) -> str:
    """The engine's description of a sketch for screen readers, from its kept elements (VSL-14)."""
    counts: dict[str, int] = {}
    for element in elements:
        counts[element["kind"]] = counts.get(element["kind"], 0) + 1
    names = {"line": "line", "arrow": "arrow", "arc": "angle mark", "circle": "circle", "polygon": "shape",
             "point": "point", "label": "label", "axis": "axis"}  # fmt: skip
    parts = [f"{n} {names[k]}{'' if n == 1 else 's'}" for k, n in counts.items()]
    words = [e["text"] for e in elements if e["kind"] == "label"]
    text = "Rough sketch: " + ", ".join(parts) + (f"; labels {', '.join(words)}" if words else "")
    return (f"{caption}. {text}" if caption else text)[:600]


async def resolve(request: Any, context: SketchContext, run: Tool) -> dict[str, Any]:
    """The resolved sketch for `visual.sketch` (VSL-43, VSL-44). Raises SketchRefused for a request that is none."""
    draft, dropped = validate(request, context)
    kept: dict[int, dict[str, Any]] = draft["kept"]
    results: list[dict[str, Any]] = []
    for check in draft["checks"]:
        element = kept.get(check["element"])
        if element is None:
            continue
        result, check_id = await _check_one(check, element, draft["frame"], context, run)
        results.append({"element": check["element"], "claim": check["claim"], "result": result, "check_id": check_id})
        if result == "fail":
            kept.pop(check["element"], None)
            dropped.append({"element": check["element"], "reason": "check_failed"})
    passed = {r["element"] for r in results if r["result"] == "pass" and r["element"] in kept}
    unchecked = [i for i, e in kept.items() if _checkable(e) and i not in passed]
    ran = [r for r in results if r["result"] != "not_checkable"]
    checked = bool(ran) and all(r["result"] == "pass" for r in ran) and not unchecked and bool(passed)
    status = "checked" if checked else "not_checked"
    elements = [kept[i] for i in sorted(kept)]
    resolved: dict[str, Any] = {"sketch_id": new_id("skt"), "sketch_version": 1, "frame": draft["frame"],
                                "elements": elements, "dropped": sorted(dropped, key=lambda d: d["element"]),
                                "checks": results, "status": status,
                                "alt": alt_text(elements, draft.get("caption"))}  # fmt: skip
    if draft.get("caption"):
        resolved["caption"] = draft["caption"]
    return resolved
