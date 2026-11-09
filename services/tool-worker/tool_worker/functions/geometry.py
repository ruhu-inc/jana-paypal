"""Geometry calculator (VER-15): `geom_figure(template, givens)` returns derived lengths, angles and areas, and the
coordinates of the figure for the vetted renderer.

Templates: triangles (Pythagoras and the trigonometric ratios as the right-angled case of the sine and cosine rules,
including the ambiguous case, which returns every triangle that fits), circles (arc, sector, chord, segment), circle
theorems, bearings, coordinate geometry (gradient, midpoint, distance, line equations), 2D vectors, transformations
(translation, reflection, rotation, enlargement), mensuration (areas and volumes) and similar shapes. Angles are in
degrees. Values are exact where SymPy keeps them short, with a decimal alongside; inconsistent givens (an angle of
180° or more, angles summing past 180°, sides that cannot close) are a DOMAIN_ERROR."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sympy as sp

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions.calculus import number
from tool_worker.functions.common import parse_arg
from tool_worker.functions.maths import exact, significant, slice_seconds
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
EXACT_TEXT_LIMIT = 160  # longer exact forms are given as decimals only
DEG = sp.pi / 180


def _fail(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _tidy(value: sp.Expr) -> sp.Expr:
    """A cheap canonical form: trig of sums expanded (sin(150° - B) with sin B known becomes a surd), then expanded
    and with surds out of denominators. Full simplification is too slow for the worker's budget."""
    value = sp.Integer(0) + value
    if value.is_Rational:
        return value
    return sp.radsimp(sp.expand(sp.expand_trig(value)))


def _value(value: sp.Expr) -> dict[str, Any]:
    """An exact value with its decimal, or the decimal alone when the exact form is long."""
    out = exact(value)
    if len(out["text"]) > EXACT_TEXT_LIMIT:
        return {"decimal": significant(value, 6), "text": significant(value, 15), "approximate": True}
    return out


def _point(x: sp.Expr, y: sp.Expr) -> list[float]:
    """A renderer coordinate: a float to 10 significant figures (deterministic)."""
    return [float(f"{float(sp.N(v, 25)):.10g}") for v in (x, y)]


def _positive(givens: dict[str, Any], name: str) -> sp.Rational:
    value = number(givens[name], name)
    if value <= 0:
        raise _fail(f"{name} must be positive")
    return value


def _angle(givens: dict[str, Any], name: str) -> sp.Rational:
    value = number(givens[name], name)
    if not 0 < value < 180:
        raise _fail(f"angle {name} = {value}° is not between 0° and 180°")
    return value


def sin_d(degrees: sp.Expr) -> sp.Expr:
    return sp.sin(degrees * DEG)


def cos_d(degrees: sp.Expr) -> sp.Expr:
    return sp.cos(degrees * DEG)


def asin_d(value: sp.Expr) -> sp.Expr:
    return _tidy(sp.asin(value) / DEG)


def acos_d(value: sp.Expr) -> sp.Expr:
    return _tidy(sp.acos(value) / DEG)


def _vector(givens: dict[str, Any], name: str) -> tuple[sp.Rational, sp.Rational]:
    pair = givens[name]
    if len(pair) != 2:
        raise _fail(f"{name} needs two components")
    return number(pair[0], f"{name}[0]"), number(pair[1], f"{name}[1]")


# --- triangles ----------------------------------------------------------------------------------------------------

SIDES = ("a", "b", "c")
ANGLES = ("A", "B", "C")


def _triangle_out(sides: dict[str, sp.Expr], angles: dict[str, sp.Expr], methods: list[str]) -> dict[str, Any]:
    sides = {k: _tidy(v) for k, v in sides.items()}
    angles = {k: _tidy(v) for k, v in angles.items()}
    area = _tidy(sides["a"] * sides["b"] * sin_d(angles["C"]) / 2)
    a_x, a_y = sp.Integer(0), sp.Integer(0)
    c_x, c_y = sides["b"] * cos_d(angles["A"]), sides["b"] * sin_d(angles["A"])
    return {
        "sides": {k: _value(sides[k]) for k in SIDES},
        "angles": {k: _value(angles[k]) for k in ANGLES},
        "area": _value(area),
        "perimeter": _value(_tidy(sum(sides.values()))),
        "methods": methods,
        "figure": {"A": _point(a_x, a_y), "B": _point(sides["c"], 0), "C": _point(c_x, c_y)},
    }


