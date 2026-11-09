"""Deterministic marking of pack items (ASM-02, ASM-03, ASM-04, ASM-06, ASM-07, ASM-17).

A verdict's outcome and marks come only from a key or a tool: the item's build-checked key, and the worker's
checkers at run time (unit conversion, `chem_balance`, `acc_double_entry`, `math_evaluate`). Each verdict names its
provenance, the check ids it used or a key criterion. A model may phrase the feedback, but an outcome it proposes is
discarded when it disagrees with the marker (`settle`). A type with no marker here cannot be built into a pack; one
with no applicable key or tool marks `unverifiable`, which makes no mastery observation (LRN-06).

Markers in this build: `numeric` (ASM-04), `mcq`, `mcq_diagnostic`, `multi_select` and `ordering` (keys), and the
structured answers of TABLE ASM-03a: `chemical_equation` (`chem_balance`: species, balance, lowest terms, state
symbols by the item's policy; CUR-15), `journal_entry` (`acc_double_entry`: a verdict per line, the entry balancing;
ASM-07) and `accounting_table` (debit and credit lines by `acc_double_entry`; every line in the key's order, the
first divergent line reported; ASM-07), and the free-response maths answers: `algebraic` (value by
`math_check_equivalence` under the item's assumptions, then the required form by `math_check_answer` or, for a
formula's subject, by the solver; ASM-05, VER-04, VER-10) and `equation_solutions` (the student's values against the
key's as sets by `math_check_answer`, a missing or an extra value incorrect; ASM-03, VER-07).

`diagram_label` (ASM-08, VSL-28): each target part id against its accepted names and aliases, case and spacing
ignored, with the configured edits tolerated for names of the configured length; a mark share per target, each target
a key criterion. A response maps target part ids to the labels placed (a mapping, or a list of `{part_id, label}`).

`structured` (ASM-06, ASM-17, CUR-15): each part is marked on its own by the marker of its answer kind. A structured
item's response is a mapping from part id to that part's response (`{"a": "9 J", "b": "3 m/s"}`); marking part `b`
reads the responses of the parts it uses from the same mapping, and a response that is not such a mapping is the
marked part's own. When a part's answer is wrong, the item marks it for error carried forward (`ecf: true`), it names
the parts it uses (`uses_parts`) and it carries a follow-through recipe (`follow_through`: an expression for
`math_evaluate`, with each symbol bound to an earlier part's value and the item's `quantities` given), the part's
expected answer is recomputed from the student's own earlier values with the same tool and the part is awarded if
the answer agrees with it (criteria flagged `follow_through`). A part with mark points is scored by them: a right (or
consistent) answer passes every point, an answer right in value but not in full (a missing unit, too few figures, the
form) loses its A points only, a wrong answer passes none, and an A point never passes without the M point it depends
on (its `depends_on`, else the nearest M before it). Method marks from working lines are not read in this build.

Misconception signatures (CUR-18) are matched on an incorrect or partial verdict when the caller passes the
misconception library (`signatures`, `agent_core.engine.signatures`); without it every verdict is what it always was.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from typing import Any

MARKER_VERSION = {"numeric": "numeric.1", "key": "key.1", "chemical_equation": "chem_balance.1",
                  "journal_entry": "acc_double_entry.1", "accounting_table": "acc_table.1",
                  "algebraic": "math_algebraic.1", "equation_solutions": "math_solutions.1",
                  "diagram_label": "diagram_label.1", "structured": "structured.1"}  # fmt: skip
_NUMBER = re.compile(
    r"^\s*(?P<num>[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:(?:[x\u00d7*]|\\times)\s*10\s*\^\s*\{?\s*(?P<pow>[-+]?\d+)\s*\}?"
    r"|[eE](?P<exp>[-+]?\d+))?\s*(?P<unit>.*?)\s*$"
)


@dataclass(frozen=True)
class Converted:
    value: Decimal
    check_id: str


Convert = Callable[[str, str], Awaitable[Converted | None]]
# One checker call on the worker: the tool's result value and its check id, or (None, None) when the tool refused the
# arguments or could not run. The marker never guesses past a missing result: the verdict is `unverifiable`.
RunTool = Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any] | None, str | None]]]


@dataclass(frozen=True)
class Verdict:
    outcome: str  # correct | incorrect | partial | unverifiable
    marks_awarded: float
    marks_available: float
    marker_version: str
    check_ids: tuple[str, ...] = ()
    error_category: str | None = None
    misconception_id: str | None = None
    criteria: tuple[dict[str, Any], ...] = ()
    first_error_step: int | None = None  # a structured answer's first divergent line, from 1 (ASM-07)
    discarded_model_outcome: str | None = field(default=None, compare=False)

    @property
    def provenance(self) -> bool:
        """A check id, or a criterion decided by a key or a tool."""
        return bool(self.check_ids) or any(c["method"] in ("key", "tool") for c in self.criteria)

    def payload(self, *, verdict_id: str, attempt_id: str, closes_opportunity: bool) -> dict[str, Any]:
        """The learning-event `verdict` payload."""
        out: dict[str, Any] = {"verdict_id": verdict_id, "attempt_id": attempt_id, "outcome": self.outcome,
                               "marks_awarded": self.marks_awarded, "marks_available": self.marks_available,
                               "check_ids": list(self.check_ids), "closes_opportunity": closes_opportunity,
                               "marker_version": self.marker_version}  # fmt: skip
        if self.error_category is not None:
            out["error_category"] = self.error_category
        if self.misconception_id is not None:
            out["misconception_id"] = self.misconception_id
        if self.criteria:
            out["criteria"] = [dict(c) for c in self.criteria]
        if self.first_error_step is not None:
            out["first_error_step"] = self.first_error_step
        return out


def settle(model_outcome: str | None, verdict: Verdict) -> Verdict:
    """The marker's verdict stands; a model's differing outcome is recorded as discarded, never applied (ASM-02)."""
    if model_outcome is None or model_outcome == verdict.outcome:
        return verdict
    return replace(verdict, discarded_model_outcome=model_outcome)


# --- numeric (ASM-04) -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Parsed:
    value: Decimal
    unit: str | None
    sig_figs: int


_FRACTION = re.compile(r"^\s*(?P<p>[-+]?\d+)\s*/\s*(?P<q>\d+)\s*(?P<unit>.*?)\s*$")
EXACT_FIGURES = 28  # an exact fraction carries every figure (VER-03a accepts fractions)


_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_MINUSES = str.maketrans({"\u2212": "-", "\u2013": "-", "\u00b7": "*", "\u22c5": "*"})


def canonical_number_text(text: str) -> str:
    """Every reasonable way of typing standard form and a unit power, as the one form `_NUMBER` reads: the minus
    sign and dot operator, "X" and "**" for times and power, "10^(-4)", superscript digits ("10⁻⁴", "m/s²",
    "mol dm⁻³"), and thousands grouped with spaces ("12 000")."""
    out = text.translate(_MINUSES)
    out = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+", lambda m: "^" + m.group(0).translate(_SUPERSCRIPTS), out)
    out = out.replace("**", "^")
    out = re.sub(r"(?<=\d)\s*X\s*(?=10\s*\^)", "x", out)
    out = re.sub(r"\^\s*\(\s*([-+]?\d+)\s*\)", r"^\1", out)
    out = re.sub(
        r"(?<![\d.])(\d{1,3})((?:[ \u2009\u202f]\d{3})+)(?![\d/])",
        lambda m: m.group(1) + re.sub(r"\s", "", m.group(2)),
        out,
    )
    return out


# What follows the number is no unit if it carries on the sum or offers a second value: "3 or 4", "3+4", "3^2", "4x".
_NOT_A_UNIT = re.compile(r"^[-+*^=<>,;:)]|^/\s*\d|(?:^|\s)\d|\b(?:or|and|to|plus|minus|times)\b|^[xyz]$", re.IGNORECASE)


def parse_numeric(text: str) -> Parsed | None:
    """A typed answer: a number (decimal, `e` or times-ten-to-the notation, or a simple fraction) and an optional
    unit."""
    cleaned = canonical_number_text(text).strip().removeprefix("=").strip()
    if "=" in cleaned:  # "v = 8.0 m/s": the value after the last equals sign
        cleaned = cleaned.rsplit("=", 1)[1]
    fraction = _FRACTION.match(cleaned)
    if fraction is not None and int(fraction["q"]) != 0:
        unit = fraction["unit"].strip() or None
        if unit is not None and _NOT_A_UNIT.search(unit):
            return None
        return Parsed(Decimal(fraction["p"]) / Decimal(fraction["q"]), unit, EXACT_FIGURES)
    match = _NUMBER.match(cleaned.replace(",", ""))
    if match is None:
        return None
    try:
        mantissa = Decimal(match.group("num"))
    except InvalidOperation:
        return None
    power = match.group("pow") or match.group("exp")
    try:
        value = mantissa.scaleb(int(power)) if power else mantissa
    except (DecimalException, ValueError, OverflowError):
        return None
    unit = match.group("unit").replace("\\,", "").replace("\\ ", "").strip() or None
    if unit is not None and unit.startswith("\\text{") and unit.endswith("}"):
        unit = unit[6:-1].strip()
    if unit is not None and _NOT_A_UNIT.search(unit):
        return None
    return Parsed(value, unit, significant_figures(match.group("num")))


def significant_figures(text: str) -> int:
    digits = text.lstrip("+-")
    if "." in digits:
        whole, fraction = digits.split(".", 1)
        stripped = (whole + fraction).lstrip("0")
        return max(1, len(stripped)) if stripped else max(1, len(fraction))
    stripped = digits.lstrip("0").rstrip("0")  # trailing zeros of a whole number are not significant
    return max(1, len(stripped))


def _close(a: Decimal, b: Decimal, tolerance: Mapping[str, Any] | None, default_relative: float) -> bool:
    if tolerance is None or tolerance["mode"] == "sig_figs":
        return abs(a - b) <= Decimal(str(default_relative)) * abs(b)
    value = Decimal(str(tolerance["value"]))
    return abs(a - b) <= (value if tolerance["mode"] == "absolute" else value * abs(b))


def _power_of_ten(a: Decimal, b: Decimal, tolerance: Mapping[str, Any] | None, default_relative: float) -> bool:
    if a == 0 or b == 0 or (a < 0) != (b < 0):
        return False
    # Compare the neighbouring powers directly: a student's exponent may be outside a float's range.
    exponent = abs(a).adjusted() - abs(b).adjusted()
    return any(
        k != 0 and abs(k) <= 6 and _close(a, b.scaleb(k), tolerance, default_relative)
        for k in (exponent - 1, exponent, exponent + 1)
    )


def round_figures(value: Decimal, figures: int) -> Decimal:
    """`value` to `figures` significant figures, half away from zero (VER-09)."""
    if value == 0:
        return value
    exponent = value.adjusted() - figures + 1
    return value.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_HALF_UP)


MIN_PRECISION = 2  # VER-09: fewer than two significant figures is never accepted


def stated_precision(item: Mapping[str, Any], part: Mapping[str, Any]) -> int:
    """The item's stated precision in significant figures (VER-09): the key check's `sf` in a source item, else a
    significant-figures tolerance, else the figures the key is written to (a pack item keeps no checks), never below
    two."""
    answer = part["answer"]
    check = (item.get("checks") or {}).get(answer.get("check_id") or "", {})
    figures = (check.get("args") or {}).get("sf")
    tolerance = answer.get("tolerance") or {}
    if figures is None and tolerance.get("mode") == "sig_figs":
        figures = tolerance["value"]
    if figures is None:
        written = str(answer["value"]).strip()
        key = parse_numeric(written)
        if key is None:
            figures = MIN_PRECISION
        elif re.fullmatch(r"[-+]?\d+", written):
            # A whole-number key's trailing zeros are ambiguous ("2000"), so every digit counts: reading it as one
            # figure would accept 2049 for $2000. An item that means fewer figures states them (sf or tolerance).
            figures = len(written.lstrip("+-").lstrip("0")) or 1
        else:
            figures = key.sig_figs
    return max(MIN_PRECISION, int(figures))


async def mark_numeric(part: Mapping[str, Any], response: str, convert: Convert, settings: Any,
                       precision: int | None = None) -> Verdict:  # fmt: skip
    """`precision` is the item's stated significant figures: an answer with more figures is accepted when it equals
    the key at that precision (VER-09 `too_many`, feedback only, never costing the mark)."""
    answer = part["answer"]
    marks = float(part["marks"])
    version = MARKER_VERSION["numeric"]
    key = Decimal(answer["value"])
    key_unit = answer["unit"]
    tolerance = answer.get("tolerance")
    default = settings.numeric_default_relative_tolerance
    check_ids = [answer["check_id"]]

    def verdict(outcome: str, category: str | None = None) -> Verdict:
        awarded = marks if outcome == "correct" else max(0.0, marks - 1) if outcome == "partial" else 0.0
        return Verdict(outcome, awarded, marks, version, tuple(check_ids), category)

    parsed = parse_numeric(response)
    if parsed is None:
        return verdict("incorrect", "unreadable")
    value = parsed.value

    def matches(candidate: Decimal) -> bool:
        if _close(candidate, key, tolerance, default):
            return True
        return (precision is not None and parsed.sig_figs > precision
                and round_figures(candidate, precision) == round_figures(key, precision))  # fmt: skip

    units_matter = key_unit is not None and answer["unit_policy"] != "none"
    if units_matter and parsed.unit is not None:
        converted = await convert(f"{parsed.value} {parsed.unit}", key_unit)
        if converted is None:
            # Not the same kind of quantity, unless the worker cannot convert anything: never mark past a missing check.
            if await convert("1 m", "cm") is None:
                return verdict("unverifiable")
            return verdict("incorrect", "unit_error" if matches(value) else "value")
        check_ids.append(converted.check_id)
        same_unit = converted.value == parsed.value
        if matches(converted.value):
            if answer["unit_policy"] == "exact" and not same_unit:
                return verdict("incorrect", "unit_error")
            value = converted.value
        elif matches(parsed.value):
            return verdict("incorrect", "unit_error")  # the right number with the wrong unit
        elif _power_of_ten(converted.value, key, tolerance, default):
            return verdict("incorrect", "power_of_ten")
        else:
            return verdict("incorrect", "value")
    elif not matches(value):
        return verdict("incorrect", "power_of_ten" if _power_of_ten(value, key, tolerance, default) else "value")
    if tolerance is not None and tolerance["mode"] == "sig_figs":
        # Too few figures beyond the slack is partial; too many is feedback only and costs nothing (VER-09, G57).
        if int(tolerance["value"]) - parsed.sig_figs > settings.numeric_sig_figs_slack:
            return verdict("partial", "sig_figs")
    if units_matter and parsed.unit is None:
        return verdict("partial", "units_missing")
    return verdict("correct")


# --- keys -----------------------------------------------------------------------------------------------------------


def _key_criterion(passed: bool) -> tuple[dict[str, Any], ...]:
    return ({"criterion_id": "key", "mark_type": "B", "result": "pass" if passed else "fail", "method": "key"},)


def mark_choice(part: Mapping[str, Any], response: str) -> Verdict:
    options = {o["id"]: o for o in part["answer"]["options"]}
    marks = float(part["marks"])
    chosen = options.get(response.strip())
    if chosen is None:
        return Verdict("incorrect", 0.0, marks, MARKER_VERSION["key"], (), "unreadable", None, _key_criterion(False))
    if chosen["correct"]:
        return Verdict("correct", marks, marks, MARKER_VERSION["key"], (), None, None, _key_criterion(True))
    return Verdict("incorrect", 0.0, marks, MARKER_VERSION["key"], (), "misconception" if chosen.get(
        "misconception_id") else "value", chosen.get("misconception_id"), _key_criterion(False))  # fmt: skip


def mark_multi(part: Mapping[str, Any], response: list[str]) -> Verdict:
    answer = part["answer"]
    marks = float(part["marks"])
    correct = {o["id"] for o in answer["options"] if o["correct"]}
    chosen = set(response)
    if chosen == correct:
        return Verdict("correct", marks, marks, MARKER_VERSION["key"], (), criteria=_key_criterion(True))
    if answer["scoring"] == "per_option":
        right = len(chosen & correct) - len(chosen - correct)
        awarded = max(0.0, marks * right / len(correct))
        if awarded > 0:
            return Verdict("partial", awarded, marks, MARKER_VERSION["key"], (), "other", None, _key_criterion(False))
    return Verdict("incorrect", 0.0, marks, MARKER_VERSION["key"], (), "value", None, _key_criterion(False))


def mark_order(part: Mapping[str, Any], response: list[str]) -> Verdict:
    answer = part["answer"]
    marks = float(part["marks"])
    expected = list(answer["correct_order"])
    if list(response) == expected:
        return Verdict("correct", marks, marks, MARKER_VERSION["key"], (), criteria=_key_criterion(True))
    if answer["scoring"] == "adjacent_pairs":
        pairs = set(itertools.pairwise(expected))
        hit = sum(1 for pair in itertools.pairwise(response) if pair in pairs)
        if hit:
            awarded = marks * hit / len(pairs)
            return Verdict("partial", awarded, marks, MARKER_VERSION["key"], (), "other", None, _key_criterion(False))
    return Verdict("incorrect", 0.0, marks, MARKER_VERSION["key"], (), "value", None, _key_criterion(False))


# --- structured answers marked by the worker's checkers (ASM-03, ASM-07, CUR-15) -------------------------------------


def _unverifiable(part: Mapping[str, Any], version: str, check_ids: tuple[str, ...] = ()) -> Verdict:
    return Verdict("unverifiable", 0.0, float(part["marks"]), version, check_ids)


def _tool_criterion(criterion_id: str, passed: bool, method: str = "tool") -> dict[str, Any]:
    return {"criterion_id": criterion_id, "mark_type": "B", "result": "pass" if passed else "fail", "method": method}


_STATE = re.compile(r"\((s|l|g|aq)\)$")
_COEFFICIENT = re.compile(r"[0-9]+(/[0-9]+)?")


def _species(text: str) -> tuple[str, str | None]:
    """A formula as written, without spaces or `^`, and its state symbol."""
    body = re.sub(r"[\s^]", "", text)
    state = _STATE.search(body)
    return (body[: state.start()], state.group(1)) if state else (body, None)


def _skeleton(answer: Mapping[str, Any]) -> tuple[str, list[tuple[str, str, str | None]]]:
    """The key's equation without coefficients, and its species as (side, formula, state) in order."""
    species = [(side, _species(s["formula"])[0], s.get("state")) for side in ("reactants", "products")
               for s in answer[side]]  # fmt: skip
    sides = (" + ".join(s["formula"] for s in answer[side]) for side in ("reactants", "products"))
    return " -> ".join(sides), species


