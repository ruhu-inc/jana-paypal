"""The online sketch around a tutor turn (VSL-43, VSL-44, MDL-33, D103, D106, D107).

Free drawing is a fallback, never the normal path. Only a turn served by an online candidate may carry `sketch_wanted`
(the decode schema offered to a local candidate never has it, VSL-01). On the accepted turn the engine's template
selector runs first: a visual the turn already asks for, or a catalogue kind of the problem's subject named by the
drawing the turn describes, fits, and then no sketch is made. Only with no fitting template, an online session and
allowance left does the engine make its own sketch call, to online candidates only, started when the turn is accepted
and run in parallel with its display and speech, so speech never waits for it. The result is validated, checked and
resolved (sketch.py) and emitted as `visual.sketch` in the turn it belongs to; one ready after `sketch_deadline_ms`
comes with the engine's late line. An unchecked sketch comes with the engine's spoken notice; a sketch that is not a
sketch at all is dropped silently with a log record. The trace's gate entries record whether the selector found a
template and whether a call was made; the `sketch.outcome` log record says how it ended (OBS-06)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from agent_core.assets.generated.visual_catalogue import VISUAL_CATALOGUE
from agent_core.contracts import validator
from agent_core.engine import turn_scope
from agent_core.engine.sketch import SketchContext, SketchRefused, resolve
from agent_core.engine.state import numbers_in
from agent_core.router import Message as ProviderMessage

# Engine-owned lines (VSL-44): never model text. Their copy keys are visual.sketch_spoken_unchecked and
# visual.sketch_added_late in the client catalogue.
SPOKEN_UNCHECKED = "This is a rough sketch, and I haven't checked it."
ADDED_LATE = "I've sketched it on the board."
SKETCH_SYSTEM = (
    "You draw one rough sketch as JSON in the sketch contract: sketch_version 1, a frame (visual_id null and its "
    "bounds), at most 40 elements from line, arrow, arc, circle, polygon, point, label and axis in that frame's "
    "coordinates, and the checks you ask the engine to run (claim and the check id, known id or curve id compared). "
    "Labels are short; a number in a label must be one of the verified values given. Never SVG, HTML or path data."
)
TURN_CONTRACT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "schemas" / "agents" / "tutor-turn.schema.json").read_text()
)
SUBJECTS = {"maths": "mathematics", "math": "mathematics"}
_WORD = re.compile(r"[a-z]+")


def template_for(wanted: str, tutor_turn: dict[str, Any], subject: str) -> str | None:
    """The catalogue visual that fits the drawing the turn describes, or None (VSL-43): a visual the turn already asks
    for, else a catalogue kind of this subject named by the description (`free body`, `ray diagram`, `number line`)."""
    if tutor_turn.get("visual"):
        return "requested_visual"
    words = set(_WORD.findall(wanted.lower().replace("-", " ").replace("_", " ")))
    subject = SUBJECTS.get(subject, subject)
    for kind in VISUAL_CATALOGUE["kinds"]:
        if subject not in kind["subjects"] or kind["renderer"] == "board":
            continue
        named = set(_WORD.findall(kind["name"].lower())) - {"and", "of", "the", "a", "diagram", "chart", "graph"}
        if named and named <= words:
            return str(kind["kind"])
    return None


# The diagram kinds of the sketch suite (EVL-28), by words in the drawing the turn describes; anything else is "other".
SKETCH_KINDS = {
    "ray_diagram": {"ray", "rays", "lens", "mirror", "refraction", "reflection"},
    "forces": {"force", "forces", "weight", "tension", "friction", "reaction", "resultant", "push", "pull"},
    "graph": {"graph", "curve", "axis", "axes", "plot", "gradient", "intercept", "parabola", "line"},
    "geometry": {"triangle", "angle", "angles", "circle", "polygon", "bearing", "shape", "chord", "tangent"},
}


def sketch_kind(wanted: str) -> str:
    """The suite's diagram kind a described drawing falls under (EVL-28)."""
    words = set(_WORD.findall(wanted.lower()))
    return next((kind for kind, cues in SKETCH_KINDS.items() if words & cues), "other")


def offer_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The tutor turn's decode schema for an online candidate: the bundled one with `sketch_wanted` back in (VSL-43)."""
    wanted = TURN_CONTRACT["properties"].get("sketch_wanted")
    if wanted is None or "properties" not in schema or "sketch_wanted" in schema["properties"]:
        return schema
    return {**schema, "properties": {**schema["properties"], "sketch_wanted": wanted}}


