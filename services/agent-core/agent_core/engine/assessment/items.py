"""Explicit student presentation and pinned-answer validation (API-27)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from agent_core.assets.visuals import resolved_of
from agent_core.config import Settings
from agent_core.contracts import errors
from agent_core.engine.assessment.diagrams import aliases, diagram, public_ref
from agent_core.engine.assessment.errors import Refused
from agent_core.store.pack_index import PackIndex

# A presentation's schema result depends only on its full JSON and the bundled, immutable contract.
# Retain digests rather than question text; all content-dependent presentation/safety checks still run.
_PRESENTATION_VALID: OrderedDict[bytes, bool] = OrderedDict()


def _valid_presentation(shown: dict[str, Any]) -> bool:
    digest = hashlib.sha256(json.dumps(shown, sort_keys=True, separators=(",", ":")).encode()).digest()
    if digest in _PRESENTATION_VALID:
        _PRESENTATION_VALID.move_to_end(digest)
        return _PRESENTATION_VALID[digest]
    valid = not errors("api/commands#/$defs/PracticeItem", shown)
    _PRESENTATION_VALID[digest] = valid
    if len(_PRESENTATION_VALID) > 4096:
        _PRESENTATION_VALID.popitem(last=False)
    return valid


def present(
    item: Mapping[str, Any], content: PackIndex, settings: Settings, *, diagnostic: bool = False
) -> dict[str, Any]:
    ref = content.subject_packs.get("cur." + ".".join(str(item["id"]).split(".")[1:3]))
    parts = []
    for part in item["parts"]:
        a = part["answer"]
        kind = a["kind"]
        shown: dict[str, Any] = {"kind": kind}
        if kind in ("mcq", "multi_select"):
            shown["options"] = [{"id": o["id"], "label": o["text"]} for o in a["options"]]
        elif kind == "numeric":
            shown["unit_prompt"] = a.get("unit") if a.get("unit_policy") == "exact" else None
            # Whether the answer has a unit at all: the client shows a unit field only then (SPEC §10.7).
            shown["unit_expected"] = bool(a.get("unit")) and a.get("unit_policy") != "none"
        elif kind == "algebraic":
            shown["variables"] = list(a["variables"])
        elif kind == "equation_solutions":
            shown["variable"] = a["variable"]
        elif kind == "chemical_equation":
            sides = [" + ".join(str(s["formula"]) for s in a[side]) for side in ("reactants", "products")]
            shown.update(
                unbalanced_equation=" -> ".join(sides), state_symbols_required=a["state_symbols"] == "required"
            )
        elif kind == "journal_entry":
            chart = content.charts.get(str(a["chart_of_accounts"]), {})
            shown.update(
                account_options=[{"id": x["id"], "label": x["name"]} for x in chart.get("accounts", [])],
                currency=a["currency"],
            )
        elif kind == "accounting_table":
            # Amounts and solved row labels are never inferred as input prompts.
            shown.update(headers=["Account", "Amount", "Side"], row_prompts=[], currency=a["currency"])
        elif kind == "diagram_label":
            shown.update(
                diagram_id=public_ref(item),
                target_ids=[aliases(item)[t["part_id"]] for t in a["targets"]],
                label_choices=[],
            )
        elif kind == "ordering":
            elements = sorted(
                a["elements"], key=lambda e: hashlib.sha256(f"{item['id']}:{e['id']}".encode()).hexdigest()
            )
            shown["elements"] = [{"id": e["id"], "label": e["text"]} for e in elements]
        elif kind == "short_text":
            shown["max_characters"] = settings.short_answer_max_chars
        else:
            raise ValueError("unsupported item input")
        parts.append(
            {
                "part_id": part["id"],
                "prompt": part["prompt"],
                "uses_parts": list(part.get("uses_parts") or []),
                "input": shown,
                **({} if diagnostic else {"marks": int(part["marks"])}),
            }
        )
    return {
        "item_id": item["id"],
        "pack_id": ref.pack_id if ref else "",
        "pack_version": ref.version if ref else "",
        "kc_ids": list(item["kc_ids"]),
        "kind": item["type"],
        "stem": item["stem"],
        "parts": parts,
        "visual": diagram(item, content) or resolved_of(item),  # a labelled diagram, else the question's figure
        **({} if diagnostic else {"marks": sum(int(p["marks"]) for p in item["parts"])}),
        # An imported past paper keeps its own numbering and the pages of its diagrams (ASM-27).
        **({"number_label": str(item["number_label"])} if item.get("number_label") else {}),
        **({"figures": copy.deepcopy(item["figures"])} if item.get("figures") else {}),
    }


def validate_answer(item: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    canonical = {k: v for k, v in answer.items() if k != "working"} if isinstance(answer, dict) else answer
    problems = errors("api/commands#/$defs/Answer", canonical)
    if problems:
        raise ValueError(problems[0])
    known = {p["id"]: p for p in item["parts"]}
    seen = set()
    for part in answer["parts"]:
        pid = part["part_id"]
        if pid not in known or pid in seen or part["kind"] != known[pid]["answer"]["kind"]:
            raise ValueError("answer parts must match the pinned item")
        seen.add(pid)
        key = known[pid]["answer"]
        kind = part["kind"]
        if item["type"] != "structured" and kind != ("mcq" if item["type"] == "mcq_diagnostic" else item["type"]):
            raise ValueError("item and answer kinds differ")
        if kind in ("mcq", "multi_select"):
            picked = [part["option_id"]] if kind == "mcq" else part["option_ids"]
            if len(picked) != len(set(picked)) or not set(picked) <= {o["id"] for o in key["options"]}:
                raise ValueError("unknown or repeated option")
        if kind == "ordering" and (
            len(set(part["element_ids"])) != len(part["element_ids"])
            or set(part["element_ids"]) != {e["id"] for e in key["elements"]}
        ):
            raise ValueError("ordering requires every element exactly once")
        if kind == "accounting_table" and any(len(row) != 3 for row in part["rows"]):
            raise ValueError("accounting rows need account, amount and side")
        if kind == "diagram_label":
            by_alias = {v: k for k, v in aliases(item).items()}
            targets = [by_alias.get(t["target_id"], t["target_id"]) for t in part["labels"]]
            if len(targets) != len(set(targets)) or not set(targets) <= {t["part_id"] for t in key["targets"]}:
                raise ValueError("unknown or repeated diagram target")
    working = answer.get("working", [])
    if not isinstance(working, list) or len(working) > Settings().assessment_working_max_lines:
        raise ValueError("invalid working lines")
    for line in working:
        if not isinstance(line, dict) or set(line) != {"part_id", "latex"} or line["part_id"] not in known:
            raise ValueError("working lines must belong to the item")
        if not isinstance(line["latex"], str) or len(line["latex"]) > Settings().assessment_working_max_chars:
            raise ValueError("invalid working line")
    return dict(answer)


def response(part: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> Any:
    kind = part["kind"]
    if kind == "mcq":
        return part["option_id"]
    if kind == "multi_select":
        return part["option_ids"]
    if kind == "numeric":
        unit = part.get("unit")
        if item is not None:
            key: Mapping[str, Any] = next(
                (p["answer"] for p in item.get("parts", []) if p["id"] == part["part_id"]), {}
            )
            if key.get("kind") == "numeric" and key.get("unit_policy") == "none":
                # Labels such as ':1' and 'times' describe a ratio; they are not extra arithmetic in the value.
                # Preserve the typed value for the ordinary strict numeric parser and ignore this unmarked field.
                unit = None
        return str(part["value"]) + (" " + unit if unit else "")
    if kind == "algebraic":
        return part["latex"]
    if kind == "equation_solutions":
        return part["values"]
    if kind == "chemical_equation":
        return part["equation"]
    if kind == "journal_entry":
        return [
            {"account": line["account_id"], "side": line["side"], "amount": line["amount"]} for line in part["lines"]
        ]
    if kind == "accounting_table":
        return [{"label": row[0], "amount": row[1], "side": row[2]} for row in part["rows"]]
    if kind == "diagram_label":
        by_alias = {v: k for k, v in aliases(item).items()} if item is not None else {}
        return {by_alias.get(p["target_id"], p["target_id"]): p["label"] for p in part["labels"]}
    if kind == "ordering":
        return part["element_ids"]
    return part["text"]


def pin_item(item: Mapping[str, Any], content: PackIndex) -> dict[str, Any]:
    snapshot = copy.deepcopy(dict(item))
    solution = snapshot.get("solution") or {}
    reference = solution.get("worked_example_id")
    if reference and not solution.get("steps"):
        example = content.record("worked_examples", reference)
        if example is None:
            raise Refused("verified_solution_unavailable")
        snapshot["solution"] = {**solution, "steps": copy.deepcopy(example["steps"])}
    return snapshot


def ready(item: Mapping[str, Any], content: PackIndex, settings: Settings, *, automatic: bool = False) -> bool:
    """Exclude questions that cannot be presented or, for mastery, automatically checked."""
    if automatic and any(p["answer"]["kind"] == "short_text" for p in item["parts"]):
        return False
    if any(
        p["answer"]["kind"] == "journal_entry"
        and not content.charts.get(p["answer"]["chart_of_accounts"], {}).get("accounts")
        for p in item["parts"]
    ):
        return False
    try:
        shown = present(item, content, settings)
        if not _valid_presentation(shown):
            return False
    except (ValueError, KeyError, TypeError):
        return False
    return True


def public_answer(item: Mapping[str, Any], answer: Any) -> Any:
    """Old saved answers use private part ids; project those to the numbered learner view."""
    if answer is None:
        return None
    result = copy.deepcopy(answer)
    numbered = aliases(item)
    for part in result["parts"]:
        if part["kind"] == "diagram_label":
            for label in part["labels"]:
                label["target_id"] = numbered.get(label["target_id"], label["target_id"])
    return result


# Pack text's `$…$` maths, the same rule as the client's `PackText.tsx`: an opening `$` before a digit opens maths only
# when the span shows it is maths, so currency ("$210 and $35") stays words and "$5 - (-4) \times 2$" is maths.
_PACK_MATHS = re.compile(
    r"(?<![\\$])\$(?:(?![\s\d$])|(?=\d)(?=[^$\n]*?[\\^_=<>+\-*/(){}]|[^$\n]*?\d[A-Za-z]|\d+(?:\.\d+)?\$))"
    r"((?:\\\$|[^$\n])+?)(?<![\s\\])\$(?!\d)"
)


def text_latex(text: str) -> str:
    """Pack text as LaTeX: its `$…$` maths as maths, the words around it as `\\text{}` (braces, backslashes escaped)."""

    def words(chunk: str) -> str:
        safe = chunk.replace("\\", "\\textbackslash ").replace("{", "\\{").replace("}", "\\}")
        return f"\\text{{{safe}}}" if chunk else ""

    out, at = [], 0
    for match in _PACK_MATHS.finditer(text):
        out.append(words(text[at : match.start()]))
        out.append(match.group(1))
        at = match.end()
    out.append(words(text[at:]))
    return "".join(out)


def _key_latex(answer: Mapping[str, Any]) -> str:
    """A part's verified key as the board shows an answer."""
    kind = answer["kind"]
    if kind in ("mcq", "multi_select"):
        return ",\\ ".join(text_latex(str(o["text"])) for o in answer["options"] if o.get("correct"))
    if kind == "numeric":
        unit = answer.get("unit") or ""
        return str(answer["value"]) + (f"\\ \\mathrm{{{unit}}}" if unit else "")
    if kind == "algebraic":
        return str(answer["expression"])
    if kind == "equation_solutions":
        return ",\\ ".join(f"{answer['variable']} = {v}" for v in answer["values"])
    if kind == "ordering":
        names = {e["id"]: e["text"] for e in answer["elements"]}
        return ",\\ ".join(text_latex(str(names.get(i, i))) for i in answer["correct_order"])
    if kind == "short_text":
        model = answer["model_answer"]
        return text_latex(str(model.get("en", "") if isinstance(model, Mapping) else model))
    return ""


