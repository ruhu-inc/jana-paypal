"""Calculus for O level and additional mathematics (VER-08): derivatives of polynomials, powers with rational
exponents, sums and constant multiples; gradients, tangents and normals; stationary points with their nature;
indefinite and definite integrals; and areas under curves with the sign handled by splitting at the roots.

A student's indefinite integral is checked up to an additive constant: the value is right when its derivative is the
integrand, and an answer with no arbitrary constant is reported with form `missing_constant`. Angles inside calculus
are radians (VER-05). Symbolic work runs inside the time slice and answers `unknown` when it runs out (VER-27)."""

from __future__ import annotations

import itertools
import re
from fractions import Fraction
from typing import Any

import sympy as sp
from sympy.calculus.util import continuous_domain
from sympy.core.relational import Relational

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions import alevel_calculus
from tool_worker.functions.common import parse_arg, substituted
from tool_worker.functions.maths import _expressions as expression_equivalence
from tool_worker.functions.maths import _sorted_values, exact, slice_seconds
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
UNKNOWN: Extras = {"method": "heuristic", "confidence": "undecided"}
PROVED: Extras = {"method": "symbolic", "confidence": "proved"}
FINITE = (sp.FiniteSet, sp.sets.sets.EmptySet)  # a solution set listed in full (possibly empty)


# --- shared ------------------------------------------------------------------------------------------------------


def function_of(args: dict[str, Any], field: str = "expression") -> tuple[sp.Expr, sp.Symbol]:
    """The expression to work on and its variable. `y = f(x)` is read as f(x); the variable is `variable` (default
    x), or the only free symbol when x does not occur."""
    tree = parse_arg(args[field], args, field).tree
    if isinstance(tree, sp.Eq) and isinstance(tree.lhs, sp.Symbol) and tree.lhs not in tree.rhs.free_symbols:
        tree = tree.rhs
    if isinstance(tree, (Relational, sp.And)):
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: give an expression or y = f(x), not an equation")
    return tree, variable_of(tree, args)


def variable_of(tree: sp.Basic, args: dict[str, Any]) -> sp.Symbol:
    name = args.get("variable")
    symbols = {s.name: s for s in tree.free_symbols}
    if name is None:
        name = "x" if "x" in symbols or len(symbols) != 1 else next(iter(symbols))
    return symbols.get(name) or sp.Symbol(name, real=True)


_NUMBER_ARG = re.compile(r"[+-]?(?:\d{1,30}(?:\.\d{0,30})?|\.\d{1,30})(?:[eE][+-]?\d{1,3})?(?:/\d{1,30})?")


def number(raw: Any, field: str) -> sp.Rational:
    """A number argument (integer, decimal or fraction, as JSON number or string), kept exact."""
    text = repr(raw) if isinstance(raw, float) else str(raw).strip()
    if isinstance(raw, bool) or not _NUMBER_ARG.fullmatch(text):
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: not a number")
    try:
        value = Fraction(text)
    except ZeroDivisionError:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: division by zero") from None
    return sp.Rational(value.numerator, value.denominator)


def point(raw: Any, field: str, args: dict[str, Any]) -> sp.Expr:
    """A number kept exact, or at A level an exact constant such as pi/4 or ln(2) (VER-30)."""
    try:
        return number(raw, field)
    except WorkerError:
        return alevel_calculus.constant(raw, field, args)


def value_at(tree: sp.Expr, var: sp.Symbol, at: sp.Expr, field: str = "at") -> sp.Expr | None:
    """f(at) when defined and real, else None; LIMIT_EXCEEDED when f(at) is a power too large to compute (SEC-08)."""
    value = sp.simplify(substituted(tree, {var: at}, field))
    if value.has(sp.zoo, sp.nan, sp.oo, -sp.oo) or value.is_real is False:
        return None
    return value


def line(gradient: sp.Expr, x0: sp.Expr, y0: sp.Expr, var: sp.Symbol) -> dict[str, Any]:
    """The line through (x0, y0) with the gradient, as y = mx + c."""
    intercept = sp.simplify(y0 - gradient * x0)
    rhs = sp.expand(gradient * var + intercept)
    return {"text": f"y = {sp.sstr(rhs)}", "latex": f"y = {sp.latex(rhs)}", "gradient": exact(gradient),
            "intercept": exact(intercept)}  # fmt: skip


