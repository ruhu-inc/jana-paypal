"""Soft pedagogy rules: the rule files, their checks and the deterministic half of matching (PED-11 to PED-17).

A soft rule is condition → action (Parlant's guidelines): a code predicate over the engine's signals (TABLE PED-14a),
a judged yes/no question, or both. Rules live in versioned content (`content/agent/rules/*.yaml`, validated against
the rule contract), ship in the signed agent bundle, and each names the scenario tests that prove it; a rule without
a passing scenario blocks the bundle build (PED-17).

This module has no model and no engine: loading (PED-11, PED-13 cycles, PED-14 signals), scope and predicate
evaluation, relationships (PED-13), conflict order (PED-12), frequency limits and the injection budget (PED-15), and
the scenario runner the build uses. The engine adds the one batched judged call (`engine.soft_rules`)."""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_core.contracts import errors as contract_errors

CONTRACT = "learning/rule"
# PED-12: the higher class wins, in this order.
CLASS_ORDER = ("safety", "answer_policy", "withholding", "honesty", "wellbeing", "pedagogy", "style")
SUBJECT_COUNT, LEVEL_COUNT, MODE_COUNT = 5, 2, 7
ENTAILS_DEPTH = 2  # TABLE PED-13a: closure depth
OPS: dict[str, Callable[[Any, Any], Any]] = {
    "eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge,
    "in": lambda have, want: have in want,
}  # fmt: skip


class RuleError(ValueError):
    """A rule file that cannot load: a contract violation, an unknown signal, a dangling or cyclic relationship."""


@dataclass(frozen=True)
class Rule:
    data: Mapping[str, Any]

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def action(self) -> str:
        return str(self.data["action"])

    @property
    def klass(self) -> str:
        return str(self.data["priority_class"])

    @property
    def priority(self) -> int:
        return int(self.data["priority"])

    @property
    def status(self) -> str:
        return str(self.data["status"])

    @property
    def scope(self) -> Mapping[str, Any]:
        return dict(self.data["scope"])

    @property
    def predicate(self) -> Mapping[str, Any] | None:
        found = self.data["condition"].get("predicate")
        return dict(found) if found is not None else None

    @property
    def question(self) -> str | None:
        found = self.data["condition"].get("question")
        return str(found) if found else None

    @property
    def high(self) -> bool:
        return bool(self.data["criticality"] == "high")

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(str(k).lower() for k in self.data.get("fallback_keywords") or [])

    @property
    def tests(self) -> tuple[str, ...]:
        return tuple(self.data["tests"])

    def related(self, kind: str) -> list[str]:
        return [str(r["target"]) for r in self.data.get("relationships") or [] if r["type"] == kind]

    @property
    def breadth(self) -> int:
        """How many (mode, subject, level) cells the scope covers: smaller is narrower (PED-12)."""
        scope = self.scope
        return (len(scope["modes"]) * len(scope.get("subjects") or range(SUBJECT_COUNT))
                * len(scope.get("levels") or range(LEVEL_COUNT)))  # fmt: skip

    def order(self) -> tuple[int, int, int, str]:
        """PED-12 precedence: higher class, then higher priority, then narrower scope, then lower id."""
        return (CLASS_ORDER.index(self.klass), -self.priority, self.breadth, self.id)

    def tokens(self) -> int:
        return math.ceil(len(line(self)) / 4)  # about 4 characters per token (PRM-09)


def line(rule: Rule) -> str:
    return f"- {rule.action}"


@dataclass(frozen=True)
class RuleBook:
    rules: dict[str, Rule]

    def __iter__(self) -> Any:
        return iter(self.rules.values())


def _signals_in(predicate: Mapping[str, Any]) -> Iterable[str]:
    for key in ("all", "any"):
        for child in predicate.get(key, []):
            yield from _signals_in(child)
    if "not" in predicate:
        yield from _signals_in(predicate["not"])
    if "signal" in predicate:
        yield str(predicate["signal"])


