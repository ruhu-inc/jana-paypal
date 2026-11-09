"""Enthalpy changes (VER-21): from calorimetry (Q = m c ΔT, then per mole) and from bond energies.

Sign convention, stated in every result: ΔH is negative for an exothermic reaction. In calorimetry the water (or
solution) takes in Q = m c ΔT; when it warms, the reaction gave the heat out, so ΔH = -Q / n. From bond energies,
ΔH = Σ(bonds broken) - Σ(bonds formed): breaking takes energy in, forming gives it out.

The specific heat capacity follows the precedence of VER-12 (question, item, pack) and has no tool default, so a
calorimetry check with none stated is `unknown` with `convention_missing`. Bond energies are data from the question
or the pack and come with each bond. A temperature change in °C is the same change in K; separate initial and final
temperatures give ΔT = final - initial. The amount reacting is given, or found from a fuel's mass and formula."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

from tool_worker.functions.chemistry import formula_mass, number_out, plain, quantity, rounded_sf, species_parse
from tool_worker.functions.elements import Constants, ConventionMissing
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
KJ_PER_MOL = "kJ mol⁻¹"
CONVENTION = "exothermic_negative"
_CELSIUS = re.compile(r"\s*(°\s*C|degC|celsius)\s*$", re.I)
_KELVIN = re.compile(r"\s*K\s*$")


def _nature(delta_h: Fraction) -> str:
    return "exothermic" if delta_h < 0 else ("endothermic" if delta_h > 0 else "thermoneutral")


def _temperature(raw: Any, field: str) -> tuple[Fraction, str]:
    """A temperature reading as (value, unit), the unit 'C' or 'K'; a bare number is a Celsius reading."""
    text = str(raw).strip()
    unit = "C"
    if _CELSIUS.search(text):
        text = _CELSIUS.sub("", text)
    elif _KELVIN.search(text):
        text, unit = _KELVIN.sub("", text), "K"
    try:
        return Fraction(text), unit
    except ValueError:
        raise WorkerError(Code.PARSE_REJECTED, f"{field}: not a temperature", position=0) from None


def _change(args: dict[str, Any]) -> Fraction:
    if "temperature_change" in args:
        raw = args["temperature_change"]
        if isinstance(raw, str) and _CELSIUS.search(raw):  # a change of 1 °C is a change of 1 K
            raw = _CELSIUS.sub(" K", raw)
        return quantity(raw, "temperature_change", "temperature_change")
    if "initial_temperature" in args and "final_temperature" in args:
        start, start_unit = _temperature(args["initial_temperature"], "initial_temperature")
        end, end_unit = _temperature(args["final_temperature"], "final_temperature")
        if start_unit != end_unit:
            raise WorkerError(Code.DOMAIN_ERROR, "give both temperatures in the same unit")
        return end - start
    raise WorkerError(Code.DOMAIN_ERROR, "give temperature_change, or initial_temperature and final_temperature")


def chem_energetics(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    task = args.get("task") or ("bond_energies" if "broken" in args or "formed" in args else "calorimetry")
    sf = int(args.get("sf", context.setting("default_sf", 3)))
    if task == "bond_energies":
        return _bonds(args, context, sf)
    extra = []
    if "specific_heat" in args:
        value = quantity(args["specific_heat"], "specific_heat", "specific_heat")
        extra.append({"symbol": "c", "value": plain(value), "unit": "J/(g*K)",
                      "source": args.get("specific_heat_source", "question")})  # fmt: skip
    constants = Constants.of(args, context, extra)
    try:
        return _calorimetry(args, context, constants, sf)
    except ConventionMissing as missing:
        return missing.result()


def _calorimetry(args: dict[str, Any], context: CallContext, constants: Constants, sf: int) -> tuple[Any, Extras]:
    check = context.check_id
    if "mass" not in args:
        raise WorkerError(Code.DOMAIN_ERROR, "give the mass of the water or solution heated (1 cm³ of water is 1 g by "
                          "the usual convention)")  # fmt: skip
    mass = quantity(args["mass"], "mass", "mass")
    if mass <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "the mass must be positive")
    change = _change(args)
    c = constants.value("c")
    heat = mass * c * change  # J taken in by the water
    working = [{"label": "Q = m c ΔT", "value": plain(heat), "unit": "J", "check_id": check}]
    value: dict[str, Any] = {
        "q": number_out(heat, "J"),
        "q_kj": number_out(heat / 1000, "kJ"),
        "temperature_change": number_out(change, "K"),
        "sign_convention": CONVENTION,
    }
    amount = None
    if "amount" in args:
        amount = quantity(args["amount"], "amount", "amount")
    elif "fuel_mass" in args and "formula" in args:
        molar, _ = formula_mass(species_parse(args["formula"]), constants, check)
        amount = quantity(args["fuel_mass"], "mass", "fuel_mass") / molar
        working.append({"label": f"M({args['formula']})", "value": plain(molar), "unit": "g mol⁻¹", "check_id": check})
    if amount is not None:
        if amount <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, "the amount must be positive")
        delta_h = -heat / amount / 1000
        working += [
            {"label": "n", "value": plain(amount), "unit": "mol", "check_id": check},
            {"label": "ΔH = \u2212Q / n", "value": plain(delta_h), "unit": KJ_PER_MOL, "check_id": check},
        ]
        value["amount"] = number_out(amount, "mol")
        value["delta_h"] = {**number_out(delta_h, KJ_PER_MOL), "rounded": rounded_sf(delta_h, sf)}
    nature = _nature(-heat)
    value["nature"] = nature
    value["statement"] = {
        "exothermic": "The water warmed, so the reaction gave out heat: exothermic, ΔH negative.",
        "endothermic": "The water cooled, so the reaction took in heat: endothermic, ΔH positive.",
        "thermoneutral": "No temperature change: no heat was transferred.",
    }[nature]
    extras = {"method": "exact", "confidence": "proved", "working": working, **constants.extras()}
    return value, extras


def _bonds(args: dict[str, Any], context: CallContext, sf: int) -> tuple[Any, Extras]:
    check = context.check_id
    working = []
    totals = {}
    for side in ("broken", "formed"):
        total = Fraction(0)
        for n, bond in enumerate(args.get(side, [])):
            energy = quantity(bond["energy"], "molar_energy", f"{side}[{n}].energy")
            if energy <= 0:
                raise WorkerError(Code.DOMAIN_ERROR, f"{side}[{n}]: a bond energy is positive")
            subtotal = energy * int(bond.get("count", 1))
            total += subtotal
            working.append({"label": f"{side}: {bond.get('count', 1)} \u00d7 {bond['bond']}", "value": plain(subtotal),
                            "unit": KJ_PER_MOL, "check_id": check})  # fmt: skip
        totals[side] = total
    if not args.get("broken") or not args.get("formed"):
        raise WorkerError(Code.DOMAIN_ERROR, "give the bonds broken and the bonds formed")
    delta_h = totals["broken"] - totals["formed"]
    working.append(
        {"label": "ΔH = broken \u2212 formed", "value": plain(delta_h), "unit": KJ_PER_MOL, "check_id": check}
    )
    nature = _nature(delta_h)
    value = {
        "broken": number_out(totals["broken"], KJ_PER_MOL),
        "formed": number_out(totals["formed"], KJ_PER_MOL),
        "delta_h": {**number_out(delta_h, KJ_PER_MOL), "rounded": rounded_sf(delta_h, sf)},
        "nature": nature,
        "sign_convention": CONVENTION,
        "statement": "Breaking bonds takes energy in and forming bonds gives it out; ΔH = broken \u2212 formed, "
        "negative when more energy is given out (exothermic).",
    }
    return value, {"method": "exact", "confidence": "proved", "working": working}
