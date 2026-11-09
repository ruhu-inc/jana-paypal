"""Re-marking a withdrawn item's attempts against its corrected item (CUR-36, ASM-23, OPS-27): the learner replay.

A confirmed wrong key is withdrawn by the safety configuration and corrected under a new id (CUR-02). Each verdict
given on the old item's attempts is marked again by the same deterministic marker (ASM-02) against the corrected
item: the stored response of a part (`normalised_response`, `choice_id`) or the answer the assessment kept in the
transcript (`response_ref`). An attempt whose response is no longer held is re-marked `unverifiable`, which withdraws
the wrong key's evidence without inventing any (LRN-06). The new verdicts supersede the old ones and the learner
model is rebuilt from the log; everything stays on this device.

The misconceptions the corrected marking finds on an attempt are recorded with its re-mark, and every reader takes an
attempt's misconception evidence from its latest marking (`store.supersession`, CUR-36): what the wrong key alone
suggested is withdrawn, and a misconception the corrected marking also finds counts once. A re-mark that changed a
result leaves the student's notice waiting in the learner database (ASM-23, `learner.remark.take_notice`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_core.engine.assessment.errors import Refused
from agent_core.engine.marking import Verdict, mark
from agent_core.engine.signatures import evidence, pack_context, verdict_source
from agent_core.learner.remark import (
    Marked,
    marked_attempts,
    notice_writer,
    original_evidence_sha256,
    projections_match_replay,
    remark_payload,
)
from agent_core.learner.service import NewEvent

if TYPE_CHECKING:
    from agent_core.engine.assessment.service import AssessmentDesk

UNVERIFIABLE = "remark-no-response"  # the marker version of a re-mark with no stored response to mark


@dataclass(frozen=True)
class RemarkReport:
    """What one learner database's re-mark did: the numbers the OPS-27 wrong-key drill records."""

    old_item_id: str
    new_item_id: str
    job_id: str
    affected_attempts: int  # attempts at the withdrawn item
    remarked_attempts: int  # of those, attempts whose every verdict now reads as a re-mark
    appended: int  # superseding verdicts appended by this run (0 when it had already run)
    changed_results: int  # re-marked verdicts whose outcome or marks differ from the wrong key's (ASM-23 notice)
    evidence_recorded: int  # misconception matches the corrected marking found on the re-marked attempts (CUR-36)
    original_sha256: str  # the item's attempts and original verdicts before
    retained_sha256: str  # the same after: equal when nothing original changed
    projections_equal_full_replay: bool
    replay_sha256: str  # the learner tables' checksum after the rebuild

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def job_id(old_item_id: str, new_item_id: str) -> str:
    """The same correction always has the same job id, so re-running it is recognisable."""
    return "remark_" + hashlib.sha256(f"{old_item_id}\n{new_item_id}".encode()).hexdigest()[:24]


def _stored_answer(desk: AssessmentDesk, attempt: dict[str, Any]) -> tuple[bool, Any]:
    """(found, answer): the answer an assessment kept in the transcript for this attempt."""
    ref = attempt.get("response_ref")
    if not ref:
        return False, None
    row = desk.learner.conn.execute("SELECT student_text FROM turns WHERE turn_id = ?", (ref,)).fetchone()
    if row is None or row[0] is None:
        return False, None
    try:
        return True, json.loads(row[0])
    except ValueError:
        return False, None


