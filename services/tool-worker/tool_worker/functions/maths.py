"""Mathematics functions (VER-03 to VER-07, VER-09, VER-10, VER-27).

Every function is a pure function of its arguments and the pinned libraries (VER-01): symbolic work runs inside a
time slice and answers `unknown` when the slice runs out (VER-27); numeric sampling is seeded from the arguments'
hash; outputs are canonical (sorted solutions, exact forms with LaTeX, decimals to stated precision)."""

from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import sympy as sp
from sympy.calculus.util import continuous_domain
from sympy.core.relational import Relational

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions import constants
from tool_worker.functions.common import parse_arg, substituted
from tool_worker.functions.numeric import round_sf
from tool_worker.grammar import TooLarge, bounded_subs, describe, exact_number, huge_power
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]


# --- shared ---------------------------------------------------------------------------------------------


def exact(value: sp.Basic, sf: int = 6) -> dict[str, Any]:
    """An exact value with its LaTeX and, when numeric, a decimal to `sf` significant figures."""
    out: dict[str, Any] = {"text": sp.sstr(value), "latex": sp.latex(value)}
    if value.is_number and value.is_real is not False:
        out["decimal"] = significant(sp.N(value, 30), sf)
    return out


def significant(value: sp.Basic, sf: int) -> str:
    """Round half away from zero to `sf` significant figures (VER-09: never banker's rounding). The power of ten is
    kept apart from the digits: a sampled (n+1)^(10^29) has an exponent beyond any Decimal context's range."""
    digits, _, power = str(sp.N(value, 40)).partition("e")
    d = Decimal(digits)
    if d == 0:
        return "0"
    rounded = round_sf(d, sf)
    adjusted = rounded.adjusted() + int(power or 0)
    if adjusted < -4 or adjusted >= 12:  # standard form for very small or large values
        mantissa = rounded.scaleb(-rounded.adjusted())
        return f"{format(mantissa, 'f')}e{adjusted}"
    return format(rounded.scaleb(int(power or 0)), "f")


