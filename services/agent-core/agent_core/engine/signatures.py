"""Misconception signatures matched on a wrong answer (an incorrect or partial verdict) before any model runs (CUR-18,
ASM-13).

The matcher reads the misconceptions the marked part lists (`misconception_ids`) first, then those of the item's KCs,
each misconception's signatures in order; the first signature the answer satisfies names the misconception. Every
decision is a key comparison or a checker call:

- `answer_transform`: the expression is evaluated over the item's `quantities` by `math_evaluate` and the student's
  value (converted to the key's unit) is compared with the result within the item's tolerance. Only arithmetic over
  the quantities the item gives is evaluated (`marking.evaluable`); free text, a choice, an equation or an unknown
  name never matches, and neither does a transform whose value is the key's. A numeric part only.
- `step_error`: the error category `math_check_step` gave the first wrong step of the student's working (TABLE
  VER-06a), when the caller supplies one.
- `structural`, by rule:
  - `subscript_changed_to_balance` (chemical equation written as text): every species of the key is written, at
    least one with its subscripts changed (the same elements in the same order, different counts), and the equation
    as written balances (`chem_balance`).
  - `debit_credit_swapped` (journal entry): every line `acc_double_entry` does not mark right is on the wrong side,
    and at least one is; with `account_class`, a swapped line's key account is of that class in the chart of accounts.
  - `wrong_account_class` (journal entry): a line on the right side for the right amount names an account of another
    class than the key's (the classes of the chart of accounts sent to `acc_double_entry`); with `account_class`, the
    account named is of that class. Without a chart of accounts no class is known and the rule never matches.
  - `ratio_inverted` (numeric): the student's number is the reciprocal of the key's, within the item's tolerance
    (an inverted ratio has the inverse unit, so the unit is not read).
  - `mass_weight_confused` (numeric): a weight key answered with the mass (key / g) or a mass key answered with the
    weight (key times g), g from the item's convention override, else the pack's convention; the student's number is
    read in the confused quantity's unit when it converts to it, else as written (a mass copied under the weight's
    unit).
  - `sign_of_g_flipped` is not decided here, and stays undecidable from what marking reads: the CUR-28 convention
    (`phy.g`, or an item's override) gives g's size (9.8 or 10 m s⁻²), never its sign, and the sign follows the
    positive direction the student chose, which a final answer does not show. For a body dropped from rest the
    flipped answer is the key negated, which is also the right answer with downwards taken as negative, so a rule
    over the key, the answer and g would mark correct work as a misconception. The rule never matches
    (`UNDECIDED_RULES`); an `answer_transform` over the item's quantities, or a `step_error` of category `sign`
    found by Check my working, can name the misconception.

The chart of accounts is the one the item's answer names, else the one its pack's conventions name
(`acc.chart_of_accounts`), from the installed packs' `charts_of_accounts` table (the `pack.ref` chart, CUR-07).
`evidence` gives the ASM-13 `misconception_evidence` payload of a match.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from agent_core.engine.marking import (
    MIN_PRECISION,
    Convert,
    RunTool,
    Verdict,
    _close,
    _species,
    evaluable,
    evaluate_expression,
    journal_key,
    journal_lines,
    parse_numeric,
    round_figures,
    stated_precision,
)
from agent_core.store.pack_index import PackIndex

__all__ = [
    "DECIDED_RULES",
    "UNDECIDED_RULES",
    "Match",
    "SignatureContext",
    "evaluable",
    "evidence",
    "find",
    "pack_context",
    "step_error_match",
    "verdict_source",
    "with_signature",
]

DECIDED_RULES = frozenset({"subscript_changed_to_balance", "debit_credit_swapped", "wrong_account_class",
                           "ratio_inverted", "mass_weight_confused"})  # fmt: skip
UNDECIDED_RULES = frozenset({"sign_of_g_flipped"})
GRAVITY = "phy.g"  # the convention key of the gravitational field strength (VER-12)
EVIDENCE_WEIGHT = 1.0  # ASM-13: one per probe choice or matched signature, not a tunable proposal


@dataclass(frozen=True)
class SignatureContext:
    """What signatures are matched against: the pack's misconception library, its conventions by key (`phy.g`), the
    chart of accounts (`{"id", "accounts": [...]}` as `acc_double_entry` reads it) and the error category of the first
    wrong step of the student's working, when Check my working found one."""

    misconceptions: Sequence[Mapping[str, Any]]
    conventions: Mapping[str, str] = field(default_factory=dict)
    chart: Mapping[str, Any] | None = None
    step_error: str | None = None


