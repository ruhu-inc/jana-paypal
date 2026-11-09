"""Chemical equilibrium (VER-32, VER-38): `chem_equilibrium` on the science-compute contract.

The reaction is validated before anything is computed: unique species ids, atoms and charge balanced across the
coefficients, every gas or aqueous species given exactly one value, units that fit the basis (concentrations for Kc,
pressures of gases for Kp) and a volume for amounts in mol. Pure solids and liquids have activity 1 and take no value.

- `constant`: Kc (concentrations in mol/L) or Kp (pressures in Pa) as the product of product activities to their
  coefficients over the reactants'. The school unit convention, (mol/L)^Δn or Pa^Δn, is reported as the unit rather
  than presented as a dimensionless thermodynamic constant. A zero reactant activity has no finite constant: unknown.
- `extent`: the ICE table from initial values and a constant: the extent is solved exactly (SymPy real roots of the
  polynomial) within the interval that keeps every species non-negative, and the root's residual is checked. No
  admissible root and more than one are both unknown, never a guessed number.
- `electrode_potential`: E°cell = E°cathode - E°anode for the two standard reduction potentials supplied; a potential is
  never multiplied by a coefficient, and non-standard (Nernst) conditions are not supported.

No rate constant, time curve or temperature dependence is derived from a constant (VER-38): the temperature is recorded
with the constant, never used to move it."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions.formula import parse_formula
from tool_worker.functions.maths import slice_seconds
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
PURE = ("solid", "liquid")
CONCENTRATION = {"mol/L": Fraction(1), "mol/m3": Fraction(1, 1000)}
PRESSURE = {"Pa": Fraction(1), "kPa": Fraction(1000), "bar": Fraction(100000)}


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _unknown(reason: str) -> tuple[dict[str, Any], Extras]:
    return {"status": "unknown", "reason": reason}, {"method": "exact", "confidence": "undecided"}


class Reaction:
    """The validated reaction: signed coefficients (products positive) of the species that have an activity."""

    def __init__(self, data: dict[str, Any], basis: str) -> None:
        species = data["species"]
        ids = [s["id"] for s in species]
        if len(set(ids)) != len(ids):
            raise _domain("each species id appears once")
        atoms: dict[str, int] = {}
        charge = 0
        for s in species:
            parsed = parse_formula(s["formula"])
            sign = 1 if s["side"] == "product" else -1
            for element, count in parsed.counts.items():
                atoms[element] = atoms.get(element, 0) + sign * s["coefficient"] * count
            charge += sign * s["coefficient"] * parsed.charge
        unbalanced = sorted(e for e, n in atoms.items() if n)
        if unbalanced or charge:
            what = ", ".join(unbalanced) if unbalanced else "charge"
            raise _domain(f"the reaction is not balanced ({what})")
        self.variable = {s["id"]: (s["coefficient"] if s["side"] == "product" else -s["coefficient"])
                         for s in species if s["state"] not in PURE}  # fmt: skip
        self.formula = {s["id"]: s["formula"] for s in species}
        if basis == "Kp" and any(s["state"] == "aqueous" for s in species):
            raise _domain("Kp is written for gases: an aqueous species needs Kc")
        if not self.variable:
            raise _domain("the reaction needs a gas or aqueous species: pure phases alone have no constant")
        self.delta_n = sum(self.variable.values())

    def unit(self, basis: str) -> str:
        if self.delta_n == 0:
            return "no unit (the powers cancel)"
        return f"{'(mol/L)' if basis == 'Kc' else 'Pa'}^{self.delta_n}"

    def expression(self, basis: str) -> str:
        def term(sid: str, n: int) -> str:
            name = f"[{self.formula[sid]}]" if basis == "Kc" else f"p({self.formula[sid]})"
            return name if abs(n) == 1 else f"{name}^{abs(n)}"

        top = " ".join(term(s, n) for s, n in self.variable.items() if n > 0)
        bottom = " ".join(term(s, n) for s, n in self.variable.items() if n < 0)
        return f"{basis} = {top} / ({bottom})"


def _values(rows: list[dict[str, Any]], reaction: Reaction, basis: str, volume: float | None) -> dict[str, Fraction]:
    """Each gas or aqueous species' value in the basis unit (mol/L or Pa); pure phases are skipped."""
    seen: dict[str, Fraction] = {}
    for row in rows:
        sid, quantity = row["species_id"], row["quantity"]
        if sid not in reaction.formula:
            raise _domain(f"{sid} is not a species of the reaction")
        if sid in seen:
            raise _domain(f"{sid} has more than one value")
        if sid not in reaction.variable:
            continue  # a pure solid or liquid: activity 1
        amount, unit = Fraction(str(quantity["value"])), quantity["unit"]
        if basis == "Kc" and unit in CONCENTRATION:
            seen[sid] = amount * CONCENTRATION[unit]
        elif basis == "Kc" and unit == "mol":
            if volume is None:
                raise _domain("amounts in mol need volume_l")
            seen[sid] = amount / Fraction(str(volume))
        elif basis == "Kp" and unit in PRESSURE:
            seen[sid] = amount * PRESSURE[unit]
        else:
            raise _domain(f"{sid}: {unit} is not a {'concentration' if basis == 'Kc' else 'pressure'}")
    missing = sorted(set(reaction.variable) - set(seen))
    if missing:
        raise _domain(f"no value for {', '.join(missing)}")
    return seen