async def mark_chemical_equation(part: Mapping[str, Any], response: Any, run: RunTool, settings: Any) -> Verdict:
    """`chem_balance` (VER-19) decides balance and lowest terms; the species and, when the item requires them, the
    state symbols must be the key's (CUR-15). A response is the coefficients in the key's species order (a list, or
    a mapping with `coefficients` and optional `states`), or the whole equation as text."""
    answer = part["answer"]
    marks = float(part["marks"])
    version = MARKER_VERSION["chemical_equation"]
    skeleton, key_species = _skeleton(answer)
    key_coefficients = [str(s["coefficient"]) for side in ("reactants", "products") for s in answer[side]]
    check_ids: list[str] = []

    def verdict(outcome: str, awarded: float, category: str | None, criteria: list[dict[str, Any]]) -> Verdict:
        return Verdict(outcome, max(0.0, awarded), marks, version, tuple(check_ids), category, criteria=tuple(criteria))

    states: list[str | None] | None = None
    if isinstance(response, str):
        value, check_id = await run("chem_balance", {"equation": response})
        if value is None or check_id is None:
            # The checker refused the student's equation; if it balances the key's species, the worker is running and
            # the species written are not the key's (or not an equation at all).
            probe, probe_id = await run("chem_balance", {"equation": skeleton})
            if probe is None or probe_id is None:
                return _unverifiable(part, version)
            check_ids.append(probe_id)
            return verdict("incorrect", 0.0, "value", [_tool_criterion("species", False)])
        check_ids.append(check_id)
        written = [(str(s["side"]), *_species(str(s["formula"]))) for s in value.get("species", [])]
        place = {(side, formula): n for n, (side, formula, _) in enumerate(key_species)}
        order = [place.get((side, formula)) for side, formula, _ in written]
        if sorted(n for n in order if n is not None) != list(range(len(key_species))) or None in order:
            return verdict("incorrect", 0.0, "value", [_tool_criterion("species", False)])
        claimed = (value.get("student") or {}).get("coefficients") or [1] * len(written)
        coefficients: list[str] = [""] * len(key_species)
        states = [None] * len(key_species)
        for n, (_, _, state), c in zip(order, written, claimed, strict=True):
            assert n is not None
            coefficients[n], states[n] = str(c), state
    else:
        raw = response.get("coefficients") if isinstance(response, Mapping) else response
        coefficients = [str(c).strip() for c in raw] if isinstance(raw, list | tuple) else []
        if isinstance(response, Mapping) and isinstance(response.get("states"), list | tuple):
            states = [str(s).strip("() ") or None if s is not None else None for s in response["states"]]
        unreadable = (
            len(coefficients) != len(key_species)
            or (states is not None and len(states) != len(coefficients))
            or not all(_COEFFICIENT.fullmatch(c) and int(c.split("/")[0]) > 0 for c in coefficients)
        )
        if unreadable:
            return verdict("incorrect", 0.0, "unreadable", [])
    value, check_id = await run("chem_balance", {"equation": skeleton, "coefficients": coefficients})
    student = (value or {}).get("student")
    if check_id is None or not isinstance(student, Mapping):
        return _unverifiable(part, version, tuple(check_ids))
    check_ids.append(check_id)
    balanced = bool(student.get("balanced"))
    criteria = [_tool_criterion("species", True), _tool_criterion("balanced", balanced)]
    if not balanced:
        return verdict("incorrect", 0.0, "unbalanced", criteria)
    # Several independent reactions have no lowest terms: then the key's own coefficients are the simplest form.
    lowest = student.get("verdict") == "correct" or (student.get("verdict") == "balanced"
                                                     and coefficients == key_coefficients)  # fmt: skip
    criteria.append(_tool_criterion("lowest_terms", lowest))
    awarded, category = marks, None
    if not lowest:
        if answer["non_lowest_multiple"] == "wrong":
            return verdict("incorrect", 0.0, "form", criteria)
        awarded, category = marks - settings.marking_chem_not_lowest_marks_lost, "form"
    if answer["state_symbols"] == "required":
        right = states is not None and all(s == k for s, (_, _, k) in zip(states, key_species, strict=True))
        criteria.append(_tool_criterion("state_symbols", right, method="key"))
        if not right:
            awarded, category = awarded - settings.marking_chem_state_symbols_marks_lost, category or "other"
    if awarded >= marks:
        return verdict("correct", marks, None, criteria)
    return verdict("partial" if awarded > 0 else "incorrect", awarded, category, criteria)


