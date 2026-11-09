"""Plausibility bands (VER-26): a result outside the band of its quantity is flagged, never changed.

The bands come with the call as conventions, the worker protocol's only channel for them: the host turns each band
of the active teaching skill (front matter `plausibility`, pushed by SKL-08) into two entries,
`plausibility.<quantity>.min` and `plausibility.<quantity>.max`, each `{value, unit, source}`. After a function
returns, every result that measures a banded quantity is converted exactly into the band's unit and compared; one
outside the band adds warning `implausible` naming the quantity, the value and the band. The value, and so the check
record's result hash, is the same with or without bands: a correct computation stays correct.

Which results measure a quantity, by name:
- a named field of a tool whose units are fixed (TABLE `FIELDS`, for example the projectile's `max_height`);
- an entry `name: {text, unit}` anywhere in the value (an unknown solved in a stated unit), named by its key or, for a
  symbol, by the conventional symbols of TABLE `SYMBOLS` (version `TABLE_VERSION`), as long as its dimension is
  the band's.
A result with no such name is not compared: a band never applies by dimension alone, so a 2 km journey is not
judged against a height band."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pint
import sympy as sp

from tool_worker.functions.maths import significant
from tool_worker.functions.units import REGISTRY, parse_unit, unit_text
from tool_worker.grammar import ParseRejected, TooLarge, parse
from tool_worker.protocol import WorkerError

TABLE_VERSION = "1"
PREFIX = "plausibility."
# Conventional symbols of the quantities skills band (version 1). A symbol names a quantity only when its dimension is
# also the band's, which separates P for power from P for pressure.
SYMBOLS: dict[str, tuple[str, ...]] = {
    "height": ("h", "H"),
    "distance": ("d", "s", "x"),
    "displacement": ("s", "x"),
    "length": ("l", "L"),
    "speed": ("v", "u"),
    "velocity": ("v", "u"),
    "acceleration": ("a",),
    "time": ("t",),
    "mass": ("m",),
    "force": ("F",),
    "weight": ("W",),
    "energy": ("E",),
    "work": ("W",),
    "power": ("P",),
    "pressure": ("p", "P"),
    "density": ("rho",),
    "current": ("I",),
    "voltage": ("V",),
    "resistance": ("R",),
    "frequency": ("f",),
    "wavelength": ("lambda",),
    "temperature": ("T", "theta"),
    "focal_length": ("f",),
}
# tool -> (path into the value, quantity names, unit); "*" matches every key at that level.
FIELDS: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...]] = {
    "phys_projectile": (
        (("max_height",), ("height", "max_height"), "m"),
        (("time_of_flight",), ("time", "time_of_flight"), "s"),
        (("range",), ("range", "distance"), "m"),
        (("launch", "speed"), ("speed", "launch_speed"), "m/s"),
        (("landing", "speed"), ("speed", "impact_speed"), "m/s"),
    ),
    "phys_circuit": (
        (("components", "*", "current"), ("current",), "A"),
        (("components", "*", "pd"), ("voltage", "pd"), "V"),
        (("components", "*", "power"), ("power",), "W"),
        (("total_current",), ("current", "total_current"), "A"),
        (("equivalent_resistance",), ("resistance", "equivalent_resistance"), "ohm"),
    ),
}


@dataclass(frozen=True)
class Band:
    quantity: str
    low: Fraction
    high: Fraction
    unit: Any
    source: str
    shown: str  # the band as stated, for messages


@dataclass(frozen=True)
class Measured:
    label: str
    names: tuple[str, ...]  # quantity names this result may carry
    symbol: str | None  # the symbol it was solved for, if any
    text: str  # the exact value as the function wrote it
    unit: str


def bands(conventions: Any) -> list[Band]:
    """The complete, readable bands among the call's conventions; an entry missing its pair or its unit is ignored."""
    if not isinstance(conventions, dict):
        return []
    halves: dict[str, dict[str, Any]] = {}
    for key, entry in conventions.items():
        if not (isinstance(key, str) and key.startswith(PREFIX) and isinstance(entry, dict)):
            continue
        quantity, _, end = key[len(PREFIX) :].rpartition(".")
        if quantity and end in ("min", "max"):
            halves.setdefault(quantity, {})[end] = entry
    found = []
    for quantity, pair in sorted(halves.items()):
        if set(pair) != {"min", "max"}:
            continue
        try:
            found.append(_band(quantity, pair["min"], pair["max"]))
        except (KeyError, ValueError, ZeroDivisionError, WorkerError, pint.errors.PintError):
            continue
    return [b for b in found if b.low <= b.high]


