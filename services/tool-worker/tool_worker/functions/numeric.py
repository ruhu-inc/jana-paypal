"""Numeric precision: rounding, significant figures and the default numeric rules of TABLE VER-09a (VER-09).

Rounding is half away from zero (`ROUND_HALF_UP` on exact decimals), never banker's rounding. Significant figures
follow the stated rules: leading zeros never count, trailing zeros after a decimal point count, trailing zeros of an
integer are not significant unless the number is written in standard form."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

_NUMBER = re.compile(
    r"^\s*(?P<sign>[+-]?)(?P<mantissa>\d+(?:\.\d*)?|\.\d+)"
    r"(?:\s*(?:[eE]|[x\u00d7]\s*10\s*\^)\s*(?P<exponent>[+-]?\d+))?\s*$"  # e, x or the multiplication sign
)


def round_sf(value: Decimal, sf: int) -> Decimal:
    """`value` rounded half away from zero to `sf` significant figures."""
    if value == 0:
        return Decimal(0)
    return value.quantize(Decimal(1).scaleb(value.adjusted() - sf + 1), rounding=ROUND_HALF_UP)


def read_number(text: str) -> tuple[Decimal, int]:
    """A written number as an exact decimal and its count of significant figures."""
    match = _NUMBER.match(text)
    if not match:
        raise ValueError(f"not a number: {text[:40]!r}")
    mantissa, exponent = match["mantissa"], match["exponent"]
    try:
        value = Decimal(match["sign"] + mantissa).scaleb(int(exponent or 0))
    except InvalidOperation as error:
        raise ValueError(f"not a number: {text[:40]!r}") from error
    digits = mantissa.replace(".", "").lstrip("0")
    if "." not in mantissa and exponent is None:
        digits = digits.rstrip("0")  # trailing zeros of a plain integer are not significant
    return value, max(len(digits), 1)


def compare_sf(student: str, key: str, min_sf: int, max_sf: int | None = None) -> dict[str, Any]:
    """A student's numeric answer against an exact key (TABLE VER-09a, physics row).

    The value matches when it equals the key rounded to the student's own precision; the answer is accepted when it
    matches with at least `min_sf` significant figures. An answer with more figures than the item's precision
    (`max_sf`) is compared at that precision instead and reported `too_many`, which is feedback only: it never costs
    the accuracy mark (VER-09). `too_few` below `min_sf` is not accepted."""
    value, student_sf = read_number(student)
    key_value, _ = read_number(key)
    if max_sf is not None and student_sf > max_sf:
        matches = round_sf(value, max_sf) == round_sf(key_value, max_sf)
        return {"accepted": matches, "value_matches": matches, "sf": "too_many", "student_sf": student_sf}
    matches = value == round_sf(key_value, student_sf)
    sf = "too_few" if student_sf < min_sf else "ok"
    return {"accepted": matches and student_sf >= min_sf, "value_matches": matches, "sf": sf, "student_sf": student_sf}


# --- the full table (TABLE VER-09a, completed in M12) -----------------------------------------------------------------
#
# Each row judges exact values: a written answer is read as an exact fraction together with the precision it was
# written to, and keys are exact (a decimal key is the item's rounded value). Rounding is done on fractions, so no
# binary float ever decides a verdict.

_ANSWER = re.compile(
    r"^\s*(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*(?:[eE]|[x\u00d7*]\s*10\s*\^\s*\{?)\s*[+-]?\d+\}?)?)"
    r"(?P<rest>.*)$"
)
_MIXED = re.compile(r"^\s*(?P<sign>[+-]?)(?P<whole>\d+)\s+(?P<p>\d+)\s*/\s*(?P<q>\d+)(?P<rest>.*)$")
_FRACTION = re.compile(r"^\s*(?P<p>[+-]?\d+)\s*/\s*(?P<q>\d+)(?P<rest>.*)$")
_LATEX_TIMES = re.compile(r"\\times|\\cdot")


@dataclass(frozen=True)
class Reading:
    """A written numeric answer: its exact value, how it was written, and any unit text after it."""

    value: Fraction
    written: str  # the number as written, without the unit
    kind: str  # decimal | fraction | mixed
    sf: int | None  # significant figures; None for a fraction or mixed number (exact)
    dp: int | None  # decimal places as written; None for a fraction or mixed number
    unit: str


def read_answer(text: str, *, money: bool = False) -> Reading:
    """Read a typed number (decimal, standard form, fraction or mixed number) and the unit text after it. A comma is a
    thousands separator only in money (VER-03)."""
    cleaned = _LATEX_TIMES.sub("\u00d7", text.strip().removeprefix("=").strip())
    if money:
        cleaned = cleaned.replace(",", "")
    mixed = _MIXED.match(cleaned)
    if mixed and int(mixed["q"]) != 0:
        magnitude = int(mixed["whole"]) + Fraction(int(mixed["p"]), int(mixed["q"]))
        written = cleaned[: len(cleaned) - len(mixed["rest"])].strip()
        return Reading(-magnitude if mixed["sign"] == "-" else magnitude, written, "mixed", None, None,
                       mixed["rest"].strip())  # fmt: skip
    fraction = _FRACTION.match(cleaned)
    if fraction and int(fraction["q"]) != 0:
        written = cleaned[: len(cleaned) - len(fraction["rest"])].strip()
        return Reading(Fraction(int(fraction["p"]), int(fraction["q"])), written, "fraction", None, None,
                       fraction["rest"].strip())  # fmt: skip
    match = _ANSWER.match(cleaned)
    if not match:
        raise ValueError(f"not a number: {text[:40]!r}")
    written = match["number"].strip()
    decimal, sf = read_number(written.replace("*", "x").replace("{", "").replace("}", ""))
    mantissa = re.split(r"[eEx\u00d7*]", written, maxsplit=1)[0]
    dp = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
    return Reading(Fraction(decimal), written, "decimal", sf, dp, match["rest"].strip())


def exponent10(value: Fraction) -> int:
    """floor(log10 |value|) of a non-zero fraction, computed exactly."""
    a = abs(value)
    e = len(str(a.numerator)) - len(str(a.denominator))
    while Fraction(10) ** e > a:
        e -= 1
    while Fraction(10) ** (e + 1) <= a:
        e += 1
    return e


def round_to(value: Fraction, exponent: int) -> Fraction:
    """`value` rounded half away from zero to a multiple of 10**exponent."""
    quantum = Fraction(10) ** exponent
    scaled = abs(value) / quantum
    whole = int(scaled)
    if scaled - whole >= Fraction(1, 2):
        whole += 1
    rounded = whole * quantum
    return -rounded if value < 0 else rounded


def round_sf_exact(value: Fraction, sf: int) -> Fraction:
    return Fraction(0) if value == 0 else round_to(value, exponent10(value) - sf + 1)


def round_dp_exact(value: Fraction, dp: int) -> Fraction:
    return round_to(value, -dp)


Judged = tuple[str, dict[str, Any], list[dict[str, str]]]


def judge_sf(value: Fraction, student_sf: int | None, key: Fraction, *, min_sf: int, item_sf: int,
             carried: Fraction | None = None) -> Judged:  # fmt: skip
    """The measurement row (Physics and Chemistry answers) and the carried-rounding row.

    Returns the value component (correct, incorrect, within_tolerance), the precision component and warnings. An
    answer equal to the key loses nothing, so its figures are never too few (the trailing zeros of 2000 may be
    significant). More figures than the item's precision compare at that precision and are feedback only. With
    `carried` (a relative share), a value that misses the key's rounding but lies within that share of the key, after
    a correct method, is accepted with warning `rounding_drift`."""
    precision: dict[str, Any]
    if student_sf is None:  # an exact fraction: equal to the key, or equal to it at the item's precision
        matches = value == key or round_sf_exact(value, item_sf) == round_sf_exact(key, item_sf)
        precision = {"sf": "ok", "exact": True}
    elif value == key:
        matches, precision = True, {"sf": "too_many" if student_sf > item_sf else "ok", "student_sf": student_sf}
    elif student_sf > item_sf:
        matches = round_sf_exact(value, item_sf) == round_sf_exact(key, item_sf)
        precision = {"sf": "too_many", "student_sf": student_sf}
    else:
        matches = value == round_sf_exact(key, student_sf)
        precision = {"sf": "too_few" if student_sf < min_sf else "ok", "student_sf": student_sf}
    precision["item_sf"] = item_sf
    if matches:
        return "correct", precision, []
    if carried is not None and key != 0 and abs(value - key) <= carried * abs(key):
        drift = {
            "code": "rounding_drift",
            "message": "the value drifted from the key by rounding carried between steps",
        }
        return "within_tolerance", precision, [drift]
    return "incorrect", precision, []


def judge_dp(value: Fraction, student_dp: int | None, key: Fraction, dp: int) -> Judged:
    """The money row: exact at the item's decimal places (whole units when `dp` is 0)."""
    correct = value == round_dp_exact(key, dp)
    written_ok = student_dp == dp
    return ("correct" if correct else "incorrect"), {"dp": "ok" if written_ok else "wrong", "item_dp": dp,
                                                     "student_dp": student_dp}, []  # fmt: skip