_MONEY = re.compile(r"^\s*(?P<open>[-(])?\s*(?:[$£€₦¥]|[A-Z]{3})?\s*(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?P<part>\.\d+)?"
                    r"\s*\)?\s*$")  # fmt: skip


def _money(text: Any) -> Decimal | None:
    """An amount as written: a currency symbol or code, thousands commas and brackets for a negative allowed."""
    found = _MONEY.match(str(text))
    if found is None:
        return None
    value = Decimal(found["whole"].replace(",", "") + (found["part"] or ""))
    return -value if found["open"] else value


def _student_lines(response: Any) -> list[dict[str, str]] | None:
    """Journal lines as `{account, side, amount}`; a line written with debit and credit columns takes the side of
    the column that holds the amount. None when a line cannot be read."""
    if not isinstance(response, list | tuple) or not response:
        return None
    lines = []
    for line in response:
        account = str(line.get("account") or line.get("label") or "").strip() if isinstance(line, Mapping) else ""
        if not account:
            return None
        if line.get("side") in ("debit", "credit"):
            if _money(line.get("amount", "")) is None:
                return None
            lines.append({"account": account, "side": str(line["side"]), "amount": str(line["amount"])})
            continue
        debit, credit = _money(line.get("debit") or 0), _money(line.get("credit") or 0)
        if debit is None or credit is None or (debit == 0) == (credit == 0):
            return None
        lines.append({"account": account, "side": "debit" if debit else "credit", "amount": str(debit or credit)})
    return lines


