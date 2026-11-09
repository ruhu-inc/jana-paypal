"""Adaptation decided by code (LRN-15 to LRN-27): one pure function per policy of TABLE LRN-15a.

Each policy takes plain values (the learner model as `agent_core.learner.model` reads it, and the session signals
the engine holds) and returns a `Decision`: the policy name, the inputs it read and the output it chose. The engine
logs every decision in the turn trace (`policy_decision`, OBS-01) and the model only words the result. Nothing here
reads a clock, a database or the network, so the same inputs always give the same decision, offline.

Thresholds come from configuration keys (`agent_core.config.keys`, the `adaptation` section of the mastery-params
file); a policy is handed the `Settings` it reads them from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_core.config.keys import Settings

POLICIES = (
    "difficulty_targeting",
    "worked_example_fading",
    "hint_start_rung",
    "gaming_detection",
    "unprompted_help",
    "self_explanation",
    "representation",
    "prerequisite_detour",
    "interleaving",
    "exam_proximity",
    "stuck_cap",
    "support_level",
)
REPRESENTATIONS = ("diagram", "simpler_numbers", "analogy", "worked_example")
TIMED_MODES = ("mock", "mastery_check")


@dataclass(frozen=True)
class Decision:
    """One policy decision: what it read and what it chose (the `policy_decision` trace entry)."""

    policy: str
    inputs: dict[str, Any]
    output: dict[str, Any]

    def entry(self) -> dict[str, Any]:
        return {"policy": self.policy, "inputs": dict(self.inputs), "output": dict(self.output)}


# --- LRN-16: difficulty targeting ---


@dataclass(frozen=True)
class Candidate:
    """An eligible item (ASM-22 exposure rules already applied) with its predicted success."""

    item_id: str
    p_hat: float
    exposures: int = 0
    pack_order: int = 0


def target_success(support_extra: bool, frustration: float, target_offset: float, s: Settings) -> float:
    """0.825, or 0.85 with extra support or high frustration, moved by the student's tuned offset (LRN-16)."""
    base = s.adapt_target_supported if support_extra or frustration >= s.frustration_high else s.adapt_target
    offset = max(-s.adapt_tuning_max_offset, min(s.adapt_tuning_max_offset, target_offset))
    return base + offset


def difficulty_targeting(
    candidates: Sequence[Candidate], s: Settings, *, support_extra: bool = False, frustration: float = 0.0,
    target_offset: float = 0.0,
) -> Decision:  # fmt: skip
    """The candidate whose p_hat is nearest the target inside the band; ties by lower exposure, then pack order. With
    none inside the band, the nearest outside it, flagged `fallback` so the trace shows it."""
    target = target_success(support_extra, frustration, target_offset, s)
    low, high = s.adapt_band
    inside = [c for c in candidates if low <= c.p_hat <= high]
    pool = inside or list(candidates)

    def rank(c: Candidate) -> tuple[float, int, int, str]:
        return (round(abs(c.p_hat - target), 12), c.exposures, c.pack_order, c.item_id)

    chosen = min(pool, key=rank) if pool else None
    return Decision(
        "difficulty_targeting",
        {
            "target": target,
            "band": [low, high],
            "candidates": [[c.item_id, c.p_hat, c.exposures, c.pack_order] for c in candidates],
        },
        {
            "item_id": chosen.item_id if chosen else None,
            "p_hat": chosen.p_hat if chosen else None,
            "fallback": bool(chosen) and not inside,
        },
    )


def tuned_offset(realised: Sequence[float], target: float, offset: float, s: Settings) -> float:
    """The weekly offline job (LRN-16): when realised success stays outside target +/- tolerance for the configured
    weeks, the offset moves one step toward correction, within the bound. `realised` is one rate per week."""
    weeks = list(realised)[-s.adapt_tuning_weeks :]
    if len(weeks) < s.adapt_tuning_weeks:
        return offset
    if all(r > target + s.adapt_tuning_tolerance for r in weeks):
        offset -= s.adapt_tuning_step  # succeeding more than aimed for: aim at a lower predicted success
    elif all(r < target - s.adapt_tuning_tolerance for r in weeks):
        offset += s.adapt_tuning_step
    else:
        return offset
    return max(-s.adapt_tuning_max_offset, min(s.adapt_tuning_max_offset, offset))


