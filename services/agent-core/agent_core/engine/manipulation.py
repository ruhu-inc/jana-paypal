"""Manipulable visuals as the student's turn (VSL-42, D103).

The engine opens a manipulation on a visual on screen (`visual.await`), naming its adjustable parts and keeping the
key for each; the student drags a point, sets a force or slides a value, and "Check" sends the values
(`POST /v1/visuals/{visual_id}/answer`). Each value becomes the line the student would have typed for that kind, and
the same verification tool marks it as it would that typed line (`math_check_answer`, VER-13), so a placement and a
typed answer can never be marked differently. The result is the student's board line with its status (UXS-16); the
model never sets the student's values.

Kinds (S3): `number_line` (a point, typed `x = 4`, or an interval, typed as its inequality), `function_graph` (a point
such as a root or turning point, typed `(2, 0)`) and `free_body` (a force's size and direction, typed `49 N at 270°`,
each part checked)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

RunTool = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any] | None, str | None]]]
KINDS = ("number_line", "function_graph", "free_body")
_POINT = re.compile(r"^\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)$")
_FORCE = re.compile(r"^\s*(?P<size>.+?)\s+at\s+(?P<angle>-?\d+(?:\.\d+)?)\s*°?\s*$")


async def checker_value(engine: Any, name: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    """One verification tool call as the checker, for a placement: its value and check id, or nothing if it failed."""
    from agent_core.engine.events import new_id
    from agent_core.tools.executor import ToolCall

    result = await engine.deps.executor.run_one(
        ToolCall(f"call_visual{new_id('cli')[4:]}_0", name, args),
        "checker",
        engine._tool_session("checker", "full"),
    )
    if result.get("status") != "ok":
        return None, None
    checks = result.get("meta", {}).get("checks") or []
    return result.get("data", {}).get("value"), checks[0]["check_id"] if checks else None


class ManipulationRefused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def typed_line(kind: str, part_id: str, value: str, variable: str = "x") -> str:
    """The line a student would type for this placement."""
    value = value.strip()
    if kind == "number_line":
        if re.search(r"[<>≤≥]", value):
            return value  # an interval is typed as its inequality
        return f"{variable} = {value}"
    if kind == "function_graph":
        return value if value.startswith("(") else f"{variable} = {value}"
    if kind == "free_body":
        found = _FORCE.match(value)
        if found is None:
            raise ManipulationRefused("validation_failed", f"{part_id}: a force is size and direction, '49 N at 270'")
        return f"{part_id} = {found['size']} at {found['angle']}°"
    raise ManipulationRefused("validation_failed", f"{kind} is not a manipulable kind")


async def _check(student: str, key: str, variable: str, run: RunTool) -> tuple[str, str | None]:
    """One typed value against its key by `math_check_answer`: correct, incorrect or unchecked, with the check id."""
    checked, check_id = await run("math_check_answer", {"student": student, "key": key, "rule": {"quantity": "exact"},
                                                        "variables": [variable]})  # fmt: skip
    if checked is None or check_id is None:
        return "unchecked", None
    status = (checked.get("components") or {}).get("value")
    return ("correct" if status == "correct" else "incorrect" if status == "incorrect" else "unchecked"), check_id


async def check_typed(kind: str, line: str, key: str, variable: str, run: RunTool) -> tuple[str, str | None]:
    """A typed line of this kind against its key, exactly as a placement is checked (free-body: size, then angle)."""
    if kind == "free_body":
        given, expected = _FORCE.match(line.split("=", 1)[-1]), _FORCE.match(key)
        if given is None or expected is None:
            return "unchecked", None
        size, check_id = await _check(given["size"], expected["size"], variable, run)
        if size != "correct":
            return size, check_id
        return await _check(given["angle"], expected["angle"], variable, run)
    student, wanted = _value(line), _value(key)
    point, target = _POINT.match(student), _POINT.match(wanted)
    if point is not None or target is not None:  # a point: each coordinate checked, x first
        if point is None or target is None:
            return "incorrect" if point is None and target is not None else "unchecked", None
        status, check_id = "correct", None
        for a, b in zip(point.groups(), target.groups(), strict=True):
            status, check_id = await _check(a, b, variable, run)
            if status != "correct":
                break
        return status, check_id
    return await _check(student, wanted, variable, run)


def _value(text: str) -> str:
    """What is compared: the value after `x =`, or an inequality or point as written."""
    return text.split("=", 1)[-1].strip() if "=" in text and not re.search(r"[<>≤≥]", text) else text.strip()


async def mark(manipulation: dict[str, Any], values: list[dict[str, str]], run: RunTool) -> dict[str, Any]:
    """Every submitted value checked against its part's key; the line the board shows, the status and the check."""
    kind, variable, keys = manipulation["kind"], manipulation.get("variable", "x"), manipulation["keys"]
    lines, statuses, check_id = [], [], None
    seen = set()
    for item in values:
        part = item["part_id"]
        if part not in keys or part in seen:
            raise ManipulationRefused("validation_failed", f"{part} is not an adjustable part of this visual")
        seen.add(part)
        line = typed_line(kind, part, item["value"], variable)
        status, check = await check_typed(kind, line, keys[part], variable, run)
        lines.append(line)
        statuses.append(status)
        check_id = check_id or check
        if status == "incorrect":
            check_id = check
    missing = sorted(set(keys) - seen)
    if missing:
        raise ManipulationRefused("validation_failed", f"set {missing[0]} before checking")
    status = "incorrect" if "incorrect" in statuses else "unchecked" if "unchecked" in statuses else "correct"
    return {"latex": "; ".join(lines)[:300], "status": status, "check_id": check_id}


def exercise_key(spec: dict[str, Any], part_id: str) -> str:
    """A matching exercise from an already resolved visual; never accept a key supplied by the client."""
    if spec.get("status") != "ok" or not spec.get("numbers_from"):
        raise ManipulationRefused("validation_failed", "this diagram has no checked values to practise")
    params, kind = spec.get("params", {}), spec.get("type")
    if kind == "number_line":
        for interval in params.get("intervals", []):
            for edge in ("from", "to"):
                if part_id == f"{interval['id']}.{edge}" and str(interval.get(edge)) not in ("inf", "-inf", "None"):
                    return str(interval[edge])
    elif kind == "function_graph":
        for curve, features in spec.get("computed", {}).get("features", {}).items():
            for feature in features:
                if part_id == f"{curve}.{feature['id']}":
                    return f"({feature['x']}, {feature['y']})"
    elif kind == "free_body":
        for force in params.get("forces", []):
            if part_id == force["id"]:
                return f"{force['magnitude']} at {force['direction_deg']}"
    raise ManipulationRefused("validation_failed", "choose a point, end point or force on this diagram")