def _cycle(rules: Mapping[str, Rule], kind: str) -> list[str] | None:
    """A cycle along one relationship type, or None."""
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for target in rules[node].related(kind):
            if state.get(target) == 1:
                return [*path, node, target]
            if state.get(target) is None and target in rules:
                found = visit(target, [*path, node])
                if found:
                    return found
        state[node] = 2
        return None

    for rule_id in rules:
        if state.get(rule_id) is None:
            found = visit(rule_id, [])
            if found:
                return found
    return None


def load_documents(documents: Sequence[Mapping[str, Any]],
                   check: Callable[[Mapping[str, Any]], None] | None = None) -> RuleBook:  # fmt: skip
    """Rule files as parsed YAML: each validated against the rule contract (unknown fields and unknown signals are
    rejected, PED-11, PED-14), ids unique, relationship targets present and no cycle in `entails` or `depends_on`
    (PED-13). `check` is an extra per-rule check (the engine passes the hard-rule loader of PED-10)."""
    rules: dict[str, Rule] = {}
    for document in documents:
        problems = contract_errors(CONTRACT, document)
        if problems:
            raise RuleError(f"rule file breaks its contract: {problems[0]}")
        for raw in document["rules"]:
            if raw["id"] in rules:
                raise RuleError(f"rule {raw['id']} is defined twice")
            if check is not None:
                check(raw)
            rules[raw["id"]] = Rule(raw)
    for rule in rules.values():
        for kind in ("entails", "overrides", "excludes", "depends_on"):
            for target in rule.related(kind):
                if target not in rules:
                    raise RuleError(f"rule {rule.id} {kind} unknown rule {target}")
    for kind in ("entails", "depends_on"):
        cycle = _cycle(rules, kind)
        if cycle:
            raise RuleError(f"{kind} cycle: {' -> '.join(cycle)}")
    return RuleBook(rules)


def load(folder: Path, check: Callable[[Mapping[str, Any]], None] | None = None) -> RuleBook:
    paths = sorted(p for p in folder.glob("*.yaml") if not p.name.startswith("scenarios"))
    return load_documents([yaml.safe_load(p.read_text()) for p in paths], check)


# --- evaluation ----------------------------------------------------------------------------------------------------


def evaluate(predicate: Mapping[str, Any], signals: Mapping[str, Any]) -> bool:
    """A code predicate over this turn's signals; a signal the engine did not compute is false for every comparison."""
    if "all" in predicate:
        return all(evaluate(p, signals) for p in predicate["all"])
    if "any" in predicate:
        return any(evaluate(p, signals) for p in predicate["any"])
    if "not" in predicate:
        return not evaluate(predicate["not"], signals)
    name, op, want = predicate["signal"], predicate["op"], predicate["value"]
    if name not in signals or signals[name] is None:
        return False
    have = signals[name]
    try:
        return bool(OPS[op](have, want))
    except TypeError:
        return False


def in_scope(rule: Rule, mode: str, subject: str, level: str, role: str = "coach") -> bool:
    scope = rule.scope
    return (rule.status == "active" and mode in scope["modes"]
            and (not scope.get("subjects") or subject in scope["subjects"])
            and (not scope.get("levels") or level in scope["levels"])
            and (not scope.get("roles") or role in scope["roles"]))  # fmt: skip


def keyword_match(rule: Rule, text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in rule.keywords)


