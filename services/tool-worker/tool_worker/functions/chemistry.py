"""Formula masses and equation balancing (VER-18, VER-19).

`chem_molar_mass` reads a formula with our own grammar (`formula.py`) and sums count * relative atomic mass with one
line of working per element, in order of first appearance; relative atomic masses come from the stated constants
(the pack's convention, for example Cl = 35.5) or, when none is stated, the standard atomic weights of the chemistry
default table, which always warns (`elements.py`).

`chem_balance` builds the element-count matrix (reactant columns positive, product columns negative, with charge as
an extra row when any species is charged) and takes its rational null space with SymPy. One dimension with every
entry of one sign gives the smallest positive integer coefficients and the atom-count table; no null space is a
`DOMAIN_ERROR` (the formulas cannot balance); a null vector that needs a zero or a species moved to the other side is
a `DOMAIN_ERROR` too; more than one dimension is `unknown` (several independent reactions). Coefficients written in
the equation, or given as `coefficients`, are a claim: checked by the matrix product, and a balanced set that is a
multiple of the smallest one is `not_lowest_terms`."""

from __future__ import annotations

import re
from fractions import Fraction
from math import gcd, lcm
from typing import Any

import sympy as sp

from tool_worker.functions.elements import Constants, ConventionMissing
from tool_worker.functions.formula import Equation, Species, Term, parse_equation, parse_formula
from tool_worker.functions.maths import exact, significant
from tool_worker.functions.quantities import number
from tool_worker.functions.units import REGISTRY, dimension_text, parse_quantity, parse_unit
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
MOLAR_MASS_UNIT = "g mol⁻¹"
ARROW = {False: "→", True: "⇌"}


def plain(value: Fraction) -> str:
    """A value as a plain decimal: exact when it terminates, else 6 significant figures."""
    denominator = value.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator != 1:
        return significant(sp.Rational(value.numerator, value.denominator), 6)
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    places = 0
    while (magnitude * 10**places).denominator != 1:
        places += 1
    digits = str(int(magnitude * 10**places)).rjust(places + 1, "0")
    text = digits if places == 0 else f"{digits[:-places]}.{digits[-places:]}"
    return sign + text


def rounded_sf(value: Fraction, sf: int) -> str:
    """`value` to `sf` significant figures, half away from zero (VER-09)."""
    return significant(sp.Rational(value.numerator, value.denominator), sf)


def number_out(value: Fraction, unit: str | None = None) -> dict[str, Any]:
    """An exact value: text, LaTeX, 6 s.f. decimal, the plain decimal, and its unit when it has one."""
    out = exact(sp.Rational(value.numerator, value.denominator))
    out["shown"] = plain(value)
    if unit:
        out["unit"] = unit
    return out


# Kinds of quantity in the chemistry checkers: (unit values are held in, its display, default unit for a bare number).
KINDS: dict[str, tuple[str, str, str | None]] = {
    "mass": ("g", "g", "g"),
    "amount": ("mol", "mol", "mol"),
    "volume": ("dm^3", "dm³", None),  # a bare volume is refused: cm³ or dm³ is the classic slip
    "concentration": ("mol/dm^3", "mol dm⁻³", "mol/dm^3"),
    "charge": ("C", "C", "C"),
    "current": ("A", "A", "A"),
    "time": ("s", "s", "s"),
    "energy": ("J", "J", "J"),
    "temperature_change": ("K", "K", "K"),
    "specific_heat": ("J/(g*K)", "J g⁻¹ K⁻¹", "J/(g*K)"),
    "molar_energy": ("kJ/mol", "kJ mol⁻¹", "kJ/mol"),
    "molar_mass": ("g/mol", MOLAR_MASS_UNIT, "g/mol"),
}


def quantity(raw: Any, kind: str, field: str) -> Fraction:
    """A given value in the kind's unit: a quantity with its unit ("250 cm^3") converted exactly, or a bare number
    in the kind's default unit."""
    target, _, default = KINDS[kind]
    text = str(raw).strip() if isinstance(raw, str) else None
    if text is None or re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", text):
        if default is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"{field}: give the unit of the {kind} (for example cm^3 or dm^3)")
        value = number(raw, field)
        return Fraction(int(value.p), int(value.q))
    try:
        given = parse_quantity(text)
    except WorkerError:
        raise
    except Exception:  # Pint refuses some malformed units with its own errors
        raise WorkerError(Code.PARSE_REJECTED, f"{field}: not a quantity with a unit", position=0) from None
    try:
        converted = given.to(parse_unit(target)).magnitude
    except Exception:  # Pint's dimensionality error: name both dimensions
        expected = dimension_text(REGISTRY.Quantity(1, parse_unit(target)))
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: found {dimension_text(given)}, expected {expected}") from None
    return Fraction(converted)