def _line_category(verdicts: list[str], balanced: bool, extra: int) -> str | None:
    """Line verdicts of `acc_double_entry` (VER-22) as one error category, the most telling first (ASM-07): a wrong
    side, a wrong account, a missing line, an entry that does not balance, a wrong amount, an extra line."""
    for found, category in (("wrong_side", "wrong_side"), ("wrong_account", "wrong_account"),
                            ("missing_entry", "missing_line")):  # fmt: skip
        if found in verdicts:
            return category
    if not balanced:
        return "unbalanced"
    if "wrong_amount" in verdicts:
        return "value"
    return "other" if extra else None


def _lines_verdict(part: Mapping[str, Any], version: str, verdicts: list[str], weights: list[float], balanced: bool,
                   extra: int, check_ids: tuple[str, ...], methods: list[str] | None = None) -> Verdict:  # fmt: skip
    """Whole marks in proportion to the key lines right (less one line's worth per extra line); the first key line
    that is not right is the first divergent line. Each key line is a criterion, decided by the tool or the key."""
    marks = float(part["marks"])
    methods = methods or ["tool"] * len(verdicts)
    criteria = tuple(_tool_criterion(f"line_{n + 1}", v == "ok", m)
                     for n, (v, m) in enumerate(zip(verdicts, methods, strict=True)))  # fmt: skip
    first = next((n + 1 for n, v in enumerate(verdicts) if v != "ok"), None)
    if first is None and balanced and not extra:
        return Verdict("correct", marks, marks, version, check_ids, criteria=criteria)
    earned = sum(w for w, v in zip(weights, verdicts, strict=True) if v == "ok") - extra * min(weights, default=1.0)
    total = sum(weights)
    awarded = float(math.floor(marks * max(0.0, earned) / total)) if total > 0 else 0.0
    return Verdict("partial" if awarded > 0 else "incorrect", awarded, marks, version, check_ids,
                   _line_category(verdicts, balanced, extra), criteria=criteria,
                   first_error_step=first or len(verdicts) + 1)  # fmt: skip


async def _double_entry(key: list[dict[str, Any]], response: Any, run: RunTool
                        ) -> tuple[list[str], bool, int, str] | None:  # fmt: skip
    """The student's lines marked against one key entry by `acc_double_entry` (VER-22): the verdict of each key line
    in key order, whether the student's entries balance, the number of extra lines, and the check id."""
    args: dict[str, Any] = {"task": "mark", "key": [{"lines": key}]}
    if isinstance(response, str):
        args["entries_text"] = response
    else:
        args["entries"] = [{"lines": response}]
    value, check_id = await run("acc_double_entry", args)
    marking = (value or {}).get("marking")
    if check_id is None or not isinstance(marking, Mapping) or not marking.get("entries"):
        return None
    entries = list(marking["entries"])
    per_key: list[str] = [""] * len(key)
    for line in entries[0]["lines"]:
        expected = line.get("expected")
        if not isinstance(expected, Mapping):
            continue
        for k, row in enumerate(key):
            side = "debit" if Decimal(row["debit"]) else "credit"
            amount = Decimal(row["debit"]) or Decimal(row["credit"])
            same = (expected["account"], expected["side"], Decimal(str(expected["amount"]))) == (
                row["account_name"], side, amount)  # fmt: skip
            if not per_key[k] and same:
                per_key[k] = str(line["verdict"])
                break
    extra = sum(line["verdict"] == "extra_entry" for line in entries[0]["lines"])
    extra += sum(len(entry["lines"]) for entry in entries[1:])
    balanced = all(bool(entry.get("balanced", True)) for entry in entries)
    return [v or "missing_entry" for v in per_key], balanced, extra, check_id


def _key_line(name: str, names: list[str], side: str, amount: str, account_id: str | None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "account_name": name,
        "accepted_names": names[:12],
        "debit": amount if side == "debit" else "0",
        "credit": amount if side == "credit" else "0",
    }
    return line | ({"account_id": account_id} if account_id else {})


