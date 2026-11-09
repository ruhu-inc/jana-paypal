"""The study planner (LRN-28 to LRN-32): optional plans proposed by code from the learner model; a model may only
phrase them.

`plan` is a pure function of a `PlannerState` (what the learner model, the profile and the packs say today) and the
settings. It returns the next activity first and the optional blocks after it, each with an approximate duration
(an estimate that sizes the suggestion and never stops work, D54), plus three ranked alternatives for the main
block. There is no daily cap: every further request plans again from the current state, at any hour.

- Warm-up: due review cards (exam-capped, LRN-13), lowest retrievability first, at most `review_cap_items` and at
  most `review_cap_share` of the plan's estimated minutes; the rest wait for later suggestions (LRN-29), unless the
  student asks for every due review now. Study's own due cards (SPEC §19.4) follow in one more warm-up block,
  `due_reviews_study`, filling what the learner-model warm-up leaves of the same item cap and minutes share.
- Main: the KC maximising exam_weight x (1 - P) x (1 + boost x failed_recently) among the subjects chosen for this
  suggestion, replaced by its weakest prerequisite when a prerequisite is under the readiness bar; a lesson when the
  KC is not started, else practice. A KC whose stuck-cap revisit date has come is revisited first. After a mock, the
  KCs of its questions that lost marks are practised first until they are secure (ASM-20; D102). The student's own
  topic choice replaces it.
- Mixed: interleaved items from mastered KCs confusable with the main KC.
- Reflect: the bars that moved today.
- At most `planner_max_subjects` subjects per suggestion, ranked by days to exam then weakness and rotated by the
  plans already taken today.
- Phases (TABLE LRN-30a, nearest exam among the profile's subjects): `countdown` puts a timed mixed paper in place of
  the main block once every `phase_mock_every_days`; `final` serves only KCs with recent errors (no new KC unless the
  student chose it) and a full mock in place of the mixed block on the same rhythm; `exam_day` offers only an
  optional light review.
- Exam dates (LRN-44): in the `exam_frontload_days` before the nearest exam the learner set, the main block comes
  from that subject's weakest KCs (in the final days, when none has a recent error, its weakest started KC, never a new
  one), and a mock's follow-up from another subject waits.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from agent_core.config.keys import Settings
from agent_core.learner.policies import exam_phase

SECURE = ("mastered", "needs_review")


BLOCK_KCS_MAX = 50  # PlanBlock.kc_ids maxItems
BLOCKS_MAX = 12  # PlanBlock.index 0 to 11
STUDY_CARDS_MAX = 10  # PlanBlock.study_cards maxItems


@dataclass(frozen=True)
class KcPlanInfo:
    kc_id: str
    subject: str
    state: str
    p_mastery: float
    exam_weight: float
    prerequisites: tuple[str, ...] = ()
    confusable: tuple[str, ...] = ()
    failed_recently: bool = False
    revisit_due: bool = False  # a moved-on KC whose revisit date has come (LRN-26)


@dataclass(frozen=True)
class SubjectInfo:
    subject: str
    days_to_exam: int | None


@dataclass(frozen=True)
class PlannerState:
    subjects: tuple[SubjectInfo, ...]
    kcs: Mapping[str, KcPlanInfo]
    due: tuple[tuple[str, float], ...]  # (kc_id, retrievability), lowest retrievability first
    moved_today: tuple[str, ...] = ()  # the KCs studied today, the latest first
    support_extra: bool = False
    plans_taken_today: int = 0
    days_since_mock: int | None = None
    chosen_kc: str | None = None
    # The latest marked mock's weak KCs (ASM-20, LRN-28; D102): the KCs of its questions that lost marks.
    follow_up: tuple[str, ...] = ()
    # LRN-44: the subject of the nearest exam date when it is within `exam_frontload_days`, else None.
    frontload: str | None = None
    # SPEC §19.4: Study's due cards as (deck_id, card_id), soonest first, at most STUDY_CARDS_MAX.
    study_due: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Block:
    kind: str  # warmup | main | mixed | reflect | mock
    kc_ids: tuple[str, ...]
    minutes: int
    reason_code: str
    study_cards: tuple[tuple[str, str], ...] = ()  # (deck_id, card_id): a Study warm-up's due cards

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "minutes": self.minutes}
        if self.reason_code:
            out["reason_code"] = self.reason_code
        if self.kc_ids:
            out["kc_ids"] = list(self.kc_ids)
        if self.study_cards:
            out["study_cards"] = [{"deck_id": deck, "card_id": card} for deck, card in self.study_cards]
        return out


@dataclass(frozen=True)
class Plan:
    phase: str
    blocks: tuple[Block, ...]
    deferred_reviews: int
    subjects: tuple[str, ...]
    alternatives: tuple[Block, ...] = field(default=())

    @property
    def minutes(self) -> int:
        return sum(b.minutes for b in self.blocks)

    def payload(self, plan_id: str, date: str) -> dict[str, Any]:
        """The `plan_generated` payload (learning-event contract)."""
        return {"plan_id": plan_id, "date": date, "minutes": max(1, self.minutes), "phase": self.phase,
                "blocks": [b.payload() for b in self.blocks], "deferred_reviews": self.deferred_reviews}  # fmt: skip


def nearest_exam(subjects: Sequence[SubjectInfo]) -> int | None:
    days = [s.days_to_exam for s in subjects if s.days_to_exam is not None and s.days_to_exam >= 0]
    return min(days) if days else None


def unstarted(state: PlannerState) -> frozenset[str]:
    """The subjects with no evidence yet (no placement, lesson or attempt). Their prior mastery is not evidence and
    makes them look weakest, so they rank after the started ones, and among themselves in the profile's own order
    (SPEC §10.2): a new profile starts with its first subject."""
    started = {k.subject for k in state.kcs.values() if k.state != "not_started"}
    return frozenset({k.subject for k in state.kcs.values()} - started)


def _listed(state: PlannerState) -> dict[str, int]:
    """Each subject's place in the profile's list."""
    return {x.subject: i for i, x in enumerate(state.subjects)}


def subject_order(state: PlannerState, s: Settings) -> list[str]:
    """Subjects by days to exam (no exam last), then started before unstarted, then weakness (mean exam-weighted
    1 - P), then name; rotated by the plans already taken today so Keep learning moves on."""

    def weakness(subject: str) -> float:
        rows = [k for k in state.kcs.values() if k.subject == subject]
        total = sum(k.exam_weight for k in rows)
        return sum(k.exam_weight * (1 - k.p_mastery) for k in rows) / total if total else 0.0

    later, listed = unstarted(state), _listed(state)
    ranked = sorted(state.subjects, key=lambda x: (
        x.days_to_exam if x.days_to_exam is not None else math.inf,
        x.subject in later,
        0.0 if x.subject in later else -weakness(x.subject),
        listed[x.subject] if x.subject in later else 0,
        x.subject,
    ))  # fmt: skip
    names = [x.subject for x in ranked]
    if names:
        shift = state.plans_taken_today % len(names)
        names = names[shift:] + names[:shift]
    first = state.chosen_kc if state.chosen_kc in state.kcs else next(iter(follow_up(state)), None)
    if first is not None:
        own = state.kcs[first].subject
        names = [own] + [n for n in names if n != own]
    return names[: s.planner_max_subjects]


def follow_up(state: PlannerState) -> tuple[str, ...]:
    """The mock's weak KCs still to work on: known to the planner, not yet secure and not parked (LRN-26)."""
    return tuple(k for k in state.follow_up if k in state.kcs and state.kcs[k].state not in (*SECURE, "moved_on"))


