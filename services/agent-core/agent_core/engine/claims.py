"""Claim binding (AGT-10): value provenance is necessary, never sufficient.

Every number a student can read or hear must trace to a check, the question or the student (gates.untraced_numbers).
This module checks what is claimed with those numbers, on every surface (`say`, `say_spoken`, board lines, visual
labels):

- An arithmetic claim ("2 - 2 = 4", "two minus two is four", `\\frac{14^2}{19.6} = 10`) is re-checked exactly: both
  sides are evaluated in rational arithmetic, so a check id or numbers borrowed from different working never make a
  false relation pass. A side written as a rounded decimal passes when it is the other side correctly rounded.
- A variable binding ("g = 10 m/s^2") must agree with what the question and the verified plan bind that variable to,
  so an altered assumption fails even when its numbers have provenance.
- A quantity's unit must agree with the unit of the source its value came from, so "10 km" fails when the checked
  result is 10 m.

Sides that are not purely numeric (a formula, a line with an unknown) are not claims of this kind and are left to the
other gates; nothing here guesses."""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from agent_core.engine.spoken import latex_to_plain, to_written
from agent_core.engine.state import SessionState, exponent_ok, numbers_in

# Unit spellings and their canonical symbol (S1/S2 physics, chemistry and accounting quantities).
UNITS: dict[str, str] = {
    "m/s^2": "m/s^2", "m/s^(2)": "m/s^2", "m/s²": "m/s^2", "m s^-2": "m/s^2", "m s^(-2)": "m/s^2",
    "metres per second squared": "m/s^2", "meters per second squared": "m/s^2",
    "km/h": "km/h", "kilometres per hour": "km/h", "m/s": "m/s", "metres per second": "m/s",
    "meters per second": "m/s", "cm/s": "cm/s",
    "km": "km", "kilometres": "km", "kilometre": "km", "kilometers": "km", "kilometer": "km",
    "cm": "cm", "centimetres": "cm", "centimetre": "cm", "mm": "mm", "millimetres": "mm",
    "metres": "m", "metre": "m", "meters": "m", "meter": "m", "m": "m",
    "\\Omega": "ohm", "ms": "ms", "milliseconds": "ms", "minutes": "min", "minute": "min", "min": "min",
    "seconds": "s", "second": "s", "s": "s", "hours": "h", "hour": "h", "h": "h",
    "kg": "kg", "kilograms": "kg", "kilogram": "kg", "grams": "g", "gram": "g", "g": "g",
    "N/kg": "m/s^2", "newtons": "N", "newton": "N", "N": "N", "joules": "J", "joule": "J", "J": "J", "kJ": "kJ",
    "watts": "W", "watt": "W", "W": "W", "volts": "V", "volt": "V", "V": "V", "amperes": "A", "amps": "A",
    "A": "A", "ohms": "ohm", "ohm": "ohm", "Ω": "ohm", "hertz": "Hz", "Hz": "Hz", "pascals": "Pa",
    "Pa": "Pa", "kelvin": "K", "K": "K", "°C": "degC", "degrees celsius": "degC", "mol": "mol",
    "moles": "mol", "%": "%", "percent": "%",
    "kJ/mol": "kJ/mol", "J/mol": "J/mol", "kJ mol^-1": "kJ/mol", "J mol^-1": "J/mol",
    "kJ mol^(-1)": "kJ/mol", "J mol^(-1)": "J/mol",
    "m^2": "m^2", "m²": "m^2", "m^3": "m^3", "m³": "m^3",
    "cm^2": "cm^2", "cm²": "cm^2", "cm^3": "cm^3", "cm³": "cm^3",
    "mm^2": "mm^2", "mm²": "mm^2", "mm^3": "mm^3", "mm³": "mm^3",
    "km^2": "km^2", "km²": "km^2", "km^3": "km^3", "km³": "km^3",
}  # fmt: skip
# Single-letter units count only after a space (`19.6s` may be 19.6 times s; `19.6 s` is a time).
_SPACED_ONLY = frozenset(u for u in UNITS if len(u) == 1 and u.isalpha())
_UNIT_ALTERNATION = "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
QUANTITY = re.compile(rf"(?<![A-Za-z_\d.])({_NUM})(\s*)({_UNIT_ALTERNATION})(?![A-Za-z\d])")
# A digit or point before the name is a coefficient ("2x = 8" binds 2x, not x), so it never starts a binding.
BINDING = re.compile(rf"(?<![A-Za-z0-9.\\])([A-Za-z](?:_\(?[A-Za-z0-9]+\)?)?)\s*=\s*({_NUM})"
                     rf"(?:\s*({_UNIT_ALTERNATION}))?"
                     r"(?=\s*(?:$|[,.;:!?)\n]|\s+(?:[a-z]+\b)))")  # fmt: skip