def journal_key(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A journal answer's key lines as `acc_double_entry` reads them."""
    return [_key_line(str(line["account_name"]), [str(n) for n in line["accepted_names"]],
                      "debit" if Decimal(line["debit"]) else "credit",
                      line["debit"] if Decimal(line["debit"]) else line["credit"], line.get("account_id"))
            for line in answer["lines"]]  # fmt: skip


def journal_lines(response: Any) -> str | list[dict[str, str]] | None:
    """A journal response as text or as lines; the narration of a `{lines, narration}` response is not marked."""
    if isinstance(response, Mapping) and "lines" in response:
        response = response["lines"]
    return response if isinstance(response, str) else _student_lines(response)


async def mark_journal_entry(part: Mapping[str, Any], response: Any, run: RunTool) -> Verdict:
    """`acc_double_entry` (VER-22) marks each line on account (by the key's names and aliases), side and amount, and
    the entry must balance; line order and the narration are not marked (CUR-15, ASM-07). A response is a list of
    lines (`account` with `side` and `amount`, or with `debit` and `credit`), a mapping of such `lines` with a
    `narration`, or text such as "Dr Equipment 5000; Cr Cash 5000"."""
    version = MARKER_VERSION["journal_entry"]
    lines = journal_lines(response)
    if not lines:
        return Verdict("incorrect", 0.0, float(part["marks"]), version, (), "unreadable")
    marked = await _double_entry(journal_key(part["answer"]), lines, run)
    if marked is None:
        return _unverifiable(part, version)
    verdicts, balanced, extra, check_id = marked
    return _lines_verdict(part, version, verdicts, [1.0] * len(verdicts), balanced, extra, (check_id,))


def _label(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9&]+", " ", str(text).lower()).split())


async def mark_accounting_table(part: Mapping[str, Any], response: Any, run: RunTool) -> Verdict:
    """Every key line is a mark point (its own `marks`, else one each). Debit and credit lines (T-accounts, trial
    balances) are marked by `acc_double_entry` on account, side and amount, and must balance; added, subtracted and
    total lines are matched by account id or label, amounts exact. The first key line, in the key's order, that is
    not right is the first divergent line (ASM-07). A response is a list of lines with `label` (or `account`, or
    `account_id`), `amount` and, for debit and credit lines, `side`."""
    version = MARKER_VERSION["accounting_table"]
    rows = list(part["answer"]["rows"])
    if not isinstance(response, list | tuple) or not all(isinstance(r, Mapping) for r in response):
        return Verdict("incorrect", 0.0, float(part["marks"]), version, (), "unreadable")
    sided = [n for n, r in enumerate(rows) if r["side"] in ("debit", "credit")]
    written = [r for r in response if r.get("side") in ("debit", "credit")]
    others = [r for r in response if r.get("side") not in ("debit", "credit")]
    verdicts = ["missing_entry"] * len(rows)
    balanced, extra = True, 0
    check_ids: tuple[str, ...] = ()
    if sided and written:
        lines = _student_lines([{**r, "account": r.get("label") or r.get("account") or r.get("account_id")}
                                for r in written])  # fmt: skip
        if lines is None:
            return Verdict("incorrect", 0.0, float(part["marks"]), version, (), "unreadable")
        key = [_key_line(str(rows[n]["label"]), [str(rows[n]["label"])], rows[n]["side"], str(rows[n]["amount"]),
                         rows[n].get("account_id")) for n in sided]  # fmt: skip
        marked = await _double_entry(key, lines, run)
        if marked is None:
            return _unverifiable(part, version)
        line_verdicts, balanced, extra, check_id = marked
        check_ids = (check_id,)
        for n, verdict in zip(sided, line_verdicts, strict=True):
            verdicts[n] = verdict
    else:
        extra += len(written)
    used: set[int] = set()
    for n, row in enumerate(rows):
        if n in sided:
            continue
        wanted = {_label(row["label"])} | ({str(row["account_id"])} if row.get("account_id") else set())
        for s, line in enumerate(others):
            names = {_label(line.get("label") or line.get("account") or ""), str(line.get("account_id") or "")}
            if s not in used and wanted & names:
                used.add(s)
                given = _money(line.get("amount", ""))
                verdicts[n] = "ok" if given is not None and given == _money(row["amount"]) else "wrong_amount"
                break
    extra += len(others) - len(used)
    weights = [float(r.get("marks", 1)) for r in rows]
    methods = ["tool" if n in sided else "key" for n in range(len(rows))]
    return _lines_verdict(part, version, verdicts, weights, balanced, extra, check_ids, methods)


# --- free-response maths: algebraic answers and equation solutions (ASM-03, ASM-05, CUR-15) -------------------------

_SIGN = r"(?P<op>>=|\\geq?|≥|>|\\gt|!=|\\neq?|≠)"
_ASSUMPTION = re.compile(r"^\s*(?P<name>[A-Za-z](?:_[A-Za-z0-9]+)?)\s*" + _SIGN + r"\s*0\s*$")
_INTEGER = re.compile(r"^\s*(?P<name>[A-Za-z](?:_[A-Za-z0-9]+)?)\s*"
                      r"(?:\\in\s*\\mathbb\s*\{?Z\}?|\u2208\s*\u2124|is an integer|integer)\s*$")  # fmt: skip
_FLAGS = {">": "positive", "\\gt": "positive", ">=": "nonnegative", "\\ge": "nonnegative", "\\geq": "nonnegative",
          "≥": "nonnegative", "!=": "nonzero", "\\ne": "nonzero", "\\neq": "nonzero", "≠": "nonzero"}  # fmt: skip


def worker_assumptions(texts: list[str]) -> dict[str, list[str]] | None:
    """An item's assumptions (`x > 0`, `a \\neq 0`, `x \\geq 0`, `n integer`) as the checker's per-variable flags
    (VER-05). None when one cannot be stated to the checker, which then cannot mark under it."""
    flags: dict[str, list[str]] = {}
    for text in texts:
        found = _ASSUMPTION.match(text)
        if found is not None:
            name, flag = found["name"], _FLAGS[found["op"]]
        elif (integer := _INTEGER.match(text)) is not None:
            name, flag = integer["name"], "integer"
        else:
            return None
        if flag not in flags.setdefault(name, []):
            flags[name].append(flag)
    return flags


def _scope(variables: list[str], assumptions: dict[str, list[str]]) -> dict[str, Any]:
    return {"variables": list(variables)} | ({"assumptions": assumptions} if assumptions else {})


_EQUALS = re.compile(r"(?<![<>!\\])=")


def equation_sides(text: str) -> tuple[str, str] | None:
    """The two sides of `left = right`; None when the text has no single equals sign."""
    sides = _EQUALS.split(text)
    return (sides[0].strip(), sides[1].strip()) if len(sides) == 2 else None


def _mentions(text: str, letter: str) -> bool:
    """Whether a letter is written in the text outside LaTeX command names (`t` is not in `\\sqrt`)."""
    plain = re.sub(r"\\[A-Za-z]+", " ", text)
    return re.search(rf"(?<![A-Za-z_]){re.escape(letter)}" if len(letter) > 1 else re.escape(letter), plain) is not None


async def _refused(part: Mapping[str, Any], version: str, run: RunTool, probe: dict[str, Any],
                   check_ids: list[str]) -> Verdict:  # fmt: skip
    """The checker refused the student's text. When it accepts the key in the same call, it is running and the
    student's text cannot be read; otherwise nothing can be marked."""
    value, check_id = await run(str(probe.pop("tool")), probe)
    if value is None or check_id is None:
        return _unverifiable(part, version, tuple(check_ids))
    return Verdict("incorrect", 0.0, float(part["marks"]), version, (*check_ids, check_id), "unreadable")


async def mark_algebraic(part: Mapping[str, Any], response: Any, run: RunTool, settings: Any) -> Verdict:
    """Value first, by `math_check_equivalence` (VER-04) under the item's assumptions (VER-05): an expression equal
    to the key except where it is undefined is the same value. Then the required form, separately (CUR-15, VER-10):
    `expanded`, `factorised` and `simplest` by `math_check_answer`; `solved_for` (the key is `subject = expression`)
    needs the subject alone on one side, and an equation equivalent to the key with the subject not isolated is the
    right value in the wrong form, found by `math_solve`. An equivalent answer in the wrong form loses the configured
    marks, or every mark when the item's `form_unmet` is `wrong` (ASM-05); a form the item does not ask for is never
    marked. A response is the answer as text; `left = right` is read as its right side unless the subject is asked
    for."""
    answer = part["answer"]
    marks = float(part["marks"])
    version = MARKER_VERSION["algebraic"]
    target = str(answer["required_form"])
    assumptions = worker_assumptions(list(answer.get("assumptions", [])))
    text = str(response).strip() if isinstance(response, str | int | float) else ""
    if assumptions is None:
        return _unverifiable(part, version)
    if not text:
        return Verdict("incorrect", 0.0, marks, version, (), "unreadable")
    scope = _scope(list(answer["variables"]), assumptions)
    check_ids: list[str] = []

    def verdict(outcome: str, awarded: float, category: str | None, criteria: list[dict[str, Any]]) -> Verdict:
        return Verdict(outcome, max(0.0, awarded), marks, version, tuple(check_ids), category, criteria=tuple(criteria))

    key = str(answer["expression"])
    subject = ""
    if target == "solved_for":
        key_sides = equation_sides(key)
        if key_sides is None:
            return _unverifiable(part, version)
        subject, key = key_sides
    sides = equation_sides(text)
    candidate: str | None = text if sides is None else sides[1]
    if subject and sides is not None:
        candidate = sides[1] if sides[0] == subject else sides[0] if sides[1] == subject else None
    if subject and candidate is not None and _mentions(candidate, subject):
        candidate = None
    isolated = candidate is not None
    if candidate is None:  # the subject is not alone on one side: solve the student's equation for it
        equation = text if sides is not None else f"{subject} = {text}"
        solved, check_id = await run("math_solve", {"equations": [equation], "unknowns": [subject], **scope})
        if solved is None or check_id is None:
            return await _refused(part, version, run, {"tool": "math_solve", "equations": [answer["expression"]],
                                                       "unknowns": [subject], **scope}, check_ids)  # fmt: skip
        check_ids.append(check_id)
        found = [s[subject]["text"] for s in solved.get("solutions") or [] if subject in s]
        if solved.get("case") != "solutions" or len(found) != 1:
            return verdict("incorrect", 0.0, "value", [_tool_criterion("value", False)])
        candidate = str(found[0])
    same, check_id = await run("math_check_equivalence", {"a": candidate, "b": key, **scope})
    if same is None or check_id is None:
        return await _refused(part, version, run, {"tool": "math_check_equivalence", "a": key, "b": key, **scope},
                              check_ids)  # fmt: skip
    check_ids.append(check_id)
    equivalence = same.get("verdict")
    if equivalence == "not_equivalent":
        return verdict("incorrect", 0.0, "value", [_tool_criterion("value", False)])
    if equivalence not in ("equivalent", "equivalent_on_domain"):
        return _unverifiable(part, version, tuple(check_ids))
    criteria = [_tool_criterion("value", True)]
    if target == "any":
        return verdict("correct", marks, None, criteria)
    if target == "solved_for":
        met = isolated
    else:
        checked, check_id = await run(
            "math_check_answer",
            {"student": candidate, "key": key, "rule": {"form": target, "quantity": "exact"}, **scope},
        )
        form = ((checked or {}).get("components") or {}).get("form")
        if check_id is None or form is None:
            return _unverifiable(part, version, tuple(check_ids))
        check_ids.append(check_id)
        met = form == "ok"
    criteria.append(_tool_criterion("form", met))
    if met:
        return verdict("correct", marks, None, criteria)
    if answer.get("form_unmet", "partial") == "wrong":
        return verdict("incorrect", 0.0, "form", criteria)
    awarded = marks - settings.marking_algebraic_form_marks_lost
    return verdict("partial" if awarded > 0 else "incorrect", awarded, "form", criteria)


_PLUS_MINUS = re.compile(r"\\pm|±|\+/-|\+-")
_SEPARATOR = re.compile(r"\s*(?:,|;|\n|\\quad|\\lor|\\text\s*\{\s*(?:or|and)\s*\}|\bor\b|\band\b)\s*")
_LABEL = re.compile(r"^(?P<name>[A-Za-z])(?:_\{?[0-9]+\}?)?\s*=\s*(?P<value>.*)$", re.S)
_NO_SOLUTION = re.compile(r"^\s*(?:no\s+(?:real\s+)?(?:solutions?|roots?)|\\emptyset|∅|\\varnothing|\{\s*\})\s*$",
                          re.I)  # fmt: skip
_SET = re.compile(r"^\s*(?:\\\{|\\left\\\{|\{)(?P<inner>.*?)(?:\\\}|\\right\\\}|\})\s*$", re.S)


def solution_values(response: Any, variable: str) -> list[str] | None:
    """The values a student gives for one unknown: a list, or text such as `x = 2 or x = -3`, `x = \\pm 3`, `{2, -3}`
    or `2 \\pm \\sqrt{3}` (each ± is two values). Values labelled with another letter answer another part and are not
    counted; `no solution` is the empty set. None when nothing can be read."""
    chunks: list[str] = []
    empty_set = False
    for raw in response if isinstance(response, list | tuple) else [response]:
        if not isinstance(raw, str | int | float) or isinstance(raw, bool):
            return None
        text = str(raw).strip()
        if _NO_SOLUTION.match(text):
            empty_set = True
            continue
        inside = _SET.match(text)
        if inside is not None:
            text = inside["inner"]
        chunks += [c for c in _SEPARATOR.split(text) if c.strip()]
    values: list[str] = []
    for chunk in chunks:
        labelled = _LABEL.match(chunk.strip())
        if labelled is not None:
            if labelled["name"] != variable:
                continue
            chunk = labelled["value"]
        chunk = chunk.strip()
        if not chunk:
            return None
        sign = _PLUS_MINUS.search(chunk)
        if sign is None:
            values.append(chunk)
            continue
        before, after = chunk[: sign.start()].strip(), chunk[sign.end() :].strip()
        values += [f"{before} + {after}", f"{before} - {after}"] if before else [after, f"-{after}"]
    return values if values or empty_set else None


def _half_unit(written: str) -> Decimal | None:
    """Half a unit in the last place of a decimal as written (0.005 for 0.56)."""
    found = re.fullmatch(r"\s*[-+]?\d*(?:\.(\d+))?\s*", written)
    if found is None:
        return None
    return Decimal(5).scaleb(-len(found.group(1) or "") - 1)


async def _same_value(student: str, key: str, tolerance: Mapping[str, Any], variable: str,
                      run: RunTool) -> tuple[bool | None, str | None, bool]:  # fmt: skip
    """Whether one student value is one key value, by `math_check_answer`, and the check id; `unreadable` is True when
    the checker read nothing in the student's value. A key written as a rounded decimal is met within the item's
    tolerance; an exact key is met exactly, or by a decimal the checker finds is the key rounded at the student's own
    precision when that precision is inside the item's tolerance (VER-09)."""
    mode, size = str(tolerance["mode"]), Decimal(str(tolerance["value"]))
    rounded = re.fullmatch(r"\s*-?\d+\.\d+\s*", key) is not None
    rule: dict[str, Any] = {"quantity": "exact"}
    if rounded:
        key_value = Decimal(key)
        if mode == "sig_figs":  # half a unit in the last stated figure of the key
            mode, size = "absolute", Decimal(5).scaleb(key_value.adjusted() - int(size))
        rule = {"tolerance": {"mode": mode, "value": str(size)}}
    checked, check_id = await run("math_check_answer", {"student": student, "key": key, "rule": rule,
                                                        "variables": [variable]})  # fmt: skip
    if checked is None or check_id is None:
        return None, None, True
    status = (checked.get("components") or {}).get("value")
    if (checked.get("details") or {}).get("reason") == "unreadable":
        return None, check_id, True
    if status == "correct" or (status == "within_tolerance" and rounded):
        return True, check_id, False
    if status == "incorrect":
        return False, check_id, False
    if status != "within_tolerance":
        return None, check_id, False
    parsed, half = parse_numeric(student), _half_unit(student)
    if parsed is None or half is None:
        return None, check_id, False
    if mode == "sig_figs":
        return parsed.sig_figs >= int(size), check_id, False
    return half <= (size if mode == "absolute" else size * abs(parsed.value)), check_id, False


async def mark_equation_solutions(part: Mapping[str, Any], response: Any, run: RunTool, settings: Any) -> Verdict:
    """The student's values and the key's compared as sets (ASM-03, VER-07): order and repeats do not matter, `±`
    gives two values, and every key value needs a matching student value and every student value a matching key
    value. Each key value is a criterion and each extra value is counted as its own failed criterion; a missing or an
    extra value is incorrect. Each comparison is a `math_check_answer` call."""
    answer = part["answer"]
    marks = float(part["marks"])
    version = MARKER_VERSION["equation_solutions"]
    variable = str(answer["variable"])
    keys = [str(v) for v in answer["values"]]
    values = solution_values(response, variable)
    if values is None or len(values) > settings.marking_solutions_max_values:
        return Verdict("incorrect", 0.0, marks, version, (), "unreadable")
    check_ids: list[str] = []
    matched: set[int] = set()
    extra = 0
    for value in dict.fromkeys(values):
        hits = []
        for n, key in enumerate(keys):
            same, check_id, unreadable = await _same_value(value, key, answer["tolerance"], variable, run)
            if check_id is not None:
                check_ids.append(check_id)
            if unreadable:
                question = {"tool": "math_check_answer", "student": key, "key": key, "variables": [variable]}
                return await _refused(part, version, run, question, check_ids)
            if same is None:
                return _unverifiable(part, version, tuple(check_ids))
            if same:
                hits.append(n)
        matched.update(hits)
        extra += not hits
    if not check_ids:  # no value given: one call still shows the checker is there to mark the empty set
        probe, check_id = await run("math_check_answer", {"student": keys[0], "key": keys[0],
                                                          "variables": [variable]})  # fmt: skip
        if probe is None or check_id is None:
            return _unverifiable(part, version)
        check_ids.append(check_id)
    criteria = [_tool_criterion(f"value_{n + 1}", n in matched) for n in range(len(keys))]
    criteria += [_tool_criterion(f"extra_{n + 1}", False) for n in range(extra)]
    if len(matched) == len(keys) and not extra:
        return Verdict("correct", marks, marks, version, tuple(check_ids), criteria=tuple(criteria))
    return Verdict("incorrect", 0.0, marks, version, tuple(check_ids), "value", criteria=tuple(criteria))


# --- diagram labels (ASM-08, VSL-28) --------------------------------------------------------------------------------


def _name(text: Any) -> str:
    return " ".join(str(text).replace("_", " ").lower().split())


def _edits(a: str, b: str, limit: int) -> int:
    """Edits between two names (insert, delete, change one letter, or swap two neighbours), stopping past `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    before: list[int] | None = None
    row = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(row[j] + 1, current[j - 1] + 1, row[j - 1] + cost)
            if before is not None and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], before[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        before, row = row, current
    return row[-1]


def label_matches(written: Any, accepted: list[str], settings: Any) -> bool:
    """A label against a part's name and aliases (VSL-28): case and spacing ignored, and the configured edits
    tolerated for a name with at least the configured letters."""
    label = _name(written)
    if not label:
        return False
    tolerance = settings.marking_label_edit_tolerance
    for name in (_name(a) for a in accepted):
        if label == name:
            return True
        long_enough = sum(c.isalpha() for c in name) >= settings.marking_label_min_letters
        if long_enough and tolerance and _edits(label, name, tolerance) <= tolerance:
            return True
    return False


def _placed(response: Any) -> dict[str, Any] | None:
    """Labels by target part id: a mapping, or a list of `{part_id, label}`."""
    if isinstance(response, Mapping):
        return {str(k): v for k, v in response.items()}
    if isinstance(response, list | tuple) and all(isinstance(r, Mapping) and "part_id" in r for r in response):
        return {str(r["part_id"]): r.get("label", "") for r in response}
    return None


def mark_labels(part: Mapping[str, Any], response: Any, settings: Any) -> Verdict:
    """Each target part id against its accepted labels; the part's marks shared equally between the targets."""
    targets = list(part["answer"]["targets"])
    marks = float(part["marks"])
    version = MARKER_VERSION["diagram_label"]
    placed = _placed(response)
    right = [placed is not None and label_matches(placed.get(str(t["part_id"]), ""), list(t["accepted_labels"]),
                                                  settings) for t in targets]  # fmt: skip
    criteria = tuple({"criterion_id": str(t["part_id"]), "mark_type": "B", "result": "pass" if ok else "fail",
                      "method": "key"} for t, ok in zip(targets, right, strict=True))  # fmt: skip
    if placed is None:
        return Verdict("incorrect", 0.0, marks, version, (), "unreadable", criteria=criteria)
    hits = sum(right)
    if hits == len(targets):
        return Verdict("correct", marks, marks, version, (), criteria=criteria)
    if hits:
        return Verdict("partial", marks * hits / len(targets), marks, version, (), "value", criteria=criteria)
    return Verdict("incorrect", 0.0, marks, version, (), "value", criteria=criteria)


# --- expressions over an item's quantities (CUR-18, ASM-06) ---------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:(?P<number>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(?P<name>[A-Za-z][A-Za-z0-9_]*)"
                    r"|(?P<op>[-+*/^()]))")  # fmt: skip