def main_candidates(state: PlannerState, subjects: Sequence[str], phase: str, s: Settings) -> list[tuple[str, str]]:
    """Main-block choices in rank order as (kc_id, reason_code): revisits first, then by score; an unready KC is
    replaced by its weakest prerequisite. Distinct KCs only."""
    pool = [k for k in state.kcs.values() if k.subject in subjects and k.state not in SECURE and k.state != "moved_on"]
    if phase in ("final", "exam_day"):
        pool = [k for k in pool if k.failed_recently]  # wrong-answers-only review; no new KC
    revisits = sorted(k.kc_id for k in state.kcs.values() if k.subject in subjects and k.revisit_due
                      and k.state not in SECURE and phase not in ("final", "exam_day"))  # fmt: skip

    def score(k: KcPlanInfo) -> float:
        return k.exam_weight * (1 - k.p_mastery) * (1 + s.planner_recent_failure_boost * k.failed_recently)

    later, listed = unstarted(state), _listed(state)
    out: list[tuple[str, str]] = [(kc, "revisit_moved_on") for kc in revisits]
    for k in sorted(pool, key=lambda k: (k.subject in later, listed.get(k.subject, 0) if k.subject in later else 0,
                                         -round(score(k), 12), k.kc_id)):  # fmt: skip
        weak = [state.kcs[j] for j in k.prerequisites if j in state.kcs
                and state.kcs[j].p_mastery < s.planner_prerequisite_ready]  # fmt: skip
        if weak and phase not in ("final", "exam_day"):
            choice = min(weak, key=lambda j: (j.p_mastery, j.kc_id)).kc_id
            out.append((choice, "prerequisite"))
        else:
            out.append((k.kc_id, "weak_high_value"))
    seen: set[str] = set()
    distinct = []
    for kc, reason in out:
        if kc not in seen:
            seen.add(kc)
            distinct.append((kc, reason))
    return distinct


