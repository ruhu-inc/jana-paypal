"""Deterministic Studio experiments using the production signature matcher and worker checks."""

from __future__ import annotations

import ast
import copy
import itertools
import math
import operator
from collections.abc import Awaitable, Callable
from decimal import Decimal
from string import Formatter
from typing import Any

from agent_core.config import Settings
from agent_core.engine import signatures
from agent_core.engine.marking import Converted, evaluate_expression
from tool_worker.protocol import WorkerError

from pack_builder import quantities
from pack_builder.verify import _figure, run_check


async def signature_bench(
    items: list[dict[str, Any]], library: list[dict[str, Any]], conventions: dict[str, str], selected: str
) -> dict[str, Any]:
    chosen = next(m for m in library if m["id"] == selected)
    rows, collisions = [], []
    for original in items:
        if not set(original["kc_ids"]) & set(chosen["kc_ids"]):
            continue
        item = copy.deepcopy(original)
        # Bind quantities from the item's checked computation, never from a model guess.
        if not item.get("quantities"):
            for part in item["parts"]:
                part["misconception_ids"] = sorted(
                    set(part.get("misconception_ids", []))
                    | {m["id"] for m in library if set(item["kc_ids"]) & set(m["kc_ids"])}
                )
            item["quantities"] = quantities.extract(item, {m["id"]: m for m in library}).quantities
        checks: list[dict[str, Any]] = []

        async def run(
            tool: str, args: dict[str, Any], checks: list[dict[str, Any]] = checks
        ) -> tuple[dict[str, Any], str]:
            check = run_check(f"chk_studio_signature_{len(checks)}", {"tool": tool, "args": args})
            checks.append({**check.record(), "value": check.value})
            return check.value, check.check_id

        async def convert(
            value: str, unit: str, run: Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any], str]]] = run
        ) -> Converted:
            result, cid = await run("units_convert", {"quantity": value, "to": unit})
            return Converted(Decimal(str(result["value"]["decimal"])), cid)

        context = signatures.SignatureContext(library, conventions)
        for part in item["parts"]:
            predictions = []
            for misconception in library:
                if not set(item["kc_ids"]) & set(misconception["kc_ids"]):
                    continue
                for sig in misconception["signatures"]:
                    response, why = None, None
                    try:
                        if sig["kind"] == "answer_transform":
                            if (
                                sig.get("applies_to_part", part["id"]) != part["id"]
                                or part["answer"]["kind"] != "numeric"
                            ):
                                continue
                            value, _ = await evaluate_expression(sig["expression"], item.get("quantities", {}), run)
                            if value is not None:
                                response = str(value)
                            else:
                                why = "Unbound quantities or an unverifiable transform"
                        elif sig["kind"] == "step_error":
                            why = f"Requires a checked working step with error category {sig['error_category']}"
                        elif sig["rule"] == "ratio_inverted" and part["answer"]["kind"] == "numeric":
                            result, _ = await run("math_evaluate", {"expression": f"1/({part['answer']['value']})"})
                            response = str(result["exact"].get("decimal", result["exact"]["text"]))
                        elif sig["rule"] == "debit_credit_swapped" and part["answer"]["kind"] == "journal_entry":
                            why = "Use the marking sandbox with a swapped journal and the pack chart of accounts"
                        else:
                            why = "Conditional structural signature: test an authored response in the marking sandbox"
                        if response is not None:
                            matched = await signatures._satisfied(
                                sig, item, part, response, context, convert, Settings(), run
                            )
                            if matched is None:
                                why = "No wrong-answer match under the production tolerance and applicability rules"
                                response = None
                    except (ValueError, KeyError, ArithmeticError, WorkerError) as error:
                        why = str(error)
                    predictions.append(
                        {
                            "misconception_id": misconception["id"],
                            "signature": sig,
                            "wrong_answer": response,
                            "reason": why,
                        }
                    )
            numeric = [p for p in predictions if p["wrong_answer"] is not None]
            for left, right in itertools.combinations(numeric, 2):
                if left["misconception_id"] == right["misconception_id"]:
                    continue
                if signatures._near(Decimal(left["wrong_answer"]), Decimal(right["wrong_answer"]), part, Settings()):
                    collisions.append(
                        {
                            "item_id": item["id"],
                            "part": part["id"],
                            "misconceptions": [left["misconception_id"], right["misconception_id"]],
                            "wrong_answer": left["wrong_answer"],
                        }
                    )
            rows.append(
                {
                    "item_id": item["id"],
                    "part": part["id"],
                    "predictions": predictions,
                    "probe": item["id"] in chosen.get("probe_item_ids", []),
                    "distractors": [o for o in part.get("options", []) if o.get("misconception_id") == selected],
                    "checks": list(checks),
                }
            )
    return {"ok": True, "items": rows, "collisions": collisions, "probe_item_ids": chosen.get("probe_item_ids", [])}


