"""The mastery model: weighted observations, the pfa_elo update, the BKT baseline and the KC state function.

Pure functions over plain values, so the projection, the reference implementations in the tests and the offline
fitting code share one definition: the observation rows and weight modifiers of LRN-06 (TABLE LRN-06a, LRN-06b), the
pfa_elo update with decay (LRN-07), credit and blame across KCs (LRN-08), implied evidence along prerequisites
(LRN-10), the four-parameter BKT path of the LRN-11 bake-off, and the ordered state rules (LRN-09).

Every number is a configuration key (`agent_core.config.keys`); `MasteryParams` holds the ones the projections fold
with, so their canonical form is hashed into each projection's state (LRN-02) and a change rebuilds them.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from agent_core.config.keys import Settings

DAY_MS = 86_400_000
SUBJECTS = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}


def _default(name: str) -> Any:
    """A parameter's default: the configuration key's declared default, never a second literal."""
    return Settings.model_fields[name].default


def _key(name: str) -> Any:
    return field(default=_default(name))


@dataclass(frozen=True)
class MasteryParams:
    """The mastery parameter file: its canonical form is hashed into every projection's state (LRN-02).

    Fields named after configuration keys take that key's value (`from_settings`); `kc_overrides` holds the fitted
    per-KC values a pack's mastery-params file ships (LRN-11) as (kc_id, beta, gamma, rho) rows."""

    # LRN-06: observation weights and modifiers
    obs_bottom_out_w: float = _key("obs_bottom_out_w")
    obs_terminal_incorrect_w: float = _key("obs_terminal_incorrect_w")
    obs_partial_success_w: float = _key("obs_partial_success_w")
    obs_recovered_success_w: tuple[float, ...] = _key("obs_recovered_success_w")
    obs_hinted_first_correct_w: tuple[float, ...] = _key("obs_hinted_first_correct_w")
    obs_mode_check_working: float = _key("obs_mode_check_working")
    obs_mode_learn: float = _key("obs_mode_learn")
    obs_first_correct_w: float = _key("obs_first_correct_w")
    obs_recovered_failure_w: float = _key("obs_recovered_failure_w")
    obs_abandoned_w: float = _key("obs_abandoned_w")
    diagnostic_not_learned_weight: float = _key("diagnostic_not_learned_weight")
    obs_fast_seconds_min: float = _key("obs_fast_seconds_min")
    obs_fast_median_share: float = _key("obs_fast_median_share")
    obs_fast_wrong: float = _key("obs_fast_wrong")
    obs_fast_correct_mcq: float = _key("obs_fast_correct_mcq")
    obs_slow_median_multiple: float = _key("obs_slow_median_multiple")
    obs_slow_correct: float = _key("obs_slow_correct")
    obs_diagnoser_clip: tuple[float, float] = _key("obs_diagnoser_clip")
    # LRN-07, LRN-09, LRN-10, ASM-14
    mastery_half_life_days: float = _key("mastery_half_life_days")
    mastery_k_a: float = _key("mastery_k_a")
    mastery_k_c: float = _key("mastery_k_c")
    mastery_beta_default: float = _key("mastery_beta_default")
    mastery_gamma_default: float = _key("mastery_gamma_default")
    mastery_rho_default: float = _key("mastery_rho_default")
    mastery_ready_p: float = _key("mastery_ready_p")
    mastery_ready_n_eff: float = _key("mastery_ready_n_eff")
    mastery_review_retrievability: float = _key("mastery_review_retrievability")
    mastery_implied_w: float = _key("mastery_implied_w")
    mastery_implied_depth: int = _key("mastery_implied_depth")
    mastery_check_retry_days: float = _key("mastery_check_retry_days")
    # LRN-11: the model and the BKT baseline
    mastery_model: str = _key("mastery_model")
    bkt_p_init: float = _key("bkt_p_init")
    bkt_p_transit: float = _key("bkt_p_transit")
    bkt_p_slip: float = _key("bkt_p_slip")
    bkt_p_guess: float = _key("bkt_p_guess")
    # LRN-13, LRN-14: the review scheduler and the rating table
    fsrs_desired_retention: float = _key("fsrs_desired_retention")
    fsrs_weights: tuple[float, ...] = _key("fsrs_weights")
    fsrs_maximum_interval_days: int = _key("fsrs_maximum_interval_days")
    rating_easy_time_share: float = _key("rating_easy_time_share")
    rating_easy_min_mastery: float = _key("rating_easy_min_mastery")
    rating_hard_time_multiple: float = _key("rating_hard_time_multiple")
    learner_day_offset_minutes: int = _key("learner_day_offset_minutes")
    # LRN-17, LRN-26: fading and the stuck cap, kept on the KC row
    fading_max_stage: int = _key("fading_max_stage")
    stuck_cap_opportunities: int = _key("stuck_cap_opportunities")
    stuck_cap_mastery: float = _key("stuck_cap_mastery")
    stuck_cap_sessions: int = _key("stuck_cap_sessions")
    stuck_revisit_days: float = _key("stuck_revisit_days")
    # LRN-16, LRN-33, LRN-27: engagement signals, streaks and freezes
    frustration_half_life_events: float = _key("frustration_half_life_events")
    confidence_bias_window: int = _key("confidence_bias_window")
    streak_min_minutes: float = _key("streak_min_minutes")
    streak_weekly_freezes: int = _key("streak_weekly_freezes")
    streak_max_freezes: int = _key("streak_max_freezes")
    support_min_days_per_week: int = _key("support_min_days_per_week")
    support_weeks: int = _key("support_weeks")
    support_weekly_freezes: int = _key("support_weekly_freezes")
    # LRN-31: the items that complete a plan block
    planner_main_items: int = _key("planner_main_items")
    planner_mixed_items: int = _key("planner_mixed_items")
    # LRN-11: fitted per-KC values from a pack's mastery-params file
    kc_overrides: tuple[tuple[str, float, float, float], ...] = ()

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> MasteryParams:
        names = [f.name for f in fields(cls) if f.name in Settings.model_fields]
        return cls(**{name: getattr(settings, name) for name in names}, **overrides)

    def canonical(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    def digest(self, content: str = "") -> str:
        """sha256 of the parameter file and the pinned content it is applied with."""
        return hashlib.sha256(self.canonical() + b"\n" + content.encode()).hexdigest()

    def kc_values(self, kc_id: str) -> tuple[float, float, float]:
        """(beta, gamma, rho) of a KC: its fitted values when the pack has them, else the defaults (LRN-07)."""
        for kc, beta, gamma, rho in self.kc_overrides:
            if kc == kc_id:
                return beta, gamma, rho
        return self.mastery_beta_default, self.mastery_gamma_default, self.mastery_rho_default

    def local_day(self, t_ms: int) -> int:
        """The student's local day number of a replay time (days since the epoch, shifted by the configured offset)."""
        return (t_ms + self.learner_day_offset_minutes * 60_000) // DAY_MS


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def subject_of(kc_id: str) -> str:
    return SUBJECTS[kc_id.split(".")[1]]


# --- observations (TABLE LRN-06a, LRN-06b) --------------------------------------------------------------------------

CLOSES = ("verdict", "abandoned", "not_learned")


class InvalidClose(ValueError):
    """An opportunity closed without a verdict or an explicit abandonment or not-learned reason (LRN-06): the engine
    invariant fails instead of silently producing zero evidence."""


@dataclass(frozen=True)
class Opportunity:
    """A closed opportunity: its graded outcomes in order, the highest hint rung shown and the mode, how it closed
    (`verdict`, `abandoned` or `not_learned`), and what the time and multiple-choice modifiers read: the final graded
    attempt's time, the item's median time and, for a multiple-choice item, its number of options."""

    mode: str
    outcomes: tuple[str, ...]
    max_rung: int
    kc_source: str = "pack"
    kc_confidence: float | None = None
    close: str = "verdict"
    time_ms: int | None = None
    median_ms: int | None = None
    options: int | None = None

    @property
    def graded(self) -> tuple[str, ...]:
        return tuple(o for o in self.outcomes if o != "unverifiable")


def observation_row(op: Opportunity, p: MasteryParams) -> tuple[str, list[tuple[int, float]]]:
    """The first matching row of TABLE LRN-06a (its name and observations), before the modifiers."""
    if op.close not in CLOSES:
        raise InvalidClose(f"unknown close reason {op.close!r}")
    final = op.outcomes[-1] if op.outcomes else None
    graded = op.graded
    if op.close == "verdict" and final is None:
        raise InvalidClose("a verdict without a graded attempt")
    if op.mode == "solution" or (op.close == "verdict" and final == "unverifiable"):
        return "excluded", []
    if op.max_rung >= 5:
        return "bottom_out", [(0, p.obs_bottom_out_w)]
    if op.close == "not_learned":
        return "not_learned", [(0, p.diagnostic_not_learned_weight)]
    if op.close == "abandoned":
        return ("abandoned_after_attempt", [(0, p.obs_abandoned_w)]) if graded else ("abandoned_unattempted", [])
    if final == "incorrect":
        return "terminal_incorrect", [(0, p.obs_terminal_incorrect_w)]
    if final == "partial":
        return "partial", [(1, p.obs_partial_success_w)]
    rung = min(op.max_rung, len(p.obs_recovered_success_w) - 1)
    if any(o in ("incorrect", "partial") for o in graded[:-1]):
        return "recovered", [(0, p.obs_recovered_failure_w), (1, p.obs_recovered_success_w[rung])]
    if op.max_rung >= 1:
        return "hinted_first_correct", [(1, p.obs_hinted_first_correct_w[min(op.max_rung, 4) - 1])]
    return "first_correct", [(1, p.obs_first_correct_w)]


def fast_limit_ms(op: Opportunity, p: MasteryParams) -> float:
    """max(3 s, 0.2 x item median): a response faster than this is a fast guess when wrong (LRN-06b, LRN-19)."""
    floor = p.obs_fast_seconds_min * 1000
    return max(floor, p.obs_fast_median_share * op.median_ms) if op.median_ms else floor


def fast_guess(op: Opportunity, p: MasteryParams) -> bool:
    """A wrong answer under the fast-guess time: the LRN-19 signal (it never reduces a score beyond LRN-06)."""
    graded = op.graded
    return bool(graded) and graded[-1] == "incorrect" and op.time_ms is not None and op.time_ms < fast_limit_ms(op, p)


def modifiers(op: Opportunity, p: MasteryParams) -> list[tuple[str, float]]:
    """Every TABLE LRN-06b modifier that applies, by name; the weight is multiplied by each."""
    out: list[tuple[str, float]] = []
    if op.mode == "check_working":
        out.append(("mode_check_working", p.obs_mode_check_working))
    elif op.mode == "learn":
        out.append(("mode_learn", p.obs_mode_learn))
    graded = op.graded
    last = graded[-1] if graded else None
    if op.time_ms is not None and last is not None:
        fast = op.time_ms < fast_limit_ms(op, p)
        if fast and last == "incorrect":
            out.append(("fast_wrong", p.obs_fast_wrong))
        if last == "correct" and op.options:
            out.append(("fast_correct_mcq", p.obs_fast_correct_mcq) if fast else ("mcq", 1 - 1 / op.options))
        if last == "correct" and op.median_ms and op.time_ms > p.obs_slow_median_multiple * op.median_ms:
            out.append(("slow_correct", p.obs_slow_correct))
    if op.kc_source == "diagnoser" and op.kc_confidence is not None:
        low, high = p.obs_diagnoser_clip
        out.append(("diagnoser_confidence", min(high, max(low, op.kc_confidence))))
    return out


def observations(op: Opportunity, p: MasteryParams) -> list[tuple[int, float]]:
    """The weighted observations (y, w) of one closed opportunity: the first matching row, then every modifier."""
    _, rows = observation_row(op, p)
    multiplier = 1.0
    for _, factor in modifiers(op, p):
        multiplier *= factor
    return [(y, w * multiplier) for y, w in rows]


# --- pfa_elo (LRN-07, LRN-08, LRN-10) -------------------------------------------------------------------------------


@dataclass
class KcEvidence:
    succ_w: float = 0.0
    fail_w: float = 0.0
    implied_w: float = 0.0
    decayed_at: int | None = None  # replay time (ms) the counts were last decayed to

    @property
    def n_eff(self) -> float:
        return max(0.0, self.succ_w + self.fail_w - self.implied_w)

    def decay_to(self, t: int, p: MasteryParams) -> None:
        """Decay every count by the same factor to replay time `t` (never forwards past a later decay)."""
        if self.decayed_at is not None:
            elapsed = max(0, t - self.decayed_at)
            factor = 2 ** (-elapsed / (p.mastery_half_life_days * DAY_MS))
            self.succ_w *= factor
            self.fail_w *= factor
            self.implied_w *= factor
        self.decayed_at = t if self.decayed_at is None else max(self.decayed_at, t)


@dataclass
class Ability:
    theta: float = 0.0
    n: float = 0.0


def kc_probability(theta: float, ev: KcEvidence, p: MasteryParams, b_ref: float = 0.0, kc_id: str = "") -> float:
    beta, gamma, rho = p.kc_values(kc_id)
    return sigmoid(theta + beta + gamma * ev.succ_w + rho * ev.fail_w - b_ref)


def item_probability(
    theta: float, weights: Mapping[str, float], evidence: Mapping[str, KcEvidence], p: MasteryParams, b_item: float
) -> float:
    eta = theta - b_item
    for kc, q in weights.items():
        ev = evidence[kc]
        beta, gamma, rho = p.kc_values(kc)
        eta += q * (beta + gamma * ev.succ_w + rho * ev.fail_w)
    return sigmoid(eta)


def normalised(weights: Sequence[tuple[str, float]]) -> dict[str, float]:
    total = sum(w for _, w in weights)
    out: dict[str, float] = {}
    for kc, w in weights:
        out[kc] = out.get(kc, 0.0) + w / total
    return out


@dataclass
class Update:
    """What one observation changed: direct weight per KC and implied weight per KC (for the projection rows)."""

    direct: dict[str, float] = field(default_factory=dict)
    implied: dict[str, float] = field(default_factory=dict)


def blame(
    w: float,
    weights: Mapping[str, float],
    first_error_kc: str | None,
    probabilities: Mapping[str, float],
) -> dict[str, float]:
    """A failure's weight per KC (LRN-08): all of it to the Checker's first-error KC when that KC is tagged, else
    split in proportion to 1 - P_k, so the weakest KC takes most blame."""
    if first_error_kc is not None and first_error_kc in weights:
        return {first_error_kc: w}
    gaps = {kc: 1 - probabilities[kc] for kc in weights}
    total = sum(gaps.values())
    return {kc: w * (gap / total if total > 0 else 1 / len(gaps)) for kc, gap in gaps.items()}


def observe(
    *,
    y: int,
    w: float,
    t: int,
    weights: Mapping[str, float],
    first_error_kc: str | None,
    ability: Ability,
    evidence: dict[str, KcEvidence],
    p: MasteryParams,
    b_item: float = 0.0,
    b_ref: Mapping[str, float] | None = None,
    prerequisites: Mapping[str, Sequence[str]] | None = None,
    dependents: Mapping[str, Sequence[str]] | None = None,
) -> Update:
    """Apply one observation (y, w) at replay time t to the subject ability and the KC evidence, in place."""
    refs = b_ref or {}
    for kc in weights:
        evidence.setdefault(kc, KcEvidence()).decay_to(t, p)
    p_hat = item_probability(ability.theta, weights, evidence, p, b_item)
    ability.theta += p.mastery_k_a / (1 + p.mastery_k_c * ability.n) * w * (y - p_hat)
    ability.n += w
    update = Update()
    if y == 1:
        for kc in weights:
            evidence[kc].succ_w += w
            update.direct[kc] = w
    else:
        probabilities = {kc: kc_probability(ability.theta, evidence[kc], p, refs.get(kc, 0.0), kc) for kc in weights}
        for kc, share in blame(w, weights, first_error_kc, probabilities).items():
            evidence[kc].fail_w += share
            update.direct[kc] = share
    # Implied evidence: a success flows to prerequisites, a failure to dependents, halving per level (LRN-10).
    graph = prerequisites if y == 1 else dependents
    if graph:
        frontier = [kc for kc, share in update.direct.items() if share > 0]
        seen = set(frontier)
        for depth in range(1, p.mastery_implied_depth + 1):
            weight = p.mastery_implied_w * 0.5 ** (depth - 1)
            following: list[str] = []
            for kc in frontier:
                for other in graph.get(kc, ()):
                    if other in seen:
                        continue
                    seen.add(other)
                    following.append(other)
                    ev = evidence.setdefault(other, KcEvidence())
                    ev.decay_to(t, p)
                    if y == 1:
                        ev.succ_w += weight
                    else:
                        ev.fail_w += weight
                    ev.implied_w += weight
                    update.implied[other] = update.implied.get(other, 0.0) + weight
            frontier = following
    return update


# --- the BKT baseline (LRN-11) ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bkt:
    """Standard four-parameter BKT: P(L1), P(T), P(S), P(G)."""

    p_init: float
    p_transit: float
    p_slip: float
    p_guess: float

    @classmethod
    def from_params(cls, p: MasteryParams) -> Bkt:
        return cls(p.bkt_p_init, p.bkt_p_transit, p.bkt_p_slip, p.bkt_p_guess)

    def predict(self, p_learned: float) -> float:
        """P(correct) before an observation."""
        return p_learned * (1 - self.p_slip) + (1 - p_learned) * self.p_guess

    def update(self, p_learned: float, correct: bool) -> float:
        """P(L|obs), then P(L') = P(L|obs) + (1 - P(L|obs)) * P(T)."""
        if correct:
            num = p_learned * (1 - self.p_slip)
            den = num + (1 - p_learned) * self.p_guess
        else:
            num = p_learned * self.p_slip
            den = num + (1 - p_learned) * (1 - self.p_guess)
        posterior = num / den if den > 0 else p_learned
        return posterior + (1 - posterior) * self.p_transit

    def trace(self, outcomes: Sequence[bool]) -> list[float]:
        """The predicted P(correct) before each observation of one student's sequence on one KC."""
        p_learned, out = self.p_init, []
        for correct in outcomes:
            out.append(self.predict(p_learned))
            p_learned = self.update(p_learned, correct)
        return out


# --- KC states (LRN-09) ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StateInputs:
    direct_observations: int
    p_mastery: float
    n_eff: float
    confirmed: bool = False
    failed_since_confirmed: bool = False
    low_retrievability: bool = False
    deferral_active: bool = False
    retry_lock_active: bool = False


def kc_state(s: StateInputs, p: MasteryParams) -> str:
    """Exactly one state, by the ordered rules."""
    if s.direct_observations == 0:
        return "not_started"
    if s.deferral_active:
        return "moved_on"
    if s.confirmed and (s.failed_since_confirmed or s.low_retrievability):
        return "needs_review"
    if s.confirmed:
        return "mastered"
    if s.retry_lock_active:
        return "learning"
    if s.p_mastery >= p.mastery_ready_p and s.n_eff >= p.mastery_ready_n_eff:
        return "ready_to_check"
    return "learning"


# TABLE LRN-09a student labels, and the LRN-34 bands.
STATE_LABELS = {
    "not_started": "Not started",
    "learning": "Learning",
    "ready_to_check": "Nearly secure",
    "mastered": "Secure",
    "needs_review": "Refresh due",
    "moved_on": "Coming back to this",
}
BANDS = {
    "not_started": "not_started",
    "learning": "learning",
    "ready_to_check": "learning",
    "moved_on": "learning",
    "mastered": "secure",
    "needs_review": "secure",
}
