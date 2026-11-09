"""Operation-only feedback grounding from checked work; no answers, formulae or learner instructions."""

from __future__ import annotations

import ast
import re
from collections import Counter
from fractions import Fraction
from typing import Any, cast

from agent_core.engine import claims, reply_checks
from agent_core.engine.state import numbers_in

OPERATIONS = {ast.Add: "addition", ast.Sub: "subtraction", ast.Mult: "multiplication",
              ast.Div: "division", ast.Pow: "exponentiation"}
VERBS = {
    "addition": r"add(?:ed|ing|ition)?",
    "subtraction": r"subtract(?:ed|ing|ion)?",
    "multiplication": r"multipl(?:y|ied|ying|ication)",
    "division": r"divid(?:e|ed|ing)|division",
    "exponentiation": r"exponentiat(?:e|ion)|rais(?:e|ed|ing) to",
}
_ANY_OPERATION = "(?:" + "|".join(VERBS.values()) + ")"
_OPPOSITE = {"addition": "subtraction", "subtraction": "addition",
             "multiplication": "division", "division": "multiplication"}
_UNIVERSAL = re.compile(
    rf"\b(?:all|every|any)\b[^.!?]{{0,70}}\b(?:must|always|requires?|needs?)\b"
    rf"[^.!?]{{0,70}}\b{_ANY_OPERATION}\b", re.I,
)


def expression_operations(source: str) -> tuple[str, ...]:
    """Operation labels in bounded numeric arithmetic; no evaluation or claim of mathematical proof."""
    if len(source) > 1000:
        return ()
    found: list[str] = []
    for side in claims.RELATION.split(claims.plain(source)):
        tree = reply_checks._tree(side)
        if tree is None:
            continue
        for node in ast.walk(tree):
            name = OPERATIONS.get(type(node.op)) if isinstance(node, ast.BinOp) else None
            if name and name not in found:
                found.append(name)
    return tuple(found)


def operations(state: Any) -> tuple[str, ...]:
    """Read bounded numeric expressions tied to this plan's receipts, never compute or expose their values."""
    if state.plan is None:
        return ()
    found: list[str] = []
    for step in state.plan.steps[:30]:
        if not step.check_id or step.check_id not in state.check_values:
            continue
        sources = [step.latex, *state.check_expressions.get(step.check_id, [])[:8]]
        for source in sources:
            for name in expression_operations(source):
                if name not in found:
                    found.append(name)
    return tuple(found)


def instruction(state: Any, student_line: str | None = None) -> str:
    names = operations(state)
    if not names:
        return ""
    observed = expression_operations(student_line or "")
    if bare_percentage(state, student_line or "") is not None:
        return (" The marked line treats a percentage rate as an absolute amount. The rate is the percent number; "
                "the change has the original amount's units. Addition or subtraction can be valid after finding "
                "that change. Ask how to use the original amount and rate to find the change.")
    actual = " The checked student expression uses " + ", ".join(observed) + "." if observed else ""
    if (state.plan.final_unit == "mol" and names == ("division",)
            and {"g", "g/mol"} <= {k.get("unit") for k in state.board.knowns}):
        return (actual + " Mass in grams and molar mass in grams per mole are different quantities even when "
                "their numbers match. Ask how dividing mass by molar mass finds amount in moles.")
    return (actual + " The checked plan uses " + ", ".join(names) + ". "
            "Ask how to repair the operation using that relation; preserve the quantities' order.")


def problems(draft: dict[str, Any], state: Any, *, checked_error: bool,
             student_line: str | None = None) -> list[str]:
    """Reject narrow, explicit contradictory advice and universal arithmetic claims on a checked-error turn.

    Reflection ('why did you multiply?'), contrasts and teaching outside this phase remain available.
    This is not a general semantic or curriculum verifier.
    """
    if not checked_error:
        return []
    return advice_problems(draft, operations(state)) + attribution_problems(
        draft, expression_operations(student_line or "")
    ) + quantity_problems(draft, state, student_line or "") + learner_facing_problems(draft, state) + (
        scalar_confirmation_problems(draft, state)
    )


