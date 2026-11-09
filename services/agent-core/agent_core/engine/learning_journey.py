"""A local handoff across existing curriculum, practice and lessons; never a second mastery model."""

from __future__ import annotations

import json
from typing import Any

from agent_core.contracts import validate
from agent_core.engine.assessment.errors import Refused
from agent_core.engine.learn import teaching_example
from agent_core.engine.study_topics import matching_topics
from agent_core.learner.mastery import SUBJECTS

PREFIX = "learning/journey#/$defs/"


class LearningJourney:
    def __init__(self, assessment: Any):
        self.assessment = assessment
        self.conn = assessment.learner.conn
        self.content = assessment.content

    def saved(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT body FROM learning_journey WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, body: dict[str, Any]) -> None:
        source = body.get("source") or {}
        self.conn.execute(
            "INSERT INTO learning_journey VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "body=excluded.body,source_id=excluded.source_id,source_revision=excluded.source_revision",
            (json.dumps(body), source.get("source_id"), source.get("revision")),
        )

    def start(self, asked: dict[str, Any], study: Any = None) -> dict[str, Any]:
        validate(PREFIX + "Start", asked)
        self.assessment.guard_tutor()
        node = self.content.nodes.get(asked["node_id"])
        kc = self.content.record("kcs", asked["kc_id"])
        if (
            not node
            or not kc
            or kc.get("node_id") != asked["node_id"]
            or (node.get("status", "active") != "active" or kc.get("status", "active") != "active")
        ):
            raise Refused("invalid_learning_objective")
        source = asked.get("source")
        if source:
            if study is None:
                raise Refused("unmapped_source")
            mapped = matching_topics(self.content, study.curriculum_text(source))["items"]
            if not any(m["node_id"] == asked["node_id"] and asked["kc_id"] in m["kc_ids"] for m in mapped):
                raise Refused("unmapped_source")
        lesson_id = asked.get("lesson_id")
        if lesson_id:
            lesson = self.content.record("lessons", lesson_id)
            if (
                not lesson
                or asked["kc_id"] not in lesson.get("kc_ids", [])
                or lesson.get("status", "active") != "active"
            ):
                raise Refused("invalid_learning_lesson")
        current = self.saved()
        # Entering the same objective from the plan or a lesson retains its source revision,
        # attempts and current question. An explicit new source still starts a new handoff.
        same_goal = current and all(current.get(k) == asked.get(k) for k in ("node_id", "kc_id"))
        if current and same_goal and (source is None or current.get("source") == source):
            if lesson_id:
                current["lesson_id"] = lesson_id
                self.save(current)
            return self.reply()
        self.save({**asked, "stage": "starting", "set_id": None, "lesson_opened": False})
        return self.reply()

    def evidence(self, body: dict[str, Any]) -> str:
        if not body.get("set_id"):
            return "none"
        state = self.assessment._get(body["set_id"], "practice")
        item_id = state["items"][0]["id"]
        # Read the canonical learner log, including corrections/supersession, rather than cached HTTP marks.
        attempts, outcomes, _ = self.assessment.item_verdicts(state["id"], item_id)
        if item_id in state.get("helped", []) or item_id in state.get("revealed", []):
            return "assisted"
        if not outcomes:
            return "not_checked" if state["clients"] or state["index"] else "none"
        if any(outcome in ("incorrect", "partial") for outcome in outcomes):
            return "incorrect"
        if len(attempts) == 1 and all(outcome == "correct" for outcome in outcomes):
            return "independent_correct"
        return "not_checked"

    def reviewing_lesson(self, lesson_id: str) -> bool:
        view = self.reply()["journey"]
        return bool(view and view["lesson_opened"] and view["lesson_id"] == lesson_id)

    def reply(self) -> dict[str, Any]:
        body = self.saved()
        if body is None:
            return {"journey": None}
        kc = self.content.record("kcs", body["kc_id"])
        node = self.content.nodes.get(body["node_id"])
        if not kc or not node or kc.get("status", "active") != "active":
            return {"journey": None}  # An uninstalled objective is never replaced with a fabricated one.
        lessons = [
            lesson["id"]
            for lesson in self.content.table("lessons")
            if body["kc_id"] in lesson.get("kc_ids", []) and lesson.get("status", "active") == "active"
        ]
        evidence = self.evidence(body)
        phase = "ready" if not body.get("set_id") else "question"
        if evidence == "independent_correct":
            phase = "complete" if body["stage"] == "check" else "ready_check"
        elif evidence != "none":
            phase = "ready_check" if body["lesson_opened"] or (evidence == "assisted" and not lessons) else "repair"
        if body.get("blocked"):
            phase = "unavailable"
        source = body.get("source")
        revision = (
            self.conn.execute(
                "SELECT current_revision FROM study_sources WHERE source_id=?", (source["source_id"],)
            ).fetchone()
            if source
            else None
        )
        prior = sum(
            any(k["kc_id"] == body["kc_id"] for k in json.loads(kcs))
            for (kcs,) in self.conn.execute("SELECT kc_ids FROM attempts")
        )
        preferred_lesson = body.get("lesson_id")
        lesson_id = preferred_lesson if preferred_lesson in lessons else sorted(lessons)[0] if lessons else None
        result = {
            "journey": {
                "goal": {
                    "node_id": body["node_id"],
                    "kc_id": body["kc_id"],
                    "title": str(node["title"]),
                    "objective": str(kc["statement"]),
                    "subject": SUBJECTS[body["kc_id"].split(".")[1]],
                },
                "source": source,
                "source_changed": bool(source and revision and revision[0] != source["revision"]),
                "phase": phase,
                "evidence": evidence,
                "prior_attempts": prior,
                "practice_set_id": body.get("set_id"),
                "lesson_id": lesson_id,
                "lesson_opened": body["lesson_opened"],
                "stage": body["stage"],
            }
        }
        validate(PREFIX + "Reply", result)
        return result

    def step(self, action: str) -> dict[str, Any]:
        self.assessment.guard_tutor()
        body, view = self.saved(), self.reply()["journey"]
        if body is None or view is None:
            raise Refused("not_found")
        if action == "review":
            if view["lesson_id"] is None:
                raise Refused("no_lesson")
            # Opening a lesson is assistance, never proof it was completed or learned.
            body["lesson_opened"] = True
            body["blocked"] = False
            # All lesson questions and worked examples may become visible during the intervention.
            # Keep them out of its later independent check even if the general practice window expires.
            lesson = self.content.record("lessons", view["lesson_id"])
            seen = set(body.get("seen_items", []))
            for block in lesson.get("blocks", []):
                seen.update(block.get("item_ids", []))
                if block.get("item_id"):
                    seen.add(block["item_id"])
                if block.get("worked_example_id"):
                    example = self.content.record("worked_examples", block["worked_example_id"])
                    if example and example.get("item_id"):
                        seen.add(example["item_id"])
            example = teaching_example(self.content, lesson)
            if example:
                if example.get("item_id"):
                    seen.add(example["item_id"])
                body["seen_stems"] = sorted(
                    set(body.get("seen_stems", [])) | {" ".join(str(example.get("problem", "")).split()).casefold()}
                )
            body["seen_items"] = sorted(seen)
        elif action == "practice":
            if body.get("set_id"):
                return self.reply()
            return self.make_question(body)
        elif action == "check":
            if view["phase"] == "question" and body["stage"] == "check":
                return self.reply()  # Repeated clicks/retries resume the same unanswered check.
            if view["phase"] not in ("ready_check", "complete", "unavailable"):
                raise Refused("intervention_pending")
            if body.get("set_id"):
                body["stage"] = "check"
            return self.make_question(body)
        else:
            raise Refused("invalid_action")
        self.save(body)
        return self.reply()

    def make_question(self, body: dict[str, Any]) -> dict[str, Any]:
        # The existing selector enforces published keys, exposure, reservations and adaptation rules.
        # In particular it cannot recycle the question whose help/answer was already exposed.
        seen = set(body.get("seen_items", []))
        if body.get("set_id"):
            seen.update(i["id"] for i in self.assessment._get(body["set_id"], "practice")["items"])
        stems = set(body.get("seen_stems", [])) | {
            " ".join(str(item["stem"]).split()).casefold()
            for identity in seen
            if (item := self.content.record("items", identity))
        }
        excluded = seen | {
            i["id"] for i in self.content.table("items") if " ".join(str(i["stem"]).split()).casefold() in stems
        }
        try:
            made = self.assessment.create_practice(
                SUBJECTS[body["kc_id"].split(".")[1]],
                [body["kc_id"]],
                1,
                exclude_item_ids=excluded,
            )
        except Refused as error:
            if error.reason != "no_items":
                raise
            body["blocked"] = True
        else:
            body.update(set_id=made["set_id"], blocked=False, lesson_opened=False)
            seen.add(made["current_item"]["item_id"])
            body["seen_items"] = sorted(seen)
        self.save(body)
        return self.reply()
