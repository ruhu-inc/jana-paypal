"""Per-student calibration (LRN-12): a personal learning-rate multiplier on `gamma_k` per subject and personal FSRS
weights, fitted on the device, offline, from the student's own learner model; nothing leaves the device.

- **Learning rate.** From `personal_rate_min_observations` graded opportunities in a subject, the multiplier m that
  maximises the likelihood of the student's opportunities under the pfa_elo replay with every KC's `gamma` times m
  (LRN-07), penalised by a normal prior on log m at 0 of `personal_rate_prior_sd`: the shrinkage toward 1.0.
- **FSRS.** From `personal_fsrs_min_reviews` reviews, the weights that best predict the student's recall. The py-fsrs
  optimiser needs PyTorch and py-fsrs is a test-only reference library that shipped code never imports (LRN-13,
  SEC-22), so the weights are fitted here by a bounded coordinate search over the initial stabilities and the
  stability growth and forgetting weights, on the same scheduler the projection uses (`learner.fsrs`).
- **Adoption.** Each fit uses the events before the student's most recent `personal_fit_recent_share` (20%) and is
  adopted only when its log loss on that recent share is lower by at least `personal_fit_min_gain` (2%) than the
  defaults'; otherwise the defaults remain.

The observations are the attempts projection's checked opportunities, each a whole success or failure with unit
weight (as `jana_tools.learner_eval` replays a log); a review is a KC's first checked opportunity of a local day,
rated Again, Hard or Good from its outcome and hints (TABLE LRN-14a without the time rows). Fitting is a pure
function of the learner database, run in idle time. Recording an adopted fit needs the learner-model piece proposed in
contract-additions-m33b.json, so the fold can apply it from the log on every rebuild; until the contract adopts it
the fit is computed and reported, and the projection keeps the defaults.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from agent_core.config.keys import Settings
from agent_core.learner.fsrs import DAY_MS, Card, Scheduler, scheduler_of
from agent_core.learner.mastery import Ability, KcEvidence, MasteryParams, item_probability, observe, subject_of

MULTIPLIERS = tuple(math.exp(math.log(0.25) + k * math.log(16) / 40) for k in range(41))  # 0.25 to 4, log-spaced
FSRS_FITTED = (0, 1, 2, 3, 8, 9, 10, 11)  # initial stabilities, recall growth and forgetting (FSRS-6 w0-w3, w8-w11)
FSRS_STEPS = (0.5, 0.8, 1.25, 2.0)
FSRS_PASSES = 3
FSRS_STEP_GAIN = 0.005  # a step is kept only when it lowers the training loss by this share: no fitting to noise


@dataclass(frozen=True)
class Graded:
    """One checked opportunity: its KC weights, whether it succeeded, when, and the item's difficulty."""

    weights: Mapping[str, float]
    correct: bool
    t_ms: int
    b_item: float = 0.0


@dataclass(frozen=True)
class Review:
    kc_id: str
    t_ms: int
    rating: str  # again | hard | good

    @property
    def recalled(self) -> bool:
        return self.rating != "again"


@dataclass(frozen=True)
class Fit:
    """A personal fit and whether it is adopted: `value` is the multiplier or the weights; the losses are on the
    student's most recent events."""

    kind: str  # learning_rate | fsrs
    subject: str | None
    events: int
    value: float | tuple[float, ...] | None
    adopted: bool
    default_loss: float | None = None
    personal_loss: float | None = None
    reason: str = ""


def log_loss(scores: Sequence[tuple[float, bool]]) -> float:
    if not scores:
        return math.nan
    return -sum(math.log(min(max(s if y else 1 - s, 1e-12), 1.0)) for s, y in scores) / len(scores)


def _adopt(default: float, personal: float, settings: Settings) -> bool:
    return math.isfinite(default) and math.isfinite(personal) and default > 0 and (
        (default - personal) / default >= settings.personal_fit_min_gain)  # fmt: skip


def _split(n: int, settings: Settings) -> int:
    """The index where the most recent share begins."""
    return n - max(1, round(n * settings.personal_fit_recent_share))


# --- the learning rate ---------------------------------------------------------------------------------------------


def scaled(params: MasteryParams, multiplier: float) -> MasteryParams:
    """The parameters with every KC's gamma (the default and the pack's fitted ones) times the multiplier."""
    return replace(
        params,
        mastery_gamma_default=params.mastery_gamma_default * multiplier,
        kc_overrides=tuple((kc, beta, gamma * multiplier, rho) for kc, beta, gamma, rho in params.kc_overrides),
    )


def replay(graded: Sequence[Graded], params: MasteryParams) -> list[tuple[float, bool]]:
    """The predicted success before each opportunity, replaying the student's opportunities in time order."""
    ability, evidence = Ability(), {}  # type: ignore[var-annotated]
    scores = []
    for row in graded:
        for kc in row.weights:
            evidence.setdefault(kc, KcEvidence()).decay_to(row.t_ms, params)
        scores.append((item_probability(ability.theta, row.weights, evidence, params, row.b_item), row.correct))
        observe(y=int(row.correct), w=1.0, t=row.t_ms, weights=row.weights, first_error_kc=None, ability=ability,
                evidence=evidence, p=params, b_item=row.b_item)  # fmt: skip
    return scores


