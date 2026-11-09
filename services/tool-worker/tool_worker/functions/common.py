"""Helpers shared by the compute functions: parsing with the call's options, and errors."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.grammar import Parsed, ParseRejected, TooLarge, bounded_subs, huge_power, parse
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

ASSUMPTION_FLAGS = {"positive": "positive", "nonzero": "nonzero", "integer": "integer", "nonnegative": "nonnegative"}


def parse_arg(
    text: str, args: dict[str, Any], field: str = "text", *, imaginary_unit: bool = False, sampled: bool = False
) -> Parsed:
    """Parse one argument; grammar errors become PARSE_REJECTED with the position, size breaches LIMIT_EXCEEDED.
    With `imaginary_unit` the letter `i` is parsed as a non-real symbol (never simplified as a real variable), for the
    caller to replace with the imaginary unit (VER-30). A power with an exponent beyond the grammar's bound, such as
    (x+1)^(10^29), is LIMIT_EXCEEDED unless the caller only samples the tree (`sampled`): symbolic work on it runs where
    the per-call budget cannot stop it (SEC-08)."""
    assumptions = {
        name: {ASSUMPTION_FLAGS[flag]: True for flag in flags} for name, flags in args.get("assumptions", {}).items()
    }
    if imaginary_unit:
        assumptions["i"] = {"real": False, "imaginary": True}
    try:
        parsed = parse(
            text,
            variables=frozenset(args.get("variables", [])),
            assumptions=assumptions,
            log_base=int(args.get("log_base", 10)),  # base 10 unless the pack says otherwise (VER-03)
            decimal_comma=bool(args.get("decimal_comma", False)),
        )
    except ParseRejected as error:
        raise WorkerError(Code.PARSE_REJECTED, f"{field}: {error}", position=error.position) from None
    except TooLarge as error:
        raise WorkerError(Code.LIMIT_EXCEEDED, f"{field}: {error}", limit=error.limit) from None
    if not sampled and huge_power(parsed.tree):
        raise WorkerError(Code.LIMIT_EXCEEDED, f"{field}: the power is too large to work with", limit="components")
    return parsed


def substituted(tree: sp.Basic, values: dict[sp.Basic, sp.Basic], field: str) -> sp.Basic:
    """`tree` at the values the call gave in `field`, refused as LIMIT_EXCEEDED when that makes a power too large to
    compute (2^x at x = 10^29): the exact power runs in C, where the per-call budget cannot stop it (SEC-08)."""
    try:
        return bounded_subs(tree, values)
    except TooLarge as error:
        raise WorkerError(Code.LIMIT_EXCEEDED, f"{field}: {error}", limit=error.limit) from None


def as_expr(value: sp.Basic | Fraction | int | float) -> sp.Basic:
    """An already-computed number as a SymPy value, built explicitly: SymPy values are kept as they are, a Python int
    or Fraction becomes an exact Integer or Rational and a float a Float. Unlike sympify it never parses text, so no
    string can reach SymPy's parser through it (SEC-07); anything else is a programming error."""
    if isinstance(value, sp.Basic):
        return value
    if isinstance(value, bool):
        raise TypeError("as_expr: a bool is not a number")
    if isinstance(value, int):
        return sp.Integer(value)
    if isinstance(value, Fraction):
        return sp.Rational(value.numerator, value.denominator)
    if isinstance(value, float):
        return sp.Float(value)
    raise TypeError(f"as_expr: expected a SymPy value or a Python number, got {type(value).__name__}")


def convention(context: CallContext, key: str) -> tuple[Any, str] | None:
    """A convention sent with the call, in the worker protocol's shape (`{value, source}`, unit optional), as
    (value, source); None when the call states none. The host sends each pack setting under one flat key such as
    `numeric.sf` (G103, G104)."""
    held = context.conventions.get(key) if isinstance(context.conventions, dict) else None
    if isinstance(held, dict) and "value" in held:
        return held["value"], str(held.get("source", "pack"))
    return None