def _three_sides(s: dict[str, sp.Expr], known: dict[str, sp.Expr]) -> dict[str, sp.Expr]:
    """Every angle not already known, each by the cosine rule (so each is in its own canonical form)."""
    if not all(s[x] + s[y] > s[z] for x, y, z in (("a", "b", "c"), ("a", "c", "b"), ("b", "c", "a"))):
        raise _fail("the three sides cannot form a triangle (each pair must be longer than the third)")
    angles = dict(known)
    for corner in ANGLES:
        if corner not in angles:
            x, y = (k for k in SIDES if k != corner.lower())
            angles[corner] = acos_d((s[x] ** 2 + s[y] ** 2 - s[corner.lower()] ** 2) / (2 * s[x] * s[y]))
    return angles


def triangle(givens: dict[str, Any]) -> dict[str, Any]:
    """Solve a triangle from three givens (at least one a side), naming each side after its opposite angle."""
    sides = {k: _positive(givens, k) for k in SIDES if k in givens}
    angles: dict[str, sp.Expr] = {k: _angle(givens, k) for k in ANGLES if k in givens}
    if givens.get("right_angle"):
        corner = givens["right_angle"]
        if corner in angles and angles[corner] != 90:
            raise _fail(f"angle {corner} cannot be both {angles[corner]}° and a right angle")
        angles[corner] = sp.Integer(90)
    if sum(angles.values()) >= 180 and len(angles) >= 2:
        if not (len(angles) == 3 and sum(angles.values()) == 180):
            raise _fail("the angles given add up to 180° or more")
    if not sides:
        raise _fail("give at least one side")
    if len(sides) + len(angles) < 3:
        raise _fail("give three facts about the triangle, at least one of them a side")
    right = any(v == 90 for v in angles.values())
    if len(angles) >= 2:
        if len(angles) == 2:
            missing = next(k for k in ANGLES if k not in angles)
            angles[missing] = 180 - sum(angles.values())
        known = next(k for k in SIDES if k in sides)
        ratio = sides[known] / sin_d(angles[known.upper()])
        found = {k: sides.get(k, ratio * sin_d(angles[k.upper()])) for k in SIDES}
        method = "trigonometric_ratios" if right else "sine_rule"
        return {"case": "two_angles", "ambiguous": False, "solutions": [_triangle_out(found, angles, [method])]}
    if len(sides) == 3:
        derived = _three_sides(sides, {})
        return {"case": "three_sides", "ambiguous": False,
                "solutions": [_triangle_out(sides, derived, ["cosine_rule"])]}  # fmt: skip
    ((corner, angle),) = angles.items()
    opposite = corner.lower()
    if opposite not in sides:  # two sides and the angle between them
        x, y = (k for k in SIDES if k != opposite)
        third = sp.sqrt(sides[x] ** 2 + sides[y] ** 2 - 2 * sides[x] * sides[y] * cos_d(angle))
        method = "pythagoras" if angle == 90 else "cosine_rule"
        full = {**sides, opposite: _tidy(third)}
        derived = _three_sides(full, angles)
        return {"case": "two_sides_included_angle", "ambiguous": False,
                "solutions": [_triangle_out(full, derived, [method])]}  # fmt: skip
    return _ambiguous(sides, corner, angle)