# --- LRN-17: worked-example fading ---


def worked_example_mode(
    p_k: float, s: Settings, *, support_extra: bool = False, last_mode: str | None = None
) -> Decision:
    """Below the example threshold: the full worked example, then a faded one; between it and `problems_from`, faded
    examples alternate with problems; from there, problems only (examples on request). Extra support raises the
    example threshold (LRN-27)."""
    below = s.support_example_below if support_extra else s.example_full_below
    if p_k < below:
        mode = "faded_example" if last_mode == "worked_example" else "worked_example"
    elif p_k < s.example_problems_from:
        mode = "problem" if last_mode == "faded_example" else "faded_example"
    else:
        mode = "problem"
    return Decision("worked_example_fading", {"p_k": p_k, "example_below": below, "last_mode": last_mode},
                    {"mode": mode})  # fmt: skip


def fading_step(stage: int, *, unaided_correct_blank: bool, bottom_out: bool, max_stage: int) -> int:
    """`fading_stage` advances one blank only after the previous blank was completed correctly without a hint, and
    goes back one after a bottom-out."""
    if bottom_out:
        return max(0, stage - 1)
    return min(stage + 1, max_stage) if unaided_correct_blank else stage


# --- LRN-18: hint starting rung ---


def hint_start_rung(p_k: float, recent_max_rungs: Sequence[int], s: Settings) -> Decision:
    """Rung 2 (concept) when P_k is low or at least half of the last opportunities on the KC needed rung 3 or more;
    otherwise rung 1 (orient)."""
    window = list(recent_max_rungs)[-s.hint_dependence_window :]
    dependent = bool(window) and sum(r >= s.hint_dependence_rung for r in window) >= s.hint_dependence_share * len(
        window
    )
    rung = 2 if p_k < s.hint_concept_below or dependent else 1
    return Decision("hint_start_rung", {"p_k": p_k, "recent_max_rungs": window}, {"rung": rung,
                                                                                   "dependent": dependent})  # fmt: skip


# --- LRN-19: gaming detection ---


@dataclass(frozen=True)
class GamingState:
    """Per step: when hints were requested, the run of fast wrong answers, whether hints are paused, and the flags
    raised this session."""

    last_hint_s: float | None = None
    fast_wrong_run: int = 0
    paused: bool = False
    session_flags: int = 0


def gaming_on_hint(state: GamingState, at_s: float, s: Settings) -> tuple[GamingState, Decision]:
    """A hint request: refused while hints are paused; two requests within the gap raise the flag and pause hints
    until an attempt arrives (the Coach gives one line of help-seeking coaching, rule `hint_burst_ask_attempt`)."""
    if state.paused:
        out = state
        decision = {"allow_hint": False, "raised": False, "coach": "hint_burst_ask_attempt"}
    elif state.last_hint_s is not None and at_s - state.last_hint_s <= s.gaming_hint_gap_s:
        out = GamingState(at_s, state.fast_wrong_run, True, state.session_flags + 1)
        decision = {"allow_hint": False, "raised": True, "coach": "hint_burst_ask_attempt"}
    else:
        out = GamingState(at_s, state.fast_wrong_run, False, state.session_flags)
        decision = {"allow_hint": True, "raised": False, "coach": None}
    decision["suggest_break"] = out.session_flags >= s.gaming_break_flags
    return out, Decision(
        "gaming_detection",
        {"event": "hint_request", "at_s": at_s, "last_hint_s": state.last_hint_s, "paused": state.paused},
        decision,
    )


def gaming_on_attempt(state: GamingState, *, wrong: bool, fast: bool, s: Settings) -> tuple[GamingState, Decision]:
    """An attempt resumes paused hints; the configured run of fast wrong answers on one step raises the flag."""
    run = state.fast_wrong_run + 1 if wrong and fast else 0
    raised = run >= s.gaming_fast_wrong_run
    flags = state.session_flags + raised
    out = GamingState(state.last_hint_s, 0 if raised else run, False, flags)
    return out, Decision("gaming_detection", {"event": "attempt", "wrong": wrong, "fast": fast,
                                              "run_before": state.fast_wrong_run},
                         {"raised": raised, "hints_resumed": state.paused,
                          "suggest_break": flags >= s.gaming_break_flags})  # fmt: skip