def pack_context(content: PackIndex, item: Mapping[str, Any]) -> SignatureContext:
    """Only the verified packs' records are used for a live answer: the misconception library, the item subject's
    conventions (`phy.g`, CUR-28) and the chart of accounts its answers name, else the one its pack's conventions name
    (`acc.chart_of_accounts`; the `charts_of_accounts` table, CUR-07). Check my working adds the step error it found
    (`dataclasses.replace`)."""
    subject = str(item.get("subject") or str(item["id"]).split(".")[1])
    conventions = content.visual_sources(subject).get("conventions", {})
    named = next((str(p["answer"]["chart_of_accounts"]) for p in item.get("parts") or []
                  if isinstance(p.get("answer"), Mapping) and p["answer"].get("chart_of_accounts")), None)  # fmt: skip
    return SignatureContext(
        misconceptions=list(content.records.get("misconceptions", {}).values()),
        conventions=conventions,
        chart=content.chart_of_accounts(subject, named),
    )


def evidence(item: Mapping[str, Any], misconception_id: str, library: Sequence[Mapping[str, Any]], source: str,
             attempt_id: str | None = None) -> dict[str, Any] | None:  # fmt: skip
    """The `misconception_evidence` payload of one match (ASM-13): weight 1 whether it came from a probe or a
    signature. Its KC is the first of the misconception's KCs the item is tagged with, else the misconception's first
    KC, else (a misconception the library does not hold, named by a distractor) the item's first KC; None when there
    is no KC to count it against.

    `source` is `probe` for a distractor chosen on a diagnostic item (CUR-16, ASM-12), `signature` for a signature
    matched by the marker (or a distractor mapped on any other item) and `check_working` for a step error matched in
    Check my working."""
    record = next((m for m in library if str(m.get("id")) == misconception_id), None)
    tagged = [str(k) for k in item.get("kc_ids") or []]
    kcs = [str(k) for k in (record or {}).get("kc_ids") or []]
    kc_id = next((k for k in kcs if k in tagged), None) or next(iter(kcs + tagged), None)
    if kc_id is None:
        return None
    payload: dict[str, Any] = {"misconception_id": misconception_id, "kc_id": kc_id, "evidence_source": source,
                               "weight": EVIDENCE_WEIGHT}  # fmt: skip
    if attempt_id is not None:
        payload["attempt_id"] = attempt_id
    return payload


def verdict_source(item: Mapping[str, Any]) -> str:
    """How a marked answer's misconception was found (ASM-13): a diagnostic item's distractor is a probe."""
    return "probe" if item.get("type") == "mcq_diagnostic" else "signature"


def step_error_match(item: Mapping[str, Any], part_id: str, context: SignatureContext) -> str | None:
    """The first misconception (in matcher order) with a `step_error` signature of the category Check my working
    gave the first wrong step (TABLE VER-06a); None without a category."""
    if context.step_error is None:
        return None
    part = next((p for p in item.get("parts") or [] if str(p.get("id")) == part_id), {"id": part_id})
    for misconception in candidates(item, part, context.misconceptions):
        for signature in misconception["signatures"]:
            if signature["kind"] == "step_error" and signature.get("error_category") == context.step_error:
                return str(misconception["id"])
    return None


@dataclass(frozen=True)
class Match:
    misconception_id: str
    kind: str
    rule: str | None
    check_ids: tuple[str, ...]


def candidates(item: Mapping[str, Any], part: Mapping[str, Any], library: Sequence[Mapping[str, Any]]
               ) -> list[Mapping[str, Any]]:  # fmt: skip
    """The part's listed misconceptions, then the other misconceptions of the item's KCs, by id."""
    by_id = {str(m["id"]): m for m in library}
    listed = [by_id[str(i)] for i in part.get("misconception_ids") or [] if str(i) in by_id]
    seen = {str(m["id"]) for m in listed}
    kcs = {str(k) for k in item.get("kc_ids") or []}
    rest = [by_id[i] for i in sorted(by_id) if i not in seen and kcs & {str(k) for k in by_id[i].get("kc_ids", [])}]
    return listed + rest


async def find(item: Mapping[str, Any], part: Mapping[str, Any], response: Any, context: SignatureContext,
               convert: Convert, settings: Any, run: RunTool) -> Match | None:  # fmt: skip
    """The first misconception whose signature the answer satisfies, or None."""
    for misconception in candidates(item, part, context.misconceptions):
        for signature in misconception["signatures"]:
            found = await _satisfied(signature, item, part, response, context, convert, settings, run)
            if found is not None:
                return Match(str(misconception["id"]), str(signature["kind"]), signature.get("rule"), found)
    return None


async def with_signature(item: Mapping[str, Any], part: Mapping[str, Any], response: Any, verdict: Verdict,
                         context: SignatureContext, convert: Convert, settings: Any,
                         run: RunTool) -> Verdict:  # fmt: skip
    """A wrong verdict with the misconception its answer matches, and the checks that showed it."""
    found = await find(item, part, response, context, convert, settings, run)
    if found is None:
        return verdict
    category = "misconception" if verdict.error_category in (None, "value") else verdict.error_category
    added = tuple(c for c in found.check_ids if c not in verdict.check_ids)
    return replace(
        verdict,
        misconception_id=found.misconception_id,
        error_category=category,
        check_ids=(*verdict.check_ids, *added),
    )


