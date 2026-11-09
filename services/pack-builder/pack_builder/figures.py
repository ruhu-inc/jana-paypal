"""Figures on items, worked examples and lessons: checked visuals that actually reach the learner (VSL-12, VSL-13).

A record may carry a `visual` (content-pack `VisualRef`) naming a catalogue kind with its params, optionally a template
preset (`ref`) under them, and the record's own build checks that computed its numbers (`numbers_from`). The build
resolves each one through the engine's one validator with the verification tools, exactly as a preset is resolved, so
every number the figure shows traces to a build check, a preset, a pack convention or a given of the record's own
statement (zero counts as given, as on the engine's board: "from rest", the start of the clock), and the
pre-resolved spec renders with no model and no network. A figure on a question may never show that question's
answer: a numeric key the statement does not give is refused anywhere in the figure's claims or computed labels.

A figure that fails is a `verify` problem naming the record; a passing one is carried into the built record as
`visual.resolved`, which is what the engine and the client show.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from agent_core.assets.resolvers import resolver
from agent_core.assets.visual_kinds import claim_numbers
from agent_core.assets.visuals import Resolver, VisualContext, VisualError, validate

from pack_builder import diagrams
from pack_builder.verify import Verifier, close, numbers_in

Key = tuple[str, str, int]  # (table, record id, lesson block index or -1)
MAX_GIVENS = 12
FIGURE = re.compile(r"(?<![A-Za-z_\d.])-?\d+(?:\.\d+)?(?:/\d+)?")


def located(tables: dict[str, list[dict[str, Any]]]) -> Iterator[tuple[Key, dict[str, Any], dict[str, Any]]]:
    """Every authored figure (a kind with params, not a library diagram): its key, its record and the `VisualRef`."""
    for table in ("items", "worked_examples"):
        for record in tables.get(table, []):
            visual = record.get("visual")
            if isinstance(visual, dict) and "visual_type" in visual:
                yield (table, str(record["id"]), -1), record, visual
    for lesson in tables.get("lessons", []):
        for n, block in enumerate(lesson.get("blocks", [])):
            visual = block.get("visual")
            if isinstance(visual, dict) and "visual_type" in visual:
                yield ("lessons", str(lesson["id"]), n), lesson, visual


def request_of(visual: dict[str, Any], givens: list[str]) -> dict[str, Any]:
    """The visual-spec request the engine's validator takes for a `VisualRef`."""
    request: dict[str, Any] = {"spec_version": 1, "type": visual["visual_type"],
                               "params": json.loads(json.dumps(visual.get("params", {}))),
                               "numbers_from": [*visual.get("numbers_from", []), *givens]}  # fmt: skip
    if visual.get("ref"):
        request["ref"] = visual["ref"]
    if visual.get("caption"):
        request["caption"] = visual["caption"]
    return request


def _statement(table: str, record: dict[str, Any], block: int) -> str:
    if table == "items":
        return " ".join([str(record["stem"]), *(str(p["prompt"]) for p in record["parts"])])
    if table == "worked_examples":
        return str(record["problem"])
    text = (record["blocks"][block].get("text") or {}) if block >= 0 else {}
    return str(text.get("en", "")) if isinstance(text, dict) else str(text)


def _fraction(number: Decimal) -> Fraction | None:
    try:
        return Fraction(number)
    except (ValueError, InvalidOperation):
        return None


def _shown_numbers(resolved: dict[str, Any]) -> list[Decimal]:
    """The numbers a learner can read on the resolved figure: its claims and its computed labels."""
    claims = (_decimal(w) for w, _ in claim_numbers(resolved["type"], resolved.get("params", {})))
    out = [n for n in claims if n is not None]
    labels = (resolved.get("computed") or {}).get("labels") or {}
    for value in labels.values():  # a reading may be exact ("3/8 A"): a fraction is one number, not two
        out += [n for n in (_decimal(m) for m in FIGURE.findall(str(value).replace("\u2212", "-"))) if n is not None]
    return out


