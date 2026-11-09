"""Validate eligible explicit totals against an authored relation, using the real worker.

This neither solves the word problem on the host nor turns inferred labels into givens. Ambiguous, mixed-unit
or transactional questions do not bind. General modelling and independent curriculum approval remain separate.
"""

import ast
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from agent_core.assets.relations import Relation, parse
from agent_core.engine import slice as flows
from agent_core.engine.state import Plan

AMOUNT = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
CURRENCY_UNITS = {"$": "usd", "dollar": "usd", "dollars": "usd", "usd": "usd",
                  "£": "gbp", "pound": "gbp", "pounds": "gbp", "gbp": "gbp",
                  "€": "eur", "euro": "eur", "euros": "eur", "eur": "eur"}


def currencies(text: str) -> set[str]:
    return {CURRENCY_UNITS[m[0]] for m in re.finditer(
        r"[$£€]|\b(?:usd|gbp|eur|dollars?|pounds?|euros?)\b", text.lower()
    )}


@dataclass(frozen=True)
class BoundRelation:
    rule: Relation
    values: dict[str, str]
    target: str
    skill_name: str
    skill_sha256: str
    currency: str | None = None

    def instruction(self) -> str:
        return (f"Applicable curriculum relation ({self.skill_name}): {self.rule.left} = {self.rule.right}. "
                f"The requested quantity is {self.target}. Preserve this relation when choosing arithmetic. "
                "Source-bound inputs: " + "; ".join(f"{k}={v}" for k, v in self.values.items()) + ". "
                "These input names are available in expressions; the requested quantity is not an input.")


def bind(rule: Relation, statement: str) -> tuple[dict[str, str], str] | None:
    text = statement.lower().replace("\u2019", "'")
    if len(text) > 4000 or any(re.search(rf"\b{re.escape(t)}\b", text) for t in rule.exclude):
        return None
    # No currency conversion or aggregation is inferred. The source must provide exactly the other totals.
    if len(currencies(text)) > 1:
        return None
    values: dict[str, str] = {}
    spans: set[tuple[int, int]] = set()
    for name, aliases in rule.quantities.items():
        matches: dict[tuple[int, int], str] = {}
        for alias in aliases:
            pattern = (rf"\b{re.escape(alias)}\b\s*(?:(?:are|is|of|were|was|=|:)\s*)?"
                       rf"[$£€]?\s*({AMOUNT})(?!\w|\.\d)")
            for match in re.finditer(pattern, text):
                if len(match[1]) > 40:
                    return None
                suffix = re.match(r"\s*([a-z/%]+)", text[match.end(1):])
                if suffix and suffix[1] not in {
                    *CURRENCY_UNITS, "currency", "unit", "units", "and", "find", "calculate", "determine", "use"
                }:
                    return None
                matches[match.span(1)] = str(Fraction(match[1].replace(",", "")))
        if len(matches) > 1:
            return None
        if matches:
            span, value = next(iter(matches.items()))
            spans.add(span)
            values[name] = value
    remaining = set(rule.quantities) - values.keys()
    if len(remaining) != 1 or len(values) != len(rule.quantities) - 1:
        return None
    # Extra amounts (including a transaction, period, or missing aggregation) keep ordinary planning.
    if {m.span() for m in re.finditer(AMOUNT, text)} != spans:
        return None
    target = next(iter(remaining))
    asks = re.findall(r"\b(?:find|calculate|determine|what is|what are)\s+([^.!?;\n]+)", text)
    notation = flows.requested_unknown(statement)
    if notation:
        asks += re.findall(rf"\b(?:use|let)\s+{re.escape(notation.lower())}\s+for\s+([^.!?;\n]+)", text)
    if not any(re.search(rf"\b{re.escape(alias)}\b", asked) for asked in asks
               for alias in rule.quantities[target]):
        return None
    return values, target


def select(engine: Any) -> BoundRelation | None:
    state = engine.state
    if state.problem is None or engine._input_source != "student_text":
        return None
    found = []
    for skill in engine.deps.assets.skills.loadable("solver"):
        if skill.meta.get("subject") != state.subject or state.level not in skill.meta.get("levels", []):
            continue
        rule = parse(skill.body)
        matched = bind(rule, state.problem.statement) if rule else None
        if matched and rule:
            values, target = matched
            found.append(BoundRelation(rule, values, target, skill.name, skill.sha256,
                                       next(iter(currencies(state.problem.statement)), None)))
    return found[0] if len(found) == 1 else None