RELATION = re.compile(r"\s*(?:=|≈|\\approx)\s*")
ARITH_SET = frozenset("0123456789. \t\n+-*/^()")
RELATIVE = Fraction(1, 10**9)
MAX_EXPONENT = 64
MAX_MAGNITUDE = 10**60  # beyond any value a school problem writes: such an expression is left unevaluated


@dataclass(frozen=True)
class Quantity:
    value: Fraction
    unit: str | None


def _strip_units(text: str) -> str:
    """Quantities as bare numbers: "9.8 m/s^2" is 9.8 for arithmetic."""

    def keep(match: re.Match[str]) -> str:
        if match.group(3) in _SPACED_ONLY and not match.group(2):
            return match.group(0)
        return match.group(1)

    return QUANTITY.sub(keep, text)


def plain(text: str) -> str:
    """A surface as plain symbols: LaTeX arithmetic and spoken maths read back (VOC-08 in reverse)."""
    return to_written(latex_to_plain(text))


# --- exact evaluation --------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|[-+*/^()])")


def evaluate(expression: str) -> Fraction | None:
    """The exact value of a purely numeric expression (+ - * / ^, brackets, implied products), or None."""
    pieces: list[str] = []
    position = 0
    text = expression.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            return None
        pieces.append(match.group(1))
        position = match.end()
    if not pieces:
        return None
    with_products: list[str] = []
    for piece in pieces:  # "2(9.8)" and ")(" are products
        follows_value = bool(with_products) and (with_products[-1] == ")" or with_products[-1][0].isdigit())
        if follows_value and (piece == "(" or piece[0].isdigit()):
            with_products.append("*")
        with_products.append(piece)
    parser = _Parser(with_products)
    try:
        value = parser.expression()
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if parser.done() else None


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.i = 0

    def done(self) -> bool:
        return self.i == len(self.tokens)

    def _peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end")
        self.i += 1
        return token

    def expression(self) -> Fraction:
        value = self.term()
        while self._peek() in ("+", "-"):
            value = value + self.term() if self._take() == "+" else value - self.term()
        return value

    def term(self) -> Fraction:
        value = self.unary()
        while self._peek() in ("*", "/"):
            value = value * self.unary() if self._take() == "*" else value / self.unary()
        return value

    def unary(self) -> Fraction:
        if self._peek() == "-":
            self._take()
            return -self.unary()
        if self._peek() == "+":
            self._take()
        return self.power()

    def power(self) -> Fraction:
        base = self.atom()
        if self._peek() == "^":
            self._take()
            exponent = self.unary()
            if exponent.denominator == 1 and abs(exponent) <= MAX_EXPONENT and (base != 0 or exponent >= 0):
                value = base ** int(exponent)
                if abs(value.numerator) > MAX_MAGNITUDE or value.denominator > MAX_MAGNITUDE:
                    raise ValueError("too large to be a school value")
                return value
            if exponent == Fraction(1, 2) and base >= 0:
                root = _exact_root(base)
                if root is not None:
                    return root
            raise ValueError("not an exact power")
        return base

    def atom(self) -> Fraction:
        piece = self._take()
        if piece == "(":
            value = self.expression()
            if self._take() != ")":
                raise ValueError("unbalanced")
            return value
        if piece[0].isdigit():
            return Fraction(piece)
        raise ValueError(f"unexpected {piece}")


def _exact_root(value: Fraction) -> Fraction | None:
    top, bottom = _isqrt(value.numerator), _isqrt(value.denominator)
    return Fraction(top, bottom) if top is not None and bottom is not None else None


def _isqrt(n: int) -> int | None:
    import math

    root = math.isqrt(n)
    return root if root * root == n else None


def _agrees(left: Fraction, right: Fraction, right_text: str, left_text: str) -> bool:
    """Equal, or one side is the other correctly rounded to its own written precision (at least two figures)."""
    if abs(left - right) <= RELATIVE * max(Fraction(1), abs(right)):
        return True
    return _rounding_of(right_text, left) or _rounding_of(left_text, right)


def _rounding_of(written: str, exact: Fraction) -> bool:
    token = written.strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return False
    digits = token.lstrip("-").replace(".", "").lstrip("0")
    if len(digits) < 2:
        return False
    places = len(token.split(".", 1)[1]) if "." in token else 0
    exact_decimal = Decimal(exact.numerator) / Decimal(exact.denominator)
    return exact_decimal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == Decimal(token)


# --- claims --------------------------------------------------------------------------------------------------------


