"""Our own grammar for chemical formulae and equations (VER-18, VER-19; VER-03: never `eval`).

A formula is element symbols with counts, nested `( )` and `[ ]` groups with multipliers, hydrate parts after a dot
(`CuSO4·5H2O`, also `.`, `*` or `•`), an optional charge and an optional state symbol `(s)`, `(l)`, `(g)` or `(aq)`.
Counts may be written as subscript digits. A charge is written after `^` (`SO4^2-`, `Fe^{3+}`, `OH^-`), in
superscript characters (`SO₄²⁻`), or as bare trailing signs with no digit before them (`Na+`, `Cl-`). Digits directly
before a bare sign are refused as ambiguous: `Cu2+` could mean Cu₂⁺ or Cu²⁺, and the checker never guesses. The
electron is `e-` (also `e^-` or `e⁻`).

An equation has exactly one arrow (`->`, `→`, `=`; `⇌` or `<=>` when reversible) and species separated by `+`.
When a side has a `+` after a space, only such a `+` separates (`Na+ + Cl-`); with no spaces it separates when the
next character can begin a species and it does not follow `^` (`H2+O2`). Each species may carry a leading
coefficient (an integer, a fraction such as `1/2` or a decimal)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from tool_worker.functions.elements import ELEMENTS
from tool_worker.protocol import Code, WorkerError

STATES = ("s", "l", "g", "aq")
SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
HYDRATE_DOTS = "·•⋅.*"
MAX_DEPTH = 8
MAX_COUNT = 999
MAX_ATOMS = 100_000
CLOSE = {"(": ")", "[": "]"}
_STATE = re.compile(r"\s*\((s|l|g|aq)\)$")
_CARET = re.compile(r"\^\s*(?:\{\s*)?(?:(\d*)\s*([+-])|([+-])\s*(\d*))\s*\}?$")
_SUPER = re.compile(r"([⁰¹²³⁴⁵⁶⁷⁸⁹]*)([⁺⁻])$")
_BARE = re.compile(r"([+-]+)$")
_ELECTRON = re.compile(r"^e\s*(?:\^\s*\{?\s*-\s*\}?|-|⁻)$")
_ARROWS = re.compile(r"(?P<arrow><=>|<->|⇌|⇋|-+>|→|⟶|=>|=)(?:\[(?P<conditions>[A-Za-z ]{1,40})\])?")
_COEFFICIENT = re.compile(r"^(\d+/\d+|\d*\.\d+|\d+)\s*(?=[A-Z(\[e])")


@dataclass(frozen=True)
class Species:
    """One formula: element counts in order of first appearance, charge, state symbol."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)
    charge: int = 0
    state: str | None = None
    electron: bool = False


def _reject(message: str, position: int) -> WorkerError:
    return WorkerError(Code.PARSE_REJECTED, message, position=position)


def parse_formula(text: str, offset: int = 0) -> Species:
    """One formula; errors are `PARSE_REJECTED` with the position of the first error in `text` (plus `offset`)."""
    lead = len(text) - len(text.lstrip())
    body = text.strip()
    start = offset + lead
    if not body:
        raise _reject("an empty formula", start)
    state = None
    found = _STATE.search(body)
    if found:
        state, body = found.group(1), body[: found.start()]
    if _ELECTRON.match(body):
        return Species(text.strip(), {}, -1, state, electron=True)
    body, charge = _charge(body, start)
    if not body:
        raise _reject("a charge with no formula", start)
    if body[0].isdigit():
        raise _reject("a formula does not start with a number", start)
    counts: dict[str, int] = {}
    pieces, last = [], 0
    for dot in re.finditer(r"\s*[" + re.escape(HYDRATE_DOTS) + r"]\s*", body):
        pieces.append((last, body[last : dot.start()]))
        last = dot.end()
    pieces.append((last, body[last:]))
    for n, (at, part) in enumerate(pieces):
        if not part:
            raise _reject("an empty part of a hydrate", start + at)
        multiplier, skip = 1, 0
        digits = re.match(r"\d+", part)
        if digits and n > 0:
            multiplier, skip = int(digits.group(0)), digits.end()
            if multiplier < 1 or multiplier > MAX_COUNT:
                raise _reject("a hydrate multiplier must be between 1 and 999", start + at)
        parser = _Parser(part[skip:].translate(SUBSCRIPTS), start + at + skip)
        for element, count in parser.parse().items():
            counts[element] = counts.get(element, 0) + count * multiplier
    if sum(counts.values()) > MAX_ATOMS:
        raise WorkerError(Code.LIMIT_EXCEEDED, "the formula has too many atoms", limit="components")
    return Species(text.strip(), counts, charge, state)


def _charge(body: str, start: int) -> tuple[str, int]:
    """The formula without its charge, and the charge."""
    caret = _CARET.search(body)
    if caret:
        digits = caret.group(1) if caret.group(2) else caret.group(4)
        sign = caret.group(2) or caret.group(3)
        size = int(digits) if digits else 1
        return body[: caret.start()].rstrip(), size if sign == "+" else -size
    sup = _SUPER.search(body)
    if sup:
        size = int(sup.group(1).translate(SUPERSCRIPTS)) if sup.group(1) else 1
        return body[: sup.start()], size if sup.group(2) == "⁺" else -size
    bare = _BARE.search(body)
    if bare:
        before = body[: bare.start()]
        if before and before[-1] in "0123456789":  # a subscript digit (₄) before the sign is unambiguous
            raise _reject("ambiguous charge: write it after ^, for example Cu^2+ or NH4^+", start + bare.start())
        signs = bare.group(1)
        if len(set(signs)) > 1:
            raise _reject("a charge has mixed signs", start + bare.start())
        return before, len(signs) if signs[0] == "+" else -len(signs)
    return body, 0


