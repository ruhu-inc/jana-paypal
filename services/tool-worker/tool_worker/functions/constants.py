"""Physical constants with stated precedence and the tool default table, TABLE VER-12a version 1 (VER-12).

Precedence: the value stated in the question, then the item's marking rule, then the content pack's conventions,
then the tool default from this table. Every constant used is listed in the check record with its source, and using
a tool default always emits `convention_default_used` naming the table version. A constant with no stated value and
no default makes the check `unknown` with `convention_missing`, never a guessed value. The pack's O level convention
for g (9.8 or 10) is the pack's decision; the default is the conventional standard gravity, so reaching it shows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sympy as sp

from tool_worker.functions.common import substituted
from tool_worker.grammar import exact_number
from tool_worker.protocol import Code, WorkerError

TABLE_VERSION = "1"
# symbol -> (value as written, unit)
TABLE: dict[str, tuple[str, str]] = {
    "g": ("9.80665", "m/s^2"),
}
PRECEDENCE = ("question", "item", "pack", "default")


class ConventionMissing(Exception):
    """A needed constant has no stated convention and no tool default."""

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.symbol = symbol

    def result(self) -> tuple[dict[str, Any], dict[str, Any]]:
        message = f"{self.symbol} has no stated value and no default in table {TABLE_VERSION}"
        return {"status": "unknown", "reason": "convention_missing"}, {
            "method": "heuristic", "confidence": "undecided",
            "warnings": [{"code": "convention_missing", "message": message}],
        }  # fmt: skip


def resolve(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The winning entry per symbol, with its value filled from the table for a default."""
    chosen: dict[str, dict[str, Any]] = {}
    for entry in entries:
        symbol, source = entry["symbol"], entry["source"]
        if source == "default":
            if symbol not in TABLE:
                raise ConventionMissing(symbol)
            value, unit = TABLE[symbol]
            entry = {"symbol": symbol, "value": value, "unit": unit, "source": "default"}
        elif "value" not in entry:
            raise WorkerError(Code.DOMAIN_ERROR, f"the constant {symbol} from the {source} has no value")
        held = chosen.get(symbol)
        if held is None or PRECEDENCE.index(source) < PRECEDENCE.index(held["source"]):
            chosen[symbol] = entry
    return chosen


def apply(
    trees: list[sp.Basic], args: dict[str, Any], unknowns: Sequence[str] = ()
) -> tuple[list[sp.Basic], dict[str, Any]]:
    """Substitute the resolved constants that appear in `trees`; extras carry the record and warnings."""
    chosen = resolve(args.get("constants", []))
    clash = sorted(set(chosen) & set(unknowns))
    if clash:
        raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(clash)} is given as a constant and asked for as an unknown")
    present = {s.name: s for t in trees for s in t.free_symbols}
    substitutions, records, warnings = {}, [], []
    for symbol in sorted(chosen):
        if symbol not in present:
            continue
        entry = chosen[symbol]
        raw = str(entry["value"])
        substitutions[present[symbol]] = exact_number(raw, 0)
        record: dict[str, Any] = {"symbol": symbol, "value": raw, "source": entry["source"]}
        if entry.get("unit"):
            record["unit"] = entry["unit"]
        records.append(record)
        if entry["source"] == "default":
            warnings.append(
                {
                    "code": "convention_default_used",
                    "message": f"{symbol} = {raw} {entry.get('unit', '')} is the tool default (table {TABLE_VERSION})",
                }
            )
    extras: dict[str, Any] = {}
    if records:
        extras["constants"] = records
    if warnings:
        extras["warnings"] = warnings
    return [substituted(t, substitutions, "constants") for t in trees], extras
