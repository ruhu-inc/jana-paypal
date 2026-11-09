"""A level calculus (VER-30): implicit and parametric differentiation, volumes of revolution, the trapezium rule with
its error, integration by a stated substitution or by parts, and first-order separable differential equations
(`math_solve_ode`).

- Implicit: for F(x, y) = G(x, y), dy/dx = -(∂H/∂x) / (∂H/∂y) with H = F - G; at a point on the curve, the gradient,
  tangent and normal. A point off the curve is a domain error.
- Parametric: dy/dx = (dy/dt) / (dx/dt), and d²y/dx² = d/dt(dy/dx) / (dx/dt); at a parameter value, the point,
  gradient, tangent and normal. Where dx/dt = 0 the tangent is vertical and stated so.
- Volume of revolution: π∫ y² dx about the x-axis (or π∫ x² dy about the y-axis, the expression then giving x in
  terms of y), exactly.
- Trapezium rule: h/2 [y₀ + 2(y₁ + … + yₙ₋₁) + yₙ] from the ordinates, with the exact integral when it has a closed
  form, the error and the percentage error, and whether the rule over- or under-estimates (the sign of f'' on the
  interval; unknown when it changes sign).
- Substitution: the integral rewritten in u with dx = (dx/du) du and the limits changed; by parts: uv - ∫v du'.
- Separable ODEs: dy/dx = f(x) g(y) separated as ∫ 1/g(y) dy = ∫ f(x) dx + c, solved for y when one branch fits,
  the constant fixed by an initial condition; every solution is checked by substitution into the equation. A right
  side that does not separate is `unknown` (`not_separable`), never solved by another method silently.

Points and limits may be exact constants such as pi/4 or ln(2). Symbolic work runs inside the time slice."""

from __future__ import annotations

import itertools
import re
from typing import Any

import sympy as sp
from sympy.calculus.util import continuous_domain
from sympy.core.relational import Relational

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions.common import parse_arg, substituted
from tool_worker.functions.maths import _expressions as expression_equivalence
from tool_worker.functions.maths import exact, significant, slice_seconds
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
UNKNOWN: Extras = {"method": "heuristic", "confidence": "undecided"}
PROVED: Extras = {"method": "symbolic", "confidence": "proved"}
_NUMBER = re.compile(r"[+-]?(?:\d{1,30}(?:\.\d{0,30})?|\.\d{1,30})(?:/\d{1,30})?")


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def constant(raw: Any, field: str, args: dict[str, Any]) -> sp.Expr:
    """An exact real constant: a number (kept exact) or an expression with no variables, such as pi/4 or ln(2)."""
    text = repr(raw) if isinstance(raw, float) else str(raw).strip()
    if isinstance(raw, bool):
        raise _domain(f"{field}: not a number")
    if _NUMBER.fullmatch(text):
        return sp.nsimplify(text, rational=True)
    tree = parse_arg(text, args, field).tree
    if isinstance(tree, (Relational, sp.And)) or tree.free_symbols or tree.is_real is not True:
        raise _domain(f"{field}: give a real constant such as 2, 1/3, pi/4 or ln(2)")
    return tree


def _symbol(tree: sp.Basic, name: str) -> sp.Symbol:
    return next((s for s in tree.free_symbols if s.name == name), sp.Symbol(name, real=True))


def _expression(args: dict[str, Any], field: str) -> sp.Expr:
    tree = parse_arg(args[field], args, field).tree
    if isinstance(tree, sp.Eq) and isinstance(tree.lhs, sp.Symbol) and tree.lhs not in tree.rhs.free_symbols:
        tree = tree.rhs
    if isinstance(tree, (Relational, sp.And)):
        raise _domain(f"{field}: give an expression, not an equation")
    return tree


def _tidy(value: sp.Expr) -> sp.Expr:
    simpler = sp.simplify(value)
    factored = sp.factor(simpler)
    return factored if sp.count_ops(factored) < sp.count_ops(simpler) else simpler