def _segments(text: str) -> list[str]:
    """Sentences and lines: a full stop ends a sentence only when a space or the end follows it (not 9.8)."""
    return [s for s in re.split(r"(?<!\d)\.(?=\s|$)|\.(?=\s+[A-Z])|[;\n!?]|,\s", text) if s.strip()]


def _left_side(text: str) -> str | None:
    """The numeric tail of the text before a relation, when it is complete (no operator or letter joins it to
    anything further left)."""
    start = len(text)
    while start > 0 and text[start - 1] in ARITH_SET:  # scanned from the end: linear in the text
        start -= 1
    side = text[start:]
    if not re.search(r"\d", side):
        return None
    before = text[: len(text) - len(side)]
    stripped = side.strip()
    if stripped[:1] in ("+", "*", "/", "^", ")") or (stripped[:1] == "-" and re.search(r"[\w)]\s*$", before)):
        return None
    if before and re.search(r"[A-Za-z_\\(]$", before):  # "2s = ...": the side continues into a letter
        return None
    return stripped


def _right_side(text: str) -> str | None:
    match = re.match(r"[\d.\s+\-*/^()]*", text)
    side = match.group(0) if match else ""
    if not re.search(r"\d", side):
        return None
    after = text[len(side) :]
    stripped = side.strip().rstrip(".")
    if stripped[-1:] in ("+", "-", "*", "/", "^", "(") or (after and re.match(r"[A-Za-z_(\\]", after)):
        return None
    return stripped