async def _satisfied(signature: Mapping[str, Any], item: Mapping[str, Any], part: Mapping[str, Any], response: Any,
                     context: SignatureContext, convert: Convert, settings: Any, run: RunTool
                     ) -> tuple[str, ...] | None:  # fmt: skip
    kind = signature["kind"]
    if kind == "step_error":
        return () if context.step_error is not None and context.step_error == signature["error_category"] else None
    if kind == "answer_transform":
        applies = signature.get("applies_to_part")
        if applies is not None and str(applies) != str(part["id"]):
            return None
        return await _transform(str(signature["expression"]), item, part, response, convert, settings, run)
    rule = signature.get("rule")
    account_class = signature.get("account_class")
    if rule == "ratio_inverted":
        return _inverted(item, part, response, settings)
    if rule == "mass_weight_confused":
        return await _mass_weight(item, part, response, context, convert, settings)
    if rule == "subscript_changed_to_balance":
        return await _subscripts(part, response, run)
    if rule in ("debit_credit_swapped", "wrong_account_class"):
        return await _journal(str(rule), account_class, part, response, context, run)
    return None  # UNDECIDED_RULES


# --- numeric answers ------------------------------------------------------------------------------------------------


def _near(value: Decimal, target: Decimal, part: Mapping[str, Any], settings: Any, figures: int = 0,
          precision: int = 0) -> bool:  # fmt: skip
    """Within the item's tolerance (a significant-figures tolerance reads as the default relative one), or equal to
    the target at the figures the student wrote when they are at least the item's stated precision (VER-09)."""
    tolerance = part["answer"].get("tolerance")
    if _close(value, target, tolerance, settings.numeric_default_relative_tolerance):
        return True
    return (figures >= max(MIN_PRECISION, precision)
            and round_figures(target, figures) == round_figures(value, figures))  # fmt: skip


async def _in_key_unit(part: Mapping[str, Any], response: Any, convert: Convert
                       ) -> tuple[Decimal, int, tuple[str, ...]] | None:  # fmt: skip
    parsed = parse_numeric(str(response))
    if parsed is None:
        return None
    unit = part["answer"].get("unit")
    if parsed.unit is None or unit is None or part["answer"].get("unit_policy") == "none":
        return parsed.value, parsed.sig_figs, ()
    converted = await convert(f"{parsed.value} {parsed.unit}", unit)
    if converted is None:
        return None
    return converted.value, parsed.sig_figs, (converted.check_id,)


async def _transform(expression: str, item: Mapping[str, Any], part: Mapping[str, Any], response: Any,
                     convert: Convert, settings: Any, run: RunTool) -> tuple[str, ...] | None:  # fmt: skip
    if part["answer"]["kind"] != "numeric":
        return None
    student = await _in_key_unit(part, response, convert)
    if student is None:
        return None
    target, check_id = await evaluate_expression(expression, item.get("quantities") or {}, run)
    if target is None or check_id is None:
        return None
    if _near(target, Decimal(part["answer"]["value"]), part, settings):
        return None  # the transform gives the key: no misconception shows
    value, figures, check_ids = student
    near = _near(value, target, part, settings, figures, stated_precision(item, part))
    return (*check_ids, check_id) if near else None


def _inverted(item: Mapping[str, Any], part: Mapping[str, Any], response: Any, settings: Any
              ) -> tuple[str, ...] | None:  # fmt: skip
    if part["answer"]["kind"] != "numeric":
        return None
    key = Decimal(part["answer"]["value"])
    parsed = parse_numeric(str(response))
    if parsed is None or key == 0 or abs(key) == 1:
        return None
    return () if _near(parsed.value, 1 / key, part, settings, parsed.sig_figs, stated_precision(item, part)) else None


def _gravity(item: Mapping[str, Any], context: SignatureContext) -> Decimal | None:
    override = {str(c["key"]): str(c["value"]) for c in item.get("conventions_override") or []}
    written = override.get(GRAVITY, context.conventions.get(GRAVITY))
    try:
        return Decimal(str(written)) if written is not None else None
    except InvalidOperation:
        return None