# --- LRN-20: unprompted help ---


def unprompted_offer(
    *, idle_s: float, focused: bool, consecutive_wrong: int, offered_on_step: bool, mode: str, preference_on: bool,
    has_attempt: bool, s: Settings,
) -> Decision:  # fmt: skip
    """Offer (never show) a hint once per step after the idle time with the window focused, or after two wrong
    attempts in a row; never in timed modes or with the preference off. Accepting gives the hint card at the computed
    rung, or the single PED-05 entry question when the step has no attempt yet."""
    trigger = ("idle" if focused and idle_s >= s.offer_idle_s
               else "repeated_errors" if consecutive_wrong >= s.offer_after_wrong else None)  # fmt: skip
    suppressed = mode in TIMED_MODES or not preference_on or offered_on_step
    offer = trigger is not None and not suppressed
    return Decision("unprompted_help",
                    {"idle_s": idle_s, "focused": focused, "consecutive_wrong": consecutive_wrong,
                     "offered_on_step": offered_on_step, "mode": mode, "preference_on": preference_on},
                    {"offer": offer, "trigger": trigger if offer else None,
                     "on_accept": ("hint" if has_attempt else "entry_question") if offer else None})  # fmt: skip


# --- LRN-21: self-explanation cadence ---


def self_explanation_due(
    *, steps_since_prompt: int | None, p_k: float, previous_prompted: bool, previous_worked_example: bool, mode: str,
    preference_on: bool = True, s: Settings,
) -> Decision:  # fmt: skip
    """At most once per `self_explanation_every_steps` completed steps, only in the band, never on two consecutive
    steps, never right after a worked-example step and never in timed modes."""
    low, high = s.self_explanation_band
    spaced = steps_since_prompt is None or steps_since_prompt >= s.self_explanation_every_steps
    due = (spaced and low <= p_k < high and not previous_prompted and not previous_worked_example
           and mode not in TIMED_MODES and preference_on)  # fmt: skip
    return Decision("self_explanation", {"steps_since_prompt": steps_since_prompt, "p_k": p_k,
                                         "previous_prompted": previous_prompted,
                                         "previous_worked_example": previous_worked_example, "mode": mode},
                    {"due": due})  # fmt: skip


# --- LRN-22: representation switching ---


def next_representation(
    posteriors: Mapping[str, tuple[float, float]],
    used: Sequence[str],
    diagram_available: bool,
    signals: int,
    s: Settings,
) -> Decision:
    """After the trigger count of confusion signals or wrong attempts following an explanation, the representation
    with the highest Beta posterior mean (prior Beta(1,1)), skipping ones used on the step and a diagram with no
    catalogue entry; ties keep the listed order."""
    choice = None
    if signals >= s.representation_trigger:
        ranked = sorted(
            (r for r in REPRESENTATIONS if r not in used and (r != "diagram" or diagram_available)),
            key=lambda r: -(posteriors.get(r, (1.0, 1.0))[0] / sum(posteriors.get(r, (1.0, 1.0)))),
        )
        choice = ranked[0] if ranked else None
    return Decision("representation", {"signals": signals, "used": list(used),
                                       "diagram_available": diagram_available,
                                       "posteriors": {k: list(v) for k, v in sorted(posteriors.items())}},
                    {"representation": choice})  # fmt: skip


def representation_outcome(
    posteriors: Mapping[str, tuple[float, float]], representation: str, success: bool
) -> dict[str, tuple[float, float]]:
    """Success within the next attempts adds to alpha, failure to beta."""
    out = {r: posteriors.get(r, (1.0, 1.0)) for r in REPRESENTATIONS}
    alpha, beta = out[representation]
    out[representation] = (alpha + 1, beta) if success else (alpha, beta + 1)
    return out


# --- LRN-23: prerequisite detours ---


@dataclass(frozen=True)
class SavedPosition:
    """Where a detour returns to: the original problem, its step and its board."""

    kc_id: str
    problem_ref: str
    step_index: int
    board: Mapping[str, Any] = field(default_factory=dict)


