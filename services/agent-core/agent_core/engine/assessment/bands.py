"""Seedable grade intervals in the exam board's own letters (ASM-19, D27).

The Monte Carlo prediction is board-neutral, but a band is only ever shown in the letters the board itself uses, set
per pack with its boundaries (`MockPaper.grade_config`). A pack without them shows no band at all: `predict_band`
returns None, never an invented numeric scale (D27, decided in D100)."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from statistics import mean
from typing import Any

from agent_core.config import Settings


def predict_band(
    mastery: Mapping[str, tuple[float, float]],
    weights: Mapping[str, float],
    checked: int,
    coverage: float,
    recent_scores: Sequence[float],
    settings: Settings,
    *,
    seed: int,
    boundaries: Sequence[float] | None = None,
    labels: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    if labels is None:  # no board letters in the pack: no band (D27)
        return None
    if checked < settings.grade_min_checked or coverage < settings.grade_min_coverage or not weights:
        return None
    if any(k not in mastery or not 0 < mastery[k][0] < 1 or mastery[k][1] < 0 for k in weights):
        return None
    thresholds = tuple(boundaries if boundaries is not None else settings.grade_boundaries)
    if list(thresholds) != sorted(thresholds, reverse=True) or len(set(thresholds)) != len(thresholds):
        raise ValueError("grade boundaries must decrease")
    rng = random.Random(seed)  # noqa: S311 - reproducible Monte Carlo, not security
    draws = []
    total = sum(weights.values())
    if total <= 0:
        return None
    for _ in range(settings.grade_draws):
        score = 0.0
        for kc, weight in weights.items():
            p, n = mastery[kc]
            logit = math.log(p / (1 - p))
            sample = rng.gauss(logit, math.sqrt(1 / (1 + n)))
            probability = 1 / (1 + math.exp(-sample)) if sample >= 0 else math.exp(sample) / (1 + math.exp(sample))
            score += weight / total * probability
        if recent_scores:
            score = score * (1 - settings.grade_mock_blend) + mean(recent_scores) * settings.grade_mock_blend
        draws.append(score)
    draws.sort()
    tail = (1 - settings.grade_interval_share) / 2
    low = draws[min(len(draws) - 1, int(tail * len(draws)))]
    high = draws[min(len(draws) - 1, int((1 - tail) * len(draws)))]

    def band(value: float) -> int:
        return next((i for i, threshold in enumerate(thresholds) if value >= threshold), len(thresholds))

    lower_band, upper_band, central_band = band(low), band(high), band(mean(draws))
    if lower_band - upper_band + 1 > settings.grade_max_band_span:
        return None
    names = list(labels)
    if len(names) != len(thresholds) + 1:
        raise ValueError("grade labels must match boundaries")
    return {
        "band": names[central_band],
        "range": [names[lower_band], names[upper_band]],
        "score_interval": [low, high],
        "confidence": settings.grade_interval_share,
        "message_key": "assessment.likely_band",
        "board_neutral": False,  # always the board's own letters now (D27)
    }


def pack_scale(papers: Iterable[Mapping[str, Any]], subject: str) -> dict[str, Any] | None:
    """The board's grade letters and boundaries a subject's pack sets (`MockPaper.grade_config`), or None when it sets
    none. The pack builder makes every paper of a pack carry the same scale or none (D27)."""
    for paper in papers:
        if paper.get("subject") == subject and paper.get("grade_config"):
            return dict(paper["grade_config"])
    return None
