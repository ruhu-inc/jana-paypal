"""Step checking with error categories (VER-06).

A step is valid when it preserves the value (expressions) or the solution set (equations and inequalities). Lost or
extra solutions are reported as such. An invalid step is classified by a bounded candidate search (at most the
configured number of candidates) in the order of TABLE VER-06a; one candidate that repairs the step exactly gives
`confidence: proved`, otherwise `other` with `heuristic`. One precedence rule: a constant written as a transcription
of one of the question's given values (two digits swapped, or one digit changed) whose replacement by that given makes
the step valid is `copy`, before `arithmetic`. Constants are read from the line as written, before evaluation."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import sympy as sp
from sympy.core.relational import Relational

from tool_worker.budget import SliceExpired, time_slice
from tool_worker.functions.common import parse_arg
from tool_worker.functions.maths import _expressions as expression_equivalence
from tool_worker.functions.maths import exact, slice_seconds, solution_set
from tool_worker.grammar import exact_number
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
FLIP = {sp.Lt: sp.Gt, sp.Gt: sp.Lt, sp.Le: sp.Ge, sp.Ge: sp.Le}


def _is_relation(tree: sp.Basic) -> bool:
    return isinstance(tree, (Relational, sp.And))


def _single_variable(*trees: sp.Basic) -> sp.Symbol | None:
    found: set[sp.Symbol] = set()
    for tree in trees:
        found |= tree.free_symbols
    return next(iter(found)) if len(found) == 1 else None


def _same_value(a: sp.Expr, b: sp.Expr) -> bool | None:
    """True, False, or None when undecided within the slice."""
    try:
        # Keep this shortcut cheap. The shared equivalence checker below screens false identities before
        # expensive simplification; doing simplify here first can exhaust the per-call CPU guard on logs.
        difference = sp.expand(a - b)
    except SliceExpired:
        raise
    if difference == 0:
        return True
    if difference.is_number:
        return False
    return None


def _relation_verdict(prev: sp.Basic, nxt: sp.Basic) -> tuple[str, dict[str, Any]]:
    var = _single_variable(prev, nxt)
    if var is None:
        if isinstance(prev, sp.Eq) and isinstance(nxt, sp.Eq):
            ratio = sp.simplify((prev.lhs - prev.rhs) / (nxt.lhs - nxt.rhs))
            if ratio.is_number and ratio != 0:
                return "valid", {}
        return "unknown", {"reason": "ambiguous"}
    before, after = solution_set(prev, var), solution_set(nxt, var)
    if isinstance(before, sp.ConditionSet) or isinstance(after, sp.ConditionSet):
        return "unknown", {"reason": "no_closed_form"}
    if before == after:
        return "valid", {}
    lost, extra = sp.Complement(before, after), sp.Complement(after, before)
    if after.is_subset(before) and lost != sp.S.EmptySet:
        detail = {"lost": sp.sstr(lost)}
        if isinstance(lost, sp.FiniteSet):
            detail["lost_solutions"] = [f"{var.name} = {sp.sstr(v)}" for v in lost.args]
        return "valid_but_lost_solutions", detail
    if before.is_subset(after) and extra != sp.S.EmptySet:
        detail = {"extraneous": sp.sstr(extra)}
        if isinstance(extra, sp.FiniteSet):
            detail["extraneous_solutions"] = [f"{var.name} = {sp.sstr(v)}" for v in extra.args]
        return "valid_but_extraneous", detail
    return "invalid", {}


def _verdict(prev: sp.Basic, nxt: sp.Basic, context: CallContext) -> tuple[str, dict[str, Any]]:
    if _is_relation(prev) != _is_relation(nxt):
        return "invalid", {"reason": "an expression and an equation are not one step apart"}
    if _is_relation(prev):
        return _relation_verdict(prev, nxt)
    if _same_value(prev, nxt):
        return "valid", {}
    result, _ = expression_equivalence(prev, nxt, context)
    verdict = result["verdict"]
    if verdict in ("equivalent", "equivalent_on_domain"):
        return "valid", {}
    if verdict == "not_equivalent":
        return "invalid", {}
    return "unknown", {"reason": result.get("reason", "ambiguous")}


# --- candidate repairs ------------------------------------------------------------------------------------


def _sides(tree: sp.Basic) -> list[tuple[str, sp.Expr]]:
    return [("lhs", tree.lhs), ("rhs", tree.rhs)] if isinstance(tree, Relational) else [("expr", tree)]


def _rebuild(tree: sp.Basic, side: str, value: sp.Expr) -> sp.Basic:
    if side == "expr":
        return value
    lhs, rhs = (value, tree.rhs) if side == "lhs" else (tree.lhs, value)
    return tree.func(lhs, rhs, evaluate=False) if isinstance(tree, Relational) else tree


def _flips(tree: sp.Basic) -> Iterator[tuple[sp.Basic, str]]:
    if type(tree) in FLIP:
        yield FLIP[type(tree)](tree.lhs, tree.rhs, evaluate=False), "the inequality sign is reversed"
    for side, expr in _sides(tree):
        for term in sp.Add.make_args(expr):
            yield _rebuild(tree, side, expr - 2 * term), f"the sign of {sp.sstr(term)} is wrong"


def _sign_candidates(prev: sp.Basic, nxt: sp.Basic) -> Iterator[tuple[sp.Basic, sp.Basic, str]]:
    """(previous line, new line) pairs with one sign flipped in either line."""
    for flipped, message in _flips(nxt):
        yield prev, flipped, message
    for flipped, message in _flips(prev):
        yield flipped, nxt, message


def _numbers(expr: sp.Basic) -> list[sp.Number]:
    return sorted(expr.atoms(sp.Number), key=lambda n: (float(n), sp.sstr(n)))


def _arithmetic_candidates(prev: sp.Basic, nxt: sp.Basic) -> Iterator[tuple[sp.Basic, str]]:
    """Replace one constant of the new line by the value that makes the step valid."""
    t = sp.Symbol("__t", real=True)
    var = _single_variable(prev, nxt)
    for number in _numbers(nxt):
        trial = nxt.xreplace({number: t})
        try:
            options = _constant_options(prev, trial, t, var)
        except (NotImplementedError, TypeError, ValueError):
            continue  # this constant cannot be solved for; try the next one
        for option in options:
            if option != number:
                yield trial.subs(t, option), f"{sp.sstr(number)} should be {sp.sstr(option)}"


def _constant_options(prev: sp.Basic, trial: sp.Basic, t: sp.Symbol, var: sp.Symbol | None) -> list[sp.Basic]:
    if isinstance(prev, sp.Eq) and var is not None:
        before = solution_set(prev, var)
        if isinstance(before, sp.FiniteSet) and len(before) == 1:
            equation = trial.subs(var, next(iter(before)))
            if isinstance(equation, sp.Eq):
                found = sp.solveset(equation.lhs - equation.rhs, t, sp.S.Reals)
                return list(found) if isinstance(found, sp.FiniteSet) else []
        return []
    if not _is_relation(prev):
        return _options_at_points(prev, trial, t)
    return []


# Fixed probe points (positive, non-integer, distinct) for the free symbols of an expression step.
PROBES = (sp.Rational(7, 3), sp.Rational(11, 5), sp.Rational(13, 7), sp.Rational(17, 4))


def _options_at_points(prev: sp.Basic, trial: sp.Basic, t: sp.Symbol) -> list[sp.Basic]:
    """Constants t with trial == prev, solved at two probe points instead of symbolically in the variables.

    A constant that repairs the step makes both lines agree at every point, so it solves the equation at each probe;
    a value that solves at one probe and not the other is not a repair. Every survivor is still verified by the
    caller. Solving at points avoids symbolic solves in the variables (seconds for logarithms)."""
    symbols = sorted(prev.free_symbols | (trial.free_symbols - {t}), key=lambda s: s.name)
    found: list[sp.Basic] | None = None
    probes = 0
    for shift in range(len(PROBES)):
        point = {s: PROBES[(shift + i) % len(PROBES)] for i, s in enumerate(symbols)} if symbols else {}
        target = prev.subs(point)
        if not (target.is_number and target.is_finite and target.is_real):
            continue
        solutions = sp.solveset(sp.expand(trial.subs(point)) - target, t, sp.S.Reals)
        if not isinstance(solutions, sp.FiniteSet):
            return []
        here = [o for o in solutions if o.is_number]
        found = here if found is None else [o for o in found if any(abs(sp.N(o - h, 30)) < 1e-20 for h in here)]
        probes += 1
        if not found or probes == 2 or not symbols:
            break
    return sorted(found or [], key=lambda n: (float(n), sp.sstr(n)))


def _dropped_term_candidates(prev: sp.Basic, nxt: sp.Basic) -> Iterator[tuple[sp.Basic, str]]:
    for _, expr in _sides(prev):
        for term in sp.Add.make_args(sp.expand(expr)):
            for side, target in _sides(nxt):
                yield _rebuild(nxt, side, target + term), f"the term {sp.sstr(term)} was dropped"


def _cancel_across_sum(e: sp.Basic) -> sp.Basic:
    """(A + c)/c -> A: cancelling a term of a sum against the denominator."""

    def rewrite(node: sp.Basic) -> sp.Basic:
        numerator, denominator = sp.fraction(node)
        if denominator != 1 and isinstance(numerator, sp.Add) and denominator in numerator.args:
            return sp.Add(*[a for a in numerator.args if a != denominator])
        return node

    return e.replace(lambda n: isinstance(n, sp.Mul) and sp.fraction(n)[1] != 1, rewrite)


def _split_log(e: sp.Basic) -> sp.Basic:
    return e.replace(
        lambda n: isinstance(n, sp.log) and isinstance(n.args[0], sp.Add),
        lambda n: sp.Add(*[sp.log(a) for a in n.args[0].args]),
    )


def _split_root(e: sp.Basic) -> sp.Basic:
    split = e.replace(
        lambda n: isinstance(n, sp.Pow) and n.exp == sp.S.Half and isinstance(n.base, sp.Add),
        lambda n: sp.Add(*[sp.sqrt(a) for a in n.base.args]),
    )
    return split.replace(lambda n: isinstance(n, sp.Abs), lambda n: n.args[0])  # the usual slip: sqrt(x^2) = x


def _split_power(e: sp.Basic) -> sp.Basic:
    return e.replace(
        lambda n: isinstance(n, sp.Pow) and n.exp.is_Integer and n.exp > 1 and isinstance(n.base, sp.Add),
        lambda n: sp.Add(*[a**n.exp for a in n.base.args]),
    )


BAD_RULES: list[tuple[str, Callable[[sp.Basic], sp.Basic]]] = [
    ("a log of a sum was split into a sum of logs", _split_log),
    ("a square root of a sum was split into a sum of roots", _split_root),
    ("a power of a sum was taken term by term", _split_power),
    ("a term was cancelled across a sum", _cancel_across_sum),
]


def _illegal_candidates(prev: sp.Basic) -> Iterator[tuple[sp.Basic, str]]:
    """The previous line with one non-equivalence rule applied; a match with the new line names the error."""
    for description, rule in BAD_RULES:
        for side, expr in _sides(prev):
            changed = rule(expr)
            if changed != expr:
                yield _rebuild(prev, side, changed), description


def _transcriptions(value: sp.Number, givens: list[sp.Number]) -> list[sp.Number]:
    """Givens that `value` looks like a copying slip of: swapped digits or one digit changed."""
    text = sp.sstr(value)
    found = []
    for given in givens:
        other = sp.sstr(given)
        if other == text or len(other) != len(text):
            continue
        if sorted(other) == sorted(text) or sum(a != b for a, b in zip(other, text, strict=True)) == 1:
            found.append(given)
    return found


def _copy_candidates(nxt_written: sp.Basic, givens: list[sp.Number]) -> Iterator[tuple[sp.Basic, str]]:
    for number in _numbers(nxt_written):
        for given in _transcriptions(number, givens):
            repaired = nxt_written.xreplace({number: given}).doit()
            yield repaired, f"the given value {sp.sstr(given)} was copied as {sp.sstr(number)}"


def _classify(
    prev: sp.Basic,
    nxt: sp.Basic,
    givens: list[sp.Number],
    budget: int,
    context: CallContext,
    written: sp.Basic | None = None,
    nxt_written: sp.Basic | None = None,
) -> tuple[str, str | None, bool]:
    tried = 0

    # The caller has already disproved the step. Recognise an exact, documented illegal rewrite before
    # spending the candidate budget on unrelated sign/arithmetic repairs (notably logarithms of sums).
    # This classifies a proved error; it can never make an unchecked transformation valid.
    if budget > 0:
        for rewritten, message in _illegal_candidates(written if written is not None else prev):
            same = rewritten == nxt
            if not same and not _is_relation(rewritten) and not _is_relation(nxt):
                # Base-10 logs are represented with a common 1/log(10) factor; distribute that factor exactly.
                same = sp.expand(rewritten - nxt) == 0
            if same:
                return "illegal_operation", message, True

    def repairs(candidate: sp.Basic, base: sp.Basic | None = None) -> bool:
        nonlocal tried
        tried += 1
        if tried > budget:
            raise StopIteration
        try:
            return _verdict(prev if base is None else base, candidate, context)[0] == "valid"
        except (TypeError, ValueError, NotImplementedError):
            return False

    try:
        for candidate, message in _copy_candidates(nxt if nxt_written is None else nxt_written, givens):
            if repairs(candidate):
                return "copy", message, True
        for base, candidate, message in _sign_candidates(prev, nxt):
            if repairs(candidate, base):
                return "sign", message, True
        for candidate, message in _arithmetic_candidates(prev, nxt):
            if repairs(candidate):
                return "arithmetic", message, True
        for candidate, message in _dropped_term_candidates(prev, nxt):
            if repairs(candidate):
                return "dropped_term", message, True
        for rewritten, message in _illegal_candidates(written if written is not None else prev):
            tried += 1
            if tried > budget:
                break
            if _verdict(rewritten, nxt, context)[0] == "valid":
                return "illegal_operation", message, True
    except StopIteration:
        pass
    return "other", None, False


# --- term moves (UXS-61) -------------------------------------------------------------------------------------

_RELATION_MARKS = ("<=", ">=", "!=", "=", "<", ">")
MAX_MOVES = 8


def split_terms(text: str) -> list[tuple[int, str]]:
    """The top-level terms of a written line as (side, text): split at `+`, `-` and the relation outside any group,
    a sign belonging to the term it starts. The board splits a line's LaTeX the same way, so term n here is term
    tn there (VSL-19)."""
    terms: list[tuple[int, str]] = []
    depth, side, current, previous = 0, 0, "", ""
    i = 0

    def close() -> None:
        if current.strip() not in ("", "+", "-"):
            terms.append((side, current.strip()))

    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        relation = next((r for r in _RELATION_MARKS if depth == 0 and text.startswith(r, i)), None)
        if relation is not None:
            close()
            current, side, previous = "", side + 1, "="
            i += len(relation)
            continue
        if depth == 0 and ch in "+-" and previous not in ("", "=", "+", "-", "^", "*", "/"):
            close()
            current, previous = ("-" if ch == "-" else ""), ch
            i += 1
            continue
        current += ch
        if ch != " ":
            previous = ch
        i += 1
    close()
    return terms


def _term_values(text: str, args: dict[str, Any]) -> list[tuple[int, sp.Expr]] | None:
    try:
        return [(side, sp.expand(parse_arg(term, args, "term").tree)) for side, term in split_terms(text)]
    except WorkerError:
        return None


def _moves(args: dict[str, Any]) -> dict[str, Any] | None:
    """For a valid step between two equations: which terms of the previous line were carried across the equals sign
    (to the term of the new line equal to their negative) and which cancelled (a pair on one side summing to zero, or
    one term on both sides). Terms are 1-based positions across the whole line; terms that stay or combine into a new
    value are not moves. None when a line is not one equation of terms the grammar reads."""
    prev, nxt = _term_values(args["prev"], args), _term_values(args["next"], args)
    if prev is None or nxt is None or {s for s, _ in prev} != {0, 1} or {s for s, _ in nxt} != {0, 1}:
        return None
    kept_prev: set[int] = set()
    kept_next: set[int] = set()
    for j, (side, value) in enumerate(nxt):
        i = next((i for i, (s, v) in enumerate(prev) if i not in kept_prev and s == side and v == value), None)
        if i is not None:
            kept_prev.add(i)
            kept_next.add(j)
    moves: list[dict[str, Any]] = []
    left = [i for i in range(len(prev)) if i not in kept_prev]
    for i in left:
        side, value = prev[i]
        target = next((j for j, (s, v) in enumerate(nxt) if j not in kept_next and s != side and v == -value), None)
        if target is not None and value != 0:
            kept_next.add(target)
            moves.append({"from": i + 1, "to": target + 1, "kind": "carry"})
    used = {m["from"] - 1 for m in moves}
    for a in left:
        for b in left:
            if a >= b or a in used or b in used:
                continue
            (side_a, va), (side_b, vb) = prev[a], prev[b]
            if va != 0 and ((side_a == side_b and va + vb == 0) or (side_a != side_b and va == vb)):
                used |= {a, b}
                moves += [{"from": a + 1, "to": None, "kind": "cancel"}, {"from": b + 1, "to": None, "kind": "cancel"}]
    if not moves or len(moves) > MAX_MOVES:
        return None
    return {"moves": sorted(moves, key=lambda m: m["from"]), "terms": [len(prev), len(nxt)]}


def math_check_step(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Check one line of working against the previous line (VER-06). With `moves`, a valid step between two
    equations also reports its term moves (UXS-61)."""
    prev_parsed = parse_arg(args["prev"], args, "prev")
    prev, written = prev_parsed.tree, prev_parsed.as_written
    nxt_parsed = parse_arg(args["next"], args, "next")
    nxt = nxt_parsed.tree
    givens = [exact_number(str(g), 0) for g in args.get("givens", [])]
    budget = int(context.setting("step_candidates", 64))
    try:
        with time_slice(slice_seconds(context) * 3):
            verdict, detail = _verdict(prev, nxt, context)
            if verdict == "valid" and args.get("moves") and (found := _moves(args)) is not None:
                detail = {**detail, **found}
            if verdict != "invalid":
                confidence = "undecided" if verdict == "unknown" else "proved"
                return {"verdict": verdict, **detail}, {"method": "symbolic", "confidence": confidence}
            category, message, repaired = _classify(prev, nxt, givens, budget, context, written, nxt_parsed.as_written)
    except SliceExpired:
        return {"verdict": "unknown", "reason": "timeout"}, {"method": "heuristic", "confidence": "undecided"}
    except (NotImplementedError, TypeError, ValueError) as error:
        raise WorkerError(Code.DOMAIN_ERROR, "the step cannot be checked automatically") from error
    value: dict[str, Any] = {"verdict": "invalid", "category": category}
    if message:
        value["message"] = message
    return value, {"method": "exact" if repaired else "heuristic", "confidence": "proved" if repaired else "heuristic"}


__all__ = ["exact", "math_check_step"]