def fit_learning_rate(graded: Sequence[Graded], params: MasteryParams, settings: Settings | None = None,
                      subject: str | None = None) -> Fit:  # fmt: skip
    settings = settings or Settings()
    if len(graded) < settings.personal_rate_min_observations:
        return Fit("learning_rate", subject, len(graded), None, False, reason="too_few_observations")
    cut = _split(len(graded), settings)
    variance = settings.personal_rate_prior_sd**2

    def objective(m: float) -> float:
        scores = replay(graded[:cut], scaled(params, m))
        return log_loss(scores) * len(scores) + math.log(m) ** 2 / (2 * variance)

    best = min(MULTIPLIERS, key=objective)
    default = log_loss(replay(graded, params)[cut:])
    personal = log_loss(replay(graded, scaled(params, best))[cut:])
    adopted = _adopt(default, personal, settings)
    return Fit("learning_rate", subject, len(graded), round(best, 4), adopted, default, personal,
               "" if adopted else "no_gain")  # fmt: skip


# --- FSRS ------------------------------------------------------------------------------------------------------------


def recall_scores(reviews: Sequence[Review], scheduler: Scheduler) -> list[tuple[float, bool]]:
    """The predicted recall before each review of a KC that has been reviewed before, in the reviews' order."""
    cards: dict[str, Card] = {}
    scores = []
    for review in reviews:
        card = cards.get(review.kc_id)
        if card is not None and card.last_review_ms is not None:
            elapsed = max(0, (review.t_ms - card.last_review_ms) // DAY_MS)
            if elapsed >= 1:
                scores.append((scheduler.retrievability_after(elapsed, card.stability), review.recalled))
        cards[review.kc_id] = scheduler.review(card, review.rating, review.t_ms)
    return scores


def fit_fsrs(reviews: Sequence[Review], params: MasteryParams, settings: Settings | None = None) -> Fit:
    settings = settings or Settings()
    if len(reviews) < settings.personal_fsrs_min_reviews:
        return Fit("fsrs", None, len(reviews), None, False, reason="too_few_reviews")
    default = scheduler_of(params)
    cut = _split(len(reviews), settings)

    def loss(weights: tuple[float, ...], upto: int | None = cut, since: int = 0) -> float:
        found = recall_scores(reviews[:upto], replace(default, weights=weights))
        recent = len(recall_scores(reviews[:since], replace(default, weights=weights))) if since else 0
        return log_loss(found[recent:])

    weights = tuple(default.weights)
    current = loss(weights)
    for _ in range(FSRS_PASSES):
        for index in FSRS_FITTED:
            for step in FSRS_STEPS:
                trial = tuple(w * step if i == index else w for i, w in enumerate(weights))
                value = loss(trial)
                if value < current * (1 - FSRS_STEP_GAIN):
                    weights, current = trial, value
    before = loss(tuple(default.weights), None, cut)
    after = loss(weights, None, cut)
    adopted = _adopt(before, after, settings)
    return Fit("fsrs", None, len(reviews), tuple(round(w, 6) for w in weights), adopted, before, after,
               "" if adopted else "no_gain")  # fmt: skip


# --- from the learner database ----------------------------------------------------------------------------------------


def _checked(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cursor = conn.execute(
        "SELECT item_ref, kc_ids, outcome, hints_used, max_rung, occurred_at FROM attempts"
        " WHERE outcome IN ('correct', 'incorrect', 'partial') ORDER BY occurred_at, attempt_id"
    )
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def graded_by_subject(conn: sqlite3.Connection, difficulty: Callable[[str | None], float]) -> dict[str, list[Graded]]:
    out: dict[str, list[Graded]] = {}
    for row in _checked(conn):
        kcs = json.loads(row["kc_ids"])
        if not kcs:
            continue
        total = sum(float(k["weight"]) for k in kcs) or 1.0
        weights = {str(k["kc_id"]): float(k["weight"]) / total for k in kcs}
        out.setdefault(subject_of(next(iter(weights))), []).append(
            Graded(weights, row["outcome"] == "correct", int(row["occurred_at"]), difficulty(row["item_ref"]))
        )
    return out


def reviews_of(conn: sqlite3.Connection, params: MasteryParams) -> list[Review]:
    """Each KC's first checked opportunity of a local day, rated as TABLE LRN-14a rates it without the time rows."""
    seen: set[tuple[str, int]] = set()
    out = []
    for row in _checked(conn):
        t = int(row["occurred_at"])
        rating = ("again" if row["outcome"] == "incorrect" or (row["max_rung"] or 0) >= 5
                  else "hard" if row["outcome"] == "partial" or row["hints_used"] else "good")  # fmt: skip
        for kc in json.loads(row["kc_ids"]):
            key = (str(kc["kc_id"]), params.local_day(t))
            if key not in seen:
                seen.add(key)
                out.append(Review(key[0], t, rating))
    return out


def personal_fits(conn: sqlite3.Connection, params: MasteryParams, difficulty: Callable[[str | None], float],
                  settings: Settings | None = None) -> list[Fit]:  # fmt: skip
    """Every personal fit the student's learner database supports now: one learning rate per subject and the FSRS
    weights. Run offline in idle time; it reads the projection and writes nothing."""
    settings = settings or Settings()
    fits = [fit_learning_rate(rows, params, settings, subject)
            for subject, rows in sorted(graded_by_subject(conn, difficulty).items())]  # fmt: skip
    return [*fits, fit_fsrs(reviews_of(conn, params), params, settings)]
