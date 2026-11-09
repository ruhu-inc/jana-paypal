"""Build-time tool verification (CUR-20).

Every `check_id` a record names is a build check: the source states the tool and its arguments, and the build runs the
tool worker's function in-process and keeps a check record (function, version, argument and result hashes). Then:

- a numeric key must equal its check's result within the item's tolerance;
- a structured key must be reproduced by its check: a chemical equation's coefficients, a journal's or a table's
  amounts, an algebraic key (equivalent to its `math_simplify` or `math_solve` result, and in the form the item asks
  for) and an equation's solution set (its `math_solve` values);
- a worked example's final answer must show its check's result;
- every number in a solution step, a worked-example step or a hint must come from the problem statement, a
  convention, a build check's result, or be a structural constant of a formula (0, 1, 2);
- hints at rungs 1 to 4 must not contain the part's final answer (a numeric key, an algebraic key as written, or
  `x = value` for an equation's solution).

Problems name the record id and, for a wrong key, the tool's result.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Any

from agent_core.config import Settings
from agent_core.engine.marking import equation_sides, worker_assumptions
from tool_worker.functions import FUNCTIONS
from tool_worker.protocol import WorkerError
from tool_worker.registry import CallContext

WORKER_VERSION = "build"
NUMBER = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")
EXPONENT = re.compile(r"\^(\{[^}]*\}|-?\d)")
STRUCTURAL = {Decimal(0), Decimal(1), Decimal(2)}
REFERENCE_DATA = ("chem.z_",)


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Check:
    check_id: str
    function: str
    function_version: int
    args_sha256: str
    result_sha256: str
    value: Any
    warning_codes: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        return {"check_id": self.check_id, "function": self.function, "function_version": self.function_version,
                "worker_version": WORKER_VERSION, "args_sha256": self.args_sha256,
                "result_sha256": self.result_sha256}  # fmt: skip

    def shown(self) -> str:
        """The result as a reader of the problem report should see it: rounded, else exact, else its figures."""
        value = self.value if isinstance(self.value, dict) else {}
        if isinstance(value.get("rounded"), str):
            return str(value["rounded"])
        for key in ("exact", "value"):
            inner = value.get(key)
            if isinstance(inner, dict):
                return str(inner.get("text") or inner.get("decimal"))
        if isinstance(value.get("value"), str):  # an accounting figure: exact text and money at the stated places
            return str(value["value"])
        figures = sorted(self.numbers())
        if 0 < len(figures) <= 8:
            return ", ".join(str(f) for f in figures)
        return json.dumps(self.value)[:300]

    def numbers(self) -> set[Decimal]:
        """Every figure the result states: the maths and science shapes (exact decimal, rounded, converted value), the
        accounting shapes (exact and money figures, amounts, totals, balances, schedule lines, ratios) and the
        chemistry counts (coefficients, atom counts). Names, references, formulas, units and verdicts are not figures.
        """
        found: set[Decimal] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key not in NOT_FIGURES:
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str):
                number = _figure(node)
                if number is not None:
                    found.add(number)
            elif isinstance(node, int | float) and not isinstance(node, bool):
                found.add(Decimal(str(node)))

        walk(self.value)
        return found


# Result fields that hold text, ids or counters, never a figure a key or a hint could show.
NOT_FIGURES = frozenset({"ref", "date", "id", "label", "account", "account_id", "line", "period", "formula", "unit",
                         "latex", "shown", "equation", "status", "side", "verdict", "reason", "code", "source",
                         "method", "kind", "task", "element", "title", "layout", "index", "dp", "sf", "unbalanced",
                         "warnings", "trace"})  # fmt: skip
_FIGURE = re.compile(r"^\s*-?\d+(\.\d+)?([eE][-+]?\d+)?\s*$")
_FRACTION = re.compile(r"^\s*(-?\d+)/(\d+)\s*$")


def _figure(text: str) -> Decimal | None:
    """A whole result string that is a number: decimal text, or an exact fraction p/q."""
    if _FIGURE.match(text):
        return _decimal(text.strip())
    fraction = _FRACTION.match(text)
    if fraction is not None and int(fraction.group(2)) != 0:
        return Decimal(fraction.group(1)) / Decimal(fraction.group(2))
    return None


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@cache
def _timeout_attempts() -> int:
    return int(Settings().build_check_timeout_attempts)


@cache
def _symbolic_slice_ms() -> int:
    return int(Settings().build_check_symbolic_slice_ms)


def run_check(check_id: str, spec: dict[str, Any]) -> Check:
    function = FUNCTIONS.get(spec.get("tool", ""))
    if function is None:
        raise ValueError(f"{check_id}: unknown tool {spec.get('tool')!r}")
    context = CallContext(
        f"build_{check_id}", check_id, {}, {}, lambda: False, settings={"symbolic_slice_ms": _symbolic_slice_ms()}
    )
    # A time slice that ran out decides nothing (VER-27: `unknown`, reason `timeout`): the same check runs again.
    for _attempt in range(_timeout_attempts()):
        value, extras = function.implementation(spec["args"], context)
        if not (isinstance(value, dict) and value.get("reason") == "timeout"):
            break
    codes = tuple(w["code"] for w in extras.get("warnings", []))
    return Check(check_id, function.name, function.version, _sha256(spec["args"]), _sha256(value), value, codes)


def numbers_in(text: str) -> list[Decimal]:
    """Numbers a reader sees in text or LaTeX; exponents of units and powers are structure, not values."""
    stripped = EXPONENT.sub(" ", text.replace("{,}", "").replace("\\,", " "))
    return [n for n in (_decimal(m) for m in NUMBER.findall(stripped)) if n is not None]


def close(a: Decimal, b: Decimal, tolerance: dict[str, Any] | None = None) -> bool:
    if tolerance is None:
        return abs(a - b) <= Decimal("1e-9") * max(Decimal(1), abs(b))
    mode, value = tolerance["mode"], Decimal(str(tolerance["value"]))
    if mode == "absolute":
        return abs(a - b) <= value
    if mode == "relative":
        return abs(a - b) <= value * abs(b)
    # sig_figs: equal once both are rounded to that many significant figures
    digits = int(value)
    return _round_sf(a, digits) == _round_sf(b, digits)


def _round_sf(x: Decimal, digits: int) -> Decimal:
    if x == 0:
        return x
    return round(x, digits - 1 - x.adjusted())


def _strings(node: Any, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, str):
                out.append(value)
            else:
                out += _strings(value, keys)
    elif isinstance(node, list):
        for value in node:
            out += _strings(value, keys)
    return out


def balance_problems(owner: str, check_id: str, value: Any) -> list[str]:
    """A `chem_balance` build check (VER-19) must balance its equation, and coefficients it states (given, or written
    in the equation) must be the balanced ones in lowest terms."""
    value = value if isinstance(value, dict) else {}
    if value.get("status") != "balanced":
        return [f"{owner}: {check_id} chem_balance cannot balance the equation ({value.get('reason', 'unknown')})"]
    student = value.get("student")
    if not isinstance(student, dict) or student.get("verdict") in ("correct", "balanced"):
        return []
    stated = " ".join(str(c) for c in student.get("coefficients", []))
    if student.get("verdict") == "not_balanced":
        elements = ", ".join(str(e) for e in student.get("unbalanced", []))
        return [f"{owner}: {check_id} states coefficients {stated}, which chem_balance finds not balanced "
                f"({elements}); balanced: {' '.join(str(c) for c in value['coefficients'])}"]  # fmt: skip
    return [f"{owner}: {check_id} states coefficients {stated}, balanced but not in lowest terms "
            f"({' '.join(str(c) for c in value['coefficients'])})"]  # fmt: skip


_STATE = re.compile(r"\((s|l|g|aq)\)$")


def _formula(text: str) -> str:
    return _STATE.sub("", re.sub(r"[\s^]", "", text))


def equation_key_problems(answer: dict[str, Any], check: Check) -> list[str]:
    """A chemical equation's key: the species and coefficients `chem_balance` gives for the key's equation."""
    if check.function != "chem_balance" or not isinstance(check.value, dict) or "coefficients" not in check.value:
        return [f"{answer['check_id']} is not a chem_balance check that balances the equation"]
    found = {(str(s["side"]), _formula(str(s["formula"]))): int(s["coefficient"]) for s in check.value["species"]}
    key = {(side, _formula(str(s["formula"]))): int(s["coefficient"]) for side in ("reactants", "products")
           for s in answer[side]}  # fmt: skip
    if set(found) != set(key):
        return [f"the key's species are not those of {answer['check_id']} ({check.value.get('equation')})"]
    if found != key:
        stated = " ".join(str(key[k]) for k in found)
        return [f"key coefficients {stated} are not chem_balance's {' '.join(str(c) for c in found.values())} "
                f"({answer['check_id']})"]  # fmt: skip
    return []


def _amount(text: Any) -> Decimal:
    return Decimal(str(text))


def journal_key_problems(answer: dict[str, Any], check: Check) -> list[str]:
    """A journal entry's key balances, each line has a debit or a credit, and every amount is a figure of its
    `acc_double_entry` build check (VER-22)."""
    problems = []
    debits = sum((_amount(line["debit"]) for line in answer["lines"]), Decimal(0))
    credits = sum((_amount(line["credit"]) for line in answer["lines"]), Decimal(0))
    if debits != credits:
        problems.append(f"the key does not balance (debits {debits}, credits {credits})")
    for line in answer["lines"]:
        if (_amount(line["debit"]) == 0) == (_amount(line["credit"]) == 0):
            problems.append(f"key line {line['account_id']} needs a debit or a credit, not both or neither")
    if check.function != "acc_double_entry":
        return [*problems, f"{answer['check_id']} is not an acc_double_entry check"]
    if isinstance(check.value, dict) and check.value.get("all_balanced") is False:
        problems.append(f"{answer['check_id']}: acc_double_entry finds the entries unbalanced")
    figures = check.numbers()
    for line in answer["lines"]:
        amount = max(_amount(line["debit"]), _amount(line["credit"]))
        if amount not in figures:
            problems.append(f"key amount {amount} ({line['account_id']}) is not a figure of {answer['check_id']}")
    return problems


def table_key_problems(answer: dict[str, Any], check: Check) -> list[str]:
    """An accounting table's key: every line's amount is a figure of its build check (`acc_statements`,
    `acc_double_entry` or another accounting checker), and a trial balance's debits equal its credits."""
    problems = []
    if not check.function.startswith("acc_"):
        problems.append(f"{answer['check_id']} is not an accounting check")
    figures = check.numbers()
    for row in answer["rows"]:
        if _amount(row["amount"]) not in figures:
            problems.append(f"key line {row['label']!r} amount {row['amount']} is not a figure of "
                            f"{check.function} ({answer['check_id']})")  # fmt: skip
    if answer["table_kind"] == "trial_balance":
        sides = {s: sum((_amount(r["amount"]) for r in answer["rows"] if r["side"] == s), Decimal(0))
                 for s in ("debit", "credit")}  # fmt: skip
        if sides["debit"] != sides["credit"]:
            problems.append(f"the trial balance key does not balance (debits {sides['debit']}, credits "
                            f"{sides['credit']})")  # fmt: skip
    return problems