def scalar_confirmation_problems(draft: dict[str, Any], state: Any) -> list[str]:
    """An explicit numeric 'is correct' needs this plan's proof, not an earlier wrong line's provenance.

    This also covers false premises inside questions. Negation and hypothetical reflection remain available.
    Values are read from existing worker-verified plan receipts; nothing is computed or regraded here.
    """
    if state.plan is None:
        return []
    allowed = set()
    for value, check in [(s.value, s.check_id) for s in state.plan.steps] + [
        (state.plan.final_value, state.plan.final_check_id)
    ]:
        if (not isinstance(value, str) or len(value) > 80
                or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:/\d+)?(?:[eE][-+]?\d{1,2})?", value)):
            continue
        try:
            scalar = Fraction(value)
        except (ValueError, ZeroDivisionError):
            continue
        if scalar in state.check_values.get(check, ()):
            allowed.add(scalar)
    number = r"[-+]?\d+(?:\.\d+)?(?:/\d+)?(?:[eE][-+]?\d{1,2})?"
    unit = "|".join(re.escape(u) for u in sorted(claims.UNITS, key=len, reverse=True))
    positive = re.compile(rf"(?<![\w.])({number})(?![\w.%])\s*"
                          rf"(?:(?:currency units?|units?|{unit})\s+)?"
                          r"(?:is|was)\s+(?:(?:already|actually|indeed|absolutely)\s+)?(?:correct|right)\b", re.I)
    errors = []
    for name in ("say", "say_spoken"):
        text = claims.plain(str(draft.get(name) or ""))
        for found in positive.finditer(text):
            prefix = re.split(r"[!?;]|(?<!\d)\.(?!\d)", text[:found.start()])[-1]
            if re.search(r"\b(?:if|suppose|imagine|whether)\b", prefix, re.I):
                continue
            try:
                claimed: Fraction | None = Fraction(found[1])
            except (ValueError, ZeroDivisionError):
                claimed = None
            if claimed not in allowed:
                errors.append(f"{name}: that numeric correctness claim is not supported by this problem's "
                              "worker-verified plan; do not praise an earlier wrong answer as correct")
    return errors


def bare_percentage(state: Any, student_line: str) -> str | None:
    """Recognise a literal rate added/subtracted as an amount, without calculating an answer.

    Only one unambiguous percentage change, two source numerals and a two-literal submitted operation bind.
    Coincident numerical amounts (base 100 or rate zero), extra givens and symbolic working keep normal coaching.
    """
    from agent_core.engine.percentage_evidence import source

    found = source(state)
    if not found or not found[1]:
        return None
    rate = Fraction(found[0])
    values = numbers_in(state.problem.statement)
    bases = [v for v in values if v != rate]
    if len(values) != 2 or len(bases) != 1 or bases[0] == 100 or rate == 0:
        return None
    for side in claims.RELATION.split(claims.plain(student_line)):
        tree = reply_checks._tree(side)
        if not tree or not isinstance(tree.body, ast.BinOp) or not isinstance(tree.body.op, ast.Add | ast.Sub):
            continue
        parts = (tree.body.left, tree.body.right)
        if all(isinstance(n, ast.Constant) for n in parts) and Counter(
            Fraction(cast(Any, n).exact_literal) for n in parts
        ) == Counter((bases[0], rate)):
            return found[0]
    return None


def learner_facing_problems(draft: dict[str, Any], state: Any = None) -> list[str]:
    from agent_core.engine.curriculum_relation import currencies

    statement = state.problem.statement if state is not None and state.problem is not None else ""
    source_currencies = currencies(statement)
    denominations = set(re.findall(r"\b(?:coins?|cents?|pence|banknotes?)\b", statement.lower()))
    financial = bool(source_currencies or denominations or re.search(r"\bcurrency\s+units?\b", statement, re.I))
    errors = []
    for name in ("say", "say_spoken"):
        text = str(draft.get(name) or "")
        if re.search(r"(?:^|[.!?]\s*)ask\s+(?:what|how|which|whether|why|when)\b", text, re.I):
            errors.append(f"{name}: address the learner with your own question; do not echo an instruction "
                          "telling the tutor to ask it")
        if re.search(r"\bhow\s+(?:can|could|should|do)\s+I\s+(?:help|guide|coach|teach)\b", text, re.I):
            errors.append(f"{name}: ask the learner about the mathematical repair, not about your coaching process")
        if re.search(r"\b(?:operation|expression|method|answer)\b[^.!?]{0,35}\bnot\s+in\s+"
                     r"(?:the\s+)?(?:check|plan|schema)\b", text, re.I):
            errors.append(f"{name}: explain the source quantities and relation; private checking jargon "
                          "does not explain the repair")
        if (not re.search(r"\bstudents?\b", statement, re.I)
                and re.search(r"\bthe student\s+(?:(?:has|had)\s+)?"
                              r"(?:correctly|successfully|calculated|found|completed)\b", text, re.I)):
            errors.append(f"{name}: address the learner directly about their checked working")
        component = re.search(r"\b(?:the|our)\s+(engine|solver|checker|diagnoser)\s+"
                              r"(?:confirms?|says?|verified|reports?|checks?)\b", text, re.I)
        if component and not re.search(rf"\b{component[1]}\b", statement, re.I):
            errors.append(f"{name}: acknowledge the checked work directly, without attributing the reply "
                          "to private implementation components")
        if financial:
            new_denominations = set(re.findall(r"\b(?:coins?|cents?|pence|banknotes?)\b", text.lower()))
            # Singular/plural spellings are the same source unit; money units are not coin counts.
            extra = {t.rstrip('s') for t in new_denominations} - {t.rstrip('s') for t in denominations}
            if currencies(text) - source_currencies or extra:
                errors.append(f"{name}: preserve the source currency/amount unit; a generic currency unit "
                              "does not establish a named currency or a number of coins or notes")
    return errors