def percent_claims(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Explicit numeric percentage assertions; 100 is the definition of percent, not an invented given."""
    found: list[tuple[str, str, tuple[str, ...]]] = []
    pattern = re.compile(
        rf"(?<![\w.])({_NUM})\s*(?:%|percent|per cent)\s+of\s+({_NUM})\s*"
        rf"(?:is|equals|=)\s*({_NUM})(?=\s*(?:[,.;:!?)]|$))", re.I,
    )
    for match in pattern.finditer(plain(text)):
        rate, base, result = match.groups()
        if all(len(v) <= 80 and exponent_ok(v) for v in (rate, base, result)):
            found.append((f"({rate})/100*({base})", result, (rate, base)))
    return found


def arithmetic_claims(text: str) -> list[tuple[str, str]]:
    """Numeric equation sides, excluding literal tautologies; no value or provenance is created here."""
    found = []
    for segment in _segments(_strip_units(plain(text))):
        parts = RELATION.split(segment)
        for left_text, right_text in itertools.pairwise(parts):
            left, right = _left_side(left_text), _right_side(right_text)
            if left is None or right is None:
                continue
            if not re.search(r"[-+*/^]", left.lstrip("-") + right.lstrip("-")) and left == right:
                continue
            found.append((left, right))
    return found


def false_claims(text: str) -> list[str]:
    """Arithmetic relations in the text that do not hold, including explicit percentage assertions."""
    problems = []
    pairs = [*arithmetic_claims(text), *((left, right) for left, right, _ in percent_claims(text))]
    for left, right in pairs:
        a, b = evaluate(left), evaluate(right)
        if a is not None and b is not None and not _agrees(a, b, right, left):
            problems.append(f"{left} = {right} does not hold")
    return problems


def quantities(text: str) -> list[Quantity]:
    found = []
    for match in QUANTITY.finditer(plain(text)):
        unit = match.group(3)
        if (unit in _SPACED_ONLY and not match.group(2)) or not exponent_ok(match.group(1)):
            continue
        found.append(Quantity(Fraction(match.group(1)), UNITS[unit]))
    return found


# What follows a one-letter "unit" in a formula: another symbol ("1/2 m v^2", "2 m g h"), a power or a product.
_FORMULA_NEXT = re.compile(r"\s*(?:[A-Za-z](?![A-Za-z])|[\^*(_])")


def measured(text: str) -> set[Fraction]:
    """The values a surface gives a unit ("4 Ω", "R = 4\\,\\Omega"), never a formula's coefficient: a number in a
    fraction or a power, or one before a single letter that another symbol follows ("1/2 m v^2" is no 2 metres)."""
    written = plain(text)
    found = set()
    for match in QUANTITY.finditer(written):
        number, space, unit = match.groups()
        if (unit in _SPACED_ONLY and not space) or not exponent_ok(number):
            continue
        before = written[: match.start()].rstrip()
        if before.endswith(("/", "^")) or (unit in _SPACED_ONLY and _FORMULA_NEXT.match(written, match.end())):
            continue
        found.add(Fraction(number))
    return found


def bindings(text: str) -> list[tuple[str, Quantity]]:
    """Variables bound to a single value ("g = 9.8 m/s^2", "s = 10")."""
    found = []
    for match in BINDING.finditer(plain(text)):
        name = match.group(1).replace("(", "").replace(")", "")
        if (name in UNITS and not name.isalpha()) or not exponent_ok(match.group(2)):
            continue
        unit = UNITS.get(match.group(3) or "", None)
        found.append((name, Quantity(Fraction(match.group(2)), unit)))
    return found


def _close(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= RELATIVE * max(Fraction(1), abs(b))


@dataclass(frozen=True)
class Sources:
    """What the engine knows binds each variable and carries each unit: the question and the verified plan."""

    bound: dict[str, list[Quantity]]
    units: list[Quantity]

    @classmethod
    def of(cls, state: SessionState) -> Sources:
        bound: dict[str, list[Quantity]] = {}
        units: list[Quantity] = []
        texts: list[str] = []
        if state.problem is not None:
            texts.append(state.problem.statement)
        texts.extend(state.student_lines)
        plan = state.plan
        if plan is not None:
            for step in plan.steps:
                texts.append(step.latex)
                if step.value is not None:
                    values = numbers_in(step.value)
                    if values:
                        units.append(Quantity(values[0], _unit_of(step.unit)))
            final = numbers_in(plan.final_value)
            if final:
                units.append(Quantity(final[0], _unit_of(plan.final_unit)))
            texts.append(plan.final_latex)
        for text in texts:
            for name, quantity in bindings(text):
                bound.setdefault(name, []).append(quantity)
            units.extend(quantities(text))
        if plan is not None:  # the plan's own bindings carry the step's unit when the latex omits it
            for step in plan.steps:
                for name, quantity in bindings(step.latex):
                    if quantity.unit is None and step.unit:
                        bound[name].append(Quantity(quantity.value, _unit_of(step.unit)))
            # A chain such as P = 90 + 90*12/100 = 100.8 need not contain a direct P = scalar
            # binding. The requested unknown still belongs to the verified final value, never a given
            # or an earlier wrong student line. Keep symbol case (p and P may name different quantities).
            from agent_core.engine.slice import requested_unknown

            goal = requested_unknown(state.problem.statement) if state.problem is not None else None
            final = numbers_in(plan.final_value)
            if goal and len(final) == 1 and final[0] in state.check_values.get(plan.final_check_id, ()):
                bound[goal] = [Quantity(final[0], _unit_of(plan.final_unit))]
        return cls(bound, units)


def _unit_of(unit: str | None) -> str | None:
    if not unit:
        return None
    cleaned = latex_to_plain(unit).strip()
    return UNITS.get(cleaned, cleaned)


def binding_problems(text: str, sources: Sources) -> list[str]:
    """A variable bound to a value, or a value given a unit, that disagrees with the question and the plan."""
    problems = []
    for name, quantity in bindings(text):
        known = sources.bound.get(name)
        if not known:
            continue
        same = [k for k in known if _close(quantity.value, k.value)]
        if not same:
            shown = ", ".join(_show(k) for k in known[:3])
            problems.append(f"{name} = {_show(quantity)} disagrees with {name} = {shown}")
        elif quantity.unit and all(k.unit and k.unit != quantity.unit for k in same):
            problems.append(f"{name} = {_show(quantity)} has a different unit from {name} = {_show(same[0])}")
    for quantity in quantities(text):
        if quantity.unit is None:
            continue
        matching = [s for s in sources.units if _close(quantity.value, s.value)]
        with_units = [s for s in matching if s.unit]
        if with_units and all(s.unit != quantity.unit for s in with_units):
            problems.append(f"{_show(quantity)} has a different unit from the checked {_show(with_units[0])}")
    return problems


def _show(quantity: Quantity) -> str:
    value = quantity.value
    number = str(value.numerator) if value.denominator == 1 else str(Decimal(value.numerator) / value.denominator)
    return f"{number} {quantity.unit}" if quantity.unit else number


def claim_problems(surfaces: dict[str, str], state: SessionState) -> list[str]:
    """Every claim on every surface that its checks do not support, as "surface: what is wrong"."""
    sources = Sources.of(state)
    problems = []
    for surface, text in surfaces.items():
        for problem in [*false_claims(text), *binding_problems(text, sources)]:
            problems.append(f"{surface}: {problem}")
    return problems


def written_surfaces(surfaces: dict[str, str]) -> dict[str, str]:
    """Every surface with its numbers as digits, so provenance applies to what is read or heard in words as it does to
    digits: "twenty-four point five metres per second" in `say` is 24.5 m/s (AGT-10, VOC-07)."""
    return {k: to_written(v) for k, v in surfaces.items()}


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
