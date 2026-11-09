"""The S1 slice flows beyond guided solve: check my working (AGT-21 and ASM-09, S1 subset) and practice help (AGT-23,
S1 subset), for the S1 topic only. The full flows arrive with M12 and M14.

Check my working: each line of the student's working is checked by code. A line with the same unknowns as the line
before it goes to `math_check_step` (VER-06): valid, or invalid with its error category. A line the step checker
cannot relate to the one before (it substitutes values into a formula) is solved for the problem's unknown with
`math_solve`, and is valid when its solution is the plan's checked answer; for simultaneous equations it is valid when
it holds at the checked solution. A line substituting the board's givens into the formula before it (`v = 0 + 2*5`
after `v = u + a*t`) is valid when it holds at the formula's value for its subject at those givens. Lines are never
matched against the plan's own route, so any valid method passes (ASM-09, `asm09_alternative_methods`). The first
invalid line is the first error; later lines are checked against the line before them (error carried forward). A
formula line with no numbers is shown unchecked. Every result carries the check id of the tool call that decided it.

Practice help: the item's stored, build-verified solution is the plan (no Solver call). An answer is marked by the
deterministic marker against the item's key and marking rule (CUR-15, ASM-02); hints come from the item's authored
ladder, which the Coach only rephrases (PED-04); an attempt comes before the first hint (PED-05).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from typing import Any

from agent_core.engine.marking import Converted, Verdict, mark
from agent_core.engine.spoken import latex_to_plain
from agent_core.engine.state import Plan, PlanStep, numbers_in
from agent_core.router import ToolCall

# A unit at the end of a line, only right after a number ("s = 10 m"; never the variable in "2 * 9.8 * s").
UNIT_TAIL = re.compile(r"(?<=\d)\s+(?:m/s\^?2|m/s²|m\s*s\^-?1|m/s|km/h|km|cm|mm|mol|m|s|h|kg|g|N|J|W|Pa)\s*\.?$")
FORMULA_UNKNOWN = re.compile(r"^\s*([A-Za-z])\s*=")
BOARD_STATUS = {"correct": "correct", "incorrect": "incorrect", "partial": "incorrect", "unverifiable": "unchecked"}


@dataclass
class LineResult:
    line: str
    status: str  # correct | incorrect | unchecked
    check_id: str | None
    category: str | None = None
    carried_forward: bool = False
    first_error: int | None = None  # the 0-based position of the line whose error this one carries forward
    worded: bool = False  # a step written in words: no tool checks it (VER-27)
    before: int | None = None  # the 0-based position of the line this one was checked against, step by step
    moves: dict[str, Any] | None = None  # the step checker's term moves for a valid step (UXS-61)


# A step written in words ("divide both sides by two"): two or more words of three letters or more that are not maths
# names. The grammar would read it as a product of one-letter variables (VER-03), so it is never sent to a checker.
_WORD = re.compile(r"[A-Za-z]{3,}")
MATHS_WORDS = frozenset({"sin", "cos", "tan", "sec", "csc", "cot", "arcsin", "arccos", "arctan", "sinh", "cosh",
                         "tanh", "log", "exp", "sqrt", "abs", "frac", "cdot", "times", "div", "left", "right", "text",
                         "mathrm", "theta", "alpha", "beta", "gamma", "delta", "lambda", "omega", "phi", "sigma",
                         "rho", "mu", "tau", "eta", "epsilon", "infty", "dfrac", "tfrac", "pmod", "mod"})  # fmt: skip
WORDED_REASON = "Written in words, so no tool checked this line."
CARRIED_REASON = "checked against your previous line (error carried forward from line {line})"
# Magnitudes in these coherent SI units can be substituted together without an implicit scale conversion.
# Other unit combinations need an explicit conversion; equal unit spellings can still share their written scale.
COHERENT_UNITS = frozenset({"m", "s", "kg", "mol", "A", "K", "m/s", "m/s^2", "m^2", "m^3",
                            "N", "J", "W", "Pa", "C", "V", "ohm", "Hz"})  # fmt: skip


def worded(text: str) -> bool:
    """A working line written as words rather than maths (VER-27, ASM-09): it stays on the board as not checked."""
    words = [w for w in _WORD.findall(text.replace("\\", " ")) if w.lower() not in MATHS_WORDS]
    return len(words) >= 2


def line_reason(result: LineResult) -> str | None:
    """A checked line's one-line reason on the board (UXS-15): a carried-forward line names the line its error comes
    from (ASM-09), a worded line says no tool checked it, an incorrect line names its category."""
    if result.carried_forward and result.first_error is not None:
        return CARRIED_REASON.format(line=result.first_error + 1)
    if result.carried_forward:
        return "checked against your previous line (error carried forward)"
    if result.worded:
        return WORDED_REASON
    return result.category


def for_tool(text: str) -> str:
    """A typed working line as the tools read it: units at the end dropped, multiplication signs normalised."""
    line = text.strip().replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    line = latex_to_plain(line).strip().strip("$")
    line = line.replace("×", "*").replace("·", "*").replace("−", "-").replace("²", "^2")
    previous = None
    while previous != line:
        previous, line = line, UNIT_TAIL.sub("", line).strip()
    return line


def unknown_of(plan: Plan) -> str | None:
    """The symbol the plan solves for, from its final answer as written (`s = 10 m`), else its last such step."""
    for latex in [plan.final_latex, *(s.latex for s in reversed(plan.steps))]:
        found = FORMULA_UNKNOWN.match(latex.replace("\\", " "))
        if found:
            return found[1]
    return None


def requested_unknown(statement: str) -> str | None:
    """Unambiguous, explicitly requested single-letter notation; never inferred from a student's answer.

    This names a goal, not its value. Ambiguous multi-symbol requests and symbols explicitly assigned a
    numeric given are left to the normal plan/system path.
    """
    declarations = re.findall(
        r"\b(?:use|let)\s+[`$]?([A-Za-z])[`$]?\s+(?:for|represent|denote|be)\b"
        r"(?!\s*[-+]?\d)\s+([^.!?;\n]+)", statement, re.I,
    )
    if re.search(
        r"\b(?:find|calculate|determine|solve\s+for)\s+[A-Za-z]\s*(?:,\s*|and\s+)[A-Za-z]\b",
        statement, re.I,
    ):
        return None
    goals: set[str] = set(
        re.findall(
            r"\b(?:find|calculate|determine|solve\s+for)\s+[`$]?([A-Za-z])[`$]?(?=\s*(?:[.?!;:]|$))",
            statement, re.I,
        )
    )
    stop = {"the", "an", "of", "in", "for", "its", "this", "that", "and", "unknown", "quantity", "value"}

    def terms(text: str) -> set[str]:
        return set(re.findall(r"[a-z]{2,}", text.lower())) - stop

    asks = re.findall(
        r"\b(?:find|calculate|determine|solve\s+for|what\s+is|what\s+are)\s+([^.!?;\n]+)", statement, re.I
    )
    targets = [terms(re.split(r"\b(?:use|let)\b", ask, flags=re.I)[0]) for ask in asks]
    named: set[str] = set()
    for symbol, description in declarations:
        meaning = terms(re.split(r"\b(?:use|let)\b", description, flags=re.I)[0])
        if meaning and any(meaning <= target for target in targets):
            named.add(symbol)
    choices = goals if len(goals) == 1 else named if not goals else set()
    if len(choices) != 1:
        return None
    symbol = next(iter(choices))
    if re.search(rf"\b{re.escape(symbol)}\s*=\s*[-+]?\d", statement):
        return None
    return symbol


async def run_tool(engine: Any, turn: Any, name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None,
                                                                                    str | None]:  # fmt: skip
    """One engine-initiated tool call on the Checker's grant, recorded in the turn's trace like any other."""
    call = ToolCall(f"slice_{name}_{len(turn.tools.log)}", name, args)
    turn.last_compute_error = None
    outcomes = await turn.tools.run([call], "checker", engine._tool_session("checker", "small"), [name], False,
                                    engine.cancelled)  # fmt: skip
    if not outcomes or outcomes[0].result["status"] != "ok":
        if outcomes:
            failure = outcomes[0].result.get("error") or {}
            turn.last_compute_error = str(failure.get("message") or failure.get("code") or "unavailable")[:240]
        return None, None
    result = outcomes[0].result
    checks = result["meta"].get("checks") or []
    data = result.get("data") or {}
    value = data.get("value") if isinstance(data, dict) else None
    return (value if isinstance(value, dict) else None), (checks[0]["check_id"] if checks else None)


def _solution_values(value: dict[str, Any] | None, unknown: str) -> list[Fraction]:
    out = []
    for solution in (value or {}).get("solutions", []):
        entry = solution.get(unknown) if isinstance(solution, dict) else None
        if isinstance(entry, dict):
            scalar = str(entry.get("decimal") or entry.get("text") or "").strip()
            # A coefficient in a symbolic answer is not a solved numerical value (e.g. 10*f is not 10).
            if re.fullmatch(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?(?:\s*/\s*\d+)?", scalar):
                out += numbers_in(scalar)
    return out


def _close(a: Fraction, b: Fraction) -> bool:
    return abs(a - b) <= Fraction(1, 1000) * max(Fraction(1), abs(b))


# `x = 3` stated on its own: at the start of the answer or after a comma, semicolon or "and" (never `x + y = 4`).
ASSIGNMENT = re.compile(r"(?:^|[,;]|\band\b)\s*([A-Za-z])\s*=\s*(-?\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*(?=$|[,;]|\band\b)")


def system_of(engine: Any, plan: Plan) -> dict[str, Fraction]:
    """A problem with two or more unknowns (simultaneous equations): each unknown's checked value, from the plan's
    final answer as written (`x = 3, y = 1`), else from a plan step stating it. A value no check record of this
    session holds is not used (PRD-05); an empty map when the answer names fewer than two unknowns."""
    checked = [v for values in engine.state.check_values.values() for v in values]
    stated: dict[str, list[Fraction]] = {}
    for latex in [plan.final_latex, *(step.latex for step in plan.steps if step.check_id)]:
        for symbol, number in ASSIGNMENT.findall(for_tool(latex)):
            stated.setdefault(symbol, []).append(Fraction(number.replace(" ", "")))
    named = {symbol for symbol, _ in ASSIGNMENT.findall(for_tool(plan.final_latex))}
    system = {}
    for symbol in named:
        values = [v for v in stated[symbol] if any(_close(v, c) for c in checked)]
        if values and all(_close(v, values[0]) for v in values):
            system[symbol] = values[0]
    return system if len(system) >= 2 and len(system) == len(named) else {}


async def _check_in_system(engine: Any, turn: Any, line: str, system: dict[str, Fraction]
                           ) -> tuple[str, str | None, str | None]:  # fmt: skip
    """A line of working on simultaneous equations, whatever the route (elimination, substitution, a rearranged
    equation): it is valid when it holds at the checked solution. The system has one solution, so a line in its
    unknowns that holds there follows from the system, and one that does not hold is wrong (ASM-09). One unknown
    is solved for with the others' checked values given; a line naming any other symbol stays unchecked."""
    parsed, _ = await run_tool(engine, turn, "math_parse", {"text": line})
    symbols = sorted(set((parsed or {}).get("free_symbols", [])))
    if not symbols or not set(symbols) <= system.keys():
        return "unchecked", None, None
    target, others = symbols[0], symbols[1:]
    equations = [line, *(f"{other} = {system[other]}" for other in others)]
    value, check_id = await run_tool(engine, turn, "math_solve", {"equations": equations, "unknowns": symbols})
    found = _solution_values(value, target)
    if not found or check_id is None:
        return "unchecked", None, None
    if len(found) == 1 and _close(found[0], system[target]):
        return "correct", check_id, None
    return "incorrect", check_id, "other"