def species_parse(text: str, field: str = "formula") -> Species:
    species = parse_formula(text)
    if species.electron:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: an electron is not a formula with a mass")
    return species


def formula_mass(species: Species, constants: Constants, check_id: str) -> tuple[Fraction, list[dict[str, Any]]]:
    """The relative formula mass and one working line per element (raises ConventionMissing)."""
    total = Fraction(0)
    lines = []
    for element, count in species.counts.items():
        ar = constants.ar(element)
        subtotal = ar * count
        total += subtotal
        lines.append({"element": element, "count": count, "ar": plain(ar), "subtotal": plain(subtotal),
                      "check_id": check_id})  # fmt: skip
    return total, lines


# --- chem_molar_mass (VER-18) ---------------------------------------------------------------------------------------


def chem_molar_mass(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    species = species_parse(args["formula"])
    constants = Constants.of(args, context)
    try:
        total, lines = formula_mass(species, constants, context.check_id)
    except ConventionMissing as missing:
        return missing.result()
    working = [
        {"label": line["element"], "value": line["subtotal"], "unit": MOLAR_MASS_UNIT, "check_id": context.check_id,
         "latex": f"{line['count']} \\times {line['ar']} = {line['subtotal']}"}
        for line in lines
    ]  # fmt: skip
    value = {
        "formula": species.text,
        "counts": dict(species.counts),
        "charge": species.charge,
        "state": species.state,
        "lines": [{k: v for k, v in line.items() if k != "check_id"} for line in lines],
        "relative_formula_mass": number_out(total),
        "molar_mass": number_out(total, MOLAR_MASS_UNIT),
    }
    return value, {"method": "table_lookup", "confidence": "proved", "working": working, **constants.extras()}


# --- chem_balance (VER-19) ------------------------------------------------------------------------------------------


def _matrix(terms: list[Term]) -> tuple[list[str], sp.Matrix, bool]:
    """Rows: elements in order of first appearance, then charge when any species is charged; reactants positive."""
    elements: list[str] = []
    for term in terms:
        elements += [e for e in term.species.counts if e not in elements]
    ionic = any(term.species.charge for term in terms)
    rows = []
    for element in elements:
        rows.append([term.species.counts.get(element, 0) * (1 if term.side == "reactants" else -1) for term in terms])
    if ionic:
        rows.append([term.species.charge * (1 if term.side == "reactants" else -1) for term in terms])
    return elements, sp.Matrix(rows), ionic


def _table(terms: list[Term], coefficients: list[Fraction], elements: list[str], ionic: bool) -> Extras:
    """Atom counts (and charge) on each side for the given coefficients."""

    def side(name: str, pick: Any) -> Fraction:
        return sum((c * pick(t.species) for t, c in zip(terms, coefficients, strict=True) if t.side == name),
                   Fraction(0))  # fmt: skip

    rows = []
    for element in elements:
        left = side("reactants", lambda s, e=element: s.counts.get(e, 0))
        right = side("products", lambda s, e=element: s.counts.get(e, 0))
        rows.append({"element": element, "reactants": _num(left), "products": _num(right)})
    out: Extras = {"atom_counts": rows}
    if ionic:
        charge = {"reactants": _num(side("reactants", lambda s: s.charge)),
                  "products": _num(side("products", lambda s: s.charge))}  # fmt: skip
        out["charge"] = charge
    return out


def _num(value: Fraction) -> int | str:
    return int(value) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _written(terms: list[Term], coefficients: list[Fraction], reversible: bool, conditions: str | None = None) -> str:
    def one(term: Term, c: Fraction) -> str:
        shown = "" if c == 1 else (str(c.numerator) if c.denominator == 1 else f"{c.numerator}/{c.denominator} ")
        return f"{shown}{term.species.text}"

    pairs = list(zip(terms, coefficients, strict=True))
    left = " + ".join(one(t, c) for t, c in pairs if t.side == "reactants")
    right = " + ".join(one(t, c) for t, c in pairs if t.side == "products")
    annotation = f"[{conditions}]" if conditions is not None else ""
    return f"{left} {ARROW[reversible]}{annotation} {right}"


def smallest(vector: list[Fraction]) -> list[int]:
    """A null vector of one sign as its smallest positive integers."""
    sign = 1 if vector[0] > 0 else -1
    common = lcm(*(v.denominator for v in vector))
    integers = [int(v * common) * sign for v in vector]
    divisor = gcd(*integers)
    return [n // divisor for n in integers]


def balance(equation: Equation) -> tuple[list[int] | None, str | None, list[str], bool]:
    """(smallest coefficients, or None with the reason a balance is undecided), the element order and ionic flag.
    Raises DOMAIN_ERROR when the formulas cannot balance."""
    terms = equation.terms
    elements, matrix, ionic = _matrix(terms)
    basis = matrix.nullspace()
    if not basis:
        raise WorkerError(Code.DOMAIN_ERROR, "cannot be balanced; check the formulas", reason="no_solution")
    if len(basis) > 1:
        return None, "several_independent_reactions", elements, ionic
    vector = [Fraction(int(sp.fraction(v)[0]), int(sp.fraction(v)[1])) for v in basis[0]]
    if any(v == 0 for v in vector):
        names = [t.species.text for t, v in zip(terms, vector, strict=True) if v == 0]
        raise WorkerError(Code.DOMAIN_ERROR, f"cannot be balanced with {', '.join(names)} taking part; check the "
                          "formulas", reason="species_not_reacting")  # fmt: skip
    if len({v > 0 for v in vector}) > 1:
        raise WorkerError(Code.DOMAIN_ERROR, "cannot be balanced with every species on its written side; check the "
                          "formulas", reason="wrong_side")  # fmt: skip
    return smallest(vector), None, elements, ionic


def _claim(args: dict[str, Any], equation: Equation) -> list[Fraction] | None:
    """The coefficients being checked: the `coefficients` argument, else those written in the equation."""
    if "coefficients" in args:
        given = [Fraction(str(c)) for c in args["coefficients"]]
        if len(given) != len(equation.terms):
            raise WorkerError(Code.DOMAIN_ERROR, f"{len(given)} coefficients for {len(equation.terms)} species")
        if any(c <= 0 for c in given):
            raise WorkerError(Code.DOMAIN_ERROR, "coefficients must be positive")
        return given
    if equation.written:
        return [t.coefficient if t.coefficient is not None else Fraction(1) for t in equation.terms]
    return None


def chem_balance(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    equation = parse_equation(args["equation"])
    terms = equation.terms
    coefficients, reason, elements, ionic = balance(equation)
    value: dict[str, Any] = {"species": [{"formula": t.species.text, "side": t.side} for t in terms],
                             "reversible": equation.reversible}  # fmt: skip
    extras: Extras = {"method": "exact", "confidence": "proved"}
    if coefficients is None:
        value.update({"status": "unknown", "reason": reason})
        extras["confidence"] = "undecided"
    else:
        exact_coefficients = [Fraction(c) for c in coefficients]
        value.update({"status": "balanced", "coefficients": coefficients,
                      "equation": _written(terms, exact_coefficients, equation.reversible, equation.conditions),
                      **_table(terms, exact_coefficients, elements, ionic)})  # fmt: skip
        for entry, c in zip(value["species"], coefficients, strict=True):
            entry["coefficient"] = c
        extras["working"] = [
            {"label": row["element"], "value": f"{row['reactants']} = {row['products']}", "check_id": context.check_id}
            for row in value["atom_counts"]
        ]
    claim = _claim(args, equation)
    if claim is not None:
        value["student"] = _check_claim(
            terms, claim, coefficients, elements, ionic, equation.reversible, equation.conditions
        )
    return value, extras


def _check_claim(terms: list[Term], claim: list[Fraction], smallest_set: list[int] | None, elements: list[str],
                 ionic: bool, reversible: bool, conditions: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A student's coefficients checked by the matrix product (each row's sum is zero when balanced)."""
    _, matrix, _ = _matrix(terms)
    product = matrix * sp.Matrix([sp.Rational(c.numerator, c.denominator) for c in claim])
    balanced = all(v == 0 for v in product)
    out: dict[str, Any] = {
        "coefficients": [_num(c) for c in claim],
        "balanced": balanced,
        "equation": _written(terms, claim, reversible, conditions),
        **_table(terms, claim, elements, ionic),
    }
    if not balanced:
        names = [*elements, *(["charge"] if ionic else [])]
        out["verdict"] = "not_balanced"
        out["unbalanced"] = [name for name, v in zip(names, product, strict=True) if v != 0]
    elif smallest_set is None:
        out["verdict"] = "balanced"  # several independent reactions: lowest terms is undefined
    else:
        factor = claim[0] / smallest_set[0]
        out["verdict"] = "correct" if factor == 1 else "not_lowest_terms"
        if factor != 1:
            out["factor"] = _num(factor)
    return out
