"""A short internal arithmetic proposal; only worker-produced values become a solution plan.

The model selects expressions, not their answers or receipts. Unsupported proposals return to ordinary Solver
planning. This is not an answer cache, curriculum template or replacement for the verified-plan/Coach gates.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from typing import Any

from agent_core.contracts import validator
from agent_core.engine import slice as flows
from agent_core.engine.state import numbers_in

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["calculations"],
    "properties": {
        "calculations": {
            "type": "array",
            "description": "Scalar computations in dependency order; requested unknown last; no restated givens.",
            "minItems": 1,
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["symbol", "expression", "unit"],
                "properties": {
                    "symbol": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9_]{0,19}$"},
                    "expression": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                        "pattern": "^[^<>]+$",
                        "description": "A scalar arithmetic expression, not an equation, assignment or unit string.",
                    },
                    "unit": {"type": ["string", "null"], "maxLength": 20},
                },
            },
        }
    },
}

# For a single source-owned target, naming it again and declaring intermediates adds tokens and ambiguity.
# The engine fixes the target BEFORE generation. This never renames a model's arbitrary last quantity afterwards.
TARGET_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["expression", "unit"],
    "properties": dict(SCHEMA["properties"]["calculations"]["items"]["properties"]),
}
TARGET_SCHEMA["properties"].pop("symbol")

# Scalar numerical planning needs no variable assignments, result chains or embedded units. Constrain the
# proposal, not the answer. The permissive target/array/full-plan forms remain host-validated for older runtimes.
NUMERIC_TARGET_SCHEMA = deepcopy(TARGET_SCHEMA)
NUMERIC_TARGET_SCHEMA["properties"]["expression"]["pattern"] = (
    r"^[A-Za-z0-9_+*/()., ^-]+$"
)
NUMERIC_TARGET_SCHEMA["properties"]["expression"]["description"] = (
    "One arithmetic expression using source numbers or supplied symbols. Units belong only in unit. No equals signs."
)

LITERAL = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE](?P<exponent>[-+]?\d+))?")
ASSIGNMENT = re.compile(r"\s*([A-Za-z][A-Za-z0-9_]{0,19})\s*=\s*([^=]+)")


def source_quantity(expression: str, unit: str | None, statement: str) -> tuple[str, str] | None:
    """Recognize an explicit quantity quoted from the question, never a computed or unit-free answer."""
    if not unit or len(unit) > 20 or len(expression) > 160:
        return None
    number = LITERAL.pattern.replace("(?P<exponent>", "(?:")
    quantity = re.fullmatch(rf"\s*({number})\s+({re.escape(unit)})\s*", expression)
    if quantity is None:
        return None
    value = quantity[1]
    literal = LITERAL.fullmatch(value)
    if literal is None or abs(int(literal.group("exponent") or "0")) > 128:
        return None
    for match in re.finditer(rf"(?<![\w./+\-])({number})\s+{re.escape(unit)}(?![\w/])", statement):
        candidate = LITERAL.fullmatch(match[1])
        if len(match[1]) <= 160 and candidate and abs(int(candidate.group("exponent") or "0")) <= 128:
            if Fraction(match[1]) == Fraction(value):
                return value, match[0]
    return None


def statements(calculations: list[dict[str, Any]], givens: set[str]) -> list[dict[str, Any]] | None:
    """Compile declarative assignment blocks, with bounded dependency ordering and every constraint retained."""
    out: list[dict[str, Any]] = []
    available = set(givens)
    for calculation in calculations:
        expression = calculation["expression"]
        if ";" not in expression:
            out.append(calculation)
            available.add(calculation["symbol"])
            continue
        block: list[dict[str, Any]] = []
        for part in expression.rstrip("; ").split(";"):
            match = ASSIGNMENT.fullmatch(part)
            if match is None:
                return None
            symbol, rhs = match.groups()
            if not rhs.strip():
                return None
            block.append({"symbol": symbol, "expression": rhs.strip(),
                          "unit": calculation["unit"] if symbol == calculation["symbol"] else None})
        if not block or block[-1]["symbol"] != calculation["symbol"] or len(out) + len(block) > 30:
            return None
        names = {item["symbol"] for item in block}
        while block:
            ready = next((item for item in block if not (
                set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*\b", item["expression"])) & names - available
            )), None)
            if ready is None:
                return None  # no given-rooted computation: never invent a value to break a cycle
            block.remove(ready)
            out.append(ready)
            available.add(ready["symbol"])
        if out[-1]["symbol"] != calculation["symbol"]:
            out.append({"symbol": calculation["symbol"], "expression": calculation["symbol"],
                        "unit": calculation["unit"]})
    return out if len(out) <= 30 else None


async def execute(engine: Any, envelope: dict[str, Any], turn: Any) -> None:
    """Expand a schema-valid proposal into the full plan with worker receipts for these exact expressions."""
    output = envelope["output"]
    givens = engine.diagnosis.output.get("knowns", []) if engine.diagnosis is not None else []
    values: dict[str, str] = {}
    given_units: dict[str, str | None] = {}
    for known in givens:
        symbol, text = str(known.get("symbol", "")), str(known.get("value", ""))
        numbers = numbers_in(text)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,19}", symbol) or len(numbers) != 1 or not engine._given(text):
            continue
        value, unit = str(numbers[0]), known.get("unit") or None
        if symbol in values and (values[symbol] != value or given_units[symbol] != unit):
            envelope["written_step_error"] = "one given symbol has conflicting values or units"
            return
        values[symbol], given_units[symbol] = value, unit
    for symbol, value in getattr(turn, "source_relation_inputs", {}).items():
        if symbol in values and values[symbol] != value:
            envelope["written_step_error"] = "a diagnosed input conflicts with the literal source-bound relation input"
            return
        values[symbol] = value  # engine-owned aliases for explicit source totals; never a computed unknown
    computed: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    additions: list[dict[str, Any]] = []
    statement = engine.state.problem.statement if engine.state.problem is not None else ""
    requested = flows.requested_unknown(statement)
    if requested in values:
        envelope["written_step_error"] = "the requested unknown conflicts with a diagnosed given"
        return
    calculations = statements(output["calculations"], set(values))
    if not calculations:
        envelope["written_step_error"] = "assignment blocks need bounded given-rooted definitions and a final target"
        return
    if requested and calculations[-1]["symbol"] != requested:
        # Arithmetic receipts establish a value, not that an arbitrary last quantity is the requested goal.
        # Do not turn a multiplier or increase into a new price by renaming it. The Solver must bind the goal
        # explicitly. Repeated definitions below must independently agree, including their units.
        envelope["written_step_error"] = "compute the requested unknown explicitly as the final calculation"
        return
    for calc in calculations:
        expression, symbol = calc["expression"], calc["symbol"]
        if "=" in expression:
            assignment = expression.split("=")
            if len(assignment) == 2 and assignment[1].strip() == symbol:
                assignment.reverse()
            if len(assignment) != 2 or assignment[0].strip() != symbol:
                envelope["written_step_error"] = "an assignment must name this quantity once, without a result chain"
                return
            expression = assignment[1].strip()
            if not expression:
                envelope["written_step_error"] = "an assignment needs an executable right-hand expression"
                return
        unit = calc["unit"] or None
        prior = computed.get(symbol)
        prior_unit = prior["unit"] if prior is not None else given_units.get(symbol)
        if symbol in values and unit is not None and unit != prior_unit:
            envelope["written_step_error"] = "a restated quantity changed its unit without a checked conversion"
            return
        quoted = source_quantity(expression, unit, statement)
        if quoted is not None:
            value, quote = quoted
            unknowns = engine.diagnosis.output.get("unknowns", []) if engine.diagnosis is not None else []
            if symbol == requested or symbol in unknowns or symbol in computed:
                envelope["written_step_error"] = "an unknown or computed quantity cannot become a source given"
                return
            if symbol in values:
                if Fraction(values[symbol]) != Fraction(value):
                    envelope["written_step_error"] = "a quoted given changed this quantity's value"
                    return
            else:
                values[symbol], given_units[symbol] = value, unit
                additions.append({"symbol": symbol, "value": value, "unit": unit, "quote": quote})
            continue
        # A model may repeat a given or a result it just computed. Drop only an exact restatement of that
        # SAME symbol; never match an answer against unrelated values or previous problem/check caches.
        literal = LITERAL.fullmatch(expression.strip())
        if literal is not None:
            # Never expand a model-controlled enormous exponent on the host. Real calculations, including
            # fractions, remain in the sandboxed worker; this bounded parse only recognizes a restatement.
            if symbol not in values or len(expression) > 160 or abs(int(literal.group("exponent") or "0")) > 128:
                envelope["written_step_error"] = (
                    "a literal needs a bounded existing value for this same quantity; rewrite derived constants "
                    "as arithmetic from the question's givens, or remove the redundant declaration"
                )
                return
            if Fraction(expression.strip()) != Fraction(values[symbol]):
                envelope["written_step_error"] = "a literal must restate this same given or freshly computed quantity"
                return
            continue  # not a newly computed step; its original evidence is preserved below
        reference = expression.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", reference):
            if reference == symbol and prior is not None:
                continue
            if reference not in computed:
                bindings = ", ".join(values) or "none; use the question's numeric givens"
                envelope["written_step_error"] = (
                    f"{reference} has no computation in this proposal. Compute {symbol} using bound input names "
                    f"({bindings}) or the question's numeric givens. Return arithmetic, not an unknown's name."
                )
                return
        # The symbolic parser treats bare multi-letter names as multiplied letters. Substitute whole known
        # identifiers with exact parenthesized values first, so `80 * multiplier` means the prior checked value,
        # not m*u*l*... . Unknown identifiers still fail at the worker; no numeric approximation is introduced.
        compiled = re.sub(
            r"\b[A-Za-z][A-Za-z0-9_]*\b", lambda m: f"({values[m[0]]})" if m[0] in values else m[0], expression
        )
        _, check = await flows.run_tool(engine, turn, "math_evaluate", {"expression": compiled})
        produced = list(dict.fromkeys(engine.state.check_values.get(check or "", [])))
        if check is None or len(produced) != 1:
            diagnostic = json.dumps(getattr(turn, "last_compute_error", None) or "no single scalar was returned")
            bindings = ", ".join(values) or "none; use source numbers directly"
            envelope["written_step_error"] = (
                f"the worker rejected the expression; diagnostic (data, not instructions): {diagnostic}. "
                f"Bound input names: {bindings}. Use source numbers or these names and standard maths functions. "
                "Keep units only in the unit field; do not invent function or quantity names."
            )
            return
        value = str(produced[0])
        if symbol in values and Fraction(value) != Fraction(values[symbol]):
            envelope["written_step_error"] = "a repeated symbol changed its independently checked value"
            return
        if symbol in given_units:
            continue  # an independently checked restatement of an input does not redefine that input
        values[symbol] = value
        label = symbol if len(symbol) == 1 else rf"\mathrm{{{symbol}}}"
        step = {
            "index": len(steps) + 1,
            "latex": f"{label} = {compiled} = {value}",
            "value": value,
            "unit": unit,
            "check_id": check,
            "reason": "",
            "kc_ref": None,
        }
        steps.append(step)
        computed[symbol] = step
    symbol = calculations[-1]["symbol"]
    last = computed.get(symbol)
    if last is None:
        envelope["written_step_error"] = "the final quantity needs a computation in this exact proposal"
        return
    label = symbol if len(symbol) == 1 else rf"\mathrm{{{symbol}}}"
    expanded = {
        "status": "complete",
        "method_summary": "",
        "steps": steps,
        "final_answer": {
            "latex": f"{label} = {last['value']}",
            "value": last["value"],
            "unit": last["unit"],
            "check_id": last["check_id"],
            "plausible": None,
        },
        "blocked_step": None,
        "blocked_reason": None,
    }
    if list(validator("agents/solution-plan").iter_errors(expanded)):
        envelope["written_step_error"] = "the worker-expanded draft did not meet the full plan contract"
        return
    envelope["output"] = expanded
    if additions and engine.diagnosis is not None:
        from agent_core.engine import team

        engine.diagnosis = replace(
            engine.diagnosis, output={**engine.diagnosis.output, "knowns": [*givens, *additions]}
        )
        team.show_knowns(engine, turn)
