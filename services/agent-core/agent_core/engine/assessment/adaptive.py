"""Diagnostic selection and read-only probe validation (ASM-11, ASM-12, ASM-13)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def select_diagnostic(
    probabilities: Mapping[str, float],
    descendants: Mapping[str, Sequence[str]],
    items: Sequence[dict[str, Any]],
    distance: float,
) -> dict[str, Any] | None:
    unresolved = {k for k, p in probabilities.items() if abs(p - 0.5) < distance}

    def score(kc: str) -> float:
        seen = {kc}
        pending = list(descendants.get(kc, ()))
        while pending:
            child = pending.pop()
            if child not in seen:
                seen.add(child)
                pending.extend(descendants.get(child, ()))
        p = probabilities[kc]
        entropy = -p * math.log2(p) - (1 - p) * math.log2(1 - p) if 0 < p < 1 else 0
        return entropy * (1 + len((seen - {kc}) & unresolved))

    available = {k for k in unresolved if any(k in i["kc_ids"] for i in items)}
    if not available:
        return None
    kc = min(available, key=lambda k: (-score(k), k))
    return min(
        (i for i in items if kc in i["kc_ids"]),
        key=lambda i: (abs(float(i.get("predicted_success", 0.5)) - 0.5), str(i["id"])),
    )


def probe_problems(
    items: Sequence[Mapping[str, Any]], library: Sequence[Mapping[str, Any]], settings: Any
) -> list[str]:
    known = {m["id"]: set(m["kc_ids"]) for m in library}
    problems = []
    for item in items:
        if item.get("type") != "mcq_diagnostic":
            continue
        for part in item["parts"]:
            answer = part["answer"]
            options = answer.get("options", [])
            bad = answer.get("kind") != "mcq" or len(options) != settings.probe_options
            bad |= len({o["id"] for o in options}) != len(options)
            bad |= sum(o.get("correct") is True for o in options) != 1
            for option in options:
                mc = option.get("misconception_id")
                if option.get("correct"):
                    bad |= mc is not None
                else:
                    bad |= not isinstance(mc, str) or not (known.get(mc, set()) & set(item["kc_ids"]))
            if bad:
                problems.append(f"ASM-12: {item['id']}#{part['id']}: invalid probe choices or misconception mapping")
    return problems


def probe_analysis(counts: Mapping[str, int], key: str, settings: Any) -> list[str]:
    total = sum(counts.values())
    if total < settings.probe_analysis_min_responses:
        return []
    return [
        option
        for option, count in counts.items()
        if option != key and count / total < settings.probe_distractor_min_share
    ]