def relate(book: RuleBook, active: set[str], allowed: set[str]) -> tuple[set[str], list[dict[str, str]]]:
    """TABLE PED-13a after matching: entails (depth 2), depends_on, overrides, then excludes by PED-12. Returns the
    active ids and what each step did."""
    rules = book.rules
    notes: list[dict[str, str]] = []
    frontier = set(active)
    for _ in range(ENTAILS_DEPTH):
        added = {t for a in frontier for t in rules[a].related("entails") if t in allowed and t not in active}
        for target in sorted(added):
            notes.append({"rule": target, "by": "entails"})
        active |= added
        frontier = added
    changed = True
    while changed:  # depends_on: A stays only while B is active this turn
        changed = False
        for rule_id in sorted(active):
            if any(t not in active for t in rules[rule_id].related("depends_on")):
                active.discard(rule_id)
                notes.append({"rule": rule_id, "by": "depends_on"})
                changed = True
    for rule_id in sorted(active):
        for target in rules[rule_id].related("overrides"):
            if rule_id in active and target in active:
                active.discard(target)
                notes.append({"rule": target, "by": f"overridden by {rule_id}"})
    for rule_id in sorted(active, key=lambda r: rules[r].order()):
        if rule_id not in active:
            continue
        for target in rules[rule_id].related("excludes"):
            if target in active:
                loser = max((rule_id, target), key=lambda r: rules[r].order())
                active.discard(loser)
                notes.append({"rule": loser, "by": "excluded"})
    for rule_id in sorted(active):  # an exclusion stated on the other side
        for other in sorted(active):
            if rule_id in rules[other].related("excludes") and rule_id in active and other in active:
                loser = max((rule_id, other), key=lambda r: rules[r].order())
                active.discard(loser)
                notes.append({"rule": loser, "by": "excluded"})
    return active, notes


@dataclass
class Frequency:
    """The frequency limits of each rule across a session: turns and steps it was injected on."""

    shown: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def allows(self, rule: Rule, turn: int, step: int) -> bool:
        limits = rule.data.get("frequency") or {}
        past = self.shown.get(rule.id, [])
        if limits.get("max_per_session") is not None and len(past) >= limits["max_per_session"]:
            return False
        if limits.get("min_turns_between") is not None and past and turn - past[-1][0] < limits["min_turns_between"]:
            return False
        return not (limits.get("once_per_step") and any(s == step for _, s in past))

    def record(self, rule_id: str, turn: int, step: int) -> None:
        self.shown.setdefault(rule_id, []).append((turn, step))


def budget(rules: Sequence[Rule], max_rules: int, max_tokens: int) -> list[Rule]:
    """The rules injected into the per-turn block: in PED-12 order, at most `max_rules` within `max_tokens`."""
    kept: list[Rule] = []
    used = 0
    for rule in sorted(rules, key=Rule.order):
        if len(kept) >= max_rules:
            break
        if used + rule.tokens() > max_tokens:
            continue
        kept.append(rule)
        used += rule.tokens()
    return kept


def block(rules: Sequence[Rule]) -> str:
    return "\n".join(line(r) for r in rules)


# --- deterministic matching (everything but the judged call) --------------------------------------------------------


@dataclass
class Situation:
    """One turn's inputs to matching."""

    mode: str
    subject: str
    level: str
    signals: Mapping[str, Any]
    text: str = ""  # the student's message, for keyword fallback
    turn: int = 1
    step: int = 1
    role: str = "coach"


@dataclass
class Matched:
    active: list[Rule]
    injected: list[Rule]
    trace: list[dict[str, Any]]
    calls: int = 0
    fallback: str | None = None  # why judged rules were decided by keywords (low class, timeout, parse failure)


def candidates(book: RuleBook, situation: Situation) -> tuple[set[str], list[Rule], set[str]]:
    """Steps 1 and 2 of PED-15: (rules active by code alone, rules that still need judging, every in-scope id)."""
    allowed = {r.id for r in book if in_scope(r, situation.mode, situation.subject, situation.level, situation.role)}
    active: set[str] = set()
    judged: list[Rule] = []
    for rule_id in sorted(allowed):
        rule = book.rules[rule_id]
        if rule.predicate is not None and not evaluate(rule.predicate, situation.signals):
            continue
        if rule.question is None:
            active.add(rule_id)
        else:
            judged.append(rule)
    return active, sorted(judged, key=Rule.order), allowed


def finish(book: RuleBook, situation: Situation, active: set[str], allowed: set[str], frequency: Frequency,
           max_rules: int, max_tokens: int, trace: list[dict[str, Any]]) -> tuple[list[Rule], list[Rule]]:  # fmt: skip
    """Steps 4 to 6 of PED-15: relationships and PED-12, frequency limits, then the injection budget."""
    related, notes = relate(book, set(active), allowed)
    trace.extend({"rule": n["rule"], "reason": n["by"]} for n in notes)
    kept = [book.rules[r] for r in sorted(related) if frequency.allows(book.rules[r], situation.turn, situation.step)]
    injected = budget(kept, max_rules, max_tokens)
    for rule in injected:
        frequency.record(rule.id, situation.turn, situation.step)
    return sorted(kept, key=Rule.order), injected