def _tool(name: str, args: dict[str, Any]) -> Any:
    """A checker run in-process to compare a key with its build check's result; None when it refuses."""
    context = CallContext(f"build_{name}", "chk_build_key", {}, {}, lambda: False, settings={})
    try:
        return FUNCTIONS[name].implementation(args, context)[0]
    except WorkerError:
        return None


def _equivalent(a: str, b: str, scope: dict[str, Any]) -> bool:
    found = _tool("math_check_equivalence", {"a": a, "b": b, **scope})
    return isinstance(found, dict) and found.get("verdict") in ("equivalent", "equivalent_on_domain")


def _scope(answer: dict[str, Any], variables: list[str]) -> tuple[dict[str, Any], list[str]]:
    assumptions = worker_assumptions(list(answer.get("assumptions", [])))
    if assumptions is None:
        return {}, [
            f"an assumption of {answer['check_id']} cannot be stated to the checker "
            f"({'; '.join(answer['assumptions'])}); use `x > 0`, `x \\geq 0`, `x \\neq 0` or `x integer`"
        ]
    return {"variables": variables} | ({"assumptions": assumptions} if assumptions else {}), []  # fmt: skip


def algebraic_key_problems(answer: dict[str, Any], check: Check) -> list[str]:
    """An algebraic key is the result of its build check and in the form the item asks for (ASM-05, VER-10): a
    `math_simplify` check to the required form (`simplest` when any form is accepted) whose result is equivalent to
    the key, which `math_check_answer` finds in that form; or, for a formula's subject (`solved_for`, the key written
    `subject = expression`), a `math_solve` check for the subject with a solution equivalent to the key's side."""
    scope, problems = _scope(answer, list(answer["variables"]))
    if problems:
        return problems
    value = check.value if isinstance(check.value, dict) else {}
    form, key = str(answer["required_form"]), str(answer["expression"])
    if form == "solved_for":
        sides = equation_sides(key)
        if sides is None:
            return [f"a key solved for a subject is written `subject = expression`, not {key!r}"]
        subject, key = sides
        if check.function != "math_solve" or value.get("case") != "solutions":
            return [f"{answer['check_id']} is not a math_solve check that solves for {subject}"]
        found = [str(s[subject]["text"]) for s in value["solutions"] if subject in s]
        if not any(_equivalent(key, f, scope) for f in found):
            return [f"the key {key} is not math_solve's {subject} = {' or '.join(found) or 'nothing'} "
                    f"({answer['check_id']})"]  # fmt: skip
        return []
    wanted = "simplest" if form == "any" else form
    if check.function != "math_simplify" or value.get("status") != "ok" or value.get("form") != wanted:
        return [f"{answer['check_id']} is not a math_simplify check to the {wanted} form"]
    result = str(value["result"]["text"])
    if not _equivalent(key, result, scope):
        return [f"the key {key} is not equivalent to math_simplify's {result} ({answer['check_id']})"]
    if form != "any":
        checked = _tool("math_check_answer", {"student": key, "key": result,
                                              "rule": {"form": form, "quantity": "exact"}, **scope})  # fmt: skip
        if ((checked or {}).get("components") or {}).get("form") != "ok":
            return [f"the key {key} is not in the {form} form the item asks for"]
    return []


