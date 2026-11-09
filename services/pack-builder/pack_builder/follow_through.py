"""Follow-through recipes checked at build (ASM-06, CUR-15).

A part marked for error carried forward may carry a recipe (`follow_through`: an expression for `math_evaluate` over
the item's `quantities` and one symbol per earlier part). At run time the marker evaluates it on the student's earlier
values; here it is evaluated on the earlier parts' keys, and the result must be this part's key within the part's
tolerance, so a recipe that would carry the wrong computation forward never ships. The recipe must name only earlier
numeric parts the part uses, and the part must be numeric and marked `ecf: true`.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from agent_core.engine.marking import WORKING_FIGURES, evaluable
from tool_worker.functions import FUNCTIONS
from tool_worker.protocol import WorkerError
from tool_worker.registry import CallContext

from pack_builder.verify import close

RETRIES = 3


def evaluate(expression: str, quantities: dict[str, Any]) -> Decimal | None:
    """The value of an expression over quantities by `math_evaluate`, read to the working figures (the marker's reading,
    `marking.evaluate_expression`); None when it is not one the checker evaluates."""
    found = evaluable(expression, quantities)
    if found is None:
        return None
    text, symbols = found
    args = {"expression": text, "values": {s: str(quantities[n]) for s, n in symbols.items()}, "sf": WORKING_FIGURES}
    context = CallContext("build_follow_through", "chk_build_follow_through", {}, {}, lambda: False, settings={})
    try:
        value = FUNCTIONS["math_evaluate"].implementation(args, context)[0]
        for _ in range(RETRIES):  # a cold or loaded build can overrun the checker's time slice: ask again
            if value.get("reason") != "timeout":
                break
            value = FUNCTIONS["math_evaluate"].implementation(args, context)[0]
        return Decimal(str(value["rounded"]))
    except (WorkerError, InvalidOperation, KeyError, TypeError):
        return None


def part_problems(item: dict[str, Any], part: dict[str, Any]) -> list[str]:
    recipe = part.get("follow_through")
    if not recipe:
        return []
    owner = f"{item['id']} part {part['id']}"
    answer = part["answer"]
    if answer["kind"] != "numeric" or not part.get("ecf"):
        return [f"{owner}: a follow-through recipe needs a numeric part marked ecf: true (ASM-06)"]
    order = [str(p["id"]) for p in item["parts"]]
    earlier = {str(p["id"]): p for p in item["parts"][: order.index(str(part["id"]))]}
    uses = {str(p) for p in part.get("uses_parts") or []}
    values: dict[str, Any] = dict(item.get("quantities") or {})
    for symbol, part_id in recipe["symbols"].items():
        used = earlier.get(str(part_id))
        if used is None or str(part_id) not in uses or used["answer"]["kind"] != "numeric":
            return [f"{owner}: follow-through symbol {symbol} names {part_id}, which is not an earlier numeric part "
                    f"in uses_parts (ASM-06)"]  # fmt: skip
        values[str(symbol)] = str(used["answer"]["value"])
    result = evaluate(str(recipe["expression"]), values)
    if result is None:
        return [f"{owner}: follow-through expression {recipe['expression']!r} cannot be evaluated over the item's "
                f"quantities and the earlier parts (ASM-06)"]  # fmt: skip
    if not close(Decimal(answer["value"]), result, answer.get("tolerance")):
        return [f"{owner}: follow-through expression gives {result.normalize()} from the earlier keys, not the key "
                f"{answer['value']} (ASM-06)"]  # fmt: skip
    return []


def problems(items: list[dict[str, Any]]) -> list[str]:
    """Every follow-through recipe that does not reproduce its part's key from the earlier parts' keys."""
    found = [problem for item in items for part in item["parts"] for problem in part_problems(item, part)]
    for item in items:
        tagged = [p for p in item["parts"] if p.get("kc_ids")]
        if tagged and (item["type"] != "structured" or len(tagged) != len(item["parts"])):
            found.append(f"{item['id']}: part KC tags require a structured item with every part tagged (ASM-06)")
        if any(set(p["kc_ids"]) - set(item.get("kc_ids") or []) for p in tagged):
            found.append(f"{item['id']}: a part KC must belong to its item (ASM-06)")
        if (
            item["type"] == "structured"
            and len(item.get("kc_ids") or []) > 1
            and any(p.get("ecf") for p in item["parts"])
            and not tagged
        ):
            found.append(f"{item['id']}: multi-KC follow-through needs part-specific KC tags (ASM-06)")
    return found
