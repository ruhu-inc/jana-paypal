"""Units and dimensions (VER-11): one Pint registry built at worker start from the pinned Pint definitions plus our
school-unit additions, a unit grammar with fixed rules for ambiguous tokens, exact magnitudes, and temperature
differences kept apart from absolute temperatures."""

from __future__ import annotations

import math
import re
from fractions import Fraction
from tokenize import TokenError
from typing import Any

import pint
import sympy as sp

from tool_worker.functions.maths import significant
from tool_worker.grammar import TooLarge, exact_number
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]

# School units on top of Pint's own definitions (dm³, cm³, M = mol dm⁻³, kWh, eV, °C, mmHg, atm, u, km/h are
# defined by Pint; these make the school spellings explicit).
ADDITIONS = (
    "kWh = kilowatt * hour",
    "mol_per_dm3 = mole / decimeter ** 3",
)
REGISTRY = pint.UnitRegistry(autoconvert_offset_to_baseunit=False, non_int_type=Fraction)
for definition in ADDITIONS:
    REGISTRY.define(definition)

SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_NUMBER = re.compile(r"^\s*([-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)(?:\s*[×x]\s*10\^?\(?([-+]?\d+)\)?)?\s*")
BASE_SYMBOLS = {"meter": "m", "second": "s", "kilogram": "kg", "gram": "g", "ampere": "A", "kelvin": "K",
                "mole": "mol", "candela": "cd", "radian": "rad", "bit": "bit", "count": ""}  # fmt: skip
SUPERSCRIPT_DIGITS = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


def normalise_unit(text: str) -> str:
    """Our unit grammar onto Pint's: superscripts, products, °C, and the fixed readings of C and M."""
    unit = text.strip().translate(SUPERSCRIPT)
    unit = unit.replace("·", " ").replace("⋅", " ").replace("°C", "degC").replace("°F", "degF").replace("°", "degree")
    unit = re.sub(r"([A-Za-z])(-?\d+)\b", r"\1^\2", unit)  # m3, s-2 -> m^3, s^-2
    unit = re.sub(r"\bM\b", "molar", unit)  # M alone is molar
    unit = re.sub(r"\bC\b", "coulomb", unit)  # C is the coulomb; Celsius needs °C
    unit = re.sub(r"\bh\b", "hour", unit)
    return unit


# Pint's unit parser tokenises the text as Python and evaluates the token tree, so malformed text ("x+6", "5 (s")
# surfaces as any of these rather than as a Pint error; every one is a rejected unit (G102).
_MALFORMED = (pint.errors.PintError, TokenError, SyntaxError, TypeError, ValueError, AttributeError, KeyError,
              IndexError, AssertionError, ZeroDivisionError, OverflowError)  # fmt: skip
MAX_TEN_POWER = 300  # as the grammar's bound on a written exponent
MAX_UNIT_POWER = 12  # no school unit has a larger power; larger ones are typing errors (m^99999999999)


_POWER = re.compile(r"\s*(?:\^|\*\*)")
_EXPONENT = re.compile(r"\s*(?:[-+]?\s*\d{1,2}(?:\.\d{1,3})?(?![\d.])|\(\s*[-+]?\s*\d{1,2}(?:\s*/\s*\d{1,2})?\s*\))")


def _powers_bounded(unit: str) -> bool:
    """Every power in the unit text is a short number (2, -3, 0.5, (1/2)) raising a unit or a group with no power in
    it. Pint evaluates unit text as Python arithmetic, so m^9^9^9 computes 9^387420489, and ((9^99)^99)^99 a power as
    large, in C before any check on the parsed unit can run and where the per-call budget cannot stop it (SEC-08)."""
    groups = [False]  # for each open parenthesis: whether its group holds a power
    after_powered_group = False
    i = 0
    while i < len(unit):
        power = _POWER.match(unit, i)
        if power:
            exponent = _EXPONENT.match(unit, power.end())
            if exponent is None or after_powered_group or _POWER.match(unit, exponent.end()):
                return False
            groups[-1] = True
            i = exponent.end()
            continue
        char = unit[i]
        if char == "(":
            groups.append(False)
        elif char == ")" and len(groups) > 1:
            closed = groups.pop()
            groups[-1] = groups[-1] or closed
            after_powered_group = closed
            i += 1
            continue
        if not char.isspace():
            after_powered_group = False
        i += 1
    return True


def parse_unit(text: str) -> Any:
    if not _powers_bounded(normalise_unit(text)):
        raise WorkerError(Code.PARSE_REJECTED, f"unit {text!r} has a power beyond {MAX_UNIT_POWER}", position=0)
    try:
        unit = REGISTRY.parse_units(normalise_unit(text))
        powers = [Fraction(str(power)) for power in unit._units.values()]
    except _MALFORMED as error:
        raise WorkerError(Code.PARSE_REJECTED, f"unknown unit {text!r}", position=0) from error
    if any(not math.isfinite(p) or abs(p) > MAX_UNIT_POWER for p in powers):
        raise WorkerError(Code.PARSE_REJECTED, f"unit {text!r} has a power beyond {MAX_UNIT_POWER}", position=0)
    return unit


