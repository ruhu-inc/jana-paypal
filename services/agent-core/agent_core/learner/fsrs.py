"""Review scheduling: one FSRS card per KC (LRN-13) and the verdict-to-rating table (LRN-14).

The scheduler is the FSRS-6 algorithm as py-fsrs (MIT) implements it, configured the way this product uses it: no
intra-day learning or relearning steps (a KC is reviewed at most once per local day) and no interval fuzzing (due dates
must be the same on every device, D19). It is the one authoritative scheduler in the core; `lrn13_matches_py_fsrs`
compares it with py-fsrs itself whenever that library is installed. The weights, the desired retention and the longest
interval are configuration keys (the default FSRS-6 weights until personal calibration, LRN-12).

No interval ends after an exam: `exam_capped_due` moves a due date that falls after the exam into the final window, so
every mastered KC of an examined subject is due at least once in its final 14 days. The exam date is profile data,
not an event, so the cap is applied when cards are read, never stored in the projection.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from agent_core.learner.mastery import MasteryParams

DAY_MS = 86_400_000
STABILITY_MIN = 0.001
MIN_DIFFICULTY, MAX_DIFFICULTY = 1.0, 10.0
RATINGS = {"again": 1, "hard": 2, "good": 3, "easy": 4}


@dataclass(frozen=True)
class Card:
    """A KC's review card (TABLE LRN-03a): FSRS memory state and the due date, in ms since the epoch."""

    state: str  # learning | review | relearning
    stability: float
    difficulty: float
    due_ms: int
    reps: int = 0
    lapses: int = 0
    last_review_ms: int | None = None