def solutions_key_problems(answer: dict[str, Any], check: Check) -> list[str]:
    """An equation's solution set is its `math_solve` build check's (VER-07): every key value is one of the solver's
    values for the item's unknown (exactly, or a rounded decimal within the item's tolerance), and every solver value
    is in the key."""
    variable = str(answer["variable"])
    value = check.value if isinstance(check.value, dict) else {}
    if check.function != "math_solve" or value.get("case") != "solutions":
        return [f"{answer['check_id']} is not a math_solve check with solutions"]
    found = list({str(s[variable]["text"]): s[variable] for s in value["solutions"] if variable in s}.values())
    scope = {"variables": [variable]}

    def same(key: str, solved: dict[str, Any]) -> bool:
        written = _decimal(key) if re.fullmatch(r"\s*-?\d+\.\d+\s*", key) else None
        if written is not None and "decimal" in solved:
            return close(written, Decimal(str(solved["decimal"])), answer["tolerance"])
        return _equivalent(key, str(solved["text"]), scope)

    keys = [str(k) for k in answer["values"]]
    problems = [f"key value {k} is not a solution math_solve gives for {variable} ({answer['check_id']})"
                for k in keys if not any(same(k, f) for f in found)]  # fmt: skip
    problems += [f"math_solve's {variable} = {f['text']} is missing from the key ({answer['check_id']})"
                 for f in found if not any(same(k, f) for k in keys)]  # fmt: skip
    return problems


