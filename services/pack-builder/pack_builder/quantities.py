"""Item quantities for misconception signatures, extracted from the stem and proved by tool (CUR-18, ASM-06, VER-12,
D90).

An `answer_transform` signature is an expression over the item's `quantities`; an item that states none never matches
one. This module finds, for every numeric part, the symbols its listed signatures use and binds each to a number the
stem states, deterministically and from the item's own structure only:

- `solution`: a chain of the worked solution writes a formula and then the same formula in numbers
  (`Q = I t = 0.4 \\times 40`), or a symbol and its number (`m = 45\\ \\text{g}`); or a step writes the formula
  (`A = 2\\pi rh`) and a later step the same left side in numbers (`A = 2 \\times \\pi \\times 4 \\times 7`).
  The two are read as expression trees and must match operator for operator, so each symbol meets the number in its
  place.
- `key`: a formula a step writes is, operator for operator, what that step's check computes (the formula
  `R_2R_3/(R_2 + R_3)` inside the check `5 + 4*12/(4 + 12)`). A misconception's own formula is never read this way:
  a wrong formula that fits the right computation would bind its symbols in the wrong roles.
- `stem`: the stem names the symbol itself (`Take g = 9.8 N/kg`, `b = AC = 7 cm`).
- `label` (accounting): the symbol's words are the label of exactly one amount the stem lists (`drawings $1200`).

Numbers are never matched to symbols by their position in the stem. A symbol two readings bind to different numbers
is not bound (the solution often converts a unit: `m = 45 g`, then `0.045 \\times 25`); nor is one whose number the
stem does not state (a derived or converted value), states with two units, or states in a unit that cannot be what
the symbol stands for (a force given a mass).

Nothing is written unless it verifies by tool (`math_evaluate`, which the marker uses to evaluate signatures):

- every quantity is a number the stem (or a part's prompt) states, in the stem's unit (the contract's reading);
- the key recomputes: a formula the solution writes, or else the part's key check with each stated number named,
  evaluated over the quantities, gives the key within the marker's tolerance, and uses every quantity the signature
  uses;
- every listed signature that fires gives a finite value that differs from the key (one equal to the key would
  describe a right answer) and is the number a student holding the misconception gives from the stem's numbers as
  the marker reads them (`scale_kept`): a signature that would need a quantity converted first (`45 g` used as
  kilograms, `30%` as 0.30, days as seconds) is refused;
- no other signature the marker would try on the item (listed, or of its KCs) becomes evaluable over the quantities
  without verifying; otherwise the item is held back (`blocking`).

A model may propose what the deterministic readings leave unbound (`proposed`, from `pack_builder.proposals`): the
item's correct-answer formula in the symbols and the number it gives each symbol. The proposal binds nothing by
itself: its formula is matched, operator for operator, against what the item's own solution or key check computes
(as `key` reads an item formula), and a symbol is bound (`model`) only where that match gives the number the model
named. Every check above then applies unchanged.

Everything else is reported with its reason. A quantity name the contract's name pattern does not allow (longer than
it allows, `name_limit`, read from the contract) leaves its item out, listed. The run is idempotent: an item whose
quantities already verify is unchanged.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import cache
from typing import Any

from agent_core.config import Settings
from agent_core.contracts import document
from agent_core.engine.marking import CONSTANTS, FUNCTIONS, _close, evaluable
from pint.errors import PintError
from tool_worker.functions.units import REGISTRY, parse_unit
from tool_worker.protocol import WorkerError

from pack_builder.follow_through import evaluate

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@cache
def name_pattern() -> re.Pattern[str]:
    """The contract's pattern for a quantity name (Item.quantities propertyNames, widened by D91)."""
    item = document("content/content-pack")["$defs"]["Item"]
    return re.compile(str(item["properties"]["quantities"]["propertyNames"]["pattern"]))


@cache
def name_limit() -> int:
    """The longest quantity name the contract's pattern allows: its first character and the repeat bound after it."""
    found = re.fullmatch(r"\^\[[^\]]+\]\[[^\]]+\]\{0,(\d+)\}\$", name_pattern().pattern)
    if found is None:
        raise RuntimeError(f"the contract's quantity name pattern is not one this tool reads: {name_pattern().pattern}")
    return 1 + int(found.group(1))


def allowed_name(name: str) -> bool:
    return name_pattern().fullmatch(name) is not None


MINUS = "\u2212"
TIMES = "\u00d7"
SUPERSCRIPTS = str.maketrans("\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207b",
                             "0123456789-")  # fmt: skip


# --- reasons a quantity or a signature is not given -----------------------------------------------------------------

NOT_EXPRESSION = "not an arithmetic expression over quantities"
CONSTANT = "uses no quantity (it can fire without any)"
LONG_NAME = "a quantity name the contract's name pattern does not allow (too long)"
UNBOUND = "no reading of the solution or stem names this symbol"
NOT_STATED = "the value is not a number the stem states (derived or converted)"
CONFLICT = "readings bind the symbol to different numbers (often a unit conversion)"
NO_VALUE = "does not evaluate over the quantities"
EQUALS_KEY = "evaluates to the key"
SCALE = "needs a quantity converted from the stem's unit (the stem's number would predict a wrong value)"
NO_RECOMPUTE = "the key does not recompute from the quantities"
UNIT_AMBIGUOUS = "the stem states the value with two different units"
NOT_IN_KEY = "a quantity it uses is not one the key's computation uses"
MISFIT = "the stem's unit for the value does not fit what the symbol stands for"


# --- numbers the stem states ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stated:
    """A number the stem or a part's prompt states, with the unit written after it (or a currency before it)."""

    value: Decimal
    unit: str | None


# Unit spellings as written in stems, longest first when matched, onto the unit grammar of `units`.
_UNITS: dict[str, str] = {
    "m/s\u00b2": "m/s^2", "m/s^2": "m/s^2", "m s\u207b\u00b2": "m/s^2", "N/kg": "N/kg", "kg/m\u00b3": "kg/m^3",
    "g/cm\u00b3": "g/cm^3", "J/(kg \u00b0C)": "J/kg/K", "J/kg \u00b0C": "J/kg/K", "J/(kg K)": "J/kg/K",
    "J/kg": "J/kg", "mol/dm\u00b3": "mol/dm^3", "g/mol": "g/mol", "g/dm\u00b3": "g/dm^3", "cm\u00b3": "cm^3",
    "dm\u00b3": "dm^3", "m\u00b3": "m^3", "mm\u00b3": "mm^3", "cm\u00b2": "cm^2", "m\u00b2": "m^2",
    "mm\u00b2": "mm^2", "km\u00b2": "km^2", "km/h": "km/hour", "km/s": "km/s", "m/s": "m/s", "cm/s": "cm/s",
    "kWh": "kWh", "kW": "kW", "MW": "MW", "W": "W", "kJ": "kJ", "MJ": "MJ", "J": "J", "kN": "kN", "N": "N",
    "kPa": "kPa", "Pa": "Pa", "N/m\u00b2": "N/m^2", "mA": "mA", "A": "A", "kV": "kV", "mV": "mV", "V": "V",
    "k\u03a9": "kohm", "M\u03a9": "Mohm", "\u03a9": "ohm", "ohms": "ohm", "kHz": "kHz", "MHz": "MHz", "Hz": "Hz",
    "kBq": "kBq", "Bq": "Bq", "\u00b0C": "delta_degC", "K": "K", "kg": "kg", "mg": "mg", "g": "g",
    "tonnes": "tonne", "km": "km", "cm": "cm", "mm": "mm", "nm": "nm", "\u03bcm": "um", "\u00b5m": "um",
    "m": "m", "ms": "ms", "s": "s", "minutes": "minute", "minute": "minute", "min": "minute", "hours": "hour",
    "hour": "hour", "h": "hour", "days": "day", "day": "day", "years": "year", "year": "year", "mol": "mol",
    "C": "C", "litres": "liter", "litre": "liter", "ml": "ml", "metres": "m", "seconds": "s",
    "\u00b0": "degree", "%": "percent",
}  # fmt: skip
_UNIT_WORDS = sorted(_UNITS, key=len, reverse=True)
_CURRENCY = "$\u00a3\u20ac\u20a6\u00a5"
_NUMBER = re.compile(
    rf"(?P<cur>[{re.escape(_CURRENCY)}])?\s*(?<![\w.])(?P<neg>-)?(?P<num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    rf"(?:\s*[{TIMES}x*]\s*10\s*(?:\^\s*\{{?\s*(?P<exp>-?\d+)\s*\}}?|(?P<sup>[\u207b\u2070-\u2079\u00b9\u00b2\u00b3]+)))?"
)  # fmt: skip