def decimal_places(value: sp.Basic, dp: int) -> str:
    d = Decimal(str(sp.N(value, 40)))
    return str(d.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


def slice_seconds(context: CallContext) -> float:
    return float(context.setting("symbolic_slice_ms", 800)) / 1000


def _sorted_values(values: list[sp.Basic]) -> list[sp.Basic]:
    def key(v: sp.Basic) -> tuple[int, float, str]:
        try:
            return (0, float(sp.N(v)), sp.sstr(v))
        except TypeError:
            return (1, 0.0, sp.sstr(v))

    return sorted(values, key=key)


def _variables(*trees: sp.Basic) -> list[sp.Symbol]:
    found: set[sp.Symbol] = set()
    for tree in trees:
        found |= tree.free_symbols
    return sorted(found, key=lambda s: s.name)


# --- math_parse (VER-03) ---------------------------------------------------------------------------------


def math_parse(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Parse plain maths or the LaTeX subset with our own grammar (VER-03)."""
    parsed = parse_arg(args["text"], args, sampled=True)  # parsed and described, never worked
    value = describe(parsed.tree)
    value["as_written_latex"] = sp.latex(parsed.as_written)
    return value, {"latex": value["latex"], "method": "exact", "confidence": "proved"}


# --- equivalence (VER-04, VER-05, VER-27) ----------------------------------------------------------------


def _undefined_points(expr: sp.Expr, var: sp.Symbol) -> set[sp.Basic]:
    """Real points where a rational subexpression divides by zero (finite sets only)."""
    points: set[sp.Basic] = set()
    for sub in sp.preorder_traversal(expr):
        if isinstance(sub, sp.Pow) and sub.exp.is_negative:
            zeros = sp.solveset(sub.base, var, sp.S.Reals)
            if isinstance(zeros, sp.FiniteSet):
                points |= set(zeros)
    return points


def _sample(var: sp.Symbol, rng: random.Random) -> sp.Basic:
    if var.is_integer:
        return sp.Integer(rng.randint(1 if var.is_positive else -6, 6))
    low = 0.1 if (var.is_positive or var.is_nonnegative) else -5.0
    return sp.Float(rng.uniform(low, 5.0), 30)


ZERO_DIGITS = 3  # a value below 10^-(3 x the working digits) is zero at the working precision


def _real_value(value: sp.Basic, floor: sp.Float) -> sp.Basic | None:
    """A sampled value as a real number; None when it is not real (an imaginary part beyond rounding noise)."""
    if value.is_real:
        return value
    real, imaginary = value.as_real_imag()
    return real if imaginary.is_number and abs(imaginary) <= floor else None


def _numeric(a: sp.Expr, b: sp.Expr, context: CallContext) -> tuple[str, dict[str, Any] | None, int]:
    """Compare at seeded points: ('equivalent' | 'not_equivalent' | 'unknown', counterexample, valid points).

    Each side is evaluated with the point as evalf's substitution, never substituted first: evalf's adaptive
    precision brings a sum that cancels to its true zero (sin^2 + cos^2 - 1 is 0, not rounding noise at the inputs'
    precision), and a sampled power such as (n+1)^(10^29) stays a float instead of an exact integer computed in C,
    where the per-call budget cannot stop it (SEC-08). So values compare relatively at any magnitude: 2e-12 x is not
    1e-12 x. A point where one side is real and the other is not is a counterexample: ln(x^2) is not 2 ln(x) at x < 0.
    Points where neither side is real are skipped."""
    rng = random.Random(context.seed)
    points = int(context.setting("equivalence_points", 12))
    digits = int(context.setting("numeric_digits", 30))
    tolerance = sp.Float(context.setting("relative_tolerance", 1e-9), digits)
    floor = sp.Float(10, digits) ** (-ZERO_DIGITS * digits)
    variables = _variables(a, b)
    valid = 0
    for _ in range(points * 3):
        if valid >= points:
            break
        point = {v: _sample(v, rng) for v in variables}
        try:
            va, vb = a.evalf(digits, subs=point), b.evalf(digits, subs=point)
        except (ZeroDivisionError, ValueError, TypeError):
            continue
        if not (va.is_number and vb.is_number) or va.has(sp.zoo, sp.nan, sp.oo) or vb.has(sp.zoo, sp.nan, sp.oo):
            continue
        ra, rb = _real_value(va, floor), _real_value(vb, floor)
        if ra is None and rb is None:
            continue
        valid += 1
        if ra is None or rb is None:
            example = {str(k): significant(v, 10) for k, v in point.items()}
            shown = {side: "not real" if r is None else significant(r, 10) for side, r in (("a", ra), ("b", rb))}
            return "not_equivalent", {"at": example, **shown}, valid
        gap = abs(ra - rb)
        if gap > floor and gap > tolerance * max(abs(ra), abs(rb)):
            example = {str(k): significant(v, 10) for k, v in point.items()}
            return "not_equivalent", {"at": example, "a": significant(ra, 10), "b": significant(rb, 10)}, valid
    return ("equivalent" if valid >= int(context.setting("equivalence_min_valid", 8)) else "unknown"), None, valid


CHEAP = (sp.expand, sp.cancel, sp.together)
EXPENSIVE = (sp.trigsimp, sp.radsimp, sp.simplify)


def _reduce(difference: sp.Basic, transforms: tuple[Any, ...]) -> sp.Basic:
    for transform in transforms:
        difference = transform(difference)
        if difference == 0 or (difference.is_number and difference != 0):
            break
    return difference


def _decided(
    difference: sp.Basic, a: sp.Expr, b: sp.Expr, context: CallContext
) -> tuple[dict[str, Any], Extras] | None:
    """A symbolic verdict when the difference reduced to zero or to a non-zero number; None otherwise."""
    if difference == 0:
        variables = _variables(a, b)
        if len(variables) == 1:
            var = variables[0]
            only_a = _undefined_points(a, var) - _undefined_points(b, var)
            only_b = _undefined_points(b, var) - _undefined_points(a, var)
            if only_a or only_b:
                excluded = [
                    exact(p) | {"undefined_in": "a" if p in only_a else "b"}
                    for p in _sorted_values(list(only_a | only_b))
                ]
                return (
                    {"verdict": "equivalent_on_domain", "excluded_points": excluded},
                    {"method": "symbolic", "confidence": "proved"},
                )
        return {"verdict": "equivalent"}, {"method": "symbolic", "confidence": "proved"}
    if difference.is_number and difference.is_real and difference != 0:
        _, example, _ = _numeric(a, b, context)
        return (
            {"verdict": "not_equivalent", "difference": exact(difference), "counterexample": example},
            {"method": "symbolic", "confidence": "proved"},
        )
    return None


def _expressions(a: sp.Expr, b: sp.Expr, context: CallContext) -> tuple[dict[str, Any], Extras]:
    """Cheap symbolic reduction, then a seeded numeric screen (a counterexample proves non-equivalence), then the
    expensive simplifiers only when the samples agree. Expensive simplification of a false identity (such as
    log(x + 4) = log(x) + log(4)) can run until the slice expires; the screen answers it in milliseconds. A huge
    power is only sampled, never expanded."""
    difference: sp.Basic = a - b
    symbolic = not huge_power(difference)
    try:
        with time_slice(slice_seconds(context)):
            if symbolic:
                difference = _reduce(difference, CHEAP)
                decided = _decided(difference, a, b, context)
                if decided:
                    return decided
    except SliceExpired:
        pass
    try:
        with time_slice(slice_seconds(context)):
            verdict, example, valid = _numeric(a, b, context)
    except SliceExpired:
        return {"verdict": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    if verdict == "not_equivalent":
        return (
            {"verdict": "not_equivalent", "counterexample": example},
            {"method": "numeric_sampling", "confidence": "proved"},
        )
    try:
        with time_slice(slice_seconds(context)):
            decided = _decided(_reduce(difference, EXPENSIVE), a, b, context) if symbolic else None
            if decided:
                return decided
    except SliceExpired:
        pass
    if verdict == "equivalent":
        return {"verdict": "equivalent", "points": valid}, {"method": "numeric_sampling", "confidence": "sampled"}
    return (
        {"verdict": "unknown", "reason": "insufficient_points", "points": valid},
        {"method": "numeric_sampling", "confidence": "undecided"},
    )


def solution_set(relation: sp.Basic, var: sp.Symbol, domain: sp.Set = sp.S.Reals) -> sp.Set:
    if isinstance(relation, sp.Eq):
        return sp.solveset(relation.lhs - relation.rhs, var, domain)
    if isinstance(relation, sp.And):
        result = domain
        for part in relation.args:
            result = result.intersect(solution_set(part, var, domain))
        return result
    if isinstance(relation, Relational):
        return sp.solveset(relation, var, domain)
    raise WorkerError(Code.DOMAIN_ERROR, "not an equation or inequality")


def _any_element(s: sp.Set) -> sp.Basic | None:
    if isinstance(s, sp.FiniteSet) and s.args:
        return _sorted_values(list(s.args))[0]
    if isinstance(s, sp.Interval):
        lo, hi = s.inf, s.sup
        if lo.is_finite and hi.is_finite:
            return (lo + hi) / 2
        if lo.is_finite:
            return lo + 1
        if hi.is_finite:
            return hi - 1
        return sp.Integer(0)
    if isinstance(s, sp.Union):
        return _any_element(s.args[0])
    return None


def _relations(a: sp.Basic, b: sp.Basic, context: CallContext) -> tuple[dict[str, Any], Extras]:
    """Relations are compared by their solution sets. A huge power is undecided: solving it runs where the per-call
    budget cannot stop it (SEC-08), and a relation has no sampled screen."""
    if huge_power(a) or huge_power(b):
        return {"verdict": "unknown", "reason": "too_large"}, {"method": "heuristic", "confidence": "undecided"}
    variables = _variables(a, b)
    try:
        with time_slice(slice_seconds(context)):
            if len(variables) == 1:
                var = variables[0]
                sa, sb = solution_set(a, var), solution_set(b, var)
                if isinstance(sa, sp.ConditionSet) or isinstance(sb, sp.ConditionSet):
                    return (
                        {"verdict": "unknown", "reason": "no_closed_form"},
                        {"method": "symbolic", "confidence": "undecided"},
                    )
                if sa == sb:
                    return {"verdict": "equivalent"}, {"method": "symbolic", "confidence": "proved"}
                witness = _any_element(sp.Complement(sa, sb)) or _any_element(sp.Complement(sb, sa))
                result: dict[str, Any] = {
                    "verdict": "not_equivalent",
                    "solutions_a": sp.sstr(sa),
                    "solutions_b": sp.sstr(sb),
                }
                if witness is not None:
                    result["counterexample"] = {"at": {var.name: exact(witness)["text"]}}
                return result, {"method": "symbolic", "confidence": "proved"}
            if isinstance(a, sp.Eq) and isinstance(b, sp.Eq):
                ratio = sp.simplify((a.lhs - a.rhs) / (b.lhs - b.rhs))
                if ratio.is_number and ratio != 0:
                    return {"verdict": "equivalent"}, {"method": "symbolic", "confidence": "proved"}
    except SliceExpired:
        return {"verdict": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    return {"verdict": "unknown", "reason": "ambiguous"}, {"method": "symbolic", "confidence": "undecided"}


def math_check_equivalence(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Equivalence of two expressions, equations or inequalities (VER-04) under assumptions (VER-05)."""
    a = parse_arg(args["a"], args, "a", sampled=True).tree  # a huge power is only sampled (_expressions)
    b = parse_arg(args["b"], args, "b", sampled=True).tree
    is_relation = (isinstance(a, (Relational, sp.And)), isinstance(b, (Relational, sp.And)))
    if is_relation[0] != is_relation[1]:
        raise WorkerError(
            Code.DOMAIN_ERROR, "compare an equation with an equation, or an expression with an expression"
        )
    value, extras = (_relations if is_relation[0] else _expressions)(a, b, context)
    warnings = (
        [{"code": "assumption_added", "message": "variables are taken as real numbers"}]
        if not args.get("assumptions") and _variables(a, b)
        else []
    )
    if warnings:
        extras["warnings"] = warnings
    return value, extras


# --- solve (VER-07) ---------------------------------------------------------------------------------------

DOMAINS = {"real": sp.S.Reals, "integer": sp.S.Integers, "positive": sp.Interval.open(0, sp.oo)}


def _numeric_roots(expr: sp.Expr, var: sp.Symbol, interval: list[Any], context: CallContext) -> list[sp.Basic]:
    lo, hi = (exact_number(str(v), 0) for v in interval)
    roots: list[sp.Basic] = []
    steps = 200
    xs = [lo + (hi - lo) * sp.Rational(i, steps) for i in range(steps + 1)]
    values = []
    for x in xs:
        try:
            values.append(sp.N(substituted(expr, {var: x}, "interval"), 30))
        except (ZeroDivisionError, TypeError):
            values.append(sp.nan)
    for (x0, v0), (x1, v1) in zip(zip(xs, values, strict=True), zip(xs[1:], values[1:], strict=True), strict=False):
        if v0.is_real and v1.is_real and (v0 == 0 or v0 * v1 < 0):
            try:
                root = sp.nsolve(expr, var, (x0 + x1) / 2, prec=30, verify=True)
            except (ValueError, ZeroDivisionError):
                continue
            if lo <= root <= hi and not any(abs(root - r) < 1e-12 for r in roots):
                roots.append(root)
    return _sorted_values(roots)


def math_solve(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Every solution in the domain, exact and to 6 significant figures (VER-07), with the constants used and their
    sources (VER-12) and the recorded positive direction for motion problems (VER-17). From version 4 (VER-30): the
    complex roots of a polynomial equation (domain `complex`, where `i` is the imaginary unit), and the numerical
    methods (Newton-Raphson, fixed-point iteration and the sign-change check) named by `method`."""
    complex_domain = args.get("domain") == "complex"
    method = args.get("method")
    stray = [] if method else [name for name in NUMERICAL_ARGS if name in args]
    if stray:
        raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(stray)} apply only with a method")
    if method and complex_domain:
        raise WorkerError(Code.DOMAIN_ERROR, "the numerical methods work over the real numbers; drop domain complex")
    trees = [
        parse_arg(text, args, f"equations[{i}]", imaginary_unit=complex_domain).tree
        for i, text in enumerate(args["equations"])
    ]
    if not all(isinstance(t, sp.Eq) for t in trees):
        raise WorkerError(Code.DOMAIN_ERROR, "math_solve takes equations; use the inequality tools for inequalities")
    from tool_worker.functions import quantities  # imported here: quantities builds on units, which imports maths

    try:
        trees, stated_units = quantities.apply(trees, args)  # VER-17: quantities with units, dimension-checked
        trees, constant_extras = constants.apply(trees, args, args.get("unknowns", []))
    except constants.ConventionMissing as no_convention:
        return no_convention.result()
    if any(t is sp.false for t in trees):
        value, extras = {"case": "no_solution"}, {"method": "symbolic", "confidence": "proved"}
    elif all(t is sp.true for t in trees):
        value, extras = {"case": "all_values"}, {"method": "symbolic", "confidence": "proved"}
    elif method:
        value, extras = _numerical([t for t in trees if t is not sp.true], args, context)
    elif complex_domain:
        value, extras = _solve_complex([t for t in trees if t is not sp.true], args, context)
    else:
        value, extras = _solve([t for t in trees if t is not sp.true], args, context)
    if args.get("positive_direction"):
        value["positive_direction"] = args["positive_direction"]
    solutions: list[dict[str, dict[str, Any]]] = list(value.get("solutions") or [])  # type: ignore[arg-type]
    for solution in solutions:
        for name in sorted(solution.keys() & stated_units.keys()):
            solution[name]["unit"] = stated_units[name]
    return value, {**extras, **constant_extras}


def _solve(trees: list[sp.Basic], args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    equations: list[sp.Eq | None] = [t if isinstance(t, sp.Eq) else None for t in trees]
    names = args.get("unknowns") or [s.name for s in _variables(*trees)]
    symbols = {s.name: s for t in trees for s in t.free_symbols}
    unknowns = [symbols.get(n) or sp.Symbol(n, real=True) for n in names]
    domain_name = args.get("domain", "real")
    domain = DOMAINS[domain_name]
    try:
        with time_slice(slice_seconds(context)):
            if len(equations) == 1 and len(unknowns) == 1:
                eq, var = equations[0], unknowns[0]
                assert eq is not None
                result = sp.solveset(eq.lhs - eq.rhs, var, domain)
                if result == domain:
                    return {"case": "all_values"}, {"method": "symbolic", "confidence": "proved"}
                if result == sp.S.EmptySet:
                    return {"case": "no_solution"}, {"method": "symbolic", "confidence": "proved"}
                if isinstance(result, sp.FiniteSet):
                    solutions = [{var.name: exact(v)} for v in _sorted_values(list(result.args))]
                    return {"case": "solutions", "solutions": solutions}, {"method": "symbolic", "confidence": "proved"}
                if not isinstance(result, sp.ConditionSet):
                    return (
                        {"case": "parametric", "set": sp.sstr(result), "latex": sp.latex(result)},
                        {"method": "symbolic", "confidence": "proved"},
                    )
            else:
                solved = sp.solve([e.lhs - e.rhs for e in equations if e is not None], unknowns, dict=True)
                real = [s for s in solved if all(v.is_real is not False for v in s.values())]
                if not real:
                    return {"case": "no_solution"}, {"method": "symbolic", "confidence": "proved"}
                if any(len(s) < len(unknowns) for s in real):
                    return (
                        {"case": "parametric", "set": sp.sstr(real)},
                        {"method": "symbolic", "confidence": "proved"},
                    )
                ordered = sorted(real, key=lambda s: [float(sp.N(s[u])) for u in unknowns])
                solutions = [{u.name: exact(s[u]) for u in unknowns} for s in ordered]
                return {"case": "solutions", "solutions": solutions}, {"method": "symbolic", "confidence": "proved"}
    except SliceExpired:
        pass
    if len(equations) == 1 and len(unknowns) == 1 and args.get("interval"):
        eq = equations[0]
        assert eq is not None
        roots = _numeric_roots(eq.lhs - eq.rhs, unknowns[0], args["interval"], context)
        solutions = [{unknowns[0].name: {"decimal": significant(r, 6), "text": significant(r, 15)}} for r in roots]
        return (
            {"case": "solutions" if roots else "no_solution", "solutions": solutions, "numeric": True},
            {"method": "numeric_sampling", "confidence": "sampled"},
        )
    raise WorkerError(Code.DOMAIN_ERROR, "no exact solution; give an interval")


# --- simplify -------------------------------------------------------------------------------------------

FORMS = {"simplest": sp.simplify, "expanded": sp.expand, "factorised": sp.factor, "single_fraction": sp.together}
A_LEVEL_FORMS = ("partial_fractions", "series", "cartesian", "modulus_argument")  # VER-30 (version 2)
SERIES_ARGS = ("order", "terms", "about")


def math_simplify(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """An expression in the requested form: the O level forms, and from version 2 the A level forms (VER-30) of
    partial fractions, a Maclaurin, Taylor or binomial series, and a complex number in Cartesian or
    modulus-argument form."""
    form = args.get("form", "simplest")
    if form in A_LEVEL_FORMS:
        return _simplify_a_level(form, args, context)
    stray = sorted({*SERIES_ARGS, "variable"} & args.keys())
    if stray:
        raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(stray)} apply only to the A level forms")
    tree = parse_arg(args["expression"], args, "expression").tree
    if isinstance(tree, (Relational, sp.And)):
        raise WorkerError(Code.DOMAIN_ERROR, "simplify takes an expression, not an equation")
    try:
        with time_slice(slice_seconds(context)):
            result = FORMS[form](tree)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    return (
        {"status": "ok", "form": form, "result": exact(result)},
        {"latex": sp.latex(result), "method": "symbolic", "confidence": "proved"},
    )


# --- A level algebra (VER-30): partial fractions, series, complex numbers ------------------------------------

UNDECIDED: Extras = {"method": "heuristic", "confidence": "undecided"}
LETTERS = "ABCDFGHJKLMNPQRSTUVW"  # the identity's unknown constants; E and I would read as e and the imaginary unit
MAX_DENOMINATOR_DEGREE = 8
MAX_SERIES_POWER = 20


class _Line:
    """Working lines, each carrying the call's check id."""

    def __init__(self, context: CallContext) -> None:
        self.check = context.check_id
        self.lines: list[dict[str, Any]] = []

    def __call__(self, label: str, value: sp.Basic | str, latex: str | None = None) -> None:
        entry: dict[str, Any] = {"label": label, "value": value if isinstance(value, str) else sp.sstr(value)}
        if latex is not None:
            entry["latex"] = latex
        self.lines.append({**entry, "check_id": self.check})


def _simplify_a_level(form: str, args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    complex_form = form in ("cartesian", "modulus_argument")
    tree = parse_arg(args["expression"], args, "expression", imaginary_unit=complex_form).tree
    if isinstance(tree, (Relational, sp.And)):
        raise WorkerError(Code.DOMAIN_ERROR, "simplify takes an expression, not an equation")
    stray = sorted(set(SERIES_ARGS) & args.keys()) if form != "series" else []
    if complex_form and "variable" in args:
        stray.append("variable")
    if stray:
        raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(stray)} do not apply to form {form}")
    working = _Line(context)
    try:
        with time_slice(slice_seconds(context)):
            if form == "partial_fractions":
                value = _partial_fractions(tree, _one_variable(tree, args, form), working)
            elif form == "series":
                value = _series(tree, _one_variable(tree, args, form), args, working)
            else:
                parts = _complex_parts(tree)
                if parts is None:
                    return {"status": "unknown", "reason": "no_closed_form"}, UNDECIDED
                value = (_cartesian if form == "cartesian" else _modulus_argument)(*parts, working)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNDECIDED
    if value.get("status") == "unknown":
        return value, UNDECIDED
    main = value.get("result") or value.get("exponential") or {}
    extras: Extras = {"latex": main.get("latex", ""), "working": working.lines, "method": "symbolic"}
    return value, {**extras, "confidence": "proved"}


def _one_variable(tree: sp.Basic, args: dict[str, Any], form: str) -> sp.Symbol:
    symbols = {s.name: s for s in tree.free_symbols}
    name = args.get("variable") or ("x" if "x" in symbols or len(symbols) != 1 else next(iter(symbols)))
    if set(symbols) - {name}:
        found = ", ".join(sorted(symbols))
        raise WorkerError(Code.DOMAIN_ERROR, f"{form} takes an expression in one variable {name}; found {found}")
    return symbols.get(name) or sp.Symbol(name, real=True)


def _number_arg(raw: Any, field: str) -> sp.Rational:
    from tool_worker.functions.calculus import number  # calculus builds on this module

    return number(raw, field)


# partial fractions


def _partial_fractions(tree: sp.Expr, var: sp.Symbol, line: _Line) -> dict[str, Any]:
    """The decomposition over the rationals (proper or improper; repeated and irreducible quadratic factors), as
    SymPy's `apart` gives it, with the identity in unknown constants and their values as working."""
    expr = sp.cancel(sp.together(tree))
    if not expr.is_rational_function(var):
        raise WorkerError(Code.DOMAIN_ERROR, f"partial fractions need a polynomial over a polynomial in {var.name}")
    numerator, denominator = sp.fraction(expr)
    if not denominator.has(var):
        raise WorkerError(Code.DOMAIN_ERROR, f"the expression has no denominator in {var.name} to split")
    num, den = sp.Poly(numerator, var), sp.Poly(denominator, var)
    if not all(c.is_Rational for c in [*num.all_coeffs(), *den.all_coeffs()]):
        raise WorkerError(Code.DOMAIN_ERROR, "partial fractions take rational coefficients")
    if den.degree() > MAX_DENOMINATOR_DEGREE:
        raise WorkerError(Code.DOMAIN_ERROR, f"the denominator's degree is over {MAX_DENOMINATOR_DEGREE}")
    quotient, remainder = sp.div(num, den)
    lead, raw_factors = den.factor_list()
    factors = sorted(((f.as_expr(), k) for f, k in raw_factors), key=lambda fk: (sp.degree(fk[0], var), sp.sstr(fk[0])))
    letters = iter(c for c in LETTERS if c != var.name)
    unknowns: list[sp.Symbol] = []
    parts: list[tuple[sp.Expr, int, sp.Expr]] = []
    for factor, multiplicity in factors:
        degree = sp.degree(factor, var)
        for power in range(1, multiplicity + 1):
            constants = [sp.Symbol(next(letters)) for _ in range(degree)]
            unknowns += constants
            parts.append(
                (factor, power, sum((c * var ** (degree - 1 - j) for j, c in enumerate(constants)), sp.S.Zero))
            )
    product = [f**k for f, k in factors]
    template = sp.Add(*(n / f**p for f, p, n in parts))
    proper = remainder.as_expr() / lead
    multiplied = sp.Add(
        *(n * sp.Mul(*(g ** (k - p if g == f else k) for g, k in factors)) for f, p, n in parts), evaluate=True
    )
    equations = sp.Poly(sp.expand(multiplied - proper), var).all_coeffs()
    solved = sp.linsolve(equations, unknowns)
    if not isinstance(solved, sp.FiniteSet) or len(solved.args) != 1:
        return {"status": "unknown", "reason": "no_closed_form"}
    values = dict(zip(unknowns, solved.args[0], strict=True))
    q = quotient.as_expr()
    mine = q + sp.Add(*(n.subs(values) / f**p for f, p, n in parts))
    result = sp.apart(expr, var)
    if sp.cancel(result - mine) != 0:
        return {"status": "unknown", "reason": "ambiguous"}
    identity = f"{sp.sstr(expr)} = {sp.sstr(q + template)}"
    line("denominator", sp.Mul(*([lead] if lead != 1 else []), *product, evaluate=False))
    if q != 0:
        line("quotient", q)
        line("remainder", remainder.as_expr())
    line("identity", identity, f"{sp.latex(expr)} \\equiv {sp.latex(q + template)}")
    line("numerators", f"{sp.sstr(proper)} = {sp.sstr(multiplied)}")
    for symbol in unknowns:
        line(symbol.name, values[symbol])
    line("result", result)
    value: dict[str, Any] = {
        "status": "ok",
        "form": "partial_fractions",
        "variable": var.name,
        "result": exact(result),
        "proper": q == 0,
        "terms": [{"numerator": exact(sp.expand(n.subs(values))), "factor": exact(f), "power": p} for f, p, n in parts],
        "constants": {s.name: sp.sstr(values[s]) for s in unknowns},
        "identity": {"text": identity, "latex": f"{sp.latex(expr)} \\equiv {sp.latex(q + template)}"},
    }
    if q != 0:
        value["quotient"] = exact(q)
    return value


# series


def _ascending(coefficients: list[sp.Expr], var: sp.Symbol, about: sp.Expr) -> dict[str, Any]:
    """A series in ascending powers of x - a, as A level writes it; about a != 0 the powers of (x - a) are kept whole
    (never expanded into powers of x)."""
    if about == 0:
        expr = sp.Add(*(c * var**k for k, c in enumerate(coefficients)))
        return {"text": sp.sstr(expr, order="rev-lex"), "latex": sp.latex(expr, order="rev-lex")}
    shift = sp.Symbol(f"({sp.sstr(var - about)})")
    expr = sp.Add(*(c * shift**k for k, c in enumerate(coefficients)))
    names = {shift: f"\\left({sp.latex(var - about)}\\right)"}
    return {"text": sp.sstr(expr, order="rev-lex"), "latex": sp.latex(expr, order="rev-lex", symbol_names=names)}


def _coefficients(shifted: sp.Expr, var: sp.Symbol, top: int, tree: sp.Expr, about: sp.Expr) -> list[sp.Expr]:
    """The coefficients of t^0 .. t^top in the expansion of shifted(t) about t = 0."""
    if shifted.is_polynomial(var):
        expansion = sp.expand(shifted)
    else:
        expansion = sp.series(shifted, var, 0, top + 1).removeO()
    if not expansion.is_polynomial(var):
        raise WorkerError(
            Code.DOMAIN_ERROR,
            f"{sp.sstr(tree)} has no series in powers of {sp.sstr(var - about)}: it is not differentiable there",
        )
    poly = sp.Poly(expansion, var)
    coefficients = [poly.coeff_monomial(var**k) for k in range(top + 1)]
    if any(c.has(sp.zoo, sp.nan, sp.oo, -sp.oo) for c in coefficients):
        raise WorkerError(Code.DOMAIN_ERROR, f"{sp.sstr(tree)} is undefined at {var.name} = {sp.sstr(about)}")
    return coefficients


def _series(tree: sp.Expr, var: sp.Symbol, args: dict[str, Any], line: _Line) -> dict[str, Any]:
    """The Maclaurin series (Taylor when `about` is not 0) to the highest power `order`, or its first `terms`
    non-zero terms; for k(a + bx)^n the binomial expansion, and the range of validity where it can be stated."""
    if ("order" in args) == ("terms" in args):
        raise WorkerError(Code.DOMAIN_ERROR, "give order (the highest power) or terms (non-zero terms), one of them")
    about = _number_arg(args.get("about", 0), "about")
    shifted = tree.subs(var, var + about) if about != 0 else tree
    terminates = bool(tree.is_polynomial(var))
    if "order" in args:
        top = int(args["order"])
        coefficients = _coefficients(shifted, var, top, tree, about)
    else:
        wanted = int(args["terms"])
        top = wanted - 1
        while True:
            coefficients = _coefficients(shifted, var, top, tree, about)
            nonzero = [k for k, c in enumerate(coefficients) if c != 0]
            if len(nonzero) >= wanted:
                top = nonzero[wanted - 1]
                coefficients = coefficients[: top + 1]
                break
            if terminates and top >= sp.degree(shifted, var):
                top = nonzero[-1] if nonzero else 0
                coefficients = coefficients[: top + 1]
                break
            if top >= MAX_SERIES_POWER:
                raise WorkerError(
                    Code.DOMAIN_ERROR, f"fewer than {wanted} non-zero terms up to power {MAX_SERIES_POWER}"
                )
            top = min(MAX_SERIES_POWER, top + wanted - len(nonzero))
    for k, c in enumerate(coefficients):
        line(f"f({sp.sstr(about)})" if k == 0 else f"f^({k})({sp.sstr(about)})", c * sp.factorial(k))
    value: dict[str, Any] = {
        "status": "ok",
        "form": "series",
        "variable": var.name,
        "about": exact(about),
        "order": top,
        "result": _ascending(coefficients, var, about),
        "terms": [{"power": k, "coefficient": exact(c)} for k, c in enumerate(coefficients) if c != 0],
        "terminates": terminates,
    }
    if about == 0:
        binomial = _binomial(tree, var)
        if binomial is not None:
            value["binomial"] = binomial
            line("binomial", binomial["rewritten"]["text"], binomial["rewritten"]["latex"])
        radius = _radius(sp.factor(sp.cancel(tree)) if tree.is_rational_function(var) else tree, var)
        if radius is not None:
            value["validity"] = _validity(radius, var)
            line("valid for", value["validity"]["text"], value["validity"]["latex"])
    line("series", value["result"]["text"], value["result"]["latex"])
    return value


def _binomial(tree: sp.Expr, var: sp.Symbol) -> dict[str, Any] | None:
    """k(a + bx)^n with a and b non-zero: n, a, b and the rewritten form k a^n (1 + (b/a) x)^n."""
    coefficient, power = tree.as_independent(var, as_Add=False)
    if not isinstance(power, sp.Pow) or not power.exp.is_Rational or not power.base.is_polynomial(var):
        return None
    base, n = power.base, power.exp
    if sp.degree(base, var) != 1:
        return None
    a, b = base.subs(var, 0), sp.Poly(base, var).coeff_monomial(var)
    if a == 0 or not (a.is_real and b.is_real and coefficient.is_number):
        return None
    scale = coefficient * a**n
    unit = 1 + b / a * var
    out: dict[str, Any] = {
        "n": exact(n),
        "a": exact(a),
        "b": exact(b),
        "natural": bool(n.is_integer and n >= 0),
        "rewritten": {
            "text": ("" if scale == 1 else f"{sp.sstr(scale)}*") + f"({sp.sstr(unit)})^({sp.sstr(n)})",
            "latex": ("" if scale == 1 else f"{sp.latex(scale)} ")
            + f"\\left({sp.latex(unit)}\\right)^{{{sp.latex(n)}}}",
        },
    }
    if coefficient != 1:
        out["coefficient"] = exact(coefficient)
    return out


SERIES_ENTIRE = (sp.exp, sp.sin, sp.cos, sp.sinh, sp.cosh)


def _radius(expr: sp.Expr, var: sp.Symbol) -> sp.Expr | None:
    """The radius of convergence of the Maclaurin series where it can be stated: the distance to the nearest zero of
    a base raised to a non-natural power (|bx/a| < 1 for (a + bx)^n), every x for polynomials and for exp, sin and
    cos of a polynomial; None otherwise."""
    if not expr.has(var) or expr.is_polynomial(var):
        return sp.oo
    if isinstance(expr, (sp.Add, sp.Mul)):
        radii = [_radius(a, var) for a in expr.args]
        if any(r is None for r in radii):
            return None
        return min(radii, key=lambda r: float(sp.N(r)) if r != sp.oo else float("inf"))
    if isinstance(expr, SERIES_ENTIRE) and expr.args[0].is_polynomial(var):
        return sp.oo
    if isinstance(expr, sp.Pow) and expr.exp.is_Rational and expr.base.is_polynomial(var):
        if expr.exp.is_integer and expr.exp >= 0:
            return _radius(expr.base, var)
        if expr.base.subs(var, 0) == 0:
            return None
        poly = sp.Poly(expr.base, var)
        roots = sp.roots(poly)
        if sum(roots.values()) != poly.degree():
            return None
        return min((sp.simplify(sp.Abs(r)) for r in roots), key=lambda r: float(sp.N(r)))
    return None


def _validity(radius: sp.Expr, var: sp.Symbol) -> dict[str, Any]:
    if radius == sp.oo:
        return {"all_real": True, "text": f"all real {var.name}", "latex": f"{sp.latex(var)} \\in \\mathbb{{R}}"}
    return {
        "all_real": False,
        "text": f"Abs({var.name}) < {sp.sstr(radius)}",
        "latex": f"\\left|{sp.latex(var)}\\right| < {sp.latex(radius)}",
        "radius": exact(radius),
    }


# complex numbers


def _imaginary(tree: sp.Basic) -> sp.Basic:
    """`i` read as the imaginary unit (VER-30): the grammar parses it as a letter, never simplified as real."""
    return tree.subs({s: sp.I for s in tree.free_symbols if s.name == "i"})


def _complex_parts(tree: sp.Expr) -> tuple[sp.Expr, sp.Expr] | None:
    """(a, b) with the number equal to a + bi, or None when SymPy finds no closed form."""
    z = _imaginary(tree)
    others = sorted(s.name for s in z.free_symbols)
    if others:
        raise WorkerError(Code.DOMAIN_ERROR, f"complex forms take a number written with i; found {', '.join(others)}")
    if z.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
        raise WorkerError(Code.DOMAIN_ERROR, "the expression is undefined")
    re, im = (sp.simplify(sp.radsimp(part)) for part in sp.expand_complex(z).as_real_imag())
    if re.has(sp.I, sp.re, sp.im) or im.has(sp.I, sp.re, sp.im):
        return None
    return re, im


def _complex_decimal(re: sp.Expr, im: sp.Expr, sf: int = 6) -> str:
    if im == 0:
        return significant(re, sf)
    imaginary = f"{significant(abs(im), sf)}i"
    if re == 0:
        return imaginary if im > 0 else f"-{imaginary}"
    return f"{significant(re, sf)} {'+' if im > 0 else '-'} {imaginary}"


def _complex_exact(re: sp.Expr, im: sp.Expr) -> dict[str, Any]:
    z = re + im * sp.I
    return {"text": sp.sstr(z), "latex": sp.latex(z), "decimal": _complex_decimal(re, im)}


def _cartesian(re: sp.Expr, im: sp.Expr, line: _Line) -> dict[str, Any]:
    line("real part", re, sp.latex(re))
    line("imaginary part", im, sp.latex(im))
    result = _complex_exact(re, im)
    line("a + bi", result["text"], result["latex"])
    return {"status": "ok", "form": "cartesian", "result": result, "real": exact(re), "imaginary": exact(im)}


def _modulus_argument(re: sp.Expr, im: sp.Expr, line: _Line) -> dict[str, Any]:
    """r = |z| and the principal argument in (-pi, pi], exact where SymPy has a closed form; with the polar and
    exponential forms."""
    if re == 0 and im == 0:
        raise WorkerError(Code.DOMAIN_ERROR, "0 has no argument")
    modulus = sp.simplify(sp.sqrt(sp.expand(re**2 + im**2)))
    argument = sp.simplify(sp.atan2(im, re))
    r_text = "" if modulus == 1 else f"{sp.sstr(modulus)}*"
    r_latex = "" if modulus == 1 else f"{sp.latex(modulus)} "
    theta, theta_latex = sp.sstr(argument), sp.latex(argument)
    polar = {
        "text": f"{r_text}(cos({theta}) + I*sin({theta}))",
        "latex": f"{r_latex}\\left(\\cos\\left({theta_latex}\\right) + i \\sin\\left({theta_latex}\\right)\\right)",
    }
    exponential = {
        "text": f"{r_text}exp({sp.sstr(sp.I * argument)})" if argument != 0 else sp.sstr(modulus),
        "latex": f"{r_latex}e^{{{sp.latex(sp.I * argument)}}}" if argument != 0 else sp.latex(modulus),
    }
    line("modulus", modulus, sp.latex(modulus))
    line("argument", argument, theta_latex)
    line("exponential form", exponential["text"], exponential["latex"])
    return {
        "status": "ok",
        "form": "modulus_argument",
        "cartesian": _complex_exact(re, im),
        "modulus": exact(modulus),
        "argument": exact(argument),
        "argument_degrees": exact(sp.simplify(argument * 180 / sp.pi)),
        "polar": polar,
        "exponential": exponential,
    }


# --- A level equations (VER-30): complex roots and numerical methods -----------------------------------------

NUMERICAL_ARGS = ("start", "steps", "tolerance", "g", "dp", "first_index")
MAX_ITERATES = 50
EXACT_LIMIT = 10**30  # an iterate stays an exact fraction while its numerator and denominator are at most this
DIVERGED = 10**12  # an iterate beyond this in size stops the iteration as diverging
DIGITS = 60


def _complex_key(re: sp.Expr, im: sp.Expr, text: str) -> tuple[float, float, str]:
    return (float(sp.N(re, 30)), float(sp.N(im, 30)), text)


def _solve_complex(trees: list[sp.Basic], args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    """Every complex root of one polynomial equation in one unknown, exact, with multiplicities and the conjugate
    pairs (non-real roots of a polynomial with real coefficients come in conjugate pairs)."""
    if len(trees) != 1 or not isinstance(trees[0], sp.Eq):
        raise WorkerError(Code.DOMAIN_ERROR, "domain complex takes one polynomial equation in one unknown")
    expr = _imaginary(trees[0].lhs - trees[0].rhs)
    names = list(args.get("unknowns") or sorted(s.name for s in expr.free_symbols))
    if "i" in names:
        raise WorkerError(Code.DOMAIN_ERROR, "i is the imaginary unit; call the unknown z or w")
    if len(names) != 1:
        raise WorkerError(Code.DOMAIN_ERROR, "domain complex takes one polynomial equation in one unknown")
    var = sp.Symbol(names[0])  # complex
    expr = expr.subs({s: var for s in expr.free_symbols if s.name == names[0]})
    if expr.free_symbols - {var}:
        others = ", ".join(sorted(s.name for s in expr.free_symbols - {var}))
        raise WorkerError(Code.DOMAIN_ERROR, f"give values for {others}")
    working = _Line(context)
    try:
        with time_slice(slice_seconds(context)):
            numerator, denominator = sp.fraction(sp.together(expr))
            if not (numerator.is_polynomial(var) and denominator.is_polynomial(var)):
                raise WorkerError(Code.DOMAIN_ERROR, "domain complex solves polynomial equations")
            poly = sp.Poly(sp.expand(numerator), var)
            if poly.degree() <= 0:
                case = "all_values" if poly.is_zero else "no_solution"
                return {"case": case, "domain": "complex"}, {"method": "symbolic", "confidence": "proved"}
            found = sp.roots(poly)
            if sum(found.values()) != poly.degree():
                return {"status": "unknown", "reason": "no_closed_form"}, UNDECIDED
            roots = []
            for root, multiplicity in found.items():
                if denominator.subs(var, root) == 0:
                    continue
                re, im = (sp.simplify(sp.radsimp(p)) for p in sp.expand_complex(root).as_real_imag())
                text = sp.sstr(re + im * sp.I)
                roots.append((_complex_key(re, im, text), re, im, multiplicity))
            roots.sort(key=lambda r: r[0])
            real_coefficients = all(c.is_real for c in poly.all_coeffs())
            working("polynomial", poly.as_expr())
            if poly.degree() == 2:
                a, b, c = poly.all_coeffs()
                working("discriminant", sp.expand(b**2 - 4 * a * c))
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNDECIDED
    if not roots:
        return {"case": "no_solution", "domain": "complex"}, {"method": "symbolic", "confidence": "proved"}
    solutions = []
    for n, (_, re, im, multiplicity) in enumerate(roots):
        entry = _complex_exact(re, im)
        if multiplicity > 1:
            entry["multiplicity"] = multiplicity
        solutions.append({var.name: entry})
        working(f"{var.name}_{n + 1}", entry["text"], entry["latex"])
    pairs = [
        [j, k]
        for j in range(len(roots))
        for k in range(j + 1, len(roots))
        if roots[j][2] != 0 and sp.expand(roots[j][1] - roots[k][1]) == 0 and sp.expand(roots[j][2] + roots[k][2]) == 0
    ]
    value = {
        "case": "solutions",
        "domain": "complex",
        "solutions": solutions,
        "conjugate_pairs": pairs,
        "real_coefficients": real_coefficients,
    }
    return value, {"working": working.lines, "method": "symbolic", "confidence": "proved"}


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _at(expr: sp.Expr, var: sp.Symbol, x: sp.Expr) -> sp.Expr:
    """expr at x: an exact fraction while x and the result are small fractions, else a 60-digit float. Raises _Stop
    when expr is undefined or not real there. A power too large to compute exactly (2^x at x = 10^29) is only
    evaluated as a float: the exact power runs in C, where the per-call budget cannot stop it (SEC-08)."""
    found = None
    if isinstance(x, sp.Rational):
        try:
            found = bounded_subs(expr, {var: x})
        except TooLarge:
            pass
    if found is not None:
        if found.has(sp.zoo, sp.nan):
            raise _Stop("undefined")
        if found.is_Rational:
            return found if max(abs(found.p), found.q) <= EXACT_LIMIT else sp.Float(found, DIGITS)
    out = expr.evalf(DIGITS, subs={var: x})
    if not (out.is_Number and out.is_finite and out.is_extended_real):
        raise _Stop("undefined")
    return sp.Float(out, DIGITS)


def _fixed(value: sp.Expr, dp: int) -> str:
    """A fixed-point decimal to `dp` places, half away from zero (VER-09): an exact fraction by integer arithmetic,
    a float from all its digits; the sign of a negative value is kept even when it rounds to 0."""
    if isinstance(value, sp.Rational):
        whole, rest = divmod(abs(value.p) * 10**dp, value.q)
        digits = str(whole + (2 * rest >= value.q)).rjust(dp + 1, "0")
        return ("-" if value.p < 0 else "") + (f"{digits[:-dp]}.{digits[-dp:]}" if dp else digits)
    return format(Decimal(str(value)).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP), "f")


def _iterate_entry(n: int, x: sp.Expr, dp: int) -> dict[str, Any]:
    shown = significant(x, 6) if abs(x) > DIVERGED else _fixed(x, dp)
    out: dict[str, Any] = {"n": n, "x": {"decimal": shown}}
    if isinstance(x, sp.Rational):
        out["x"]["text"] = sp.sstr(x)
    return out


def _run(step: Any, start: sp.Expr, args: dict[str, Any]) -> tuple[list[sp.Expr], str, int | None]:
    """x0, x1, ... until `steps` iterates, |x(n+1) - x(n)| < tolerance, divergence, or a failure in `step`."""
    steps = args.get("steps")
    tolerance = _number_arg(args["tolerance"], "tolerance") if "tolerance" in args else None
    if steps is None and tolerance is None:
        raise WorkerError(Code.DOMAIN_ERROR, "give steps (how many iterates) or tolerance (when to stop)")
    if tolerance is not None and tolerance <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "tolerance must be positive")
    limit = int(steps) if steps is not None else MAX_ITERATES
    xs = [start]
    while True:
        if len(xs) - 1 >= limit:
            return xs, ("steps" if steps is not None else "max_steps"), None
        try:
            following = step(xs[-1])
        except _Stop as stop:
            return xs, stop.reason, len(xs) - 1
        xs.append(following)
        if abs(following) > DIVERGED:
            return xs, "diverged", None
        if tolerance is not None and abs(following - xs[-2]) < tolerance:
            return xs, "tolerance", None


def _real_at(expr: sp.Expr, var: sp.Symbol, at: sp.Float) -> sp.Float | None:
    out = expr.evalf(50, subs={var: at})
    return out if out.is_Number and out.is_finite and out.is_extended_real else None


def _refine(h: sp.Expr, var: sp.Symbol, guesses: list[sp.Expr]) -> sp.Float | None:
    """A root of h by Newton's method at 50 digits (no compiled code), from the first guess that reaches one; a step
    that leaves the real domain of h is halved until it stays inside. None when no guess reaches a root."""
    derivative = sp.diff(h, var)
    for guess in guesses:
        x = sp.Float(guess, 50)
        hx = _real_at(h, var, x)
        for _ in range(100):
            dx = _real_at(derivative, var, x)
            if hx is None or dx is None or dx == 0:
                break
            change = hx / dx
            for _ in range(60):
                candidate = sp.Float(x - change, 50)
                h_candidate = _real_at(h, var, candidate)
                if h_candidate is not None:
                    break
                change = change / 2
            else:
                break
            x, hx = candidate, h_candidate
            if abs(change) < sp.Float("1e-40") * max(1, abs(x)):
                return x
    return None


def _numeric_unknown(f: sp.Expr, args: dict[str, Any]) -> sp.Symbol:
    names = args.get("unknowns") or sorted(s.name for s in f.free_symbols)
    if len(names) != 1:
        raise WorkerError(Code.DOMAIN_ERROR, "the numerical methods take one equation in one unknown")
    symbols = {s.name: s for s in f.free_symbols}
    var = symbols.get(names[0]) or sp.Symbol(names[0], real=True)
    if f.free_symbols - {var}:
        others = ", ".join(sorted(s.name for s in f.free_symbols - {var}))
        raise WorkerError(Code.DOMAIN_ERROR, f"give values for {others}")
    return var


def _numerical(trees: list[sp.Basic], args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    if len(trees) != 1 or not isinstance(trees[0], sp.Eq):
        raise WorkerError(Code.DOMAIN_ERROR, "the numerical methods take one equation in one unknown")
    f = trees[0].lhs - trees[0].rhs
    var = _numeric_unknown(f, args)
    method = args["method"]
    if method == "sign_change":
        extra = sorted({"start", "steps", "tolerance", "g", "first_index"} & args.keys())
        if extra:
            raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(extra)} do not apply to the sign-change check")
    else:
        if "interval" in args:
            raise WorkerError(Code.DOMAIN_ERROR, "interval applies to the sign-change check; give start")
        if "start" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, f"{method} needs start (the first value)")
        if method == "iteration" and "g" not in args:
            raise WorkerError(Code.DOMAIN_ERROR, "iteration needs g, the rearrangement x = g(x)")
        if method == "newton_raphson" and "g" in args:
            raise WorkerError(Code.DOMAIN_ERROR, "g applies to iteration; Newton-Raphson uses the equation itself")
    working = _Line(context)
    try:
        with time_slice(slice_seconds(context)):
            if method == "sign_change":
                return _sign_change(f, var, args, working)
            if method == "newton_raphson":
                return _newton_raphson(f, var, args, working)
            return _fixed_point(f, var, args, working)
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, UNDECIDED


def _label(var: sp.Symbol, n: int) -> str:
    return f"{var.name}_{n}"


def _newton_raphson(f: sp.Expr, var: sp.Symbol, args: dict[str, Any], line: _Line) -> tuple[dict[str, Any], Extras]:
    """x(n+1) = x(n) - f(x(n))/f'(x(n)), every iterate reported; stops when f'(x(n)) = 0."""
    derivative = sp.diff(f, var)
    dp, first = int(args.get("dp", 6)), int(args.get("first_index", 0))
    line("f'", derivative, sp.latex(derivative))
    real_f, real_derivative = _real_roots(f), _real_roots(derivative)

    def step(x: sp.Expr) -> sp.Expr:
        fx, dx = _at(real_f, var, x), _at(real_derivative, var, x)
        n = first + len(xs_seen) - 1
        line(f"f({_label(var, n)})", significant(fx, 10))
        line(f"f'({_label(var, n)})", significant(dx, 10))
        if dx == 0 or (isinstance(dx, sp.Float) and abs(dx) < sp.Float("1e-45")):
            raise _Stop("derivative_zero")
        following = x - fx / dx
        if isinstance(following, sp.Rational):
            if max(abs(following.p), following.q) > EXACT_LIMIT:
                following = sp.Float(following, DIGITS)
        else:
            following = sp.Float(following, DIGITS)
        xs_seen.append(following)
        return following

    start = _number_arg(args["start"], "start")
    xs_seen = [start]
    xs, stopped, at = _run(step, start, args)
    value = _iterated("newton_raphson", var, xs, stopped, at, dp, first, line)
    value["derivative"] = exact(derivative)
    return value, {"working": line.lines, "method": "rule_based", "confidence": "proved"}


def _real_roots(expr: sp.Expr) -> sp.Expr:
    """Odd roots taken as real (the calculator's cube root of a negative number), not SymPy's principal root:
    b^(p/q) with q odd is sign(b)^p |b|^(p/q)."""
    return expr.replace(
        lambda e: isinstance(e, sp.Pow) and e.exp.is_Rational and not e.exp.is_integer and e.exp.q % 2 == 1,
        lambda e: sp.sign(e.base) ** e.exp.p * sp.Abs(e.base) ** e.exp,
    )


def _fixed_point(f: sp.Expr, var: sp.Symbol, args: dict[str, Any], line: _Line) -> tuple[dict[str, Any], Extras]:
    """x(n+1) = g(x(n)); convergence from |g'(root)| < 1 at the root of x = g(x) near the iterates, with the
    staircase (g' > 0) or cobweb (g' < 0) diagram."""
    g_tree = parse_arg(args["g"], args, "g").tree
    if isinstance(g_tree, sp.Eq) and isinstance(g_tree.lhs, sp.Symbol) and g_tree.lhs.name == var.name:
        g_tree = g_tree.rhs  # "x = g(x)" written in full
    if isinstance(g_tree, (Relational, sp.And)):
        raise WorkerError(Code.DOMAIN_ERROR, "g: give the rearrangement's right-hand side g(x)")
    g = g_tree.subs({s: var for s in g_tree.free_symbols if s.name == var.name})
    if g.free_symbols - {var}:
        others = ", ".join(sorted(s.name for s in g.free_symbols - {var}))
        raise WorkerError(Code.DOMAIN_ERROR, f"g: give values for {others}")
    dp, first = int(args.get("dp", 6)), int(args.get("first_index", 0))
    start = _number_arg(args["start"], "start")
    real_g = _real_roots(g)
    xs, stopped, at = _run(lambda x: _at(real_g, var, x), start, args)
    value = _iterated("iteration", var, xs, stopped, at, dp, first, line)
    value["g"] = exact(g)
    guesses = [xs[-1], xs[0]] if stopped == "tolerance" else [xs[0], *(v for v in xs[1:] if abs(v) <= DIVERGED)]
    root = _refine(var - real_g, var, guesses)
    if root is None:
        value["convergence"] = {"status": "unknown", "reason": "no_fixed_point"}
        return value, {"working": line.lines, "method": "rule_based", "confidence": "proved"}
    slope = sp.diff(real_g, var).evalf(30, subs={var: root})
    residual = f.evalf(30, subs={var: root})
    if not (slope.is_Number and slope.is_extended_real):
        value["convergence"] = {"status": "unknown", "reason": "no_fixed_point"}
        return value, {"working": line.lines, "method": "rule_based", "confidence": "proved"}
    size = abs(slope)
    converges = None if abs(size - 1) < sp.Float("1e-20") else bool(size < 1)
    value["convergence"] = {
        "status": "ok",
        "root": {"decimal": _fixed(root, dp), "text": significant(root, 15)},
        "g_prime": {"decimal": significant(slope, 6)},
        "converges": converges,
        "diagram": "staircase" if slope >= 0 else "cobweb",
        "rearrangement_matches": bool(residual.is_Number and abs(residual) < sp.Float("1e-20")),
    }
    line("g'(root)", significant(slope, 6))
    return value, {"working": line.lines, "method": "rule_based", "confidence": "proved"}


def _iterated(
    method: str, var: sp.Symbol, xs: list[sp.Expr], stopped: str, at: int | None, dp: int, first: int, line: _Line
) -> dict[str, Any]:
    iterates = [_iterate_entry(first + n, x, dp) for n, x in enumerate(xs)]
    for entry in iterates:
        line(_label(var, entry["n"]), entry["x"]["decimal"])
    value: dict[str, Any] = {
        "status": "ok",
        "method": method,
        "variable": var.name,
        "dp": dp,
        "iterates": iterates,
        "stopped": stopped,
    }
    if at is not None:
        value["at"] = first + at
    if stopped == "tolerance":
        value["root"] = {"decimal": _fixed(xs[-1], dp)}
    return value


def _sign_change(f: sp.Expr, var: sp.Symbol, args: dict[str, Any], line: _Line) -> tuple[dict[str, Any], Extras]:
    """f(a) and f(b) and whether they differ in sign; a root is located in (a, b) only when f is also continuous on
    [a, b]. The midpoint is then within (b - a)/2 of it."""
    if "interval" not in args:
        raise WorkerError(Code.DOMAIN_ERROR, "the sign-change check needs interval [a, b]")
    lo, hi = (_number_arg(v, "interval") for v in args["interval"])
    if not lo < hi:
        raise WorkerError(Code.DOMAIN_ERROR, "interval: the lower end must be below the upper end")
    ends, signs = [], []
    for end in (lo, hi):
        at = substituted(f, {var: end}, "interval")
        if at.has(sp.zoo, sp.nan, sp.oo, -sp.oo) or at.is_real is False:
            raise WorkerError(Code.DOMAIN_ERROR, f"the function is undefined at {var.name} = {sp.sstr(end)}")
        at = sp.simplify(at)
        numeric = sp.N(at, 30)
        if at == 0:
            signs.append(0)
        elif not numeric.is_real or abs(numeric) < sp.Float("1e-25"):
            return {"status": "unknown", "reason": "ambiguous"}, UNDECIDED
        else:
            signs.append(1 if numeric > 0 else -1)
        ends.append(at)
        line(f"f({sp.sstr(end)})", at, sp.latex(at))
    continuous = continuous_domain(f, var, sp.Interval(lo, hi)) == sp.Interval(lo, hi)
    change = signs[0] * signs[1] < 0
    value: dict[str, Any] = {
        "status": "ok",
        "method": "sign_change",
        "variable": var.name,
        "interval": [exact(lo), exact(hi)],
        "f_lower": exact(ends[0]),
        "f_upper": exact(ends[1]),
        "sign_change": change,
        "continuous": continuous,
        "root_located": change and continuous,
    }
    zeros = [end for end, sign in zip((lo, hi), signs, strict=True) if sign == 0]
    if zeros:
        value["root_at_end"] = exact(zeros[0])
    if change and continuous:
        value["estimate"] = exact((lo + hi) / 2)
        value["error_bound"] = exact((hi - lo) / 2)
        line("root", f"{sp.sstr((lo + hi) / 2)} ± {sp.sstr((hi - lo) / 2)}")
    return value, {"working": line.lines, "method": "exact", "confidence": "proved"}


# --- evaluate (VER-09 subset, VER-05 angles) ---------------------------------------------------------------

TRIG = (sp.sin, sp.cos, sp.tan, sp.sec, sp.csc, sp.cot)
INVERSE_TRIG = (sp.asin, sp.acos, sp.atan)


def _in_degrees(tree: sp.Basic) -> sp.Basic:
    """Trig functions take degrees; inverse trig functions answer in degrees."""

    def convert(node: sp.Basic) -> sp.Basic:
        if not node.args:
            return node
        args = [convert(a) for a in node.args]
        if isinstance(node, TRIG):
            return node.func(args[0] * sp.pi / 180)
        if isinstance(node, INVERSE_TRIG):
            return node.func(args[0]) * 180 / sp.pi
        return node.func(*args)

    return convert(tree)


def math_evaluate(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    tree = parse_arg(args["expression"], args, "expression").tree
    warnings = []
    level = args.get("level", "O")
    angle_unit = args.get("angle_unit")
    if angle_unit is None and tree.has(*TRIG, *INVERSE_TRIG):
        angle_unit = "degrees" if level == "O" else "radians"
        warnings.append({"code": "assumption_added", "message": f"angles are taken in {angle_unit}"})
    if angle_unit == "degrees":
        tree = _in_degrees(tree)
    given = sorted(set(args.get("values", {})) & {c["symbol"] for c in args.get("constants", [])})
    if given:
        raise WorkerError(Code.DOMAIN_ERROR, f"give {', '.join(given)} once: in values or in constants")
    try:
        [tree], constant_extras = constants.apply([tree], args)
    except constants.ConventionMissing as no_convention:
        return no_convention.result()
    warnings += constant_extras.pop("warnings", [])
    symbols = {s.name: s for s in tree.free_symbols}
    values = {}
    for name, raw in args.get("values", {}).items():
        if name not in symbols:
            continue
        values[symbols[name]] = exact_number(str(raw), 0) if isinstance(raw, (int, float, str)) else raw
    missing = sorted(set(symbols) - {s.name for s in values})
    if missing:
        raise WorkerError(Code.DOMAIN_ERROR, f"no value given for {', '.join(missing)}")
    try:
        with time_slice(slice_seconds(context)):
            # Parsed numbers and supplied constants are already exact. Searching
            # numerically for another closed form adds no evidence and can spend
            # the whole slice on ordinary non-special-angle trigonometry.
            result = sp.simplify(bounded_subs(tree, values))
    except TooLarge as error:
        raise WorkerError(Code.LIMIT_EXCEEDED, f"expression: {error}", limit=error.limit) from None
    except SliceExpired:
        return {"status": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    if result.has(sp.zoo, sp.nan) or result.is_real is False:
        raise WorkerError(Code.DOMAIN_ERROR, "the expression is undefined for these values")
    value: dict[str, Any] = {"status": "ok", "exact": exact(result)}
    if "sf" in args:
        value["rounded"] = significant(result, int(args["sf"]))
    elif "dp" in args:
        value["rounded"] = decimal_places(result, int(args["dp"]))
    extras: Extras = {"latex": sp.latex(result), "method": "exact", "confidence": "proved", **constant_extras}
    if warnings:
        extras["warnings"] = warnings
    return value, extras