def givens_of(engine: Any) -> dict[str, Fraction]:
    """The givens on the board (the Diagnoser's knowns), each symbol with its value; a given with no number is left
    out."""
    return {k["symbol"]: numbers_in(str(k.get("value", "")))[0]
            for k in engine.state.board.knowns if numbers_in(str(k.get("value", "")))}  # fmt: skip


def target_unknown(engine: Any, plan: Plan) -> str | None:
    """Bind the explicitly requested goal even if a model labelled its checked result differently."""
    statement = engine.state.problem.statement if engine.state.problem is not None else ""
    requested = requested_unknown(statement)
    if requested and requested not in givens_of(engine) and not system_of(engine, plan):
        return requested
    return unknown_of(plan)


async def _check_substitution(engine: Any, turn: Any, formula: str, line: str
                              ) -> tuple[str, str | None, str | None]:  # fmt: skip
    """A line that substitutes the givens into the formula before it (`v = 0 + 2*5` after `v = u + a*t`), whatever
    the route: when every symbol of the formula but the line's subject is a given on the board, the formula is solved
    for the subject at the givens' values, and the line is valid when it holds there (ASM-09). Unchecked when the
    formula names any other quantity or does not fix one value for the subject."""
    subject = FORMULA_UNKNOWN.match(line)
    givens = givens_of(engine)
    if not subject or not givens or subject[1] in givens:
        return "unchecked", None, None
    parsed, _ = await run_tool(engine, turn, "math_parse", {"text": formula})
    symbols = set((parsed or {}).get("free_symbols", []))
    if (
        (parsed or {}).get("kind") != "relation"
        or subject[1] not in symbols
        or not symbols - {subject[1]} <= givens.keys()
    ):
        return "unchecked", None, None
    others = sorted(symbols - {subject[1]})
    equations = [formula, *(f"{other} = {givens[other]}" for other in others)]
    value, _ = await run_tool(engine, turn, "math_solve", {"equations": equations, "unknowns": [subject[1], *others]})
    found = _solution_values(value, subject[1])
    if len(found) != 1:
        return "unchecked", None, None
    return await _check_in_system(engine, turn, line, {**givens, subject[1]: found[0]})


