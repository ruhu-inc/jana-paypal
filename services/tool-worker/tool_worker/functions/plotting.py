"""Graph sampling for plots (VER-14): adaptive samples of each curve over its domain, split at discontinuities and
vertical asymptotes, with the roots, turning points (with nature), intercepts and pairwise intersections, each
citing the call's check id. Plot renderers draw only these points (VSL-12).

Where each curve is continuous comes from SymPy's continuous domain, so a sign change across an asymptote is never
taken for a root. Samples are computed with mpmath at fixed precision from a tree walk of the parsed expression (no
code is generated or evaluated), rounded to 10 significant figures, so they are identical on every machine. Features
are exact where SymPy finds them inside the time slice; otherwise roots are found numerically inside each segment
and the check says `numeric_sampling`."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import mpmath
import sympy as sp
from sympy.calculus.util import continuous_domain

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions.calculus import FINITE, function_of, nature, number, value_at
from tool_worker.functions.maths import INVERSE_TRIG, TRIG, _in_degrees, _sorted_values, exact, slice_seconds
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
Evaluate = Callable[[Any], Any]
DIGITS = 20  # working precision of the samples (decimal digits)
NOISE = 1e-15  # samples smaller than this are rounding noise of the working precision
FUNCTIONS: dict[Any, str] = {
    sp.sin: "sin", sp.cos: "cos", sp.tan: "tan", sp.sec: "sec", sp.csc: "csc", sp.cot: "cot", sp.asin: "asin",
    sp.acos: "acos", sp.atan: "atan", sp.sinh: "sinh", sp.cosh: "cosh", sp.tanh: "tanh", sp.exp: "exp",
    sp.log: "log",
}  # fmt: skip


class Unsupported(Exception):
    """The expression has a node the sampler does not evaluate."""


# --- evaluation --------------------------------------------------------------------------------------------------


def context_for_call() -> Any:
    """A private mpmath context per call, so concurrent calls never share a working precision."""
    mp = mpmath.MPContext()
    mp.dps = DIGITS
    return mp


def compile_tree(expr: sp.Basic, var: sp.Symbol, mp: Any) -> Evaluate:
    """A function of one mpf built by walking the tree (no code is generated or evaluated)."""
    if expr == var:
        return lambda t: t
    if expr.is_Rational:
        p, q = int(expr.p), int(expr.q)
        return lambda t: mp.mpf(p) / q
    if expr.is_number:
        value = mp.mpf(str(sp.N(expr, DIGITS + 5)))
        return lambda t: value
    parts = [compile_tree(a, var, mp) for a in expr.args]
    if isinstance(expr, sp.Add):
        return lambda t: mp.fsum([f(t) for f in parts])
    if isinstance(expr, sp.Mul):
        return lambda t: mp.fprod([f(t) for f in parts])
    if isinstance(expr, sp.Pow):
        base, power = parts
        if expr.exp.is_Integer:
            n = int(expr.exp)
            return lambda t: base(t) ** n
        return lambda t: mp.power(base(t), power(t))
    if isinstance(expr, sp.Abs):
        (inner,) = parts
        return lambda t: abs(inner(t))
    if expr.func in FUNCTIONS and len(parts) == 1:
        function, (inner,) = getattr(mp, FUNCTIONS[expr.func]), parts
        return lambda t: function(inner(t))
    raise Unsupported(type(expr).__name__)


def sampler(expr: sp.Expr, var: sp.Symbol, mp: Any) -> Callable[[Any], Any | None]:
    """f(x) as an mpf, or None where it is undefined or not real."""
    evaluate = compile_tree(expr, var, mp)

    def at(x: Any) -> Any | None:
        try:
            y = evaluate(mp.mpf(x))
        except (ZeroDivisionError, ValueError, OverflowError, TypeError):
            return None
        if not isinstance(y, mp.mpf) or not mp.isfinite(y):
            return None
        return y

    return at


def rounded(value: Any, mp: Any) -> float:
    """A sample as a float to 10 significant figures (deterministic across machines); magnitudes below the working
    precision's noise (tan 360° computed as 1e-20) are zero."""
    return float(mp.nstr(value, 10)) if abs(value) >= NOISE else 0.0


# --- segments and adaptive sampling --------------------------------------------------------------------------------


