"""Money for the accounting checkers (VER-22 to VER-25): exact amounts, presentation rounding and conventions.

Amounts are read exactly, from integers or decimal text (never binary floats), into fractions; every sum, average and
ratio stays exact, and only presentation rounds, half away from zero, to the places in force. A convention is taken
from the call (the question or the item), then from the pack's conventions sent with the call, then from the tool's
configured default; a presentation default is visible as `convention_default_used`. Conventions that change a figure
(which definition of a ratio a board uses) have no tool default: without one the figure is `unknown`."""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
# An amount as written: optional sign or brackets for a negative, an optional currency symbol or ISO code, digits
# with optional thousands separators and decimals.
_AMOUNT = re.compile(
    r"^\s*(?P<open>[-(]?)\s*(?:[$£€₦¥]|[A-Z]{3}\s*)?\s*(?P<digits>\d{1,3}(?:,\d{3})+|\d+)(?P<decimals>\.\d+)?\s*"
    r"(?P<close>\)?)\s*$"
)
AMOUNT_PATTERN = r"^\s*[-(]?\s*([$£€₦¥]|[A-Z]{3}\s*)?\s*(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\s*\)?\s*$"


def amount(raw: Any, field: str) -> Fraction:
    """An exact amount from an integer or decimal text ("2,500.50", "$300", "(120)" for a negative)."""
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: give an amount as an integer or decimal text, never a float")
    if isinstance(raw, int):
        return Fraction(raw)
    match = _AMOUNT.match(raw)
    if not match or (match["open"] == "(") != (match["close"] == ")"):
        raise WorkerError(Code.DOMAIN_ERROR, f"{field}: {raw[:40]!r} is not an amount")
    value = Fraction(match["digits"].replace(",", "") + (match["decimals"] or ""))
    return -value if match["open"] else value


def positive(raw: Any, field: str, allow_zero: bool = False) -> Fraction:
    value = amount(raw, field)
    if value < 0 or (value == 0 and not allow_zero):
        raise WorkerError(Code.DOMAIN_ERROR, f"{field} must be {'zero or more' if allow_zero else 'more than zero'}")
    return value


def round_half_up(value: Fraction, dp: int) -> Fraction:
    """`value` rounded half away from zero to `dp` places, exactly (VER-09: never banker's rounding)."""
    scale = 10**dp
    whole = int(abs(value) * scale + Fraction(1, 2))
    return Fraction(whole if value >= 0 else -whole, scale)


def money(value: Fraction, dp: int) -> str:
    """Plain decimal text at `dp` places, without a currency symbol ("2500.00", "-120.50")."""
    rounded = round_half_up(value, dp)
    whole, part = divmod(int(abs(rounded) * 10**dp), 10**dp)
    sign = "-" if rounded < 0 else ""
    return f"{sign}{whole}.{part:0{dp}d}" if dp else f"{sign}{whole}"


def exact_text(value: Fraction) -> str:
    """Exact text: a terminating decimal as decimal text, any other fraction as p/q."""
    q = value.denominator
    twos = fives = 0
    while q % 2 == 0:
        q, twos = q // 2, twos + 1
    while q % 5 == 0:
        q, fives = q // 5, fives + 1
    if q != 1:
        return f"{value.numerator}/{value.denominator}"
    places = max(twos, fives)
    return money(value, places) if places else str(value.numerator)


def figure(value: Fraction, dp: int) -> dict[str, str]:
    """An exact figure with its value rounded for presentation."""
    return {"exact": exact_text(value), "value": money(value, dp)}


def convention(context: CallContext, key: str) -> tuple[Any, str] | None:
    """A pack convention sent with the call (`{value, source}`, or a bare value), with its source."""
    held = context.conventions.get(key) if isinstance(context.conventions, dict) else None
    if isinstance(held, dict) and "value" in held:
        return held["value"], str(held.get("source", "pack"))
    return None if held is None else (held, "pack")


def places(args: dict[str, Any], context: CallContext, field: str = "dp") -> int:
    """Decimal places for presentation: the call, then the pack's `acc.dp`, then the configured default (VER-24)."""
    if field in args:
        return int(args[field])
    held = convention(context, "acc.dp")
    if held is not None:
        return int(held[0])
    return int(context.setting("acc_dp", 2))


def choice(
    args: dict[str, Any], context: CallContext, field: str, key: str, allowed: tuple[str, ...], default: str
) -> tuple[str, str, list[dict[str, str]]]:
    """A presentation convention: the call's value, then the pack's, then the tool default with a warning.
    Returns (value, source, warnings)."""
    if field in args:
        return str(args[field]), "item", []
    held = convention(context, key)
    if held is not None and held[0] in allowed:
        return str(held[0]), held[1], []
    message = f"no {key} convention was stated; the tool default {default} was used"
    return default, "default", [{"code": "convention_default_used", "message": message}]


def normal(text: str) -> str:
    """An account or line name for matching: lower case, single spaces, without "a/c" or "account" at the end."""
    words = re.sub(r"[^a-z0-9&]+", " ", text.lower()).split()
    if words[-2:] == ["a", "c"]:
        words = words[:-2]
    elif words[-1:] in (["ac"], ["account"]):
        words = words[:-1]
    return " ".join(words)


def ordered_unique(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)