async def check_lines(
    engine: Any,
    turn: Any,
    lines: list[str],
    plan: Plan,
    *,
    start_index: int = 0,
    earlier_error: bool = False,
    first_error_at: int | None = None,
) -> list[LineResult]:
    """Each line from `start_index` checked against the last line before it a tool can read (a worded or unreadable
    line is skipped over, VER-27). `first_error_at` is the position of an earlier line's error, if any."""
    unknown = target_unknown(engine, plan)
    answer = numbers_in(plan.final_value)
    system = system_of(engine, plan)
    results: list[LineResult] = []
    first_error_seen = earlier_error or first_error_at is not None
    first_error = first_error_at
    for index in range(start_index, len(lines)):
        raw = lines[index]
        line = for_tool(raw)
        status, check_id, category, moves = "unchecked", None, None, None
        if "[?]" in raw:
            results.append(LineResult(raw, "unchecked", None, "unreadable"))
            continue
        if worded(raw):
            results.append(LineResult(raw, "unchecked", None, None, worded=True))
            continue
        before_at = next((i for i in range(index - 1, -1, -1) if "[?]" not in lines[i] and not worded(lines[i])),
                         None) if index > 0 and "[?]" not in lines[index - 1] else None  # fmt: skip
        before = lines[before_at] if before_at is not None else None
        if before is not None:
            value, check_id = await run_tool(engine, turn, "math_check_step",
                                             {"prev": for_tool(before), "next": line, "moves": True})  # fmt: skip
            verdict = (value or {}).get("verdict")
            if verdict == "valid":
                status = "correct"
                if (value or {}).get("moves"):
                    moves = {"moves": value["moves"], "terms": value["terms"]}  # type: ignore[index]
            elif verdict in ("invalid", "valid_but_lost_solutions", "valid_but_extraneous"):
                status, category = "incorrect", (value or {}).get("category", "illegal_operation")
        if status == "unchecked" and system and numbers_in(line) and "=" in line:
            status, check_id, category = await _check_in_system(engine, turn, line, system)
        elif status == "unchecked" and unknown and numbers_in(line) and "=" in line:
            value, check_id = await run_tool(engine, turn, "math_solve", {"equations": [line], "unknowns": [unknown]})
            found = _solution_values(value, unknown)
            if found and answer:
                status = (
                    "correct"
                    if len(found) == len(answer) and all(any(_close(f, expected) for expected in answer) for f in found)
                    else "incorrect"
                )
                category = None if status == "correct" else "other"
            else:
                check_id = None
        if status == "unchecked" and before is not None and numbers_in(line) and "=" in line:
            status, check_id, category = await _check_substitution(engine, turn, for_tool(before), line)
        results.append(LineResult(raw, status, check_id if status != "unchecked" else None, category,
                                  carried_forward=first_error_seen, first_error=first_error, before=before_at,
                                  moves=moves if status == "correct" else None))  # fmt: skip
        if status == "incorrect" and not first_error_seen:
            first_error = index
        first_error_seen = first_error_seen or status == "incorrect"
    return results