def _lines(gradient: sp.Expr | None, x0: sp.Expr, y0: sp.Expr) -> dict[str, Any]:
    """Tangent and normal at (x0, y0); None gradient means a vertical tangent."""
    x = sp.Symbol("x", real=True)

    def through(m: sp.Expr) -> dict[str, Any]:
        rhs = sp.expand(sp.simplify(m * (x - x0) + y0))
        return {"text": f"y = {sp.sstr(rhs)}", "latex": f"y = {sp.latex(rhs)}", "gradient": exact(m)}

    def vertical() -> dict[str, Any]:
        return {"text": f"x = {sp.sstr(x0)}", "latex": f"x = {sp.latex(x0)}", "vertical": True}

    if gradient is None:
        return {"tangent": vertical(), "normal": through(sp.Integer(0))}
    if gradient == 0:
        return {"tangent": through(gradient), "normal": vertical()}
    return {"tangent": through(gradient), "normal": through(-1 / gradient)}


def _student(args: dict[str, Any], expected: sp.Expr, context: CallContext, extras: Extras) -> dict[str, str]:
    student = parse_arg(args["student"], args, "student", sampled=True).tree  # only sampled
    if isinstance(student, sp.Eq):
        student = student.rhs
    verdict, check = expression_equivalence(student, expected, context)
    if check["confidence"] != "proved":
        extras.update(method=check["method"], confidence=check["confidence"])
    kind = verdict["verdict"]
    value = "correct" if kind in ("equivalent", "equivalent_on_domain") else "incorrect"
    return {"value": value if kind != "unknown" else "unknown"}


# --- math_differentiate: implicit and parametric -------------------------------------------------------------------