def _verified(engine: Any) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {k: list(v) for k, v in engine.state.check_values.items()}
    for known in engine.state.board.knowns:
        found = numbers_in(str(known.get("value", "")))
        if known.get("known_id") and found:
            values[str(known["known_id"])] = found[:2]
    return values


def _curves(engine: Any) -> dict[str, str]:
    """The curves of the visuals on screen by part id (a function graph's expressions), for point_on_curve."""
    shown = {v["visual_id"] for v in engine.state.shown_visuals}
    curves: dict[str, str] = {}
    for record in engine.persisted.values():
        spec = record["content"] if record["kind"] == "visual" else None
        if not isinstance(spec, dict) or spec.get("visual_id") not in shown:
            continue
        for curve in (spec.get("params") or {}).get("functions", []) or []:
            if isinstance(curve, dict) and curve.get("id") and curve.get("expression"):
                curves[f"{curve['id']}"] = str(curve["expression"])
    return curves


class SketchFlow:
    """One engine's sketches: the call in flight and the ones shown, by id (for "Remove sketch")."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.task: asyncio.Task[None] | None = None
        self.shown: dict[str, str] = {}  # sketch id -> the part that holds it

    def cancel(self) -> None:
        """A cancelled turn or a session change cancels the call in flight (MDL-33)."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def start(self, turn: Any, tutor_turn: dict[str, Any], served_online: bool) -> None:
        """After the turn is accepted: the selector, then, only when nothing fits, the parallel sketch call."""
        wanted = tutor_turn.get("sketch_wanted")
        if not wanted or getattr(turn, "fork", False):
            return
        engine = self.engine
        fit = template_for(str(wanted), tutor_turn, engine.state.subject or "")
        budget = getattr(engine.deps.router, "budget", None)
        live = sketch_kind(str(wanted)) in engine.deps.settings.sketch_live_kinds  # its EVL-28 gate passed
        allowed = served_online and live and (budget is None or budget.allows())
        called = fit is None and allowed
        # The trace's gate entries (OBS-06): whether the selector found a template and whether a call was made; the
        # call's outcome, latency and drops are the `sketch.outcome` log record, written when it settles.
        turn.extra_gates += [{"gate": "sketch_template", "fired": fit is not None, "nudges": 0, "attempt": 1,
                              "outcome": "passed" if fit is not None else "skipped"},
                             {"gate": "sketch_call", "fired": called, "nudges": 0, "attempt": 1,
                              "outcome": "passed" if called else "skipped"}]  # fmt: skip
        engine._log("info", "engine", "sketch.selector", turn_id=turn.turn_id, template=fit, called=called)
        if not called:
            return
        report: dict[str, Any] = {"turn_id": turn.turn_id}
        self.cancel()
        problem = engine.state.problem.problem_id if engine.state.problem else None
        self.task = asyncio.ensure_future(self._run(turn, tutor_turn, str(wanted), time.monotonic(), problem, report))

    async def _run(self, turn: Any, tutor_turn: dict[str, Any], wanted: str, accepted: float, problem: str | None,
                   report: dict[str, Any]) -> None:  # fmt: skip
        try:
            await self._settle(turn, tutor_turn, wanted, accepted, problem, report)
        finally:
            report.setdefault("latency_ms", int((time.monotonic() - accepted) * 1000))
            self.engine._log("info", "engine", "sketch.outcome", **report)

    async def _settle(self, turn: Any, tutor_turn: dict[str, Any], wanted: str, accepted: float, problem: str | None,
                      gate: dict[str, Any]) -> None:  # fmt: skip
        engine = self.engine
        settings = engine.deps.settings
        try:
            with turn_scope(turn.turn_id):
                request = await self._call(tutor_turn, wanted, gate)
                if request is None:
                    gate["outcome"] = "dropped"
                    return
                context = SketchContext(values=_verified(engine), curves=_curves(engine), guard=engine.sketch_guard,
                                        traced=engine.sketch_traced, max_elements=settings.sketch_max_elements,
                                        max_labels=settings.sketch_max_labels,
                                        position_share=settings.sketch_position_share,
                                        length_share=settings.sketch_length_share,
                                        angle_deg=settings.sketch_angle_deg)  # fmt: skip
                resolved = await resolve(request, context, engine.sketch_tool)
        except SketchRefused as refused:
            gate["outcome"] = "dropped"
            engine._log("info", "engine", "sketch.dropped", turn_id=turn.turn_id, reason=str(refused))
            return
        except asyncio.CancelledError:
            gate["outcome"] = "cancelled"
            raise
        except Exception as error:  # a failed call never touches the turn
            gate["outcome"] = "failed"
            engine._log("warning", "engine", "sketch.failed", turn_id=turn.turn_id, error=type(error).__name__)
            return
        current = engine.state.problem.problem_id if engine.state.problem else None
        if current != problem:  # the student moved on: the result is discarded (MDL-33)
            gate["outcome"] = "discarded"
            return
        if not resolved["elements"]:
            gate["outcome"] = "dropped"
            engine._log("info", "engine", "sketch.dropped", turn_id=turn.turn_id, reason="no_elements")
            return
        if engine.events.turn_id not in (None, turn.turn_id):  # a newer turn is running: the sketch is not its
            gate["outcome"] = "superseded"
            return
        late = (time.monotonic() - accepted) * 1000 > settings.sketch_deadline_ms
        gate.update(outcome="late" if late else "shown", status=resolved["status"],
                    latency_ms=int((time.monotonic() - accepted) * 1000),
                    dropped=[d["reason"] for d in resolved["dropped"]])  # fmt: skip
        self.emit(turn, resolved, late)

    async def _call(self, tutor_turn: dict[str, Any], wanted: str, report: dict[str, Any]) -> Any:
        """The engine's own sketch call (MDL-33): online candidates only, the accepted turn, the problem and its
        verified values, the sketch contract as the response format, at most `sketch_max_completion_tokens`."""
        engine = self.engine
        problem = engine.state.problem.statement if engine.state.problem else ""
        user = json.dumps({"drawing": wanted, "turn": tutor_turn.get("say", ""), "problem": problem,
                           "verified": {k: [str(v) for v in vals] for k, vals in _verified(engine).items()},
                           "curves": _curves(engine)})  # fmt: skip
        schema = validator("visuals/sketch")
        result = await engine.deps.router.complete(
            "coach", [ProviderMessage("system", SKETCH_SYSTEM), ProviderMessage("user", user)],
            output=("sketch", schema.schema, schema), allow=lambda c: getattr(c, "locality", "local") == "cloud",
            max_tokens=engine.deps.settings.sketch_max_completion_tokens,
        )  # fmt: skip
        # The call's tokens and cost, for the cost model's own sketch line (BIZ-08) and the allowance (BIZ-07).
        calls = result.record.model_calls()
        report["tokens"] = {"input": sum(int(c["tokens"]["input"]) for c in calls),
                            "output": sum(int(c["tokens"]["output"]) for c in calls),
                            "cache_read": sum(int(c["tokens"].get("cache_read", 0)) for c in calls)}  # fmt: skip
        report["cost_usd"] = sum(float(c.get("cost_usd") or 0) for c in calls)
        return result.structured if result.schema_valid else None

    def emit(self, turn: Any, resolved: dict[str, Any], late: bool) -> None:
        """`visual.sketch` in the turn it belongs to, with the engine's late line and, unchecked, its spoken notice."""
        engine = self.engine
        events = engine.events
        ended = events.turn_id is None
        events.turn_id = turn.turn_id  # the parts belong to the turn the sketch was made for, ended or not
        try:
            part = events.start_part("visual")
            events.emit({"type": "visual.sketch", "part_id": part, "sketch": resolved})
            events.end_part(part)
            engine._persist(turn, part, "sketch", resolved)
            self.shown[resolved["sketch_id"]] = part
            unchecked = [SPOKEN_UNCHECKED] if resolved["status"] == "not_checked" else []
            for line in ([ADDED_LATE] if late else []) + unchecked:
                said = events.start_part("text")
                events.text(said, line)
                events.end_part(said)
                engine._persist(turn, said, "text", line)
        finally:
            if ended:
                events.turn_id = None

    def remove(self, sketch_id: str) -> str | None:
        """ "Remove sketch": the part that held it, gone from the engine's record (VSL-43); None when not shown here."""
        part = self.shown.pop(sketch_id, None)
        if part is not None:
            self.engine.persisted.pop(part, None)
        return part
