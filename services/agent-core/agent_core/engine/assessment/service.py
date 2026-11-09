"""Offline assessment commands and transactional learning evidence (ASM-10 to ASM-24, API-27)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from agent_core.config import Settings
from agent_core.engine.adaptation import practice_selection
from agent_core.engine.assessment.adaptive import probe_problems, select_diagnostic
from agent_core.engine.assessment.criteria import JudgeCriterion
from agent_core.engine.assessment.errors import Refused
from agent_core.engine.assessment.evidence import install
from agent_core.engine.assessment.explain import ExplainCommands, ExplainJudge, explain_view, run_state
from agent_core.engine.assessment.items import pin_item, present, ready, response, validate_answer, worked_answer
from agent_core.engine.assessment.mocks import MockCommands
from agent_core.engine.assessment.past_papers import PastPaperCommands
from agent_core.engine.assessment.probes import ProbeCommands
from agent_core.engine.assessment.variants import VariantCommands
from agent_core.engine.learn import SUBJECT_CODES, SUBJECT_NAMES
from agent_core.engine.marking import Converted, RunTool, Verdict, mark
from agent_core.engine.safety import redact_personal
from agent_core.engine.signatures import evidence, pack_context, verdict_source
from agent_core.learner.mastery import Ability, KcEvidence, MasteryParams, item_probability, kc_probability
from agent_core.learner.service import Learner, NewEvent
from agent_core.memory.render import BANDS
from agent_core.store.assessment import AssessmentStore, dump
from agent_core.store.ids import new_ulid
from agent_core.store.pack_index import PackIndex
from agent_core.store.supersession import read_log

DAY_MS = 86_400_000


def aggregate_verdict(verdicts: Mapping[str, Verdict]) -> Verdict:
    """One item's verdict from its parts' verdicts: what an assessment attempt records."""
    outcomes = {v.outcome for v in verdicts.values()}
    outcome = (
        "correct"
        if outcomes == {"correct"}
        else (
            "unverifiable"
            if "unverifiable" in outcomes
            else "partial"
            if outcomes & {"correct", "partial"}
            else "incorrect"
        )
    )
    return Verdict(
        outcome,
        sum(v.marks_awarded for v in verdicts.values()),
        sum(v.marks_available for v in verdicts.values()),
        "assessment-v1",
        tuple(c for v in verdicts.values() for c in v.check_ids),
        misconception_id=next((v.misconception_id for v in verdicts.values() if v.misconception_id), None),
    )


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def millis(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class AssessmentDesk(MockCommands, PastPaperCommands, VariantCommands, ProbeCommands, ExplainCommands):
    def __init__(
        self,
        learner: Learner,
        content: PackIndex,
        settings: Settings,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        monotonic_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000,
        run_tool: RunTool | None = None,
        judge_method: JudgeCriterion | None = None,
        explain_judge: ExplainJudge | None = None,
        explain_possible: Callable[[], bool] = lambda: True,
        recover: bool = False,
    ) -> None:
        self.learner, self.content, self.settings = learner, content, settings
        self.clock_ms, self.monotonic_ms, self.run_tool = clock_ms, monotonic_ms, run_tool
        self.judge_method = judge_method
        self.explain_possible = explain_possible
        self.explain_judge = explain_judge  # explain it back before mastery; None keeps the check as it was
        install(learner, settings)
        self.store = AssessmentStore(learner.conn)
        self.running: dict[str, int] = {}
        self.paused: dict[str, int] = {}
        self.variant_proofs: set[tuple[str, str]] = set()
        self.busy: set[str] = set()
        self.policy_decisions: list[Any] = []
        if recover:
            self.recover()

    def _get(self, identity: str, kind: str | None = None) -> dict[str, Any]:
        state = self.store.get(identity.split("_", 1)[-1])
        if state is None or (kind is not None and state["kind"] != kind):
            raise Refused("not_found")
        return state

    def _commit(
        self, state: dict[str, Any], events: Sequence[NewEvent] = (), extra: Callable[[], None] | None = None
    ) -> None:
        now = self.clock_ms()

        def save(conn: Any, stored: Any) -> None:
            if extra is not None:
                extra()
            self.store.put(state, now)
            if state["kind"] in ("diagnostic", "mastery_check"):
                conn.execute(
                    "INSERT INTO assessment_runs (assessment_id, kind, state, body, updated_at) VALUES"
                    " (?, ?, ?, ?, ?) ON CONFLICT(assessment_id) DO UPDATE SET state=excluded.state,"
                    " body=excluded.body, updated_at=excluded.updated_at",
                    (state["id"], state["kind"], run_state(state), dump(state), now),
                )

        self.learner.record_batch(list(events), extra=save)

    def _event(
        self, name: str, payload: dict[str, Any], session: str | None = None, source: str = "engine"
    ) -> NewEvent:
        return NewEvent(name, payload, source=source, session_id=session, occurred_at=self.clock_ms())

    def _reserved(self) -> set[str]:
        reserved = {
            q["item_id"] for p in self.content.table("mock_papers") for s in p["sections"] for q in s["questions"]
        }
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in self.content.table("items"):
            if (item.get("review") or {}).get("status") == "published":
                groups.setdefault(".".join(item["id"].split(".")[1:3]), []).append(item)
        for bank in groups.values():
            needed = math.ceil(len(bank) * self.settings.mock_reserved_bank_share)
            already = sum(item["id"] in reserved for item in bank)
            extra = max(0, needed - already)
            if not extra:
                continue
            candidates = sorted(
                (i for i in bank if i["id"] not in reserved and i["type"] != "mcq_diagnostic"),
                key=lambda i: hashlib.sha256(i["id"].encode()).digest(),
            )
            # Explicit mock-paper questions remain reserved. Fill the additional share from redundant
            # surfaces first, so a random hash order does not consume a KC's only third context.
            surfaces: dict[str, Counter[str]] = {}
            by_item = {}
            for item in candidates:
                if not ready(item, self.content, self.settings, automatic=True):
                    continue
                surface = self._surface(item)
                by_item[item["id"]] = surface
                for kc in item["kc_ids"]:
                    surfaces.setdefault(kc, Counter())[surface] += 1
            deferred = []
            for item in candidates:
                candidate_surface = by_item.get(item["id"])
                if candidate_surface is not None and any(
                    surfaces[kc][candidate_surface] == 1 and len(surfaces[kc]) <= self.settings.mastery_check_items
                    for kc in item["kc_ids"]
                ):
                    deferred.append(item)
                    continue
                reserved.add(item["id"])
                if candidate_surface is not None:
                    for kc in item["kc_ids"]:
                        surfaces[kc][candidate_surface] -= 1
                        if not surfaces[kc][candidate_surface]:
                            del surfaces[kc][candidate_surface]
                extra -= 1
                if not extra:
                    break
            # A genuinely too-small bank cannot meet both constraints. Keep the mock reserve intact;
            # the mastery preflight below leaves that check waiting without exposing an incomplete set.
            reserved.update(i["id"] for i in deferred[:extra])
        return reserved

    def _exposed(self) -> dict[str, int]:
        seen: dict[str, int] = {}
        for event in self.learner.log.by_seq():
            if event.type == "item_exposed" and not event.erased:
                item_id = event.payload["item_id"]
                seen[item_id] = max(seen.get(item_id, 0), event.occurred_at)
        return seen

    def check_possible(self, kc_id: str) -> bool:
        """Whether a mastery check of this KC can start now: enough differently worded questions for all its items.
        With fewer the check would open on "the next question is not available yet", so practice is offered instead."""
        questions = self.eligible("mastery_check", [kc_id])
        return len({self._surface(i) for i in questions}) >= int(self.settings.mastery_check_items)

    def eligible(self, surface: str, kc_ids: Sequence[str] = (), subject: str | None = None) -> list[dict[str, Any]]:
        reserved = self._reserved()
        exposed = self._exposed() if surface in ("mastery_check", "review", "diagnostic", "practice") else {}
        cutoff = self.clock_ms() - self.settings.practice_exposure_days * DAY_MS
        prefix = f"itm.{SUBJECT_CODES[subject]}." if subject in SUBJECT_CODES else "itm."
        protected = {
            r.get("current_id")
            for r in self.store.all()
            if r["kind"] in ("diagnostic", "mastery_check") and r["state"] in ("in_progress", "waiting", "stopped")
        }
        for runtime in self.store.all():
            if runtime["kind"] == "mock":
                protected.update(i["id"] for i in runtime["items"].values())
            elif runtime["kind"] == "practice" and runtime["state"] == "in_progress":
                protected.update(i["id"] for i in runtime["items"][runtime["index"] :])
        out = []
        for item in self.content.table("items"):
            iid = item["id"]
            if not str(iid).startswith(prefix) or iid in reserved or iid in protected:
                continue
            if (item.get("review") or {}).get("status") != "published":
                continue
            if kc_ids and not set(item["kc_ids"]) & set(kc_ids):
                continue
            if surface != "diagnostic" and item["type"] == "mcq_diagnostic":
                continue
            if item["type"] == "mcq_diagnostic" and probe_problems(
                [item], self.content.table("misconceptions"), self.settings
            ):
                continue
            if surface in ("mastery_check", "review", "diagnostic") and iid in exposed:
                continue
            if surface == "practice" and exposed.get(iid, 0) > cutoff:
                continue
            if ready(item, self.content, self.settings, automatic=surface in ("mastery_check", "diagnostic")):
                out.append(pin_item(item, self.content))
        return sorted(out, key=lambda i: str(i["id"]))

    def _exposure(self, item: Mapping[str, Any], surface: str, session: str | None = None) -> NewEvent:
        shown = present(item, self.content, self.settings)
        return self._event(
            "item_exposed", {"item_id": item["id"], "pack_version": shown["pack_version"], "surface": surface}, session
        )

    def search(self, query: str) -> list[dict[str, Any]]:
        """Worked-solution search (UXS-51, ASM-22): installed ordinary items only, after the mock-reserved pool and
        every item of a protected assessment are left out, matched when the stem holds every word of the query. Each
        preview returned is recorded as seen before it is shown; a blank query matches nothing and records nothing."""
        words = [w for w in query.casefold().split() if w]
        if not words:
            return []
        items = [i for i in self.eligible("search") if all(w in str(i["stem"]).casefold() for w in words)]
        items = items[: self.settings.search_results_max]
        replies = [present(i, self.content, self.settings) for i in items]
        self.learner.record_batch([self._exposure(i, "search_preview") for i in items])
        return replies

    def solution(self, item_id: str) -> dict[str, Any]:
        """An item's full worked solution on request, with no attempt needed (UXS-51, PED-09, D87), as the board
        shows a worked example (`WorkedExampleView`). The same filter as search applies first: a reserved, mock,
        check or protected item, or one not installed, is not found and nothing of it is shown. Opening it records
        the item as seen (ASM-22)."""
        if self.mock_active():
            raise Refused("tutor_locked")  # solutions wait while a mock sitting is open (PED-09, AGT-24)
        item = next((i for i in self.eligible("search") if i["id"] == item_id), None)
        steps = list(((item or {}).get("solution") or {}).get("steps") or [])
        if item is None or not steps:
            raise Refused("not_found")
        reference = (item.get("solution") or {}).get("worked_example_id")
        example = self.content.record("worked_examples", reference) if reference else None
        parts = item.get("parts") or []
        last = steps[-1]
        keys = [p["answer"]["check_id"] for p in parts if p["answer"].get("check_id")]
        check = last.get("check_id") or (keys[0] if keys else "")
        final = example["final_answer"] if example else {"latex": last["latex"], "check_id": check}
        self.learner.record_batch([self._exposure(item, "search_open")])
        return {
            "worked_example_id": str(reference or item_id),
            "problem": str(item["stem"]),
            "steps": [{"n": i + 1, "latex": str(s["latex"]), "reason": str(s.get("reason") or "")[:120],
                       "check_id": s.get("check_id")} for i, s in enumerate(steps)],
            "final_answer": {"latex": str(final["latex"]), "check_id": str(final.get("check_id") or "")},
            "faded_steps": [],
        }  # fmt: skip

    def accept_variant(self, item: dict[str, Any], tool_check_id: str | None) -> dict[str, Any]:
        if not tool_check_id or (dump(item), tool_check_id) not in self.variant_proofs:
            raise Refused("variant_key_unverified")
        out = copy.deepcopy(item)
        for part in out["parts"]:
            part["answer"]["check_id"] = tool_check_id
        return out

    def guard_tutor(self) -> None:
        if self.mock_active() or any(
            s["kind"] == "mastery_check"
            and s["state"] in ("in_progress", "stopped")
            and s.get("current_id") is not None
            for s in self.store.all()
        ):
            raise Refused("tutor_locked")

    def mock_active(self) -> bool:
        return any(
            s["kind"] == "mock" and self._exam_row(s["id"])[0] in ("in_progress", "paused", "interrupted")
            for s in self.store.all()
        )

    def _new(self, kind: str, subject: str, **values: Any) -> dict[str, Any]:
        if subject not in SUBJECT_CODES:
            raise Refused("invalid_subject")
        return {
            "id": new_ulid(),
            "kind": kind,
            "subject": subject,
            "state": "in_progress",
            "started": self.clock_ms(),
            "answered": [],
            "clients": {},
            **values,
        }

    def _probabilities(self, state: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, KcEvidence], Ability]:
        rows = self.learner.conn.execute("SELECT kc_id, succ_w, fail_w, implied_w FROM kc_mastery").fetchall()
        ev = {k: KcEvidence(succ_w=s, fail_w=f, implied_w=i or 0) for k, s, f, i in rows}
        row = self.learner.conn.execute(
            "SELECT theta,n FROM subject_ability WHERE subject=?", (state["subject"],)
        ).fetchone()
        ability = Ability(*row) if row else Ability()
        params = MasteryParams.from_settings(self.settings)
        prefix = f"kc.{SUBJECT_CODES[state['subject']]}."
        probs = {
            k: kc_probability(ability.theta, ev.get(k, KcEvidence()), params, self.content.reference_difficulty(k))
            for k in self.content.kcs
            if k.startswith(prefix)
        }
        return probs, ev, ability

    def start_assessment(self, kind: str, subject: str, kc_id: str | None = None) -> dict[str, Any]:
        if kind not in ("diagnostic", "mastery_check") or subject not in SUBJECT_CODES:
            raise Refused("invalid_assessment")
        if self.mock_active():
            raise Refused("tutor_locked")
        if kind == "mastery_check" and (
            kc_id not in self.content.kcs or not str(kc_id).startswith(f"kc.{SUBJECT_CODES[subject]}.")
        ):
            raise Refused("invalid_kc")
        for state in self.store.all():
            if state["kind"] == kind and state["subject"] == subject:
                if state["state"] in ("in_progress", "waiting", "stopped", "explaining"):
                    return self.assessment(state["id"])
                if state.get("kc_id") == kc_id and state.get("retry_at") and self.clock_ms() < state["retry_at"]:
                    raise Refused("retry_not_due")
        state = self._new(
            kind,
            subject,
            kc_id=kc_id,
            weighted_pending=[],
            observations=[],
            current_id=None,
            selected={},
            available_at=None,
            retry_at=None,
            surfaces=[],
            anchors=[],
            active_ms=0,
            last_active=self.clock_ms(),
        )
        state["anchors"] = [k for k in self.content.kcs if k.startswith(f"kc.{SUBJECT_CODES[subject]}.")]
        # Proposed pack field (ASM-10); deterministic fallback while older packs have no anchors.
        root = self.content.nodes.get(f"cur.{SUBJECT_CODES[subject]}.o", {})
        state["anchors"] = list(root.get("diagnostic_anchors") or state["anchors"])
        events = self._advance(state)
        self._commit(state, events)
        return self._assessment_view(state)

    def _advance(self, state: dict[str, Any]) -> list[NewEvent]:
        if state["kind"] == "mastery_check":
            count = len(state["answered"])
            if count >= self.settings.mastery_check_items:
                state["state"], state["current_id"] = "completed", None
                return []
            # Rebuild from pinned questions: older checkpoints stored broad context tags (for example,
            # all transport questions), which incorrectly exhausted an otherwise varied bank.
            state["surfaces"] = [self._surface(i) for i in state["selected"].values()]
            bank = [
                i
                for i in self.eligible("mastery_check", [state["kc_id"]], state["subject"])
                if self._surface(i) not in state["surfaces"]
            ]
            if len({self._surface(i) for i in bank}) < self.settings.mastery_check_items - count:
                state.update(state="waiting", current_id=None, available_at=None)
                return []
            if count == self.settings.mastery_check_items - 1:
                due = state["first_served"] + int(self.settings.mastery_check_min_spacing_hours * 3_600_000)
                if self.clock_ms() < due:
                    state.update(state="waiting", current_id=None, available_at=due)
                    return []
            chosen = bank[0] if bank else None
        else:
            if (
                state["active_ms"] >= self.settings.diagnostic_max_minutes * 60_000
                or len(state["answered"]) >= self.settings.diagnostic_max_items
            ):
                state.update(state="completed", current_id=None)
                return []
            probs, ev, ability = self._probabilities(state)
            resolved = all(
                abs(probs.get(k, 0.5) - 0.5) >= self.settings.diagnostic_resolved_distance for k in state["anchors"]
            )
            if resolved and len(state["answered"]) >= self.settings.diagnostic_min_items:
                state.update(state="completed", current_id=None)
                return []
            bank = self.eligible("diagnostic", subject=state["subject"])
            params = MasteryParams.from_settings(self.settings)
            for i in bank:
                weights = {k: 1 / len(i["kc_ids"]) for k in i["kc_ids"]}
                for k in weights:
                    ev.setdefault(k, KcEvidence())
                i["predicted_success"] = item_probability(
                    ability.theta, weights, ev, params, self.content.item_difficulty(i["id"])
                )
            selection_probs = (
                {k: p for k, p in probs.items() if k in state["anchors"]} if not state["answered"] else probs
            )
            chosen = select_diagnostic(
                selection_probs, self.content.dependents(), bank, self.settings.diagnostic_resolved_distance
            )
            if chosen is None and len(state["answered"]) < self.settings.diagnostic_min_items and bank:
                chosen = min(bank, key=lambda i: (abs(i["predicted_success"] - 0.5), i["id"]))
        if chosen is None:
            state.update(state="completed" if state["kind"] == "diagnostic" else "waiting", current_id=None)
            return []
        state.update(state="in_progress", current_id=chosen["id"], available_at=None)
        state["selected"][chosen["id"]] = chosen
        state.setdefault("first_served", self.clock_ms())
        state["surfaces"].append(self._surface(chosen))
        return [self._exposure(chosen, state["kind"], state["id"])]

    @staticmethod
    def _surface(item: Mapping[str, Any]) -> str:
        if item.get("template_id"):
            return "template:" + str(item["template_id"])
        # Tags describe a domain, not a question template. With older packs that lack template ids,
        # distinguish the actual context and requested task, while grouping number-only variants.
        text = [str(item["stem"]), *(str(p["prompt"]) for p in item.get("parts", []))]
        normal = [" ".join(re.sub(r"[+\-]?\d+(?:[.,]\d+)*(?:e[+\-]?\d+)?", "#", t.casefold()).split()) for t in text]
        return "question:" + hashlib.sha256(dump(normal).encode()).hexdigest()

    def _assessment_view(self, state: Mapping[str, Any]) -> dict[str, Any]:
        item = state["selected"].get(state["current_id"])
        return {
            "assessment_id": state["id"],
            "kind": state["kind"],
            "subject": state["subject"],
            "kc_id": state["kc_id"],
            "state": state["state"],
            "answered_count": len(state["answered"]),
            "current_item": present(item, self.content, self.settings, diagnostic=state["kind"] == "diagnostic")
            if item
            else None,
            "available_at": iso(state["available_at"]) if state["available_at"] else None,
            "retry_at": iso(state["retry_at"]) if state["retry_at"] else None,
            "outcome_topics": self._outcome_topics(state),
            "answered_items": [dict(a) for a in state["answered"]],
            **({"last_mark": state.get("last_mark")} if state["kind"] == "mastery_check" else {}),
            **({"explain": explain_view(state, self.content)} if state.get("explain") else {}),
        }

    def _outcome_topics(self, state: Mapping[str, Any]) -> list[dict[str, str]]:
        rows = dict(self.learner.conn.execute("SELECT kc_id,status FROM kc_mastery"))
        topics: dict[str, str] = {}
        for kc in state["anchors"] if state["kind"] == "diagnostic" else [state["kc_id"]]:
            if kc in self.content.kcs:
                topics[self.content.kcs[kc].node_id] = rows.get(kc, "not_started")
        return [{"topic_id": k, "status": v} for k, v in sorted(topics.items())]

    def assessment(self, identity: str, *, serve_waiting: bool = True) -> dict[str, Any]:
        state = self._get(identity)
        if state["kind"] not in ("diagnostic", "mastery_check"):
            raise Refused("not_found")
        # Opening a scheduled check is the explicit action that may serve its first question.
        if serve_waiting:
            state.pop("scheduled", None)
        events: list[NewEvent] = []
        if state["state"] == "in_progress" and state["kind"] == "diagnostic":
            state["active_ms"] += max(0, self.clock_ms() - state["last_active"])
            state["last_active"] = self.clock_ms()
            if state["active_ms"] >= self.settings.diagnostic_max_minutes * 60_000:
                state.update(state="completed", current_id=None)
        elif serve_waiting and state["state"] == "waiting" and not self.mock_active():
            if state["available_at"] is None or self.clock_ms() >= state["available_at"]:
                events = self._advance(state)
        self._commit(state, events)
        return self._assessment_view(state)

    def list_assessments(self) -> list[dict[str, Any]]:
        return [
            self.assessment(s["id"], serve_waiting=False)
            for s in self.store.all()
            if s["kind"] in ("diagnostic", "mastery_check")
        ]

    def stop_assessment(self, identity: str) -> dict[str, Any]:
        self.assessment(identity, serve_waiting=False)
        state = self._get(identity)
        if state["state"] in ("in_progress", "waiting"):
            state["state"] = "stopped"
            self._commit(state)
        return self._assessment_view(state)

    def resume_assessment(self, identity: str) -> dict[str, Any]:
        if self.mock_active():
            raise Refused("tutor_locked")
        state = self._get(identity)
        if state["state"] != "stopped":
            raise Refused("invalid_transition")
        state["last_active"] = self.clock_ms()
        state["state"] = "in_progress" if state["current_id"] else "waiting"
        self._commit(state)
        return self.assessment(identity)

    def diagnostic_observations(self, identity: str) -> list[tuple[int, float]]:
        return [(o["y"], o["weight"]) for o in self._get(identity)["observations"]]

    async def _convert(self, quantity: str, to: str) -> Converted | None:
        if self.run_tool is None:
            return None
        result, check_id = await self.run_tool("units_convert", {"quantity": quantity, "to": to})
        value = ((result or {}).get("value") or {}).get("decimal")
        return Converted(Decimal(str(value)), check_id) if value is not None and check_id else None

    async def _mark(self, item: Mapping[str, Any], answer: Any) -> dict[str, Verdict]:
        if answer is None:
            return {p["id"]: Verdict("incorrect", 0, p["marks"], "assessment-unanswered") for p in item["parts"]}
        try:
            typed = validate_answer(item, answer)
        except ValueError as error:
            raise Refused(str(error)) from None
        responses = {a["part_id"]: response(a, item) for a in typed["parts"]}
        context = pack_context(self.content, item)
        out = {}
        for p in item["parts"]:
            if p["id"] not in responses:
                out[p["id"]] = Verdict("incorrect", 0, p["marks"], "assessment-unanswered")
            else:
                out[p["id"]] = await mark(
                    item,
                    p["id"],
                    responses if item["type"] == "structured" else responses[p["id"]],
                    self._convert,
                    self.settings,
                    self.run_tool,
                    signatures=context,
                )
        return out

    def _attempt_events(
        self,
        state: Mapping[str, Any],
        item: Mapping[str, Any],
        verdicts: Mapping[str, Verdict],
        answer: Any,
        time_ms: int,
    ) -> tuple[str, list[NewEvent], Callable[[], None]]:
        if item["type"] == "structured" and all(p.get("kc_ids") for p in item["parts"]):
            # Each part is its own opportunity. Error carried forward can credit its own KC, never the KC of
            # the earlier wrong part. Separate step indices keep projection closure/history independent.
            events, transcripts = [], []
            first = ""
            for index, part in enumerate(item["parts"], 1):
                part_item = {**item, "type": "numeric", "kc_ids": part["kc_ids"], "parts": [part]}
                aid, own, write_part = self._attempt_events(
                    state,
                    part_item,
                    {part["id"]: verdicts[part["id"]]},
                    answer,
                    time_ms // len(item["parts"]),
                )
                first = first or aid
                own[0].payload["step_index"] = index
                own[0].payload["part_id"] = part["id"]
                own[1].payload.update(
                    verdicts[part["id"]].payload(
                        verdict_id=own[1].payload["verdict_id"], attempt_id=aid, closes_opportunity=True
                    )
                )
                events.extend(own)
                transcripts.append(write_part)

            def write_parts() -> None:
                for write in transcripts:
                    write()

            return first, events, write_parts
        aid, tid = new_ulid(), new_ulid()
        sid = state["id"]
        attempt = {
            "attempt_id": aid,
            "session_id": sid,
            "mode": state["kind"],
            "item_id": item["id"],
            "problem_ref": item["id"],
            "kc_ids": [{"kc_id": k, "weight": 1 / len(item["kc_ids"])} for k in item["kc_ids"]],
            "kc_source": "pack",
            "attempt_index": 1,
            "response_kind": "typed",
            "response_ref": "trn_" + tid,
            "time_ms": max(0, time_ms),
        }
        aggregate = aggregate_verdict(verdicts)
        events = [
            self._event("attempt", attempt, sid, "student"),
            self._event(
                "verdict", aggregate.payload(verdict_id=new_ulid(), attempt_id=aid, closes_opportunity=True), sid
            ),
        ]
        for verdict in verdicts.values():
            if verdict.misconception_id:
                payload = evidence(
                    item, verdict.misconception_id, self.content.table("misconceptions"), verdict_source(item), aid
                )
                if payload:
                    events.append(self._event("misconception_evidence", payload, sid))

        def transcript() -> None:
            self.learner.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, mode, subject, started_at) VALUES (?, ?, ?, ?)",
                (sid, state["kind"], state["subject"], self.clock_ms()),
            )
            index = self.learner.conn.execute(
                "SELECT coalesce(max(idx),-1)+1 FROM turns WHERE session_id=?", (sid,)
            ).fetchone()[0]
            self.learner.conn.execute(
                "INSERT INTO turns (turn_id, session_id, idx, speaker, status, student_text, input_kind,"
                " started_at, ended_at) VALUES (?, ?, ?, 'student', 'complete', ?, 'typed', ?, ?)",
                ("trn_" + tid, sid, index, redact_personal(dump(answer))[0], self.clock_ms(), self.clock_ms()),
            )

        return aid, events, transcript

    async def assessment_answer(
        self, identity: str, client_id: str, item_id: str, answer: Any, *, not_learned: bool = False
    ) -> dict[str, Any]:
        state = self._get(identity)
        fingerprint = hashlib.sha256(dump([item_id, answer, not_learned]).encode()).hexdigest()
        cached = state["clients"].get(client_id)
        if cached:
            if cached["fingerprint"] != fingerprint:
                raise Refused("request_conflict")
            return dict(cached["reply"])
        self.assessment(identity)
        state = self._get(identity)
        if self.mock_active() or state["id"] in self.busy:
            raise Refused("tutor_locked")
        if state["state"] != "in_progress" or state["current_id"] != item_id:
            raise Refused("item_not_current")
        if not_learned and (state["kind"] != "diagnostic" or answer is not None):
            raise Refused("not_learned_not_allowed")
        if not not_learned and answer is None:
            raise Refused("answer_required")
        self.busy.add(state["id"])
        try:
            item = state["selected"][item_id]
            events: list[NewEvent] = []
            extra: Callable[[], None] | None = None
            aid = None
            explaining = False
            if not_learned:
                observation = {
                    "y": 0,
                    "weight": self.settings.diagnostic_not_learned_weight,
                    "kc_id": item["kc_ids"][0],
                    "at": self.clock_ms(),
                }
                verdicts = await self._mark(item, None)
                aid, events, extra = self._attempt_events(state, item, verdicts, None, 0)
                state["observations"].append(observation)
            else:
                verdicts = await self._mark(item, answer)
                if state["kind"] == "mastery_check" and any(v.outcome == "unverifiable" for v in verdicts.values()):
                    raise Refused("worker_unavailable")
                aid, events, extra = self._attempt_events(state, item, verdicts, answer, 0)
                correct = all(v.outcome == "correct" for v in verdicts.values())
                state["observations"].append(
                    {"y": int(correct), "weight": 1, "kc_id": item["kc_ids"][0], "at": self.clock_ms()}
                )
            if state["kind"] == "diagnostic":
                events.insert(
                    0,
                    self._event(
                        "diagnostic_item",
                        {
                            "subject": state["subject"],
                            "kc_id": item["kc_ids"][0],
                            "item_id": item_id,
                            "response": "not_learned" if not_learned else "answered",
                            "attempt_id": aid,
                        },
                        state["id"],
                    ),
                )
            else:
                passed = all(v.outcome == "correct" for v in verdicts.values())
                result = aggregate_verdict(verdicts)
                state["last_mark"] = {
                    "verdict": "partially_correct" if result.outcome == "partial" else result.outcome,
                    "marks_awarded": result.marks_awarded,
                    "marks_total": result.marks_available,
                }
                count = len(state["answered"]) + 1
                done = count == self.settings.mastery_check_items
                # Explain it back: the last item passed on a KC with key points waits for the learner's explanation.
                explaining = passed and done and self.explains(state["kc_id"])
                events.append(
                    self._event(
                        "mastery_check",
                        {
                            "kc_id": state["kc_id"],
                            "item_index": count,
                            "attempt_id": aid,
                            "item_result": "pass" if passed else "fail",
                            "check_state": "failed"
                            if not passed
                            else "passed"
                            if done and not explaining
                            else "in_progress",
                        },
                        state["id"],
                    )
                )
                if not passed:
                    state.update(
                        state="failed", retry_at=self.clock_ms() + int(self.settings.mastery_check_retry_days * DAY_MS)
                    )
            state["answered"].append({"item_id": item_id, "attempt_id": aid, "answered_at": iso(self.clock_ms())})
            state["current_id"] = None
            if explaining:
                self.begin_explaining(state)

            # Append evidence before selecting, in the same transaction; selection sees updated priors.
            def advance() -> None:
                extra()
                if state["state"] not in ("failed", "explaining"):
                    following = self._advance(state)
                    # Events cannot be nested into Learner.record_batch; append/fold under its existing transaction.
                    for event in following:
                        stored = self.learner.log.append(
                            event.type,
                            event.payload,
                            source=event.source,
                            session_id=event.session_id,
                            occurred_at=event.occurred_at,
                        )
                        self.learner.projector.apply(stored)
                reply = self._assessment_view(state)
                state["clients"][client_id] = {"fingerprint": fingerprint, "reply": reply}
                self.store.put(state, self.clock_ms())
                self.learner.conn.execute(
                    "INSERT INTO assessment_runs VALUES (?, ?, ?, ?, ?) ON CONFLICT(assessment_id)"
                    " DO UPDATE SET state=excluded.state, body=excluded.body, updated_at=excluded.updated_at",
                    (state["id"], state["kind"], run_state(state), dump(state), self.clock_ms()),
                )

            self.learner.record_batch(events, extra=lambda conn, stored: advance())
            return self._assessment_view(state)
        finally:
            self.busy.discard(state["id"])

    def probe_for(self, kc_id: str, *, first_lesson: bool = False) -> dict[str, Any] | None:
        rows = self.learner.conn.execute(
            "SELECT outcome,misconception_id,kc_ids FROM attempts ORDER BY occurred_at DESC"
        ).fetchall()
        unmatched = sum(
            outcome == "incorrect" and mc is None
            for outcome, mc, kcs in rows
            if kc_id in [k["kc_id"] for k in json.loads(kcs)]
        )
        if not first_lesson and unmatched < self.settings.probe_unmatched_errors:
            return None
        bank = [i for i in self.eligible("diagnostic", [kc_id]) if i["type"] == "mcq_diagnostic"]
        return bank[0] if bank else None

    def choose_probe(self, item_id: str, option_id: str) -> dict[str, Any] | None:
        item = self.content.record("items", item_id)
        if item is None or item["type"] != "mcq_diagnostic" or item_id in self._reserved():
            raise Refused("invalid_probe")
        if probe_problems([item], self.content.table("misconceptions"), self.settings):
            raise Refused("invalid_probe")
        options = item["parts"][0]["answer"]["options"]
        chosen = next((o for o in options if o["id"] == option_id), None)
        if chosen is None:
            raise Refused("invalid_option")
        if chosen["correct"]:
            return None
        mc = self.content.record("misconceptions", chosen["misconception_id"])
        assert mc is not None
        payload = evidence(item, mc["id"], self.content.table("misconceptions"), "probe")
        assert payload is not None
        self.learner.record_batch([self._event("misconception_evidence", payload)])
        text = mc["fix"]["text"]
        return {"misconception_id": mc["id"], "fix": text.get("en", "") if isinstance(text, dict) else str(text)}

    def create_practice(
        self, subject: str, kc_ids: Sequence[str], size: int, *, exclude_item_ids: Sequence[str] = ()
    ) -> dict[str, Any]:
        self.guard_tutor()
        # LRN-16, LRN-26: each next item nearest the target success, none on a parked KC; decisions kept for the trace.
        bank, self.policy_decisions = practice_selection(
            self.learner.conn, self.learner.projector.ctx.params,
            [i for i in self.eligible("practice", kc_ids, subject) if i["id"] not in exclude_item_ids], size,
            self.settings, self.clock_ms(), difficulty=self.content.item_difficulty,
        )  # fmt: skip
        if not bank:
            raise Refused("no_items")
        state = self._new("practice", subject, items=bank, index=0)
        self._commit(state, [self._exposure(bank[0], "practice", state["id"])])
        return {
            "set_id": "set_" + state["id"],
            "items_total": len(bank),
            "current_item": present(bank[0], self.content, self.settings),
        }

    async def practice_answer(
        self, identity: str, client_id: str, item_id: str, answer: Any, time_ms: int, hints_used: int = 0
    ) -> dict[str, Any]:
        self.guard_tutor()
        state = self._get(identity, "practice")
        fingerprint = hashlib.sha256(dump([item_id, answer]).encode()).hexdigest()
        if client_id in state["clients"]:
            cached = state["clients"][client_id]
            if cached["fingerprint"] != fingerprint:
                raise Refused("request_conflict")
            return dict(cached["reply"])
        if state["index"] >= len(state["items"]) or state["items"][state["index"]]["id"] != item_id:
            raise Refused("item_not_current")
        if state["id"] in self.busy:
            raise Refused("request_conflict")
        if hints_used:
            raise Refused("submit_helped_answer_in_tutor")
        self.busy.add(state["id"])
        try:
            item = state["items"][state["index"]]
            verdicts = await self._mark(item, answer)
            aid, events, extra = self._attempt_events(state, item, verdicts, answer, time_ms)
            before = self._band(item["kc_ids"][0])
            outcomes = {v.outcome for v in verdicts.values()}
            missed = not (outcomes == {"correct"} or "unverifiable" in outcomes)
            # Help before the answer (ASM-28, D120): a first miss keeps the question for a second try and reveals
            # nothing; the worked answer comes with the second try (or "Don't know", `practice_reveal`).
            misses = state.setdefault("misses", {})
            first_miss = missed and not misses.get(item["id"]) and item.get("type") != "mcq_diagnostic"
            if missed:
                misses[item["id"]] = int(misses.get(item["id"], 0)) + 1
            if not first_miss:
                state["index"] += 1
            unmatched = all(v.outcome == "incorrect" and v.misconception_id is None for v in verdicts.values())
            if unmatched and not first_miss:
                kc = item["kc_ids"][0]
                prior = sum(
                    outcome == "incorrect" and mc is None
                    for outcome, mc, kcs in self.learner.conn.execute(
                        "SELECT outcome,misconception_id,kc_ids FROM attempts"
                    )
                    if kc in [k["kc_id"] for k in json.loads(kcs)]
                )
                if prior + 1 >= self.settings.probe_unmatched_errors:
                    probe = self.probe_for(kc, first_lesson=True)
                    if probe and all(i["id"] != probe["id"] for i in state["items"]):
                        state["items"].insert(state["index"], probe)
            following = state["items"][state["index"]] if state["index"] < len(state["items"]) else None
            if following and not first_miss:
                events.append(self._exposure(following, "practice", state["id"]))
            outcome = (
                "correct"
                if outcomes == {"correct"}
                else "not_checked"
                if "unverifiable" in outcomes
                else "partially_correct"
                if outcomes & {"correct", "partial"}
                else "incorrect"
            )
            first = next((p for p in item["parts"] if verdicts[p["id"]].outcome != "correct"), None)
            reply: dict[str, Any] = {
                "attempt_id": "att_" + aid,
                "verdict": outcome,
                "marks_awarded": sum(v.marks_awarded for v in verdicts.values()),
                "marks_total": sum(v.marks_available for v in verdicts.values()),
                "feedback_key": "assessment." + outcome,
                "first_error": None,
                "mastery_change": {"kc_id": item["kc_ids"][0], "band_before": before, "band_after": before},
                "next_item": present(following, self.content, self.settings) if following else None,
                "reflection": {
                    "card_type": "reflection",
                    "did": "Your response was checked.",
                    "right": "All criteria met."
                    if outcome == "correct"
                    else "Not yet. Have another go."
                    if first_miss
                    else "Compare the criteria below.",
                    # A mark point can name the answer, so a first miss points at the help instead (ASM-28).
                    "next_goal": "Try again, or ask for a hint."
                    if first_miss
                    else (first.get("mark_points") or [{}])[0].get("criterion", first["prompt"])
                    if first
                    else "Try the next item.",
                    "marks": [
                        {
                            "criterion": p["prompt"],
                            "type": "independent",
                            "awarded": verdicts[p["id"]].outcome == "correct",
                        }
                        for p in item["parts"]
                    ],
                },
            }

            if missed and not first_miss:
                reply["worked"] = worked_answer(item, self.content)  # proposed addition (contract-additions-m45d.json)

            def save() -> None:
                extra()
                reply["mastery_change"]["band_after"] = self._band(item["kc_ids"][0])
                state["clients"][client_id] = {"fingerprint": fingerprint, "reply": reply}
                self.store.put(state, self.clock_ms())

            self.learner.record_batch(events, extra=lambda conn, stored: save())
            return reply
        finally:
            self.busy.discard(state["id"])

    def practice_reveal(self, identity: str, item_id: str) -> dict[str, Any]:
        """ "Don't know" or "Show me the answer" on the question shown (ASM-28, PED-09, D87): its worked answer with its
        check, and the set moves on to the next question. Nothing is marked (no attempt, no mastery change); a question
        given up with no try is seen, and the plan treats it like any other question left (LRN-16)."""
        self.guard_tutor()
        state = self._get(identity, "practice")
        if state["index"] >= len(state["items"]) or state["items"][state["index"]]["id"] != item_id:
            raise Refused("item_not_current")
        item = state["items"][state["index"]]
        state["index"] += 1
        state.setdefault("revealed", []).append(item_id)
        following = state["items"][state["index"]] if state["index"] < len(state["items"]) else None
        band = self._band(item["kc_ids"][0])
        self._commit(state, [self._exposure(following, "practice", state["id"])] if following else [])
        return {
            "item_id": item_id,
            "worked": worked_answer(item, self.content),
            "mastery_change": {"kc_id": item["kc_ids"][0], "band_before": band, "band_after": band},
            "next_item": present(following, self.content, self.settings) if following else None,
        }

    def _band(self, kc_id: str) -> str:
        row = self.learner.conn.execute("SELECT status FROM kc_mastery WHERE kc_id=?", (kc_id,)).fetchone()
        return BANDS.get(row[0], BANDS["not_started"]) if row else BANDS["not_started"]

    def item_verdicts(self, session_id: str, item_id: str) -> tuple[dict[str, dict[str, Any]], list[str], str | None]:
        """An item's attempts in a session, their verdicts' outcomes in order and the last verdict's misconception,
        each verdict as it reads now: a re-marked answer reads as its latest marking, once (ASM-23, CUR-36)."""
        attempts: dict[str, dict[str, Any]] = {}
        outcomes: list[str] = []
        misconception_id = None
        for event in read_log(e for e in self.learner.log.merged() if e.session_id == session_id):
            if event.type == "attempt" and event.payload.get("item_id") == item_id:
                attempts[event.payload["attempt_id"]] = event.payload
            elif event.type == "verdict" and event.payload["attempt_id"] in attempts:
                outcomes.append(event.payload["outcome"])
                misconception_id = event.payload.get("misconception_id")
        return attempts, outcomes, misconception_id

    def practice_help(self, identity: str, item_id: str, reason: str = "help") -> dict[str, Any]:
        """The tutor with the item loaded (UXS-24). `why_right` (UXS-62) is offered only once the item is marked
        correct, never during a mock sitting, and never changes the item's mark."""
        self.guard_tutor()
        state = self._get(identity, "practice")
        item = next((i for i in state["items"] if i["id"] == item_id), None)
        if item is None:
            raise Refused("not_found")
        # Resuming after tutor assistance must never look like a fresh independent journey check.
        if item_id not in state.setdefault("helped", []):
            state["helped"].append(item_id)
            self._commit(state)
        attempts, outcomes, misconception_id = self.item_verdicts(state["id"], item_id)
        if reason == "why_right" and (not outcomes or outcomes[-1] != "correct"):
            raise Refused("why_right_needs_a_correct_answer")
        submitted = None
        if attempts:
            latest = next(reversed(attempts.values()))
            row = self.learner.conn.execute(
                "SELECT student_text FROM turns WHERE turn_id=?", (latest.get("response_ref"),)
            ).fetchone()
            if row and row[0]:
                submitted = json.loads(row[0])
        mc = self.content.record("misconceptions", misconception_id) if misconception_id else None
        fix = (mc or {}).get("fix", {}).get("text", "")
        return {
            "item": item,
            "subject": state["subject"],
            "mode": "guided",
            "answer": submitted,
            "outcomes": outcomes,
            "misconception_id": misconception_id,
            "fix": fix.get("en", "") if isinstance(fix, dict) else str(fix),
            "origin": {
                "kind": "practice_item",
                "ref": state["id"] + ":" + item_id + ("#why_right" if reason == "why_right" else ""),
            },
            "reason": reason,
        }

    def tutor_context(self, origin: Mapping[str, Any]) -> dict[str, Any]:
        if origin["kind"] == "exam_review":
            return self.review(str(origin.get("ref") or ""))
        if origin["kind"] == "practice_item":
            if str(origin.get("ref") or "").startswith("probe:"):
                return self.probe_context(str(origin["ref"])[6:])
            reference = str(origin.get("ref") or "")
            reference, _, why = reference.partition("#")
            if ":" in reference:
                identity, item_id = reference.split(":", 1)
                return self.practice_help(identity, item_id, "why_right" if why == "why_right" else "help")
            item_id = str(origin.get("ref") or "")
            for state in self.store.all():
                if state["kind"] == "practice" and any(i["id"] == item_id for i in state["items"]):
                    return self.practice_help(state["id"], str(item_id))
        raise Refused("invalid_assessment_origin")

    def completed_tutor_question(self, text: str, subject: str) -> tuple[dict[str, Any], str] | None:
        """Reuse an answered mastery question's verified key after its check ends.

        Matching preserves numbers, symbols and case. A similar question, changed given, unfinished check,
        another subject or ambiguous match must never supply an answer key to the tutor.
        """
        try:
            self.guard_tutor()
        except Refused:
            return None
        for suffix in (
            " Please guide me through this one step at a time.",
            " Please guide me through it one step at a time.",
        ):
            text = text.removesuffix(suffix)
        wanted = " ".join(text.split())
        matches: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        for state in self.store.all():
            if (
                state["kind"] != "mastery_check"
                or state["subject"] != subject
                or state["state"] not in ("completed", "failed")
            ):
                continue
            answered = {a["item_id"] for a in state["answered"]}
            for item_id, item in state["selected"].items():
                current = self.content.record("items", item_id)
                if (
                    item_id not in answered
                    or current is None
                    or current.get("review", {}).get("status") != "published"
                    or current["stem"] != item["stem"]
                ):
                    continue
                current_parts = [(p["id"], p["prompt"], p["answer"]) for p in current["parts"]]
                pinned_parts = [(p["id"], p["prompt"], p["answer"]) for p in item["parts"]]
                if current_parts != pinned_parts:
                    continue
                item = pin_item(current, self.content)  # retain corrected working when the question and key agree
                for part in item["parts"]:
                    question = str(item["stem"]) + " " + str(part["prompt"])
                    forms = [question]
                    key = part["answer"]
                    if key["kind"] == "mcq":
                        forms.append(question + " " + " ".join(o["id"] + ". " + o["text"] for o in key["options"]))
                    elif key["kind"] == "ordering":
                        forms.append(question + " " + "; ".join(e["text"] for e in key["elements"]))
                    if wanted in (" ".join(form.split()) for form in forms):
                        matches[(item_id, part["id"])] = (copy.deepcopy(item), str(part["id"]))
        return next(iter(matches.values())) if len(matches) == 1 else None

    def schedule_ready_checks(self) -> int:
        scheduled = 0
        active = {
            (s["subject"], s["kind"])
            for s in self.store.all()
            if s["kind"] == "mastery_check" and s["state"] in ("in_progress", "waiting", "stopped", "explaining")
        }
        for (kc,) in self.learner.conn.execute(
            "SELECT kc_id FROM kc_mastery WHERE status='ready_to_check' ORDER BY kc_id"
        ):
            subject = SUBJECT_NAMES[kc.split(".")[1]]
            if (subject, "mastery_check") in active or kc not in self.content.kcs:
                continue
            state = self._new(
                "mastery_check",
                subject,
                kc_id=kc,
                observations=[],
                current_id=None,
                selected={},
                available_at=self.clock_ms(),
                retry_at=None,
                surfaces=[],
                anchors=[],
                active_ms=0,
                last_active=self.clock_ms(),
            )
            state["state"] = "waiting"
            state["scheduled"] = True
            self._commit(state)
            active.add((subject, "mastery_check"))
            scheduled += 1
        return scheduled

    def practice(self, identity: str) -> dict[str, Any]:
        self.guard_tutor()
        state = self._get(identity, "practice")
        if state.get("purpose") == "probe":
            raise Refused("not_found")
        item = state["items"][state["index"]] if state["index"] < len(state["items"]) else None
        if item is not None:
            self.learner.record_batch(
                [self._exposure(item, "review" if state.get("purpose") == "mock_review" else "practice", state["id"])]
            )
        return {
            "set_id": "set_" + state["id"],
            "items_total": len(state["items"]),
            "current_item": present(item, self.content, self.settings) if item else None,
        }

    def prepare_review_practice(self, subject: str, kc_ids: Sequence[str]) -> str | None:
        # Called inside the mock report transaction; no preview is exposed until GET opens the set.
        items = []
        used = set()
        for kc in kc_ids:
            choices = [i for i in self.eligible("review", [kc], subject) if i["id"] not in used]
            for item in choices[: self.settings.mock_review_items_per_kc]:
                items.append(item)
                used.add(item["id"])
        if not items:
            return None
        practice = self._new("practice", subject, items=items, index=0, purpose="mock_review")
        self.store.put(practice, self.clock_ms())
        return "set_" + str(practice["id"])