def detour_offer(
    kc_id: str,
    failures: int,
    prerequisites: Mapping[str, float],
    declined: Sequence[str],
    stack: Sequence[SavedPosition],
    s: Settings,
) -> Decision:
    """After the configured failures on a KC, a detour to its weakest prerequisite below the bar: a micro-lesson and
    two items, about five minutes, the original problem kept on a return stack. Not offered again for a KC the student
    declined it on this session, and refused beyond the stack depth."""
    weak = sorted((p, j) for j, p in prerequisites.items() if p < s.detour_prereq_below)
    target = weak[0][1] if weak else None
    reason = None
    if failures < s.detour_failures:
        reason = "not_enough_failures"
    elif target is None:
        reason = "no_weak_prerequisite"
    elif kc_id in declined:
        reason = "declined_this_session"
    elif len(stack) >= s.detour_max_depth:
        reason = "stack_full"
    offer = reason is None
    return Decision("prerequisite_detour", {"kc_id": kc_id, "failures": failures,
                                            "prerequisites": dict(sorted(prerequisites.items())),
                                            "declined": list(declined), "depth": len(stack)},
                    {"offer": offer, "prerequisite_kc_id": target if offer else None, "refused": reason,
                     "items": s.detour_items if offer else 0,
                     "minutes": s.detour_minutes if offer else 0})  # fmt: skip


def detour_push(stack: Sequence[SavedPosition], position: SavedPosition, s: Settings) -> tuple[SavedPosition, ...]:
    if len(stack) >= s.detour_max_depth:
        raise ValueError("a third nested detour is refused (LRN-23)")
    return (*stack, position)


def detour_pop(stack: Sequence[SavedPosition]) -> tuple[SavedPosition, tuple[SavedPosition, ...]]:
    """The position the finished detour returns to (the same step and board), and the rest of the stack."""
    return stack[-1], tuple(stack[:-1])


# --- LRN-24, LRN-25: block, then interleave; exam proximity ---


def practice_sequence(
    kcs: Mapping[str, float], items_per_kc: int, confusable: Mapping[str, Sequence[str]], kinds: Mapping[str, str],
    s: Settings, *, phase: str = "normal",
) -> Decision:  # fmt: skip
    """A practice set: each KC below `block_until` (and every recall-type KC) in its own block; the rest mixed with
    the KCs they are confusable with, no more than `interleave_max_run` consecutive items from one KC. In the
    countdown phase the mixed share of the set rises to `countdown_interleave_share` (LRN-25)."""
    blocked = sorted(k for k, p in kcs.items() if p < s.block_until or kinds.get(k) in RECALL_KINDS)
    mixable = sorted(k for k in kcs if k not in blocked)
    groups: list[list[str]] = []
    seen: set[str] = set()
    for kc in mixable:
        if kc in seen:
            continue
        group = [kc] + [o for o in confusable.get(kc, ()) if o in mixable and o not in seen and o != kc]
        seen.update(group)
        groups.append(sorted(set(group)))
    sequence: list[str] = []
    blocks = [[kc] * items_per_kc for kc in blocked]
    mixed_sets = [interleave({kc: items_per_kc for kc in group}, s.interleave_max_run) for group in groups]
    if phase == "countdown" and mixed_sets and blocks:  # at least half the items mixed (LRN-25)
        mixed_total = sum(len(m) for m in mixed_sets)
        while blocks and mixed_total < s.countdown_interleave_share * (mixed_total + sum(map(len, blocks))):
            blocks.pop()
    for block in blocks:
        sequence += block
    for mixed in mixed_sets:
        sequence += mixed
    return Decision("interleaving", {"kcs": dict(sorted(kcs.items())), "phase": phase},
                    {"blocked": blocked, "mixed_groups": groups, "sequence": sequence})  # fmt: skip


RECALL_KINDS = ("factual",)  # the pack's KC kinds that are vocabulary-type recall (LRN-24 "kind: recall")


def interleave(counts: Mapping[str, int], max_run: int) -> list[str]:
    """Items from several KCs with no run longer than `max_run` from one KC, when the counts allow it: the KC with the
    most items left goes next unless it just reached the run limit (ties by KC id)."""
    left = dict(counts)
    out: list[str] = []
    while any(left.values()):
        run_kc = out[-1] if out else None
        run = 0
        for kc in reversed(out):
            if kc != run_kc:
                break
            run += 1
        options = sorted((k for k, n in left.items() if n and not (k == run_kc and run >= max_run)),
                         key=lambda k: (-left[k], k))  # fmt: skip
        if not options:  # only the KC at its run limit is left: the set cannot avoid a run
            options = [k for k, n in left.items() if n]
        out.append(options[0])
        left[options[0]] -= 1
    return out