def judge_angle(value: Fraction, key: Fraction, dp: int, tolerance: Fraction) -> Judged:
    """The angle row: degrees to the item's decimal places, accepted within the stated tolerance."""
    if value == key or value == round_dp_exact(key, dp):
        return "correct", {"dp": "ok", "item_dp": dp}, []
    if abs(value - key) <= tolerance:
        return "within_tolerance", {"dp": "ok", "item_dp": dp}, []
    return "incorrect", {"dp": "ok", "item_dp": dp}, []


def judge_probability(reading: Reading, key: Fraction, sf: int) -> Judged:
    """The probability row: an exact fraction, or a decimal to the item's significant figures; either is accepted."""
    if not 0 <= reading.value <= 1:
        return "incorrect", {"sf": "ok", "exact": reading.sf is None}, []
    if reading.value == key:
        return "correct", {"sf": "ok", "exact": True}, []
    if reading.sf is None:
        return "incorrect", {"sf": "ok", "exact": True}, []
    if reading.sf < sf:
        matches = reading.value == round_sf_exact(key, reading.sf)
        return ("correct" if matches else "incorrect"), {"sf": "too_few", "student_sf": reading.sf,
                                                         "item_sf": sf}, []  # fmt: skip
    matches = round_sf_exact(reading.value, sf) == round_sf_exact(key, sf)
    status = "too_many" if reading.sf > sf else "ok"
    return ("correct" if matches else "incorrect"), {"sf": status, "student_sf": reading.sf, "item_sf": sf}, []