def components(domain: sp.Set) -> list[sp.Interval]:
    if isinstance(domain, sp.Interval):
        return [domain]
    if isinstance(domain, sp.Union):
        return [c for c in domain.args if isinstance(c, sp.Interval)]
    return []


def sample_segment(at: Callable[[Any], Any | None], lo: Any, hi: Any, budget: int, tolerance: float,
                   min_width: Any, mp: Any) -> list[list[list[float]]]:  # fmt: skip
    """Adaptive samples on [lo, hi]: start uniform, then bisect the intervals where the curve departs most from its
    chord, until the budget is spent or every chord is within tolerance. An undefined sample splits the segment."""
    start = max(2, min((budget + 1) // 2, 17))
    xs = [lo + (hi - lo) * mp.mpf(i) / (start - 1) for i in range(start)]
    points: dict[Any, Any] = {x: at(x) for x in xs}
    ys = [y for y in points.values() if y is not None]
    scale = max((max(ys) - min(ys)) if ys else mp.mpf(1), mp.mpf(1))
    heap: list[tuple[float, int, Any, Any]] = []
    order = 0

    def push(a: Any, b: Any) -> None:
        nonlocal order
        if b - a < min_width:
            return
        mid = (a + b) / 2
        ya, yb, ym = points[a], points[b], at(mid)
        points[mid] = ym
        error = 1.0 if ya is None or yb is None or ym is None else float(abs(ym - (ya + yb) / 2) / scale)
        order += 1
        heapq.heappush(heap, (-error, order, a, b))

    for a, b in itertools.pairwise(xs):
        push(a, b)
    while heap and len(points) + 2 <= budget:
        error, _, a, b = heapq.heappop(heap)
        if -error < tolerance:
            break
        mid = (a + b) / 2
        push(a, mid)
        push(mid, b)
    segments: list[list[list[float]]] = [[]]
    for x in sorted(points):
        y = points[x]
        if y is None:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append([rounded(x, mp), rounded(y, mp)])
    return [s for s in segments if s]


def _edge(point: sp.Basic, inward: int, open_end: bool, asymptote: bool, span: Any, mp: Any) -> Any:
    """Where sampling starts at an end of a segment: on a closed end, at it; beside a hole, very close; beside an
    asymptote, a small share of the range away."""
    x = mp.mpf(str(sp.N(point, DIGITS + 5)))
    if not open_end:
        return x
    return x + inward * span * (mp.mpf("1e-3") if asymptote else mp.mpf("1e-9"))


# --- features ----------------------------------------------------------------------------------------------------


def _feature(x: sp.Expr, y: sp.Expr, check_id: str, **more: Any) -> dict[str, Any]:
    return {"x": exact(x), "y": exact(y), "check_id": check_id, **more}


def _in(domain: sp.Set, x: sp.Expr) -> bool:
    return bool(domain.contains(x) == sp.true)


def _numeric_roots(at: Callable[[Any], Any | None], segments: list[list[list[float]]], mp: Any) -> list[Any]:
    """Sign changes between neighbouring samples of one segment (never across a split), refined by bisection."""
    roots = []
    for segment in segments:
        for (x0, y0), (x1, y1) in itertools.pairwise(segment):
            if y0 == 0:
                roots.append(mp.mpf(x0))
            elif y0 * y1 < 0:
                a, b = mp.mpf(x0), mp.mpf(x1)
                fa = at(a)
                for _ in range(60):
                    m = (a + b) / 2
                    fm = at(m)
                    if fm is None or fa is None:
                        break
                    if (fm < 0) == (fa < 0):
                        a, fa = m, fm
                    else:
                        b = m
                roots.append((a + b) / 2)
        if segment and segment[-1][1] == 0:
            roots.append(mp.mpf(segment[-1][0]))
    return sorted(set(roots))


def curve_features(curve: Curve, segments: list[list[list[float]]], marks: set[str], context: CallContext,
                   mp: Any) -> tuple[dict[str, Any], bool]:  # fmt: skip
    """Roots, turning points and the y-intercept; True in the second place when a numeric fallback was used."""
    expr, var, interval, domain, at = curve.expr, curve.var, curve.interval, curve.domain, curve.at
    out: dict[str, Any] = {}
    numeric = False
    check_id = context.check_id
    if "roots" in marks:
        try:
            with time_slice(slice_seconds(context)):
                found = sp.solveset(expr, var, interval)
                if not isinstance(found, FINITE):
                    raise SliceExpired
                xs = [r for r in _sorted_values(list(found.args)) if r.is_real is not False and _in(domain, r)]
                out["roots"] = [_feature(r, sp.Integer(0), check_id) for r in xs]
        except SliceExpired:
            numeric = True
            out["roots"] = [{"x": {"decimal": mp.nstr(r, 6)}, "y": {"text": "0"}, "check_id": check_id,
                             "numeric": True} for r in _numeric_roots(at, segments, mp)]  # fmt: skip
    if "turning_points" in marks:
        try:
            with time_slice(slice_seconds(context)):
                found = sp.solveset(sp.diff(expr, var), var, interval)
                if not isinstance(found, FINITE):
                    raise SliceExpired
                xs = [r for r in _sorted_values(list(found.args)) if r.is_real is not False and _in(domain, r)]
                points = []
                for r in xs:
                    y = value_at(expr, var, r)
                    if y is not None:
                        kind, _ = nature(expr, var, r, xs)
                        points.append(_feature(r, y, check_id, nature=kind))
                out["turning_points"] = points
        except SliceExpired:
            numeric = True
            out["turning_points"] = []
            out["turning_points_unknown"] = True
    if "y_intercept" in marks and _in(domain, sp.Integer(0)):
        y0 = value_at(expr, var, sp.Integer(0))
        if y0 is not None:
            out["y_intercept"] = _feature(sp.Integer(0), y0, check_id)
    return out, numeric


# --- math_plot_points --------------------------------------------------------------------------------------------

ALL_MARKS = ("roots", "turning_points", "y_intercept", "asymptotes", "intersections")


def math_plot_points(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Sampled points per curve, split into continuous segments, with the features to mark (VER-14)."""
    lo, hi = (number(v, "x_range") for v in args["x_range"])
    if not lo < hi:
        raise WorkerError(Code.DOMAIN_ERROR, "x_range: the lower end must be below the upper end")
    marks = set(args.get("marks", ALL_MARKS))
    budget = min(int(args.get("max_points", 500)), int(context.setting("plot_max_points", 500)))
    mp = context_for_call()
    warnings: list[dict[str, str]] = []
    curves, numeric = [], False
    parsed: list[tuple[str, sp.Expr, sp.Symbol, sp.Set]] = []
    for i, curve in enumerate(args["curves"]):
        expr, var = function_of({**args, "expression": curve["expr"], "variable": "x"}, "expression")
        if expr.has(*TRIG, *INVERSE_TRIG):
            unit = args.get("angle_unit") or ("degrees" if args.get("level", "O") == "O" else "radians")
            if "angle_unit" not in args and not warnings:
                warnings.append({"code": "assumption_added", "message": f"angles are taken in {unit}"})
            if unit == "degrees":
                expr = _in_degrees(expr)
        others = sorted(s.name for s in expr.free_symbols - {var})
        if others:
            raise WorkerError(Code.DOMAIN_ERROR, f"curves[{i}]: give values for {', '.join(others)}")
        c_lo, c_hi = (number(v, "domain") for v in curve["domain"]) if "domain" in curve else (lo, hi)
        interval = sp.Interval(max(lo, c_lo), min(hi, c_hi))
        try:
            with time_slice(slice_seconds(context)):
                domain = continuous_domain(expr, var, interval)
        except SliceExpired:
            domain, numeric = interval, True
        parsed.append((curve["id"], expr, var, domain))
        try:
            at = sampler(expr, var, mp)
        except Unsupported as error:
            raise WorkerError(Code.DOMAIN_ERROR, f"curves[{i}]: cannot sample {error}") from None
        found, used_numeric = _curve(Curve(curve["id"], expr, var, interval, domain, at), budget, marks, context, mp)
        numeric = numeric or used_numeric
        curves.append(found)
    value: dict[str, Any] = {"x_range": [exact(lo)["text"], exact(hi)["text"]], "curves": curves}
    if "intersections" in marks and len(parsed) > 1:
        value["intersections"] = _intersections(parsed, sp.Interval(lo, hi), context)
    extras: Extras = {"method": "numeric_sampling" if numeric else "symbolic",
                      "confidence": "sampled" if numeric else "proved"}  # fmt: skip
    if warnings:
        extras["warnings"] = warnings
    return value, extras


@dataclass(frozen=True)
class Curve:
    id: str
    expr: sp.Expr
    var: sp.Symbol
    interval: sp.Interval
    domain: sp.Set
    at: Callable[[Any], Any | None]


def _breaks(curve: Curve, pieces: list[sp.Interval], context: CallContext) -> tuple[list[sp.Basic], list[sp.Basic]]:
    """The points inside the range where the curve is not continuous: vertical asymptotes (an infinite one-sided
    limit) and holes (finite limits)."""
    asymptotes, holes = [], []
    ends = {p for piece in pieces for p in (piece.inf, piece.sup)}
    for point in sorted(ends, key=lambda p: float(sp.N(p))):
        if point in (curve.interval.inf, curve.interval.sup) or _in(curve.domain, point):
            continue
        try:
            with time_slice(slice_seconds(context)):
                limits = [sp.limit(curve.expr, curve.var, point, side) for side in ("+", "-")]
        except SliceExpired:
            limits = [sp.oo]
        if any(limit.is_infinite or limit in (sp.zoo, sp.nan) for limit in limits):
            asymptotes.append(point)
        else:
            holes.append(point)
    return asymptotes, holes


def _shares(lengths: list[float], budget: int) -> list[int]:
    """The point budget split across segments by length, at least five each, never more than the budget."""
    minimum = 5
    if minimum * len(lengths) > budget:
        raise WorkerError(Code.LIMIT_EXCEEDED, "the curve has too many separate segments in this range",
                          limit="data_points")  # fmt: skip
    total = sum(lengths) or 1.0
    shares = [max(minimum, int(budget * length / total)) for length in lengths]
    while sum(shares) > budget:
        largest = shares.index(max(shares))
        shares[largest] -= 1
    return shares


def _curve(curve: Curve, budget: int, marks: set[str], context: CallContext, mp: Any) -> tuple[dict[str, Any], bool]:
    pieces = [p for p in components(curve.domain) if p.sup != p.inf]
    span = mp.mpf(str(sp.N(curve.interval.sup - curve.interval.inf, DIGITS + 5)))
    asymptotes, holes = _breaks(curve, pieces, context)
    tolerance = float(context.setting("plot_chord_tolerance", 0.001))
    segments: list[list[list[float]]] = []
    shares = _shares([float(sp.N(p.sup - p.inf)) for p in pieces], budget)
    for piece, share in zip(pieces, shares, strict=True):
        a = _edge(piece.inf, 1, bool(piece.left_open), piece.inf in asymptotes, span, mp)
        b = _edge(piece.sup, -1, bool(piece.right_open), piece.sup in asymptotes, span, mp)
        segments += sample_segment(curve.at, a, b, share, tolerance, span * mp.mpf("1e-7"), mp)
    out: dict[str, Any] = {"id": curve.id, "segments": segments, "points": sum(len(s) for s in segments)}
    if "asymptotes" in marks:
        out["asymptotes"] = [{"x": exact(p), "check_id": context.check_id} for p in asymptotes]
        out["holes"] = [{"x": exact(p), "check_id": context.check_id} for p in holes]
    features, numeric = curve_features(curve, segments, marks, context, mp)
    return {**out, **features}, numeric


def _intersections(parsed: list[tuple[str, sp.Expr, sp.Symbol, sp.Set]], interval: sp.Interval,
                   context: CallContext) -> list[dict[str, Any]]:  # fmt: skip
    out = []
    for i, (id_a, f, var, dom_a) in enumerate(parsed):
        for id_b, g, var_b, dom_b in parsed[i + 1 :]:
            g = g.subs(var_b, var)
            try:
                with time_slice(slice_seconds(context)):
                    found = sp.solveset(f - g, var, interval)
            except SliceExpired:
                out.append({"curves": [id_a, id_b], "unknown": True})
                continue
            if not isinstance(found, FINITE):
                out.append({"curves": [id_a, id_b], "unknown": True})
                continue
            for x in _sorted_values(list(found.args)):
                if x.is_real is False or not (_in(dom_a, x) and _in(dom_b, x)):
                    continue
                y = value_at(f, var, x)
                if y is not None:
                    out.append({"curves": [id_a, id_b], **_feature(x, y, context.check_id)})
    return out