def frontload_candidates(state: PlannerState, subject: str, subjects: Sequence[str], phase: str, s: Settings
                         ) -> list[tuple[str, str]]:  # fmt: skip
    """LRN-44: the main-block choices before an exam, from that subject first. In the final days a subject with no
    recent error still front-loads its weakest started KCs (never a new one); a subject with nothing to work on leaves
    the usual choices."""
    own = main_candidates(state, [subject], phase, s)
    if not own and phase in ("final", "exam_day"):
        started = [k for k in state.kcs.values() if k.subject == subject and k.state not in (*SECURE, "moved_on")
                   and k.state != "not_started"]  # fmt: skip
        own = [(k.kc_id, "weak_high_value") for k in sorted(started, key=lambda k: (k.p_mastery, k.kc_id))]
    rest = [c for c in main_candidates(state, subjects, phase, s) if c not in own]
    return own + rest


def main_block(state: PlannerState, kc: str, reason: str, s: Settings) -> Block:
    lesson = state.kcs[kc].state == "not_started" if kc in state.kcs else True
    practice_minutes, lesson_minutes = s.planner_main_minutes
    return Block("main", (kc,), lesson_minutes if lesson else practice_minutes, reason)


def plan(state: PlannerState, s: Settings, *, all_reviews: bool = False) -> Plan:
    days = nearest_exam(state.subjects)
    phase = exam_phase(days, s)
    subjects = subject_order(state, s)
    frontload = state.frontload if state.frontload in {x.subject for x in state.subjects} else None
    if frontload is not None and state.chosen_kc not in state.kcs:
        subjects = ([frontload] + [n for n in subjects if n != frontload])[: s.planner_max_subjects]
    due = [(kc, r) for kc, r in state.due if kc in state.kcs and state.kcs[kc].subject in subjects]
    if phase == "exam_day":  # an optional light review, no mock
        kcs = tuple(kc for kc, _ in due[: s.review_cap_items])
        block = Block("warmup", kcs, s.exam_day_review_minutes, "exam_phase")
        return Plan(phase, (block,), max(0, len(due) - len(kcs)), tuple(subjects))
    blocks: list[Block] = []
    alternatives: list[Block] = []
    mock_due = state.days_since_mock is None or state.days_since_mock >= s.phase_mock_every_days
    candidates = (frontload_candidates(state, frontload, subjects, phase, s) if frontload is not None
                  else main_candidates(state, subjects, phase, s))  # fmt: skip
    chosen = state.chosen_kc if state.chosen_kc in state.kcs else None
    weak = tuple(k for k in follow_up(state) if state.kcs[k].subject in subjects
                 and (frontload is None or state.kcs[k].subject == frontload))  # fmt: skip
    main: Block | None
    if chosen is not None:
        main = main_block(state, chosen, "weak_high_value", s)
        candidates = [c for c in candidates if c[0] != chosen]
    elif weak:  # after a mock: practice on the KCs whose questions lost marks (ASM-20, LRN-28; D102)
        main = Block("main", weak, s.planner_main_minutes[0], "mock_follow_up")
        candidates = [c for c in candidates if c[0] not in weak]
    elif phase == "countdown" and mock_due:
        main = Block("mock", (), s.phase_mock_minutes, "exam_phase")
    elif candidates:
        main = main_block(state, *candidates[0], s)
        candidates = candidates[1:]
    else:
        main = None
    alternatives = [main_block(state, kc, reason, s) for kc, reason in candidates[: s.planner_alternatives]]
    if main is not None:
        blocks.append(main)
    focus = main.kc_ids[0] if main is not None and main.kc_ids else None
    if phase == "final" and mock_due:
        blocks.append(Block("mock", (), s.phase_mock_minutes, "exam_phase"))
    elif focus is not None and not state.support_extra:
        mixed = tuple(sorted(k for k in state.kcs[focus].confusable if k in state.kcs and state.kcs[k].state in SECURE))
        if mixed:
            blocks.append(Block("mixed", mixed, s.planner_mixed_minutes, "interleave"))
    # A block names at most BLOCK_KCS_MAX KCs (the API's PlanBlock.kc_ids); a busy day reflects on its latest.
    moved = tuple(sorted([k for k in state.moved_today if k in state.kcs][:BLOCK_KCS_MAX]))
    if moved:
        blocks.append(Block("reflect", moved, s.planner_reflect_minutes, ""))
    rest = sum(b.minutes for b in blocks)
    if state.support_extra and main is not None and main.kind == "main":
        # Extra support sizes the suggestion at about `support_activity_minutes` (an estimate, never a cutoff).
        trimmed = max(1, s.support_activity_minutes - (rest - main.minutes))
        blocks[0] = replace(main, minutes=min(main.minutes, trimmed))
        rest = sum(b.minutes for b in blocks)
    take = len(due) if all_reviews else warmup_size(len(due), rest, s)
    # A block names at most BLOCK_KCS_MAX KCs (PlanBlock.kc_ids): more reviews, as "more review" serves with no cap
    # (D54), are consecutive warm-up blocks, within the plan's BLOCKS_MAX blocks.
    take = min(take, BLOCK_KCS_MAX * (BLOCKS_MAX - len(blocks)))
    reviews = [kc for kc, _ in due[:take]]
    warmups = [Block("warmup", tuple(part), warmup_minutes(len(part), s), "due_reviews")
               for part in (reviews[i : i + BLOCK_KCS_MAX] for i in range(0, take, BLOCK_KCS_MAX))]  # fmt: skip
    # SPEC §19.4: Study's due cards follow in one warm-up block of their own, in what the learner-model warm-up
    # leaves of the review share (all of them, to the contract's ten, when every due review is asked for).
    cards = state.study_due[:STUDY_CARDS_MAX]
    if cards and len(blocks) + len(warmups) < BLOCKS_MAX:
        extra = len(cards) if all_reviews else study_share(take, len(cards), rest, s)
        if extra:
            warmups.append(Block("warmup", (), warmup_minutes(extra, s), "due_reviews_study", cards[:extra]))
    blocks[:0] = warmups
    return Plan(phase, tuple(blocks), len(due) - take, tuple(subjects), tuple(alternatives))