STRUCTURED_KEYS = {"chemical_equation": equation_key_problems, "journal_entry": journal_key_problems,
                   "accounting_table": table_key_problems, "algebraic": algebraic_key_problems,
                   "equation_solutions": solutions_key_problems}  # fmt: skip


def _bare(text: str) -> str:
    """Text without spaces, `$` or sizing commands, to find a written answer inside a hint."""
    return re.sub(r"\s+|\$|\\left|\\right|\\,", "", text)


def gives_away(answer: dict[str, Any], hint: str) -> bool:
    """Whether a hint states the final answer of a free-response maths part: the algebraic key, or `x = value` for
    a key value of an equation."""
    bare = _bare(hint)
    if answer["kind"] == "algebraic":
        return _bare(str(answer["expression"])) in bare
    if answer["kind"] == "equation_solutions":
        return any(_bare(f"{answer['variable']} = {v}") in bare for v in answer["values"])
    return False


class Verifier:
    def __init__(self, checks: dict[str, dict[str, Any]], conventions: list[dict[str, Any]]) -> None:
        self.specs = checks
        self.results: dict[str, Check] = {}
        self.problems: list[str] = []
        # Reference data a visual draws (VSL-12 N3, proton numbers) is not a number a solution may quote unchecked.
        self.convention_numbers = {n for c in conventions if not c["key"].startswith(REFERENCE_DATA)
                                   for n in numbers_in(c["value"])}  # fmt: skip
        self.pack_constants = {c["key"].rsplit(".", 1)[-1]: (c["value"], c.get("unit")) for c in conventions}

    def check(self, owner: str, check_id: str, overrides: dict[str, str] | None = None) -> Check | None:
        """Run one build check; `overrides` are the item's convention overrides by symbol (CUR-28), which take the
        place of the pack's value for a constant the check takes from the pack (VER-12: the item before the pack)."""
        if check_id in self.results:
            return self.results[check_id]
        spec = self.specs.get(check_id)
        if spec is None:
            self.problems.append(f"{owner}: {check_id} has no build check (tool and arguments)")
            return None
        overrides = overrides or {}
        entries = spec.get("args", {}).get("constants", [])
        unstated = [
            e["symbol"]
            for e in entries
            if e.get("source") == "pack"
            and "value" not in e
            and e["symbol"] not in self.pack_constants
            and e["symbol"] not in overrides
        ]
        if unstated:
            self.problems.append(f"{owner}: {check_id} needs a constant that neither the item nor the pack states "
                                 f"({', '.join(unstated)}, VER-12)")  # fmt: skip
            return None
        spec = self._with_pack_constants(spec, overrides)
        try:
            result = run_check(check_id, spec)
        except (WorkerError, ValueError, KeyError, TypeError) as error:
            self.problems.append(f"{owner}: {check_id} failed in {spec.get('tool')}: {error}")
            return None
        # VER-12: a shipped pack states every constant its items use, so no check reaches the tool default.
        for code in result.warning_codes:
            if code in ("convention_default_used", "convention_missing"):
                self.problems.append(f"{owner}: {check_id} needs a constant that neither the item nor the pack "
                                     f"states ({code}, VER-12)")  # fmt: skip
        if result.function == "chem_balance":
            self.problems += balance_problems(owner, check_id, result.value)
        self.results[check_id] = result
        return result

    def _with_pack_constants(self, spec: dict[str, Any], overrides: dict[str, str]) -> dict[str, Any]:
        """Fill `{symbol, source: pack}` entries from the item's convention overrides, else from the pack's conventions
        (a key's last part is its symbol)."""
        entries = spec.get("args", {}).get("constants")
        if not entries:
            return spec
        filled = []
        for entry in entries:
            if entry.get("source") == "pack" and "value" not in entry and entry["symbol"] in overrides:
                unit = self.pack_constants.get(entry["symbol"], (None, None))[1]
                entry = {**entry, "source": "item", "value": overrides[entry["symbol"]], **({"unit": unit} if unit
                                                                                             else {})}  # fmt: skip
            elif entry.get("source") == "pack" and "value" not in entry and entry["symbol"] in self.pack_constants:
                value, unit = self.pack_constants[entry["symbol"]]
                entry = {**entry, "value": value, **({"unit": unit} if unit else {})}
            filled.append(entry)
        return {**spec, "args": {**spec["args"], "constants": filled}}

    def _allowed(self, owner: str, given: set[Decimal], text: str, where: str, check_ids: list[str]) -> None:
        allowed = given | self.convention_numbers | STRUCTURAL
        for check_id in check_ids:
            result = self.results.get(check_id)
            if result is not None:
                allowed |= result.numbers()
        for number in numbers_in(text):
            if not any(close(number, a) for a in allowed):
                self.problems.append(f"{owner}: {where} shows {number}, which no build check or the statement gives")

    def item(self, item: dict[str, Any]) -> None:
        owner = item["id"]
        overrides = {str(o["key"]).rsplit(".", 1)[-1]: str(o["value"]) for o in item.get("conventions_override", [])}
        given = set(numbers_in(item["stem"])) | {n for p in item["parts"] for n in numbers_in(p["prompt"])}
        step_checks = [s["check_id"] for s in item["solution"].get("steps", []) if "check_id" in s]
        for check_id in step_checks:
            self.check(owner, check_id, overrides)
        for part in item["parts"]:
            answer = part["answer"]
            key: Decimal | None = None
            if "check_id" in answer:
                result = self.check(owner, answer["check_id"], overrides)
                if answer["kind"] == "numeric" and result is not None:
                    key = _decimal(answer["value"])
                    if key is None or not any(close(key, n, answer["tolerance"]) for n in result.numbers()):
                        self.problems.append(
                            f"{owner} part {part['id']}: key {answer['value']} does not match {result.function} "
                            f"result {result.shown()} ({answer['check_id']})"
                        )
            if answer["kind"] in STRUCTURED_KEYS and "check_id" in answer:
                checked = self.results.get(answer["check_id"])
                if checked is not None:
                    self.problems += [f"{owner} part {part['id']}: {p}" for p in STRUCTURED_KEYS[answer["kind"]](
                        answer, checked)]  # fmt: skip
            checks = [*step_checks, *([answer["check_id"]] if "check_id" in answer else [])]
            for hint in part["hints"]:
                text = " ".join(_strings(hint, ("en", "fr", "pt", "ar")))
                self._allowed(owner, given, text, f"hint {hint['rung']} of part {part['id']}", checks)
                if key is not None and hint["rung"] <= 4:
                    tolerance = answer.get("tolerance")
                    if any(close(n, key, tolerance) for n in numbers_in(text)):
                        self.problems.append(
                            f"{owner} part {part['id']}: hint {hint['rung']} gives away the final answer {key}"
                        )
                if hint["rung"] <= 4 and gives_away(answer, text):
                    self.problems.append(f"{owner} part {part['id']}: hint {hint['rung']} gives away the final answer")
        for step in item["solution"].get("steps", []):
            self._allowed(owner, given, step["latex"], f"solution step {step['n']}", step_checks)

    def worked_example(self, example: dict[str, Any]) -> None:
        owner = example["id"]
        given = set(numbers_in(example["problem"]))
        checks = [s["check_id"] for s in example["steps"] if "check_id" in s]
        final = example["final_answer"]
        for check_id in [*checks, final["check_id"]]:
            self.check(owner, check_id)
        result = self.results.get(final["check_id"])
        if result is not None:
            shown = numbers_in(final["latex"])
            if not shown or not any(close(n, r) for n in shown for r in result.numbers()):
                self.problems.append(
                    f"{owner}: final answer {final['latex']} does not show {result.function} result "
                    f"{result.shown()} ({final['check_id']})"
                )
        for step in example["steps"]:
            self._allowed(owner, given, step["latex"], f"step {step['n']}", [*checks, final["check_id"]])
