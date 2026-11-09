"""Check explicit arithmetic in a completion explanation; never infer or grade an answer from prose."""

import ast
import hashlib
import re
from collections import Counter
from fractions import Fraction
from typing import Any, cast

from agent_core.engine import claims, reply_provenance
from agent_core.engine import slice as flows
from agent_core.engine.state import NUMBER, numbers_in


def _tree(expression: str) -> ast.Expression | None:
    if len(expression) > 160 or any(c not in claims.ARITH_SET for c in expression):
        return None
    try:
        normalized = expression.strip().replace("^", "**")
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return None
    nodes = list(ast.walk(tree))
    allowed = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.UAdd,
        ast.USub,
    )
    if len(nodes) > 80 or not all(isinstance(n, allowed) for n in nodes):
        return None
    for node in nodes:
        if isinstance(node, ast.Constant):
            literal = ast.get_source_segment(normalized, node)
            if literal is None:
                return None
            try:
                cast(Any, node).exact_literal = str(Fraction(literal))
            except ValueError:
                return None
    return tree


def _signature(node: ast.AST) -> str:
    def digest(value: Any) -> str:
        return hashlib.sha256(repr(value).encode()).hexdigest()

    if isinstance(node, ast.Constant):
        return digest(("number", str(cast(Any, node).exact_literal)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult | ast.Div):
        factors: Counter[str] = Counter()

        def collect(part: ast.AST, sign: int) -> None:
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Mult | ast.Div):
                collect(part.left, sign)
                collect(part.right, -sign if isinstance(part.op, ast.Div) else sign)
            else:
                factors[_signature(part)] += sign

        collect(node, 1)
        return digest(sorted((key, count) for key, count in factors.items() if count))
    if isinstance(node, ast.BinOp):
        return digest((type(node.op).__name__, _signature(node.left), _signature(node.right)))
    if isinstance(node, ast.UnaryOp):
        return digest((type(node.op).__name__, _signature(node.operand)))
    return digest(ast.dump(node, include_attributes=False))


def _part_of_checked_work(expression: str, engine: Any) -> bool:
    tree = _tree(expression)
    if tree is None:
        return False
    wanted = _signature(tree.body)
    state = engine.state
    sources = [
        s.latex
        for s in state.board.steps
        if s.status in ("correct", "equivalent_unsimplified") and s.check_id in state.check_values
    ][-32:]
    sources.extend(s.latex for s in state.plan.steps[:30] if s.check_id in state.check_values)
    for source in sources:
        for side in claims.RELATION.split(claims.plain(source)):
            parsed = _tree(side)
            if parsed and any(_signature(n) == wanted for n in ast.walk(parsed) if isinstance(n, ast.BinOp)):
                return True
    return False


def _source_percentage(expression: str, engine: Any) -> bool:
    """A simple question's sole stated percentage applied to its sole other stated number.

    This recognizes relevance, not an answer. Ambiguous multi-base/rate questions retain the checked-work route.
    The candidate expression is still evaluated by the worker and its asserted result still faces the truth gate.
    """
    statement = engine.state.problem.statement if engine.state.problem else ""
    rates = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:%|percent\b|per cent\b)", statement, re.I)
    if len(rates) != 1 or len(rates[0]) > 80:
        return False
    rate = Fraction(rates[0])
    given = set(numbers_in(statement))
    other = given - {rate}
    if len(other) != 1 or given != {rate, *other}:
        return False
    tree = _tree(expression)
    base = next(iter(other))
    original = next((m[0] for m in NUMBER.finditer(statement) if numbers_in(m[0]) == [base]), "")
    source = _tree(f"({rates[0]})/100*({original})")
    return bool(tree and source and _signature(tree.body) == _signature(source.body))


def approved_expression_problems(draft: dict[str, Any], engine: Any) -> list[str]:
    """A completion may quote checked work, but cannot approve a reconstructed, unproved expression.

    This narrow phase deliberately prefers an acknowledgement when an equivalent alternative has not been
    checked. Later explanations retain normal tool-led verification; no new expression becomes a board mark.
    """
    numeric = r"[0-9. +*/^()\-]{3,160}"
    patterns = [
        rf"\b(?:calculation|expression|formula|working)\s+`?(?:[A-Za-z]\s*=\s*)?({numeric})`?\s+"
        r"(?:is|was)\s+correct\b",
        rf"\bcorrectly\s+(?:used|calculated|evaluated)\s+`?(?:[A-Za-z]\s*=\s*)?({numeric})",
    ]
    errors = []
    for name in ("say", "say_spoken"):
        text = claims.plain(str(draft.get(name) or ""))
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                expression = match[1].strip()
                tree = _tree(expression)
                if tree and isinstance(tree.body, ast.BinOp) and not _part_of_checked_work(expression, engine):
                    errors.append(f"{name}: that reconstructed expression is not the checked working; "
                                  "quote the checked calculation accurately or confirm it without redoing arithmetic")
    return errors


async def completed_arithmetic(engine: Any, turn: Any, draft: dict[str, Any]) -> None:
    """Only after the student's final answer is checked, give explicit, source-grounded calculations real receipts.

    Literal assertions, unknown operands, unit-bearing new results and implicit word-problem reasoning are not
    admitted. All ordinary truth, unit, numeric-provenance, verdict, leak and safety gates still run afterwards.
    """
    if not (engine.state.solved and turn.attempt_verdict == "correct" and engine.state.plan is not None):
        return
    texts = [str(draft.get(k) or "") for k in ("say", "say_spoken")]
    candidates = []
    for text in texts:
        candidates.extend(claims.percent_claims(text))
        candidates.extend(
            (left, right, tuple(str(n) for n in numbers_in(left))) for left, right in claims.arithmetic_claims(text)
        )
    seen: set[str] = set()
    for expression, asserted, _inputs in candidates:
        if engine.cancelled.is_set() or len(seen) >= 4:
            break
        if expression in seen or len(expression) > 160 or not re.search(r"[+*/^-]", expression.lstrip("-")):
            continue
        if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", asserted) or len(asserted) > 80:
            continue
        wanted = claims.evaluate(asserted)
        provenance = engine.state.provenance()
        if wanted is None or wanted in provenance:
            continue
        if not (_part_of_checked_work(expression, engine) or _source_percentage(expression, engine)):
            continue  # true arithmetic with the givens is not necessarily part of the student's solution
        if any(q.value == wanted and q.unit for text in texts for q in claims.quantities(text)):
            continue  # a scalar calculation is not evidence for a physical-unit assignment
        seen.add(expression)
        await flows.run_tool(engine, turn, "math_evaluate", {"expression": expression})
        # Numeric provenance parses a written fraction as one value. Give those operands their own real
        # receipts too (e.g. 10/100 in a source-grounded percentage), within the same four-check budget.
        for match in NUMBER.finditer(expression):
            operand = match[0]
            if len(seen) >= 4 or engine.cancelled.is_set():
                break
            if "/" in operand and operand not in seen and any(
                value not in engine.state.provenance() for value in numbers_in(operand)
            ):
                seen.add(operand)
                await flows.run_tool(engine, turn, "math_evaluate", {"expression": operand})
        # The worker records its actual result, never the asserted number. A false assertion still fails
        # claim_problems even if that number already has unrelated provenance.
    draft["numbers_from"] = reply_provenance.references(draft, engine.state)