BARE_SIDE = re.compile(r"\s*(?:[A-Za-z](?:_\{?\w+\}?)?\s*)?")
# A few words before a stated equation ("oh, then s = 10 m"): words of two or more letters, then a bare symbol.
LEAD_WORDS = re.compile(r"\s*((?:[A-Za-z\u2019']{2,}[,.!]?\s+)+)([A-Za-z](?:_\{?\w+\}?)?\s*=.*)", re.S)
FUNCTION_WORDS = frozenset({"sin", "cos", "tan", "log", "ln", "sqrt", "exp", "arcsin", "arccos", "arctan"})


def equation_part(text: str) -> str:
    """The equation a reply states after a few words ("oh, then s = 10 m" gives "s = 10 m"), or the text as it is. A
    function name before the symbol ("sin x = 0.5") is maths, not a lead-in."""
    found = LEAD_WORDS.fullmatch(text)
    if found is None or found.group(1).split()[-1].strip(",.!").lower() in FUNCTION_WORDS:
        return text
    return found.group(2)


async def reduced_chain(engine: Any, turn: Any, line: str, stated: Fraction) -> tuple[str, bool]:
    """A chained line ("a = 20 / 4 = 5 m/s^2") as the value it states ("a = 5 m/s^2"), and whether its plain arithmetic
    agrees with that value. A side with symbols or units ("(v - u)/t") stays unjudged here; so does a line whose first
    side is not one symbol, or one the checker cannot evaluate."""
    sides = [side.strip() for side in line.split("=")]
    if len(sides) < 3 or not BARE_SIDE.fullmatch(sides[0]) or not sides[0]:
        return line, True
    for side in sides[1:-1]:
        if re.search(r"[A-Za-z]", side):
            continue
        result, _ = await run_tool(engine, turn, "math_evaluate", {"expression": for_tool(side), "values": {}})
        found = ((result or {}).get("exact") or {}).get("text") if (result or {}).get("status") == "ok" else None
        try:
            if found is not None and Fraction(str(found)) != stated:
                return line, False
        except (ValueError, ZeroDivisionError):
            continue
    return f"{sides[0]} = {sides[-1]}", True