def _ambiguous(sides: dict[str, sp.Expr], corner: str, angle: sp.Expr) -> dict[str, Any]:
    """Two sides and an angle opposite one of them: the sine rule gives the angle opposite the other known side,
    which may be acute or obtuse, so none, one or two triangles fit."""
    opposite = corner.lower()
    other = next(k for k in SIDES if k != opposite and k in sides)
    third = next(k for k in SIDES if k not in (opposite, other))
    sine = _tidy(sides[other] * sin_d(angle) / sides[opposite])
    if sp.N(sine - 1, 30) > 0:
        raise _fail(f"no triangle fits: sin {other.upper()} would be {sp.sstr(sine)}, more than 1")
    first = asin_d(sine)
    candidates = [first] if sine == 1 else [first, _tidy(180 - first)]
    solutions = []
    for candidate in candidates:
        last = _tidy(180 - angle - candidate)
        if sp.N(last, 30) <= 0:
            continue
        found_angles = {corner: angle, other.upper(): candidate, third.upper(): last}
        length = _tidy(sides[opposite] * sin_d(last) / sin_d(angle))
        solutions.append(_triangle_out({**sides, third: length}, found_angles, ["sine_rule"]))
    if not solutions:
        raise _fail("no triangle fits these givens")
    return {"case": "two_sides_non_included_angle", "ambiguous": len(solutions) == 2, "solutions": solutions}


# --- circles ------------------------------------------------------------------------------------------------------


def circle(givens: dict[str, Any]) -> dict[str, Any]:
    if "r" in givens:
        r = _positive(givens, "r")
    elif "d" in givens:
        r = _positive(givens, "d") / 2
    else:
        raise _fail("give the radius r or the diameter d")
    out: dict[str, Any] = {"radius": _value(r), "circumference": _value(2 * sp.pi * r), "area": _value(sp.pi * r**2)}
    theta: sp.Expr | None = None
    if "angle" in givens:
        theta = number(givens["angle"], "angle")
    elif "chord" in givens:
        chord = _positive(givens, "chord")
        if chord > 2 * r:
            raise _fail("a chord cannot be longer than the diameter")
        theta = 2 * asin_d(chord / (2 * r))
    elif "arc" in givens:
        theta = _tidy(_positive(givens, "arc") * 180 / (sp.pi * r))
    if theta is not None:
        if not 0 < sp.N(theta, 30) <= 360:
            raise _fail("the angle at the centre must be more than 0° and at most 360°")
        sector = _tidy(theta / 360 * sp.pi * r**2)
        triangle_area = _tidy(r**2 * sin_d(theta) / 2)
        out.update(
            angle=_value(theta),
            arc=_value(_tidy(theta / 360 * 2 * sp.pi * r)),
            sector=_value(sector),
            chord=_value(_tidy(2 * r * sin_d(theta / 2))),
            segment=_value(_tidy(sector - triangle_area)),
            figure={"O": [0.0, 0.0], "P": _point(r, 0), "Q": _point(r * cos_d(theta), r * sin_d(theta))},
        )
    return out


THEOREMS: dict[str, tuple[str, Callable[[sp.Expr], sp.Expr], float]] = {
    # theorem: (what the derived angle is, rule, largest valid given angle)
    "angle_at_centre": ("angle at the centre, twice the angle at the circumference", lambda x: 2 * x, 180),
    "angle_at_circumference": ("angle at the circumference, half the angle at the centre", lambda x: x / 2, 360),
    "same_segment": ("angle in the same segment, equal", lambda x: x, 180),
    "semicircle": ("angle in a semicircle", lambda x: sp.Integer(90), 180),
    "cyclic_quadrilateral": ("opposite angle of a cyclic quadrilateral", lambda x: 180 - x, 180),
    "tangent_radius": ("angle between tangent and radius", lambda x: sp.Integer(90), 180),
    "alternate_segment": ("angle in the alternate segment, equal to the tangent-chord angle", lambda x: x, 180),
    "tangents_from_point": ("angle between the radii to the two tangent points", lambda x: 180 - x, 180),
    "isosceles_radii": ("each base angle of the triangle of two radii", lambda x: (180 - x) / 2, 180),
}


def circle_theorem(givens: dict[str, Any]) -> dict[str, Any]:
    theorem = givens.get("theorem")
    if theorem not in THEOREMS:
        raise _fail(f"theorem must be one of {', '.join(sorted(THEOREMS))}")
    meaning, rule, largest = THEOREMS[theorem]
    given = number(givens.get("angle", 90), "angle")
    if not 0 < given < largest:
        raise _fail(f"the given angle must be between 0° and {largest}°")
    return {"theorem": theorem, "given": _value(given), "derived": _value(rule(given)), "meaning": meaning}


