"""Stoichiometry chains (VER-20), with the checks they consume listed in the check record (VER-02 `inputs_from`).

A chain is a list of typed steps evaluated in order; a step's input is a given quantity (with its unit; a bare number
is in the step's default unit, except volumes, which need theirs) or `@id`, the value of an earlier step. Every
intermediate value comes back with its unit and a check id, as a step result and as lines of working: volumes
converted to dm³, molar masses (one line per formula), mole ratios from the balanced equation, and each step's value.

Molar masses and the balanced equation are recomputed here with the same grammar and constants as `chem_molar_mass`
and `chem_balance`, so the chain never trusts a number it did not check. A `chem_molar_mass` or `chem_balance` check
cited in `cites` is listed in `inputs_from` when the chain uses it, and its working lines carry that check's id; a
cited value that differs from the recomputed one is a `DOMAIN_ERROR` (`cited_check_differs`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from tool_worker.functions.chemistry import (
    KINDS,
    MOLAR_MASS_UNIT,
    balance,
    formula_mass,
    number_out,
    plain,
    quantity,
    rounded_sf,
    species_parse,
)
from tool_worker.functions.elements import Constants, ConventionMissing
from tool_worker.functions.formula import Equation, Species, parse_equation
from tool_worker.functions.units import parse_quantity, unit_text
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
Key = tuple[tuple[tuple[str, int], ...], int]
# op -> (inputs and their kinds, the kind of the step's value)
OPS: dict[str, tuple[dict[str, str], str]] = {
    "amount_from_mass": ({"mass": "mass"}, "amount"),
    "mass_from_amount": ({"amount": "amount"}, "mass"),
    "amount_from_concentration": ({"concentration": "concentration", "volume": "volume"}, "amount"),
    "concentration_from_amount": ({"amount": "amount", "volume": "volume"}, "concentration"),
    "amount_from_gas_volume": ({"volume": "volume"}, "amount"),
    "gas_volume_from_amount": ({"amount": "amount"}, "volume"),
    "mole_ratio": ({"amount": "amount"}, "amount"),
    "limiting_reagent": ({}, "amount"),
    "percentage_yield": ({}, "percent"),
    "purity": ({"pure_mass": "mass", "sample_mass": "mass"}, "percent"),
    "atom_economy": ({}, "percent"),
    "electrolysis": ({"current": "current", "time": "time"}, "amount"),
}
UNITS = {**{kind: display for kind, (_, display, _) in KINDS.items()}, "percent": "%"}
NEEDS = {"amount_from_mass": ("species",), "mass_from_amount": ("species",), "mole_ratio": ("from", "to"),
         "limiting_reagent": ("amounts",), "percentage_yield": ("actual", "theoretical"), "atom_economy": ("product",),
         "electrolysis": ("electrons",)}  # fmt: skip


def _key(species: Species) -> Key:
    return tuple(sorted(species.counts.items())), species.charge


@dataclass
class Value:
    amount: Fraction
    kind: str


@dataclass
class Chain:
    args: dict[str, Any]
    context: CallContext
    constants: Constants
    values: dict[str, Value] = field(default_factory=dict)
    working: list[dict[str, Any]] = field(default_factory=list)
    inputs_from: list[str] = field(default_factory=list)
    masses: dict[Key, Fraction] = field(default_factory=dict)
    equation: Equation | None = None
    coefficients: list[Fraction] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)

    # --- cited checks -------------------------------------------------------------------------------------------

    def _cite(self, tool: str, species: Species | None = None) -> dict[str, Any] | None:
        for cite in self.args.get("cites", []):
            if cite["check"] != tool:
                continue
            if species is None or _key(species_parse(cite.get("formula", ""), "cites.formula")) == _key(species):
                return dict(cite)
        return None

    def _consumed(self, check_id: str) -> None:
        if check_id not in self.inputs_from:
            self.inputs_from.append(check_id)

    def line(self, label: str, value: Fraction | str, unit: str | None, check_id: str | None = None) -> None:
        entry: dict[str, Any] = {"label": label, "value": value if isinstance(value, str) else plain(value),
                                 "check_id": check_id or self.context.check_id}  # fmt: skip
        if unit:
            entry["unit"] = unit
        self.working.append(entry)

    # --- molar masses and the equation ----------------------------------------------------------------------------

    def molar_mass(self, text: str, field_name: str) -> Fraction:
        species = species_parse(text, field_name)
        key = _key(species)
        if key in self.masses:
            return self.masses[key]
        total, _ = formula_mass(species, self.constants, self.context.check_id)
        cite = self._cite("chem_molar_mass", species)
        check_id = None
        if cite is not None:
            if "molar_mass" in cite and quantity(cite["molar_mass"], "molar_mass", "cites.molar_mass") != total:
                message = f"the cited molar mass of {text} ({cite['molar_mass']}) differs from {plain(total)}"
                raise WorkerError(Code.DOMAIN_ERROR, message, reason="cited_check_differs")
            check_id = cite["check_id"]
            self._consumed(check_id)
        self.line(f"M({species.text})", total, MOLAR_MASS_UNIT, check_id)
        self.masses[key] = total
        return total

    def balanced(self) -> tuple[Equation, list[Fraction]]:
        if self.equation is not None and self.coefficients is not None:
            return self.equation, self.coefficients
        if "equation" not in self.args:
            raise WorkerError(Code.DOMAIN_ERROR, "this step needs the equation")
        equation = parse_equation(self.args["equation"])
        smallest, reason, _, _ = balance(equation)
        written = [t.coefficient or Fraction(1) for t in equation.terms] if equation.written else None
        if written is not None and not _balances(equation, written):
            raise WorkerError(Code.DOMAIN_ERROR, "the equation as written is not balanced", reason="not_balanced")
        if smallest is None and written is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"the equation does not fix one set of coefficients ({reason}); "
                              "write them in the equation")  # fmt: skip
        coefficients = written if written is not None else [Fraction(c) for c in smallest or []]
        cite = self._cite("chem_balance")
        if cite is not None:
            cited = [Fraction(str(c)) for c in cite.get("coefficients", [])]
            if cited and (
                len(cited) != len(coefficients) or len({c / k for c, k in zip(cited, coefficients, strict=True)}) != 1
            ):
                raise WorkerError(Code.DOMAIN_ERROR, "the cited balanced equation differs from this one",
                                  reason="cited_check_differs")  # fmt: skip
            self._consumed(cite["check_id"])
        self.equation, self.coefficients = equation, coefficients
        return equation, coefficients

    def coefficient(self, text: str, field_name: str, side: str | None = None) -> Fraction:
        equation, coefficients = self.balanced()
        key = _key(species_parse(text, field_name))
        for term, c in zip(equation.terms, coefficients, strict=True):
            if _key(term.species) == key and (side is None or term.side == side):
                return c
        where = f"the {side} of the equation" if side else "the equation"
        raise WorkerError(Code.DOMAIN_ERROR, f"{field_name}: {text} is not in {where}")

    # --- inputs ---------------------------------------------------------------------------------------------------

    def given(self, step: dict[str, Any], name: str, kinds: tuple[str, ...]) -> Value:
        if name not in step:
            raise WorkerError(Code.DOMAIN_ERROR, f"step {step['id']}: {step['op']} needs {name}")
        raw = step[name]
        where = f"step {step['id']}.{name}"
        if isinstance(raw, str) and raw.startswith("@"):
            found = self.values.get(raw[1:])
            if found is None:
                raise WorkerError(Code.DOMAIN_ERROR, f"{where}: {raw} is not an earlier step")
            if found.kind not in kinds:
                raise WorkerError(Code.DOMAIN_ERROR, f"{where}: {raw} is a {found.kind}, expected {' or '.join(kinds)}")
            return found
        errors = []
        for kind in kinds:
            try:
                value = quantity(raw, kind, where)
            except WorkerError as error:
                errors.append(error)
                continue
            if kind == "volume" and unit_text(parse_quantity(str(raw)).units) != UNITS["volume"]:
                self.line(f"{name} in dm³", value, UNITS["volume"])  # volumes converted to dm³ (VER-20)
            return Value(value, kind)
        raise errors[0]

    # --- the steps ------------------------------------------------------------------------------------------------

    def run(self, step: dict[str, Any]) -> dict[str, Any]:
        op = step["op"]
        for needed in NEEDS.get(op, ()):
            if needed not in step:
                raise WorkerError(Code.DOMAIN_ERROR, f"step {step['id']}: {op} needs {needed}")
        inputs = {name: self.given(step, name, (kind,)) for name, kind in OPS[op][0].items()}
        extra: dict[str, Any] = {}
        result = getattr(self, f"_{op}")(step, {k: v.amount for k, v in inputs.items()}, extra)
        kind = "mass" if op == "electrolysis" and "species" in step else OPS[op][1]
        if op == "percentage_yield" and result > 100:
            self.warnings.append({"code": "implausible", "message": f"step {step['id']}: a yield over 100%"})
        self.values[step["id"]] = Value(result, kind)
        self.line(f"{step['id']}: {op.replace('_', ' ')}", result, UNITS[kind])
        sf = int(self.args.get("sf", self.context.setting("default_sf", 3)))
        return {"id": step["id"], "op": op, "value": number_out(result, UNITS[kind]),
                "rounded": rounded_sf(result, sf), "check_id": self.context.check_id, **extra}  # fmt: skip

    def _amount_from_mass(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        return v["mass"] / self.molar_mass(step["species"], f"step {step['id']}.species")

    def _mass_from_amount(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        return v["amount"] * self.molar_mass(step["species"], f"step {step['id']}.species")

    def _amount_from_concentration(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        return v["concentration"] * v["volume"]

    def _concentration_from_amount(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        _positive(v["volume"], step, "volume")
        return v["amount"] / v["volume"]

    def _amount_from_gas_volume(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        return v["volume"] / self._constant("Vm", "molar gas volume", "dm³ mol⁻¹")

    def _gas_volume_from_amount(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        return v["amount"] * self._constant("Vm", "molar gas volume", "dm³ mol⁻¹")

    def _mole_ratio(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        here = f"step {step['id']}"
        a = self.coefficient(step["from"], f"{here}.from")
        b = self.coefficient(step["to"], f"{here}.to")
        cite = self._cite("chem_balance")
        self.line(f"ratio {step['from']} : {step['to']}", f"{plain(a)} : {plain(b)}", None,
                  cite["check_id"] if cite else None)  # fmt: skip
        extra["ratio"] = [plain(a), plain(b)]
        return v["amount"] * b / a

    def _limiting_reagent(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        here = f"step {step['id']}"
        shares = {}
        for name in step["amounts"]:
            amount = self.given({"id": step["id"], "op": step["op"], name: step["amounts"][name]}, name, ("amount",))
            coefficient = self.coefficient(name, f"{here}.amounts", "reactants")
            shares[name] = (amount.amount, coefficient)
        if len(shares) < 2:
            raise WorkerError(Code.DOMAIN_ERROR, f"{here}: give the amounts of at least two reactants")
        limiting = min(shares, key=lambda n: (shares[n][0] / shares[n][1], list(shares).index(n)))
        extent = shares[limiting][0] / shares[limiting][1]
        extra["limiting"] = limiting
        extra["excess"] = {n: number_out(a - extent * c, "mol") for n, (a, c) in shares.items() if n != limiting}
        self.line(f"{step['id']}: limiting reagent", limiting, None)
        if "product" in step:
            extra["product"] = step["product"]
            return extent * self.coefficient(step["product"], f"{here}.product", "products")
        return shares[limiting][0]

    def _percentage_yield(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        # A reference fixes the kind (mass or amount); the other value is read in the same kind.
        first, second = (
            ("theoretical", "actual") if str(step["theoretical"]).startswith("@") else ("actual", "theoretical")
        )
        one = self.given(step, first, ("mass", "amount"))
        other = self.given(step, second, (one.kind,))
        actual, theoretical = (one, other) if first == "actual" else (other, one)
        _positive(theoretical.amount, step, "theoretical")
        return actual.amount / theoretical.amount * 100

    def _purity(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        _positive(v["sample_mass"], step, "sample_mass")
        if v["pure_mass"] > v["sample_mass"]:
            raise WorkerError(Code.DOMAIN_ERROR, f"step {step['id']}: the pure mass exceeds the sample's mass")
        return v["pure_mass"] / v["sample_mass"] * 100

    def _atom_economy(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        equation, coefficients = self.balanced()
        here = f"step {step['id']}"
        wanted = self.coefficient(step["product"], f"{here}.product", "products")
        useful = wanted * self.molar_mass(step["product"], f"{here}.product")
        total = sum((c * self.molar_mass(t.species.text, f"{here}.reactants")
                     for t, c in zip(equation.terms, coefficients, strict=True)
                     if t.side == "reactants" and not t.species.electron), Fraction(0))  # fmt: skip
        self.line(f"{step['id']}: mass of {step['product']} / mass of reactants", f"{plain(useful)} / {plain(total)}",
                  "g")  # fmt: skip
        return useful / total * 100

    def _electrolysis(self, step: dict[str, Any], v: dict[str, Fraction], extra: Extras) -> Fraction:
        charge = v["current"] * v["time"]
        self.line(f"{step['id']}: Q = I t", charge, "C")
        electrons = charge / self._constant("F", "Faraday constant", "C mol⁻¹")
        self.line(f"{step['id']}: amount of electrons", electrons, "mol")
        amount = electrons / int(step["electrons"])
        extra["charge"] = number_out(charge, "C")
        extra["electrons"] = number_out(electrons, "mol")
        if "species" in step:
            self.line(f"{step['id']}: amount of {step['species']}", amount, "mol")
            extra["amount"] = number_out(amount, "mol")
            return amount * self.molar_mass(step["species"], f"step {step['id']}.species")
        return amount

    def _constant(self, symbol: str, name: str, unit: str) -> Fraction:
        value = self.constants.value(symbol)
        if not any(w["label"] == name for w in self.working):
            self.line(name, value, unit)
        return value


def _positive(value: Fraction, step: dict[str, Any], name: str) -> None:
    if value <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, f"step {step['id']}: {name} must be positive")


def _balances(equation: Equation, coefficients: list[Fraction]) -> bool:
    totals: dict[str, Fraction] = {}
    for term, c in zip(equation.terms, coefficients, strict=True):
        sign = 1 if term.side == "reactants" else -1
        for element, count in [*term.species.counts.items(), ("charge", term.species.charge)]:
            totals[element] = totals.get(element, Fraction(0)) + sign * c * count
    return all(v == 0 for v in totals.values())


def chem_stoichiometry(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    ids = [step["id"] for step in args["steps"]]
    if len(set(ids)) != len(ids):
        raise WorkerError(Code.DOMAIN_ERROR, "step ids must be unique")
    chain = Chain(args, context, Constants.of(args, context))
    try:
        steps = [chain.run(step) for step in args["steps"]]
    except ConventionMissing as missing:
        return missing.result()
    value = {"steps": steps, "result": steps[-1]}
    if chain.equation is not None and chain.coefficients is not None:
        value["coefficients"] = [plain(c) for c in chain.coefficients]
    extras: Extras = {"method": "exact", "confidence": "proved", "working": chain.working, **chain.constants.extras()}
    if chain.warnings:
        extras["warnings"] = extras.get("warnings", []) + chain.warnings
    if chain.inputs_from:
        extras["inputs_from"] = chain.inputs_from
    return value, extras