async def check_reply_equation(engine: Any, turn: Any, text: str, plan: Plan) -> tuple[str, str | None] | None:
    """A guided reply that is one equation in the problem's own quantities, checked on its own maths whatever the
    route ("x - 2 = 5" after dividing first, "4x - 8 = 20" after expanding; elimination or substitution for a pair).
    A line in the givens only ("1/2 g = 5") holds at the givens' values; a one-unknown equation is solved for the
    unknown and compared with the checked answer; a simultaneous-equations line holds at the checked solution. None
    when the line names any other quantity or no tool decides it: the caller then matches stated values (ASM-09)."""
    line = for_tool(text)
    if line.count("=") != 1 or worded(text) or not numbers_in(line):
        return None
    system = system_of(engine, plan)
    if system:
        status, check_id, _ = await _check_in_system(engine, turn, line, system)
        return None if status == "unchecked" else (status, check_id)
    parsed, _ = await run_tool(engine, turn, "math_parse", {"text": line})
    if not parsed or parsed.get("kind") != "relation":
        return None
    symbols = set(parsed.get("free_symbols", []))
    givens = givens_of(engine)
    if symbols and symbols <= givens.keys():
        status, check_id, _ = await _check_in_system(engine, turn, line, givens)
        return None if status == "unchecked" else (status, check_id)
    unknown, answer = target_unknown(engine, plan), numbers_in(plan.final_value)
    if not unknown or not answer or unknown in givens or symbols != {unknown}:
        return None
    found, check_id = await _goal_values(engine, turn, line, unknown)
    if not found or check_id is None:
        return None
    right = len(found) == len(answer) and all(any(_close(f, expected) for expected in answer) for f in found)
    return ("correct" if right else "incorrect"), check_id


async def _goal_values(engine: Any, turn: Any, line: str, target: str) -> tuple[list[Fraction], str | None]:
    """A direct scalar assignment uses arithmetic, including in subjects without algebra-solver grants."""
    left, right = line.split("=", 1)
    if left.strip() == target and not re.search(r"[A-Za-z]", right):
        value, check_id = await run_tool(engine, turn, "math_evaluate", {"expression": right, "values": {}})
        exact = ((value or {}).get("exact") or {}).get("text")
        try:
            return ([Fraction(str(exact))] if exact is not None else []), check_id
        except (ValueError, ZeroDivisionError):
            return [], None
    value, check_id = await run_tool(engine, turn, "math_solve", {"equations": [line], "unknowns": [target]})
    return _solution_values(value, target), check_id


METHODS = ("substitution", "elimination", "divide_first", "expand_first", "givens_formula", "other_formula")
_FACTOR = re.compile(r"\d+(?:\.\d+)?\s*\*?\s*\(")  # a number times a bracket: `4(x - 2)`


