"""Bounded percentage conversions from the current question, computed by the real worker.

These are numeric evidence, not a word-problem solution or an exemption from truth, unit or withholding gates.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Any

from agent_core.engine import claims, reply_provenance
from agent_core.engine import slice as flows
from agent_core.engine.spoken import to_written
from agent_core.engine.state import numbers_in

RATE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", re.I)


def source(state: Any) -> tuple[str, int] | None:
    if state.problem is None or state.plan is None:
        return None
    if not any(s.check_id in state.check_values for s in state.plan.steps if s.check_id):
        return None
    statement = state.problem.statement
    rates = list(RATE.finditer(statement))
    if len(rates) != 1 or len(rates[0][1]) > 40:
        return None
    # Ambiguous or negated directions do not establish an increase/decrease multiplier.
    up = re.search(r"\b(?:increas(?:e|es|ed|ing)|rise|rises|rising|rose|rais(?:e|es|ed|ing)|growth)\b", statement, re.I)
    down = re.search(r"\b(?:decreas\w*|reduc\w*|discount\w*|fall\w*)\b", statement, re.I)
    negated = re.search(r"\b(?:not|never|doesn't|isn't|don't)\b", statement, re.I)
    direction = 0 if negated or bool(up) == bool(down) else 1 if up else -1
    return rates[0][1], direction


def derived(state: Any) -> set[Fraction]:
    found = source(state)
    if found is None:
        return set()
    rate, direction = found
    fraction = Fraction(rate) / 100
    return {Fraction(100), fraction, *({1 + direction * fraction, 100 + direction * Fraction(rate)}
                                     if direction else set())}


def problems(draft: dict[str, Any], state: Any) -> list[str]:
    """A percentage conversion attests a scalar, never a new mass, length or other physical-unit assignment."""
    expected = derived(state)
    if not expected:
        return []
    known = claims.Sources.of(state).units
    rate, direction = source(state) or ("0", 0)
    allowed_rates = {Fraction(rate), Fraction(100)}
    if direction:
        allowed_rates.add(100 + direction * Fraction(rate))
    if state.plan:
        for step in state.plan.steps:
            if step.check_id in state.check_values and step.unit in ("%", "percent"):
                allowed_rates.update(numbers_in(str(step.value or "")))
    errors = []
    for name in ("say", "say_spoken"):
        text = to_written(str(draft.get(name) or ""))
        # An explicitly paired operation and percent change is an arithmetic claim, even without '='.
        # Checking its truth creates no value, receipt, learner verdict or new word-problem relation.
        equivalence = re.compile(
            r"\b(multiply|divide)\s+(?:by\s+)?(\d+(?:\.\d+)?)\s*"
            r"(?:\(\s*|to\s+)(add|subtract|increase\s+by|decrease\s+by)\s+"
            r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", re.I
        )
        for claim in equivalence.finditer(text):
            if re.search(r"\b(?:not|never|don't)\s*$", text[:claim.start()], re.I):
                continue
            operation, factor, change, percent = (s.lower() for s in claim.groups())
            if max(len(factor), len(percent)) > 40:
                continue
            actual = claims.evaluate(f"1*({factor})" if operation == "multiply" else f"1/({factor})")
            expected_factor = claims.evaluate(f"1{'-' if change.startswith(('subtract', 'decrease')) else '+'}"
                                              f"({percent})/100")
            if actual is None or expected_factor is None or not claims._close(actual, expected_factor):
                errors.append(f"{name}: the factor operation is not equivalent to that percent change; "
                              "use the current question's verified multiplier or ask about the relation")
        for rule in re.finditer(r"\b(?:divid\w*|division)\s*\(?\s*(?:if|when|for)\b[^.!?]{0,48}"
                                r"\b(?:decreas\w*|reduc\w*|discount\w*)\b", text, re.I):
            if not re.search(r"\b(?:not|never|don't|avoid)\s*$", text[:rule.start()], re.I):
                errors.append(f"{name}: do not teach divide-if-decreasing as a percentage rule; "
                              "explain the factor or relation for this question, without an unsupported general rule")
        given_base = bool(state.problem and Fraction(100) in numbers_in(state.problem.statement))
        reached_base = any(Fraction(100) in numbers_in(s.latex) for s in state.board.steps if s.owner == "student")
        plan_base = any(s.check_id in state.check_values and Fraction(100) in numbers_in(str(s.value or ""))
                        for s in state.plan.steps)
        if Fraction(100) in numbers_in(text) and not (given_base or reached_base or plan_base or _definition(text)):
            errors.append(f"{name}: the percent-base receipt supports its definition, not an unrelated result of 100")
        for sentence in re.split(r"(?<=[!?])|(?<=\.)(?!\d)", text):
            for match in RATE.finditer(sentence):
                if Fraction(match[1]) in allowed_rates:
                    continue
                contrast = (sentence.endswith("?") and re.search(r"\b(?:or|rather|instead|why)\b", sentence, re.I))
                reflection = re.search(r"\byou (?:wrote|used|chose)\b", sentence, re.I) and re.search(
                    r"\b(?:instead|not|wrong|incorrect)\b", sentence, re.I)
                if not (contrast or reflection):
                    errors.append(f"{name}: the source percentage is {rate}%; do not substitute a different rate")
        goal = flows.requested_unknown(state.problem.statement)
        if goal:
            label = re.search(rf"\buse\s+{re.escape(goal)}\s+for\s+(?:the\s+)?([^.!?;\n]+)",
                              state.problem.statement, re.I)
            if label and re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,59}", label[1]):
                pattern = (rf"\b{re.escape(label[1].strip())}\s+(?:is|was|equals|=)\s+"
                           r"([-+]?\d+(?:\.\d+)?(?:/\d+)?(?:[eE][-+]?\d+)?)"
                           r"(?![A-Za-z0-9_.])(?!\s*(?:%|percent\b))")
                expected_goal = claims.evaluate(state.plan.final_value)
                for match in re.finditer(pattern, text, re.I):
                    value = claims.evaluate(match[1]) if len(match[1]) <= 80 else None
                    if value is not None and expected_goal is not None and not claims._close(value, expected_goal):
                        errors.append(f"{name}: the requested quantity must agree with the checked final working, "
                                      "not a percentage-base conversion")
        for quantity in claims.quantities(text):
            if quantity.value not in expected or quantity.unit is None:
                continue
            if quantity.value in allowed_rates and quantity.unit == "%":
                continue
            if not any(q.value == quantity.value and q.unit == quantity.unit for q in known):
                errors.append(f"{name}: a percentage conversion does not establish {quantity.value} {quantity.unit}")
        found = source(state)
        if found and found[1]:
            expected_multiplier = 1 + found[1] * Fraction(found[0]) / 100
            # An amount/increase and a multiplicative factor are distinct quantities, even if all digits trace.
            atom = r"\d+(?:\.\d+)?\s*%?"
            pattern = rf"({atom}(?:\s*[+*/-]\s*{atom}){{0,7}})\s+(?:as|is)\s+(?:(?:a|the)\s+)?multiplier\b"
            for match in re.finditer(pattern, text, re.I):
                expression = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", match[1])
                value = claims.evaluate(expression)
                if value is not None and value != expected_multiplier:
                    errors.append(f"{name}: the percentage amount is not the new-amount multiplier; "
                                  "confirm the checked method without misnaming these quantities")
            amount_label = (r"\b(?:calculated|found|computed|obtained)\s+(?:the\s+)?"
                            r"(?:change|increase|decrease|discount)\s+(?:using|with)\s+(?:the\s+)?multiplier\s+"
                            rf"({atom}(?:\s*[+*/-]\s*{atom}){{0,7}})")
            for match in re.finditer(amount_label, text, re.I):
                expression = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", match[1])
                value = claims.evaluate(expression)
                if value == expected_multiplier and value != Fraction(found[0]) / 100:
                    errors.append(f"{name}: that multiplier gives the final amount, not just the change; "
                                  "acknowledge the requested quantity accurately")
    return errors


def _definition(text: str) -> bool:
    """Every written 100 must be a percent denominator, not an unrelated example/result with the same value."""
    occurrences = list(re.finditer(r"(?<![\w.])100(?:\.0+)?(?!\w|\.\d)", text))
    return bool(occurrences) and all(
        re.match(r"\s*(?:%|percent\b)", text[m.end():], re.I)
        or (re.search(r"(?:\bper|\bout of|\bover|/)\s*$", text[:m.start()], re.I)
            and re.search(r"\bpercent(?:age)?s?\b|%",
                          text[max(0, text.rfind('.', 0, m.start()) + 1):m.start()], re.I))
        for m in occurrences
    )


async def verify(engine: Any, turn: Any, draft: dict[str, Any]) -> None:
    found = source(engine.state)
    if found is None or problems(draft, engine.state) or engine.cancelled.is_set():
        return
    rate, direction = found
    texts = [to_written(str(draft.get(k) or "")) for k in ("say", "say_spoken")]
    missing = {v for text in texts for v in numbers_in(text)} - engine.state.provenance()
    fraction = Fraction(rate) / 100
    multiplier = 1 + direction * fraction
    total = 100 + direction * Fraction(rate)
    if fraction in missing or (direction and multiplier in missing):
        value, check = await flows.run_tool(
            engine, turn, "units_convert", {"quantity": f"{rate} percent", "to": "dimensionless"}
        )
        exact = ((value or {}).get("value") or {}).get("text")
        if check and exact and fraction in engine.state.check_values.get(check, ()) and not engine.cancelled.is_set():
            if direction and multiplier in missing:
                await flows.run_tool(engine, turn, "math_evaluate", {"expression": f"1{'+-'[direction < 0]}({exact})"})
    base_needed = Fraction(100) in missing and all(_definition(t) for t in texts if Fraction(100) in numbers_in(t))
    if base_needed or (direction and total in missing):
        if not engine.cancelled.is_set():
            # Pint's actual dimensionless -> percent conversion supplies the base. Never evaluate the literal 100.
            value, check = await flows.run_tool(engine, turn, "units_convert", {"quantity": "1", "to": "percent"})
            base = ((value or {}).get("value") or {}).get("text")
            if (direction and total in missing and check and base and not engine.cancelled.is_set()
                    and Fraction(100) in engine.state.check_values.get(check, ())):
                await flows.run_tool(engine, turn, "math_evaluate",
                                     {"expression": f"({base}){'+-'[direction < 0]}({rate})"})
    draft["numbers_from"] = reply_provenance.references(draft, engine.state)