def nature(f: sp.Expr, var: sp.Symbol, at: sp.Expr, neighbours: list[sp.Expr]) -> tuple[str, sp.Expr]:
    """A stationary point's nature from the second derivative, or from the sign of the first derivative either side
    when the second derivative is zero there."""
    second = sp.simplify(substituted(sp.diff(f, var, 2), {var: at}, "stationary_points"))
    numeric = sp.N(second, 30)
    if numeric.is_real and numeric > 0:
        return "minimum", second
    if numeric.is_real and numeric < 0:
        return "maximum", second
    gaps = [abs(sp.N(n - at, 30)) for n in neighbours if n != at]
    h = sp.nsimplify(min([*gaps, sp.Integer(1)]) / 4, rational=True)
    first = sp.diff(f, var)
    left, right = (sp.N(substituted(first, {var: at + d}, "stationary_points"), 30) for d in (-h, h))
    if left < 0 < right:
        return "minimum", second
    if left > 0 > right:
        return "maximum", second
    return "inflection", second


def stationary_points(f: sp.Expr, var: sp.Symbol, domain: sp.Set) -> list[dict[str, Any]] | None:
    """Every stationary point of f in the domain with its nature, or None when the roots of f' have no closed form."""
    roots = sp.solveset(sp.diff(f, var), var, domain)
    if not isinstance(roots, FINITE):
        return None
    xs = _sorted_values([r for r in roots.args if r.is_real is not False])
    out = []
    for x0 in xs:
        y0 = value_at(f, var, x0, "stationary_points")
        if y0 is None:
            continue
        kind, second = nature(f, var, x0, xs)
        out.append({"x": exact(x0), "y": exact(y0), "nature": kind, "second_derivative": exact(second)})
    return out


def interval_of(args: dict[str, Any]) -> sp.Set:
    if "interval" not in args:
        return sp.S.Reals
    lo, hi = (number(v, "interval") for v in args["interval"])
    if not lo < hi:
        raise WorkerError(Code.DOMAIN_ERROR, "interval: the lower end must be below the upper end")
    return sp.Interval(lo, hi)


def _student_value(verdict: str) -> str:
    if verdict in ("equivalent", "equivalent_on_domain"):
        return "correct"
    return "incorrect" if verdict == "not_equivalent" else "unknown"


# --- math_differentiate -------------------------------------------------------------------------------------------