def method_of(statement: str, plan: Plan, line: str, *, pair: bool, givens: set[str]) -> str | None:
    """The method a guided line accepted on its own maths shows (ASM-09), named so the tutor can say it (T3): one of
    `METHODS`, or None when the line is a step of the plan's own route, a stated value or a route with no plain name.
    Read from the line's shape only; the line was already checked by a tool."""
    plain = for_tool(line)
    compact = re.sub(r"\s+", "", plain)
    if compact in {re.sub(r"\s+", "", for_tool(t)) for t in [plan.final_latex, *(s.latex for s in plan.steps)]}:
        return None
    if re.fullmatch(r"[A-Za-z](?:_\w+)?=[-+]?[\d./]+", compact):
        return None  # a stated value is an answer, not a method
    if pair:
        return "substitution" if "(" in plain else "elimination"
    symbols = set(re.findall(r"[A-Za-z]", plain))
    if symbols and symbols <= givens:
        return "givens_formula"
    equation = next((part for part in re.split(r"(?<=[.?!])\s+", statement) if "=" in part), "")
    if _FACTOR.search(for_tool(equation)) and "(" not in plain:
        total, side = numbers_in(equation.rsplit("=", 1)[1]), numbers_in(plain.rsplit("=", 1)[1])
        if total and side and side[0] and total[0] != side[0] and (total[0] / side[0]).denominator == 1:
            return "divide_first"
        if total and side and total[0] == side[0]:
            return "expand_first"
        return None
    return "other_formula" if not equation else None


def assigns(plan: Plan, symbol: str) -> bool:
    """The plan states a value for this symbol (`v = 10` as a step or in the answer)."""
    return any(found == symbol for latex in [plan.final_latex, *(step.latex for step in plan.steps)]
               for found, _ in ASSIGNMENT.findall(for_tool(latex)))  # fmt: skip


def solved(plan: Plan, system: dict[str, Fraction], results: list[tuple[str, str]]) -> bool:
    """The working reaches the checked answer: no line is wrong and its last line, checked correct, states the answer;
    for simultaneous equations every unknown is stated in a correct line. A line no tool could read (a formula, a
    step in words, an unreadable line) is not wrong (ASM-09), so it does not stop a route from solving the problem."""
    if not results or results[-1][1] != "correct" or any(status == "incorrect" for _, status in results):
        return False
    if system:
        stated = {symbol: Fraction(number.replace(" ", ""))
                  for line, status in results if status == "correct"
                  for symbol, number in ASSIGNMENT.findall(for_tool(line))}  # fmt: skip
        return all(symbol in stated and _close(stated[symbol], value) for symbol, value in system.items())
    answer, last = numbers_in(plan.final_value), numbers_in(results[-1][0])
    return bool(answer and last and abs(last[-1] - answer[0]) <= max(abs(answer[0]), Fraction(1)) / 1000)