def warmup_minutes(reviews: int, s: Settings) -> int:
    return max(1, math.ceil(reviews * s.planner_review_item_minutes - 1e-9))


def warmup_size(due: int, rest: int, s: Settings) -> int:
    """The most due reviews the first suggestion takes (LRN-29): at most `review_cap_items`, and their minutes at most
    `review_cap_share` of the whole plan's. A plan with nothing else is reviews only, within the item cap."""
    take = min(due, s.review_cap_items)
    if not rest:
        return take
    while take and warmup_minutes(take, s) > s.review_cap_share * (warmup_minutes(take, s) + rest) + 1e-9:
        take -= 1
    return take


def study_share(taken: int, cards: int, rest: int, s: Settings) -> int:
    """The Study cards that fit after `taken` learner-model reviews (SPEC §19.4): the two warm-ups together hold at
    most `review_cap_items` items, and their minutes at most `review_cap_share` of the whole plan's."""
    take = max(0, min(cards, s.review_cap_items - taken))

    def minutes(n: int) -> int:
        return (warmup_minutes(taken, s) if taken else 0) + (warmup_minutes(n, s) if n else 0)

    if not rest:
        return take
    while take and minutes(take) > s.review_cap_share * (minutes(take) + rest) + 1e-9:
        take -= 1
    return take


# --- the student's choices (LRN-31, LRN-32): each changes only the plan it is made on ----------------------------


def swap_main(proposal: Plan, alternative: int) -> Plan:
    """The plan with its main block replaced by one of the ranked alternatives."""
    chosen = proposal.alternatives[alternative]
    blocks = [chosen if b.kind in ("main", "mock") and i == _main_index(proposal) else b
              for i, b in enumerate(proposal.blocks)]  # fmt: skip
    if _main_index(proposal) is None:
        blocks.append(chosen)
    rest = tuple(a for i, a in enumerate(proposal.alternatives) if i != alternative)
    return replace(proposal, blocks=tuple(blocks), alternatives=rest)


def _main_index(proposal: Plan) -> int | None:
    return next((i for i, b in enumerate(proposal.blocks) if b.kind in ("main", "mock")), None)


def shorten(proposal: Plan) -> Plan:
    """The plan cut to its next activity: the first block only."""
    return replace(proposal, blocks=proposal.blocks[:1])


def reorder(proposal: Plan, order: Sequence[int]) -> Plan:
    """The plan's blocks in the student's order (a permutation of their indexes)."""
    if sorted(order) != list(range(len(proposal.blocks))):
        raise ValueError("an order lists every block once")
    return replace(proposal, blocks=tuple(proposal.blocks[i] for i in order))


def from_payload(payload: Mapping[str, Any]) -> Plan:
    """A stored proposal back as a plan (its alternatives are recomputed when the student asks to change it)."""
    blocks = tuple(Block(b["kind"], tuple(b.get("kc_ids", ())), int(b["minutes"]), str(b.get("reason_code") or ""),
                         tuple((str(c["deck_id"]), str(c["card_id"])) for c in b.get("study_cards", ())))
                   for b in payload["blocks"])  # fmt: skip
    return Plan(str(payload["phase"]), blocks, int(payload.get("deferred_reviews", 0)), ())