def _linear(side: str) -> dict[str, int] | None:
    """Signed quantity names only; symbolic relation comparison computes no learner result."""
    if len(side) > 160:
        return None
    try:
        tree = ast.parse(side, mode="eval")
    except SyntaxError:
        return None

    def visit(node: ast.AST, sign: int, out: dict[str, int]) -> bool:
        if isinstance(node, ast.Name):
            out[node.id] = out.get(node.id, 0) + sign
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
            return visit(node.left, sign, out) and visit(
                node.right, -sign if isinstance(node.op, ast.Sub) else sign, out
            )
        return False

    out: dict[str, int] = {}
    return out if visit(tree.body, 1, out) else None


def coaching_problems(draft: dict[str, Any], relation: BoundRelation) -> list[str]:
    """Check explicit linear quantity equalities, including false premises inside 'how' questions.

    Only the uniquely source-bound authored relation is used. Yes/no comparisons, hypotheticals and explicit
    negation remain questions rather than asserted facts. This is deliberately not a general prose verifier.
    """
    def equation(left: str, right: str) -> tuple[tuple[str, int], ...] | None:
        lhs, rhs = _linear(left), _linear(right)
        if lhs is None or rhs is None:
            return None
        for key, value in rhs.items():
            lhs[key] = lhs.get(key, 0) - value
        nonzero = sorted((key, value) for key, value in lhs.items() if value)
        sign = -1 if nonzero and nonzero[0][1] < 0 else 1
        return tuple((key, sign * value) for key, value in nonzero)

    expected = equation(relation.rule.left, relation.rule.right)
    if expected is None:
        return []
    aliases = {alias: name for name, terms in relation.rule.quantities.items() for alias in terms}
    alias = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + r")\b")
    quantity = "(?:" + "|".join(re.escape(n) for n in relation.rule.quantities) + ")"
    side = rf"{quantity}(?:\s*[+-]\s*{quantity}){{0,7}}"
    pattern = re.compile(rf"\b({side})\s*(?:equals?|=|is)\s*({side})\b")
    errors = []
    for surface in ("say", "say_spoken"):
        text = str(draft.get(surface) or "").lower().replace("\u2019", "'")
        for sentence in re.split(r"(?<=[!?])|(?<=\.)(?!\d)", text):
            if re.search(r"\b(?:if|unless|suppose|imagine|could|would|might|whether|not|wrong|incorrect|false)\b",
                         sentence) or re.match(r"\s*(?:does|do|is|are|can|why)\b", sentence):
                continue
            normalized = alias.sub(lambda m: aliases[m[0]], sentence)
            normalized = re.sub(r"\bplus\b", "+", normalized)
            normalized = re.sub(r"\bminus\b", "-", normalized)
            for found in pattern.finditer(normalized):
                actual = equation(found[1], found[2])
                if actual is not None and actual != expected:
                    errors.append(f"{surface}: that asserted quantity equality contradicts the source-bound "
                                  f"curriculum relation {relation.rule.left} = {relation.rule.right}; "
                                  "ask how the quantities relate without a false premise")
    return errors


async def verify(engine: Any, turn: Any, plan: Plan, relation: BoundRelation) -> str | None:
    unit = (plan.final_unit or "").strip().lower()
    if unit not in ("", "unit", "units", "currency", "currency unit", "currency units") and (
        CURRENCY_UNITS.get(unit) is None or CURRENCY_UNITS[unit] != relation.currency
    ):
        return "The computed total changes the source currency or uses a non-financial unit. Preserve the source unit."
    value = plan.final_value
    if len(value) > 80 or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:/\d+)?(?:[eE][-+]?\d{1,2})?", value):
        return "the curriculum relation needs a checked scalar final value"
    values = {**relation.values, relation.target: value}

    def expand(side: str) -> str:
        return re.sub(r"[a-z][a-z_]*", lambda m: f"({values[m[0]]})", side)

    _, check = await flows.run_tool(engine, turn, "math_evaluate", {
        "expression": f"({expand(relation.rule.left)})-({expand(relation.rule.right)})"
    })
    produced = engine.state.check_values.get(check or "", [])
    valid = bool(check and produced and all(v == Fraction(0) for v in produced))
    engine._log("info", "engine", "plan.curriculum_relation", skill=relation.skill_name,
                skill_sha256=relation.skill_sha256, passed=valid, check_id=check)
    if valid:
        return None
    return "The computed final value does not satisfy the applicable source-bound curriculum relation. " + (
        relation.instruction()
    )