# --- bearings, coordinates and vectors ------------------------------------------------------------------------------


def _three_figures(bearing: sp.Expr) -> str:
    text = significant(bearing, 15) if not bearing.is_Integer else str(int(bearing))
    if bearing.is_Integer:
        return text.zfill(3)
    value = sp.N(bearing, 30)
    whole, _, rest = format(round(float(value), 1), ".1f").partition(".")
    return f"{whole.zfill(3)}.{rest}"


def _bearing_of(dx: sp.Expr, dy: sp.Expr) -> sp.Expr:
    """The bearing (clockwise from north) of the displacement (east dx, north dy)."""
    if dx == 0 and dy == 0:
        raise _fail("the two points are the same")
    angle = _tidy(sp.atan2(dx, dy) / DEG)
    return _tidy(angle + 360) if sp.N(angle) < 0 else angle


def bearing(givens: dict[str, Any]) -> dict[str, Any]:
    if "from" in givens and "to" in givens:
        (x0, y0), (x1, y1) = _vector(givens, "from"), _vector(givens, "to")
        theta = _bearing_of(x1 - x0, y1 - y0)
        distance = _tidy(sp.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2))
        start = (x0, y0)
    else:
        theta = number(givens["bearing"], "bearing")
        if not 0 <= theta < 360:
            raise _fail("a bearing is at least 0° and less than 360°")
        distance = _positive(givens, "distance")
        start = (sp.Integer(0), sp.Integer(0))
    east, north = _tidy(distance * sin_d(theta)), _tidy(distance * cos_d(theta))
    back = _tidy(theta + 180 if sp.N(theta) < 180 else theta - 180)
    return {
        "bearing": {**_value(theta), "three_figure": _three_figures(theta)},
        "back_bearing": {**_value(back), "three_figure": _three_figures(back)},
        "distance": _value(distance),
        "east": _value(east),
        "north": _value(north),
        "figure": {"P": _point(*start), "Q": _point(start[0] + east, start[1] + north)},
    }


def _line_text(gradient: sp.Expr | None, through: tuple[sp.Expr, sp.Expr]) -> dict[str, Any]:
    x, _y = sp.symbols("x y", real=True)
    if gradient is None:
        return {"text": f"x = {sp.sstr(through[0])}", "latex": f"x = {sp.latex(through[0])}", "vertical": True}
    intercept = _tidy(through[1] - gradient * through[0])
    rhs = gradient * x + intercept
    return {"text": f"y = {sp.sstr(rhs, order='lex')}", "latex": f"y = {sp.latex(rhs, order='lex')}",
            "gradient": _value(gradient), "intercept": _value(intercept)}  # fmt: skip


def coordinates(givens: dict[str, Any]) -> dict[str, Any]:
    if "p1" in givens and "p2" in givens:
        (x1, y1), (x2, y2) = _vector(givens, "p1"), _vector(givens, "p2")
        if (x1, y1) == (x2, y2):
            raise _fail("the two points are the same")
        gradient = None if x1 == x2 else (y2 - y1) / (x2 - x1)
        mid = ((x1 + x2) / 2, (y1 + y2) / 2)
        normal = sp.Integer(0) if gradient is None else (None if gradient == 0 else -1 / gradient)
        return {
            "gradient": _value(gradient) if gradient is not None else {"vertical": True},
            "midpoint": [_value(mid[0]), _value(mid[1])],
            "distance": _value(_tidy(sp.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2))),
            "line": _line_text(gradient, (x1, y1)),
            "perpendicular_bisector": _line_text(normal, mid),
            "figure": {"P1": _point(x1, y1), "P2": _point(x2, y2), "M": _point(*mid)},
        }
    point = _vector(givens, "point")
    gradient = number(givens["gradient"], "gradient")
    return {"line": _line_text(gradient, point), "figure": {"P": _point(*point)}}


