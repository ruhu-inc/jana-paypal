"""The first-session interview of the student hand-off (UXS-10 step 2, JRN-03 step 3; TOL-20, LRN-03, LRN-04).

Two short questions, one per card, in engine-written copy (`content/agent/templates/engine.en.yaml`, `interview_*`):
what the student wants help with (the goals of TABLE LRN-03a: chosen options and, optionally, their own words) and
how confident they feel in their subject. Each answer is merged into the profile by schema (`merge_profile`, LRN-04:
`profile_updated` names the field, never the value); a low confidence adds the goal of building it. The questions
are the same whether the tutor model is warm or still loading (MDL-20, ARC-22: JRN-03 F2), and no model is called, so
the hand-off never waits for the tutor. Skipping is always allowed and leaves an empty goal list.

What the student answered is kept as profile goals only; which questions were answered is an `audit_log` entry
(action `interview_answer`, the question id, whether it was skipped: never the words). The cards follow the
`card_question` shape of the events contract, so the client renders them as it renders a tutor's question card."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_core.engine.input import normalise_input
from agent_core.engine.receipts import Receipts
from agent_core.engine.safety import redact_personal
from agent_core.engine.ui import question_card
from agent_core.learner.profile import merge_profile, stored_profile
from agent_core.learner.service import Learner
from agent_core.store.ids import new_ulid

QUESTIONS = ("goals", "confidence")
ACTION = "interview_answer"
GOAL_OPTIONS = ("interview_goal_exams", "interview_goal_catch_up", "interview_goal_understand", "interview_goal_faster")
CONFIDENCE_OPTIONS = ("interview_confident_very", "interview_confident_fairly", "interview_confident_not_very",
                      "interview_confident_not_at_all")  # fmt: skip
LOW_CONFIDENCE = frozenset({"interview_confident_not_very", "interview_confident_not_at_all"})
MAX_GOALS, GOAL_CHARS = 5, 120  # learner-model contract: `goals` holds at most 5 strings of at most 120 characters


class InterviewRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Interview:
    learner: Learner
    copy: Receipts
    clock_ms: Any

    @property
    def conn(self) -> Any:
        return self.learner.conn

    def answered(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT target FROM audit_log WHERE action = ?", (ACTION,)).fetchall()
        return [q for q in QUESTIONS if (q,) in rows]

    def current(self) -> str | None:
        """The next question, or None when the interview is over. Goals already in the profile (from an earlier
        build or another route) count the goals question as answered."""
        done = set(self.answered())
        if stored_profile(self.conn).get("goals") is not None and "goals" not in done and not done:
            return None  # goals were set before this interview existed: nothing more to ask
        return next((q for q in QUESTIONS if q not in done), None)

    def view(self) -> dict[str, Any]:
        question = self.current()
        shown = None if question is None else {"question_id": question, "card": self.card(question)}
        return {"total": len(QUESTIONS), "answered": len(self.answered()), "done": question is None, "question": shown}

    def card(self, question: str) -> dict[str, Any]:
        cards = self.copy.cards
        if question == "goals":
            options = [{"label": cards[key]} for key in GOAL_OPTIONS]
            return question_card(cards["interview_goals_prompt"], options, True, True)
        subject = self._subject()
        options = [{"label": cards[key]} for key in CONFIDENCE_OPTIONS]
        return question_card(cards["interview_confidence_prompt"].format(subject=subject), options, False, False)

    def answer(self, question: str, answer: Mapping[str, Any]) -> dict[str, Any]:
        """One answer (the `CardAnswer` body's `answer`): option ids of the card, the student's own words (goals only,
        already screened by the API), or `skipped`. Only the current question may be answered."""
        if question != self.current():
            raise InterviewRefused("not_current" if question in QUESTIONS else "unknown_question")
        card = self.card(question)
        labels = {o["id"]: o["label"] for o in card["options"]}
        chosen = [str(i) for i in answer.get("option_ids") or []]
        if any(i not in labels for i in chosen):
            raise InterviewRefused("unknown_option")
        skipped = answer.get("skipped") is True
        now = int(self.clock_ms())
        goals = list(stored_profile(self.conn).get("goals") or [])
        if question == "goals" and not skipped:
            own = redact_personal(normalise_input(str(answer.get("text") or "")).strip())[0][:GOAL_CHARS]
            goals = [labels[i] for i in chosen] + ([own] if own else [])
        elif question == "confidence" and not skipped:
            if len(chosen) != 1:
                raise InterviewRefused("one_option")
            key = CONFIDENCE_OPTIONS[[o["id"] for o in card["options"]].index(chosen[0])]
            if key in LOW_CONFIDENCE:
                goals.append(self.copy.cards["interview_confidence_goal"].format(subject=self._subject()))
        goals = list(dict.fromkeys(g[:GOAL_CHARS] for g in goals if g))[:MAX_GOALS]
        merge_profile(self.learner, {"goals": goals}, now)
        self.conn.execute("INSERT INTO audit_log (audit_id, actor_role, action, target, detail, at) VALUES"
                          " (?, 'student', ?, ?, ?, ?)",
                          (new_ulid(now), ACTION, question, json.dumps({"skipped": skipped}), now))  # fmt: skip
        return self.view()

    def _subject(self) -> str:
        subjects = stored_profile(self.conn).get("subjects") or []
        first = subjects[0] if subjects else "your subjects"
        return str(first.get("subject") if isinstance(first, Mapping) else first)