def math_differentiate(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """The derivative (orders 1 to 3), and on request the gradient, tangent and normal at a point, the stationary
    points with their nature, and a check of a student's derivative (VER-08); implicit and parametric
    differentiation at A level (VER-30)."""
    mode = args.get("mode", "explicit")
    if mode == "implicit":
        return alevel_calculus.implicit(args, context)
    if mode == "parametric":
        return alevel_calculus.parametric(args, context)
    if "expression" not in args:
        raise WorkerError(Code.DOMAIN_ERROR, "give expression")
    tree, var = function_of(args)
    order = int(args.get("order", 1))
    try:
        with time_slice(slice_seconds(context)):
            derivative = sp.diff(tree, var, order)
            derivative = sp.expand(derivative) if derivative.is_polynomial(var) else sp.simplify(derivative)
            value: dict[str, Any] = {"variable": var.name, "order": order, "derivative": exact(derivative)}
            if "at" in args:
                value["at"] = _at_point(tree, var, point(args["at"], "at", args))
            if args.get("stationary_points"):
                points = stationary_points(tree, var, interval_of(args))
                if points is None:
                    return {"status": "unknown", "reason": "no_closed_form", **value}, UNKNOWN
                value["stationary_points"] = points
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    extras: Extras = {"latex": sp.latex(derivative), **PROVED}
    if "student" in args:
        student = parse_arg(args["student"], args, "student", sampled=True).tree  # only sampled
        verdict, check = expression_equivalence(student, derivative, context)
        value["student"] = {"value": _student_value(verdict["verdict"])}
        if check["confidence"] != "proved":
            extras.update(method=check["method"], confidence=check["confidence"])
    return {"status": "ok", **value}, extras


def _at_point(tree: sp.Expr, var: sp.Symbol, x0: sp.Expr) -> dict[str, Any]:
    y0 = value_at(tree, var, x0)
    gradient = value_at(sp.diff(tree, var), var, x0)
    if y0 is None or gradient is None:
        raise WorkerError(Code.DOMAIN_ERROR, f"the curve or its gradient is undefined at {var.name} = {sp.sstr(x0)}")
    out: dict[str, Any] = {"x": exact(x0), "y": exact(y0), "gradient": exact(gradient),
                           "tangent": line(gradient, x0, y0, var)}  # fmt: skip
    if gradient == 0:
        out["normal"] = {"text": f"x = {sp.sstr(x0)}", "latex": f"x = {sp.latex(x0)}", "vertical": True}
    else:
        out["normal"] = line(-1 / gradient, x0, y0, var)
    return out


# --- math_integrate -----------------------------------------------------------------------------------------------


def math_integrate(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """The indefinite integral (plus an arbitrary constant), a definite integral, the area between the curve and the
    x-axis split at the roots, and a check of a student's answer up to an additive constant (VER-08); at A level
    (VER-30) a volume of revolution, the trapezium rule with its error, and a stated substitution or parts."""
    tree, var = function_of(args)
    if "volume" in args:
        return alevel_calculus.volume(args, tree, var, context)
    if args.get("method") == "trapezium":
        return alevel_calculus.trapezium(args, tree, var, context)
    if "substitution" in args:
        return alevel_calculus.substitution(args, tree, var, context)
    if "parts_u" in args:
        return alevel_calculus.parts(args, tree, var, context)
    constant = args.get("constant", "c")
    definite = "lower" in args or "upper" in args
    if definite and not ("lower" in args and "upper" in args):
        raise WorkerError(Code.DOMAIN_ERROR, "a definite integral needs both lower and upper")
    try:
        with time_slice(slice_seconds(context)):
            antiderivative = sp.integrate(tree, var)
            if antiderivative.has(sp.Integral):
                return {"status": "unknown", "reason": "no_closed_form"}, UNKNOWN
            general = f"{sp.sstr(antiderivative)} + {constant}"
            value: dict[str, Any] = {
                "variable": var.name,
                "antiderivative": exact(antiderivative),
                "general": {"text": general, "latex": f"{sp.latex(antiderivative)} + {constant}"},
            }
            definite_value = None
            if definite:
                lower, upper = point(args["lower"], "lower", args), point(args["upper"], "upper", args)
                found, definite_value = _definite(tree, antiderivative, var, lower, upper, bool(args.get("area")))
                value.update(found)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    extras: Extras = {"latex": sp.latex(antiderivative), **PROVED}
    if "student" in args:
        value["student"], check = _check_student(args, tree, var, definite_value, context)
        if check["confidence"] != "proved":
            extras.update(method=check["method"], confidence=check["confidence"])
    return {"status": "ok", **value}, extras


def _definite(tree: sp.Expr, antiderivative: sp.Expr, var: sp.Symbol, lower: sp.Expr, upper: sp.Expr,
              area: bool) -> tuple[dict[str, Any], sp.Expr]:  # fmt: skip
    low, high = (lower, upper) if lower <= upper else (upper, lower)
    if continuous_domain(tree, var, sp.Interval(low, high)) != sp.Interval(low, high):
        raise WorkerError(Code.DOMAIN_ERROR, "the function is undefined or discontinuous on part of the interval")

    def between(a: sp.Expr, b: sp.Expr) -> sp.Expr:
        return sp.simplify(
            substituted(antiderivative, {var: b}, "upper") - substituted(antiderivative, {var: a}, "lower")
        )

    whole = between(lower, upper)
    out: dict[str, Any] = {"lower": exact(lower), "upper": exact(upper), "definite": exact(whole)}
    if not area:
        return out, whole
    roots = sp.solveset(tree, var, sp.Interval.open(low, high))
    if not isinstance(roots, FINITE):
        raise WorkerError(Code.DOMAIN_ERROR, "the curve's roots in the interval have no closed form")
    cuts = [low, *_sorted_values(list(roots.args)), high]
    pieces, total = [], sp.Integer(0)
    for a, b in itertools.pairwise(cuts):
        signed = between(a, b)
        total += abs(signed)
        side = "above" if sp.N(signed) > 0 else "below" if sp.N(signed) < 0 else "on"
        pieces.append({"from": exact(a), "to": exact(b), "signed": exact(signed), "side": side})
    out["area"] = {"total": exact(sp.simplify(total)), "pieces": pieces}
    return out, whole


def _check_student(args: dict[str, Any], tree: sp.Expr, var: sp.Symbol, definite: sp.Expr | None,
                   context: CallContext) -> tuple[dict[str, Any], Extras]:  # fmt: skip
    """A definite answer is compared with the value; an indefinite one by its derivative, so any additive constant is
    accepted, and an answer with no arbitrary constant has form `missing_constant`."""
    student = parse_arg(args["student"], args, "student", sampled=True).tree  # differentiated, then only sampled
    if isinstance(student, (Relational, sp.And)):
        student = student.rhs if isinstance(student, sp.Eq) else student
    if definite is not None:
        verdict, check = expression_equivalence(student, definite, context)
        return {"value": _student_value(verdict["verdict"]), "form": "ok"}, check
    verdict, check = expression_equivalence(sp.diff(student, var), tree, context)
    arbitrary = sorted(s.name for s in student.free_symbols - tree.free_symbols - {var})
    form = "ok" if arbitrary else "missing_constant"
    out: dict[str, Any] = {"value": _student_value(verdict["verdict"]), "form": form}
    if arbitrary:
        out["constant"] = arbitrary[0] if len(arbitrary) == 1 else arbitrary
    return out, check
