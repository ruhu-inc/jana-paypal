"""Resume source learning using saved work; never infer syllabus mastery or call a provider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_core.engine.study import StudyDesk


class StudyJourney:
    def __init__(self, desk: StudyDesk):
        self.desk, self.conn = desk, desk.conn

    def root(self, aid_id: str) -> str | None:
        seen = set()
        for _ in range(64):
            if aid_id in seen:
                return None
            seen.add(aid_id)
            if self.conn.execute("SELECT 1 FROM study_journeys WHERE root_aid_id=?", (aid_id,)).fetchone():
                return aid_id
            row = self.conn.execute("SELECT parent_aid_id FROM study_aid_parents WHERE aid_id=?", (aid_id,)).fetchone()
            if not row:
                return None
            aid_id = row[0]
        return None

    def register(self, aid: dict[str, Any], request: dict[str, Any]) -> None:
        parent_id = request.get("context_aid_id")
        if parent_id:
            parent = self.desk.aid(parent_id)
            self.conn.execute("INSERT INTO study_aid_parents VALUES (?,?)", (aid["aid_id"], parent_id))
            # Choosing cards from a saved explanation starts a source journey, including older explanations.
            if not self.root(parent_id) and parent["kind"] == "chat" and aid["kind"] == "flashcards":
                self.start(parent_id)
            self.resume(aid["aid_id"])
        elif request.get("learning_intent") == "lesson" and aid["kind"] == "chat":
            self.start(aid["aid_id"])

    def start(self, aid_id: str) -> None:
        from agent_core.engine.study import StudyError

        aid = self.desk.aid(aid_id)
        if aid["kind"] != "chat" or aid["status"] != "ready":
            raise StudyError("invalid_study_request")
        root = self.root(aid_id)
        if root:
            self.resume(aid_id)
            return
        self.conn.execute(
            "INSERT INTO study_journeys(root_aid_id,current_aid_id,updated_at) VALUES (?,?,?)",
            (aid_id, aid_id, self.desk.clock()),
        )

    def resume(self, aid_id: str, reviewed: bool = False) -> None:
        if root := self.root(aid_id):
            self.conn.execute(
                "UPDATE study_journeys SET current_aid_id=CASE WHEN ? THEN ? ELSE current_aid_id END,updated_at=?,"
                "lesson_reviewed_at=CASE WHEN ? THEN ? ELSE lesson_reviewed_at END WHERE root_aid_id=?",
                (
                    self.desk.aid(aid_id)["kind"] in ("flashcards", "quiz"),
                    aid_id,
                    self.desk.clock(),
                    reviewed,
                    self.desk.clock(),
                    root,
                ),
            )

    def saved_result(self, quiz_id: str) -> tuple[dict[str, Any], int] | None:
        """Only a submitted result, never a per-question check or an in-progress retry."""
        quiz = self.desk.quiz(quiz_id)["view"]
        if quiz.get("draft"):
            return None
        # New submissions distinguish check-answer from submit explicitly. Historical full attempts remain usable.
        rows = self.conn.execute(
            "SELECT a.body,a.submitted_at FROM study_attempts a LEFT JOIN study_quiz_submissions s "
            "ON s.attempt_id=a.attempt_id WHERE a.quiz_id=? AND (s.kind='submit' OR "
            "(s.attempt_id IS NULL AND NOT EXISTS (SELECT 1 FROM json_each(a.body,'$.results') "
            "WHERE json_extract(value,'$.verdict')='skipped'))) ORDER BY a.submitted_at DESC,a.attempt_id DESC",
            (quiz_id,),
        ).fetchone()
        return (json.loads(rows[0]), rows[1]) if rows else None

    def current(self, authorised: Any) -> dict[str, Any] | None:
        for root, current, reviewed, _ in self.conn.execute(
            "SELECT * FROM study_journeys ORDER BY updated_at DESC,root_aid_id DESC"
        ).fetchall():
            lesson = self.desk.aid(root)
            try:
                authorised(lesson["sources"])
            except ValueError:
                continue  # A Home summary must not disclose private material without its current grant.
            sources = [self.desk.source(s["source_id"]) for s in lesson["sources"]]
            if any(
                self.conn.execute("SELECT 1 FROM study_library_archive WHERE source_id=?", (s["source_id"],)).fetchone()
                for s in sources
            ):
                continue
            return self.view(lesson, current, reviewed)
        return None

    def view(self, lesson: dict[str, Any], current: str, reviewed: int) -> dict[str, Any]:
        from agent_core.engine.study import checked

        root = lesson["aid_id"]
        rows = self.conn.execute(
            "WITH RECURSIVE family(id,depth) AS (SELECT ?,0 UNION ALL "
            "SELECT link.aid_id,f.depth+1 FROM study_aid_parents link JOIN family f ON link.parent_aid_id=f.id "
            "WHERE f.depth<64) SELECT a.body FROM study_aids a JOIN family f ON a.aid_id=f.id "
            "ORDER BY a.created_at DESC,a.aid_id DESC",
            (root,),
        ).fetchall()
        aids = [json.loads(row[0]) for row in rows]
        parents = dict(self.conn.execute("SELECT aid_id,parent_aid_id FROM study_aid_parents"))

        def path(identity: str) -> set[str]:
            seen: set[str] = set()
            while identity not in seen and len(seen) < 64:
                seen.add(identity)
                if identity not in parents:
                    break
                identity = parents[identity]
            return seen

        # Opening an older branch resumes its own work, not an unrelated later regeneration.
        lineage = path(current)
        family = [
            a
            for a in aids
            if a["sources"] == lesson["sources"]
            and (current == root or a["aid_id"] in lineage or current in path(a["aid_id"]))
        ]
        cards = next((a for a in family if a["kind"] == "flashcards"), None)
        quiz = next(
            (a for a in family if a["kind"] == "quiz" and (not cards or cards["aid_id"] in path(a["aid_id"]))), None
        )
        out = {
            "root_aid_id": root,
            "sources": lesson["sources"],
            "title": lesson["title"],
            "lesson_aid_id": root,
            "cards_aid_id": cards["aid_id"] if cards else None,
            "quiz_aid_id": quiz["aid_id"] if quiz else None,
            "next_aid_id": root,
            "phase": "lesson",
            "cards_reviewed": 0,
            "cards_total": 0,
            "cards_due": 0,
            "review_card_ids": [],
            "questions_answered": 0,
            "questions_total": 0,
            "questions_correct": 0,
            "questions_to_review": 0,
            "retry_quiz": False,
            "next_review_at": None,
        }
        if cards:
            rows = self.conn.execute(
                "SELECT f.card_id,f.due_at,(json_extract(f.schedule,'$.reps')>0) FROM study_flashcards f "
                "WHERE f.deck_id=? AND json_extract(f.body,'$.suspended')=0",
                (cards["deck_id"],),
            ).fetchall()
            out.update(
                cards_total=len(rows),
                cards_reviewed=sum(bool(r[2]) for r in rows),
                cards_due=sum(r[1] <= self.desk.clock() for r in rows),
                review_card_ids=[r[0] for r in rows if r[1] <= self.desk.clock()],
                next_review_at=min((r[1] for r in rows), default=None),
                phase="flashcards",
                next_aid_id=cards["aid_id"],
            )
        if (
            cards
            and not quiz
            and out["cards_total"] > 0
            and out["cards_reviewed"] == out["cards_total"]
            and not out["cards_due"]
        ):
            out["phase"] = "make_quiz"
        if quiz:
            stored = self.desk.quiz(quiz["quiz_id"])["view"]
            out["questions_total"] = len(stored["items"])
            draft = stored.get("draft")
            result = self.saved_result(quiz["quiz_id"])
            if draft:
                out.update(questions_answered=len(draft["answers"]), phase="quiz", next_aid_id=quiz["aid_id"])
            elif result:
                replies, submitted = result
                out.update(
                    questions_answered=sum(r["verdict"] != "skipped" for r in replies["results"]),
                    questions_correct=sum(r["verdict"] == "correct" for r in replies["results"]),
                    questions_to_review=sum(r["verdict"] != "correct" for r in replies["results"]),
                )
                if out["questions_to_review"]:
                    retry = reviewed >= submitted
                    out.update(
                        phase="quiz" if retry else "review",
                        next_aid_id=quiz["aid_id"] if retry else root,
                        retry_quiz=retry,
                    )
                elif out["cards_due"]:
                    assert cards is not None  # cards_due is only counted when there are cards
                    out.update(phase="flashcards", next_aid_id=cards["aid_id"])
                else:
                    out.update(phase="complete", next_aid_id=quiz["aid_id"])
            elif not cards or out["cards_reviewed"] >= out["cards_total"]:
                out.update(phase="quiz", next_aid_id=quiz["aid_id"])
        if any(a["status"] != "ready" for a in (lesson, cards, quiz) if a):
            out.update(phase="unavailable", next_aid_id=root)
        return checked("StudyJourney", out)