def exam_phase(days_to_exam: int | None, s: Settings) -> str:
    """TABLE LRN-30a: `normal` beyond the countdown, `countdown`, `final` in the last days, `exam_day` on the day."""
    if days_to_exam is None or days_to_exam > s.phase_countdown_days:
        return "normal"
    if days_to_exam > s.phase_final_days:
        return "countdown"
    return "final" if days_to_exam >= 1 else "exam_day"


def exam_proximity(
    candidates: Sequence[str], started: Sequence[str], recent_errors: Sequence[str], chosen: Sequence[str],
    days_to_exam: int | None, s: Settings,
) -> Decision:  # fmt: skip
    """In `final`, no new KC enters unless the student deliberately chose it, and KCs with errors in the recent window
    come first; in other phases the candidates stand."""
    phase = exam_phase(days_to_exam, s)
    allowed = list(candidates)
    if phase in ("final", "exam_day"):
        allowed = [k for k in candidates if k in started or k in chosen]
        allowed.sort(key=lambda k: (k not in recent_errors, candidates.index(k)))
    return Decision("exam_proximity", {"days_to_exam": days_to_exam, "candidates": list(candidates),
                                       "chosen": list(chosen)},
                    {"phase": phase, "allowed": allowed,
                     "interleave_share": s.countdown_interleave_share if phase == "countdown" else None})  # fmt: skip


# --- LRN-26: stuck cap ---


def stuck_cap(
    *, stuck_count: int, stuck_sessions: int, p_k: float, revisit_at: int | None, now: int, s: Settings
) -> Decision:
    """Serve no further opportunity on a KC that reached the cap without reaching the bar until its revisit date;
    the revisit starts with a different representation."""
    parked = revisit_at is not None and now < revisit_at
    capped = p_k < s.stuck_cap_mastery and (stuck_count >= s.stuck_cap_opportunities
                                            or stuck_sessions >= s.stuck_cap_sessions)  # fmt: skip
    return Decision("stuck_cap", {"stuck_count": stuck_count, "stuck_sessions": stuck_sessions, "p_k": p_k,
                                  "revisit_at": revisit_at},
                    {"serve": not (parked or capped), "moved_on": parked or capped, "revisit_at": revisit_at,
                     "change_representation": revisit_at is not None and not parked})  # fmt: skip


# --- LRN-27: support for weaker students ---


def support_level(
    *,
    current: str,
    diagnostic_mean: float | None,
    active_days_by_week: Sequence[int],
    recovered_weeks: int,
    s: Settings,
) -> Decision:
    """`extra` when the entry diagnostic's mean P_k is low or attendance was below the minimum in each of the last two
    weeks; cleared once both have recovered for two weeks (`recovered_weeks` counts consecutive weeks with both
    conditions clear). The label is never shown to the student."""
    weeks = list(active_days_by_week)[-s.support_weeks :]
    low_attendance = len(weeks) == s.support_weeks and all(d < s.support_min_days_per_week for d in weeks)
    low_start = diagnostic_mean is not None and diagnostic_mean < s.support_diagnostic_below
    if low_attendance or low_start:
        level = "extra"
    elif current == "extra" and recovered_weeks < s.support_weeks:
        level = "extra"
    else:
        level = "standard"
    return Decision("support_level", {"current": current, "diagnostic_mean": diagnostic_mean,
                                      "active_days_by_week": weeks, "recovered_weeks": recovered_weeks},
                    {"support_level": level, "effects": support_effects(level, s)})  # fmt: skip


def support_effects(level: str, s: Settings) -> dict[str, Any]:
    """What `extra` changes (LRN-27): the worked-example threshold, the target success, the activity estimate (never
    a cutoff, D54), the weekly freezes and a reminder on each enabled day."""
    extra = level == "extra"
    return {
        "example_below": s.support_example_below if extra else s.example_full_below,
        "target_success": s.adapt_target_supported if extra else s.adapt_target,
        "activity_minutes": s.support_activity_minutes if extra else None,
        "weekly_freezes": s.support_weekly_freezes if extra else s.streak_weekly_freezes,
        "daily_reminder": extra,
    }