async def check_guided(engine: Any, turn: Any, text: str, plan: Plan) -> tuple[list[LineResult], int]:
    """Check explicit equations in a guided reply without asking a model to rederive them.

    Every component must pass independently. Symbolic formulas must match a planned relation; a numerical
    substitution with only the target unknown must solve to the checked answer. Prose and ambiguous input retain
    the specialist path. No numerical agreement can conceal an earlier wrong formula in the same reply.
    """
    lines = [s.strip() for s in re.split(r"\n|;|,\s*(?:so|then|hence|therefore)\s+", text, flags=re.I) if s.strip()]
    if not lines or len(lines) > 6 or any("=" not in s or worded(s) for s in lines):
        return [], 0
    # The solver accepts one relation at a time. Check every adjacent equality of a chain rather than sending
    # unsupported chained syntax and later accepting only its final number.
    expanded: list[str] = []
    for raw in lines:
        sides = [side.strip() for side in raw.split("=")]
        if len(sides) > 6 or any(not side for side in sides):
            return [], 0
        expanded.extend(f"{left} = {right}" for left, right in pairwise(sides))
    lines = expanded
    results = []
    reached = 0
    answer = numbers_in(plan.final_value)
    system = system_of(engine, plan)
    skill = engine._teaching_skill()
    section = re.search(r"(?ms)^## Reference relations\s*\n(.*?)(?=^## |\Z)", skill.body) if skill else None
    authored = re.findall(r"(?m)^- `([^`\n]{1,300})`\s*$", section[1])[:6] if section else []
    for raw in lines:
        line = for_tool(raw)
        parsed, _ = await run_tool(engine, turn, "math_parse", {"text": line})
        if not parsed or parsed.get("kind") != "relation":
            results.append(LineResult(raw, "unchecked", None))
            continue
        symbols = set(parsed.get("free_symbols", []))
        status, check_id = "unchecked", None
        if not symbols:
            left, right = line.split("=", 1)
            value, cited = await run_tool(
                engine, turn, "math_evaluate", {"expression": f"({left}) - ({right})", "values": {}}
            )
            exact = ((value or {}).get("exact") or {}).get("text")
            if exact is not None and cited:
                try:
                    status = "correct" if Fraction(str(exact)) == 0 else "incorrect"
                    check_id = cited
                except (ValueError, ZeroDivisionError):
                    pass
            results.append(LineResult(raw, status, check_id, "other" if status == "incorrect" else None))
            continue
        # Compare with planned equations, including differently arranged but equivalent formulas.
        references = (
            [(None, formula) for formula in authored]
            if authored and len(symbols) > 1
            else [(step.index, step.latex) for step in plan.steps]
        )
        for step_index, formula in references:
            reference = for_tool(formula)
            if reference.count("=") != 1 or worded(reference):
                continue
            ref, _ = await run_tool(engine, turn, "math_parse", {"text": reference})
            if not ref or set(ref.get("free_symbols", [])) != symbols:
                continue
            value, cited = await run_tool(engine, turn, "math_check_equivalence", {"a": line, "b": reference})
            if (value or {}).get("verdict") == "equivalent" and cited:
                status, check_id = "correct", cited
                reached = max(reached, step_index or 0)
                break
        given_symbols = {k["symbol"] for k in engine.state.board.knowns}
        if status == "unchecked" and system:  # simultaneous equations: any route's line, at the checked solution
            status, check_id, _ = await _check_in_system(engine, turn, line, system)
            results.append(LineResult(raw, status, check_id, "other" if status == "incorrect" else None))
            continue
        givens = givens_of(engine)
        units = {
            str(k.get("unit") or "").strip().replace("²", "^2")
            for k in engine.state.board.knowns
            if k["symbol"] in symbols
        }
        coherent = len(units) == 1 or (bool(units) and units <= COHERENT_UNITS)
        left, right = line.split("=", 1)
        numeric_substitution = bool(re.search(r"[A-Za-z]", left)) != bool(re.search(r"[A-Za-z]", right))
        if status == "unchecked" and symbols and symbols <= givens.keys() and coherent and numeric_substitution:
            # The middle of a chain can substitute only known quantities, e.g. (v-u)/t = 20/4. A plan's
            # final value alone cannot prove that substitution; every named quantity must be on Given.
            value, cited = await run_tool(
                engine,
                turn,
                "math_evaluate",
                {
                    "expression": f"({left}) - ({right})",
                    "values": {name: str(givens[name]) for name in symbols},
                },
            )
            exact = ((value or {}).get("exact") or {}).get("text")
            if exact is not None and cited:
                try:
                    status = "correct" if Fraction(str(exact)) == 0 else "incorrect"
                    check_id = cited
                except (ValueError, ZeroDivisionError):
                    pass
        target = next(iter(symbols)) if len(symbols) == 1 else None
        if status == "unchecked" and target and target not in given_symbols and answer:
            # A direct substitution needs arithmetic, not a subject-restricted algebra solver. In particular,
            # accounting permits math_evaluate but not math_solve. E=18000-7000 needs only the former.
            found, cited = await _goal_values(engine, turn, line, target)
            # A student may name the same unknown s while the hidden plan calls it h. Solve the student's
            # one-unknown equation itself; never rename a stated given or infer the value from a coefficient.
            if found and cited:
                status = (
                    "correct"
                    if len(found) == len(answer)
                    and all(
                        any(
                            abs(f - expected) <= Fraction(1, 10**9) * max(Fraction(1), abs(expected))
                            for expected in answer
                        )
                        for f in found
                    )
                    else "incorrect"
                )
                if status == "incorrect" and target != target_unknown(engine, plan):
                    # Another route's quantity (v on the way to s) or the unknown under another name: a value
                    # that is not the answer is not shown as wrong, it is left to the Checker's reading.
                    status = "unchecked"
                check_id = cited if status != "unchecked" else None
        results.append(LineResult(raw, status, check_id, "other" if status == "incorrect" else None))
    return results, reached


