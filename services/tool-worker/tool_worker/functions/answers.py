"""Answer form and composite answer check (VER-10) with the numeric rules of TABLE VER-09a (VER-09).

`math_check_answer(student, key, rule)` returns separate components and an overall verdict computed only from the
item's marking rule:

- value: `correct`, `incorrect` or `within_tolerance` (`unknown` when the equivalence check is undecided, VER-27);
- unit: `ok`, `missing`, `wrong_dimension` or `convertible` (a correct value in another unit of the same dimension
  is correct unless the rule fixes the unit);
- precision: significant figures (`ok`, `too_few`, `too_many`) or decimal places (`ok`, `wrong`) by the row of TABLE
  VER-09a that applies (measurement, carried rounding, money, angle, exact, probability);
- form: `ok`, `unsimplified` for the target `simplest`, `not_<target>` for any other unmet target form, or
  `not_required` when the rule sets none.

The precision defaults (item precision, minimum figures, carried-rounding share, money and angle places, angle
tolerance, probability figures) come from the rule, then the pack's conventions, then the configured defaults."""

from __future__ import annotations

import math
import re
from fractions import Fraction
from tokenize import TokenError
from typing import Any

import pint
import sympy as sp
from sympy.core.relational import Relational

from tool_worker.functions.common import convention, parse_arg
from tool_worker.functions.maths import _expressions, _relations
from tool_worker.functions.numeric import (
    Judged,
    Reading,
    judge_angle,
    judge_dp,
    judge_probability,
    judge_sf,
    read_answer,
    round_sf_exact,
)
from tool_worker.functions.units import REGISTRY, parse_unit
from tool_worker.grammar import Parsed, huge_power
from tool_worker.protocol import WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
NUMERIC_ROWS = ("measurement", "money", "angle", "probability")
SEVERITY = {"ignore": 0, "partial": 1, "incorrect": 2}


def _setting(rule: dict[str, Any], context: CallContext, name: str, setting: str, default: Any) -> Any:
    """A precision parameter: the item's rule, then the pack's convention `numeric.<name>`, then the configured
    default."""
    if name in rule:
        return rule[name]
    held = convention(context, f"numeric.{name}")
    return held[0] if held is not None else context.setting(setting, default)


# --- the numeric path ----------------------------------------------------------------------------------------------


def _reading(text: str, money: bool) -> Reading | None:
    try:
        return read_answer(text, money=money)
    except ValueError:
        return None


def _unit_of(text: str) -> Any | None:
    """A unit from our unit grammar, or None when the text is not one (Pint refuses some text with a TypeError)."""
    try:
        return parse_unit(text)
    except (WorkerError, pint.errors.PintError, TokenError, TypeError, ValueError, AttributeError, KeyError):
        return None


def _is_unit(text: str) -> bool:
    """Text after a number that names a unit with a dimension (2pi is an expression, not 2 of a unit `pi`)."""
    if not text:
        return True
    unit = _unit_of(text)
    if unit is None:
        return False
    return not REGISTRY.Quantity(1, unit).dimensionless or text.strip() in ("°", "degrees", "degree")


def _unit(student: Reading, key: Reading) -> tuple[str, Fraction, dict[str, Any]]:
    """The unit component and the student's value in the key's unit."""
    if not key.unit:
        if not student.unit or _dimensionless(student.unit):
            return "ok", student.value, {}
        return "wrong_dimension", student.value, {"found": student.unit, "expected": "no unit"}
    if not student.unit:
        return "missing", student.value, {"expected": key.unit}
    source, target = _unit_of(student.unit), _unit_of(key.unit)
    try:
        converted = REGISTRY.Quantity(student.value, source).to(target).magnitude
    except (pint.errors.PintError, TypeError, ValueError, AttributeError):
        return "wrong_dimension", student.value, {"found": student.unit, "expected": key.unit}
    value = converted if isinstance(converted, Fraction) else Fraction(str(converted))
    if source == target:
        return "ok", value, {}
    return "convertible", value, {"converted": _fraction_text(value), "unit": key.unit}


def _dimensionless(text: str) -> bool:
    unit = _unit_of(text)
    return unit is not None and bool(REGISTRY.Quantity(1, unit).dimensionless)