def quantity_problems(draft: dict[str, Any], state: Any, student_line: str) -> list[str]:
    """Narrow assertions about two explicit operands must agree with their source quantities.

    Numeric checking/reverse methods stay available. No natural-language claim is turned into a board verdict.
    """
    givens = [k for k in state.board.knowns if len(numbers_in(str(k.get("value") or ""))) == 1]
    known_values = {numbers_in(str(k["value"]))[0] for k in givens}
    operands: list[Fraction] = []
    for side in claims.RELATION.split(claims.plain(student_line)):
        parsed = reply_checks._tree(side)
        if parsed and isinstance(parsed.body, ast.BinOp) and all(
            isinstance(n, ast.Constant) for n in (parsed.body.left, parsed.body.right)
        ):
            operands = [Fraction(cast(Any, n).exact_literal) for n in (parsed.body.left, parsed.body.right)]
            break
    check_units = (len(givens) == len(operands) == 2
                   and Counter(numbers_in(str(k["value"]))[0] for k in givens) == Counter(operands))
    actual_units = Counter(str(k.get("unit") or "") for k in givens)
    aliases = "|".join(re.escape(s) for s in sorted(claims.UNITS, key=len, reverse=True))
    pair = re.compile(rf"(?<!\w)({aliases})\s+(?:by|times)\s+({aliases})(?![\w/]|\s+per\b)", re.I)
    allowed = set(known_values)
    if state.plan is not None:
        for step in state.plan.steps[:30]:
            if step.check_id not in state.check_values:
                continue
            for side in claims.RELATION.split(claims.plain(step.latex)):
                parsed = reply_checks._tree(side)
                if parsed and isinstance(parsed.body, ast.BinOp):
                    allowed.update(Fraction(cast(Any, n).exact_literal) for n in ast.walk(parsed)
                                   if isinstance(n, ast.Constant))
    errors = []
    rate = bare_percentage(state, student_line)
    for name in ("say", "say_spoken"):
        text = str(draft.get(name) or "")
        if rate is not None:
            if re.search(r"\bhow\s+does\s+(?:a|the)\s+percentage\s+rate\s+represent\s+"
                         r"(?:an|the)\s+absolute\s+amount\b", text, re.I):
                errors.append(f"{name}: a relative percentage rate alone does not represent an absolute amount; "
                              "ask how to apply it to the original amount")
            for clause in re.split(r"(?<=[!?])|(?<=\.)(?!\d)", text):
                if re.search(r"\byou\s+(?:have\s+)?added\s+(?:the\s+)?(?:price\s+)?(?:increase|change)\b",
                             clause, re.I):
                    errors.append(f"{name}: the submitted numeral is the percentage rate, not the absolute "
                                  "increase; ask how to find the change from the original amount")
                choices = re.search(r"\b(?:represent|mean|stand for)\s+(.+)\?\s*$", clause, re.I)
                if (choices and re.search(rf"(?<![\w.]){re.escape(rate)}(?![\w.%])", clause)
                        and re.search(r"\bchange\b", choices[1], re.I)
                        and re.search(r"\b(?:final|new)\s+(?:price|amount|value)\b", choices[1], re.I)
                        and not re.search(r"\b(?:rate|percent|percentage)\b", choices[1], re.I)):
                    errors.append(f"{name}: neither amount label identifies this percentage rate; offer the "
                                  "rate as a valid choice or ask how it determines the change")
        for operation in ("subtraction", "division", "multiplication", "addition"):
            if operation not in expression_operations(student_line) and re.search(
                rf"\b(?:fix|correct|repair)\s+(?:(?:the|your)\s+)?{operation}\s+order\b", text, re.I
            ):
                errors.append(f"{name}: the marked line did not use {operation}; ask how to choose the "
                              "repair rather than claiming its operation order was wrong")
        if (state.plan is not None and state.plan.final_unit == "mol" and operations(state) == ("division",)
                and {"g", "g/mol"} <= {k.get("unit") for k in givens}
                and expression_operations(student_line) == ("multiplication",)):
            if check_units and re.search(r"\byou\s+(?:have\s+)?(?:squared|cubed)\b", text, re.I):
                errors.append(f"{name}: the line multiplies mass by molar mass, two differently labelled "
                              "quantities; equal numerals do not establish that either quantity was squared")
            # Neither inverse-mole division nor the already rejected multiplication is a valid repair.
            # Questions reflecting on a wrong method, or offering the correct order, remain available.
            for clause in re.split(r"(?<=[!?])|(?<=\.)(?!\d)", text):
                inverse = re.search(r"\bdivid(?:e|ed|ing)\s+(?:the\s+)?molar mass\s+by\s+(?:the\s+)?mass\b",
                                    clause, re.I)
                correct = re.search(r"\b(?:divid(?:e|ed|ing)\s+(?:the\s+)?mass\s+by\s+(?:the\s+)?molar mass"
                                    r"|mass\s+(?:divided by|over)\s+(?:the\s+)?molar mass)\b", clause, re.I)
                reflection = re.search(r"\b(?:why|wrong|incorrect|not|avoid|inverse|reciprocal)\b", clause, re.I)
                if (inverse and not correct and not reflection and clause.rstrip().endswith("?")
                        and re.search(r"\bor\s+(?:multipl(?:y|ied|ying)|multiplication)\b", clause, re.I)):
                    errors.append(f"{name}: neither offered choice repairs this checked multiplication; "
                                  "ask about dividing mass by molar mass, preserving that order")
        if re.search(r"\b(?:since|because)\b[^.!?]{0,90}\bno unknowns?\b[^.!?]{0,90}\b(?:use|divid|multipl)",
                     text, re.I):
            errors.append(f"{name}: absence of an unknown among the givens does not justify an operation; "
                          "ask about the relation between the source quantities")
        if check_units:
            for match in pair.finditer(text):
                units = Counter(claims.UNITS.get(s, claims.UNITS.get(s.lower(), s)) for s in match.groups())
                if units != actual_units:
                    errors.append(f"{name}: describe the actual source units {', '.join(actual_units)}; "
                                  "do not substitute two quantities with the same unit")
        if len(operations(state)) == 1 and not re.search(r"\b(?:check|verify|undo|reverse|reciprocal)\b", text, re.I):
            for match in re.finditer(r"\b(?:divide|multiply|subtract|add)\s+(?:the\s+)?(\d+(?:\.\d+)?)\b", text, re.I):
                if allowed and Fraction(match[1]) not in allowed:
                    errors.append(f"{name}: that operand is not in the checked method; ask about the source "
                                  "quantities rather than using the stated result as a new input")
    return errors