class _Parser:
    """Recursive descent over one hydrate part: elements, counts and bracketed groups."""

    def __init__(self, text: str, offset: int) -> None:
        self.text = text
        self.i = 0
        self.offset = offset

    def parse(self) -> dict[str, int]:
        counts = self.sequence(0, None)
        if self.i < len(self.text):
            raise _reject(f"unexpected {self.text[self.i]!r}", self.offset + self.i)
        if not counts:
            raise _reject("no elements in the formula", self.offset)
        return counts

    def sequence(self, depth: int, closing: str | None) -> dict[str, int]:
        counts: dict[str, int] = {}
        while self.i < len(self.text):
            char = self.text[self.i]
            if char == closing:
                return counts
            if char in CLOSE:
                if depth >= MAX_DEPTH:
                    raise WorkerError(Code.LIMIT_EXCEEDED, "brackets nested too deeply", limit="components")
                opened = self.i
                self.i += 1
                inner = self.sequence(depth + 1, CLOSE[char])
                if self.i >= len(self.text):
                    raise _reject(f"{char!r} is never closed", self.offset + opened)
                self.i += 1
                if not inner:
                    raise _reject("empty brackets", self.offset + opened)
                times = self.count()
                for element, count in inner.items():
                    counts[element] = counts.get(element, 0) + count * times
            elif char.isupper():
                element = self.element()
                counts[element] = counts.get(element, 0) + self.count()
            else:
                raise _reject(f"unexpected {char!r}", self.offset + self.i)
        return counts  # the end of the text: an unclosed group is reported by the caller

    def element(self) -> str:
        two = self.text[self.i : self.i + 2]
        if len(two) == 2 and two[1].islower():
            if two in ELEMENTS:
                self.i += 2
                return two
            raise _reject(f"unknown element {two!r}", self.offset + self.i)
        if self.text[self.i] in ELEMENTS:
            self.i += 1
            return self.text[self.i - 1]
        raise _reject(f"unknown element {self.text[self.i]!r}", self.offset + self.i)

    def count(self) -> int:
        digits = re.match(r"\d+", self.text[self.i :])
        if not digits:
            return 1
        value = int(digits.group(0))
        if value < 1 or value > MAX_COUNT:
            raise _reject("a count must be between 1 and 999", self.offset + self.i)
        self.i += digits.end()
        return value


# --- equations ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    """One species of an equation with its written coefficient (None when none is written) and side."""

    species: Species
    side: str  # reactants | products
    coefficient: Fraction | None
    position: int


@dataclass(frozen=True)
class Equation:
    terms: list[Term]
    reversible: bool
    conditions: str | None = None  # inert presentation metadata, never part of the element/charge matrix

    @property
    def written(self) -> bool:
        """Whether any coefficient is written (the equation is then a claim to check)."""
        return any(t.coefficient is not None for t in self.terms)


def parse_equation(text: str) -> Equation:
    arrows = list(_ARROWS.finditer(text))
    if len(arrows) != 1:
        position = arrows[1].start() if len(arrows) > 1 else len(text)
        raise _reject("an equation needs exactly one arrow (->, = or <=>)", position)
    arrow = arrows[0]
    reversible = arrow.group("arrow") in ("<=>", "<->", "⇌", "⇋")
    terms: list[Term] = []
    for side, (begin, end) in (("reactants", (0, arrow.start())), ("products", (arrow.end(), len(text)))):
        chunks = _split(text, begin, end)
        if not chunks:
            raise _reject(f"no {side}", begin)
        for chunk_start, chunk in chunks:
            terms.append(_term(chunk, chunk_start, side))
    return Equation(terms, reversible, arrow.group("conditions"))


def _split(text: str, begin: int, end: int) -> list[tuple[int, str]]:
    """The species texts of one side with their positions."""
    side = text[begin:end]
    if re.search(r"\s\+", side):  # spaced: only a '+' after a space separates ("Na+ + Cl-")
        cuts = [m for m in re.finditer(r"\+", side) if m.start() > 0 and side[m.start() - 1].isspace()]
    else:
        cuts = [m for m in re.finditer(r"\+(?=[0-9A-Z(\[])", side) if m.start() > 0 and side[m.start() - 1] != "^"]
    pieces, last = [], 0
    for cut in cuts:
        pieces.append((begin + last, side[last : cut.start()]))
        last = cut.end()
    pieces.append((begin + last, side[last:]))
    out = []
    for position, piece in pieces:
        if not piece.strip():
            raise _reject("a '+' with no species after it", position)
        out.append((position, piece))
    return out


def _term(chunk: str, position: int, side: str) -> Term:
    lead = len(chunk) - len(chunk.lstrip())
    body = chunk.strip()
    coefficient = None
    found = _COEFFICIENT.match(body)
    if found:
        coefficient = Fraction(found.group(1))
        if coefficient <= 0:
            raise _reject("a coefficient must be positive", position + lead)
        body = body[found.end() :]
        lead += found.end()
    return Term(parse_formula(body, position + lead), side, coefficient, position + lead)