_PRECISION = re.compile(r"\s*(?:significant\s+figure|decimal\s+place|s\.f\.|d\.p\.|sf\b|dp\b)", re.IGNORECASE)


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("en", ""))
    return str(value or "")


def _plain(text: str, money: bool = True) -> str:
    """Text with the minus sign, spacing commands and dollar signs normalised: `$` is money in accounting and opens or
    closes KaTeX maths everywhere else (`$2x + 2y = 0$`)."""
    text = text.replace(MINUS, "-").replace("\\,", " ").replace("\\ ", " ")
    return text.replace("$", " $") if money else text.replace("$", " ")


def stem_texts(item: Mapping[str, Any]) -> list[str]:
    """The text a student is given: the stem and every part's prompt (a later part may state more)."""
    return [_text(item.get("stem")), *(_text(p.get("prompt")) for p in item.get("parts") or [])]


def _unit_after(text: str) -> str | None:
    rest = text.lstrip(" ")
    for word in _UNIT_WORDS:
        if rest.startswith(word):
            after = rest[len(word) : len(word) + 1]
            if not after or not (after.isalnum() or after == "_") or word in ("%", "\u00b0"):
                return _UNITS[word]
    return None


def stated_numbers(item: Mapping[str, Any]) -> list[Stated]:
    """Every number the stem and prompts state, a power of ten folded in, with the unit written after it."""
    found: list[Stated] = []
    money = item.get("subject") == "acc"
    for raw in stem_texts(item):
        text = _plain(raw, money)
        for match in _NUMBER.finditer(text):
            digits = match["num"].replace(",", "")
            exponent = match["exp"] or (match["sup"].translate(SUPERSCRIPTS) if match["sup"] else None)
            value = Decimal(digits) * (Decimal(10) ** int(exponent) if exponent else 1)
            if match["neg"]:
                value = -value
            if _PRECISION.match(text[match.end() :]):
                continue  # "to 3 significant figures" states how to answer, not a quantity
            unit = "currency" if match["cur"] else _unit_after(text[match.end() :])
            found.append(Stated(value, unit))
    return found


# What a symbol stands for, by the units it may carry, per subject: a reading that gives a symbol a number stated in
# another kind of unit (`F` given a mass) is refused. Symbols not listed are not checked.
_KINDS: dict[str, dict[str, tuple[str, ...]]] = {
    "phy": {
        "m": ("kg",), "v": ("m/s",), "u": ("m/s",), "v1": ("m/s",), "v2": ("m/s",), "c": ("m/s", "J/kg/K"),
        "t": ("s", "delta_degC"), "t1": ("s", "delta_degC"), "t2": ("s", "delta_degC"), "T": ("s", "delta_degC"),
        "d": ("m",), "d1": ("m",), "d2": ("m",), "s": ("m",), "h": ("m",), "l": ("m",), "L": ("m",),
        "r": ("m", "degree"), "i": ("degree",), "lambda": ("m",), "F": ("N", "J"), "F1": ("N",), "F2": ("N",),
        "W": ("N", "J"), "P": ("W", "Pa"), "p": ("Pa", "kg*m/s"), "p1": ("Pa",), "p2": ("Pa",), "I": ("A",),
        "I1": ("A",), "I2": ("A",), "V": ("V", "m^3"), "V1": ("V", "m^3"), "V2": ("V", "m^3"), "R": ("ohm",),
        "R1": ("ohm",), "R2": ("ohm",), "R3": ("ohm",), "Q": ("C", "J"), "E": ("J",), "f": ("Hz", "m"),
        "a": ("m/s^2",), "g": ("m/s^2",), "k": ("N/m",), "rho": ("kg/m^3",), "A": ("m^2",), "dT": ("delta_degC",),
        "theta": ("delta_degC", "degree"),
    },
    "chm": {"m": ("g",), "M": ("g/mol",), "n": ("mol",), "V": ("m^3",), "c": ("mol/dm^3", "g/dm^3")},
}  # fmt: skip


def _dimension(unit: str) -> str | None:
    if unit in ("degree", "\u00b0"):
        return "angle"
    if unit in ("delta_degC", "K", "degC"):
        return "temperature"
    if unit in ("currency", "percent"):
        return unit
    try:
        return str(REGISTRY.Quantity(1, parse_unit(unit)).dimensionality)
    except (WorkerError, ValueError, TypeError, AttributeError) as _:
        return None


def fits(subject: str, symbol: str, unit: str | None) -> bool:
    """Whether a number stated with `unit` can be the symbol's value; a number with no unit always can."""
    kinds = _KINDS.get(subject, {}).get(symbol)
    if kinds is None or unit is None:
        return True
    return _dimension(unit) in {_dimension(k) for k in kinds}


# --- the worked solution, read as expressions -----------------------------------------------------------------------

GREEK = {"lambda", "rho", "theta", "alpha", "beta", "gamma", "mu", "sigma", "omega", "phi", "epsilon", "eta", "pi"}
LATEX_FUNCTIONS = {"sin", "cos", "tan", "ln", "log", "exp", "sqrt"}


@dataclass(frozen=True)
class Token:
    kind: str  # "num", "name", "op", "fn"
    text: str
    unit: str | None = None


def _braced(text: str, start: int) -> tuple[str, int]:
    """The group starting at `text[start]` ("{...}" or one character) and the index after it."""
    if start >= len(text):
        return "", start
    if text[start] != "{":
        if text[start] == "\\":
            word = re.match(r"\\[A-Za-z]+", text[start:])
            if word:
                return word.group(0), start + len(word.group(0))
        return text[start], start + 1
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    raise ValueError("unbalanced braces")