def _band(quantity: str, low: dict[str, Any], high: dict[str, Any]) -> Band:
    """One band in the unit of its lower bound; an upper bound written in another unit is converted to it."""
    unit = parse_unit(str(low["unit"]))
    top, shown = Fraction(str(high["value"])), f"{low['value']} to {high['value']} {low['unit']}"
    if str(high.get("unit", low["unit"])) != str(low["unit"]):
        top = Fraction(REGISTRY.Quantity(top, parse_unit(str(high["unit"]))).to(unit).magnitude)
        shown = f"{low['value']} {low['unit']} to {high['value']} {high['unit']}"
    return Band(quantity, Fraction(str(low["value"])), top, unit, str(low.get("source", "pack")), shown)


def _walk(value: Any, path: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    if not path:
        yield "", value
        return
    head, rest = path[0], path[1:]
    if not isinstance(value, dict):
        return
    keys = sorted(value) if head == "*" else [head] if head in value else []
    for key in keys:
        for label, found in _walk(value[key], rest):
            yield (f"{key}.{label}" if label else key), found


def _entries(value: Any, trail: str = "") -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Every `name: {text, unit}` entry in a value, with its key and dotted label."""
    if isinstance(value, dict):
        for key in sorted(value):
            item, label = value[key], f"{trail}.{key}" if trail else str(key)
            if isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("unit"), str):
                yield str(key), label, item
            else:
                yield from _entries(item, label)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _entries(item, f"{trail}[{i}]")


def measured(tool: str, value: Any) -> list[Measured]:
    """The results of one call that measure a named quantity."""
    out = []
    for path, names, unit in FIELDS.get(tool, ()):
        for label, item in _walk(value, path):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out.append(Measured(label, names, None, item["text"], unit))
    for key, label, item in _entries(value):
        symbol = key.split("_", 1)[0]
        out.append(Measured(label, (key,), symbol, item["text"], item["unit"]))
    return out


def _applies(band: Band, result: Measured, unit: Any) -> bool:
    if band.quantity not in result.names and result.symbol not in SYMBOLS.get(band.quantity, ()):
        return False
    return bool(REGISTRY.Quantity(1, unit).dimensionality == REGISTRY.Quantity(1, band.unit).dimensionality)


def _in_band_unit(text: str, unit: Any, band: Band) -> sp.Basic | None:
    """The value converted exactly to the band's unit; None when it is not a real number."""
    tree = parse(text).tree
    if not (tree.is_real and tree.is_finite):
        return None
    one = REGISTRY.Quantity(1, unit)
    if one.units == band.unit:
        return tree
    if not one._is_multiplicative or not REGISTRY.Quantity(1, band.unit)._is_multiplicative:
        if not tree.is_Rational:  # an offset unit (°C) converts only a rational value exactly
            return None
        quantity = REGISTRY.Quantity(Fraction(int(tree.p), int(tree.q)), unit).to(band.unit)
        return sp.Rational(Fraction(quantity.magnitude).numerator, Fraction(quantity.magnitude).denominator)
    factor = Fraction(one.to(band.unit).magnitude)
    return tree * sp.Rational(factor.numerator, factor.denominator)


def _outside(value: sp.Basic, band: Band) -> bool:
    low, high = sp.Rational(band.low.numerator, band.low.denominator), sp.Rational(band.high.numerator,
                                                                                    band.high.denominator)  # fmt: skip
    return bool(sp.N(value - low, 30) < 0 or sp.N(value - high, 30) > 0)


def flags(tool: str, value: Any, conventions: Any) -> list[dict[str, str]]:
    """Warning `implausible` for every banded result outside its band (VER-26)."""
    stated = bands(conventions)
    if not stated:
        return []
    warnings = []
    for result in measured(tool, value):
        try:
            unit = parse_unit(result.unit)
        except WorkerError:
            continue
        for band in stated:
            try:
                if not _applies(band, result, unit):
                    continue
                converted = _in_band_unit(result.text, unit, band)
            except (ParseRejected, TooLarge, WorkerError, pint.errors.PintError, ValueError, TypeError):
                continue
            if converted is None or not _outside(converted, band):
                continue
            shown = significant(converted, 6)
            message = (
                f"{result.label} = {shown} {unit_text(band.unit)} is outside the {band.quantity} band of "
                f"{band.shown} ({band.source}); the computation stands, so check the givens and their units first"
            )
            warnings.append({"code": "implausible", "message": message[:500]})
    return warnings