def _quotient(reaction: Reaction, activities: dict[str, Fraction]) -> tuple[Fraction, Fraction]:
    top, bottom = Fraction(1), Fraction(1)
    for sid, n in reaction.variable.items():
        if n > 0:
            top *= activities[sid] ** n
        else:
            bottom *= activities[sid] ** -n
    return top, bottom


def _checked(value: float, unit: str, context: CallContext) -> dict[str, Any]:
    return {"value": value, "unit": unit, "check_id": context.check_id}


def _constant(args: dict[str, Any], reaction: Reaction, context: CallContext) -> tuple[dict[str, Any], Extras]:
    basis = args["basis"]
    activities = _values(args["values"], reaction, basis, args.get("volume_l"))
    top, bottom = _quotient(reaction, activities)
    if bottom == 0:
        return _unknown("no_physical_root")  # a zero reactant activity: no finite constant, never infinity
    unit = reaction.unit(basis)
    working = [
        {"label": "expression", "value": reaction.expression(basis), "check_id": context.check_id},
        {"label": "unit convention", "value": f"school convention: {unit}", "check_id": context.check_id},
        {"label": "temperature", "value": str(args["temperature_k"]), "unit": "K", "check_id": context.check_id},
    ]
    value = {"status": "ok", "constant": _checked(float(top / bottom), unit, context)}
    return value, {"method": "exact", "confidence": "proved", "working": working}


def _extent(args: dict[str, Any], reaction: Reaction, context: CallContext) -> tuple[dict[str, Any], Extras]:
    basis = args["basis"]
    initial = _values(args["initial"], reaction, basis, args.get("volume_l"))
    if not any(n > 0 for n in reaction.variable.values()) or not any(n < 0 for n in reaction.variable.values()):
        return _unknown("unsupported_reaction")  # the extent needs a species with an activity on each side
    k = Fraction(str(args["constant"]))
    # Every species stays non-negative: products bound x from below, reactants from above.
    low = max(-initial[s] / n for s, n in reaction.variable.items() if n > 0)
    high = min(initial[s] / -n for s, n in reaction.variable.items() if n < 0)
    if low > high:
        return _unknown("no_physical_root")
    x = sp.Symbol("x", real=True)
    top, bottom = sp.Integer(1), sp.Integer(1)
    for sid, n in reaction.variable.items():
        level = sp.Rational(initial[sid].numerator, initial[sid].denominator) + n * x
        if n > 0:
            top *= level**n
        else:
            bottom *= level ** (-n)
    k_exact = sp.Rational(k.numerator, k.denominator)
    try:
        with time_slice(slice_seconds(context)):
            roots = sp.Poly(sp.expand(top - k_exact * bottom), x).real_roots()
            lo, hi = sp.Rational(low.numerator, low.denominator), sp.Rational(high.numerator, high.denominator)
            admissible = sorted({r for r in roots if lo <= r <= hi and bottom.subs(x, r) != 0}, key=float)
    except SliceExpired:
        return _unknown("budget_exceeded")
    if not admissible:
        return _unknown("no_physical_root")
    if len(admissible) > 1:
        return _unknown("ambiguous_roots")
    extent = admissible[0]
    residual = abs(float((top / bottom).subs(x, extent)) - float(k))
    if residual > 1e-9 * max(1.0, float(k)):
        return _unknown("no_physical_root")
    unit = "mol/L" if basis == "Kc" else "Pa"
    ice = [{"species_id": sid, "initial": float(initial[sid]), "change": float(n * extent),
            "equilibrium": max(0.0, float(initial[sid] + n * extent))}
           for sid, n in reaction.variable.items()]  # fmt: skip
    working = [{"label": "expression", "value": reaction.expression(basis), "check_id": context.check_id},
               {"label": "residual", "value": f"{residual:.3g}", "check_id": context.check_id}]  # fmt: skip
    value = {"status": "ok", "ice": ice, "extent": _checked(float(extent), unit, context)}
    return value, {"method": "symbolic", "confidence": "proved", "working": working}


def _electrode(args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    electrodes = args["electrode_potentials"]
    roles = {e["role"]: e for e in electrodes}
    if set(roles) != {"anode", "cathode"} or electrodes[0]["id"] == electrodes[1]["id"]:
        raise _domain("give one cathode and one anode, each a different half-cell")
    cell = Fraction(str(roles["cathode"]["standard_potential_v"])) - Fraction(
        str(roles["anode"]["standard_potential_v"])
    )
    working = [{"label": "E°cell = E°cathode - E°anode", "value": f"{float(cell)}", "unit": "V",
                "check_id": context.check_id}]  # fmt: skip
    return {"status": "ok", "cell_potential": _checked(float(cell), "V", context)}, {"method": "exact",
                                                                                     "confidence": "proved",
                                                                                     "working": working}  # fmt: skip


def chem_equilibrium(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    task = args["task"]
    if task == "electrode_potential":
        return _electrode(args, context)
    reaction = Reaction(args["reaction"], args["basis"])
    if task == "constant":
        return _constant(args, reaction, context)
    return _extent(args, reaction, context)