async def _mass_weight(item: Mapping[str, Any], part: Mapping[str, Any], response: Any, context: SignatureContext,
                       convert: Convert, settings: Any) -> tuple[str, ...] | None:  # fmt: skip
    answer = part["answer"]
    g = _gravity(item, context)
    parsed = parse_numeric(str(response))
    if answer["kind"] != "numeric" or answer.get("unit") is None or not g or parsed is None:
        return None
    key = f"{answer['value']} {answer['unit']}"
    as_force = await convert(key, "N")
    if as_force is not None:
        target, unit, check_ids = as_force.value / g, "kg", [as_force.check_id]
    else:
        as_mass = await convert(key, "kg")
        if as_mass is None:
            return None
        target, unit, check_ids = as_mass.value * g, "N", [as_mass.check_id]
    value = parsed.value  # the number as written: a mass copied as the weight keeps the weight's unit
    if parsed.unit is not None:
        converted = await convert(f"{parsed.value} {parsed.unit}", unit)
        if converted is not None:
            value = converted.value
            check_ids.append(converted.check_id)
    near = _near(value, target, part, settings, parsed.sig_figs, stated_precision(item, part))
    return tuple(check_ids) if near else None


# --- chemical equations ---------------------------------------------------------------------------------------------


def _elements(formula: str) -> str:
    return "".join(c for c in formula if not c.isdigit())


def _proportional(a: Sequence[Any], b: Sequence[Any]) -> bool:
    try:
        x, y = [Fraction(str(v)) for v in a], [Fraction(str(v)) for v in b]
    except (ValueError, ZeroDivisionError):
        return False
    if len(x) != len(y) or not x or 0 in x or 0 in y:
        return False
    return all(p / q == x[0] / y[0] for p, q in zip(x, y, strict=True))


async def _subscripts(part: Mapping[str, Any], response: Any, run: RunTool) -> tuple[str, ...] | None:
    answer = part["answer"]
    if answer["kind"] != "chemical_equation" or not isinstance(response, str):
        return None
    value, check_id = await run("chem_balance", {"equation": response})
    if value is None or check_id is None:
        return None
    written = [(str(s["side"]), _species(str(s["formula"]))[0], s.get("coefficient", 1)) for s in value["species"]]
    key = [(side, _species(s["formula"])[0]) for side in ("reactants", "products") for s in answer[side]]
    if len(written) != len(key):
        return None
    left = list(range(len(written)))
    changed = 0
    for side, formula in key:
        exact = next((n for n in left if written[n][:2] == (side, formula)), None)
        if exact is None:
            exact = next((n for n in left if written[n][0] == side and written[n][1] != formula
                          and _elements(written[n][1]) == _elements(formula)), None)  # fmt: skip
            if exact is None:
                return None
            changed += 1
        left.remove(exact)
    if not changed:
        return None
    student = value.get("student")
    if isinstance(student, Mapping):
        balanced = bool(student.get("balanced"))
    else:
        balanced = value.get("status") == "balanced" and _proportional(value.get("coefficients") or [],
                                                                       [c for _, _, c in written])  # fmt: skip
    return (check_id,) if balanced else None


# --- journal entries ------------------------------------------------------------------------------------------------


def _classes(chart: Mapping[str, Any] | None) -> dict[str, str]:
    return {str(a["account_id"]): str(a["class"]) for a in (chart or {}).get("accounts", [])}


async def _journal(rule: str, account_class: str | None, part: Mapping[str, Any], response: Any,
                   context: SignatureContext, run: RunTool) -> tuple[str, ...] | None:  # fmt: skip
    if part["answer"]["kind"] != "journal_entry":
        return None
    lines = journal_lines(response)
    if not lines:
        return None
    args: dict[str, Any] = {"task": "mark", "key": [{"lines": journal_key(part["answer"])}]}
    args |= {"entries_text": lines} if isinstance(lines, str) else {"entries": [{"lines": lines}]}
    if context.chart is not None:
        args["chart"] = dict(context.chart)
    value, check_id = await run("acc_double_entry", args)
    marking = (value or {}).get("marking")
    if check_id is None or not isinstance(marking, Mapping) or len(marking.get("entries") or []) != 1:
        return None
    marked = list(marking["entries"][0]["lines"])
    described = list(((value or {}).get("entries") or [{}])[0].get("lines") or [])
    classes = _classes(context.chart)

    def key_class(line: Mapping[str, Any]) -> str | None:
        return classes.get(str((line.get("expected") or {}).get("account_id") or ""))

    wrong = [line for line in marked if line["verdict"] != "ok"]
    if rule == "debit_credit_swapped":
        if not wrong or any(line["verdict"] != "wrong_side" for line in wrong):
            return None
        if account_class is not None and not any(key_class(line) == account_class for line in wrong):
            return None
        return (check_id,)
    if not classes:
        return None  # no chart of accounts: no class is known
    found = False
    for n, line in enumerate(marked):
        if line["verdict"] != "wrong_account":
            continue
        written = described[n].get("class") if n < len(described) else None
        expected = key_class(line)
        if written is None or expected is None or written == expected:
            return None
        if account_class is not None and written != account_class:
            return None
        found = True
    others = [line for line in wrong if line["verdict"] != "wrong_account"]
    return (check_id,) if found and not others else None
