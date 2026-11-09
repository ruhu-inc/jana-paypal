"""Persisted probe entry points for lessons and the Coach (ASM-13)."""

from __future__ import annotations

from typing import Any

from agent_core.engine.assessment.errors import Refused
from agent_core.engine.assessment.items import present
from agent_core.engine.marking import mark_choice
from agent_core.engine.signatures import evidence
from agent_core.learner.projections import PROBE_PREFIX


class ProbeCommands:
    def lesson_probe(self: Any, lesson_id: str) -> dict[str, Any] | None:
        lesson = self.content.record("lessons", lesson_id)
        if lesson is None:
            raise Refused("not_found")
        topic = lesson["node_id"]
        for state in self.store.all():
            if state.get("purpose") == "probe" and state.get("topic") == topic:
                if state["state"] == "completed":
                    return None
                return {
                    "probe_id": state["id"],
                    "current_item": present(state["items"][0], self.content, self.settings, diagnostic=True),
                }
        self.guard_tutor()
        candidate = next((probe for kc in lesson["kc_ids"] if (probe := self.probe_for(kc, first_lesson=True))), None)
        if candidate is None:
            return None
        subject = {"mth": "mathematics", "phy": "physics", "chm": "chemistry", "bio": "biology", "acc": "accounting"}[
            candidate["id"].split(".")[1]
        ]
        state = self._new("practice", subject, purpose="probe", topic=topic, items=[candidate], index=0, coach=None)
        self._commit(state, [self._exposure(candidate, "diagnostic", state["id"])])
        return {
            "probe_id": state["id"],
            "current_item": present(candidate, self.content, self.settings, diagnostic=True),
        }

    def answer_probe(self: Any, identity: str, option_id: str) -> dict[str, Any]:
        self.guard_tutor()
        state = self._get(identity, "practice")
        if state.get("purpose") != "probe":
            raise Refused("not_found")
        item = state["items"][0]
        if state["state"] == "completed":
            if state["option_id"] != option_id:
                raise Refused("already_answered")
            return dict(self.probe_context(identity))
        option = next((o for o in item["parts"][0]["answer"]["options"] if o["id"] == option_id), None)
        if option is None:
            raise Refused("invalid_option")
        mc_id = option.get("misconception_id") if not option["correct"] else None
        fix = ""
        if mc_id:
            payload = evidence(item, mc_id, self.content.table("misconceptions"), "probe")
            if payload is None:
                raise Refused("invalid_probe")
            record = self.content.record("misconceptions", mc_id)
            text = record["fix"]["text"]
            fix = text.get("en", "") if isinstance(text, dict) else str(text)
        part = item["parts"][0]
        answer = {"parts": [{"part_id": part["id"], "kind": "mcq", "option_id": option_id}]}
        # Probes are diagnostic opportunities, including a correct response: preserve their mode and keyed verdict
        # atomically with the completed probe so a retry/restart never adds a second observation (ASM-01).
        _, events, transcript = self._attempt_events(
            {**state, "kind": "diagnostic"},
            item,
            {part["id"]: mark_choice(part, option_id)},
            answer,
            0,
        )
        for event in events:  # marked as the probe's, so the learner model leaves mastery alone (TABLE ASM-01a)
            if "problem_ref" in event.payload:
                event.payload["problem_ref"] = PROBE_PREFIX + state["id"]
        state.update(state="completed", option_id=option_id, coach={"misconception_id": mc_id, "fix": fix})
        self._commit(state, events, transcript)
        return dict(self.probe_context(identity))

    def probe_context(self: Any, identity: str) -> dict[str, Any]:
        state = self._get(identity, "practice")
        if state.get("purpose") != "probe" or state["state"] != "completed":
            raise Refused("probe_not_answered")
        item = state["items"][0]
        return {
            "item": item,
            "answer": {"parts": [{"part_id": item["parts"][0]["id"], "kind": "mcq", "option_id": state["option_id"]}]},
            "mode": "guided",
            "subject": state["subject"],
            "origin": {"kind": "practice_item", "ref": "probe:" + state["id"]},
            **state["coach"],
        }
