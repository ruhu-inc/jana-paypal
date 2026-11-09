"""Projectiles at O level: vertical, horizontal and angled projection (VER-17), with g from the stated constants
(VER-12). An angled launch is at `angle` degrees above the horizontal; its components are exact for any rational
angle (30° and 60° give surds), so two launches that land together compare exactly.

Up is positive and the origin is the ground directly below the launch point, recorded in every result. Times,
heights, range and the landing velocity are exact (surds where they arise) with 6 significant figure decimals; the
trajectory points for the simulation and the motion graphs are evenly spaced in time from launch to landing and
rounded to 6 significant figures. Air resistance is ignored, as at O level."""

from __future__ import annotations

from typing import Any

import sympy as sp

from tool_worker.functions import constants
from tool_worker.functions.maths import exact, significant
from tool_worker.functions.quantities import number, unit_scale
from tool_worker.functions.units import dimension_text, parse_quantity
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
SPEED, LENGTH = "m s⁻¹", "m"


def _given(raw: Any, field: str, dimension: str) -> sp.Rational:
    """A number in SI units, or a quantity with its unit ("72 km/h") converted exactly to SI."""
    if not isinstance(raw, str):
        return number(raw, field)
    quantity = parse_quantity(raw)
    found = dimension_text(quantity)
    if found != "1" and found != dimension:  # a bare number is already in SI units
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: found {found}, expected {dimension}")
    base = quantity.to_base_units().magnitude
    return sp.Rational(base.numerator, base.denominator)


def _gravity(args: dict[str, Any]) -> tuple[sp.Rational, Extras]:
    """g by the precedence of VER-12: question, item, pack, then the tool default (which always warns)."""
    entries = list(args.get("constants", []))
    if "g" in args:
        entries.append({"symbol": "g", "value": args["g"], "source": args["g_source"]})
    if not any(e["symbol"] == "g" for e in entries):
        entries.append({"symbol": "g", "source": "default"})
    [value], extras = constants.apply([sp.Symbol("g")], {"constants": entries})
    unit = next((r.get("unit") for r in extras.get("constants", []) if r["symbol"] == "g"), None)
    if unit:
        scale, _ = unit_scale(str(unit))
        value = value * scale
    if not (value.is_Rational and value > 0):
        raise WorkerError(Code.DOMAIN_ERROR, "g must be a positive number")
    extras["constants"] = [r for r in extras.get("constants", []) if r["symbol"] == "g"]
    return sp.Rational(value), extras


def _point(value: sp.Basic) -> float:
    return float(significant(value, 6))


def phys_projectile(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Time of flight, maximum height, range, landing velocity and trajectory points (VER-17)."""
    motion = args["motion"]
    speed = _given(args["speed"], "speed", SPEED)
    height = _given(args.get("height", 0), "height", LENGTH)
    if speed < 0 or height < 0:
        raise WorkerError(Code.DOMAIN_ERROR, "speed and height are magnitudes and cannot be negative")
    g, extras = _gravity(args)
    if motion == "vertical":
        if speed > 0 and "direction" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "a vertical launch needs a direction: up or down")
        ux, uy = sp.Integer(0), (speed if args.get("direction", "up") == "up" else -speed)
    elif motion == "angled":
        if "direction" in args:
            raise WorkerError(Code.DOMAIN_ERROR, "direction is for vertical motion; an angled launch has its angle")
        degrees = sp.nsimplify(number(args["angle"], "angle"), rational=True)
        if not 0 < degrees < 90:
            raise WorkerError(Code.DOMAIN_ERROR, "an angled launch is between 0 and 90 degrees above the horizontal")
        theta = degrees * sp.pi / 180
        ux, uy = sp.nsimplify(speed * sp.cos(theta)), sp.nsimplify(speed * sp.sin(theta))
    else:
        if "direction" in args:
            raise WorkerError(Code.DOMAIN_ERROR, "direction is for vertical motion; a horizontal launch has none")
        if height == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a horizontal launch needs a height above the ground")
        ux, uy = speed, sp.Integer(0)
    landing = (uy + sp.sqrt(uy**2 + 2 * g * height)) / g  # the positive root of y(t) = 0
    if landing == 0:
        raise WorkerError(Code.DOMAIN_ERROR, "the object starts on the ground and does not rise")
    rise = uy / g if uy > 0 else sp.Integer(0)
    top = height + uy * rise - g * rise**2 / 2
    vy_land = uy - g * landing
    impact_speed = sp.sqrt(ux**2 + vy_land**2)
    angle = sp.Integer(90) if ux == 0 else sp.atan(-vy_land / ux) * 180 / sp.pi

    count = int(args.get("points", 21))
    points = []
    for i in range(count):
        t = landing * sp.Rational(i, count - 1)
        y = height + uy * t - g * t**2 / 2
        points.append({"t": _point(t), "x": _point(ux * t), "y": _point(y), "vx": _point(ux), "vy": _point(uy - g * t)})
    value: dict[str, Any] = {
        "motion": motion,
        "positive_direction": "up",
        "origin": "ground below the launch point",
        "units": {"time": "s", "distance": "m", "speed": "m/s", "angle": "degrees"},
        "g": exact(g),
        "launch": {"speed": exact(speed), "height": exact(height), "vx": exact(ux), "vy": exact(uy),
                   **({"angle": exact(sp.nsimplify(number(args["angle"], "angle"), rational=True))}
                      if motion == "angled" else {})},
        "time_of_flight": exact(landing),
        "time_to_max_height": exact(rise),
        "max_height": exact(top),
        "range": exact(ux * landing),
        "landing": {"vx": exact(ux), "vy": exact(vy_land), "speed": exact(impact_speed),
                    "angle_below_horizontal": exact(angle)},
        "points": points,
    }  # fmt: skip
    working = [
        {"label": "y(t) = h + u_y t - g t^2/2 = 0", "value": value["time_of_flight"]["text"], "unit": "s"},
        {"label": "maximum height h + u_y^2/(2g)", "value": value["max_height"]["text"], "unit": "m"},
        {"label": "range u_x T", "value": value["range"]["text"], "unit": "m"},
    ]
    return value, {**extras, "working": working, "method": "exact", "confidence": "proved"}