def checked_phase_problems(draft: dict[str, Any], state: Any, *, completed: bool, opening: bool = False) -> list[str]:
    """Ground recommendations and immediate completion attribution in the checked phase's actual work."""
    errors = learner_facing_problems(draft, state) if completed or opening else []
    if opening:
        for name in ("say", "say_spoken"):
            text = str(draft.get(name) or "")
            if text and "?" not in text:
                errors.append(f"{name}: this opening needs one brief question about the operation or relation; "
                              "ask the student to reason instead of supplying a new worked instruction")
    if completed:
        errors += scalar_confirmation_problems(draft, state)
        last = next((s for s in reversed(state.board.steps) if s.owner == "student"), None)
        if last and last.status == "correct" and last.check_id in state.check_values:
            errors += attribution_problems(draft, expression_operations(last.latex))
    if (state.plan is None or state.plan.final_unit != "mol" or operations(state) != ("division",)
            or not {"g", "g/mol"} <= {k.get("unit") for k in state.board.knowns}):
        return errors
    for name in ("say", "say_spoken"):
        text = str(draft.get(name) or "")
        for clause in re.split(r"(?<=[!?])|(?<=\.)(?!\d)", text):
            if clause.rstrip().endswith("?") or re.search(
                r"\b(?:not|never|don't|inverse|reciprocal|reverse|check)\b", clause, re.I
            ):
                continue
            reversed_mass = re.search(
                r"\bmolar mass\b(?:\s+of\s+\d+(?:\.\d+)?\s*g/mol)?\s*"
                r"(?:÷|/|divided\s+by|over)\s*(?:(?:the\s+)?mass\b|\d+(?:\.\d+)?\s*g\b)", clause, re.I
            )
            if reversed_mass:
                errors.append(f"{name}: molar mass divided by mass has inverse-mole units, not mol; "
                              "keep the source quantities in the order of the checked method")
    return errors