def _label(words: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", words.strip().lower()).strip("_")


def _subscript(text: str) -> str:
    inner = re.sub(r"\\(?:text|mathrm)\{([^{}]*)\}", r"\1", text)
    return re.sub(r"[^A-Za-z0-9]", "", inner)


def latex_tokens(text: str) -> list[Token] | None:
    """A LaTeX expression as tokens (None for anything outside the subset: relations, nuclide notation, ...)."""
    out: list[Token] = []
    i = 0
    text = text.replace(MINUS, "-").replace(TIMES, "\\times ")
    while i < len(text):
        char = text[i]
        if char.isspace() or char == "~":
            i += 1
            continue
        if char == "\\":
            word_match = re.match(r"\\([A-Za-z]+|.)", text[i:])
            if word_match is None:
                return None
            word = word_match.group(1)
            i += len(word_match.group(0))
            if word in (",", ";", "!", " ", "quad", "qquad", "left", "right", "displaystyle"):
                continue
            if word in ("times", "cdot"):
                out.append(Token("op", "*"))
            elif word == "div":
                out.append(Token("op", "/"))
            elif word == "%":
                if not out or out[-1].kind != "num":
                    return None
                out[-1] = Token("num", out[-1].text, "percent")
            elif word in ("frac", "tfrac", "dfrac"):
                top, i = _braced(text, i)
                bottom, i = _braced(text, i)
                inner_top, inner_bottom = latex_tokens(top), latex_tokens(bottom)
                if inner_top is None or inner_bottom is None:
                    return None
                out += [Token("op", "("), Token("op", "("), *inner_top, Token("op", ")"), Token("op", "/"),
                        Token("op", "("), *inner_bottom, Token("op", ")"), Token("op", ")")]  # fmt: skip
            elif word == "sqrt":
                inside, i = _braced(text, i)
                inner = latex_tokens(inside)
                if inner is None:
                    return None
                out += [Token("fn", "sqrt"), Token("op", "("), *inner, Token("op", ")")]
            elif word in ("text", "mathrm", "textrm"):
                inside, i = _braced(text, i)
                if out and out[-1].kind == "num" and out[-1].unit is None:
                    unit = inside.strip()
                    raised = re.match(r"\^\s*\{?\s*(-?\d)\s*\}?", text[i:])
                    if raised:  # cm^3 written \text{cm}^3: the power belongs to the unit
                        unit += "^" + raised.group(1)
                        i += len(raised.group(0))
                    out[-1] = Token("num", out[-1].text, unit or None)
                elif out and out[-1].text == ")" and inside.strip() in _UNITS:
                    continue  # the unit of `3.0 \times 10^{8}\ \text{m/s}`
                elif inside.strip():
                    out.append(Token("name", _label(inside)))
            elif word == "circ":
                if out and out[-1].kind == "op" and out[-1].text == "^":
                    out.pop()
                if out and out[-1].kind == "num":
                    out[-1] = Token("num", out[-1].text, "degree")
                rest = re.match(r"\s*\\text\{C\}|\s*C\b", text[i:])
                if rest:
                    i += len(rest.group(0))
                    if out and out[-1].kind == "num":
                        out[-1] = Token("num", out[-1].text, "delta_degC")
            elif word == "Delta":
                following = re.match(r"\s*(\\[A-Za-z]+|[A-Za-z])", text[i:])
                if following is None:
                    return None
                i += len(following.group(0))
                out.append(Token("name", "d" + following.group(1).lstrip("\\")))
            elif word == "Omega" and out and out[-1].kind == "num":
                out[-1] = Token("num", out[-1].text, "ohm")
            elif word in LATEX_FUNCTIONS:
                out.append(Token("fn", word))
            elif word in GREEK:
                out.append(Token("name", word))
            else:
                return None  # \rightarrow inside a segment, \ldots, \pm, nuclide notation, ...
            continue
        if char.isdigit() or (char == "." and i + 1 < len(text) and text[i + 1].isdigit()):
            number = re.match(r"\d+(?:\.\d+)?|\.\d+", text[i:])
            assert number is not None
            i += len(number.group(0))
            out.append(Token("num", number.group(0)))
            continue
        if char.isalpha():
            i += 1
            name = char
            if i < len(text) and text[i] == "_":
                sub, i = _braced(text, i + 1)
                name += _subscript(sub)
            out.append(Token("name", name))
            continue
        if char == "_":
            sub, i = _braced(text, i + 1)
            if not out or out[-1].kind != "name":
                return None
            out[-1] = Token("name", out[-1].text + _subscript(sub))
            continue
        if char == "^" and (degree := re.match(r"\^\s*\{?\s*\\circ\s*\}?(\s*\\text\{C\}|\s*C\b)?", text[i:])):
            i += len(degree.group(0))
            if out and out[-1].kind == "num":
                out[-1] = Token("num", out[-1].text, "delta_degC" if degree.group(1) else "degree")
            continue
        if char in "+-*/^":
            out.append(Token("op", char))
            i += 1
            if char == "^":
                power, i = _braced(text, i)
                inner = latex_tokens(power)
                if inner is None:
                    return None
                out += [Token("op", "("), *inner, Token("op", ")")]
            continue
        if char in "({[":
            out.append(Token("op", "("))
            i += 1
            continue
        if char in ")}]":
            out.append(Token("op", ")"))
            i += 1
            continue
        return None  # ":", ",", "<", "|", ...
    return out


def _explicit(tokens: Sequence[Token]) -> list[Token]:
    """Products written out, a parenthesised signed number folded into one number, `a \\times 10^{k}` folded."""
    folded: list[Token] = []
    for token in tokens:
        folded.append(token)
        if len(folded) >= 4 and [t.text for t in folded[-4:-2]] == ["(", "-"] and folded[-2].kind == "num" and (
                folded[-1].text == ")"):  # fmt: skip
            number = folded[-2]
            del folded[-4:]
            folded.append(Token("num", "-" + number.text, number.unit))
    out: list[Token] = []
    index = 0
    while index < len(folded):
        token = folded[index]
        after = [t.text for t in folded[index + 1 : index + 8]]
        if token.kind == "num" and after[:4] == ["*", "10", "^", "("]:
            sign = "-" if after[4:5] == ["-"] else ""
            at = index + 5 + len(sign)  # the exponent's index
            if at + 1 < len(folded) and folded[at].kind == "num" and folded[at + 1].text == ")":
                out.append(Token("num", f"{token.text}e{sign}{folded[at].text}", token.unit))
                index = at + 2
                continue
        out.append(token)
        index += 1
    explicit: list[Token] = []
    for token in out:
        if explicit:
            before = explicit[-1]
            ends = before.kind in ("num", "name") or before.text == ")"
            starts = token.kind in ("num", "name", "fn") or token.text == "("
            if ends and starts:
                explicit.append(Token("op", "*"))
        if token.kind == "op" and token.text == "-" and (not explicit or explicit[-1].text in ("(", "*", "/", "^")):
            explicit.append(Token("op", "~"))  # a unary minus
            continue
        explicit.append(token)
    return explicit


def statements(latex: str) -> list[list[list[Token]]]:
    """A solution step's statements (`2(4) + 2y = 0 \\Rightarrow y = -4` is two; so are `a = 1,\\quad b = 2`), each
    a chain of sides (`segments`)."""
    parts = re.split(r"\\(?:Rightarrow|implies|Longrightarrow)|,\s*\\q?quad|;\s*\\q?quad|\\text\{\s*(?:and|or|so)\s*\}",
                     latex)  # fmt: skip
    return [segments(part) for part in parts if part.strip()]


def segments(latex: str) -> list[list[Token]]:
    """A chain (`Q = I t = 0.4 \\times 40 = 16`) as one token list per side; a side outside the subset is an empty
    list, so neighbours never pair across it. `\\rightarrow` and `\\approx` read as `=` (`\\frac{14500}{95500}
    \\rightarrow 15.18\\%`)."""
    text = re.sub(r"\\(?:rightarrow|to|approx|longrightarrow)(?![A-Za-z])", "=", latex)
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        depth += char == "{"
        depth -= char == "}"
        if char == "=" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [_explicit(tokens) if (tokens := latex_tokens(part)) else [] for part in parts]


def _symbolic(tokens: Sequence[Token]) -> bool:
    return any(t.kind == "name" and t.text != "pi" for t in tokens)


def _numeric(tokens: Sequence[Token]) -> bool:
    return bool(tokens) and not _symbolic(tokens) and any(t.kind == "num" for t in tokens)


def expression_tokens(expression: str) -> list[Token] | None:
    """A checker or signature expression (`50*g*8`, `2*pi*(6400 + 850)`, `m*v^2/2`) as tokens."""
    out: list[Token] = []
    position = 0
    pattern = re.compile(r"\s*(\d+(?:\.\d+)?(?:[eE]-?\d+)?|[A-Za-z_][A-Za-z0-9_]*|\*\*|[-+*/^()])\s*")
    while position < len(expression):
        match = pattern.match(expression, position)
        if match is None or match.end() == position:
            return None
        position = match.end()
        text = match.group(1)
        if text[0].isdigit():
            out.append(Token("num", text))
        elif text in FUNCTIONS or text == "sqrt":
            out.append(Token("fn", text))
        elif text[0].isalpha() or text[0] == "_":
            out.append(Token("name", text))
        else:
            out.append(Token("op", "^" if text == "**" else text))
    return _explicit(out)


# An expression tree: ("num", Decimal), ("name", str), ("fn", name, child), ("neg", child) or (op, left, right).
Node = tuple[Any, ...]


class _Parser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = list(tokens)
        self.at = 0

    def peek(self) -> Token | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> Token:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def expression(self) -> Node:
        node = self.term()
        while (token := self.peek()) is not None and token.kind == "op" and token.text in "+-":
            node = (self.take().text, node, self.term())
        return node

    def term(self) -> Node:
        node = self.unary()
        while (token := self.peek()) is not None and token.kind == "op" and token.text in "*/":
            node = (self.take().text, node, self.unary())
        return node

    def unary(self) -> Node:
        token = self.peek()
        if token is not None and token.text == "~":
            self.take()
            return ("neg", self.unary())
        return self.power()

    def power(self) -> Node:
        node = self.atom()
        token = self.peek()
        if token is not None and token.text == "^":
            self.take()
            return ("^", node, self.unary())
        return node

    def atom(self) -> Node:
        token = self.take()
        if token.kind == "num":
            return ("num", Decimal(token.text))
        if token.kind == "name":
            return ("name", token.text)
        if token.kind == "fn":
            return ("fn", token.text, self.unary())
        if token.text == "(":
            node = self.expression()
            if self.take().text != ")":
                raise ValueError("unbalanced")
            return node
        raise ValueError(f"unexpected {token.text}")


def tree(tokens: Sequence[Token]) -> Node | None:
    """The expression tree of a token list, or None when it does not parse."""
    if not tokens:
        return None
    parser = _Parser(tokens)
    try:
        node = parser.expression()
    except (ValueError, IndexError, InvalidOperation):
        return None
    return node if parser.at == len(parser.tokens) else None


def subtrees(node: Node) -> Iterable[Node]:
    yield node
    if node[0] in ("fn", "neg"):
        yield from subtrees(node[-1])
    elif node[0] not in ("num", "name"):
        yield from subtrees(node[1])
        yield from subtrees(node[2])


def match(pattern: Node, node: Node, env: dict[str, Decimal]) -> bool:
    """Whether `node` is `pattern` with each of its symbols a number (the same number wherever a symbol recurs); a
    name in both (`g`, `pi`) stands for itself."""
    kind = pattern[0]
    if kind == "name":
        if node[0] == "name":
            return bool(node[1] == pattern[1])
        if node[0] == "neg" and node[1][0] == "num":
            node = ("num", -node[1][1])  # y = -4
        if node[0] != "num" or pattern[1] == "pi":
            return False
        if pattern[1] in env:
            return bool(env[pattern[1]] == node[1])
        env[pattern[1]] = node[1]
        return True
    if kind != node[0]:
        return False
    if kind == "num":
        return bool(pattern[1] == node[1])
    if kind == "fn":
        return bool(pattern[1] == node[1]) and match(pattern[2], node[2], env)
    if kind == "neg":
        return match(pattern[1], node[1], env)
    return match(pattern[1], node[1], env) and match(pattern[2], node[2], env)


def render(node: Node) -> str:
    """A tree as a checker expression."""
    kind = node[0]
    if kind == "num":
        return _decimal_text(node[1])
    if kind == "name":
        return str(node[1])
    if kind == "fn":
        return f"{node[1]}({render(node[2])})"
    if kind == "neg":
        return f"(-{render(node[1])})"
    return f"({render(node[1])} {kind} {render(node[2])})"


def _has_operator(node: Node) -> bool:
    return node[0] not in ("num", "name")


# --- bindings -------------------------------------------------------------------------------------------------------


@dataclass
class Binding:
    values: dict[str, set[Decimal]] = field(default_factory=dict)
    sources: dict[str, set[str]] = field(default_factory=dict)

    def add(self, symbol: str, value: Decimal, source: str) -> None:
        self.values.setdefault(symbol, set()).add(value)
        self.sources.setdefault(symbol, set()).add(source)


def _check_expression(item: Mapping[str, Any], check_id: str | None) -> str | None:
    check = (item.get("checks") or {}).get(check_id or "") or {}
    if check.get("tool") != "math_evaluate":
        return None
    expression = (check.get("args") or {}).get("expression")
    return str(expression) if expression else None


@dataclass
class Structure:
    """What the item's solution writes, step by step: the formulas in symbols, and what it computes in numbers (the
    chain's numeric sides and the step's math_evaluate check)."""

    steps: list[tuple[list[Node], list[Node]]]

    @property
    def formulas(self) -> list[Node]:
        return [f for formulas, _ in self.steps for f in formulas]


def _defined(lhs: Node) -> bool:
    """A left side that names one quantity (`A`, `c^2`): a later step with the same left side computes the same
    quantity. A left side over two symbols (`p_1V_1` in `p_1V_1 = p_2V_2`) does not: the formula on its right is
    another state of the system, whose numbers are not the ones a later `p_1V_1 = 160 \\times 120` writes."""
    names = {n[1] for n in subtrees(lhs) if n[0] == "name" and n[1] != "pi"}
    return len(names) == 1


def structure(item: Mapping[str, Any], binding: Binding, wanted: set[str]) -> Structure:
    """Read the solution's chains: a symbol given one number (`m = 45 g`), a formula followed in its chain by the
    same formula in numbers (`Q = I t = 0.4 \\times 40`), or written in an earlier step for the same left side
    (`A = 2\\pi rh`, then `A = 2 \\times \\pi \\times 4 \\times 7`)."""
    steps = list((item.get("solution") or {}).get("steps") or [])
    found: list[tuple[list[Node], list[Node]]] = []
    by_lhs: dict[str, list[Node]] = {}
    chains: list[list[list[Token]]] = []
    for step in steps:
        formulas: list[Node] = []
        computed: list[Node] = []
        expression = _check_expression(item, step.get("check_id"))
        tokens = expression_tokens(expression) if expression else None
        if tokens is not None and (checked := tree(tokens)) is not None:
            computed.append(checked)
        for chain in statements(str(step.get("latex") or "")):
            chains.append(chain)
            _read_chain(chain, formulas, computed, by_lhs, binding, wanted)
        found.append((formulas, computed))
    for chain in chains:
        nodes = [tree(side) for side in chain]
        lhs = nodes[0] if nodes else None
        if lhs is None or render(lhs) not in by_lhs:
            continue
        for side, node in zip(chain[1:], nodes[1:], strict=True):
            if node is not None and _numeric(side):
                for formula in by_lhs[render(lhs)]:
                    _bind(formula, node, binding, wanted, "solution")
    return Structure(found)


def _read_chain(chain: list[list[Token]], formulas: list[Node], computed: list[Node], by_lhs: dict[str, list[Node]],
                binding: Binding, wanted: set[str]) -> None:  # fmt: skip
    nodes = [tree(side) for side in chain]
    lhs = nodes[0] if nodes else None
    name = render(lhs) if lhs is not None and _defined(lhs) else None
    for index, (side, node) in enumerate(zip(chain, nodes, strict=True)):
        if node is None:
            continue
        if _numeric(side):
            computed.append(node)
            continue
        if not _symbolic(side):
            continue
        formulas.append(node)
        if name is not None and index > 0:
            by_lhs.setdefault(name, []).append(node)
        following = next((n for s, n in zip(chain[index + 1 :], nodes[index + 1 :], strict=True)
                          if n is not None and _numeric(s)), None)  # fmt: skip
        if following is not None:
            _bind(node, following, binding, wanted, "solution")


def _bind(pattern: Node, node: Node, binding: Binding, wanted: set[str], source: str) -> None:
    env: dict[str, Decimal] = {}
    if match(pattern, node, env):
        for symbol, value in env.items():
            if symbol in wanted:
                binding.add(symbol, value, source)


def _operators(node: Node) -> int:
    return sum(1 for n in subtrees(node) if _has_operator(n))


def key_bindings(found: Structure, binding: Binding, wanted: set[str]) -> None:
    """Bind symbols where a formula a solution step writes is, operator for operator, what that step computes in
    numbers or checks: `R_p = R_2R_3/(R_2 + R_3)` inside the step's check `5 + 4*12/(4 + 12)`. Only the item's own
    formulas are read this way, never a misconception's (a wrong formula that fits the right computation would bind
    its symbols in the wrong roles), and only against its own step; a formula with one operator is matched only
    against a whole computation, since its shape fits too many parts of one. A formula that fits two places with
    different numbers binds nothing (the binding conflicts)."""
    for formulas, computed in found.steps:
        for pattern in formulas:
            if not _has_operator(pattern):
                continue
            for numbers in computed:
                for node in subtrees(numbers) if _operators(pattern) > 1 else [numbers]:
                    _bind(pattern, node, binding, wanted, "key")


_STEM_NAMED = re.compile(r"(?<![A-Za-z0-9_])(?P<name>[A-Za-z](?:_\{?[A-Za-z0-9]{1,4}\}?)?)\s*=\s*(?:[A-Z]{2}\s*=\s*)?"
                         r"(?P<neg>-)?(?P<num>\d+(?:\.\d+)?)(?![\d.])")  # fmt: skip


def stem_bindings(item: Mapping[str, Any], wanted: set[str], binding: Binding) -> None:
    """Bind the wanted symbols the stem names itself (`Take g = 9.8 N/kg`, `b = AC = 7 cm`)."""
    for raw in stem_texts(item):
        for match in _STEM_NAMED.finditer(_plain(raw).replace("$", " ")):
            name = re.sub(r"[_{}]", "", match["name"])
            if name in wanted:
                value = Decimal(match["num"])
                binding.add(name, -value if match["neg"] else value, "stem")


_AMOUNT = re.compile(rf"(?P<label>[A-Za-z][A-Za-z '\-()]*?)\s*(?::|of|was|is)?\s*[{re.escape(_CURRENCY)}]\s*"
                     r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")  # fmt: skip
_STOP = {"the", "a", "an", "of", "for", "in", "on", "at", "and", "year", "his", "her", "its", "was", "is"}


def _words(text: str) -> set[str]:
    words = {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z]+", text.lower())}
    return words - _STOP


def label_bindings(item: Mapping[str, Any], wanted: set[str], binding: Binding) -> None:
    """Bind a multi-letter symbol whose words are those of exactly one amount's label in the stem (`drawings $1200`,
    `trade receivables $3400` for `receivables`); an amount whose label more than one symbol fits binds none."""
    amounts: list[tuple[set[str], Decimal]] = []
    for raw in stem_texts(item):
        for clause in re.split(r"[;\n]|\.\s", _plain(raw)):
            for match in _AMOUNT.finditer(clause):
                if re.match(r"\s*(?:each|per|a |an )", clause[match.end() :]):
                    continue  # a price per unit: which total or unit amount a symbol means is not decided by words
                amounts.append((_words(match["label"]), Decimal(match["num"].replace(",", ""))))
    for symbol in wanted:
        if len(symbol) < 3 or symbol in GREEK:
            continue
        words = _words(symbol.replace("_", " "))
        if not words:
            continue
        fits = [value for label, value in amounts if words <= label]
        if len(fits) == 1:
            binding.add(symbol, fits[0], "label")


# --- verification ---------------------------------------------------------------------------------------------------


_DECIMAL = re.compile(r"-?\d+(?:\.\d+)?")


def check_bindings(item: Mapping[str, Any], wanted: set[str], binding: Binding) -> None:
    """Bind a wanted symbol that one of the item's own tool checks names as an argument (`acc_ratios` with
    `closing_inventory: '7600'`): the check computes the key from it under that name."""
    for check in (item.get("checks") or {}).values():
        for name, value in ((check or {}).get("args") or {}).items():
            if name in wanted and isinstance(value, (str, int, float)) and not isinstance(value, bool):
                text = str(value).strip()
                if _DECIMAL.fullmatch(text):
                    binding.add(str(name), Decimal(text), "check")


def _computations(item: Mapping[str, Any], found: Structure) -> list[Node]:
    """What the item computes in numbers: each step's numeric sides and checks, and every numeric part's key check."""
    computed = [node for _, numbers in found.steps for node in numbers]
    for part in item.get("parts") or []:
        expression = _check_expression(item, (part.get("answer") or {}).get("check_id"))
        tokens = expression_tokens(expression) if expression else None
        node = tree(tokens) if tokens else None
        if node is not None:
            computed.append(node)
    return computed


def _match_unordered(pattern: Node, node: Node, env: dict[str, Decimal]) -> bool:
    """`match`, also with the two sides of a sum or product in either order (`a + b` fits `7600 + 5800`); `env` is
    left unchanged when nothing fits."""
    trial = dict(env)
    if match(pattern, node, trial):
        env.update(trial)
        return True
    if pattern[0] in ("+", "*") and node[0] == pattern[0]:
        for left, right in ((node[2], node[1]), (node[1], node[2])):
            trial = dict(env)
            if _match_unordered(pattern[1], left, trial) and _match_unordered(pattern[2], right, trial):
                env.update(trial)
                return True
    elif pattern[0] not in ("num", "name") and pattern[0] == node[0] and pattern[0] not in ("fn", "neg"):
        trial = dict(env)
        if _match_unordered(pattern[1], node[1], trial) and _match_unordered(pattern[2], node[2], trial):
            env.update(trial)
            return True
    return False


def model_bindings(item: Mapping[str, Any], found: Structure, proposed: Mapping[str, Any],
                   binding: Binding) -> tuple[list[Node], set[str]]:  # fmt: skip
    """Bind what a model proposed only where the item's own computation proves it: each proposed formula is matched,
    operator for operator, against what the item computes (as `key_bindings` reads an item's formula; a formula of
    one operator or none only against a whole computation), and its symbols are bound (`model`) only when the match
    gives every one of them the number the model named. A symbol an
    earlier reading bound keeps that binding. Returns the formulas that fit (the key may recompute from them) and the
    symbols they bind, which may include helper symbols beyond the wanted ones."""
    mapping: dict[str, Decimal] = {}
    for name, value in (proposed.get("quantities") or {}).items():
        text = str(value).strip()
        if allowed_name(str(name)) and _DECIMAL.fullmatch(text):
            mapping[str(name)] = Decimal(text)
    computed = _computations(item, found)
    fitted: list[Node] = []
    bound: set[str] = set()
    for text in (proposed.get("formulas") or {}).values():
        tokens = expression_tokens(str(text))
        pattern = tree(tokens) if tokens else None
        if pattern is None:
            continue
        names = {n[1] for n in subtrees(pattern) if n[0] == "name" and n[1] != "pi"}
        if not names or not names <= set(mapping) or not all(allowed_name(n) for n in names):
            continue
        if any(_proves(pattern, node, names, mapping) for numbers in computed
               for node in (subtrees(numbers) if _operators(pattern) > 1 else [numbers])):  # fmt: skip
            fitted.append(pattern)
            for name in names:
                if name not in binding.values:
                    binding.add(name, mapping[name], "model")
                bound.add(name)
    return fitted, bound


def _proves(pattern: Node, node: Node, names: set[str], mapping: Mapping[str, Decimal]) -> bool:
    env: dict[str, Decimal] = {}
    return _match_unordered(pattern, node, env) and all(env.get(n) == mapping[n] for n in names)


def names_in(expression: str) -> list[str]:
    seen: list[str] = []
    for name in _NAME.findall(expression):
        if name not in FUNCTIONS and name not in CONSTANTS and name not in seen:
            seen.append(name)
    return seen


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _with_unit(value: Decimal, unit: str | None) -> Any:
    """A stated number as a Pint quantity in the stem's unit; money and angles are plain numbers (an angle is read in
    degrees by the checker at O level), a percentage is a percentage."""
    if unit is None or unit in ("currency", "$", "degree"):
        return REGISTRY.Quantity(Fraction(str(value)))
    try:
        return REGISTRY.Quantity(Fraction(str(value)), parse_unit(unit))
    except (WorkerError, ValueError, TypeError, AttributeError) as _:
        return None


def _pint_value(node: Node, env: Mapping[str, Any]) -> Any:
    kind = node[0]
    if kind == "unit":
        return _with_unit(node[1], node[2])
    if kind == "num":
        return REGISTRY.Quantity(Fraction(str(node[1])))
    if kind == "name":
        if node[1] == "pi":
            return REGISTRY.Quantity(Fraction(math.pi))
        return env[node[1]]
    if kind == "neg":
        return -_pint_value(node[1], env)
    if kind == "fn":
        inner = _pint_value(node[2], env)
        if node[1] == "sqrt":
            return inner**0.5
        if node[1] == "abs":
            return abs(inner)
        if node[1] in ("sin", "cos", "tan"):  # an angle in degrees, as the checker reads it at O level
            function = {"sin": math.sin, "cos": math.cos, "tan": math.tan}[node[1]]
            return REGISTRY.Quantity(Fraction(function(math.radians(float(inner.to("dimensionless").magnitude)))))
        raise ValueError("a function of a quantity with a unit")  # log, exp, ...: the unit cannot be followed
    left, right = _pint_value(node[1], env), _pint_value(node[2], env)
    if kind == "+":
        return left + right
    if kind == "-":
        return left - right
    if kind == "*":
        return left * right
    if kind == "/":
        return left / right
    exponent = right.to("dimensionless").magnitude
    return left ** (int(exponent) if Fraction(exponent).denominator == 1 else float(exponent))


def scale_kept(expression: str, quantities: Mapping[str, str], units: Mapping[str, str | None], key_unit: str | None,
               plain: Decimal, coherent: bool) -> bool:  # fmt: skip
    """Whether the signature's value over the stem's numbers as written (`plain`, what the marker computes) is the
    number a student holding the misconception gives. Evaluated with the stem's units: when the result is a quantity
    of the key's kind, it is read in the key's unit and must be `plain` (`r/T` over km and days is not a speed in
    km/s; `cost*rate` over $ and % is not cost times 30); when it is of another kind (`pi*r` for an area, `p1` for a
    volume), the student writes the number they computed from the stem, which is `plain`, provided the stem's units
    are `coherent` for the item (its key computes from them with no conversion, `key_coherent`)."""
    tokens = expression_tokens(expression)
    node = tree(tokens) if tokens else None
    if node is None:
        return False
    env = {n: _with_unit(Decimal(quantities[n]), units.get(n)) for n in names_in(expression) if n in quantities}
    if any(v is None for v in env.values()):
        return False
    try:
        value = _pint_value(node, env)
        key = REGISTRY.Quantity(1) if key_unit in (None, "$", "currency") else _with_unit(Decimal(1), key_unit)
        if key is None or value.dimensionality != key.dimensionality:
            return coherent
        read = Decimal(str(float(value.to(key.units).magnitude)))
    except PintError as _:
        return coherent  # a sum of unlike quantities (`t2 + 273`): the student adds the numbers as written
    except (ValueError, TypeError, ZeroDivisionError, OverflowError, AttributeError, KeyError) as _:
        return coherent
    return _close(read, plain, {"mode": "relative", "value": 1e-6}, 1e-6)


def key_unit_of(answer: Mapping[str, Any]) -> str | None:
    """The key's unit as a unit: a word the marker ignores (`times` with `unit_policy: none`) is a pure number."""
    unit = answer.get("unit")
    if unit and answer.get("unit_policy") == "none" and unit not in ("$", "currency"):
        if _with_unit(Decimal(1), str(unit)) is None:
            return None
    return str(unit) if unit else None


MAX_READINGS = 1024  # the unit readings of a key check tried (two per stated literal); more is not a school item


def key_coherent(item: Mapping[str, Any], part: Mapping[str, Any], quantities: Mapping[str, str],
                 units: Mapping[str, str | None], stated: Sequence[Stated], correct: str | None,
                 default_relative: float) -> bool:  # fmt: skip
    """Whether the part's key computes from the stem's numbers in the stem's units with no unit conversion: some
    reading of its key check, each literal the stem states taken in its stated unit or as a pure number (the 4 of
    `4/3` in a sphere's volume), or else of the expression the key recomputed from (`correct`, over the quantities in
    their units), gives the key in the key's unit. False when the key converts (`*86400` for days, `/1000` for kPa)."""
    answer = part["answer"]
    key_unit = key_unit_of(answer)
    key = REGISTRY.Quantity(1) if key_unit in (None, "$", "currency") else _with_unit(Decimal(1), key_unit)
    if key is None:
        return False
    target = Decimal(str(answer["value"]))
    env = {n: _with_unit(Decimal(v), units.get(n)) for n, v in quantities.items()}
    if any(v is None for v in env.values()):
        return False

    def gives_key(node: Node) -> bool:
        try:
            value = _pint_value(node, env)
            if value.dimensionality != key.dimensionality:
                return False
            read = Decimal(str(float(value.to(key.units).magnitude)))
        except (PintError, ValueError, TypeError, ZeroDivisionError, OverflowError, AttributeError, KeyError) as _:
            return False
        return _close(read, target, answer.get("tolerance"), default_relative)

    expression = _check_expression(item, answer.get("check_id"))
    for text in (expression, correct):
        tokens = expression_tokens(text) if text else None
        node = tree(tokens) if tokens else None
        if node is None:
            continue
        names = {n[1] for n in subtrees(node) if n[0] == "name"} - {"pi"}
        if not names <= set(env):
            continue
        numbered, _ = _numbered(node, 0)
        literals = [n for n in subtrees(numbered) if n[0] == "num"]
        options = [[None, *sorted({s.unit for s in stated if s.value == n[1] and s.unit})] for n in literals]
        if math.prod(len(o) for o in options) > MAX_READINGS:
            continue
        for chosen in itertools.product(*options):
            if gives_key(_with_literal_units(numbered, dict(enumerate(chosen)))):
                return True
    return False


def _numbered(node: Node, start: int) -> tuple[Node, int]:
    """The tree with each literal numbered in order (`("num", value, n)`), and the next number."""
    if node[0] == "num":
        return ("num", node[1], start), start + 1
    if node[0] == "name":
        return node, start
    if node[0] in ("fn", "neg"):
        child, after = _numbered(node[-1], start)
        return (*node[:-1], child), after
    left, middle = _numbered(node[1], start)
    right, after = _numbered(node[2], middle)
    return (node[0], left, right), after


def _with_literal_units(node: Node, units: Mapping[int, str | None]) -> Node:
    if node[0] == "num":
        unit = units.get(node[2]) if len(node) > 2 else None
        return ("unit", node[1], unit) if unit else ("num", node[1])
    if node[0] == "name":
        return node
    if node[0] in ("fn", "neg"):
        return (*node[:-1], _with_literal_units(node[-1], units))
    return (node[0], _with_literal_units(node[1], units), _with_literal_units(node[2], units))


@dataclass
class SignatureOutcome:
    part_id: str
    misconception_id: str
    expression: str
    live: bool
    reason: str | None = None
    value: str | None = None


@dataclass
class ItemOutcome:
    item_id: str
    quantities: dict[str, str]
    signatures: list[SignatureOutcome]
    kc_signatures: list[SignatureOutcome]  # signatures of the item's other KC misconceptions the quantities reach
    symbols: dict[str, str]  # a reason per wanted symbol that is not given
    recompute: dict[str, str]  # part id: how its key recomputed, or why not
    sources: dict[str, list[str]]
    long_names: list[str]

    @property
    def live(self) -> list[SignatureOutcome]:
        return [s for s in self.signatures if s.live]


def linked(item: Mapping[str, Any], library: Mapping[str, Mapping[str, Any]], *, listed: bool = True
           ) -> list[tuple[Mapping[str, Any], str, str]]:  # fmt: skip
    """(numeric part, misconception id, expression) for each `answer_transform` signature a numeric part lists; with
    `listed=False`, those of the other misconceptions of the item's KCs, which the marker also tries
    (`signatures.candidates`)."""
    kcs = {str(k) for k in item.get("kc_ids") or []}
    out = []
    for part in item.get("parts") or []:
        if (part.get("answer") or {}).get("kind") != "numeric":
            continue
        named = [str(m) for m in part.get("misconception_ids") or []]
        if listed:
            ids = named
        else:
            ids = sorted(m for m, r in library.items()
                         if m not in named and kcs & {str(k) for k in r.get("kc_ids", [])})  # fmt: skip
        for misconception_id in ids:
            record = library.get(misconception_id)
            for signature in (record or {}).get("signatures") or []:
                applies = signature.get("applies_to_part")
                if signature["kind"] == "answer_transform" and applies in (None, part["id"]):
                    out.append((part, misconception_id, str(signature["expression"])))
    return out


def _shape_ok(expression: str) -> bool:
    return evaluable(expression, dict.fromkeys(names_in(expression), "1")) is not None


def _recompute(item: Mapping[str, Any], part: Mapping[str, Any], quantities: Mapping[str, str],
               formulas: Sequence[Node], settings: Settings) -> str | None:  # fmt: skip
    """The part's key from the quantities, within the marker's tolerance: a formula the solution writes whose symbols
    are all quantities, else the key check with each stated number that is exactly one quantity's named; the
    expression, or None."""
    answer = part["answer"]
    key = Decimal(str(answer["value"]))
    tolerance = answer.get("tolerance")
    default = settings.numeric_default_relative_tolerance
    for formula in formulas:
        names = {n[1] for n in subtrees(formula) if n[0] == "name" and n[1] != "pi"}
        if not names or not names <= set(quantities):
            continue
        expression = render(formula)
        value = evaluate(expression, dict(quantities))
        if value is not None and _close(value, key, tolerance, default):
            return expression
    check = _check_expression(item, answer.get("check_id"))
    tokens = expression_tokens(check) if check is not None else None
    if tokens is None:
        return None
    by_value: dict[Decimal, list[str]] = {}
    for name, text in quantities.items():
        by_value.setdefault(Decimal(text), []).append(name)
    rewritten: list[str] = []
    for token in tokens:
        named = by_value.get(Decimal(token.text), []) if token.kind == "num" else []
        if len(named) == 1 and Decimal(token.text) not in (0, 1):  # 0 and 1 are too often not a quantity
            rewritten.append(named[0])
        else:
            rewritten.append("-" if token.text == "~" else token.text)
    expression = " ".join(rewritten)
    if expression == " ".join("-" if t.text == "~" else t.text for t in tokens):
        return None  # no quantity in the key's computation
    value = evaluate(expression, dict(quantities))
    return expression if value is not None and _close(value, key, tolerance, default) else None


@dataclass
class _Context:
    item: Mapping[str, Any]
    quantities: dict[str, str]
    units: dict[str, str | None]
    stated: list[Stated]
    formulas: list[Node]
    settings: Settings
    recompute: dict[str, str | None] = field(default_factory=dict)
    coherent: dict[str, bool] = field(default_factory=dict)


def _judge(outcome: SignatureOutcome, part: Mapping[str, Any], context: _Context, symbols: Mapping[str, str]) -> None:
    """Whether the signature can fire on the part over the quantities, and why not."""
    expression, quantities = outcome.expression, context.quantities
    names = names_in(expression)
    if not _shape_ok(expression):
        outcome.reason = NOT_EXPRESSION
        return
    if not names:
        outcome.reason = CONSTANT
        return
    if not all(allowed_name(n) for n in names):
        outcome.reason = LONG_NAME
        return
    missing = [n for n in names if n not in quantities]
    if missing:
        outcome.reason = "; ".join(f"{n}: {symbols.get(n, UNBOUND)}" for n in missing)
        return
    value = evaluate(expression, quantities)
    if value is None:
        outcome.reason = NO_VALUE
        return
    outcome.value = _decimal_text(value)
    answer = part["answer"]
    if _close(value, Decimal(str(answer["value"])), answer.get("tolerance"),
              context.settings.numeric_default_relative_tolerance):  # fmt: skip
        outcome.reason = EQUALS_KEY
        return
    part_id = str(part["id"])
    if part_id not in context.recompute:
        context.recompute[part_id] = _recompute(context.item, part, quantities, context.formulas, context.settings)
    correct = context.recompute[part_id]
    if correct is None:
        outcome.reason = NO_RECOMPUTE
        return
    if not set(names) <= set(names_in(correct)):
        outcome.reason = NOT_IN_KEY
        return
    if part_id not in context.coherent:
        context.coherent[part_id] = key_coherent(context.item, part, quantities, context.units, context.stated, correct,
                                                 context.settings.numeric_default_relative_tolerance)  # fmt: skip
    if not scale_kept(expression, quantities, context.units, key_unit_of(answer), value, context.coherent[part_id]):
        outcome.reason = SCALE
        return
    outcome.live = True


def extract(item: Mapping[str, Any], library: Mapping[str, Mapping[str, Any]], settings: Settings | None = None,
            proposed: Mapping[str, Any] | None = None) -> ItemOutcome:  # fmt: skip
    """The quantities an item can be given, each signature its numeric parts list with whether it can fire, and every
    reason. The quantities are only those the live signatures use; the misconceptions of the item's KCs that the
    marker also tries are judged over them too (`kc_signatures`). `proposed` is a model's proposal for the item
    (`model_bindings`), read only for what the item's own computation proves."""
    settings = settings or Settings()
    links = linked(item, library)
    wanted: set[str] = set()
    long_names: set[str] = set()
    for _, _, expression in links:
        if _shape_ok(expression):
            for name in names_in(expression):
                (wanted if allowed_name(name) else long_names).add(name)
    binding = Binding()
    shape = structure(item, binding, wanted)
    key_bindings(shape, binding, wanted)
    stem_bindings(item, wanted, binding)
    label_bindings(item, wanted, binding)
    check_bindings(item, wanted, binding)
    formulas = list(shape.formulas)
    if proposed:
        fitted, helpers = model_bindings(item, shape, proposed, binding)
        formulas += fitted
        wanted |= helpers
    stated = stated_numbers(item)
    quantities: dict[str, str] = {}
    units: dict[str, str | None] = {}
    symbols: dict[str, str] = {}
    subject = str(item.get("subject"))
    for name in sorted(wanted):
        values = binding.values.get(name, set())
        same = [s for s in stated if values and s.value == next(iter(values))]
        fitting = {s.unit for s in same if fits(subject, name, s.unit)}
        if not values:
            symbols[name] = UNBOUND
        elif len(values) > 1:
            symbols[name] = CONFLICT
        elif not same:
            symbols[name] = NOT_STATED
        elif not fitting:
            symbols[name] = MISFIT
        elif len(fitting) > 1:
            symbols[name] = UNIT_AMBIGUOUS
        else:
            quantities[name] = _decimal_text(next(iter(values)))
            units[name] = next(iter(fitting))
    context = _Context(item, quantities, units, stated, formulas, settings)
    outcomes: list[SignatureOutcome] = []
    for part, misconception_id, expression in links:
        outcome = SignatureOutcome(str(part["id"]), misconception_id, expression, False)
        _judge(outcome, part, context, symbols)
        outcomes.append(outcome)
    used = {n for o in outcomes if o.live for n in names_in(o.expression)}
    given = {n: v for n, v in quantities.items() if n in used}
    context.quantities = given
    others: list[SignatureOutcome] = []
    for part, misconception_id, expression in linked(item, library, listed=False):
        names = names_in(expression)
        if names and set(names) <= set(given):
            outcome = SignatureOutcome(str(part["id"]), misconception_id, expression, False)
            _judge(outcome, part, context, symbols)
            others.append(outcome)
    sources = {n: sorted(binding.sources.get(n, set())) for n in given}
    how = {k: v if v is not None else NO_RECOMPUTE for k, v in context.recompute.items()}
    return ItemOutcome(str(item["id"]), given, outcomes, others, symbols, how, sources, sorted(long_names))


HARMFUL = (SCALE, NOT_IN_KEY, NO_RECOMPUTE)


def blocking(outcome: ItemOutcome) -> list[str]:
    """Why the item must not be given its quantities although a signature can fire: another signature the marker
    would try on it (listed by a part, or of the item's KCs) becomes evaluable over the quantities but could not be
    verified (it would need a converted quantity, uses a quantity the key's computation does not, or the key does
    not recompute). A signature equal to the key, or one with no value, is not blocking: the marker never names a
    misconception for a transform that gives the key or none."""
    names = set(outcome.quantities)
    return [f"{s.part_id} {s.misconception_id} {s.expression}: {s.reason}"
            for s in [*outcome.signatures, *outcome.kc_signatures]
            if not s.live and s.reason in HARMFUL and set(names_in(s.expression)) <= names]  # fmt: skip


# --- a run over items -----------------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What a run would write, and the report of everything it would not."""

    outcomes: list[ItemOutcome]
    write: dict[str, dict[str, str]]  # item id: the quantities it is to carry (new or changed)
    unchanged: list[str]
    blocked: dict[str, list[str]]
    long_names: dict[str, list[str]]  # item id: names waiting for the contract to widen; the item is left out
    skipped: list[str]  # records the pipeline does not edit (held out, retired)

    @property
    def carried(self) -> set[str]:
        return set(self.write) | set(self.unchanged)

    def report(self) -> dict[str, Any]:
        carried = self.carried
        live = {f"{s.misconception_id} :: {s.expression}" for o in self.outcomes if o.item_id in carried
                for s in o.live}  # fmt: skip
        kc_live = {f"{s.misconception_id} :: {s.expression}" for o in self.outcomes if o.item_id in carried
                   for s in o.kc_signatures if s.live}  # fmt: skip
        reasons: dict[str, int] = {}
        not_live: list[dict[str, str]] = []
        for outcome in self.outcomes:
            left_out = "long names" if outcome.item_id in self.long_names else "blocked"
            for s in outcome.signatures:
                if s.live and outcome.item_id in carried:
                    continue
                reason = s.reason or f"the item is left out ({left_out})"
                reasons[_reason_class(reason)] = reasons.get(_reason_class(reason), 0) + 1
                not_live.append({"item": outcome.item_id, "part": s.part_id, "misconception": s.misconception_id,
                                 "expression": s.expression, "reason": reason})  # fmt: skip
        return {
            "items_with_signatures": len(self.outcomes),
            "items_given_quantities": len(carried),
            "write": self.write,
            "unchanged": sorted(self.unchanged),
            "signatures_live": sorted(live),
            "kc_signatures_live": sorted(kc_live - live),
            "blocked": self.blocked,
            "long_names": self.long_names,
            "skipped": sorted(self.skipped),
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "evidence": {
                o.item_id: {"sources": o.sources, "recompute": o.recompute}
                for o in self.outcomes
                if o.item_id in carried
            },
            "not_live": not_live,
        }


def _reason_class(reason: str) -> str:
    """A reason without the symbol it names, so `m: <reason>` counts with every other symbol's `<reason>`."""
    head, _, rest = reason.partition(": ")
    if rest and _NAME.fullmatch(head):
        return "symbol: " + rest.split("; ")[0]
    return reason


EDITABLE = ("drafted", "verified", "in_eval_bank", "published")  # never a held-out or retired record


def plan(items: Iterable[Mapping[str, Any]], library: Mapping[str, Mapping[str, Any]],
         settings: Settings | None = None, proposals: Mapping[str, Mapping[str, Any]] | None = None
         ) -> Plan:  # fmt: skip
    """Extract and verify every item whose numeric parts list an `answer_transform` signature; `proposals` holds a
    model's proposal per item id (`pack_builder.proposals`), verified like every other reading."""
    settings = settings or Settings()
    proposals = proposals or {}
    result = Plan([], {}, [], {}, {}, [])
    for item in items:
        if not linked(item, library):
            continue
        if (item.get("review") or {}).get("status", "drafted") not in EDITABLE:
            result.skipped.append(str(item["id"]))
            continue
        outcome = extract(item, library, settings, proposals.get(str(item["id"])))
        result.outcomes.append(outcome)
        if outcome.long_names:
            result.long_names[outcome.item_id] = outcome.long_names
            continue
        if not outcome.quantities:
            continue
        problems = blocking(outcome)
        if problems:
            result.blocked[outcome.item_id] = problems
            continue
        if dict(item.get("quantities") or {}) == outcome.quantities:
            result.unchanged.append(outcome.item_id)
        else:
            result.write[outcome.item_id] = dict(sorted(outcome.quantities.items()))
    return result


def unbound(outcome: ItemOutcome) -> list[str]:
    """The symbols no reading of the item binds, of the listed signatures that fail only for want of them: what a
    model may be asked to propose (`pack_builder.proposals`)."""
    names: set[str] = set()
    for signature in outcome.signatures:
        if signature.live or not signature.reason:
            continue
        reasons = [r.partition(": ") for r in signature.reason.split("; ")]
        if all(rest == UNBOUND for _, _, rest in reasons):
            names |= {head for head, _, _ in reasons}
    return sorted(names)