def _decimal(text: str) -> Decimal | None:
    try:
        value = Fraction(str(text).strip())
    except (ValueError, ZeroDivisionError):
        return None
    return Decimal(value.numerator) / Decimal(value.denominator)


def answer_shown(item: dict[str, Any], resolved: dict[str, Any]) -> list[str]:
    """The item's numeric keys its figure shows, unless the statement itself gives them (never hand over the answer)."""
    given = set(numbers_in(_statement("items", item, -1)))
    shown = _shown_numbers(resolved)
    leaked = []
    for part in item["parts"]:
        answer = part["answer"]
        if answer.get("kind") != "numeric":
            continue
        key = _decimal(str(answer["value"]))
        if key is None or any(close(key, g) for g in given):
            continue
        if any(close(n, key, answer.get("tolerance")) for n in shown):
            leaked.append(f"part {part['id']} key {answer['value']}")
    return leaked


def figure_problems(source: Any, verifier: Verifier) -> tuple[list[str], dict[Key, dict[str, Any]]]:
    """Every authored figure resolved and checked: (problems, the resolved spec by figure)."""
    presets = {str(p.get("id")): p for p in source.library["visual_presets"]}
    base = diagrams.preset_context(source.pack, source.tables.get("conventions", []), presets)
    problems: list[str] = []
    passed: dict[Key, dict[str, Any]] = {}
    for key, record, visual in located(source.tables):
        table, rid, block = key
        owner = rid if block < 0 else f"{rid} block {block + 1}"
        values: dict[str, list[Fraction]] = {}
        for check_id in visual.get("numbers_from", []):
            check = verifier.check(owner, check_id)
            if check is None:
                continue
            values[check_id] = [f for f in (_fraction(n) for n in check.numbers()) if f is not None]
        knowns: dict[str, Fraction] = {"kn_zero": Fraction(0)}  # given in words ("from rest", the start of the clock)
        for n, number in enumerate(dict.fromkeys(numbers_in(_statement(table, record, block)))):
            value = _fraction(number)
            if value is not None and n < MAX_GIVENS:
                knowns[f"kn_given{n + 1}"] = value
        ctx = VisualContext(base.subject, base.level, base.stage, check_values=values, knowns=knowns,
                            pack_values=dict(base.pack_values), presets=dict(base.presets),
                            conventions=dict(base.conventions))  # fmt: skip
        prefix = "fig_" + hashlib.sha256(f"{table}:{rid}:{block}".encode()).hexdigest()[:10]
        call = diagrams.build_call(prefix)
        try:
            resolved, _ = validate(request_of(visual, list(knowns)), ctx, Resolver(resolver(call, ctx.conventions)))
        except VisualError as error:
            problems.append(f"{owner}: its figure does not resolve ({error.code}: {error.detail})")
            continue
        # Only the givens a claim uses stay cited; the rest were candidates, not sources.
        used = {w for w, _ in claim_numbers(resolved["type"], resolved.get("params", {}))}
        resolved["numbers_from"] = [
            ref for ref in resolved["numbers_from"]
            if not ref.startswith("kn_") or any(_same(w, knowns[ref]) for w in used)
        ]  # fmt: skip
        if table == "items":
            leaked = answer_shown(record, resolved)
            if leaked:
                problems.append(f"{owner}: its figure shows the answer ({', '.join(leaked)})")
                continue
        passed[key] = resolved
    return problems, passed


def _same(written: str, value: Fraction) -> bool:
    number = _decimal(written)
    return number is not None and close(number, Decimal(value.numerator) / Decimal(value.denominator))


def carry(tables: dict[str, list[dict[str, Any]]], passed: dict[Key, dict[str, Any]]) -> None:
    """Each resolved figure into its record's `visual.resolved`, which is what the engine and the client show."""
    for key, _, visual in located(tables):
        resolved = passed.get(key)
        if resolved is not None:
            visual["resolved"] = resolved
