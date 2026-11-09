"""A level mechanics (VER-31): projectiles at an angle, moments, friction on inclines, connected particles,
work-energy-power, momentum and impulse, circular motion and simple harmonic motion.

Every task is solved exactly: given decimals stay exact rationals, angles in degrees become exact trigonometric
values (an angle given by its tangent, "tan a = 3/4", gives rational sines and cosines), and every result carries
its exact text, LaTeX and a 6 significant figure decimal. g comes from the stated constants by the precedence of
VER-12 and is resolved only when a task needs it, so a collision never reports a default g. Contradictory or
impossible input (a beam that would need a downward support, a collision whose objects do not approach, a
coefficient of restitution above 1) is a DOMAIN_ERROR with no number; input that leaves the answer undetermined (a
statically indeterminate beam, a rough surface with no coefficient of friction) is `unknown` with its reason, never
a guess (VER-27).

Sign conventions are stated in every result: up is positive for vertical motion; along a slope, up the slope is
positive; in a collision, positive is from A towards B; in simple harmonic motion, displacement is measured from
the centre, positive on the side of the first displacement. Strings are light and inextensible, pulleys smooth and
air resistance is ignored unless a resistance is given."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import sympy as sp

from tool_worker.functions.common import as_expr
from tool_worker.functions.maths import exact
from tool_worker.functions.projectile import _gravity
from tool_worker.functions.quantities import number
from tool_worker.functions.units import dimension_text, parse_quantity
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
Value = dict[str, Any]

# Accepted SI dimensions per kind of quantity (a bare number is taken as already in SI units).
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "length": ("m",),
    "speed": ("m s⁻¹",),
    "acceleration": ("m s⁻²",),
    "mass": ("kg",),
    "force": ("kg m s⁻²",),
    "time": ("s",),
    "energy": ("kg m² s⁻²",),
    "power": ("kg m² s⁻³",),
    "energy_or_power": ("kg m² s⁻²", "kg m² s⁻³"),
    "impulse": ("kg m s⁻¹",),
    "angular_speed": ("s⁻¹ rad", "s⁻¹"),
    "frequency": ("s⁻¹",),
    "ratio": (),
}
UNITS = {
    "length": "m", "speed": "m/s", "acceleration": "m/s^2", "mass": "kg", "force": "N", "time": "s", "energy": "J",
    "power": "W", "momentum": "N s", "angle": "degrees", "angular_speed": "rad/s", "frequency": "Hz", "moment": "N m",
}  # fmt: skip
NUMBER = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*$")
FRACTION = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*/\s*(\d+(?:\.\d*)?|\.\d+)\s*$")
UNKNOWN_MARK = "?"


def _tidy(value: Any) -> sp.Expr:
    """A canonical exact form: rationals as they are, anything else simplified."""
    expr = as_expr(value)
    return expr if expr.is_Rational else sp.simplify(expr)


def _sign(value: Any) -> int:
    """The sign of an exact real number (surds and trigonometric values of rational degrees)."""
    expr = as_expr(value)
    if expr.is_zero:
        return 0
    if expr.is_positive:
        return 1
    if expr.is_negative:
        return -1
    approx = sp.N(expr, 60)
    if abs(approx) < sp.Float("1e-45", 60):  # a zero SymPy cannot simplify away
        return 0
    return 1 if approx > 0 else -1


def _equal(a: Any, b: Any) -> bool:
    return _sign(as_expr(a) - as_expr(b)) == 0


def _quantity(raw: Any, field: str, kind: str) -> tuple[sp.Expr, str | None]:
    """A given value as an exact SI number, with the dimension it was stated in (None for a bare number)."""
    if not isinstance(raw, str) or NUMBER.match(raw):
        return number(raw, field), None
    fraction = FRACTION.match(raw)
    if fraction:
        top, bottom = number(fraction.group(1), field), number(fraction.group(2), field)
        if bottom == 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{field}: division by zero")
        return top / bottom, None
    quantity = parse_quantity(raw)
    found = dimension_text(quantity)
    allowed = DIMENSIONS[kind]
    if found != "1" and found not in allowed:
        expected = " or ".join(allowed) if allowed else "a plain number"
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: found {found}, expected {expected}")
    base = quantity.to_base_units().magnitude
    return sp.Rational(base.numerator, base.denominator), (None if found == "1" else found)


class Problem:
    """The arguments of one call, read on demand, with g resolved only when a task needs it."""

    def __init__(self, args: dict[str, Any], context: CallContext) -> None:
        self.args, self.check = args, context.check_id
        self.working: list[dict[str, Any]] = []
        self.extras: Extras = {}
        self._g: sp.Rational | None = None

    def has(self, key: str) -> bool:
        return key in self.args and self.args[key] != UNKNOWN_MARK

    def get(self, key: str, kind: str, *, default: Any = None, sign: str = "any") -> sp.Expr | None:
        """A given quantity (None when absent and no default); `sign` is any, positive or nonnegative."""
        if not self.has(key):
            return None if default is None else as_expr(default)
        value, _ = _quantity(self.args[key], key, kind)
        if sign == "positive" and not value > 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{key} must be positive")
        if sign == "nonnegative" and value < 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{key} cannot be negative")
        return value

    def need(self, key: str, kind: str, *, sign: str = "positive") -> sp.Expr:
        value = self.get(key, kind, sign=sign)
        if value is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"{self.args.get('task')} needs {key}")
        return value

    @property
    def g(self) -> sp.Rational:
        if self._g is None:
            self._g, extras = _gravity(self.args)
            self.extras.update(extras)
        return self._g

    def angle(self, low: int, high: int, *, key: str = "angle", tan_key: str = "tan_angle",
              sin_key: str | None = "sin_angle", default_zero: bool = False
              ) -> tuple[sp.Expr, sp.Expr, sp.Expr]:  # fmt: skip
        """(sin, cos, degrees) from an angle in degrees, its tangent or its sine, within [low, high] degrees."""
        given = [k for k in (key, tan_key, sin_key) if k is not None and k in self.args]
        if len(given) > 1:
            raise WorkerError(Code.DOMAIN_ERROR, f"give one of {', '.join(given)}, not several")
        if key in self.args:
            degrees = number(self.args[key], key)
            sin, cos = sp.sin(degrees * sp.pi / 180), sp.cos(degrees * sp.pi / 180)
        elif tan_key in self.args:
            tan, _ = _quantity(self.args[tan_key], tan_key, "ratio")
            sin, cos = tan / sp.sqrt(1 + tan**2), 1 / sp.sqrt(1 + tan**2)
            degrees = sp.atan(tan) * 180 / sp.pi
        elif sin_key is not None and sin_key in self.args:
            sin, _ = _quantity(self.args[sin_key], sin_key, "ratio")
            if _sign(sp.Abs(sin) - 1) > 0:
                raise WorkerError(Code.DOMAIN_ERROR, f"{sin_key} must be between -1 and 1")
            cos = sp.sqrt(1 - sin**2)
            degrees = sp.asin(sin) * 180 / sp.pi
        elif default_zero:
            return sp.Integer(0), sp.Integer(1), sp.Integer(0)
        else:
            raise WorkerError(Code.DOMAIN_ERROR, f"give the angle in degrees ({key}) or its tangent ({tan_key})")
        if _sign(degrees - low) < 0 or _sign(degrees - high) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{key} must be between {low} and {high} degrees")
        return sin, cos, degrees

    def line(self, label: str, value: sp.Expr, unit: str = "") -> None:
        entry: dict[str, Any] = {"label": label, "value": sp.sstr(value)}
        if unit:
            entry["unit"] = unit
        self.working.append({**entry, "check_id": self.check})

    def done(self, value: Value) -> tuple[Value, Extras]:
        return value, {**self.extras, "working": self.working, "method": "exact", "confidence": "proved"}


def _unknown(task: str, reason: str, needs: str) -> tuple[Value, Extras]:
    """Undetermined by the input (VER-27): say what is missing, never guess."""
    return {"task": task, "status": "unknown", "reason": reason, "needs": needs}, {
        "method": "heuristic", "confidence": "undecided",
        "warnings": [{"code": "domain_restricted", "message": needs}],
    }  # fmt: skip


def _out(value: Any) -> dict[str, Any]:
    return exact(_tidy(value))


class Undetermined(Exception):
    """The input does not determine the answer: the result is `unknown` with this reason."""

    def __init__(self, reason: str, needs: str) -> None:
        super().__init__(needs)
        self.reason, self.needs = reason, needs


# --- projectiles at an angle ------------------------------------------------------------------------------------


def _projectile(p: Problem) -> Value:
    u = p.need("speed", "speed", sign="nonnegative")
    sin, cos, degrees = p.angle(-90, 90)
    h = p.get("height", "length", default=0, sign="nonnegative")
    assert h is not None
    ux, uy = u * cos, u * sin
    if h == 0 and _sign(uy) <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "launched from the ground, the projectile must be thrown above the "
                          "horizontal")  # fmt: skip
    g = p.g
    flight = _tidy((uy + sp.sqrt(uy**2 + 2 * g * h)) / g)  # the positive root of y(t) = 0
    rising = _sign(uy) > 0
    rise = uy / g if rising else sp.Integer(0)
    top = h + (uy**2 / (2 * g) if rising else 0)
    vy_land = uy - g * flight
    landing_angle = sp.Integer(90) if _sign(ux) == 0 else sp.atan(-vy_land / ux) * 180 / sp.pi
    value: Value = {
        "task": "projectile_angle",
        "positive_direction": "up",
        "origin": "ground below the launch point; x horizontal in the direction of launch",
        "units": {"time": "s", "distance": "m", "speed": "m/s", "angle": "degrees"},
        "g": exact(g),
        "launch": {"speed": _out(u), "angle": _out(degrees), "height": _out(h), "vx": _out(ux), "vy": _out(uy)},
        "time_of_flight": _out(flight),
        "time_to_max_height": _out(rise),
        "max_height": _out(top),
        "range": _out(ux * flight),
        "landing": {"vx": _out(ux), "vy": _out(vy_land), "speed": _out(sp.sqrt(u**2 + 2 * g * h)),
                    "angle_below_horizontal": _out(landing_angle)},
    }  # fmt: skip
    p.line("u_x = u cos a, u_y = u sin a", sp.Tuple(_tidy(ux), _tidy(uy)), "m/s")
    p.line("y(t) = h + u_y t - g t^2/2 = 0", flight, "s")
    p.line("maximum height h + u_y^2/(2g)", _tidy(top), "m")
    p.line("range u_x T", _tidy(ux * flight), "m")
    if "time" in p.args:
        t = p.need("time", "time", sign="nonnegative")
        if _sign(t - flight) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"the projectile lands at t = {value['time_of_flight']['decimal']} s,"
                              " before that time")  # fmt: skip
        vy = uy - g * t
        direction = (90 if _sign(vy) >= 0 else -90) if _sign(ux) == 0 else sp.atan(vy / ux) * 180 / sp.pi
        value["at_time"] = {"time": _out(t), "x": _out(ux * t), "y": _out(h + uy * t - g * t**2 / 2), "vx": _out(ux),
                            "vy": _out(vy), "speed": _out(sp.sqrt(ux**2 + vy**2)),
                            "angle_above_horizontal": _out(direction)}  # fmt: skip
    if "horizontal_distance" in p.args:
        d = p.need("horizontal_distance", "length", sign="nonnegative")
        if _sign(ux) <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a vertical launch never moves horizontally")
        t = d / ux
        if _sign(t - flight) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the projectile lands before it travels that far")
        value["at_distance"] = {"x": _out(d), "time": _out(t), "y": _out(h + uy * t - g * t**2 / 2)}
    return value


# --- moments and equilibrium ------------------------------------------------------------------------------------


def _moments(p: Problem) -> Value:
    length = p.need("length", "length")
    unknowns: list[sp.Symbol] = []

    def field(item: dict[str, Any], key: str, kind: str, name: str, sign: str = "nonnegative") -> sp.Expr | None:
        if key not in item:
            return None
        if item[key] == UNKNOWN_MARK:
            symbol = sp.Symbol(name)
            unknowns.append(symbol)
            return symbol
        value, _ = _quantity(item[key], f"{item['id']}.{key}", kind)
        if sign == "nonnegative" and value < 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{item['id']}.{key} cannot be negative")
        return value

    def required(value: sp.Expr | None, item: dict[str, Any], key: str) -> sp.Expr:
        if value is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"{item['id']} needs its {key}")
        return value

    loads: list[tuple[str, sp.Expr, sp.Expr, sp.Expr | None]] = []  # id, distance, weight, mass
    items = list(p.args.get("loads", []))
    if "beam_mass" in p.args or "beam_weight" in p.args:  # a uniform beam's weight acts at its midpoint
        items.insert(0, {"id": "beam", "mass": p.args.get("beam_mass"), "force": p.args.get("beam_weight")})
    for item in items:
        if item["id"] == "beam":
            item = {k: v for k, v in item.items() if v is not None}
            distance: sp.Expr = length / 2
        else:
            distance = required(field(item, "distance", "length", f"x_{item['id']}"), item, "distance")
        mass, force = field(item, "mass", "mass", f"m_{item['id']}"), field(item, "force", "force", f"W_{item['id']}")
        if (mass is None) == (force is None):
            raise WorkerError(Code.DOMAIN_ERROR, f"load {item['id']}: give its force or its mass")
        weight = mass * p.g if mass is not None else required(force, item, "force")
        loads.append((item["id"], distance, weight, mass))
    supports: list[tuple[str, sp.Expr, sp.Expr]] = []
    for item in p.args.get("supports", []):
        distance = required(field(item, "distance", "length", f"x_{item['id']}"), item, "distance")
        reaction = field(item, "reaction", "force", f"R_{item['id']}")
        if reaction is None:  # an unstated reaction is what the question asks for
            reaction = sp.Symbol(f"R_{item['id']}")
            unknowns.append(reaction)
        supports.append((item["id"], distance, reaction))
    ids = [entry[0] for entry in loads] + [entry[0] for entry in supports]
    if len(set(ids)) != len(ids):
        raise WorkerError(Code.DOMAIN_ERROR, "every load and support needs its own id")
    for name, distance, *_ in [*loads, *supports]:
        if not distance.free_symbols and (_sign(distance) < 0 or _sign(distance - length) > 0):
            raise WorkerError(Code.DOMAIN_ERROR, f"{name} is not on the beam (0 to {sp.sstr(length)} m from A)")
    resultant = sum((r for _, _, r in supports), sp.Integer(0)) - sum((w for _, _, w, _ in loads), sp.Integer(0))
    moment = sum((r * d for _, d, r in supports), sp.Integer(0)) - sum((w * d for _, d, w, _ in loads), sp.Integer(0))
    if len(unknowns) > 2:
        names = ", ".join(s.name for s in unknowns)
        raise Undetermined("underdetermined", f"a beam gives two equations (resolving and moments) but there are "
                           f"{len(unknowns)} unknowns ({names}): state more of them")  # fmt: skip
    solution: dict[sp.Symbol, sp.Expr] = {}
    if unknowns:
        found = sp.solve([resultant, moment], unknowns, dict=True)
        if not found:
            raise WorkerError(Code.DOMAIN_ERROR, "no values of the unknowns put the beam in equilibrium")
        if len(found) > 1 or any(s not in found[0] for s in unknowns):
            raise Undetermined("ambiguous", "the equilibrium equations do not fix the unknowns: state more of them")
        solution = found[0]
        if any(not v.is_real for v in solution.values()):
            raise WorkerError(Code.DOMAIN_ERROR, "no real values of the unknowns put the beam in equilibrium")
    resolved = {
        name: _tidy(as_expr(e).subs(solution))
        for name, e in [
            *((f"x_{n}", d) for n, d, *_ in [*loads, *supports]),
            *((f"R_{n}", r) for n, _, r in supports),
            *((f"W_{n}", w) for n, _, w, _ in loads),
            *((f"m_{n}", m) for n, _, _, m in loads if m is not None),
        ]
    }
    for name in [n for n, *_ in [*loads, *supports]]:
        x = resolved[f"x_{name}"]
        if _sign(x) < 0 or _sign(x - length) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{name} would have to be {sp.sstr(x)} m from A, off the beam")
    for name, _, _ in supports:
        if _sign(resolved[f"R_{name}"]) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"the reaction at {name} would be negative: the beam would tilt "
                              "and lose contact there")  # fmt: skip
    for name, *_ in loads:
        if _sign(resolved[f"W_{name}"]) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"the load {name} would have to act upwards")
    net_force, net_moment = _tidy(resultant.subs(solution)), _tidy(moment.subs(solution))
    value: Value = {
        "task": "moments",
        "conventions": "distances from end A; loads act downwards and support reactions upwards; moments about A, "
                       "anticlockwise positive",
        "units": {"distance": "m", "force": "N", "moment": "N m", "mass": "kg"},
        "length": _out(length),
        "loads": {name: {"distance": exact(resolved[f"x_{name}"]), "force": exact(resolved[f"W_{name}"]),
                         **({"mass": exact(resolved[f"m_{name}"])} if mass is not None else {})}
                  for name, _, _, mass in loads},
        "supports": {name: {"distance": exact(resolved[f"x_{name}"]), "reaction": exact(resolved[f"R_{name}"])}
                     for name, _, _ in supports},
        "solved": sorted(s.name for s in unknowns),
        "resultant_force": exact(net_force),
        "moment_about_A": exact(net_moment),
        "equilibrium": _sign(net_force) == 0 and _sign(net_moment) == 0,
    }  # fmt: skip
    if p._g is not None:
        value["g"] = exact(p._g)
    p.line("resolving vertically: sum of reactions - sum of loads", net_force, "N")
    p.line("moments about A: sum of R d - sum of W x", net_moment, "N m")
    for symbol in unknowns:
        p.line(symbol.name, resolved[symbol.name], "m" if symbol.name.startswith("x_") else
               ("kg" if symbol.name.startswith("m_") else "N"))  # fmt: skip
    return value


# --- friction on an inclined plane ------------------------------------------------------------------------------


def _incline(p: Problem) -> Value:
    m = p.need("mass", "mass")
    sin, cos, degrees = p.angle(0, 89)
    push = p.get("applied_force", "force", default=0)  # parallel to the slope unless applied_angle is given
    push_sin, push_cos, _ = p.angle(
        -89, 89, key="applied_angle", tan_key="applied_tan_angle", sin_key=None, default_zero=True
    )
    assert push is not None
    g = p.g
    normal = m * g * cos - push * push_sin
    if _sign(normal) < 0:
        raise WorkerError(Code.DOMAIN_ERROR, "the applied force lifts the block off the slope")
    tendency = push * push_cos - m * g * sin  # resultant without friction, up the slope positive
    mu = p.get("mu", "ratio", sign="nonnegative")
    value: Value = {
        "task": "incline_friction",
        "conventions": "along the slope, up the slope positive; the block starts at rest; friction F <= mu R",
        "units": {"force": "N", "acceleration": "m/s^2", "angle": "degrees"},
        "g": exact(g),
        "angle": _out(degrees),
        "normal_reaction": _out(normal),
        "weight_component_down_slope": _out(m * g * sin),
    }
    p.line("R = m g cos a - P sin b", _tidy(normal), "N")
    p.line("resultant up the slope without friction", _tidy(tendency), "N")
    if mu is None:
        if not p.has("acceleration"):
            raise Undetermined("underdetermined", "give the coefficient of friction mu, or the acceleration and "
                               "direction of motion to find mu")  # fmt: skip
        a = p.need("acceleration", "acceleration", sign="nonnegative")
        if "direction" not in p.args:
            raise WorkerError(Code.DOMAIN_ERROR, "an acceleration needs its direction: up_slope or down_slope")
        moving = 1 if p.args["direction"] == "up_slope" else -1
        friction = moving * tendency - m * a  # friction opposes the motion
        if _sign(normal) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "with no normal reaction there is no friction to find mu from")
        mu = _tidy(friction / normal)
        if _sign(mu) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "that acceleration needs negative friction: the block could not "
                              "move like that")  # fmt: skip
        value.update({"mu": exact(mu), "friction": _out(friction), "friction_direction":
                      "down_slope" if moving > 0 else "up_slope", "limiting_friction": _out(friction),
                      "slides": _sign(a) > 0, "limiting": True, "direction": p.args["direction"],
                      "acceleration": _out(a)})  # fmt: skip
        p.line("mu = F / R", mu)
        return value
    limit = mu * normal
    excess = sp.Abs(tendency) - limit
    toward = _sign(tendency)
    value.update({"mu": _out(mu), "limiting_friction": _out(limit)})
    p.line("limiting friction mu R", _tidy(limit), "N")
    if _sign(excess) <= 0:
        value.update(
            {
                "slides": False,
                "limiting": _sign(excess) == 0 and toward != 0,
                "direction": "none",
                "acceleration": exact(sp.Integer(0)),
                "friction": _out(sp.Abs(tendency)),
                "friction_direction": "none" if toward == 0 else ("down_slope" if toward > 0 else "up_slope"),
            }
        )
        return value  # fmt: skip
    a = excess / m
    value.update({"slides": True, "limiting": True, "direction": "up_slope" if toward > 0 else "down_slope",
                  "acceleration": _out(a), "friction": _out(limit),
                  "friction_direction": "down_slope" if toward > 0 else "up_slope"})  # fmt: skip
    p.line("a = (|resultant| - mu R) / m", _tidy(a), "m/s^2")
    return value


# --- connected particles ----------------------------------------------------------------------------------------


def _connected(p: Problem) -> Value:
    arrangement = p.args["arrangement"]
    m_a, m_b = p.need("mass_a", "mass"), p.need("mass_b", "mass")
    if arrangement == "hanging":
        if any(k in p.args for k in ("mu", "angle", "tan_angle", "sin_angle", "surface")):
            raise WorkerError(Code.DOMAIN_ERROR, "two hanging particles touch no surface: no mu, angle or surface")
        sin, cos, degrees = sp.Integer(1), sp.Integer(0), sp.Integer(90)  # A hangs straight down too
    elif arrangement == "table":
        if any(k in p.args for k in ("angle", "tan_angle", "sin_angle")):
            raise WorkerError(Code.DOMAIN_ERROR, "a table is horizontal: use arrangement incline for a slope")
        sin, cos, degrees = sp.Integer(0), sp.Integer(1), sp.Integer(0)
    else:
        sin, cos, degrees = p.angle(0, 89)
    mu = p.get("mu", "ratio", sign="nonnegative")
    if mu is None:
        if p.args.get("surface") == "rough":
            raise Undetermined("underdetermined", "the surface is rough: give the coefficient of friction mu")
        mu = sp.Integer(0)
    g = p.g
    normal = m_a * g * cos
    limit = mu * normal
    tendency = m_b * g - m_a * g * sin  # positive: B descends and A moves up the slope (or along the table)
    excess = sp.Abs(tendency) - limit
    toward = _sign(tendency)
    if _sign(excess) <= 0:
        a, moving, friction = sp.Integer(0), "at_rest", sp.Abs(tendency)
    else:
        a, moving, friction = excess / (m_a + m_b), ("b_descends" if toward > 0 else "a_descends"), limit
    signed = a if moving != "a_descends" else -a  # B's acceleration downwards
    tension = m_b * (g - signed)
    value: Value = {
        "task": "connected_particles",
        "arrangement": arrangement,
        "conventions": "light inextensible string over a smooth pulley; released from rest; A is on the surface "
        "(or on the left when both hang) and B hangs",
        "units": {"force": "N", "acceleration": "m/s^2", "speed": "m/s", "distance": "m", "time": "s"},
        "g": exact(g),
        "acceleration": _out(a),
        "moving": moving,
        "tension": _out(tension),
        "force_on_pulley": _out(tension * sp.sqrt(2 + 2 * sin)),
    }
    if arrangement != "hanging":
        value.update({"angle": _out(degrees), "normal_reaction": _out(normal), "friction": _out(friction),
                      "limiting_friction": _out(limit), "mu": _out(mu)})  # fmt: skip
    p.line("(m_A + m_B) a = |m_B g - m_A g sin a| - mu R", _tidy(a), "m/s^2")
    p.line("T = m_B (g - a_B)", _tidy(tension), "N")
    if "time" in p.args:
        t = p.need("time", "time", sign="nonnegative")
        value["after_time"] = {"time": _out(t), "speed": _out(a * t), "distance": _out(a * t**2 / 2)}
    return value


# --- work, energy and power -------------------------------------------------------------------------------------


def _work_energy(p: Problem) -> Value:
    find = p.args["find"]
    m = p.need("mass", "mass")
    u = p.need("initial_speed", "speed", sign="nonnegative")
    v = None if find == "final_speed" else p.need("final_speed", "speed", sign="nonnegative")
    h = p.get("height_gain", "length", default=0)
    d = p.get("distance", "length", sign="positive")
    force = None if find == "driving_force" else p.get("driving_force", "force", default=0, sign="nonnegative")
    resistance = None if find == "resistance" else p.get("resistance", "force", default=0, sign="nonnegative")
    _, cos, _ = p.angle(0, 90, key="force_angle", tan_key="force_tan_angle", sin_key=None, default_zero=True)
    assert h is not None
    if d is None and (find != "final_speed" or _sign(force) != 0 or _sign(resistance) != 0):
        raise Undetermined("underdetermined", "work done by a force needs the distance moved (distance)")
    gpe = m * p.g * h if h != 0 else sp.Integer(0)
    ke_start = m * u**2 / 2
    work_force = force * d * cos if force is not None and d is not None else sp.Integer(0)
    work_resistance = resistance * d if resistance is not None and d is not None else sp.Integer(0)
    if v is None:  # find the final speed
        ke_end = ke_start + work_force - work_resistance - gpe
        if _sign(ke_end) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the energy runs out first: the body stops before it gets there")
        v = sp.sqrt(2 * ke_end / m)
    ke_end = m * v**2 / 2
    change = ke_end - ke_start
    if find == "driving_force":
        assert d is not None and resistance is not None
        if _sign(cos) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a force at right angles to the motion does no work")
        work_resistance = resistance * d
        work_force = change + gpe + work_resistance
        force = work_force / (d * cos)
        if _sign(force) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the driving force would have to act backwards")
    elif find == "resistance":
        assert d is not None and force is not None
        work_force = force * d * cos
        work_resistance = work_force - change - gpe
        resistance = work_resistance / d
        if _sign(resistance) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the resistance would have to help the motion: the energies do not "
                              "balance")  # fmt: skip
    value: Value = {
        "task": "work_energy_power",
        "part": "work_energy",
        "conventions": "work done by the driving force - work done against resistance = gain in KE + gain in GPE; "
        "height gain upwards positive",
        "units": {"energy": "J", "force": "N", "speed": "m/s"},
        "ke_initial": _out(ke_start),
        "ke_final": _out(ke_end),
        "ke_change": _out(change),
        "gpe_change": _out(gpe),
        "work_by_driving_force": _out(work_force),
        "work_against_resistance": _out(work_resistance),
        find: _out({"final_speed": v, "driving_force": force, "resistance": resistance}[find]),
    }
    if p._g is not None:
        value["g"] = exact(p._g)
    p.line("gain in KE = m (v^2 - u^2)/2", _tidy(change), "J")
    p.line("gain in GPE = m g h", _tidy(gpe), "J")
    p.line(find, value[find]["text"], {"final_speed": "m/s"}.get(find, "N"))
    return value


def _power(p: Problem) -> Value:
    power = p.get("power", "power", sign="nonnegative")
    speed = p.get("speed", "speed", sign="nonnegative")
    force = p.get("driving_force", "force", sign="nonnegative")
    m = p.get("mass", "mass", sign="positive")
    resistance = p.get("resistance", "force", default=0, sign="nonnegative")
    assert resistance is not None
    sin, _, degrees = p.angle(-89, 89, default_zero=True)

    def slope() -> sp.Expr:
        """The weight component down the slope, m g sin a (zero on the level)."""
        if _sign(sin) == 0:
            return sp.Integer(0)
        if m is None:
            raise Undetermined("underdetermined", "on a slope the mass is needed for the weight component")
        return m * p.g * sin

    maximum = False
    if power is not None and speed is not None and force is not None:
        if not _equal(power, force * speed):
            raise WorkerError(Code.DOMAIN_ERROR, "power, driving force and speed do not satisfy P = F v")
    elif power is not None and speed is not None:
        if _sign(speed) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "at zero speed P = F v gives no driving force")
        force = power / speed
    elif power is not None and force is not None:
        if _sign(force) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "at zero driving force P = F v gives no speed")
        speed = power / force
    elif force is not None and speed is not None:
        power = force * speed
    elif power is not None and (m is not None or "resistance" in p.args):
        force, maximum = resistance + slope(), True  # at maximum speed the acceleration is zero
        if _sign(force) <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, "nothing opposes the motion, so there is no maximum speed")
        speed = power / force
    else:
        raise Undetermined("underdetermined", "give two of power, driving force and speed (or the power with the "
                           "resistance for the maximum speed)")  # fmt: skip
    assert power is not None and speed is not None and force is not None
    value: Value = {
        "task": "work_energy_power",
        "part": "power",
        "conventions": "P = F v; along the motion positive; a slope angle above the horizontal is uphill",
        "units": {"power": "W", "force": "N", "speed": "m/s", "acceleration": "m/s^2"},
        "power": _out(power),
        "driving_force": _out(force),
        "speed": _out(speed),
        "at_maximum_speed": maximum,
    }
    p.line("P = F v", _tidy(power), "W")
    if m is not None:
        a = (force - resistance - slope()) / m
        value["acceleration"] = _out(a)
        p.line("a = (F - R - m g sin a) / m", _tidy(a), "m/s^2")
    if p._g is not None:
        value["g"], value["slope_angle"] = exact(p._g), _out(degrees)
    return value


def _efficiency(p: Problem) -> Value:
    given: dict[str, sp.Expr] = {}
    dims: set[str] = set()
    for key in ("input", "useful_output"):
        if p.has(key):
            given[key], dim = _quantity(p.args[key], key, "energy_or_power")
            if _sign(given[key]) < 0:
                raise WorkerError(Code.DOMAIN_ERROR, f"{key} cannot be negative")
            if dim:
                dims.add(dim)
    if len(dims) > 1:
        raise WorkerError(Code.DOMAIN_ERROR, "input and useful_output must both be energies or both powers")
    ratio = p.get("efficiency", "ratio", sign="nonnegative")
    if ratio is not None and _sign(ratio - 1) > 0:
        raise WorkerError(Code.DOMAIN_ERROR, "efficiency is a fraction from 0 to 1 (an efficiency over 1 is "
                          "impossible)")  # fmt: skip
    source, useful = given.get("input"), given.get("useful_output")
    if source is not None and useful is not None:
        if _sign(source) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "no input, so no efficiency")
        found = useful / source
        if ratio is not None and not _equal(found, ratio):
            raise WorkerError(Code.DOMAIN_ERROR, "input, useful output and efficiency do not agree")
        ratio = found
        if _sign(ratio - 1) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the useful output cannot exceed the input")
    elif source is not None and ratio is not None:
        useful = source * ratio
    elif useful is not None and ratio is not None:
        if _sign(ratio) == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a zero efficiency gives no useful output")
        source = useful / ratio
    else:
        raise Undetermined("underdetermined", "give two of input, useful_output and efficiency")
    assert source is not None and useful is not None and ratio is not None
    p.line("efficiency = useful output / input", _tidy(ratio))
    return {"task": "work_energy_power", "part": "efficiency", "units": "as given (J for energy, W for power)",
            "input": _out(source), "useful_output": _out(useful), "wasted": _out(source - useful),
            "efficiency": _out(ratio), "percent": _out(ratio * 100)}  # fmt: skip


# --- momentum and impulse ---------------------------------------------------------------------------------------


def _collision(p: Problem) -> Value:
    m_a, m_b = p.need("mass_a", "mass"), p.need("mass_b", "mass")
    u_a, u_b = p.need("velocity_a", "speed", sign="any"), p.need("velocity_b", "speed", sign="any")
    if _sign(u_a - u_b) <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "A and B do not approach each other, so they do not collide (A is "
                          "behind B, positive from A towards B)")  # fmt: skip
    e = p.get("restitution", "ratio", sign="nonnegative")
    v_a, v_b = p.get("final_velocity_a", "speed"), p.get("final_velocity_b", "speed")
    momentum = m_a * u_a + m_b * u_b
    if e is not None:
        if _sign(e - 1) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the coefficient of restitution is at most 1")
        found_a = (momentum - m_b * e * (u_a - u_b)) / (m_a + m_b)
        found_b = (momentum + m_a * e * (u_a - u_b)) / (m_a + m_b)
        if (v_a is not None and not _equal(v_a, found_a)) or (v_b is not None and not _equal(v_b, found_b)):
            raise WorkerError(Code.DOMAIN_ERROR, "the final velocities given disagree with the coefficient of "
                              "restitution and conservation of momentum")  # fmt: skip
        v_a, v_b = found_a, found_b
    else:
        if v_a is None and v_b is None:
            raise Undetermined("underdetermined", "give the coefficient of restitution e or a velocity after impact")
        if v_b is None:
            assert v_a is not None
            v_b = (momentum - m_a * v_a) / m_b
        elif v_a is None:
            v_a = (momentum - m_b * v_b) / m_a
        elif not _equal(m_a * v_a + m_b * v_b, momentum):
            raise WorkerError(Code.DOMAIN_ERROR, "momentum is not conserved by those velocities")
        e = (v_b - v_a) / (u_a - u_b)
        if _sign(e) < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "A would pass through B: the velocities after impact are impossible")
        if _sign(e - 1) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the kinetic energy would increase (e > 1): impossible")
    ke_before = (m_a * u_a**2 + m_b * u_b**2) / 2
    ke_after = (m_a * v_a**2 + m_b * v_b**2) / 2

    def heading(v: sp.Expr) -> str:
        return {1: "positive", 0: "at_rest", -1: "negative"}[_sign(v)]

    value: Value = {
        "task": "momentum_impulse",
        "part": "collision",
        "conventions": "direct impact along a line; positive from A towards B; Newton's law of restitution "
        "v_B - v_A = e (u_A - u_B)",
        "units": {"velocity": "m/s", "momentum": "N s", "energy": "J"},
        "final_velocity_a": _out(v_a),
        "final_velocity_b": _out(v_b),
        "direction_a": heading(v_a),
        "direction_b": heading(v_b),
        "restitution": _out(e),
        "momentum": _out(momentum),
        "impulse_on_b": _out(m_b * (v_b - u_b)),
        "impulse_on_a": _out(m_a * (v_a - u_a)),
        "ke_before": _out(ke_before),
        "ke_after": _out(ke_after),
        "ke_loss": _out(ke_before - ke_after),
    }
    p.line("m_A u_A + m_B u_B = m_A v_A + m_B v_B", _tidy(momentum), "N s")
    p.line("v_B - v_A = e (u_A - u_B)", sp.Tuple(_tidy(v_a), _tidy(v_b)), "m/s")
    p.line("loss of kinetic energy", _tidy(ke_before - ke_after), "J")
    return value


def _impulse(p: Problem) -> Value:
    m = p.need("mass", "mass")
    u = p.need("initial_velocity", "speed", sign="any")
    v = p.get("final_velocity", "speed")
    impulse = p.get("impulse", "impulse")
    force, time = p.get("force", "force"), p.get("time", "time", sign="positive")
    candidates = []
    if v is not None:
        candidates.append(m * (v - u))
    if impulse is not None:
        candidates.append(impulse)
    if force is not None and time is not None:
        candidates.append(force * time)
    if not candidates:
        raise Undetermined("underdetermined", "give the final velocity, the impulse, or a force and its time")
    if any(not _equal(c, candidates[0]) for c in candidates[1:]):
        raise WorkerError(Code.DOMAIN_ERROR, "the impulse, force and time and change of momentum given disagree")
    impulse = candidates[0]
    v = u + impulse / m
    value: Value = {
        "task": "momentum_impulse",
        "part": "impulse",
        "conventions": "impulse = F t = m v - m u; velocities signed, positive in the direction of the initial "
        "velocity as given",
        "units": {"impulse": "N s", "velocity": "m/s", "force": "N", "energy": "J"},
        "impulse": _out(impulse),
        "initial_velocity": _out(u),
        "final_velocity": _out(v),
        "ke_change": _out(m * (v**2 - u**2) / 2),
    }
    if time is not None:
        value["average_force"] = _out(impulse / time)
    p.line("I = m (v - u)", _tidy(impulse), "N s")
    return value


# --- circular motion --------------------------------------------------------------------------------------------


def _rate(p: Problem) -> sp.Expr | None:
    """Angular speed from at most one of angular_speed, period and frequency."""
    given = [k for k in ("angular_speed", "period", "frequency") if p.has(k)]
    if len(given) > 1:
        raise WorkerError(Code.DOMAIN_ERROR, f"give one of angular_speed, period and frequency, not {given}")
    if not given:
        return None
    key = given[0]
    value = p.need(key, {"angular_speed": "angular_speed", "period": "time", "frequency": "frequency"}[key])
    return value if key == "angular_speed" else (2 * sp.pi / value if key == "period" else 2 * sp.pi * value)


def _circular(p: Problem) -> Value:
    omega = _rate(p)
    if p.args.get("part", "horizontal") == "conical_pendulum":
        return _conical(p, omega)
    r, v = p.get("radius", "length", sign="positive"), p.get("speed", "speed", sign="positive")
    if r is not None and v is not None and omega is not None:
        if not _equal(v, omega * r):
            raise WorkerError(Code.DOMAIN_ERROR, "speed, radius and angular speed do not satisfy v = r omega")
    elif r is not None and v is not None:
        omega = v / r
    elif r is not None and omega is not None:
        v = omega * r
    elif v is not None and omega is not None:
        r = v / omega
    else:
        raise Undetermined("underdetermined", "give two of radius, speed and the rate (angular speed, period or "
                           "frequency)")  # fmt: skip
    assert r is not None and v is not None and omega is not None
    m = p.get("mass", "mass", sign="positive")
    value: Value = {
        "task": "circular_motion",
        "part": "horizontal",
        "conventions": "uniform circular motion; acceleration and resultant force towards the centre",
        "units": {"length": "m", "speed": "m/s", "angular_speed": "rad/s", "period": "s", "frequency": "Hz",
                  "acceleration": "m/s^2", "force": "N"},
        "radius": _out(r),
        "speed": _out(v),
        "angular_speed": _out(omega),
        "period": _out(2 * sp.pi / omega),
        "frequency": _out(omega / (2 * sp.pi)),
        "acceleration": _out(v**2 / r),
    }  # fmt: skip
    p.line("v = r omega", _tidy(v), "m/s")
    p.line("a = v^2 / r = r omega^2", _tidy(v**2 / r), "m/s^2")
    if m is not None:
        value["force"] = _out(m * v**2 / r)
        p.line("F = m v^2 / r", _tidy(m * v**2 / r), "N")
    return value


def _conical(p: Problem, omega: sp.Expr | None) -> Value:
    length = p.need("length", "length")
    g = p.g
    if any(k in p.args for k in ("angle", "tan_angle", "sin_angle")):
        sin, cos, degrees = p.angle(0, 90)
    elif p.has("radius"):
        r = p.need("radius", "length")
        if _sign(r - length) >= 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the radius of the circle must be less than the string's length")
        sin = r / length
        cos = sp.sqrt(1 - sin**2)
        degrees = sp.asin(sin) * 180 / sp.pi
    elif omega is not None:
        cos = g / (length * omega**2)
        if _sign(cos - 1) >= 0:
            raise WorkerError(Code.DOMAIN_ERROR, "too slow: below omega^2 = g / l the string hangs vertically")
        sin = sp.sqrt(1 - cos**2)
        degrees = sp.acos(cos) * 180 / sp.pi
    else:
        raise Undetermined("underdetermined", "give the angle of the string, the radius or the angular speed")
    if _sign(sin) == 0 or _sign(cos) == 0:
        raise WorkerError(Code.DOMAIN_ERROR, "a conical pendulum's string is strictly between vertical and horizontal")
    found = sp.sqrt(g / (length * cos))
    if omega is not None and not _equal(found, omega):
        raise WorkerError(Code.DOMAIN_ERROR, "the angular speed given disagrees with the angle of the string")
    omega = found
    r = length * sin
    m = p.get("mass", "mass", sign="positive")
    value: Value = {
        "task": "circular_motion",
        "part": "conical_pendulum",
        "conventions": "angle of the string to the vertical; T cos a = m g vertically, T sin a = m r omega^2 "
                       "towards the centre",
        "units": {"length": "m", "speed": "m/s", "angular_speed": "rad/s", "period": "s", "angle": "degrees",
                  "force": "N", "acceleration": "m/s^2"},
        "g": exact(g),
        "angle": _out(degrees),
        "radius": _out(r),
        "depth": _out(length * cos),
        "angular_speed": _out(omega),
        "speed": _out(omega * r),
        "period": _out(2 * sp.pi / omega),
        "acceleration": _out(omega**2 * r),
    }  # fmt: skip
    p.line("omega^2 = g / (l cos a)", _tidy(omega**2), "rad^2/s^2")
    if m is not None:
        value["tension"] = _out(m * g / cos)
        p.line("T = m g / cos a", _tidy(m * g / cos), "N")
    return value


# --- simple harmonic motion -------------------------------------------------------------------------------------


def _shm(p: Problem) -> Value:
    rates = [k for k in ("angular_frequency", "period", "frequency") if p.has(k)]
    if len(rates) > 1:
        raise WorkerError(Code.DOMAIN_ERROR, f"give one of angular_frequency, period and frequency, not {rates}")
    omega = None
    if rates:
        key = rates[0]
        given = p.need(key, {"angular_frequency": "angular_speed", "period": "time", "frequency": "frequency"}[key])
        omega = given if key == "angular_frequency" else (2 * sp.pi / given if key == "period" else 2 * sp.pi * given)
    amplitude = p.get("amplitude", "length", sign="positive")
    v_max = p.get("max_speed", "speed", sign="positive")
    a_max = p.get("max_acceleration", "acceleration", sign="positive")
    if omega is None:
        if amplitude is not None and v_max is not None:
            omega = v_max / amplitude
        elif amplitude is not None and a_max is not None:
            omega = sp.sqrt(a_max / amplitude)
        elif v_max is not None and a_max is not None:
            omega = a_max / v_max
    if amplitude is None and omega is not None:
        amplitude = v_max / omega if v_max is not None else (a_max / omega**2 if a_max is not None else None)
    if omega is None or amplitude is None:
        raise Undetermined("underdetermined", "give the amplitude and one of angular_frequency, period, frequency, "
                           "max_speed or max_acceleration")  # fmt: skip
    if (v_max is not None and not _equal(v_max, amplitude * omega)) or (
        a_max is not None and not _equal(a_max, amplitude * omega**2)
    ):
        raise WorkerError(Code.DOMAIN_ERROR, "the maximum speed or acceleration disagrees with v_max = A omega and "
                          "a_max = A omega^2")  # fmt: skip
    value: Value = {
        "task": "shm",
        "conventions": "x measured from the centre of oscillation; a = -omega^2 x; from the centre x = A sin(omega "
                       "t), from the positive extreme x = A cos(omega t)",
        "units": {"displacement": "m", "speed": "m/s", "acceleration": "m/s^2", "angular_frequency": "rad/s",
                  "period": "s", "frequency": "Hz", "force": "N", "energy": "J"},
        "amplitude": _out(amplitude),
        "angular_frequency": _out(omega),
        "period": _out(2 * sp.pi / omega),
        "frequency": _out(omega / (2 * sp.pi)),
        "max_speed": _out(amplitude * omega),
        "max_acceleration": _out(amplitude * omega**2),
    }  # fmt: skip
    p.line("v_max = A omega", _tidy(amplitude * omega), "m/s")
    p.line("a_max = A omega^2", _tidy(amplitude * omega**2), "m/s^2")
    m = p.get("mass", "mass", sign="positive")
    if m is not None:
        value["max_force"] = _out(m * amplitude * omega**2)
        value["energy"] = _out(m * omega**2 * amplitude**2 / 2)
    if p.has("displacement"):
        x = p.need("displacement", "length", sign="any")
        if _sign(sp.Abs(x) - amplitude) > 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the displacement cannot exceed the amplitude")
        speed = omega * sp.sqrt(amplitude**2 - x**2)
        value["at_displacement"] = {
            "displacement": _out(x),
            "speed": _out(speed),
            "acceleration": _out(-(omega**2) * x),
        }
        p.line("v^2 = omega^2 (A^2 - x^2)", _tidy(speed), "m/s")
    if p.has("time"):
        t = p.need("time", "time", sign="nonnegative")
        if "start" not in p.args:
            raise Undetermined("ambiguous", "state where the motion starts (start: centre or extreme) to find the "
                               "position at a time")  # fmt: skip
        phase = omega * t
        if p.args["start"] == "centre":
            x, velocity = amplitude * sp.sin(phase), amplitude * omega * sp.cos(phase)
        else:
            x, velocity = amplitude * sp.cos(phase), -amplitude * omega * sp.sin(phase)
        value["at_time"] = {"time": _out(t), "start": p.args["start"], "displacement": _out(x),
                            "velocity": _out(velocity), "acceleration": _out(-(omega**2) * x)}  # fmt: skip
        p.line("x(t)", _tidy(x), "m")
    return value


TASKS: dict[str, Callable[[Problem], Value]] = {
    "projectile_angle": _projectile,
    "moments": _moments,
    "incline_friction": _incline,
    "connected_particles": _connected,
    "momentum_impulse": lambda p: _collision(p) if p.args.get("part", "collision") == "collision" else _impulse(p),
    "circular_motion": _circular,
    "shm": _shm,
    "work_energy_power": lambda p: {"work_energy": _work_energy, "power": _power, "efficiency": _efficiency}[
        p.args.get("part", "work_energy")
    ](p),
}


def phys_mechanics(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """A level mechanics by task (VER-31); see the module docstring for the conventions."""
    problem = Problem(args, context)
    try:
        value = TASKS[args["task"]](problem)
    except Undetermined as undetermined:
        return _unknown(args["task"], undetermined.reason, undetermined.needs)
    return problem.done(value)