_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_CMP = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def constraint(text: str, parameters: dict[str, Any]) -> bool:
    """A bounded arithmetic/boolean constraint language; no Python eval, calls, attributes or indexing."""
    tree = ast.parse(text, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("Constraint is too complex")

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return parameters[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float | str | bool):
            return Decimal(str(node.value)) if isinstance(node.value, int | float) else node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd | ast.Not):
            value = visit(node.operand)
            return not value if isinstance(node.op, ast.Not) else -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BoolOp):
            return (all if isinstance(node.op, ast.And) else any)(visit(v) for v in node.values)
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for op, right in zip(node.ops, node.comparators, strict=True):
                value = visit(right)
                if type(op) not in _CMP or not _CMP[type(op)](left, value):
                    return False
                left = value
            return True
        raise ValueError("Unsupported constraint; use arithmetic, comparisons, and/or/not")

    return bool(visit(tree))


def render(value: Any, parameters: dict[str, Any]) -> Any:
    if isinstance(value, str):
        pieces = []
        for literal, field, spec, conversion in Formatter().parse(value):
            pieces.append(literal)
            if field is not None:
                if field not in parameters or spec or conversion:
                    raise ValueError(
                        "Template placeholders must be declared parameters or available pack.ref context slots"
                    )
                pieces.append(str(parameters[field]))
        return "".join(pieces)
    if isinstance(value, list):
        return [render(v, parameters) for v in value]
    if isinstance(value, dict):
        return {k: render(v, parameters) for k, v in value.items()}
    return value


def sample_template(
    template: dict[str, Any], count: int = 50, contexts: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    if not 1 <= count <= 50:
        raise ValueError("Choose between 1 and 50 samples")
    domains = []
    for spec in template["parameters"]:
        if spec["kind"] == "choice":
            values = spec.get("choices") or []
            if len(values) < 2:
                raise ValueError("Choice parameters need at least two choices")
        else:
            low, high = Decimal(str(spec["min"])), Decimal(str(spec["max"]))
            step = Decimal(str(spec.get("step") or (1 if spec["kind"] == "integer" else "0.1")))
            if not all(v.is_finite() for v in (low, high, step)) or low > high or step <= 0:
                raise ValueError("Invalid parameter range or step")
            if spec["kind"] == "integer" and any(v != v.to_integral_value() for v in (low, high, step)):
                raise ValueError("Integer parameters require integral ranges and steps")
            size = int((high - low) // step) + 1
            # Include both endpoints of the declared discrete domain without allocating huge ranges.
            values = [
                low + step * i
                for i in sorted({round(j * (size - 1) / max(1, min(count, size) - 1)) for j in range(min(count, size))})
            ]
        domains.append((spec["name"], values))
    variants: list[dict[str, Any]] = []
    distribution: dict[str, int] = {}
    total = math.prod(len(values) for _, values in domains)
    for index in range(min(count, total)):
        position = index * (total - 1) // max(1, min(count, total) - 1)
        parameters = {}
        for name, values in reversed(domains):
            position, offset = divmod(position, len(values))
            parameters[name] = values[offset]
        parameters.update(
            {f"context.{name}": values[index % len(values)] for name, values in (contexts or {}).items() if values}
        )
        row: dict[str, Any] = {"parameters": {k: str(v) for k, v in parameters.items()}, "ok": False}
        try:
            failed = [c for c in template.get("constraints") or [] if not constraint(c, parameters)]
            if failed:
                row["problems"] = [f"Constraint failed: {c}" for c in failed]
            else:
                tool = template["answer_tool"]
                check = run_check(
                    f"chk_studio_template_{index}", {"tool": tool["tool"], "args": render(tool["args"], parameters)}
                )
                if isinstance(check.value, dict) and check.value.get("status") in ("unknown", "error", "undefined"):
                    raise ValueError("The answer tool did not verify a markable key")
                kind = template.get("item_type", "numeric")
                if kind == "numeric":
                    number = _figure(check.shown())
                    if number is None or not number.is_finite():
                        raise ValueError("Numeric templates need a finite real numeric key")
                elif kind == "algebraic":
                    if not isinstance(check.value.get("result"), dict) or not check.value["result"].get("text"):
                        raise ValueError("Algebraic templates need a verified symbolic expression")
                elif kind == "equation_solutions":
                    if not isinstance(check.value.get("solutions"), list) or check.value.get("case") not in (
                        "solutions",
                        "no_solution",
                    ):
                        raise ValueError("Equation templates need a verified solution set")
                elif kind == "chemical_equation":
                    if not check.value.get("coefficients"):
                        raise ValueError("Chemical templates need verified balanced coefficients")
                elif kind == "journal_entry":
                    if (
                        check.function != "acc_double_entry"
                        or not check.value.get("entries")
                        or not check.value.get("all_balanced")
                    ):
                        raise ValueError("Journal templates need verified balanced entries")
                elif kind == "accounting_table":
                    figures = check.numbers()
                    if not check.function.startswith("acc_") or not figures or any(not v.is_finite() for v in figures):
                        raise ValueError("Accounting templates need finite figures from a named accounting tool")
                row.update(
                    ok=True,
                    stem=render(template["stem_template"], parameters),
                    key=check.value,
                    key_text=check.shown(),
                    check=check.record(),
                    problems=[],
                )
                key = check.shown()
                distribution[key] = distribution.get(key, 0) + 1
        except (ValueError, KeyError, ArithmeticError, SyntaxError, WorkerError) as error:
            row["problems"] = [str(error)]
        variants.append(row)
    return {
        "ok": all(v["ok"] for v in variants),
        "variants": variants,
        "distribution": distribution,
        "domain_combinations": total,
        "template_parameters": template["parameters"],
    }
