"""Quantities with units inside equations (VER-17 with VER-11 and VER-12), and given numbers as exact values.

A motion problem is solved by `math_solve` with its knowns given as quantities ("72 km/h"), each converted exactly to
SI base units before solving, and each unknown asked for in a stated unit. Before anything is substituted, every
equation is checked for dimensional consistency: both sides, and every pair of added terms, must have the same
dimension, otherwise the call is a `DOMAIN_ERROR` naming both dimensions (for example "found m s⁻¹, expected m").
An unknown is solved in its stated unit by scaling its symbol, so a speed asked for in km/h comes back in km/h."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.functions import constants, units
from tool_worker.functions.common import substituted
from tool_worker.grammar import TooLarge, exact_number
from tool_worker.protocol import Code, WorkerError

Dims = dict[str, Fraction]
BASE_UNITS = {"[length]": "meter", "[time]": "second", "[mass]": "kilogram", "[current]": "ampere",
              "[temperature]": "kelvin", "[substance]": "mole", "[luminosity]": "candela"}  # fmt: skip


def number(raw: Any, field: str) -> sp.Rational:
    """A given number (JSON number or decimal string) as an exact rational; decimals stay exact (VER-03)."""
    text = repr(raw) if isinstance(raw, float) else str(raw)
    try:
        value = exact_number(text.strip().lstrip("+"), 0)
    except TooLarge as error:
        raise WorkerError(Code.LIMIT_EXCEEDED, f"{field}: {error}", limit=error.limit) from None
    except ValueError:
        raise WorkerError(Code.PARSE_REJECTED, f"{field}: not a number", position=0) from None
    return sp.Rational(value)


def _dims_of(quantity: Any) -> Dims:
    return {str(k): Fraction(v) for k, v in dict(quantity.dimensionality).items() if v}


def dims_text(dims: Dims) -> str:
    """A dimension in SI base units, for example 'm s⁻¹' ('1' when dimensionless)."""
    quantity = units.REGISTRY.Quantity(1, "dimensionless")
    for name, power in sorted(dims.items()):
        quantity = quantity * units.REGISTRY.Quantity(1, BASE_UNITS.get(name, "dimensionless")) ** power
    return units.dimension_text(quantity)


def _names(unit: Any) -> dict[str, Any]:
    """A unit's named parts and powers (Pint's own string form cannot format fractional powers)."""
    return dict(unit._units)


def _offset(unit: Any) -> bool:
    return bool({"degree_Celsius", "degree_Fahrenheit"} & set(_names(unit)))


def _si_value(text: str) -> tuple[sp.Basic, Dims]:
    """A quantity's magnitude in SI base units, exactly; angles in degrees become exact multiples of pi."""
    quantity = units.parse_quantity(text)
    if _offset(quantity.units):
        raise WorkerError(Code.DOMAIN_ERROR, f"{text!r}: give temperatures in an equation in K")
    magnitude = sp.Rational(quantity.magnitude.numerator, quantity.magnitude.denominator)
    if _names(quantity.units) == {"degree": 1}:
        return magnitude * sp.pi / 180, {}
    base = quantity.to_base_units().magnitude
    return sp.Rational(Fraction(base).numerator, Fraction(base).denominator), _dims_of(quantity)


def unit_scale(text: str) -> tuple[sp.Rational, Dims]:
    """How many SI base units one stated unit is, and its dimension."""
    unit = units.parse_unit(text)
    if _offset(unit):
        raise WorkerError(Code.DOMAIN_ERROR, f"{text!r}: ask for temperatures in an equation in K")
    one = units.REGISTRY.Quantity(1, unit)
    base = Fraction(one.to_base_units().magnitude)
    return sp.Rational(base.numerator, base.denominator), _dims_of(one)


def _add(a: Dims, b: Dims, times: Fraction = Fraction(1)) -> Dims:
    out = dict(a)
    for name, power in b.items():
        out[name] = out.get(name, Fraction(0)) + power * times
    return {k: v for k, v in out.items() if v}


