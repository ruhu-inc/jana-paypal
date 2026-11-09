"""The authoring pipeline (CUR-19, as amended by D65): no human approval step.

Each authored record carries, in its source only, the role id of its drafter and the states it has passed through.
The history must follow the state machine from `drafted`, end at the record's review status, and pass each gate when
it was crossed: `verified` needs this build's tool verification and the licence gate. A record reaches the evaluation
bank (or the held-out split, CUR-22) from `verified`, and `published` only from the bank, through the signing service.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from typing import Any

TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("drafted", "verified"),
        ("verified", "drafted"),
        ("verified", "held_out"),
        ("verified", "in_eval_bank"),
        ("in_eval_bank", "published"),
        ("in_eval_bank", "drafted"),
        ("published", "drafted"),  # a new version starts; the published one stays live until replaced
        ("published", "retired"),
        ("held_out", "retired"),
    }
)
STATES = frozenset(s for edge in TRANSITIONS for s in edge)


def review_problems(rid: str, review: dict[str, Any], drafted_by: str | None) -> list[str]:
    """The record's status is a state of the pipeline (states of the approval workflow retired with D65)."""
    if review["status"] not in STATES:
        return [f"{rid}: {review['status']} is not a pipeline state since D65 (CUR-19)"]
    return []


def history_problems(
    rid: str,
    history: Sequence[str],
    review: dict[str, Any],
    drafted_by: str | None,
    *,
    verified: bool,
    licensed: bool,
) -> list[str]:
    if not history or history[0] != "drafted":
        return [f"{rid}: the pipeline history must start at drafted"]
    problems = []
    for before, after in itertools.pairwise(history):
        if (before, after) not in TRANSITIONS:
            problems.append(f"{rid}: {before} -> {after} is not a pipeline step")
    if "verified" in history[1:] and history[-1] != "drafted":
        if not verified:
            problems.append(f"{rid}: reached verified without passing tool verification")
        if not licensed:
            problems.append(f"{rid}: reached verified without passing the licence gate")
    if history[-1] != review["status"]:
        problems.append(f"{rid}: the history ends at {history[-1]}, the record says {review['status']}")
    return problems


def paths(start: str = "drafted", end: str = "published", limit: int = 12) -> Iterator[list[str]]:
    """Every walk from `start` to `end` of at most `limit` states, revisits included (for the no-skip proof)."""
    stack = [[start]]
    while stack:
        path = stack.pop()
        if path[-1] == end:
            yield path
            continue
        if len(path) >= limit:
            continue
        for before, after in TRANSITIONS:
            if before == path[-1]:
                stack.append([*path, after])
