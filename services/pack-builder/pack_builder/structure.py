"""Record structure the contract cannot state (CUR-11, CUR-12, CUR-13, CUR-25 references, CUR-28).

- References: every id a record names exists in the pack (or in the diagram library it is built with): KCs, nodes,
  items, worked examples, misconceptions, diagrams and templates (TABLE CUR-25a, "Schema and references").
- Lessons (CUR-11): every required block of TABLE CUR-11a is present, blocks follow the script order (a `visual` may
  stand anywhere), the idea and summary texts keep to their word limits and the lesson to its length in minutes. A
  step-shape lesson (TABLE CUR-11b, D110) opens with a `try_first` step, has 3 to 7 steps and then one three-item
  check, names its idea in at least one step, keeps each step's lead-in and why to their sentences, and pairs each
  action with an item type it can take; every item it uses has a worked solution to show.
- Worked examples (CUR-12): steps are numbered 1 to n in order, every step that shows a number has a build check,
  and the fading order lists only existing steps.
- Items (CUR-13): every part has one hint at each rung 1 to 5 (the rungs of PED-03).
- Conventions (CUR-28): an item that tells the student to take a pack constant at another value ("take g = 10")
  declares that override, and an override the item declares is stated to the student. The verifier then keys the
  item with the stated value (`Verifier.item`).
- The chart of accounts (CUR-07, CUR-28): the pack's `pack.ref` chart is well formed, holds every chart the
  conventions or an answer name, and agrees with the chart each build check sends.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from agent_core.config import Settings
from agent_core.store.packs import row_id

from pack_builder.verify import numbers_in

# TABLE CUR-11a: the script order of a lesson's blocks; `visual` is free-standing and `summary` is optional.
SCRIPT = ("prior_knowledge", "idea", "worked_example", "guided_step", "quick_check", "common_mistakes", "exit_check",
          "summary")  # fmt: skip
REQUIRED = ("prior_knowledge", "idea", "worked_example", "guided_step", "quick_check", "common_mistakes", "exit_check")
FREE = frozenset({"visual"})
# TABLE CUR-11b: the step shape's blocks, and the item types each action can take (an action not listed takes any).
STEP_BLOCKS = frozenset({"step", "check"})
ACTION_TYPES = {"choose": frozenset({"mcq", "mcq_diagnostic", "multi_select"}),
                "calculate": frozenset({"numeric", "algebraic", "equation_solutions"})}  # fmt: skip
RUNGS = (1, 2, 3, 4, 5)


class Known:
    """The ids a pack can reference: its own records and the diagram library built with it."""

    def __init__(self, tables: Mapping[str, list[dict[str, Any]]], library: Iterable[str] = ()) -> None:
        self.ids: dict[str, set[str]] = {}
        for table, records in tables.items():
            found = set()
            for record in records:
                try:
                    found.add(row_id(table, record))
                except (KeyError, TypeError):
                    continue
            self.ids[table] = found
        self.ids["diagrams"] = self.ids.get("diagrams", set()) | set(library)

    def has(self, table: str, rid: str) -> bool:
        return rid in self.ids.get(table, set())


# Fields that name another record, by the table that record lives in.
_REFERENCE = {"kc_ids": "kcs", "kc_id": "kcs", "confusable_with": "kcs", "node_id": "curriculum_nodes",
              "item_ids": "items", "item_id": "items", "probe_item_ids": "items",
              "worked_example_id": "worked_examples", "variant_of": "worked_examples",
              "misconception_ids": "misconceptions", "misconception_id": "misconceptions",
              "diagram_id": "diagrams", "diagram_ids": "diagrams", "template_id": "item_templates"}  # fmt: skip
# Cross-pack references are allowed where the contract allows them (prerequisite edges are checked with the map).
_SKIP = {"kcs": ("prerequisites",), "kc_migrations": ("from_kc_ids", "to_kc_ids", "from", "to")}


def _references(node: Any, skip: tuple[str, ...]) -> Iterable[tuple[str, str]]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in skip:
                continue
            table = _REFERENCE.get(key)
            if table is not None:
                for rid in value if isinstance(value, list) else [value]:
                    if isinstance(rid, str):
                        yield table, rid
            elif isinstance(value, Mapping | list):
                yield from _references(value, skip)
    elif isinstance(node, list):
        for value in node:
            yield from _references(value, skip)


def reference_problems(tables: Mapping[str, list[dict[str, Any]]], known: Known, pack: Mapping[str, Any]) -> list[str]:
    """Every referenced id exists. A KC id of another subject-level is a cross-pack reference the map check owns."""
    here = f"{pack.get('subject')}.{pack.get('level')}"
    problems = []
    for table, records in tables.items():
        if table in ("search_fts", "search_vectors", "kc_migrations"):
            continue
        for record in records:
            try:
                rid = row_id(table, record)
            except (KeyError, TypeError):
                continue
            body = {k: v for k, v in record.items() if k != "id"}
            for target, ref in _references(body, _SKIP.get(table, ())):
                if target == "kcs" and ".".join(ref.split(".")[1:3]) != here:
                    continue
                if not known.has(target, ref):
                    problems.append(f"{rid}: references {ref}, which is not in the pack (CUR-25)")
    return sorted(set(problems))


# --- lessons (CUR-11) -----------------------------------------------------------------------------------------------


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text))


_SENTENCE = re.compile(r"[.!?](?=\s|$)")


def _sentences(text: str) -> int:
    """Sentences in a text: ends of sentence followed by a space or the end ("0.75" is not one)."""
    text = text.strip()
    return len(_SENTENCE.findall(text)) + (0 if not text or _SENTENCE.search(text[-1]) else 1)


def lesson_problems(lesson: Mapping[str, Any], settings: Settings,
                    items: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:  # fmt: skip
    """The block script of TABLE CUR-11a, or the step shape of TABLE CUR-11b, and the lesson's length in minutes.
    With the pack's items (by id) the step shape's actions and worked solutions are checked too."""
    rid = str(lesson["id"])
    kinds = [str(b["type"]) for b in lesson["blocks"]]
    if STEP_BLOCKS & set(kinds):
        problems = step_problems(lesson, settings, items)
    else:
        problems = [f"{rid}: no {kind} block (CUR-11)" for kind in REQUIRED if kind not in kinds]
        ranked = [(SCRIPT.index(k), k) for k in kinds if k not in FREE]
        for (before, first), (after, second) in itertools.pairwise(ranked):
            if after < before:
                problems.append(f"{rid}: {second} comes after {first}, out of the lesson script order (CUR-11)")
        limits = {"idea": settings.lesson_idea_max_words, "summary": settings.lesson_summary_max_words}
        for block in lesson["blocks"]:
            limit = limits.get(str(block["type"]))
            if limit is not None:
                count = _words(str(block["text"]["en"]))
                if count > limit:
                    problems.append(f"{rid}: the {block['type']} block has {count} words (at most {limit}, CUR-11)")
    minutes = int(lesson["estimated_minutes"])
    if not settings.lesson_min_minutes <= minutes <= settings.lesson_max_minutes:
        problems.append(f"{rid}: {minutes} minutes, outside {settings.lesson_min_minutes} to "
                        f"{settings.lesson_max_minutes} (CUR-11)")  # fmt: skip
    return problems


def step_problems(lesson: Mapping[str, Any], settings: Settings,
                  items: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:  # fmt: skip
    """TABLE CUR-11b: steps only then one three-item check, opening with `try_first`, an idea named, sentence limits,
    and (with the items) each action on an item type it can take, every item with a worked solution."""
    rid = str(lesson["id"])
    blocks = list(lesson["blocks"])
    kinds = [str(b["type"]) for b in blocks]
    problems = [f"{rid}: the step shape has no {k} block; a lesson is one shape or the other (CUR-11b)"
                for k in dict.fromkeys(kinds) if k not in STEP_BLOCKS]  # fmt: skip
    steps = [b for b in blocks if b["type"] == "step"]
    if not settings.lesson_steps_min <= len(steps) <= settings.lesson_steps_max:
        problems.append(f"{rid}: {len(steps)} steps, outside {settings.lesson_steps_min} to "
                        f"{settings.lesson_steps_max} (CUR-11b)")  # fmt: skip
    if not steps or kinds[0] != "step" or steps[0]["action"] != "try_first":
        problems.append(f"{rid}: the lesson does not open with a try_first step (CUR-11b)")
    if kinds.count("check") != 1 or kinds[-1] != "check":
        problems.append(f"{rid}: one check block, after the last step, closes the lesson (CUR-11b)")
    if not any(b.get("names") for b in steps):
        problems.append(f"{rid}: no step names the idea after the student used it (CUR-11b)")
    limits = (("text", settings.lesson_step_text_max_sentences), ("why", settings.lesson_step_why_max_sentences),
              ("names", settings.lesson_step_why_max_sentences))  # fmt: skip
    for n, step in enumerate(steps, start=1):
        for field, limit in limits:
            count = _sentences(str((step.get(field) or {}).get("en") or ""))
            if count > limit:
                problems.append(f"{rid}: step {n}'s {field} has {count} sentences (at most {limit}, CUR-11b)")
    if items is None:
        return problems
    for n, step in enumerate(steps, start=1):
        item = items.get(str(step["item_id"]))
        allowed = ACTION_TYPES.get(str(step["action"]))
        if item is not None and allowed is not None and item["type"] not in allowed:
            problems.append(f"{rid}: step {n} asks the student to {step['action']} with a {item['type']} item "
                            "(CUR-11b)")  # fmt: skip
    used = [str(s["item_id"]) for s in steps] + [str(i) for b in blocks if b["type"] == "check" for i in b["item_ids"]]
    for item_id in used:
        item = items.get(item_id)
        if item is not None and not (item.get("solution") or {}).get("steps"):
            problems.append(f"{rid}: {item_id} has no worked solution to show (CUR-11b)")
    return problems


# --- worked examples (CUR-12) ---------------------------------------------------------------------------------------


def worked_example_problems(example: Mapping[str, Any]) -> list[str]:
    """Steps 1 to n in order; a build check on every step that shows a number; a fading order of existing steps."""
    rid = str(example["id"])
    problems = []
    numbers = [int(s["n"]) for s in example["steps"]]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"{rid}: steps are numbered {numbers}, not 1 to {len(numbers)} in order (CUR-12)")
    for step in example["steps"]:
        if "check_id" not in step and numbers_in(str(step["latex"])):
            problems.append(f"{rid}: step {step['n']} shows a number but has no build check (CUR-12)")
    unknown = [n for n in example["fading_order"] if n not in numbers]
    if unknown:
        problems.append(f"{rid}: the fading order names steps {unknown} that do not exist (CUR-12)")
    return problems


# --- items (CUR-13) -------------------------------------------------------------------------------------------------


def hint_problems(item: Mapping[str, Any]) -> list[str]:
    """Five authored hints per part, one at each rung of PED-03."""
    problems = []
    for part in item["parts"]:
        rungs = sorted(int(h["rung"]) for h in part["hints"])
        if tuple(rungs) != RUNGS:
            problems.append(f"{item['id']} part {part['id']}: hints at rungs {rungs}, not one at each of 1 to 5 "
                            "(CUR-13)")  # fmt: skip
    return problems


# --- conventions (CUR-28) -------------------------------------------------------------------------------------------

# "Take g = 10 N/kg", "take g as 10 m/s^2", "use c = 3.0 x 10^8 m/s": an instruction to use a constant's value.
_STATED = re.compile(
    r"\b(?:take|use|taking|using)\s+(?P<symbol>[A-Za-z][A-Za-z0-9_]{0,10})\s*(?:=|as|to be)\s*"
    r"(?P<value>-?\d+(?:\.\d+)?(?:\s*(?:[x\u00d7]|\\times)\s*10\s*\^\s*\{?-?\d+\}?|[eE]-?\d+)?)",
    re.IGNORECASE,
)


def _decimal(text: str) -> Decimal | None:
    cleaned = re.sub(r"\s+", "", text)
    power = re.fullmatch("(-?\\d+(?:\\.\\d+)?)(?:[x\u00d7]|\\\\times)10\\^\\{?(-?\\d+)\\}?", cleaned)
    try:
        if power is not None:
            return Decimal(power.group(1)) * Decimal(10) ** int(power.group(2))
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def constants_of(conventions: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, Decimal]]:
    """Physical constants of the pack a question may restate (a numeric value with a unit): symbol (the key's last
    part) -> (key, value)."""
    found = {}
    for convention in conventions:
        if not convention.get("unit"):
            continue
        value = _decimal(str(convention["value"]))
        if value is not None:
            found[str(convention["key"]).rsplit(".", 1)[-1]] = (str(convention["key"]), value)
    return found


def stated_constants(item: Mapping[str, Any]) -> dict[str, Decimal]:
    """Constants the item's text tells the student to take, by symbol."""
    texts = [str(item["stem"]), *(str(p["prompt"]) for p in item["parts"])]
    stated = {}
    for text in texts:
        for match in _STATED.finditer(text.replace("{,}", "")):
            value = _decimal(match.group("value"))
            if value is not None:
                stated[match.group("symbol")] = value
    return stated