def dimension(expr: sp.Basic, known: dict[str, Dims]) -> Dims | None:
    """The dimension of an expression; None for a literal zero, which fits any dimension."""
    if expr.is_number:
        return None if expr == 0 else {}
    if isinstance(expr, sp.Symbol):
        return known[expr.name]
    if isinstance(expr, sp.Mul):
        total: Dims = {}
        for factor in expr.args:
            found = dimension(factor, known)
            if found is None:
                return None
            total = _add(total, found)
        return total
    if isinstance(expr, sp.Pow):
        base, exponent = expr.args
        found = dimension(base, known)
        if exponent.is_Rational:
            return None if found is None else _add({}, found, Fraction(int(exponent.p), int(exponent.q)))
        if found or dimension(exponent, known):
            raise WorkerError(Code.DOMAIN_ERROR, "a power with a variable exponent needs dimensionless quantities")
        return {}
    if isinstance(expr, sp.Add):
        terms = [d for d in (dimension(term, known) for term in expr.args) if d is not None]
        for term in terms[1:]:
            if term != terms[0]:
                raise WorkerError(Code.DOMAIN_ERROR, f"terms of different dimensions are added: found "
                                  f"{dims_text(term)}, expected {dims_text(terms[0])}")  # fmt: skip
        return terms[0] if terms else None
    for argument in expr.args:  # functions (sin, log, sqrt as a power above, abs): dimensionless arguments
        found = dimension(argument, known)
        if found:
            name = getattr(expr.func, "__name__", "a function")
            raise WorkerError(
                Code.DOMAIN_ERROR, f"the argument of {name} must be dimensionless, found {dims_text(found)}"
            )
    return {}


def apply(trees: list[sp.Basic], args: dict[str, Any]) -> tuple[list[sp.Basic], dict[str, str]]:
    """Check the equations' dimensions, substitute the quantities in SI and scale each unknown to its stated unit.

    Returns the trees and the unit of each unknown. Without `quantities` and `units` the trees pass through."""
    given: dict[str, str] = args.get("quantities") or {}
    wanted: dict[str, str] = args.get("units") or {}
    if not given and not wanted:
        return trees, {}
    unknowns = set(args.get("unknowns") or [])
    chosen = constants.resolve(args.get("constants", []))
    for label, clash in (
        ("a quantity and an unknown", set(given) & unknowns),
        ("a quantity and a constant", set(given) & set(chosen)),
        ("a quantity and a unit to solve for", set(given) & set(wanted)),
    ):
        if clash:
            raise WorkerError(
                Code.DOMAIN_ERROR,
                f"{', '.join(sorted(clash))} is given as {label}; quantities must "
                "contain only known numeric values with units, such as u: '12 m/s'; put unknown "
                "symbols only in unknowns and their requested units in units, such as s: 'm'",
            )
    known: dict[str, Dims] = {}
    values: dict[str, sp.Basic] = {}
    scales: dict[str, sp.Rational] = {}
    for name, text in given.items():
        values[name], known[name] = _si_value(text)
    for name, text in wanted.items():
        scales[name], known[name] = unit_scale(text)
    for symbol, entry in chosen.items():
        if entry.get("unit"):
            scale, known[symbol] = unit_scale(str(entry["unit"]))
            if scale != 1:  # constants are substituted as written, so their unit must be coherent SI
                raise WorkerError(Code.DOMAIN_ERROR, f"give the constant {symbol} in SI units when quantities are used")
    present = sorted({s.name for t in trees for s in t.free_symbols})
    missing = [name for name in present if name not in known]
    if missing:
        raise WorkerError(Code.DOMAIN_ERROR, f"no unit for {', '.join(missing)}: give it as a quantity, a constant "
                          "with its unit, or an unknown with the unit to solve in")  # fmt: skip
    for tree in trees:
        if isinstance(tree, sp.Eq):
            left, right = dimension(tree.lhs, known), dimension(tree.rhs, known)
            if left is not None and right is not None and left != right:
                raise WorkerError(Code.DOMAIN_ERROR, f"the two sides differ in dimension: found {dims_text(right)}, "
                                  f"expected {dims_text(left)}")  # fmt: skip
    substitutions: dict[sp.Basic, sp.Basic] = {}
    for tree in trees:
        for symbol in tree.free_symbols:
            if symbol.name in values:
                substitutions[symbol] = values[symbol.name]
            elif symbol.name in scales:
                substitutions[symbol] = scales[symbol.name] * symbol
    stated = {name: units.unit_text(units.parse_unit(text)) for name, text in wanted.items()}
    return [substituted(t, substitutions, "quantities") for t in trees], stated  # simultaneous, bounded (SEC-08)