def vector(givens: dict[str, Any]) -> dict[str, Any]:
    u = _vector(givens, "u")
    out: dict[str, Any] = {"u": [_value(c) for c in u], "magnitude_u": _value(_tidy(sp.sqrt(u[0] ** 2 + u[1] ** 2)))}
    if u != (0, 0):
        direction = _tidy(sp.atan2(u[1], u[0]) / DEG)
        out["direction_u"] = _value(direction)
    if "k" in givens:
        k = number(givens["k"], "k")
        out["k_u"] = [_value(k * c) for c in u]
    if "v" in givens:
        v = _vector(givens, "v")
        out.update(
            v=[_value(c) for c in v],
            magnitude_v=_value(_tidy(sp.sqrt(v[0] ** 2 + v[1] ** 2))),
            u_plus_v=[_value(a + b) for a, b in zip(u, v, strict=True)],
            u_minus_v=[_value(a - b) for a, b in zip(u, v, strict=True)],
            parallel=u[0] * v[1] - u[1] * v[0] == 0,
        )
    return out


# --- transformations ----------------------------------------------------------------------------------------------


def _reflect_line(text: str) -> tuple[sp.Expr, sp.Expr, sp.Expr]:
    """a, b, c of the mirror line ax + by + c = 0, from x = k, y = k, y = mx + k or y = x forms (our grammar)."""
    tree = parse_arg(text, {}, "line").tree
    if not isinstance(tree, sp.Eq):
        raise _fail("line: give the mirror line as an equation, for example y = x or x = 2")
    symbols = {s.name: s for s in tree.free_symbols}
    if set(symbols) - {"x", "y"}:
        raise _fail("line: use only x and y")
    x, y = symbols.get("x", sp.Symbol("x", real=True)), symbols.get("y", sp.Symbol("y", real=True))
    expression = sp.expand(tree.lhs - tree.rhs)
    poly = sp.Poly(expression, x, y)
    if poly.total_degree() != 1:
        raise _fail("line: the mirror line must be straight")
    return poly.coeff_monomial(x), poly.coeff_monomial(y), poly.coeff_monomial(1)


def transformation(givens: dict[str, Any]) -> dict[str, Any]:
    points = [(number(p[0], "points"), number(p[1], "points")) for p in givens["points"]]
    spec = givens["transformation"]
    kind = spec.get("kind")
    image: list[tuple[sp.Expr, sp.Expr]]
    if kind == "translation":
        dx, dy = _vector(spec, "vector")
        image = [(x + dx, y + dy) for x, y in points]
    elif kind == "reflection":
        a, b, c = _reflect_line(spec["line"])
        image = []
        for x, y in points:
            t = 2 * (a * x + b * y + c) / (a**2 + b**2)
            image.append((x - t * a, y - t * b))
    elif kind == "rotation":
        cx, cy = _vector(spec, "centre") if "centre" in spec else (sp.Integer(0), sp.Integer(0))
        angle = number(spec["angle"], "angle")
        if spec.get("direction", "anticlockwise") == "clockwise":
            angle = -angle
        c, s = cos_d(angle), sin_d(angle)
        image = [(_tidy(cx + (x - cx) * c - (y - cy) * s), _tidy(cy + (x - cx) * s + (y - cy) * c)) for x, y in points]
    elif kind == "enlargement":
        cx, cy = _vector(spec, "centre") if "centre" in spec else (sp.Integer(0), sp.Integer(0))
        k = number(spec["scale"], "scale")
        if k == 0:
            raise _fail("an enlargement's scale factor cannot be 0")
        image = [(cx + k * (x - cx), cy + k * (y - cy)) for x, y in points]
    else:
        raise _fail("transformation kind must be translation, reflection, rotation or enlargement")
    return {
        "kind": kind,
        "object": [[_value(x), _value(y)] for x, y in points],
        "image": [[_value(x), _value(y)] for x, y in image],
        "figure": {"object": [_point(x, y) for x, y in points], "image": [_point(x, y) for x, y in image]},
    }


# --- mensuration and similar shapes ----------------------------------------------------------------------------------