def worked_answer(item: Mapping[str, Any], content: PackIndex) -> dict[str, Any]:
    """The worked answer a practice question shows once it is earned (ASM-28): after the second try, on "Don't know" or
    when the student asks. It is the item's verified solution as the board shows a worked example (`WorkedExampleView`):
    each step with its check, and the final answer from the verified key with the key's check (tools mark, never a
    model). A question without authored steps shows its key as the one step."""
    pinned = pin_item(item, content)
    parts = list(pinned.get("parts") or [])
    keys = [p["answer"] for p in parts]
    key_check = next((str(a["check_id"]) for a in keys if a.get("check_id")), "") or f"key:{pinned['id']}"
    final = " ;\\ ".join(x for x in (_key_latex(a) for a in keys) if x) or "\\text{}"
    steps = list((pinned.get("solution") or {}).get("steps") or [])
    reference = (pinned.get("solution") or {}).get("worked_example_id")
    example = content.record("worked_examples", reference) if reference else None
    if example is not None and example.get("final_answer"):
        final, key_check = (
            str(example["final_answer"]["latex"]),
            str(example["final_answer"].get("check_id") or key_check),
        )
    shown = [
        {
            "n": i + 1,
            "latex": str(s["latex"])[:4000],
            "reason": str(s.get("reason") or "")[:120],
            "check_id": s.get("check_id"),
        }
        for i, s in enumerate(steps[:30])
    ] or [{"n": 1, "latex": final[:4000], "reason": "", "check_id": key_check}]
    return {
        "worked_example_id": str(reference or pinned["id"])[:96],
        "problem": str(pinned["stem"])[:4000],
        "steps": shown,
        "final_answer": {"latex": final[:4000], "check_id": key_check[:64]},
        "faded_steps": [],
        "visual": resolved_of(example) or resolved_of(pinned),
    }
