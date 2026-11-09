"""Acids, bases and titrations (VER-21): pH and pOH of strong acids and bases, neutralisation volumes and
concentrations, and titration curve points for a strong acid against a strong base with the equivalence point marked.

Kw comes from the stated constants, else the chemistry default table (1.0 * 10⁻¹⁴ at 298 K, with a warning). A strong
acid gives [H⁺] = c * basicity and a strong base [OH⁻] = c * the hydroxide ions per formula; pH = -log₁₀[H⁺] and
pH + pOH = pKw, exactly (logarithms stay symbolic, so 0.1 mol dm⁻³ HCl has pH exactly 1). Along a titration the excess
acid or base sets the pH, as at O level; at the equivalence point pH = pKw / 2. Water's own ions are neglected, which
is reported when an excess concentration falls below 10⁻⁶ mol dm⁻³. A weak acid's pH needs its Ka (`ka`, A level,
VER-32): [H⁺] = √(Ka c) when that ionises under 5% of the acid, else the quadratic [H⁺]² + Ka [H⁺] - Ka c = 0, and the
working says which and why. Weak bases, weak acids without Ka and titration curves of weak acids are `unknown`, as is
an acid or base whose strength is neither known nor stated.

Volumes are given with their units (cm³ or dm³) and reported in cm³, as in a titration; concentrations in
mol dm⁻³. Curve points are [titrant volume in cm³, pH], rounded to 4 and 3 decimal places for drawing only: every
value a student reads (the equivalence point, the start, read-outs at stated volumes) is exact."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.functions.chemistry import number_out, plain, quantity
from tool_worker.functions.elements import Constants, ConventionMissing
from tool_worker.functions.formula import parse_formula
from tool_worker.functions.maths import exact
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
CONC = "mol dm⁻³"
CM3 = "cm³"
DEFAULT_POINTS = 41
MAX_READOUTS = 20
WATER_LIMIT = Fraction(1, 10**6)
# Ions released per formula unit for the strong acids and bases of O level (formula -> count), and weak ones.
STRONG = {
    "acid": {"HCl": 1, "HBr": 1, "HI": 1, "HNO3": 1, "H2SO4": 2, "HClO4": 1},
    "base": {"NaOH": 1, "KOH": 1, "LiOH": 1, "Ca(OH)2": 2, "Ba(OH)2": 2, "Sr(OH)2": 2},
}
WEAK = {
    "acid": ("CH3COOH", "HCOOH", "C2H5COOH", "H2CO3", "H3PO4", "HF", "HNO2", "H2SO3", "H2S", "C6H8O7"),
    "base": ("NH3", "NH4OH", "CH3NH2"),
}
PROTONS = {"acid": "basicity", "base": "acidity"}


def _key(text: str) -> tuple[tuple[tuple[str, int], ...], int]:
    species = parse_formula(text)
    return tuple(sorted(species.counts.items())), species.charge


_STRONG_KEYS = {kind: {_key(f): n for f, n in table.items()} for kind, table in STRONG.items()}
_WEAK_KEYS = {kind: {_key(f) for f in table} for kind, table in WEAK.items()}


def log10(value: Fraction) -> sp.Expr:
    """log₁₀ of a positive rational, exact (an integer for a power of ten)."""
    return sp.log(sp.Rational(value.numerator, value.denominator), 10)


def ph_out(value: sp.Expr, dp: int) -> dict[str, Any]:
    out = exact(value)
    out["rounded"] = rounded(value, dp)
    return out


def rounded(value: sp.Expr, dp: int) -> str:
    return str(Decimal(str(sp.N(value, 30))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


def _draw(value: sp.Expr | Fraction, dp: int) -> float:
    number = sp.Rational(value.numerator, value.denominator) if isinstance(value, Fraction) else value
    return float(Decimal(str(sp.N(number, 30))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


class Side:
    """One solution: its formula, the H⁺ or OH⁻ it releases per formula unit, its strength and given values."""

    def __init__(self, kind: str, args: dict[str, Any], warnings: list[dict[str, str]]) -> None:
        self.kind = kind
        self.formula: str | None = args.get(kind)
        stated: int | None = args.get(PROTONS[kind])
        key = _key(self.formula) if self.formula else None
        known = _STRONG_KEYS[kind].get(key) if key else None
        if known is not None and stated is not None and stated != known:
            raise WorkerError(Code.DOMAIN_ERROR, f"{self.formula} releases {known} ions per formula, not {stated}")
        if stated is None and known is None:
            stated = 1
            what = self.formula or f"the {kind}"
            warnings.append({"code": "assumption_added", "message": f"{PROTONS[kind]} of {what} taken as 1"})
        self.protons = Fraction(known if known is not None else stated or 1)
        strength = args.get(f"{kind}_strength")
        if strength is None and key is not None:
            strength = "strong" if known is not None else ("weak" if key in _WEAK_KEYS[kind] else None)
        if known is not None and strength == "weak":
            raise WorkerError(Code.DOMAIN_ERROR, f"{self.formula} is a strong {kind}")
        self.strength: str | None = strength
        name = f"{kind}_concentration"
        self.concentration = quantity(args[name], "concentration", name) if name in args else None
        name = f"{kind}_volume"
        self.volume = quantity(args[name], "volume", name) if name in args else None
        for label, value in (("concentration", self.concentration), ("volume", self.volume)):
            if value is not None and value <= 0:
                raise WorkerError(Code.DOMAIN_ERROR, f"the {kind} {label} must be positive")

    @property
    def given(self) -> bool:
        return self.formula is not None or self.concentration is not None or self.volume is not None

    @property
    def label(self) -> str:
        return self.formula or self.kind

    def undecided(self) -> tuple[dict[str, Any], Extras] | None:
        """`unknown` for a weak or unstated strength (pH needs a strong acid or base here)."""
        if self.strength == "strong":
            return None
        reason = "weak_acid_or_base" if self.strength == "weak" else "strength_unknown"
        message = (f"{self.label} is weak: its pH needs Ka or Kb" if self.strength == "weak"
                   else f"state whether {self.label} is strong ({self.kind}_strength)")  # fmt: skip
        return {"status": "unknown", "reason": reason}, {
            "method": "heuristic", "confidence": "undecided",
            "warnings": [{"code": "domain_restricted", "message": message}],
        }  # fmt: skip


def _task(args: dict[str, Any], acid: Side, base: Side) -> str:
    if "task" in args:
        return str(args["task"])
    if any(k in args for k in ("analyte", "volume_max", "at_volumes", "points")):
        return "titration_curve"
    return "neutralisation" if acid.given and base.given and "ph" not in args else "ph"


def chem_acid_base(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    warnings: list[dict[str, str]] = []
    acid, base = Side("acid", args, warnings), Side("base", args, warnings)
    constants = Constants.of(args, context)
    dp = int(args.get("dp", 2))
    task = _task(args, acid, base)
    try:
        if task == "neutralisation":
            value, working = _neutralisation(acid, base, context)
        else:
            kw = constants.value("Kw")
            if task == "ph":
                weak = "ph" not in args and acid.given and not base.given and acid.strength == "weak"
                if weak and "ka" in args:
                    value, working = _weak_ph(args, acid, kw, dp, context)
                else:
                    undecided = None if "ph" in args else (acid if acid.given else base).undecided()
                    if undecided is not None:
                        return undecided
                    value, working = _ph(args, acid, base, kw, dp, warnings, context)
            else:
                undecided = acid.undecided() or base.undecided()
                if undecided is not None:
                    return undecided
                value, working = _curve(args, acid, base, kw, dp, warnings, context)
    except ConventionMissing as missing:
        return missing.result()
    value = {"task": task, **value}
    extras: Extras = {"method": "exact", "confidence": "proved", "working": working, **constants.extras()}
    if warnings:
        extras["warnings"] = extras.get("warnings", []) + warnings
    return value, extras


# --- pH -------------------------------------------------------------------------------------------------------------


def _ph(args: dict[str, Any], acid: Side, base: Side, kw: Fraction, dp: int, warnings: list[dict[str, str]],
        context: CallContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # fmt: skip
    pkw = -log10(kw)
    kw_exact = sp.Rational(kw.numerator, kw.denominator)
    if "ph" in args:
        ph = sp.Rational(str(args["ph"]))
        h = sp.Integer(10) ** (-ph)
        oh = kw_exact / h
        poh = pkw - ph
    else:
        if acid.given and base.given:
            raise WorkerError(Code.DOMAIN_ERROR, "give one solution for its pH (or both for a neutralisation)")
        side = acid if acid.given else base
        if side.concentration is None:
            raise WorkerError(Code.DOMAIN_ERROR, f"give the {side.kind} concentration")
        ions = side.concentration * side.protons
        if ions < WATER_LIMIT:
            warnings.append({"code": "domain_restricted", "message": "water's own ions are neglected"})
        if side.kind == "acid":
            h, ph = sp.Rational(ions.numerator, ions.denominator), -log10(ions)
            oh, poh = kw_exact / h, pkw + log10(ions)
        else:
            oh, poh = sp.Rational(ions.numerator, ions.denominator), -log10(ions)
            h, ph = kw_exact / oh, pkw + log10(ions)
    value = {"h": {**exact(h), "unit": CONC}, "oh": {**exact(oh), "unit": CONC}, "ph": ph_out(ph, dp),
             "poh": ph_out(poh, dp), "pkw": exact(pkw)}  # fmt: skip
    check = context.check_id
    working = [{"label": "[H⁺]", "value": exact(h)["decimal"], "unit": CONC, "check_id": check},
               {"label": "pH", "value": value["ph"]["rounded"], "check_id": check},
               {"label": "pOH", "value": value["poh"]["rounded"], "check_id": check}]  # fmt: skip
    return value, working


APPROXIMATION_LIMIT = Fraction(1, 20)  # the square-root approximation holds while under 5% of the acid ionises


def _weak_ph(args: dict[str, Any], acid: Side, kw: Fraction, dp: int,
             context: CallContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # fmt: skip
    """VER-32: a weak monoprotic acid from its Ka; the approximation only while the ionised fraction is under 5%."""
    if acid.concentration is None:
        raise WorkerError(Code.DOMAIN_ERROR, "give the acid concentration")
    if acid.protons != 1:
        raise WorkerError(Code.DOMAIN_ERROR, "the Ka method here is for a monoprotic acid")
    ka = Fraction(str(args["ka"]))
    c = sp.Rational(acid.concentration.numerator, acid.concentration.denominator)
    ka_exact = sp.Rational(ka.numerator, ka.denominator)
    approximate = sp.sqrt(ka_exact * c)
    fraction = approximate / c
    if fraction < sp.Rational(APPROXIMATION_LIMIT.numerator, APPROXIMATION_LIMIT.denominator):
        h = approximate
        why = f"square-root approximation: {rounded(100 * fraction, 1)}% ionised, under 5%"
    else:
        h = (-ka_exact + sp.sqrt(ka_exact**2 + 4 * ka_exact * c)) / 2
        why = f"quadratic: the approximation would ionise {rounded(100 * fraction, 1)}% of the acid, not under 5%"
    ph = -sp.log(h, 10)
    pkw = -log10(kw)
    kw_exact = sp.Rational(kw.numerator, kw.denominator)
    value = {"h": {**exact(h), "unit": CONC}, "oh": {**exact(kw_exact / h), "unit": CONC}, "ph": ph_out(ph, dp),
             "poh": ph_out(pkw - ph, dp), "pkw": exact(pkw)}  # fmt: skip
    check = context.check_id
    working = [{"label": "method", "value": why, "check_id": check},
               {"label": "[H⁺]", "value": exact(h)["decimal"], "unit": CONC, "check_id": check},
               {"label": "pH", "value": value["ph"]["rounded"], "check_id": check}]  # fmt: skip
    return value, working


# --- neutralisation -------------------------------------------------------------------------------------------------


def _neutralisation(acid: Side, base: Side, context: CallContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """c_a V_a * basicity = c_b V_b * acidity, solved for the one value not given."""
    names = {"acid_concentration": acid.concentration, "acid_volume": acid.volume,
             "base_concentration": base.concentration, "base_volume": base.volume}  # fmt: skip
    missing = [name for name, v in names.items() if v is None]
    if len(missing) > 1:
        raise WorkerError(Code.DOMAIN_ERROR, f"give three of the four values; missing {', '.join(missing)}")
    check = context.check_id
    working: list[dict[str, Any]] = []
    value: dict[str, Any] = {"ratio": {"acid": plain(base.protons), "base": plain(acid.protons)}}
    if missing:
        solved = missing[0]
        known_side, other = (base, acid) if solved.startswith("acid") else (acid, base)
        assert known_side.concentration is not None and known_side.volume is not None
        ions = known_side.concentration * known_side.volume * known_side.protons
        working.append({"label": f"amount of {known_side.label}", "value": plain(ions / known_side.protons),
                        "unit": "mol", "check_id": check})  # fmt: skip
        if solved.endswith("concentration"):
            assert other.volume is not None
            other.concentration = ions / other.protons / other.volume
        else:
            assert other.concentration is not None
            other.volume = ions / other.protons / other.concentration
        value["solved"] = solved
    assert acid.concentration and acid.volume and base.concentration and base.volume
    hydrogen = acid.concentration * acid.volume * acid.protons
    hydroxide = base.concentration * base.volume * base.protons
    for side in (acid, base):
        assert side.concentration is not None and side.volume is not None
        amount = side.concentration * side.volume
        value[side.kind] = {
            "formula": side.formula,
            "concentration": number_out(side.concentration, CONC),
            "volume": number_out(side.volume * 1000, CM3),
            "amount": number_out(amount, "mol"),
        }
        working.append({"label": f"amount of {side.label}", "value": plain(amount), "unit": "mol", "check_id": check})
    if missing:
        solved_side = acid if value["solved"].startswith("acid") else base
        item = "concentration" if value["solved"].endswith("concentration") else "volume"
        working.append({"label": f"{solved_side.label} {item}", "value": value[solved_side.kind][item]["shown"],
                        "unit": CONC if item == "concentration" else CM3, "check_id": check})  # fmt: skip
    value["neutral"] = hydrogen == hydroxide
    if hydrogen != hydroxide:
        value["excess"] = {"of": "acid" if hydrogen > hydroxide else "base",
                           "ions": number_out(abs(hydrogen - hydroxide), "mol")}  # fmt: skip
    return value, working


# --- titration curve ------------------------------------------------------------------------------------------------


def _curve(args: dict[str, Any], acid: Side, base: Side, kw: Fraction, dp: int, warnings: list[dict[str, str]],
           context: CallContext) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # fmt: skip
    analyte_kind = args.get("analyte", "acid")
    flask, burette = (acid, base) if analyte_kind == "acid" else (base, acid)
    for side, needs in ((flask, ("concentration", "volume")), (burette, ("concentration",))):
        for item in needs:
            if getattr(side, item) is None:
                raise WorkerError(Code.DOMAIN_ERROR, f"the titration needs the {side.kind} {item}")
    assert flask.concentration and flask.volume and burette.concentration
    pkw = -log10(kw)
    ions = flask.concentration * flask.volume * flask.protons
    per_dm3 = burette.concentration * burette.protons
    equivalence = ions / per_dm3  # dm³ of titrant
    low = [False]

    def ph_at(volume_dm3: Fraction) -> sp.Expr:
        added = per_dm3 * volume_dm3
        total = flask.volume + volume_dm3  # type: ignore[operator]
        if added == ions:
            return pkw / 2
        excess = abs(ions - added) / total
        if excess < WATER_LIMIT:
            low[0] = True
        acid_left = (ions > added) == (flask.kind == "acid")
        return -log10(excess) if acid_left else pkw + log10(excess)

    v_max = quantity(args["volume_max"], "volume", "volume_max") if "volume_max" in args else 2 * equivalence
    if v_max <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, "volume_max must be positive")
    count = int(args.get("points", DEFAULT_POINTS))
    grid = {v_max * i / (count - 1) for i in range(count)}
    for near in (Fraction(0), v_max / 100, v_max / 1000, v_max / 10000):  # the steep rise through equivalence
        for volume in (equivalence - near, equivalence + near):
            if 0 <= volume <= v_max:
                grid.add(volume)
    volumes = sorted(grid)
    points = [[_draw(v * 1000, 4), _draw(ph_at(v), 3)] for v in volumes]
    value: dict[str, Any] = {
        "analyte": flask.kind,
        "titrant": burette.kind,
        "equivalence": {"volume": number_out(equivalence * 1000, CM3), "ph": ph_out(pkw / 2, dp)},
        "half_equivalence": {"volume": number_out(equivalence * 500, CM3), "ph": ph_out(ph_at(equivalence / 2), dp)},
        "start_ph": ph_out(ph_at(Fraction(0)), dp),
        "end_ph": ph_out(ph_at(v_max), dp),
        "points": points,
        "equivalence_index": volumes.index(equivalence) if equivalence <= v_max else None,
        "axes": {"x": "volume of titrant", "x_unit": CM3, "y": "pH"},
    }
    readouts = []
    for raw in args.get("at_volumes", [])[:MAX_READOUTS]:
        volume = quantity(raw, "volume", "at_volumes")
        if volume < 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a titrant volume cannot be negative")
        readouts.append({"volume": number_out(volume * 1000, CM3), "ph": ph_out(ph_at(volume), dp)})
    if readouts:
        value["at"] = readouts
    if low[0]:
        warnings.append({"code": "domain_restricted", "message": "near the equivalence point water's own ions are "
                         "neglected"})  # fmt: skip
    check = context.check_id
    working = [
        {"label": f"amount of {'H⁺' if flask.kind == 'acid' else 'OH⁻'} in the flask", "value": plain(ions),
         "unit": "mol", "check_id": check},
        {"label": "equivalence volume", "value": value["equivalence"]["volume"]["shown"], "unit": CM3,
         "check_id": check},
        {"label": "pH at equivalence", "value": value["equivalence"]["ph"]["rounded"], "check_id": check},
    ]  # fmt: skip
    return value, working
