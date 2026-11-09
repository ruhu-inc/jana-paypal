"""Element symbols, the chemistry default table and constant resolution for the chemistry checkers (VER-18, VER-20,
VER-21 with VER-12).

Element data: the 118 element symbols in atomic-number order, and the abridged standard atomic weights published by
the IUPAC Commission on Isotopic Abundances and Atomic Weights (CIAAW, ciaaw.org, abridged table of 2021, at most five
significant figures). Atomic weights are measured facts, re-keyed here by hand; no text or table layout is copied.
Elements with no standard atomic weight (no stable isotope with a characteristic terrestrial composition) have none,
so a check that needs one without a stated value is `unknown` with `convention_missing`. Registered as a shipped data
set in scripts/governance/shipped-assets.toml.

Constants follow the precedence of VER-12: the question, then the item's marking rule, then the pack's conventions,
then the tool default. The chemistry requirements name their own defaults (standard atomic weights for VER-18, a
molar gas volume of 24 dm³ mol⁻¹ at r.t.p. for VER-20, Kw = 1.0 * 10⁻¹⁴ at 298 K for VER-21), which TABLE VER-12a
version 1 does not carry; they live in this separate, versioned table, and reaching one always emits
`convention_default_used` naming it, so a pack that relies on them is visible in every check record (and fails its
build, VER-12). The Faraday constant, the specific heat capacity of water and Avogadro's constant have no default."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from tool_worker.functions.quantities import number
from tool_worker.functions.units import REGISTRY, parse_unit
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

SYMBOLS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr "
    "Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt "
    "Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc "
    "Lv Ts Og"
).split()
ELEMENTS = frozenset(SYMBOLS)

# CIAAW abridged standard atomic weights (2021), as decimal strings so they stay exact.
ATOMIC_WEIGHTS: dict[str, str] = dict(
    zip(
        "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr "
        "Nb Mo Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au "
        "Hg Tl Pb Bi Th Pa U".split(),
        "1.0080 4.0026 6.94 9.0122 10.81 12.011 14.007 15.999 18.998 20.180 22.990 24.305 26.982 28.085 30.974 32.06 "
        "35.45 39.95 39.098 40.078 44.956 47.867 50.942 51.996 54.938 55.845 58.933 58.693 63.546 65.38 69.723 72.630 "
        "74.922 78.971 79.904 83.798 85.468 87.62 88.906 91.224 92.906 95.95 101.07 102.91 106.42 107.87 112.41 "
        "114.82 118.71 121.76 127.60 126.90 131.29 132.91 137.33 138.91 140.12 140.91 144.24 150.36 151.96 157.25 "
        "158.93 162.50 164.93 167.26 168.93 173.05 174.97 178.49 180.95 183.84 186.21 190.23 192.22 195.08 196.97 "
        "200.59 204.38 207.2 208.98 232.04 231.04 238.03".split(),
        strict=True,
    )
)

TABLE_VERSION = "1"
TABLE_NAME = f"chemistry defaults table {TABLE_VERSION}"
# Constants other than relative atomic masses: symbol -> (default value or None, the unit values are held in).
CONSTANTS: dict[str, tuple[str | None, str | None]] = {
    "Vm": ("24", "dm^3/mol"),  # molar gas volume at r.t.p. (VER-20)
    "Kw": ("1.0e-14", "mol^2/dm^6"),  # ionic product of water at 298 K (VER-21)
    "F": (None, "C/mol"),  # Faraday constant: no default
    "c": (None, "J/(g*K)"),  # specific heat capacity of water: no default
    "L": (None, "1/mol"),  # Avogadro constant: no default
}
ALIASES = {
    "vm": "Vm", "molar_volume": "Vm", "molar_gas_volume": "Vm", "kw": "Kw", "f": "F", "faraday": "F",
    "c": "c", "c_water": "c", "specific_heat": "c", "specific_heat_capacity": "c", "l": "L", "na": "L",
    "avogadro": "L",
}  # fmt: skip
PRECEDENCE = ("question", "item", "pack", "default")
_AR = re.compile(r"^(?:Ar\(([A-Za-z]{1,2})\)|ar_([a-z]{1,2})|Ar_([A-Za-z]{1,2}))$")
_BY_LOWER = {s.lower(): s for s in SYMBOLS}


def canonical(symbol: str) -> str | None:
    """`Ar(Cl)`, `ar_cl` (a pack key's last part) or `Ar_Cl` -> `Ar(Cl)`; constant aliases -> their symbol."""
    match = _AR.match(symbol.strip())
    if match:
        element = _BY_LOWER.get(next(g for g in match.groups() if g).lower())
        return f"Ar({element})" if element else None
    text = symbol.strip()
    if text in CONSTANTS:
        return text
    return ALIASES.get(text.lower())


class ConventionMissing(Exception):
    """A needed constant has no stated value and no default in the chemistry table."""

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.symbol = symbol

    def result(self) -> tuple[dict[str, Any], dict[str, Any]]:
        message = f"{self.symbol} has no stated value and no default in the {TABLE_NAME}; state it (VER-12)"
        return {"status": "unknown", "reason": "convention_missing", "constant": self.symbol}, {
            "method": "heuristic", "confidence": "undecided",
            "warnings": [{"code": "convention_missing", "message": message}],
        }  # fmt: skip


@dataclass
class Constants:
    """The constants stated for one call (its `constants` argument and the call's conventions), resolved on demand;
    every constant used is recorded with its source, in the order first used."""

    stated: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> winning entry
    records: list[dict[str, Any]] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)
    _cache: dict[str, Fraction] = field(default_factory=dict)

    @classmethod
    def of(cls, args: dict[str, Any], context: CallContext, extra: list[dict[str, Any]] | None = None) -> Constants:
        entries = list(extra or []) + list(args.get("constants", []))
        conventions = context.conventions if isinstance(context.conventions, dict) else {}
        for name, convention in sorted(conventions.items()):
            if isinstance(convention, dict) and "source" in convention and canonical(name):
                entries.append({"symbol": name, **convention})
        found = cls()
        for entry in entries:
            symbol = canonical(str(entry.get("symbol", "")))
            if symbol is None:
                raise WorkerError(Code.DOMAIN_ERROR, f"unknown chemistry constant {entry.get('symbol')!r}")
            source = entry.get("source", "question")
            if source != "default" and "value" not in entry:
                raise WorkerError(Code.DOMAIN_ERROR, f"the constant {symbol} from the {source} has no value")
            held = found.stated.get(symbol)
            if held is None or PRECEDENCE.index(source) < PRECEDENCE.index(held["source"]):
                found.stated[symbol] = {**entry, "symbol": symbol, "source": source}
        return found

    def ar(self, element: str) -> Fraction:
        return self.value(f"Ar({element})")

    def value(self, symbol: str) -> Fraction:
        if symbol in self._cache:
            return self._cache[symbol]
        entry = self.stated.get(symbol)
        unit = None if symbol.startswith("Ar(") else CONSTANTS[symbol][1]
        if entry is None or entry["source"] == "default":
            raw = ATOMIC_WEIGHTS.get(symbol[3:-1]) if symbol.startswith("Ar(") else CONSTANTS[symbol][0]
            if raw is None:
                raise ConventionMissing(symbol)
            entry = {"symbol": symbol, "value": raw, "source": "default", **({"unit": unit} if unit else {})}
            self.defaults.append(symbol)
        value = _fraction(entry["value"], symbol)
        if entry.get("unit") and unit and entry["unit"] != unit:
            value = _convert(value, entry["unit"], unit, symbol)
        if value <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"the constant {symbol} must be positive")
        record = {"symbol": symbol, "value": str(entry["value"]), "source": entry["source"]}
        if entry.get("unit"):
            record["unit"] = str(entry["unit"])
        self.records.append(record)
        self._cache[symbol] = value
        return value

    def extras(self) -> dict[str, Any]:
        """`constants` for the check record and the `convention_default_used` warning, when any default was used."""
        out: dict[str, Any] = {}
        if self.records:
            out["constants"] = self.records
        if self.defaults:
            names = ", ".join(self.defaults[:12]) + (" and others" if len(self.defaults) > 12 else "")
            message = f"{names}: tool default from the {TABLE_NAME} (no value was stated)"
            out["warnings"] = [{"code": "convention_default_used", "message": message[:500]}]
        return out


def _fraction(raw: Any, symbol: str) -> Fraction:
    value = number(raw, symbol)
    return Fraction(int(value.p), int(value.q))


def _convert(value: Fraction, unit: str, target: str, symbol: str) -> Fraction:
    quantity = REGISTRY.Quantity(value, parse_unit(unit))
    try:
        converted = quantity.to(parse_unit(target)).magnitude
    except Exception:  # a dimensionality error from Pint
        raise WorkerError(Code.DOMAIN_ERROR, f"the constant {symbol} is not in a unit of {target}") from None
    return Fraction(converted)


def merge(*extras: dict[str, Any]) -> dict[str, Any]:
    """Several extras dictionaries into one: lists are concatenated, other keys taken from the last."""
    out: dict[str, Any] = {}
    for item in extras:
        for key, value in item.items():
            out[key] = out.get(key, []) + value if isinstance(value, list) else value
    return out