SHAPES: dict[str, tuple[tuple[str, ...], Callable[..., dict[str, sp.Expr]]]] = {
    "rectangle": (("length", "width"), lambda a, b: {"area": a * b, "perimeter": 2 * (a + b)}),
    "square": (("side",), lambda s: {"area": s**2, "perimeter": 4 * s}),
    "triangle": (("base", "height"), lambda b, h: {"area": b * h / 2}),
    "parallelogram": (("base", "height"), lambda b, h: {"area": b * h}),
    "trapezium": (("a", "b", "height"), lambda a, b, h: {"area": (a + b) * h / 2}),
    "circle": (("radius",), lambda r: {"area": sp.pi * r**2, "circumference": 2 * sp.pi * r}),
    "cuboid": (("length", "width", "height"),
               lambda a, b, h: {"volume": a * b * h, "surface_area": 2 * (a * b + a * h + b * h)}),
    "cube": (("side",), lambda s: {"volume": s**3, "surface_area": 6 * s**2}),
    "prism": (("cross_section_area", "length"), lambda a, length: {"volume": a * length}),
    "cylinder": (("radius", "height"), lambda r, h: {"volume": sp.pi * r**2 * h, "curved_surface": 2 * sp.pi * r * h,
                                                     "surface_area": 2 * sp.pi * r * (r + h)}),
    "cone": (("radius", "height"), lambda r, h: {
        "volume": sp.pi * r**2 * h / 3, "slant_height": sp.sqrt(r**2 + h**2),
        "curved_surface": sp.pi * r * sp.sqrt(r**2 + h**2), "surface_area": sp.pi * r * (r + sp.sqrt(r**2 + h**2))}),
    "sphere": (("radius",), lambda r: {"volume": 4 * sp.pi * r**3 / 3, "surface_area": 4 * sp.pi * r**2}),
    "hemisphere": (("radius",), lambda r: {"volume": 2 * sp.pi * r**3 / 3, "curved_surface": 2 * sp.pi * r**2,
                                           "surface_area": 3 * sp.pi * r**2}),
    "pyramid": (("base_area", "height"), lambda a, h: {"volume": a * h / 3}),
}  # fmt: skip


def mensuration(givens: dict[str, Any]) -> dict[str, Any]:
    shape = givens.get("shape")
    if shape not in SHAPES:
        raise _fail(f"shape must be one of {', '.join(sorted(SHAPES))}")
    names, formula = SHAPES[shape]
    dims = givens.get("dims", {})
    missing = [n for n in names if n not in dims]
    if missing:
        raise _fail(f"{shape} needs {', '.join(missing)}")
    values = [_positive(dims, n) for n in names]
    return {"shape": shape, **{k: _value(_tidy(v)) for k, v in formula(*values).items()}}


POWERS = {"length": 1, "area": 2, "volume": 3}


def similar(givens: dict[str, Any]) -> dict[str, Any]:
    scale = givens["scale_from"]
    kind = scale.get("kind")
    if kind not in POWERS:
        raise _fail("scale_from kind must be length, area or volume")
    ratio = _positive(scale, "b") / _positive(scale, "a")
    k = _tidy(ratio ** sp.Rational(1, POWERS[kind]))
    out: dict[str, Any] = {"length_factor": _value(k), "area_factor": _value(_tidy(k**2)),
                           "volume_factor": _value(_tidy(k**3))}  # fmt: skip
    if "convert" in givens:
        convert = givens["convert"]
        target = convert.get("kind")
        if target not in POWERS:
            raise _fail("convert kind must be length, area or volume")
        out["converted"] = {"kind": target, "b": _value(_tidy(_positive(convert, "a") * k ** POWERS[target]))}
    return out


# --- geom_figure ---------------------------------------------------------------------------------------------------

TEMPLATES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "triangle": triangle,
    "circle": circle,
    "circle_theorem": circle_theorem,
    "bearing": bearing,
    "coordinates": coordinates,
    "vector": vector,
    "transformation": transformation,
    "mensuration": mensuration,
    "similar": similar,
}


def geom_figure(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Derived measures and renderer coordinates for one figure template (VER-15)."""
    template = args["template"]
    try:
        with time_slice(slice_seconds(context) * 2):
            value = TEMPLATES[template](dict(args.get("givens", {})))
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    except KeyError as missing:
        raise _fail(f"{template}: missing given {missing}") from None
    return {"status": "ok", "template": template, **value}, {"method": "symbolic", "confidence": "proved"}