@dataclass(frozen=True)
class Scheduler:
    weights: tuple[float, ...]
    desired_retention: float
    maximum_interval: int

    def __post_init__(self) -> None:
        if len(self.weights) != 21:
            raise ValueError("FSRS-6 takes 21 weights")

    @property
    def decay(self) -> float:
        return -self.weights[20]

    @property
    def factor(self) -> float:
        return math.pow(0.9, 1 / self.decay) - 1

    # --- the FSRS-6 formulas ------------------------------------------------------------------------------------------

    def retrievability_after(self, elapsed_days: float, stability: float) -> float:
        return math.pow(1 + self.factor * elapsed_days / stability, self.decay)

    def retrievability(self, card: Card, now_ms: int) -> float:
        """The probability of recall now; 0 for a card that was never reviewed."""
        if card.last_review_ms is None:
            return 0.0
        elapsed = max(0, (now_ms - card.last_review_ms) // DAY_MS)
        return self.retrievability_after(elapsed, card.stability)

    def next_interval(self, stability: float) -> int:
        interval = (stability / self.factor) * (math.pow(self.desired_retention, 1 / self.decay) - 1)
        return int(min(max(round(interval), 1), self.maximum_interval))

    def initial_stability(self, rating: int) -> float:
        return max(self.weights[rating - 1], STABILITY_MIN)

    def initial_difficulty(self, rating: int, clamp: bool = True) -> float:
        value = self.weights[4] - math.exp(self.weights[5] * (rating - 1)) + 1
        return min(max(value, MIN_DIFFICULTY), MAX_DIFFICULTY) if clamp else value

    def next_difficulty(self, difficulty: float, rating: int) -> float:
        w = self.weights
        delta = -(w[6] * (rating - 3))
        damped = difficulty + (10.0 - difficulty) * delta / 9.0
        reverted = w[7] * self.initial_difficulty(4, clamp=False) + (1 - w[7]) * damped
        return min(max(reverted, MIN_DIFFICULTY), MAX_DIFFICULTY)

    def short_term_stability(self, stability: float, rating: int) -> float:
        w = self.weights
        increase = math.exp(w[17] * (rating - 3 + w[18])) * math.pow(stability, -w[19])
        if rating >= 3:
            increase = max(increase, 1.0)
        return max(stability * increase, STABILITY_MIN)

    def forget_stability(self, difficulty: float, stability: float, r: float) -> float:
        w = self.weights
        long_term = (w[11] * math.pow(difficulty, -w[12]) * (math.pow(stability + 1, w[13]) - 1)
                     * math.exp((1 - r) * w[14]))  # fmt: skip
        short_term = stability / math.exp(w[17] * w[18])
        return max(min(long_term, short_term), STABILITY_MIN)

    def recall_stability(self, difficulty: float, stability: float, r: float, rating: int) -> float:
        w = self.weights
        hard_penalty = w[15] if rating == 2 else 1.0
        easy_bonus = w[16] if rating == 4 else 1.0
        grow = math.exp(w[8]) * (11 - difficulty) * math.pow(stability, -w[9]) * (math.exp((1 - r) * w[10]) - 1)
        return max(stability * (1 + grow * hard_penalty * easy_bonus), STABILITY_MIN)

    # --- one review --------------------------------------------------------------------------------------------------

    def review(self, card: Card | None, rating: str, now_ms: int) -> Card:
        """The card after one rated review at `now_ms` (a new card when `card` is None)."""
        g = RATINGS[rating]
        if card is None or card.last_review_ms is None:
            stability, difficulty = self.initial_stability(g), self.initial_difficulty(g)
            reps, lapses = 0, 0
        else:
            elapsed = max(0, (now_ms - card.last_review_ms) // DAY_MS)
            if elapsed < 1:
                stability = self.short_term_stability(card.stability, g)
            else:
                r = self.retrievability_after(elapsed, card.stability)
                stability = (self.forget_stability(card.difficulty, card.stability, r) if g == 1
                             else self.recall_stability(card.difficulty, card.stability, r, g))  # fmt: skip
            difficulty = self.next_difficulty(card.difficulty, g)
            reps, lapses = card.reps, card.lapses + (g == 1 and card.state == "review")
        interval = self.next_interval(stability)
        return Card("review", stability, difficulty, now_ms + interval * DAY_MS, reps + 1, lapses, now_ms)


def scheduler_of(params: MasteryParams) -> Scheduler:
    """The scheduler a parameter set configures (`fsrs_*` keys)."""
    return Scheduler(tuple(params.fsrs_weights), params.fsrs_desired_retention, params.fsrs_maximum_interval_days)


def exam_capped_due(card: Card, exam_day_ms: int | None, window_days: int) -> int:
    """The due date as shown and planned: one that would fall after the exam is moved into its final window (never
    before the day after the last review), so no interval ends after the exam (LRN-13)."""
    if exam_day_ms is None or card.due_ms <= exam_day_ms:
        return card.due_ms
    floor = (card.last_review_ms if card.last_review_ms is not None else card.due_ms) + DAY_MS
    return min(max(exam_day_ms - window_days * DAY_MS, floor), exam_day_ms) if floor <= exam_day_ms else card.due_ms


# --- the rating table (TABLE LRN-14a) -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reviewed:
    """What the rating reads from the day's first reviewable opportunity on a KC."""

    outcomes: tuple[str, ...]  # graded outcomes in order
    max_rung: int
    close: str = "verdict"  # verdict | abandoned | not_learned
    time_ms: int | None = None
    median_ms: int | None = None
    p_mastery: float = 0.0


def review_rating(r: Reviewed, easy_time_share: float, easy_min_mastery: float, hard_time_multiple: float) -> str:
    """Again, Hard, Good or Easy by the first matching row of TABLE LRN-14a.

    A final `partial` is not a wrong answer: it rates Hard, as a correct answer reached with difficulty does."""
    graded = [o for o in r.outcomes if o != "unverifiable"]
    first = graded[0] if graded else None
    final = graded[-1] if graded else None
    if r.max_rung >= 5 or r.close in ("abandoned", "not_learned") or final in (None, "incorrect"):
        return "again"
    slow = r.time_ms is not None and r.median_ms is not None and r.time_ms > hard_time_multiple * r.median_ms
    if final == "partial" or r.max_rung >= 1 or first != "correct" or slow:
        return "hard"
    fast = r.time_ms is not None and r.median_ms is not None and r.time_ms < easy_time_share * r.median_ms
    return "easy" if fast and r.p_mastery >= easy_min_mastery else "good"


def replay(scheduler: Scheduler, reviews: Sequence[tuple[int, str]]) -> Card | None:
    """The card after a sequence of (time, rating) reviews: what a recorded review log schedules."""
    card: Card | None = None
    for at, rating in reviews:
        card = scheduler.review(card, rating, at)
    return card