async def _marking(desk: AssessmentDesk, item: dict[str, Any], marked: Marked) -> tuple[dict[str, Any], list[str]]:
    """The corrected marking of one verdict, and the misconceptions it matched (each part's, ASM-13)."""
    from agent_core.engine.assessment.service import aggregate_verdict

    attempt = marked.attempt
    parts = {str(p["id"]): p for p in item["parts"]}
    part = parts.get(str(attempt.get("part_id"))) if attempt.get("part_id") else None
    response = attempt.get("normalised_response", attempt.get("choice_id"))
    verdict: Verdict | None = None
    matched: list[str] = []
    if part is not None and response is not None:
        verdict = await mark(item, part["id"], response, desk._convert, desk.settings, desk.run_tool,
                             signatures=pack_context(desk.content, item))  # fmt: skip
        matched = [verdict.misconception_id] if verdict.misconception_id else []
    elif response is None:
        found, answer = _stored_answer(desk, attempt)
        if found:
            try:
                by_part = await desk._mark(item, answer)
                verdict = by_part.get(str(attempt["part_id"])) if attempt.get("part_id") else aggregate_verdict(by_part)
                matched = [verdict.misconception_id] if verdict is not None and verdict.misconception_id else []
            except Refused:  # the kept answer does not fit the corrected item's parts
                verdict = None
    if verdict is None:
        available = float(part["marks"]) if part is not None else float(sum(p["marks"] for p in item["parts"]))
        verdict = Verdict("unverifiable", 0, available, UNVERIFIABLE)
    payload = verdict.payload(verdict_id=marked.verdict["verdict_id"], attempt_id=attempt["attempt_id"],
                              closes_opportunity=bool(marked.verdict["closes_opportunity"]))  # fmt: skip
    return payload, list(dict.fromkeys(matched))


def _changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return any(before.get(k) != after.get(k) for k in ("outcome", "marks_awarded", "marks_available"))


async def remark_withdrawn(desk: AssessmentDesk, old_item_id: str, new_item_id: str) -> RemarkReport:
    """Re-mark every verdict on `old_item_id`'s attempts against the installed corrected item `new_item_id`, append
    the superseding verdicts and rebuild the learner model, in one transaction (ASM-23). Running it again appends
    nothing."""
    if old_item_id == new_item_id:
        raise ValueError("CUR-36: a corrected item has a new id")
    item = desk.content.record("items", new_item_id)
    if item is None:
        raise ValueError(f"CUR-36: the corrected item {new_item_id} is not installed")
    found = marked_attempts(desk.learner, old_item_id)
    before = original_evidence_sha256(desk.learner, old_item_id)
    library = desk.content.table("misconceptions")
    events, changed, matches = [], 0, 0
    for marked in found:
        marking, misconceptions = await _marking(desk, item, marked)
        payload = remark_payload(marked, marking)
        if payload is None:
            continue
        changed += _changed(marked.verdict, payload)  # against the verdict as it read before this run
        now = desk.clock_ms()
        events.append(NewEvent("verdict", payload, source="engine", session_id=marked.session_id, occurred_at=now))
        for mc_id in misconceptions:  # the corrected marking's matches, recorded with the re-mark (CUR-36)
            found_mc = evidence(item, mc_id, library, verdict_source(item), marked.attempt["attempt_id"])
            if found_mc is not None:
                matches += 1
                events.append(NewEvent("misconception_evidence", found_mc, source="engine",
                                       session_id=marked.session_id, occurred_at=now))  # fmt: skip
    job = job_id(old_item_id, new_item_id)
    desk.learner.supersede(events, extra=notice_writer(job, changed, desk.clock_ms()))
    after = marked_attempts(desk.learner, old_item_id)
    attempts = {m.attempt["attempt_id"] for m in after}
    remarked = {a for a in attempts if all(m.verdict.get("supersedes") for m in after if m.attempt["attempt_id"] == a)}
    equal, checksum = projections_match_replay(desk.learner)
    return RemarkReport(
        old_item_id=old_item_id,
        new_item_id=new_item_id,
        job_id=job,
        affected_attempts=len(attempts),
        remarked_attempts=len(remarked),
        appended=len(events) - matches,
        changed_results=changed,
        evidence_recorded=matches,
        original_sha256=before,
        retained_sha256=original_evidence_sha256(desk.learner, old_item_id),
        projections_equal_full_replay=equal,
        replay_sha256=checksum,
    )