def parse_quantity(text: str) -> pint.Quantity:
    match = _NUMBER.match(text)
    if not match:
        raise WorkerError(Code.PARSE_REJECTED, "a quantity starts with a number", position=0)
    try:
        magnitude = exact_number(match.group(1).lstrip("+"), 0)
    except TooLarge as error:
        raise WorkerError(Code.LIMIT_EXCEEDED, str(error), limit="components") from None
    if match.group(2):
        if abs(int(match.group(2))) > MAX_TEN_POWER:  # 10^99999999999 is computed exactly, in C (SEC-08)
            raise WorkerError(Code.LIMIT_EXCEEDED, "the power of ten is too large", limit="components")
        magnitude = magnitude * sp.Integer(10) ** int(match.group(2))
    unit_text = text[match.end() :].strip()
    unit = parse_unit(unit_text) if unit_text else REGISTRY.dimensionless
    return REGISTRY.Quantity(Fraction(int(magnitude.p), int(magnitude.q)), unit)


SI_ORDER = ("kilogram", "meter", "second", "ampere", "kelvin", "mole", "candela", "radian")


def _power(power: Any) -> str:
    value = Fraction(power)
    text = str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    return "" if value == 1 else text.translate(SUPERSCRIPT_DIGITS)


def dimension_text(quantity: pint.Quantity) -> str:
    """The dimension as SI base units in SI order, for example 'm s⁻¹' or 'kg m² s⁻²'; '1' when dimensionless."""
    items = dict(quantity.to_base_units().unit_items())
    order = [n for n in SI_ORDER if n in items] + sorted(n for n in items if n not in SI_ORDER)
    parts = [f"{BASE_SYMBOLS.get(n, n)}{_power(items[n])}" for n in order if BASE_SYMBOLS.get(n, n)]
    return " ".join(parts) or "1"


def unit_text(unit: Any) -> str:
    """A unit in symbols with superscript powers, for example 'mol dm⁻³'."""
    parts = []
    for name, power in unit._units.items():
        symbol = REGISTRY.get_symbol(name) if name in REGISTRY else name
        parts.append(f"{symbol}{_power(power)}")
    return " ".join(parts) or "1"


def _magnitude(value: Any) -> dict[str, str]:
    if isinstance(value, Fraction):
        rational = sp.Rational(value.numerator, value.denominator)
        return {"text": sp.sstr(rational), "decimal": significant(rational, 6)}
    return {"text": repr(float(value)), "decimal": significant(sp.Float(value, 17), 6)}


def units_convert(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Convert a quantity to a target unit (VER-11); `difference` converts a change, not an absolute value."""
    quantity = parse_quantity(args["quantity"])
    target = parse_unit(args["to"])
    if args.get("difference"):
        quantity = REGISTRY.Quantity(quantity.magnitude, _as_delta(quantity.units))
        target = _as_delta(target)
    if quantity.dimensionality != REGISTRY.Quantity(1, target).dimensionality:
        found, expected = dimension_text(quantity), dimension_text(REGISTRY.Quantity(1, target))
        raise WorkerError(Code.DOMAIN_ERROR, f"cannot convert: found {found}, expected {expected}")
    converted = quantity.to(target)
    value = {
        "value": _magnitude(converted.magnitude),
        "unit": unit_text(converted.units),
        "difference": bool(args.get("difference")),
    }
    return value, {"method": "exact", "confidence": "proved"}


def _as_delta(unit: Any) -> Any:
    """Absolute temperature units as their difference units (a 20 °C rise is a 20 K rise)."""
    text = str(unit)
    for offset in ("degree_Celsius", "degree_Fahrenheit"):
        if offset in text:
            return REGISTRY.parse_units(text.replace(offset, f"delta_{offset}"))
    return unit


def units_check_dimensions(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Whether a quantity (or unit) has the expected dimension, naming both (VER-11)."""
    text = args["quantity"]
    quantity = parse_quantity(text) if _NUMBER.match(text) else REGISTRY.Quantity(1, parse_unit(text))
    expected = REGISTRY.Quantity(1, parse_unit(args["expected"]))
    found_text, expected_text = dimension_text(quantity), dimension_text(expected)
    value = {
        "match": quantity.dimensionality == expected.dimensionality,
        "found": found_text,
        "expected": expected_text,
    }
    if not value["match"]:
        value["message"] = f"found {found_text}, expected {expected_text}"
    return value, {"method": "rule_based", "confidence": "proved"}