def _fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _numeric(student_text: str, key: Reading, row: str, rule: dict[str, Any],
             context: CallContext) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:  # fmt: skip
    """Components and details for an answer judged by a numeric row of TABLE VER-09a."""
    student = _reading(student_text, row == "money")
    if student is None:
        return {"value": "incorrect", "unit": "missing" if key.unit else "ok", "precision": {}}, {
            "reason": "unreadable"}, []  # fmt: skip
    unit, value, unit_details = _unit(student, key)
    judged: Judged
    tolerance = rule.get("tolerance")
    if tolerance is not None:
        bound = Fraction(str(tolerance["value"])) * (abs(key.value) if tolerance["mode"] == "relative" else 1)
        status = "correct" if value == key.value else "within_tolerance" if abs(value - key.value) <= bound else (
            "incorrect")  # fmt: skip
        judged = (status, {"tolerance": tolerance["mode"]}, [])
    elif row == "money":
        judged = judge_dp(value, student.dp, key.value, int(_setting(rule, context, "dp", "money_dp", 2)))
    elif row == "angle":
        dp = int(_setting(rule, context, "dp", "angle_dp", 1))
        allowed = Fraction(str(_setting(rule, context, "angle_tolerance", "angle_tolerance_deg", 0.1)))
        judged = judge_angle(value, key.value, dp, allowed)
    elif row == "probability":
        judged = judge_probability(Reading(value, student.written, student.kind, student.sf, student.dp, ""),
                                   key.value, int(_setting(rule, context, "sf", "probability_sf", 3)))  # fmt: skip
    else:
        carried = None
        if rule.get("carried"):
            carried = Fraction(str(_setting(rule, context, "carried_share", "carried_rounding_share", 0.01)))
        judged = judge_sf(value, student.sf, key.value, min_sf=int(_setting(rule, context, "min_sf", "min_sf", 2)),
                          item_sf=int(_setting(rule, context, "sf", "default_sf", 3)), carried=carried)  # fmt: skip
    status, precision, warnings = judged
    if unit == "wrong_dimension":  # the number alone, so the feedback can say "right number, wrong unit"
        status = "correct" if student.value == key.value else status
    details: dict[str, Any] = {"row": row, **unit_details}
    return {"value": status, "unit": unit, "precision": precision}, details, warnings


# --- the expression path -------------------------------------------------------------------------------------------


def _numeric_value(tree: sp.Basic) -> Fraction | None:
    """An exact number's value to 50 digits as a fraction (for comparing a decimal against an exact key)."""
    if not isinstance(tree, sp.Expr) or tree.free_symbols or not tree.is_real:
        return None
    if tree.is_Rational:
        return Fraction(int(tree.p), int(tree.q))
    return Fraction(str(sp.N(tree, 50)))


def _exact_min_sf(rule: dict[str, Any], context: CallContext) -> int:
    """The fewest figures a decimal for an exact key may have: the item's or pack's `min_sf` when one is stated,
    otherwise the item's precision. The configured two-figure minimum is the measurement row's allowance for a
    measured key; an exact key such as pi has no measurement behind it, so 3 for pi is too few, not 'pi to 1 s.f.'."""
    if "min_sf" in rule or convention(context, "numeric.min_sf") is not None:
        return int(_setting(rule, context, "min_sf", "min_sf", 2))
    return int(_setting(rule, context, "sf", "default_sf", 3))


