"""Explain it back: before a skill is marked mastered, the learner explains its key idea in their own words.

A mastery check whose items all passed does not confirm mastery at once when the KC has key points in its pack
(`key_points`, each with one follow-up question): the check waits in `explaining` for the learner's explanation,
typed or the transcript of what they said (the judging is the same either way). The Checker's judge decides each key
point and must quote the learner's own words for a point to pass; a quote the explanation does not contain fails it.

- Every point met: the `mastery_check` event passes and the KC is mastered.
- A first explanation that misses a point: one targeted follow-up, the pack's question for the first missed point,
  which probes it and never states it; the next explanation is judged on the missed points only.
- The follow-up answered without the point: not yet. The check fails as a failed item does, with its retry date, and
  nothing of the answer is given.
- No judge reachable: the explanation is not counted and the check keeps waiting (`unavailable`), never a pass.

Nothing the learner writes is kept: the check stores only which points were met and the outcome."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from agent_core.engine.assessment.errors import Refused
from agent_core.store.assessment import dump

DAY_MS = 86_400_000
# The judge's criteria (key point id, what the explanation must say) and the learner's words: each point's
# {id, pass, evidence, status}, or None when no judge answered.
ExplainJudge = Callable[[Sequence[tuple[str, str]], str], Awaitable[list[dict[str, Any]] | None]]


def key_points(content: Any, kc_id: str) -> list[dict[str, Any]]:
    """The KC's key points from its pack record ([] when it has none, so its check confirms mastery as before)."""
    record = content.record("kcs", kc_id) or {}
    return cast(
        list[dict[str, Any]], [p for p in record.get("key_points") or [] if isinstance(p, Mapping) and p.get("id")]
    )


def _english(text: Any) -> str:
    return str(text.get("en", "")) if isinstance(text, Mapping) else str(text or "")


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s/=+\-.\u00d7\u00f7]", " ", text.casefold()).split())


def quoted(evidence: str, text: str) -> bool:
    """Every evidence excerpt must be the learner's own words, not a judge paraphrase.

    A compound criterion may need multiple quoted spans. Accept only balanced excerpts joined additively,
    each at least three words, found in source order; unsupported commentary or a fabricated span fails.
    """
    found = _plain(evidence)
    source = _plain(text)
    if len(found.split()) >= 3 and found in source:
        return True
    if len(evidence) > 4096:
        return False
    excerpts = list(re.finditer(r'"([^"\n]+)"|\u201c([^\u201d\n]+)\u201d', evidence))
    if not 2 <= len(excerpts) <= 8 or evidence[:excerpts[0].start()].strip():
        return False
    if evidence[excerpts[-1].end():].strip() not in ("", "."):
        return False
    position = 0
    end = excerpts[0].start()
    for index, excerpt in enumerate(excerpts):
        if index and not re.fullmatch(r"\s*(?:,?\s*and|[,;]|\.\.\.|\u2026)\s*", evidence[end:excerpt.start()]):
            return False
        words = _plain(excerpt[1] or excerpt[2])
        at = source.find(words, position)
        if len(words.split()) < 3 or at < 0:
            return False
        position, end = at + len(words), excerpt.end()
    return True


def run_state(state: Mapping[str, Any]) -> str:
    """The `assessment_runs.state` column: a check waiting for the learner's explanation is still in progress there
    (the run's body holds `explaining`), so retention and the storage contract's states are unchanged."""
    return "in_progress" if state["state"] == "explaining" else str(state["state"])


def explain_view(state: Mapping[str, Any], content: Any) -> dict[str, Any] | None:
    """The `explain` field of the assessment's view, while it is explaining or once it has explained."""
    explain = state.get("explain")
    if not explain:
        return None
    return {"statement": str(content.kc_statement(state["kc_id"]) or "")[:200], "follow_up": explain.get("follow_up"),
            "attempts": int(explain["attempts"]), "outcome": str(explain["outcome"])}  # fmt: skip


class ExplainCommands:
    """Mixed into `AssessmentDesk`: the explanation that confirms a passed check (explain it back)."""

    explain_judge: ExplainJudge | None
    explain_possible: Callable[[], bool]
    settings: Any
    content: Any
    busy: set[str]
    clock_ms: Callable[[], int]

    def explains(self, kc_id: str) -> bool:
        """Whether a passed check on this KC waits for the learner's explanation."""
        return self.explain_judge is not None and self.explain_possible() and bool(key_points(self.content, kc_id))

    def begin_explaining(self, state: dict[str, Any]) -> None:
        state.update(state="explaining", current_id=None)
        state["explain"] = {"attempts": 0, "met": [], "follow_up": None, "outcome": "awaiting"}

    async def explain_back(self, identity: str, client_id: str, text: str, modality: str) -> dict[str, Any]:
        """`POST /v1/assessments/{assessment_id}/explain`: the learner's explanation judged against the key points."""
        state = self._get(identity, "mastery_check")  # type: ignore[attr-defined]
        fingerprint = hashlib.sha256(dump([text, modality]).encode()).hexdigest()
        cached = state["clients"].get(client_id)
        if cached:
            if cached["fingerprint"] != fingerprint:
                raise Refused("request_conflict")
            return dict(cached["reply"])
        if state["state"] != "explaining" or self.explain_judge is None:
            raise Refused("not_explaining")
        if state["id"] in self.busy:
            raise Refused("tutor_locked")
        self.busy.add(state["id"])
        try:
            explain = state["explain"]
            points = [p for p in key_points(self.content, state["kc_id"]) if p["id"] not in explain["met"]]
            judged = await self.explain_judge([(str(p["id"]), _english(p["text"])) for p in points], text)
            events = []
            if judged is None:
                explain["outcome"] = "unavailable"  # not counted: the check keeps waiting, never a pass
            else:
                passed = {str(j["id"]) for j in judged if j.get("pass") and quoted(str(j.get("evidence", "")), text)}
                explain["met"] = sorted({*explain["met"], *passed})
                explain["attempts"] = int(explain["attempts"]) + 1
                missed = [p for p in points if p["id"] not in passed]
                if not missed:
                    explain.update(outcome="passed", follow_up=None)
                    state.update(state="completed")
                    events.append(self._check_event(state, "pass", "passed"))
                elif explain["attempts"] < 2:
                    explain.update(outcome="follow_up", follow_up=_english(missed[0]["follow_up"])[:300])
                else:
                    explain.update(outcome="not_yet", follow_up=None)
                    retry = self.clock_ms() + int(self.settings.mastery_check_retry_days * DAY_MS)
                    state.update(state="failed", retry_at=retry)
                    events.append(self._check_event(state, "fail", "failed"))
            reply = self._assessment_view(state)  # type: ignore[attr-defined]
            state["clients"][client_id] = {"fingerprint": fingerprint, "reply": reply}
            self._commit(state, events)  # type: ignore[attr-defined]
            return self._assessment_view(state)  # type: ignore[attr-defined,no-any-return]
        finally:
            self.busy.discard(state["id"])

    def _check_event(self, state: Mapping[str, Any], result: str, check: str) -> Any:
        last = state["answered"][-1] if state["answered"] else {}
        payload = {"kc_id": state["kc_id"], "item_index": max(1, min(len(state["answered"]), 3)),
                   "attempt_id": last.get("attempt_id") or "att_" + str(state["id"]), "item_result": result,
                   "check_state": check}  # fmt: skip
        return self._event("mastery_check", payload, state["id"])  # type: ignore[attr-defined]
