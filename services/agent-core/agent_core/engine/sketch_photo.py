"""Snap your sketch (JM-D10): the student photographs their own velocity-time graph or free-body diagram and Jana
points at the part that is wrong, on their drawing.

Muse Spark reads what was drawn (each axis, straight part of the line, force arrow and label, with its ends on the
image; `vision.SKETCH_READ_INSTRUCTION`), SAM 3.1 outlines each element (`photo_regions.sharpen`) and the student
confirms the reading like any photo. This module then compares the drawing with the truth the engine already holds:
the problem's `free_body` or `motion_graph` visual, whose numbers the tools checked when it was resolved (VSL-16). It
never compares with a model's opinion and never says what the right drawing is: a finding names an element and what
is off about it ("points the wrong way", "goes up where it should not"), the Coach asks about it, and the element
lights on the student's photo.

Anything the reader could not read with confidence is labelled as unread and never judged; a drawing that cannot be
compared (no checked diagram for this question, or nothing readable) says so in the engine's own words.

Directions: the reader gives points on the image (x right, y down); the checked visual gives directions in degrees
anticlockwise from the +x axis. The photo may be tilted, so a drawn weight arrow (always straight down) sets the tilt;
a drawn slope sets which way an incline rises (a mirrored sketch is not a wrong one)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from typing import Any

from agent_core.engine.sketch import SketchContext, _check_one

FREE_BODY_TOLERANCE_DEG = 30.0  # a hand-drawn arrow: wider than the tutor sketch's 3 degrees (VSL-44)
FLAT_DEG = 8.0  # a drawn segment within this of horizontal is flat
CONFIDENT = 0.6  # a reader confidence below this, or a `[?]`, leaves the element unread
GRAPH_KINDS = ("motion_graph", "free_body")

# What students write for each force, by the force's name in the checked visual.
ALIASES = {
    "weight": {"w", "weight", "mg", "gravity", "fg", "g", "gravitational"},
    "normal": {"r", "n", "normal", "reaction", "normalreaction", "fn", "rn"},
    "friction": {"f", "fr", "friction", "ff", "frictional", "μ", "μr"},
    "tension": {"t", "tension", "ft"},
    "push": {"p", "push", "applied", "fa", "force"},
    "drag": {"d", "drag", "airresistance", "resistance"},
    "thrust": {"thrust", "engine"},
}


@dataclass(frozen=True)
class Finding:
    """One thing about the drawing: the photo region it is about (None for something missing), what was found and
    which element of the checked visual it concerns (a force or segment id; never a value)."""

    region_id: str | None
    outcome: str  # ok, wrong_direction, opposite, missing, extra, unread, wrong_shape, axis_label, extra_segment
    about: str | None = None
    name: str | None = None  # what the student would call it ("friction", "the y-axis")


@dataclass
class SketchCheck:
    kind: str | None  # free_body, motion_graph, or None when there is nothing to compare with
    findings: list[Finding] = field(default_factory=list)
    reason: str | None = None  # why nothing was compared: no_truth, nothing_read

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome not in ("ok", "unread")]

    @property
    def unread(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == "unread"]


def _word(text: str | None) -> str:
    return re.sub(r"[^a-zμ]", "", (text or "").lower())


def _readable(region: dict[str, Any]) -> bool:
    element = region.get("sketch_element") or {}
    label = str(element.get("label") or "")  # the label as read; the region's text is the engine's description
    return float(region.get("confidence", 0)) >= CONFIDENT and "[?]" not in label


def _angle(element: dict[str, Any]) -> float | None:
    """The element's direction in degrees anticlockwise from +x, with y up (the image's y runs down)."""
    start, end = element.get("from"), element.get("to")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    dx, dy = end["x"] - start["x"], -(end["y"] - start["y"])
    if math.hypot(dx, dy) < 1e-3:
        return None
    return math.degrees(math.atan2(dy, dx)) % 360


def _turn(a: float, b: float) -> float:
    """The smaller angle between two directions, 0 to 180."""
    return abs((a - b + 180) % 360 - 180)


def _force_key(force: dict[str, Any]) -> str:
    name = _word(force.get("name"))
    for key, words in ALIASES.items():
        if key in name or name in words:
            return key
    return _word(force.get("id"))


def _label_key(label: str | None, forces: list[dict[str, Any]]) -> str | None:
    word = _word(label)
    if not word:
        return None
    for force in forces:
        if word in (_word(force.get("id")), _word(force.get("name"))):
            return _force_key(force)
    for key, words in ALIASES.items():
        if word in words:
            return key
    return None


async def _direction_holds(drawn: float, wanted: float) -> bool:
    """The online sketch's `direction` claim (TABLE VSL-44a, `sketch._check_one`), with a hand-drawn tolerance: the
    drawn arrow as a unit arrow in a y-up frame against the checked direction as a vector."""
    frame = {"x_min": -2, "x_max": 2, "y_min": -2, "y_max": 2}
    arrow = {"kind": "arrow", "from": [0, 0], "to": [math.cos(math.radians(drawn)), math.sin(math.radians(drawn))]}
    target = [Fraction(math.cos(math.radians(wanted))).limit_denominator(10**6),
              Fraction(math.sin(math.radians(wanted))).limit_denominator(10**6)]  # fmt: skip
    context = SketchContext(values={"force": target}, angle_deg=FREE_BODY_TOLERANCE_DEG)

    async def no_tool(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        return None, None  # a direction claim runs no tool

    result, _ = await _check_one({"claim": "direction", "against": "force"}, arrow, frame, context, no_tool)
    return result == "pass"


async def check_free_body(params: dict[str, Any], regions: list[dict[str, Any]]) -> SketchCheck:
    forces = list(params.get("forces") or [])
    by_key = {_force_key(f): f for f in forces}
    arrows = [r for r in regions if (r.get("sketch_element") or {}).get("kind") == "arrow"]
    findings: list[Finding] = []
    drawn: list[tuple[dict[str, Any], dict[str, Any], float]] = []  # (region, truth force, drawn angle)
    for region in arrows:
        element = region["sketch_element"]
        angle = _angle(element)
        if not _readable(region) or angle is None:
            findings.append(Finding(region["region_id"], "unread"))
            continue
        if not str(element.get("label") or "").strip():
            findings.append(Finding(region["region_id"], "unread"))  # an unlabelled arrow names no force: never guessed
            continue
        key = _label_key(element.get("label"), forces)
        force = by_key.get(key) if key else None
        if force is None:
            findings.append(Finding(region["region_id"], "extra", None, element.get("label") or None))
            continue
        drawn.append((region, force, angle))
    # The photo's tilt, from a drawn weight (straight down in truth); none drawn, none assumed.
    tilt = next((_signed(angle - 270) for _, force, angle in drawn if _force_key(force) == "weight"), 0.0)
    if abs(tilt) > 35:
        tilt = 0.0  # a weight drawn that far off is itself the mistake, not the camera
    mirror = _mirrored(params, regions, tilt)
    seen: set[str] = set()
    for region, force, angle in drawn:
        wanted = float(force["direction_deg"])
        if mirror:
            wanted = (180 - wanted) % 360
        corrected = (angle - tilt) % 360
        holds = await _direction_holds(corrected, wanted)
        outcome = "ok" if holds else "opposite" if _turn(corrected, wanted) >= 150 else "wrong_direction"
        findings.append(Finding(region["region_id"], outcome, str(force["id"]), str(force.get("name") or force["id"])))
        seen.add(str(force["id"]))
    for force in forces:
        if str(force["id"]) not in seen and not any(f.outcome == "unread" for f in findings):
            findings.append(Finding(None, "missing", str(force["id"]), str(force.get("name") or force["id"])))
    return SketchCheck("free_body", findings)


def _signed(degrees: float) -> float:
    return (degrees + 180) % 360 - 180


def _mirrored(params: dict[str, Any], regions: list[dict[str, Any]], tilt: float) -> bool:
    """Whether the student drew the incline rising the other way (a mirror image is not a mistake)."""
    surface = params.get("surface") or {}
    incline = float(surface.get("incline_deg") or 0)
    if surface.get("kind") != "incline" or incline == 0:
        return False
    for region in regions:
        element = region.get("sketch_element") or {}
        if element.get("kind") == "line_segment" and _readable(region):
            angle = _angle(element)
            if angle is not None:
                line = (angle - tilt) % 180  # a line has no direction: its slope in (-90, 90]
                slope = line if line <= 90 else line - 180
                return abs(slope + incline) < abs(slope - incline)
    return False


GRAPH_AXES = {
    "velocity_time": {"v", "velocity", "speed", "u", "ms", "vms"},
    "speed_time": {"v", "velocity", "speed", "ms", "vms"},
    "distance_time": {"s", "d", "distance", "x", "m", "sm", "dm"},
    "displacement_time": {"s", "x", "displacement", "d", "m", "sm"},
}
TIME_WORDS = {"t", "time", "s", "ts", "times"}


def _shape(dv: Fraction) -> str:
    return "up" if dv > 0 else "down" if dv < 0 else "flat"


def check_motion_graph(params: dict[str, Any], regions: list[dict[str, Any]]) -> SketchCheck:
    findings: list[Finding] = []
    graph = str(params.get("graph") or "velocity_time")
    points = sorted(params.get("points") or [], key=lambda p: Fraction(p["t"]))
    truth = [(f"seg_{a['id']}_{b['id']}", _shape(Fraction(b["value"]) - Fraction(a["value"])))
             for a, b in pairwise(points)]  # fmt: skip
    axes = [r for r in regions if (r.get("sketch_element") or {}).get("kind") == "axis"]
    for region in axes:
        element = region["sketch_element"]
        angle = _angle(element)
        if not _readable(region) or angle is None:
            findings.append(Finding(region["region_id"], "unread"))
            continue
        vertical = _turn(angle, 90) <= 30
        label = _word(element.get("label"))
        if not label:
            findings.append(Finding(region["region_id"], "unread", None, "the y-axis" if vertical else "the x-axis"))
            continue
        words = GRAPH_AXES.get(graph, set()) if vertical else TIME_WORDS
        name = "the y-axis" if vertical else "the x-axis"
        findings.append(Finding(region["region_id"], "ok" if label in words else "axis_label", "axis", name))
    segments = []
    for region in regions:
        element = region.get("sketch_element") or {}
        if element.get("kind") != "line_segment":
            continue
        angle = _angle(element)
        if not _readable(region) or angle is None:
            findings.append(Finding(region["region_id"], "unread"))
            continue
        start, end = element["from"], element["to"]
        if end["x"] < start["x"]:
            angle = (angle + 180) % 360
        left = min(start["x"], end["x"])
        slope = _signed(angle)
        shape = "flat" if abs(slope) <= FLAT_DEG else "up" if slope > 0 else "down"
        segments.append((left, region, shape))
    segments.sort(key=lambda item: item[0])
    for index, (_, region, shape) in enumerate(segments):
        if index >= len(truth):
            findings.append(Finding(region["region_id"], "extra_segment", None, f"part {index + 1} of your line"))
            continue
        about, wanted = truth[index]
        outcome = "ok" if shape == wanted else "wrong_shape"
        findings.append(Finding(region["region_id"], outcome, about, f"part {index + 1} of your line"))
    for about, _ in truth[len(segments) :]:
        if not any(f.outcome == "unread" for f in findings):
            findings.append(Finding(None, "missing", about, "a part of the line"))
    return SketchCheck("motion_graph", findings)


def truth_visual(engine: Any) -> dict[str, Any] | None:
    """The checked free-body or motion-graph visual of the current problem, the newest first, or None."""
    shown = {v["visual_id"] for v in engine.state.shown_visuals if v.get("kind") in GRAPH_KINDS}
    for record in reversed(list(engine.persisted.values())):
        spec = record.get("content") if record.get("kind") == "visual" else None
        if isinstance(spec, dict) and spec.get("visual_id") in shown and spec.get("status", "ok") == "ok":
            return spec
    return None


async def check(spec: dict[str, Any] | None, regions: list[dict[str, Any]]) -> SketchCheck:
    if spec is None:
        return SketchCheck(None, reason="no_truth")
    if not any(r.get("sketch_element") for r in regions):
        return SketchCheck(spec["type"], reason="nothing_read")
    params = spec.get("params") or {}
    if spec["type"] == "free_body":
        return await check_free_body(params, regions)
    return check_motion_graph(params, regions)


# --- what the student hears ------------------------------------------------------------------------------------

# Engine lines (never model text): what could not be checked, and why.
NO_TRUTH = ("I can only check a sketch against a diagram I've worked out for this question, and I don't have one yet. "
            "Tell me what you drew and we can talk it through.")  # fmt: skip
NOTHING_READ = "I couldn't make out the parts of your sketch, so I haven't checked it. Could you retake the photo?"
UNREAD_ONE = "I couldn't read one part of your sketch clearly, so I haven't checked that part."
UNREAD_SOME = "I couldn't read {n} parts of your sketch clearly, so I haven't checked those."

PROBLEM_NOTES = {
    "wrong_direction": "the {name} arrow the student drew points in the wrong direction",
    "opposite": "the {name} arrow the student drew points the opposite way to the real force",
    "extra": "the student drew an arrow labelled {name} that is not one of the forces on the object",
    "missing": "the student's diagram has no {name} arrow",
    "wrong_shape": "{name} has the wrong shape for that stretch of the motion",
    "extra_segment": "{name} is a stretch the motion does not have",
    "axis_label": "{name} is labelled with the wrong quantity",
}


def coach_note(result: SketchCheck, capture_id: str) -> str:
    """The Coach's instruction for the turn: what the engine found, which element to point at, and never the answer."""
    first = result.problems[0]
    note = ("the student photographed their own sketch and the engine compared it with the checked diagram. "
            + PROBLEM_NOTES[first.outcome].format(name=first.name or "one"))  # fmt: skip
    if first.region_id is not None:
        note += ("; it is lit on their photo already: refer to it in words (the arrow you labelled ...) and ask "
                 "one question that leads them to see what is off; in a voice turn's say_spoken only, you may mark it "
                 "with "
                 f'<point at="{capture_id}:{first.region_id}"/>, never in say')  # fmt: skip
    else:
        note += "; ask one question that leads them to notice it"
    others = len(result.problems) - 1
    if others:
        note += f"; {others} other part(s) also need another look, but ask about this one first"
    return note + ("; never say which way it should point, what the right shape is or what the label should be, "
                   "and never draw it for them")  # fmt: skip