FUNCTIONS = frozenset({"sqrt", "sin", "cos", "tan", "ln", "log", "exp", "abs"})
CONSTANTS = frozenset({"pi"})
WORKING_FIGURES = 12  # the figures a recomputed value is read to; the item's tolerance applies after


def evaluable(expression: str, quantities: Mapping[str, Any]) -> tuple[str, dict[str, str]] | None:
    """An expression the checker can evaluate: arithmetic over the given quantities, numbers, `pi` and the common
    functions. Each quantity is renamed to a checker symbol (`q_1`, ...) so a name such as `total` or `b1` is one
    quantity, and every product is written out. Returns the rewritten expression and the quantity of each symbol, or
    None for anything else (free text, a choice, an equation, a name the item does not give, an unknown function)."""
    text = str(expression).strip()
    if not text:
        return None
    out: list[str] = []
    symbols: dict[str, str] = {}
    previous = ""  # "operand", "function" or "op"
    position = 0
    while position < len(text):
        token = _TOKEN.match(text, position)
        if token is None or token.end() == position:
            return None
        position = token.end()
        name, number, op = token["name"], token["number"], token["op"]
        if (number is not None or name is not None or op == "(") and previous == "operand":
            out.append("*")
        if name is not None:
            if text[position:].lstrip().startswith("("):
                if name not in FUNCTIONS:
                    return None
                out.append(name)
                previous = "function"
                continue
            if name in quantities:
                out.append(f"({symbols.setdefault(name, f'q_{len(symbols) + 1}')})")
            elif name in CONSTANTS:
                out.append(name)
            else:
                return None
            previous = "operand"
        elif number is not None:
            out.append(number)
            previous = "operand"
        else:
            out.append(op)
            previous = "operand" if op == ")" else "op"
    return " ".join(out), {symbol: name for name, symbol in symbols.items()}