def _expression(student: Parsed, key: Parsed, student_text: str, rule: dict[str, Any],
                context: CallContext) -> tuple[dict[str, Any], dict[str, Any], Extras]:  # fmt: skip
    """Value by equivalence (VER-04); a decimal given for an exact number is compared at its own precision and needs
    at least the item's minimum figures unless it equals the key."""
    a, b = student.tree, key.tree
    is_relation = (isinstance(a, (Relational, sp.And)), isinstance(b, (Relational, sp.And)))
    if is_relation[0] != is_relation[1]:
        return {"value": "incorrect", "unit": "ok", "precision": {}}, {"reason": "relation_mismatch"}, {
            "method": "rule_based", "confidence": "proved"}  # fmt: skip
    key_number = _numeric_value(b) if not is_relation[1] else None
    reading = _reading(student_text, False) if key_number is not None else None
    if key_number is not None and reading is not None and reading.kind == "decimal" and not reading.unit:
        if reading.value == key_number:
            status = "correct"
        else:
            status = "within_tolerance" if reading.value == round_sf_exact(key_number, reading.sf or 1) else (
                "incorrect")  # fmt: skip
        too_few = reading.value != key_number and (reading.sf or 1) < _exact_min_sf(rule, context)
        precision = {"sf": "too_few" if too_few else "ok", "student_sf": reading.sf}
        return {"value": status, "unit": "ok", "precision": precision}, {"row": "exact"}, {
            "method": "exact", "confidence": "proved"}  # fmt: skip
    found, extras = (_relations if is_relation[0] else _expressions)(a, b, context)
    verdict = found["verdict"]
    status = "correct" if verdict in ("equivalent", "equivalent_on_domain") else (
        "incorrect" if verdict == "not_equivalent" else "unknown")  # fmt: skip
    details: dict[str, Any] = {"row": "exact", "equivalence": verdict}
    if found.get("counterexample"):
        details["counterexample"] = found["counterexample"]
    return {"value": status, "unit": "ok", "precision": {"exact": True}}, details, extras


# --- forms -------------------------------------------------------------------------------------------------------

_DECIMAL = re.compile(r"\d\.\d|(?<!\d)\.\d|\d\.(?!\d)")
_STANDARD_FORM = re.compile(
    r"^\s*[+-]?[1-9](?:\.\d+)?\s*(?:[x\u00d7*]|\\times|\\cdot)\s*10\s*\^\s*(?:\{\s*[+-]?\d+\s*\}|\(?[+-]?\d+\)?)\s*"
    r"(?P<unit>.*)$"
)
_BEARING = re.compile(r"^\s*\d{3}(?:\.\d+)?\s*(?:°|\^\{?\\circ\}?|degrees?)?\s*$")
_SURD = re.compile(r"(?:sqrt\s*\(?|√\s*\(?|\\sqrt\s*\{)\s*(\d+)")


def _evaluate(node: sp.Basic) -> sp.Basic:
    """The written tree with evaluation applied (bottom up)."""
    if not node.args:
        return node
    return node.func(*[_evaluate(a) for a in node.args])


def _terms(node: sp.Basic) -> list[sp.Basic]:
    if isinstance(node, sp.Add):
        return [t for a in node.args for t in _terms(a)]
    return [node]


def _factors(node: sp.Basic) -> list[sp.Basic]:
    if isinstance(node, sp.Mul):
        return [f for a in node.args for f in _factors(a)]
    return [node]


def _is_negation(node: sp.Basic) -> bool:
    return isinstance(node, sp.Mul) and len(node.args) == 2 and node.args[0] == -1


def _unsimplified_node(node: sp.Basic) -> bool:
    if isinstance(node, sp.Add):
        terms = _terms(node)
        return len(sp.Add.make_args(sp.Add(*[_evaluate(t) for t in terms]))) < len(terms)
    if isinstance(node, sp.Mul) and not _is_negation(node):
        factors = [f for f in _factors(node) if f != -1]
        numerators = [f for f in factors if f.is_Integer]
        denominators = [f.base for f in factors if isinstance(f, sp.Pow) and f.base.is_Integer and f.exp == -1]
        if len([n for n in numerators if abs(n) != 1]) > 1 or len(denominators) > 1:
            return True  # numbers left to multiply out
        p = math.prod(int(n) for n in numerators) if numerators else 1
        q = math.prod(int(d) for d in denominators) if denominators else 1
        if denominators and (q == 1 or math.gcd(p, q) > 1):
            return True  # a fraction not in lowest terms
        bases = [f.base if isinstance(f, sp.Pow) else f for f in factors if not f.is_number]
        return len(bases) != len(set(bases))  # like factors, x*x
    if isinstance(node, sp.Pow):
        return bool(node.base.is_Integer and node.exp.is_Integer and int(node.exp) > 1)
    return False


def _surd_unsimplified(text: str, written: sp.Basic) -> bool:
    for match in _SURD.finditer(text):
        n = int(match.group(1))
        if any(n % (k * k) == 0 for k in range(2, math.isqrt(n) + 1)):
            return True  # a square factor left under the root
    for node in sp.preorder_traversal(written):
        if isinstance(node, sp.Pow) and node.exp.is_negative:
            if any(isinstance(p, sp.Pow) and p.exp.is_Rational and not p.exp.is_Integer
                   for p in sp.preorder_traversal(node.base)):  # fmt: skip
                return True  # a surd left in the denominator
    return False