async def sketch_turn(engine: Any, capture: dict[str, Any], ids: Any) -> Any:
    """The turn for a confirmed photo of the student's sketch (`check_my_working` with a sketch capture)."""
    from agent_core.engine import turn_scope
    from agent_core.engine.ui import emit_part

    turn = engine._begin("check_my_working", ids)
    with turn_scope(turn.turn_id):
        try:
            result = await check(truth_visual(engine), capture.get("regions") or [])
            engine._log("info", "engine", "sketch_photo.checked", turn_id=turn.turn_id, kind=result.kind,
                        reason=result.reason, outcomes=[f.outcome for f in result.findings])  # fmt: skip
            if result.reason is not None:
                return engine._finish_text(turn, NO_TRUTH if result.reason == "no_truth" else NOTHING_READ, None,
                                           "completed")  # fmt: skip
            regions = [f"{capture['capture_id']}:{f.region_id}" for f in result.findings if f.region_id]
            engine.state.photo_marks = regions
            if result.unread:
                n = len(result.unread)
                line = UNREAD_ONE if n == 1 else UNREAD_SOME.format(n=n)
                part = engine.events.start_part("text")
                engine.events.text(part, line)
                engine.events.end_part(part)
                engine._persist(turn, part, "text", line)
            problem = result.problems[0] if result.problems else None
            if problem is not None and problem.region_id is not None:
                event = {"type": "board.highlight", "board_id": engine.state.board.board_id, "step_ids": [],
                         "ttl_ms": None, "photo_regions": [f"{capture['capture_id']}:{problem.region_id}"]}  # fmt: skip
                part = emit_part(engine.events, "board", [event])
                engine._persist(turn, part, "board", [event])
            engine.state.transcript.append({"role": "user", "content": capture.get("text", ""), "kind": "attempt",
                                            "source": "ocr_text"})  # fmt: skip
            if problem is None:
                checked = sum(f.outcome == "ok" for f in result.findings)
                unread = ", though some parts could not be read" if result.unread else ""
                note = (f"the student photographed their own sketch; the engine checked {checked} part(s) of it "
                        f"against the checked diagram and found nothing wrong{unread}: say so briefly and ask what "
                        "it shows")  # fmt: skip
                return await engine._coach_turn(turn, intent_note=note)
            return await engine._coach_turn(turn, intent_note=coach_note(result, capture["capture_id"]))
        except Exception:
            engine._finish_crashed(turn)
            raise