async def evaluate_expression(expression: str, quantities: Mapping[str, Any], run: RunTool
                              ) -> tuple[Decimal | None, str | None]:  # fmt: skip
    """The value of an expression over the given quantities by `math_evaluate`, and its check id. No value and no
    check id when it is not an expression the checker can evaluate or the checker refuses it; a check id without a
    value when the checker ran and could not decide (VER-27)."""
    found = evaluable(expression, quantities)
    if found is None:
        return None, None
    text, symbols = found
    values = {symbol: str(quantities[name]) for symbol, name in symbols.items()}
    result, check_id = await run("math_evaluate", {"expression": text, "values": values, "sf": WORKING_FIGURES})
    if result is None or check_id is None:
        return None, None
    try:
        return (Decimal(str(result["rounded"])) if result.get("status") == "ok" else None), check_id
    except (InvalidOperation, KeyError):
        return None, check_id


# --- structured items: parts, mark points and follow-through (ASM-06, ASM-17, CUR-15) -------------------------------


def part_responses(item: Mapping[str, Any], part_id: str, response: Any) -> dict[str, Any]:
    """A structured item's responses by part id; a response that is not such a mapping is the marked part's own."""
    ids = {str(p["id"]) for p in item["parts"]}
    if isinstance(response, Mapping) and response and {str(k) for k in response} <= ids:
        return {str(k): v for k, v in response.items()}
    return {part_id: response}


async def _follow_through(item: Mapping[str, Any], part: Mapping[str, Any], responses: Mapping[str, Any],
                          convert: Convert, settings: Any, run: RunTool) -> Verdict | None:  # fmt: skip
    """The part marked against its answer recomputed from the student's earlier values (error carried forward);
    `unverifiable` when the checker could not decide the recomputation, and None when the recipe cannot be applied to
    what the student wrote."""
    recipe = part.get("follow_through")
    answer = part["answer"]
    uses = {str(p) for p in part.get("uses_parts") or []}
    if not part.get("ecf") or not recipe or answer["kind"] != "numeric":
        return None
    parts = {str(p["id"]): p for p in item["parts"]}
    values: dict[str, Any] = dict(item.get("quantities") or {})
    check_ids: list[str] = []
    for symbol, earlier_id in recipe["symbols"].items():
        earlier = parts.get(str(earlier_id))
        if earlier is None or str(earlier_id) not in uses or earlier["answer"]["kind"] != "numeric":
            return None
        written = parse_numeric(str(responses.get(str(earlier_id), "")))
        if written is None:
            return None
        value, unit = written.value, earlier["answer"]["unit"]
        if written.unit is not None and unit is not None and earlier["answer"]["unit_policy"] != "none":
            converted = await convert(f"{written.value} {written.unit}", unit)
            if converted is None:
                return None
            value = converted.value
            check_ids.append(converted.check_id)
        values[str(symbol)] = str(value)
    key, check_id = await evaluate_expression(str(recipe["expression"]), values, run)
    if check_id is None:
        return None
    check_ids.append(check_id)
    if key is None:
        return _unverifiable(part, MARKER_VERSION["structured"], tuple(check_ids))
    following = {**part, "answer": {**answer, "value": str(key)}}
    verdict = await mark_numeric(following, str(responses.get(str(part["id"]), "")), convert, settings,
                                 stated_precision(item, part))  # fmt: skip
    return replace(verdict, check_ids=(*verdict.check_ids, *check_ids))