def implicit(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    tree = parse_arg(args["expression"], args, "expression").tree
    if not isinstance(tree, sp.Eq):
        raise _domain("expression: implicit differentiation needs an equation in x and y, such as x^2 + y^2 = 25")
    x, y = _symbol(tree, args.get("variable", "x")), _symbol(tree, args.get("dependent", "y"))
    h = tree.lhs - tree.rhs
    if y not in h.free_symbols:
        raise _domain(f"expression: the equation does not involve {y.name}; differentiate explicitly")
    try:
        with time_slice(slice_seconds(context)):
            dydx = _tidy(-sp.diff(h, x) / sp.diff(h, y))
            value: dict[str, Any] = {"mode": "implicit", "variable": x.name, "dependent": y.name,
                                     "derivative": exact(dydx)}  # fmt: skip
            working = [{"label": "differentiate each term with respect to x, d/dx f(y) = f'(y) dy/dx",
                        "value": f"dy/dx = -({sp.sstr(sp.diff(h, x))}) / ({sp.sstr(sp.diff(h, y))})",
                        "check_id": context.check_id}]  # fmt: skip
            if "point" in args:
                x0, y0 = (constant(v, "point", args) for v in args["point"])
                at = {x: x0, y: y0}
                if sp.simplify(substituted(h, at, "point")) != 0:
                    raise _domain(f"point: ({sp.sstr(x0)}, {sp.sstr(y0)}) is not on the curve")
                denominator = sp.simplify(substituted(sp.diff(h, y), at, "point"))
                gradient = None if denominator == 0 else sp.simplify(substituted(dydx, at, "point"))
                value["at"] = {"x": exact(x0), "y": exact(y0),
                               "gradient": exact(gradient) if gradient is not None else {"vertical": True},
                               **_lines(gradient, x0, y0)}  # fmt: skip
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    extras: Extras = {**PROVED, "working": working}
    if "student" in args:
        value["student"] = _student(args, dydx, context, extras)
    return {"status": "ok", **value}, extras


def parametric(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    xt, yt = _expression(args, "x_expression"), _expression(args, "y_expression")
    name = args.get("parameter") or next(iter(sorted(s.name for s in (xt.free_symbols | yt.free_symbols))), "t")
    t = _symbol(xt + yt, name)
    order = int(args.get("order", 1))
    if order > 2:
        raise _domain("order: parametric derivatives are given to the second order")
    try:
        with time_slice(slice_seconds(context)):
            dx, dy = sp.diff(xt, t), sp.diff(yt, t)
            if dx == 0:
                raise _domain("x_expression: x does not change with the parameter; the curve is a vertical line")
            dydx = _tidy(dy / dx)
            value: dict[str, Any] = {"mode": "parametric", "parameter": t.name, "derivative": exact(dydx)}
            working = [{"label": "dy/dx = (dy/dt) / (dx/dt)",
                        "value": f"({sp.sstr(dy)}) / ({sp.sstr(dx)})", "check_id": context.check_id}]  # fmt: skip
            second = None
            if order == 2:
                second = _tidy(sp.diff(dydx, t) / dx)
                value["second_derivative"] = exact(second)
                working.append({"label": "d²y/dx² = d/dt(dy/dx) / (dx/dt)", "value": sp.sstr(second),
                                "check_id": context.check_id})  # fmt: skip
            if "at" in args:
                t0 = constant(args["at"], "at", args)
                x0, y0, dx0, dy0 = (sp.simplify(substituted(e, {t: t0}, "at")) for e in (xt, yt, dx, dy))
                if dx0 == 0 and dy0 == 0:
                    return {"status": "unknown", "reason": "singular_point", "parameter": t.name}, UNKNOWN
                gradient = None if dx0 == 0 else sp.simplify(dy0 / dx0)
                at: dict[str, Any] = {"parameter_value": exact(t0), "x": exact(x0), "y": exact(y0),
                                      "gradient": exact(gradient) if gradient is not None else {"vertical": True},
                                      **_lines(gradient, x0, y0)}  # fmt: skip
                if second is not None and dx0 != 0:
                    at["second_derivative"] = exact(sp.simplify(substituted(second, {t: t0}, "at")))
                value["at"] = at
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    extras: Extras = {**PROVED, "working": working}
    if "student" in args:
        value["student"] = _student(args, second if order == 2 and second is not None else dydx, context, extras)
    return {"status": "ok", **value}, extras


# --- math_integrate: volumes, trapezium rule, substitution and parts -----------------------------------------------


def _limits(args: dict[str, Any]) -> tuple[sp.Expr, sp.Expr]:
    if "lower" not in args or "upper" not in args:
        raise _domain("give both lower and upper")
    return constant(args["lower"], "lower", args), constant(args["upper"], "upper", args)


def _continuous(tree: sp.Expr, var: sp.Symbol, a: sp.Expr, b: sp.Expr) -> None:
    """The function is defined and continuous on [a, b]. Its value at each end must be computable first: an integral
    over [0, 10^29] of 2^x would compute 2^(10^29) exactly, in C, where the per-call budget cannot stop it (SEC-08)."""
    substituted(tree, {var: a}, "lower")
    substituted(tree, {var: b}, "upper")
    low, high = (a, b) if sp.N(a) <= sp.N(b) else (b, a)
    if continuous_domain(tree, var, sp.Interval(low, high)) != sp.Interval(low, high):
        raise _domain("the function is undefined or discontinuous on part of the interval")


def volume(args: dict[str, Any], tree: sp.Expr, var: sp.Symbol, context: CallContext) -> tuple[Any, Extras]:
    a, b = _limits(args)
    _continuous(tree, var, a, b)
    axis = args["volume"]
    try:
        with time_slice(slice_seconds(context)):
            integral = sp.integrate(tree**2, (var, a, b))
            if integral.has(sp.Integral):
                return {"status": "unknown", "reason": "no_closed_form"}, UNKNOWN
            result = sp.simplify(sp.pi * integral)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    rule = f"V = π∫ {'y' if axis == 'x_axis' else 'x'}² d{var.name}"
    working = [{"label": "volume of revolution", "value": f"{rule} from {sp.sstr(a)} to {sp.sstr(b)}",
                "check_id": context.check_id}]  # fmt: skip
    value = {"status": "ok", "volume": {"axis": axis, "value": exact(result), "over_pi": exact(sp.simplify(integral))},
             "variable": var.name, "lower": exact(a), "upper": exact(b)}  # fmt: skip
    extras: Extras = {**PROVED, "working": working}
    if "student" in args:
        value["student"] = _student(args, result, context, extras)
    return value, extras


def trapezium(args: dict[str, Any], tree: sp.Expr, var: sp.Symbol, context: CallContext) -> tuple[Any, Extras]:
    a, b = _limits(args)
    n = int(args["strips"])
    _continuous(tree, var, a, b)
    sf = int(args.get("sf", 4))
    h = (b - a) / n
    xs = [sp.simplify(a + k * h) for k in range(n + 1)]
    ys = [sp.simplify(substituted(tree, {var: xk}, "upper")) for xk in xs]
    if any(yk.is_real is False or yk.has(sp.zoo, sp.nan) for yk in ys):
        raise _domain("an ordinate is undefined or not real")
    estimate = sp.nsimplify(h / 2 * (ys[0] + 2 * sum(ys[1:-1], sp.Integer(0)) + ys[-1]))
    value: dict[str, Any] = {
        "status": "ok", "strips": n, "width": exact(h),
        "ordinates": [{"x": exact(xk), "y": exact(yk, sf + 2)} for xk, yk in zip(xs, ys, strict=True)],
        "estimate": {**exact(sp.simplify(estimate), sf), "rounded": significant(sp.N(estimate, 30), sf)},
    }  # fmt: skip
    working = [{"label": "trapezium rule", "value": "h/2 [y0 + 2(y1 + ... + y(n-1)) + yn], h = (b - a)/n",
                "check_id": context.check_id}]  # fmt: skip
    extras: Extras = {**PROVED, "working": working}
    try:
        with time_slice(slice_seconds(context)):
            integral = sp.integrate(tree, (var, a, b))
            second = sp.diff(tree, var, 2)
            low, high = (a, b) if sp.N(a) <= sp.N(b) else (b, a)
            curvature = _sign_on(second, var, low, high)
    except SliceExpired:
        integral, curvature = None, "unknown"
    if integral is not None and not integral.has(sp.Integral):
        integral = sp.simplify(integral)
        error = sp.simplify(estimate - integral)
        value["exact"] = exact(integral, sf + 2)
        value["error"] = exact(error, sf)
        if integral != 0:
            value["percentage_error"] = significant(sp.N(abs(error) / abs(integral) * 100, 30), sf)
    value["estimate_kind"] = {"positive": "overestimate", "negative": "underestimate", "zero": "exact"}.get(
        curvature, "unknown")  # fmt: skip
    if "student" in args:
        student = constant(args["student"], "student", args)
        rounded = sp.nsimplify(significant(sp.N(estimate, 30), sf), rational=True)
        value["student"] = {"value": "correct" if student in (sp.simplify(estimate), rounded) else "incorrect"}
    return value, extras


def _sign_on(expr: sp.Expr, var: sp.Symbol, low: sp.Expr, high: sp.Expr) -> str:
    """The sign of expr on [low, high]: positive, negative, zero, or mixed / unknown."""
    if sp.simplify(expr) == 0:
        return "zero"
    roots = sp.solveset(expr, var, sp.Interval.open(low, high))
    if not isinstance(roots, (sp.FiniteSet, sp.sets.sets.EmptySet)):
        return "unknown"
    if roots != sp.S.EmptySet:
        samples = sorted({sp.N(r) for r in roots.args})
        cuts = [sp.N(low), *samples, sp.N(high)]
        signs = {sp.sign(sp.N(expr.subs(var, (p + q) / 2))) for p, q in itertools.pairwise(cuts)}
    else:
        signs = {sp.sign(sp.N(expr.subs(var, (low + high) / 2)))}
    if signs == {1}:
        return "positive"
    if signs == {-1}:
        return "negative"
    return "mixed"


def substitution(args: dict[str, Any], tree: sp.Expr, var: sp.Symbol, context: CallContext) -> tuple[Any, Extras]:
    sub = parse_arg(args["substitution"], args, "substitution").tree
    if not isinstance(sub, sp.Eq) or not isinstance(sub.lhs, sp.Symbol):
        raise _domain("substitution: write it as u = g(x)")
    u, g = sub.lhs, sub.rhs
    if var not in g.free_symbols:
        raise _domain(f"substitution: g must involve {var.name}")
    try:
        with time_slice(slice_seconds(context)):
            dg = sp.diff(g, var)
            candidate = sp.simplify(tree / dg)
            inverse = [r for r in sp.solve(sp.Eq(u, g), var) if r.is_real is not False]
            transformed = None
            if not candidate.has(var):
                transformed = candidate
            else:
                # x written in terms of u; with several branches (x = ±√(u - k)) they must all agree.
                attempts = {sp.simplify(candidate.subs(var, branch)) for branch in inverse}
                attempt = attempts.pop() if len(attempts) == 1 else None
                if attempt is not None and not attempt.has(var):
                    transformed = attempt
                else:
                    attempt = sp.simplify(candidate.subs(g, u))
                    if not attempt.has(var):
                        transformed = attempt
            if transformed is None:
                return {"status": "unknown", "reason": "substitution_does_not_remove_x"}, UNKNOWN
            transformed = sp.simplify(transformed.subs(u, sp.Symbol(u.name, real=True)))
            u = sp.Symbol(u.name, real=True)
            value: dict[str, Any] = {"status": "ok", "method": "substitution", "integrand_in_u": exact(transformed)}
            working = [{"label": f"d{u.name}/d{var.name}", "value": sp.sstr(dg), "check_id": context.check_id},
                       {"label": f"integrand in {u.name}", "value": sp.sstr(transformed),
                        "check_id": context.check_id}]  # fmt: skip
            antiderivative = sp.integrate(transformed, u)
            if antiderivative.has(sp.Integral):
                return {"status": "unknown", "reason": "no_closed_form", **value}, UNKNOWN
            if "lower" in args or "upper" in args:
                a, b = _limits(args)
                _continuous(tree, var, a, b)
                ua, ub = sp.simplify(substituted(g, {var: a}, "lower")), sp.simplify(substituted(g, {var: b}, "upper"))
                result = sp.simplify(
                    substituted(antiderivative, {u: ub}, "upper") - substituted(antiderivative, {u: ua}, "lower")
                )
                value["limits_in_u"] = [exact(ua), exact(ub)]
                value["definite"] = exact(result)
                expected = result
            else:
                back = sp.simplify(antiderivative.subs(u, g))
                value["antiderivative"] = exact(back)
                expected = back
            # The result is checked against SymPy's own integral of the original.
            direct = sp.integrate(tree, (var, *_limits(args))) if "definite" in value else None
            if direct is not None and not direct.has(sp.Integral) and sp.simplify(direct - expected) != 0:
                return {"status": "unknown", "reason": "check_failed"}, UNKNOWN
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    extras: Extras = {**PROVED, "working": working}
    if "student" in args and "definite" in value:
        value["student"] = _student(args, expected, context, extras)
    return value, extras


def parts(args: dict[str, Any], tree: sp.Expr, var: sp.Symbol, context: CallContext) -> tuple[Any, Extras]:
    u = _expression(args, "parts_u")
    try:
        with time_slice(slice_seconds(context)):
            dv = sp.simplify(tree / u)
            v = sp.integrate(dv, var)
            if v.has(sp.Integral):
                return {"status": "unknown", "reason": "dv_has_no_closed_form"}, UNKNOWN
            du = sp.diff(u, var)
            remaining = sp.simplify(v * du)
            rest = sp.integrate(remaining, var)
            if rest.has(sp.Integral):
                return {"status": "unknown", "reason": "no_closed_form"}, UNKNOWN
            antiderivative = sp.simplify(u * v - rest)
            if sp.simplify(sp.diff(antiderivative, var) - tree) != 0:
                return {"status": "unknown", "reason": "check_failed"}, UNKNOWN
            value: dict[str, Any] = {"status": "ok", "method": "parts", "u": exact(u), "dv": exact(dv), "v": exact(v),
                                     "du": exact(du), "remaining_integral": exact(remaining),
                                     "antiderivative": exact(antiderivative)}  # fmt: skip
            expected = antiderivative
            if "lower" in args or "upper" in args:
                a, b = _limits(args)
                _continuous(tree, var, a, b)
                expected = sp.simplify(
                    substituted(antiderivative, {var: b}, "upper") - substituted(antiderivative, {var: a}, "lower")
                )
                value["definite"] = exact(expected)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    working = [{"label": "by parts", "value": "∫u dv = uv - ∫v du", "check_id": context.check_id}]
    extras: Extras = {**PROVED, "working": working}
    if "student" in args and "definite" in value:
        value["student"] = _student(args, expected, context, extras)
    return value, extras


# --- math_solve_ode -------------------------------------------------------------------------------------------------


def math_solve_ode(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """dy/dx = f(x) g(y): the general solution, the particular solution through a stated point, and a check of a
    student's solution by substitution."""
    rhs = _expression(args, "rhs")
    x, y = _symbol(rhs, args.get("independent", "x")), _symbol(rhs, args.get("dependent", "y"))
    if x == y:
        raise _domain("independent and dependent must be different variables")
    c = sp.Symbol(args.get("constant", "c"), real=True)
    if c in (x, y):
        raise _domain("constant: choose a name that is not one of the variables")
    try:
        with time_slice(slice_seconds(context)):
            value, working = _separable(rhs, x, y, c, args, context)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNKNOWN
    if value.get("status") == "unknown":
        return value, {**UNKNOWN, "working": working}
    extras: Extras = {**PROVED, "working": working}
    if "student" in args:
        value["student"] = _check_ode_student(args, rhs, x, y, context, extras)
    return value, extras


def _separable(rhs: sp.Expr, x: sp.Symbol, y: sp.Symbol, c: sp.Symbol, args: dict[str, Any],
               context: CallContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # fmt: skip
    check = context.check_id
    parts = sp.separatevars(sp.factor(rhs), symbols=[x, y], dict=True) if rhs.has(y) else {
        "coeff": sp.Integer(1), x: rhs, y: sp.Integer(1)}  # fmt: skip
    if not parts:
        return {"status": "unknown", "reason": "not_separable"}, []
    f = sp.simplify(parts["coeff"] * parts.get(x, sp.Integer(1)))
    g = sp.simplify(parts.get(y, sp.Integer(1)))
    if g == 0:
        raise _domain("rhs: dy/dx is identically zero; y is constant")
    left, right = sp.integrate(1 / g, y), sp.integrate(f, x)
    if left.has(sp.Integral) or right.has(sp.Integral):
        return {"status": "unknown", "reason": "no_closed_form"}, []
    shown = _abs_logs(left)  # the A level form: ln|g(y)|
    working = [
        {"label": "separate", "value": f"∫ 1/({sp.sstr(g)}) d{y.name} = ∫ {sp.sstr(f)} d{x.name}", "check_id": check},
        {"label": "integrate", "value": f"{sp.sstr(shown)} = {sp.sstr(right)} + {c.name}", "check_id": check},
    ]
    value: dict[str, Any] = {"status": "ok", "method": "separation of variables",
                             "implicit": {"text": f"{sp.sstr(shown)} = {sp.sstr(right)} + {c.name}",
                                          "latex": f"{sp.latex(shown)} = {sp.latex(right)} + {c.name}"}}  # fmt: skip
    solutions = _solve_for(left, right, c, y)
    if len(solutions) == 1 and _satisfies(solutions[0], rhs, x, y):
        value["general"] = exact(solutions[0])
    if "condition" not in args:
        return value, working
    x0, y0 = (constant(v, "condition", args) for v in args["condition"])
    if sp.simplify(substituted(g, {y: y0}, "condition")) == 0:
        particular = y0  # the constant solution y = y0 through the point: separating would divide by zero
        working.append({"label": "constant solution", "value": f"g({sp.sstr(y0)}) = 0, so {y.name} = {sp.sstr(y0)}",
                        "check_id": check})  # fmt: skip
    else:
        # The constant from the condition; with ln of a negative value it is complex here, and the real constant of
        # the ln|.| form is reported.
        at_x0 = substituted(right, {x: x0}, "condition")
        c0 = sp.simplify(substituted(left, {y: y0}, "condition") - at_x0)
        c_shown = sp.simplify(substituted(shown, {y: y0}, "condition") - at_x0)
        if c_shown.is_real is not True:
            raise _domain("condition: the solution is undefined at the stated point")
        working.append({"label": f"{x.name} = {sp.sstr(x0)}, {y.name} = {sp.sstr(y0)}",
                        "value": f"{c.name} = {sp.sstr(c_shown)}", "check_id": check})  # fmt: skip
        fitting = []
        for candidate in solutions:
            branch = sp.simplify(candidate.subs(c, c0))
            if branch.has(sp.I):
                branch = sp.simplify(sp.expand_complex(branch))
            if branch.has(sp.I) or sp.simplify(substituted(branch, {x: x0}, "condition") - y0) != 0:
                continue
            if _satisfies(branch, rhs, x, y):
                fitting.append(branch)
        if len(fitting) != 1:
            return {"status": "unknown", "reason": "ambiguous_branch" if fitting else "no_explicit_solution",
                    "implicit": value["implicit"], "constant_value": exact(c_shown)}, working  # fmt: skip
        particular = fitting[0]
        value["constant_value"] = exact(c_shown)
    value["particular"] = exact(particular)
    if "at" in args:
        x1 = constant(args["at"], "at", args)
        y1 = sp.simplify(substituted(particular, {x: x1}, "at"))
        if y1.is_real is False or y1.has(sp.zoo, sp.nan):
            raise _domain(f"at: the solution is undefined at {x.name} = {sp.sstr(x1)}")
        value["at"] = {"x": exact(x1), "y": exact(y1)}
    return value, working


def _solve_for(left: sp.Expr, right: sp.Expr, c: sp.Symbol, y: sp.Symbol) -> list[sp.Expr]:
    """y from left(y) = right(x) + c. With logarithms on the left both sides are exponentiated first (e^c written as
    one constant), which leaves an algebraic equation. Each branch is verified by substitution afterwards, so SymPy's
    own (slow) check is skipped."""
    if left.has(sp.log):
        k = sp.Dummy("k", positive=True)
        algebraic = sp.simplify(sp.exp(sp.expand_log(left, force=True)))
        found = sp.solve(sp.Eq(algebraic, k * sp.exp(right)), y, check=False)
        return [sp.simplify(sol.subs(k, sp.exp(c))) for sol in found]
    return [sp.simplify(sol) for sol in sp.solve(sp.Eq(left, right + c), y, check=False)]


def _abs_logs(expr: sp.Expr) -> sp.Expr:
    return expr.replace(lambda e: isinstance(e, sp.log), lambda e: sp.log(sp.Abs(e.args[0])))


def _satisfies(solution: sp.Expr, rhs: sp.Expr, x: sp.Symbol, y: sp.Symbol) -> bool:
    """The solution satisfies dy/dx = rhs identically (checked by substitution)."""
    residual = sp.simplify(sp.diff(solution, x) - rhs.subs(y, solution))
    if residual == 0:
        return True
    samples = [sp.Rational(1, 3), sp.Rational(1, 2), sp.Rational(2, 3)]
    free = residual.free_symbols - {x}
    values = []
    for s in samples:
        point = residual.subs({x: s, **{f: sp.Rational(3, 7) for f in free}})
        values.append(complex(sp.N(point, 30)))
    return all(abs(v) < 1e-20 for v in values)


def _check_ode_student(args: dict[str, Any], rhs: sp.Expr, x: sp.Symbol, y: sp.Symbol, context: CallContext,
                       extras: Extras) -> dict[str, Any]:  # fmt: skip
    """A student's explicit solution y = h(x): it satisfies the equation, and the stated condition if there is one."""
    tree = parse_arg(args["student"], args, "student").tree
    if isinstance(tree, sp.Eq):
        if tree.lhs == y and y not in tree.rhs.free_symbols:
            tree = tree.rhs
        else:
            solutions = sp.solve(tree, y)
            if len(solutions) != 1:
                return {"value": "unknown", "reason": "implicit_solution"}
            tree = solutions[0]
    tree = tree.subs({s: x for s in tree.free_symbols if s.name == x.name})
    satisfies = _satisfies(tree, rhs, x, y)
    out: dict[str, Any] = {"satisfies_equation": satisfies}
    if "condition" in args:
        x0, y0 = (constant(v, "condition", args) for v in args["condition"])
        out["satisfies_condition"] = sp.simplify(substituted(tree, {x: x0}, "condition") - y0) == 0
    out["value"] = "correct" if satisfies and out.get("satisfies_condition", True) else "incorrect"
    return out