def overrides_of(item: Mapping[str, Any]) -> dict[str, str]:
    """The item's declared convention overrides, by symbol."""
    return {str(o["key"]).rsplit(".", 1)[-1]: str(o["value"]) for o in item.get("conventions_override", [])}


def convention_problems(item: Mapping[str, Any], constants: Mapping[str, tuple[str, Decimal]]) -> list[str]:
    rid = str(item["id"])
    problems = []
    stated = stated_constants(item)
    declared = {str(o["key"]): str(o["value"]) for o in item.get("conventions_override", [])}
    for symbol, value in sorted(stated.items()):
        if symbol not in constants:
            continue
        key, default = constants[symbol]
        override = declared.get(key)
        if override is None and value != default:
            problems.append(f"{rid}: the item says {symbol} = {value} but does not declare it overrides {key} "
                            f"(pack value {default}, CUR-28)")  # fmt: skip
        elif override is not None and _decimal(override) != value:
            problems.append(f"{rid}: the item says {symbol} = {value} but overrides {key} with {override} (CUR-28)")
    for key, declared_value in sorted(declared.items()):
        symbol = key.rsplit(".", 1)[-1]
        if symbol in constants and symbol not in stated:
            problems.append(f"{rid}: overrides {key} with {declared_value} but never tells the student (CUR-28)")
    return problems