def _scored_by_points(part: Mapping[str, Any], verdict: Verdict, follow_through: bool) -> Verdict:
    """The part's verdict scored by its M, A and B mark points (ASM-17)."""
    points = list(part["mark_points"])
    marks = float(part["marks"])
    if verdict.outcome == "correct":
        passed = {str(p["id"]): True for p in points}
    elif verdict.outcome == "partial":  # right in value, not in full: the accuracy marks are lost
        passed = {str(p["id"]): p["type"] != "A" for p in points}
    else:
        passed = {str(p["id"]): False for p in points}
    method_mark = ""
    for point in points:  # an A mark is never awarded without the M mark it depends on
        if point["type"] == "M":
            method_mark = str(point["id"])
        elif point["type"] == "A":
            needs = str(point.get("depends_on") or method_mark)
            if needs and not passed.get(needs, False):
                passed[str(point["id"])] = False
    tool = not verdict.criteria or any(c["method"] == "tool" for c in verdict.criteria)
    criteria = tuple({"criterion_id": str(p["id"]), "mark_type": str(p["type"]),
                      "result": "pass" if passed[str(p["id"])] else "fail", "method": "tool" if tool else "key",
                      **({"follow_through": True} if follow_through else {})} for p in points)  # fmt: skip
    awarded = min(marks, float(sum(int(p["marks"]) for p in points if passed[str(p["id"])])))
    if verdict.outcome == "partial":
        awarded = min(awarded, verdict.marks_awarded)
    outcome = ("correct" if verdict.outcome == "correct" and awarded >= marks
               else "partial" if awarded > 0 else "incorrect")  # fmt: skip
    return replace(verdict, outcome=outcome, marks_awarded=awarded, marks_available=marks,
                   marker_version=MARKER_VERSION["structured"], criteria=criteria)  # fmt: skip


async def mark_structured_part(item: Mapping[str, Any], part: Mapping[str, Any], response: Any, convert: Convert,
                               settings: Any, run: RunTool | None) -> Verdict:  # fmt: skip
    """One part of a structured item, by the marker of its answer kind, with follow-through and mark points."""
    responses = part_responses(item, str(part["id"]), response)
    own = responses.get(str(part["id"]))
    if own is None:
        return Verdict("incorrect", 0.0, float(part["marks"]), MARKER_VERSION["structured"], (), "unreadable")
    kind = ANSWER_MARKERS.get(str(part["answer"]["kind"]))
    verdict = await _mark_kind(kind, item, part, own, convert, settings, run)
    follow = False
    if verdict.outcome == "incorrect" and run is not None:
        carried = await _follow_through(item, part, responses, convert, settings, run)
        if carried is not None and carried.outcome in ("correct", "partial", "unverifiable"):
            verdict, follow = carried, True
    if verdict.outcome == "unverifiable":
        return verdict
    if part.get("mark_points"):
        return _scored_by_points(part, verdict, follow)
    if follow:
        criteria = verdict.criteria or ({"criterion_id": "follow_through", "mark_type": "A", "result": "pass",
                                         "method": "tool"},)  # fmt: skip
        verdict = replace(verdict, criteria=tuple({**c, "follow_through": True} for c in criteria))
    return replace(verdict, marker_version=MARKER_VERSION["structured"])


# --- dispatch -------------------------------------------------------------------------------------------------------

# The item types this build can mark (ASM-03); a pack item of any other type is refused at build time.
MARKERS: dict[str, str] = {
    "numeric": "numeric",
    "mcq": "choice",
    "mcq_diagnostic": "choice",
    "multi_select": "multi",
    "ordering": "order",
    "chemical_equation": "chemical_equation",
    "journal_entry": "journal_entry",
    "accounting_table": "accounting_table",
    "algebraic": "algebraic",
    "equation_solutions": "equation_solutions",
    "diagram_label": "diagram_label",
    "structured": "structured",
}
# The marker of each answer kind, for the parts of a structured item; `short_text` has none in this build.
ANSWER_MARKERS: dict[str, str] = {k: v for k, v in MARKERS.items() if k not in ("mcq_diagnostic", "structured")}
TOOL_MARKED = frozenset({"chemical_equation", "journal_entry", "accounting_table", "algebraic", "equation_solutions"})


async def _mark_kind(kind: str | None, item: Mapping[str, Any], part: Mapping[str, Any], response: Any,
                     convert: Convert, settings: Any, run: RunTool | None) -> Verdict:  # fmt: skip
    if kind == "numeric":
        return await mark_numeric(part, str(response), convert, settings, stated_precision(item, part))
    if kind == "choice":
        return mark_choice(part, str(response))
    if kind == "multi":
        return mark_multi(part, list(response))
    if kind == "order":
        return mark_order(part, list(response))
    if kind == "diagram_label":
        return mark_labels(part, response, settings)
    if kind not in TOOL_MARKED:
        return Verdict("unverifiable", 0.0, float(part["marks"]), "none.1")
    if run is None:
        return _unverifiable(part, MARKER_VERSION[str(kind)])
    if kind == "chemical_equation":
        verdict = await mark_chemical_equation(part, response, run, settings)
    elif kind == "journal_entry":
        verdict = await mark_journal_entry(part, response, run)
    elif kind == "algebraic":
        verdict = await mark_algebraic(part, response, run, settings)
    elif kind == "equation_solutions":
        verdict = await mark_equation_solutions(part, response, run, settings)
    else:
        verdict = await mark_accounting_table(part, response, run)
    if verdict.outcome == "unverifiable":
        return verdict
    # The key's build check comes first, as for numeric answers: the key the answer was marked against (ASM-02).
    return replace(verdict, check_ids=(str(part["answer"]["check_id"]), *verdict.check_ids))


async def mark(item: Mapping[str, Any], part_id: str, response: Any, convert: Convert, settings: Any,
               run: RunTool | None = None, *, signatures: Any = None) -> Verdict:  # fmt: skip
    """Mark one part of a pack item. An item type without a marker is `unverifiable` (no mastery observation), and so
    is a structured answer when no checker can be run (`run` is the worker's checker call). With `signatures` (a
    `SignatureContext`: the misconception library and what its rules read), an incorrect or partial verdict carries
    the misconception whose signature the answer matches (CUR-18), found before any model runs."""
    part = next(p for p in item["parts"] if p["id"] == part_id)
    own = response
    if item["type"] == "structured":
        verdict = await mark_structured_part(item, part, response, convert, settings, run)
        own = part_responses(item, part_id, response).get(part_id)
    else:
        verdict = await _mark_kind(MARKERS.get(item["type"]), item, part, response, convert, settings, run)
    if signatures is None or run is None or verdict.misconception_id or verdict.outcome not in ("incorrect", "partial"):
        return verdict
    from agent_core.engine.signatures import with_signature

    return await with_signature(item, part, own, verdict, signatures, convert, settings, run)


async def mark_structured(item: Mapping[str, Any], responses: Mapping[str, Any], convert: Convert, settings: Any,
                          run: RunTool | None = None, *, signatures: Any = None) -> dict[str, Verdict]:  # fmt: skip
    """Every part of a structured item that has a response, each marked on its own (ASM-06)."""
    return {str(p["id"]): await mark(item, str(p["id"]), responses, convert, settings, run, signatures=signatures)
            for p in item["parts"] if str(p["id"]) in responses}  # fmt: skip