def attribution_problems(draft: dict[str, Any], observed: tuple[str, ...]) -> list[str]:
    """A description of the student's checked line must not substitute another arithmetic operation."""
    if not observed:
        return []
    past = {"addition": "added", "subtraction": "subtracted", "multiplication": "multiplied",
            "division": "divided", "exponentiation": "exponentiated"}
    errors = []
    for surface in ("say", "say_spoken"):
        text = str(draft.get(surface) or "").replace("\u2019", "'")
        for sentence in re.findall(r"[^.!?]+[.!?]?", text):
            hypothetical = re.search(r"\b(?:if|unless|suppose|imagine|could|would|might)\b", sentence, re.I)
            historical = re.search(r"\b(?:earlier|previously|before|initially)\b", sentence, re.I)
            if sentence.endswith("?") or hypothetical or historical:
                continue
            if re.search(r"\byou\b", sentence, re.I):
                for operation in observed:
                    if re.search(rf"\bwithout\s+(?:{VERBS[operation]})\b", sentence, re.I):
                        errors.append(f"{surface}: the checked line includes {operation}; do not describe "
                                      "this completed working as omitting it")
            attribution = re.search(r"\byou(?:'ve| have)?\s+(?:(?:correctly|successfully)\s+)?"
                                    r"(?:used|applied|calculated|found)\b(.{0,180})", sentence, re.I)
            if attribution:
                actual = re.split(r"\b(?:instead of|rather than|but|should|could|would|to check|to verify)\b",
                                  attribution[1], maxsplit=1, flags=re.I)[0]
                described = [(m.start(), operation) for operation, verb in VERBS.items()
                             if (m := re.search(rf"\b(?:{verb})\b", actual, re.I))]
                if described and min(described)[1] not in observed:
                    errors.append(f"{surface}: describe the operations in the actual checked student line, "
                                  "not the repaired method as if the learner already used it")
            if len(observed) != 1:
                continue
            for name, verb in VERBS.items():
                if name == observed[0]:
                    continue
                if re.search(rf"\byou(?:'ve| have)?\s+(?:used\s+(?:the\s+)?(?:{verb})|{past[name]})\b", sentence, re.I):
                    errors.append(f"{surface}: the checked student line uses {observed[0]}, not {name}; "
                                  "describe the actual line, then ask about the checked method")
    return errors


def advice_problems(draft: dict[str, Any], expected: tuple[str, ...]) -> list[str]:
    """Text consistency with a supplied operation set; callers supply their independently checked context."""
    errors: list[str] = []
    for surface in ("say", "say_spoken"):
        text = str(draft.get(surface) or "").replace("\u2019", "'")
        assertions = [s for s in re.findall(r"[^.!?]+[.!?]?", text) if not s.endswith("?")]
        if any(_UNIVERSAL.search(s) for s in assertions):
            errors.append(f"{surface}: one checked question does not prove a universal arithmetic rule; "
                          "ask about this line and its checked method")
        if len(expected) != 1:
            continue
        wanted = re.compile(rf"\b(?:{VERBS[expected[0]]})\b", re.I)
        for clause in re.findall(r"[^.!?]+[.!?]?", text):
            if wanted.search(clause):
                continue  # asking the student to compare operations is allowed
            if re.search(r"\b(?:reciprocal|inverse|negative|check)\b", clause, re.I):
                continue  # equivalent arithmetic and checking are not necessarily the opposite method
            directive = re.search(r"\b(?:should|must|need to|ought to|mean to)\b", clause, re.I)
            if not directive and not re.match(r"\s*(?:add|subtract|multiply|divide)\b", clause, re.I):
                continue
            for name, verb in VERBS.items():
                if name != _OPPOSITE.get(expected[0]) or not re.search(rf"\b(?:{verb})\b", clause, re.I):
                    continue
                if re.search(rf"\b(?:not|never|avoid)\s+(?:\w+\s+){{0,2}}(?:{verb})\b", clause, re.I):
                    continue
                errors.append(f"{surface}: recommend this question's checked {expected[0]} method, "
                              f"rather than directing the student to use {name}")
    return errors
