"""Statistics for O level (VER-13): summaries of a list or a frequency table, grouped-data estimates, and the
probabilities of combined and conditional events, all exact.

Values are read as exact fractions, so a mean is an exact fraction and a standard deviation an exact surd (with a
decimal alongside). The quartile method is a parameter: the call's value, then the pack's convention, then the tool
default with warning `convention_default_used`; the method used is in the result. Grouped data: the mean from class
midpoints, the modal class (by frequency density when the class widths differ), and the median and quartiles
estimated by linear interpolation of the cumulative frequency at n/2, n/4 and 3n/4."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.functions.calculus import number
from tool_worker.functions.common import convention
from tool_worker.functions.maths import exact
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
QUARTILE_METHODS = ("exclusive", "inclusive", "interpolated")
DEFAULT_QUARTILE_METHOD = "exclusive"  # tool default when neither the call nor the pack states one


def _fraction(raw: Any, field: str) -> Fraction:
    value = number(raw, field)
    return Fraction(int(value.p), int(value.q))


def _out(value: Fraction | sp.Expr) -> dict[str, Any]:
    if isinstance(value, Fraction):
        value = sp.Rational(value.numerator, value.denominator)
    return exact(value)


def _root(variance: Fraction) -> sp.Expr:
    """The standard deviation, exact (a surd when the variance is not a square)."""
    return sp.sqrt(sp.Rational(variance.numerator, variance.denominator))


# --- ungrouped: a list or a frequency table ---------------------------------------------------------------------------


class Ordered:
    """Sorted values with frequencies, read by 1-based position without expanding the table."""

    def __init__(self, pairs: list[tuple[Fraction, int]]) -> None:
        self.pairs = sorted(pairs)
        self.n = sum(f for _, f in self.pairs)

    def at(self, position: int) -> Fraction:
        seen = 0
        for value, frequency in self.pairs:
            seen += frequency
            if position <= seen:
                return value
        raise IndexError(position)

    def median_of(self, first: int, last: int) -> Fraction:
        """The median of the positions first..last (inclusive)."""
        count = last - first + 1
        middle = first + (count - 1) // 2
        return self.at(middle) if count % 2 else (self.at(middle) + self.at(middle + 1)) / 2

    def interpolated(self, position: Fraction) -> Fraction:
        """The value at a fractional position, by linear interpolation between neighbouring positions."""
        position = min(max(position, Fraction(1)), Fraction(self.n))
        low = int(position)
        share = position - low
        lower = self.at(low)
        return lower if share == 0 else lower + share * (self.at(low + 1) - lower)


def quartiles(data: Ordered, method: str) -> tuple[Fraction, Fraction, Fraction]:
    """Q1, the median and Q3. `exclusive`: medians of the halves below and above the median, leaving out the median
    itself when n is odd; `inclusive`: the halves include it; `interpolated`: the (n+1)/4, (n+1)/2 and 3(n+1)/4 th
    values with linear interpolation."""
    n = data.n
    median = data.median_of(1, n)
    if method == "interpolated":
        return data.interpolated(Fraction(n + 1, 4)), median, data.interpolated(Fraction(3 * (n + 1), 4))
    if n < 2:
        return median, median, median
    half = n // 2
    if n % 2 and method == "inclusive":
        return data.median_of(1, half + 1), median, data.median_of(half + 1, n)
    return data.median_of(1, half), median, data.median_of(n - half + 1, n)


def summary(data: Ordered, method: str) -> dict[str, Any]:
    n = data.n
    total = sum(v * f for v, f in data.pairs)
    mean = total / n
    variance = sum((f * (v - mean) * (v - mean) for v, f in data.pairs), Fraction(0)) / n
    top = max(f for _, f in data.pairs)
    modes = [v for v, f in data.pairs if f == top]
    q1, median, q3 = quartiles(data, method)
    sd = _root(Fraction(variance))
    return {
        "n": n,
        "sum": _out(total),
        "mean": _out(mean),
        "median": _out(median),
        "mode": [_out(m) for m in modes] if top > 1 or len(modes) == 1 else [],
        "range": _out(data.pairs[-1][0] - data.pairs[0][0]),
        "min": _out(data.pairs[0][0]),
        "max": _out(data.pairs[-1][0]),
        "quartiles": {"q1": _out(q1), "q2": _out(median), "q3": _out(q3), "method": method},
        "iqr": _out(q3 - q1),
        "variance": _out(variance),
        "standard_deviation": _out(sd),
        "population": True,
    }


# --- grouped data ----------------------------------------------------------------------------------------------------


def grouped(classes: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for i, c in enumerate(classes):
        lower, upper = _fraction(c["lower"], f"grouped[{i}].lower"), _fraction(c["upper"], f"grouped[{i}].upper")
        frequency = int(c["frequency"])
        if not lower < upper:
            raise WorkerError(Code.DOMAIN_ERROR, f"grouped[{i}]: the lower bound must be below the upper bound")
        rows.append((lower, upper, frequency))
    rows.sort()
    for (_, upper, _), (lower, _, _) in itertools.pairwise(rows):
        if lower < upper:
            raise WorkerError(Code.DOMAIN_ERROR, "grouped: the classes overlap")
    n = sum(f for _, _, f in rows)
    if n == 0:
        raise WorkerError(Code.DOMAIN_ERROR, "grouped: the total frequency is zero")
    total = sum((lo + hi) / 2 * f for lo, hi, f in rows)
    widths = {hi - lo for lo, hi, _ in rows}
    by_density = len(widths) > 1
    weight: Callable[[tuple[Fraction, Fraction, int]], Fraction] = (
        (lambda r: Fraction(r[2]) / (r[1] - r[0])) if by_density else (lambda r: Fraction(r[2]))
    )
    best = max(weight(r) for r in rows)
    modal = [{"lower": _out(lo), "upper": _out(hi)} for lo, hi, f in rows if weight((lo, hi, f)) == best]

    def estimate(position: Fraction) -> Fraction:
        cumulative = 0
        for lo, hi, f in rows:
            if f and cumulative + f >= position:
                return lo + (position - cumulative) / f * (hi - lo)
            cumulative += f
        return rows[-1][1]

    cumulative, running = [], 0
    for _lo, hi, f in rows:
        running += f
        cumulative.append({"upper": _out(hi), "cumulative_frequency": running})
    q1, median, q3 = estimate(Fraction(n, 4)), estimate(Fraction(n, 2)), estimate(Fraction(3 * n, 4))
    mean = total / n
    variance = sum((f * ((lo + hi) / 2 - mean) * ((lo + hi) / 2 - mean) for lo, hi, f in rows), Fraction(0)) / n
    return {
        "n": n,
        "mean": _out(mean),
        "midpoint_sum": _out(total),
        "modal_class": modal,
        "modal_by": "frequency_density" if by_density else "frequency",
        "median": _out(median),
        "quartiles": {"q1": _out(q1), "q2": _out(median), "q3": _out(q3), "method": "cumulative_interpolation"},
        "iqr": _out(q3 - q1),
        "variance": _out(variance),
        "standard_deviation": _out(_root(Fraction(variance))),
        "cumulative": cumulative,
        "estimated": True,
    }


# --- probability -------------------------------------------------------------------------------------------------


def probability(events: dict[str, Any]) -> dict[str, Any]:
    """P(A), P(B) and their relation (independent, mutually exclusive, or P(A and B) given) to the combined and
    conditional probabilities, as exact fractions."""
    p_a, p_b = _fraction(events["p_a"], "events.p_a"), _fraction(events["p_b"], "events.p_b")
    relation = events.get("relation")
    if "p_a_and_b" in events:
        both = _fraction(events["p_a_and_b"], "events.p_a_and_b")
    elif relation == "independent":
        both = p_a * p_b
    elif relation == "mutually_exclusive":
        both = Fraction(0)
    else:
        raise WorkerError(
            Code.DOMAIN_ERROR, "events: give p_a_and_b, or the relation independent or mutually_exclusive"
        )
    either = p_a + p_b - both
    for name, p in (("p_a", p_a), ("p_b", p_b), ("p_a_and_b", both), ("p_a_or_b", either)):
        if not 0 <= p <= 1:
            raise WorkerError(Code.DOMAIN_ERROR, f"events: {name} = {p} is not a probability")
    if both > min(p_a, p_b):
        raise WorkerError(Code.DOMAIN_ERROR, "events: P(A and B) cannot exceed P(A) or P(B)")
    out: dict[str, Any] = {
        "p_a": _out(p_a),
        "p_b": _out(p_b),
        "p_not_a": _out(1 - p_a),
        "p_not_b": _out(1 - p_b),
        "p_a_and_b": _out(both),
        "p_a_or_b": _out(either),
        "p_neither": _out(1 - either),
        "independent": both == p_a * p_b,
        "mutually_exclusive": both == 0,
    }
    if p_b:
        out["p_a_given_b"] = _out(both / p_b)
    if p_a:
        out["p_b_given_a"] = _out(both / p_a)
    return out


def sequence(probabilities: list[Any]) -> dict[str, Any]:
    """Successive outcomes (independent, or already conditional as given): the product, as on a tree diagram."""
    product = Fraction(1)
    for i, p in enumerate(probabilities):
        value = _fraction(p, f"sequence[{i}]")
        if not 0 <= value <= 1:
            raise WorkerError(Code.DOMAIN_ERROR, f"sequence[{i}] is not a probability")
        product *= value
    return {"product": _out(product), "steps": len(probabilities)}


# --- stats_compute ---------------------------------------------------------------------------------------------------


def _quartile_method(args: dict[str, Any], context: CallContext) -> tuple[str, list[dict[str, str]]]:
    if "quartile_method" in args:
        return args["quartile_method"], []
    held = convention(context, "quartile_method")
    if held is not None and held[0] in QUARTILE_METHODS:
        return str(held[0]), []
    message = f"no quartile method stated; the tool default {DEFAULT_QUARTILE_METHOD} was used"
    return DEFAULT_QUARTILE_METHOD, [{"code": "convention_default_used", "message": message}]


def stats_compute(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Exactly one of data, frequencies, grouped, events or sequence (VER-13)."""
    given = [k for k in ("data", "frequencies", "grouped", "events", "sequence") if k in args]
    if len(given) != 1:
        raise WorkerError(Code.DOMAIN_ERROR, "give exactly one of data, frequencies, grouped, events or sequence")
    kind = given[0]
    limit = int(context.setting("stats_max_points", 500))
    if kind in ("data", "frequencies", "grouped", "sequence") and len(args[kind]) > limit:
        raise WorkerError(Code.LIMIT_EXCEEDED, f"{kind} has more than {limit} items", limit="data_points")
    warnings: list[dict[str, str]] = []
    if kind in ("data", "frequencies"):
        if kind == "data":
            counts: dict[Fraction, int] = {}
            for i, raw in enumerate(args["data"]):
                v = _fraction(raw, f"data[{i}]")
                counts[v] = counts.get(v, 0) + 1
            pairs = list(counts.items())
        else:
            pairs = [(_fraction(r["value"], f"frequencies[{i}].value"), int(r["frequency"]))
                     for i, r in enumerate(args["frequencies"])]  # fmt: skip
            pairs = [(v, f) for v, f in pairs if f > 0]
        if not pairs:
            raise WorkerError(Code.DOMAIN_ERROR, f"{kind}: no values")
        merged: dict[Fraction, int] = {}
        for v, f in pairs:
            merged[v] = merged.get(v, 0) + f
        method, warnings = _quartile_method(args, context)
        value = {"kind": kind, **summary(Ordered(list(merged.items())), method)}
    elif kind == "grouped":
        value = {"kind": kind, **grouped(args["grouped"])}
    elif kind == "events":
        value = {"kind": kind, **probability(args["events"])}
    else:
        value = {"kind": kind, **sequence(args["sequence"])}
    extras: Extras = {"method": "exact", "confidence": "proved"}
    if warnings:
        extras["warnings"] = warnings
    return value, extras