# --- practice ---------------------------------------------------------------------------------------------------


def board_check_id(build_check_id: str) -> str:
    """A pack's build check id as the board's check id pattern allows it (letters and digits only): a stable alias
    derived from the build id, so the same build check always shows as the same board check."""
    import hashlib

    return "chk_b" + hashlib.sha256(build_check_id.encode()).hexdigest()[:24]


def plan_from_item(item: dict[str, Any], part_id: str) -> tuple[Plan, dict[str, list[Fraction]]]:
    """The item's stored solution as the plan; its build-verified values become this session's check values."""
    part = next(p for p in item["parts"] if p["id"] == part_id)
    answer = part["answer"]
    values: dict[str, list[Fraction]] = {}
    steps = []
    for raw in (item.get("solution") or {}).get("steps", []):
        check_id = board_check_id(str(raw["check_id"])) if raw.get("check_id") else None
        latex = str(raw.get("latex", ""))
        value = None
        if check_id and raw.get("check_id") == answer.get("check_id") and answer.get("kind") == "numeric":
            value = str(answer["value"])
        elif check_id:
            # An operand or exponent at the end of an unevaluated expression is not the check's result.
            right = for_tool(latex.rsplit("=", 1)[-1])
            if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+|/\d+)?", right):
                value = right
        if check_id and value is not None:
            values.setdefault(check_id, []).append(Fraction(value))
        steps.append(PlanStep(int(raw["n"]), latex, value, None, check_id))
    final_check = board_check_id(str(answer["check_id"])) if answer.get("check_id") else ""
    if final_check and "value" in answer:  # a numeric key; structured and algebraic keys have no single value
        values.setdefault(final_check, []).append(Fraction(Decimal(str(answer["value"]))))
    final = answer.get("value") or answer.get("expression") or ", ".join(str(v) for v in answer.get("values", []))
    last = steps[-1].latex if steps else ""
    plan = Plan(steps, str(final or ""), answer.get("unit"), final_check, "the item's worked solution", last)
    return plan, values


def chat_answer(part: dict[str, Any], text: Any) -> Any:
    """Translate unambiguous typed choice labels and ordered lists to the marker's IDs.

    The assessment API already supplies typed IDs. Chat accepts the labels the student can see, without
    asking a model to interpret or grade them; unknown, repeated or ambiguous labels remain incorrect.
    """
    if not isinstance(text, str):
        return text
    key = part["answer"]

    def normal(value: str) -> str:
        return " ".join(value.strip().split())

    if key["kind"] == "mcq":
        matches = [
            o["id"]
            for o in key["options"]
            if normal(text) in (o["id"], normal(o["text"]), normal(o["id"] + ". " + o["text"]))
        ]
        return matches[0] if len(matches) == 1 else text
    if key["kind"] == "ordering":
        pieces = re.split(r"\s*(?:<|>|→|->|,|;)\s*", text.strip())
        result = []
        for piece in pieces:
            matches = [e["id"] for e in key["elements"] if normal(piece) in (e["id"], normal(e["text"]))]
            if len(matches) != 1:
                return []
            result.append(matches[0])
        return result
    return text


async def mark_answer(engine: Any, turn: Any, item: dict[str, Any], part_id: str, text: Any) -> Verdict:
    async def convert(quantity: str, to: str) -> Converted | None:
        value, check_id = await run_tool(engine, turn, "units_convert", {"quantity": quantity, "to": to})
        decimal = ((value or {}).get("value") or {}).get("decimal")
        if decimal is None or check_id is None:
            return None
        return Converted(Decimal(str(decimal)), check_id)

    async def run(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        return await run_tool(engine, turn, name, args)  # structured answers: chem_balance, acc_double_entry

    context = engine.deps.misconception_context(item) if engine.deps.misconception_context is not None else None
    part = next(p for p in item["parts"] if p["id"] == part_id)
    return await mark(item, part_id, chat_answer(part, text), convert, engine.deps.settings, run, signatures=context)


def authored_hint(item: dict[str, Any], part_id: str, rung: int) -> str | None:
    part = next(p for p in item["parts"] if p["id"] == part_id)
    hint = next((h for h in part.get("hints", []) if int(h.get("rung", 0)) == rung), None)
    text = (hint or {}).get("text") or {}
    return str(text.get("en")) if isinstance(text, dict) and text.get("en") else None