def match_offline(book: RuleBook, situation: Situation, frequency: Frequency, max_rules: int, max_tokens: int,
                  judged_answers: Mapping[str, bool] | None = None) -> Matched:  # fmt: skip
    """Matching with judged conditions decided by `judged_answers` when given, else by keywords; a high-criticality
    rule that cannot be judged defaults to active (PED-15)."""
    trace: list[dict[str, Any]] = []
    active, judged, allowed = candidates(book, situation)
    for rule_id in sorted(active):
        trace.append({"rule": rule_id, "reason": "code predicate"})
    for rule in judged:
        if judged_answers is not None and rule.id in judged_answers:
            applies, reason = judged_answers[rule.id], "judged"
        elif keyword_match(rule, situation.text):
            applies, reason = True, "keyword fallback"
        else:
            applies, reason = rule.high, "high criticality default" if rule.high else "no keyword"
        if applies:
            active.add(rule.id)
            trace.append({"rule": rule.id, "reason": reason})
    kept, injected = finish(book, situation, active, allowed, frequency, max_rules, max_tokens, trace)
    return Matched(kept, injected, trace, 0, None if judged_answers is not None else "keywords")


# --- scenarios and the build check (PED-16, PED-17) ----------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    rule: str
    passed: bool
    detail: str = ""


def run_scenarios(book: RuleBook, scenarios: Sequence[Mapping[str, Any]], max_rules: int = 50,
                  max_tokens: int = 100_000) -> list[ScenarioResult]:  # fmt: skip
    """Each scenario: a situation (mode, subject, level, signals, student text, scripted judged answers) and the
    rules it expects active and inactive after matching."""
    results = []
    for scenario in scenarios:
        situation = Situation(scenario.get("mode", "guided"), scenario.get("subject", "physics"),
                              scenario.get("level", "O"), dict(scenario.get("signals") or {}),
                              str(scenario.get("text", "")))  # fmt: skip
        matched = match_offline(book, situation, Frequency(), max_rules, max_tokens, scenario.get("judged"))
        ids = {r.id for r in matched.active}
        missing = [r for r in scenario.get("expect_active", []) if r not in ids]
        extra = [r for r in scenario.get("expect_inactive", []) if r in ids]
        detail = "; ".join([*(f"{r} not active" for r in missing), *(f"{r} active" for r in extra)])
        results.append(ScenarioResult(str(scenario["id"]), str(scenario["rule"]), not missing and not extra, detail))
    return results


def load_scenarios(folder: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob("scenarios*.yaml")):
        out += list((yaml.safe_load(path.read_text()) or {}).get("scenarios") or [])
    return out


def build_errors(book: RuleBook, scenarios: Sequence[Mapping[str, Any]]) -> list[str]:
    """Why the rules cannot ship (PED-17): a rule whose named scenario tests are missing or fail. Retired rules keep
    their ids and need no live test."""
    results = {r.scenario: r for r in run_scenarios(book, scenarios)}
    errors = []
    for rule in book:
        if rule.status == "retired":
            continue
        named = [results.get(t) for t in rule.tests]
        if not any(r is not None and r.passed and r.rule == rule.id for r in named):
            details = [f"{t}: {results[t].detail}" if t in results else f"{t}: missing" for t in rule.tests]
            errors.append(f"rule {rule.id} has no passing scenario test ({'; '.join(details)})")
    return errors


def folder_errors(folder: Path) -> list[str]:
    """The bundle build's check of the shipped rules folder: loading errors, then rules without a passing test."""
    if not folder.is_dir():
        return []
    try:
        book = load(folder)
    except RuleError as error:
        return [str(error)]
    return build_errors(book, load_scenarios(folder))