# --- the chart of accounts (CUR-07 pack.ref, CUR-18, CUR-28) --------------------------------------------------------

CHART_CONVENTION = "acc.chart_of_accounts"
CHART_ID = re.compile(r"coa\.[a-z0-9_.]+\.v[0-9]+")


def _class_of(account: Mapping[str, Any]) -> tuple[Any, Any]:
    return account.get("class"), account.get("normal_side")


def _charts_named(node: Any) -> Iterable[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key == "chart_of_accounts" and isinstance(value, str):
                yield value
            elif isinstance(value, Mapping | list):
                yield from _charts_named(value)
    elif isinstance(node, list):
        for value in node:
            yield from _charts_named(value)


def chart_problems(charts: list[dict[str, Any]], tables: Mapping[str, list[dict[str, Any]]],
                   checks: Mapping[str, Mapping[str, Any]]) -> list[str]:  # fmt: skip
    """The pack's charts of accounts (`pack.ref`, CUR-07): each in the shape the accounting checkers read
    (`acc_double_entry`'s `chart`) with an id and unique account ids; the chart the conventions name and every chart an
    answer names are among them (CUR-28); and every build check that sends a chart of the same id gives each account
    both hold the same class and normal side (a check may send fewer accounts or names, or add an item's personal
    accounts)."""
    from agent_core.tools.generated.worker_schemas import WORKER_SCHEMAS
    from jsonschema import Draft202012Validator

    shape = Draft202012Validator(WORKER_SCHEMAS["acc_double_entry"]["properties"]["chart"])
    problems: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for chart in charts:
        cid = str(chart.get("id") or "")
        errors = [e.message for e in shape.iter_errors(chart)]
        if not CHART_ID.fullmatch(cid):
            errors.append("a chart needs an id coa.<name>.v<n>")
        accounts = [a for a in chart.get("accounts") or [] if isinstance(a, Mapping)]
        ids = [str(a.get("account_id")) for a in accounts]
        errors += [f"account {a} is listed twice" for a in sorted({i for i in ids if ids.count(i) > 1})]
        if cid in by_id:
            errors.append("the chart id is used twice")
        problems += [f"{cid or 'chart of accounts'}: {e} (CUR-07)" for e in errors]
        by_id.setdefault(cid, {str(a.get("account_id")): dict(a) for a in accounts})
    named = [(f"convention {c['key']}", str(c["value"])) for c in tables.get("conventions", [])
             if c.get("key") == CHART_CONVENTION]  # fmt: skip
    for table in ("items", "worked_examples", "item_templates"):
        for record in tables.get(table, []):
            named += [(str(record.get("id")), cid) for cid in sorted(set(_charts_named(record.get("parts") or [])))]
    problems += [f"{who}: names chart of accounts {cid}, which the pack's ref does not hold (CUR-07, CUR-28)"
                 for who, cid in named if cid not in by_id]  # fmt: skip
    for check_id, spec in sorted(checks.items()):
        sent = (spec.get("args") or {}).get("chart")
        if not isinstance(sent, Mapping) or str(sent.get("id")) not in by_id:
            continue
        held = by_id[str(sent.get("id"))]
        given = {str(a.get("account_id")): a for a in sent.get("accounts") or [] if isinstance(a, Mapping)}
        wrong = sorted(a for a, account in given.items()
                       if a in held and _class_of(held[a]) != _class_of(account))  # fmt: skip
        if wrong:
            problems.append(f"{check_id}: its chart {sent.get('id')} differs from the pack's chart for "
                            f"{', '.join(wrong[:5])} (CUR-07)")  # fmt: skip
    return problems