def _factorised(tree: sp.Expr, written: sp.Basic) -> bool:
    variables = sorted(tree.free_symbols, key=lambda s: s.name)
    if not variables or not tree.is_polynomial(*variables):
        return isinstance(written, (sp.Mul, sp.Pow))
    content, factors = sp.factor_list(tree)
    if not factors or (len(factors) == 1 and factors[0][1] == 1 and abs(content) == 1):
        return True  # irreducible: any written form is fully factorised
    if isinstance(written, sp.Add):
        return False
    for factor in _factors(written):
        base = factor.base if isinstance(factor, sp.Pow) and factor.exp.is_Integer else factor
        base = _evaluate(base)
        if base.is_number or not base.free_symbols:
            continue
        part_content, parts = sp.factor_list(base)
        if abs(part_content) != 1 or len(parts) != 1 or parts[0][1] != 1:
            return False
    return True


def _single_fraction(written: sp.Basic) -> bool:
    for node in sp.preorder_traversal(written):
        if isinstance(node, sp.Add) and len(_terms(node)) > 1:
            for term in _terms(node):
                if any(isinstance(f, sp.Pow) and f.exp.is_negative for f in _factors(term)):
                    return False
    return True


def _expanded(tree: sp.Expr, written: sp.Basic) -> bool:
    for node in sp.preorder_traversal(written):
        if isinstance(node, sp.Mul) and not _is_negation(node):
            if any(isinstance(f, sp.Add) and f.free_symbols for f in _factors(node)):
                return False
        if isinstance(node, sp.Pow) and isinstance(node.base, sp.Add) and node.exp.is_Integer and node.exp > 1:
            return False
    return len(_terms(written)) <= len(sp.Add.make_args(sp.expand(tree)))


def form_met(target: str, text: str, parsed: Parsed | None, reading: Reading | None) -> bool:
    """Whether the student's answer as written is in the target form (VER-10)."""
    if target == "standard_form":
        return bool(_STANDARD_FORM.match(text)) or (
            reading is not None and reading.kind == "decimal" and 1 <= abs(reading.value) < 10
            and not re.search(r"[eE]", reading.written))  # fmt: skip
    if target == "decimal":
        return reading is not None and reading.kind == "decimal"
    if target == "mixed_number":
        if reading is None:
            return False
        if reading.kind == "mixed":
            p, q = (int(n) for n in re.findall(r"\d+", reading.written)[-2:])
            return p < q and math.gcd(p, q) == 1
        if reading.kind == "fraction":
            return abs(reading.value) < 1 and reading.value == Fraction(reading.written.replace(" ", ""))
        return reading.value.denominator == 1 and "." not in reading.written
    if target == "bearing":
        return bool(_BEARING.match(text)) and reading is not None and 0 <= reading.value < 360
    if target == "exact":
        return not _DECIMAL.search(text)
    if parsed is None:
        if reading is None:
            return False
        if target in ("simplest", "single_fraction") and reading.kind == "fraction":
            p, q = (int(n) for n in re.findall(r"\d+", reading.written)[-2:])
            return math.gcd(p, q) == 1 and q != 1
        return True
    written, tree = parsed.as_written, parsed.tree
    if huge_power(tree):
        return False  # expanding or factorising it runs where the per-call budget cannot stop it (SEC-08): unchecked
    if target == "simplest":
        nodes = list(sp.preorder_traversal(written))
        if any(_unsimplified_node(n) for n in nodes) or _surd_unsimplified(text, written):
            return False
        numerator, denominator = sp.fraction(sp.together(tree))
        common = sp.gcd(numerator, denominator) if numerator.free_symbols or denominator.free_symbols else 1
        if isinstance(common, sp.Expr) and not common.is_number:
            return False
        # The fraction as written: evaluation cancels a factor left on the top and the bottom (6(x+2) over
        # (x-2)(x+2)) and divides out a common number ((2x+2)/4), so either is still unsimplified.
        top, bottom = (_evaluate(side) for side in sp.fraction(written))
        if bottom != 1 and (top.free_symbols or bottom.free_symbols):
            shared = sp.gcd(top, bottom)
            return bool(shared.is_number and abs(shared) == 1)
        return True
    if target == "surd_simplest":
        return not _surd_unsimplified(text, written) and not any(
            isinstance(n, sp.Add) and _unsimplified_node(n) for n in sp.preorder_traversal(written))  # fmt: skip
    if target == "factorised":
        return _factorised(tree, written)
    if target == "expanded":
        return _expanded(tree, written)
    if target == "single_fraction":
        return _single_fraction(written)
    return True


def _form(target: str | None, text: str, parsed: Parsed | None, reading: Reading | None) -> str:
    if target is None:
        return "not_required"
    if form_met(target, text, parsed, reading):
        return "ok"
    return "unsimplified" if target == "simplest" else f"not_{target}"


# --- the overall verdict ---------------------------------------------------------------------------------------------


def overall(components: dict[str, Any], rule: dict[str, Any]) -> tuple[str, list[str]]:
    """The verdict from the item's marking rule only: a wrong value or dimension is incorrect; a unit fixed by the
    rule and given in another unit is incorrect; a missing unit, unmet precision or unmet form costs what the rule
    says (by default partial for a missing unit or form, and not accepted for too few figures or wrong places, as
    TABLE VER-09a states). `too_many` is feedback only and never costs anything (VER-09)."""
    if components["value"] == "unknown":
        return "unknown", ["value"]
    if components["value"] == "incorrect":
        return "incorrect", ["value"]
    if components["unit"] == "wrong_dimension":
        return "incorrect", ["unit"]
    costs: list[tuple[str, str]] = []
    policy = rule.get("unit_policy", "convertible")
    if components["unit"] == "convertible" and policy == "exact":
        costs.append(("incorrect", "unit"))
    if components["unit"] == "missing" and policy != "none":
        costs.append((rule.get("on_unit_missing", "partial"), "unit"))
    precision = components["precision"]
    if precision.get("sf") == "too_few" or precision.get("dp") == "wrong":
        costs.append((rule.get("on_precision_unmet", "incorrect"), "precision"))
    if components["form"] not in ("ok", "not_required"):
        costs.append((rule.get("on_form_unmet", "partial"), "form"))
    worst = max((SEVERITY[c] for c, _ in costs), default=0)
    if worst == 0:
        return "correct", []
    return ("incorrect" if worst == 2 else "partial"), [part for c, part in costs if SEVERITY[c] == worst]


# --- math_check_answer -------------------------------------------------------------------------------------------


def math_check_answer(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """A student's final answer against the item's key under its marking rule (VER-10, VER-09)."""
    rule: dict[str, Any] = dict(args.get("rule", {}))
    student_text, key_text = args["student"], args["key"]
    target = rule.get("form")
    row = rule.get("quantity")
    if row is None and target == "bearing":
        row = "angle"
    key_reading = _reading(key_text, row == "money")
    numeric = key_reading is not None and _is_unit(key_reading.unit) and row != "exact"
    if numeric and row is None and key_reading is not None:
        row = "exact" if key_reading.kind in ("fraction", "mixed") else "measurement"
        numeric = row != "exact"
    warnings: list[dict[str, str]] = []
    if numeric and key_reading is not None:
        components, details, warnings = _numeric(student_text, key_reading, row or "measurement", rule, context)
        extras: Extras = {"method": "exact", "confidence": "proved"}
        parsed = None
    else:
        parsed = parse_arg(student_text, args, "student", sampled=True)  # a huge power is only sampled
        key = parse_arg(key_text, args, "key", sampled=True)
        components, details, extras = _expression(parsed, key, student_text, rule, context)
        if target is None and row == "exact" and key.tree.is_number:
            target = "exact"  # TABLE VER-09a: a decimal where an exact answer is asked leaves the form unmet
    reading = _reading(student_text, row == "money")
    components["form"] = _form(target, student_text, parsed, reading)
    if target is not None:
        details["form_target"] = target
    verdict, because = overall(components, rule)
    value: dict[str, Any] = {"verdict": verdict, "components": components, "details": details}
    if because:
        value["because"] = because
    if warnings:
        extras = {**extras, "warnings": warnings}
    return value, extras
