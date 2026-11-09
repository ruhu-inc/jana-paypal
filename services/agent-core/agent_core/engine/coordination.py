"""The engine's coordination surface for M14.a to M14.c: roles and audiences (AGT-01), auxiliary roles (AGT-02),
delegation order (AGT-15), steering (AGT-17, API-25), side questions (AGT-18, API-26), the cards a tutor turn opens
(API-41) and what a stop does to them (TOL-14). A mixin of `Engine`, kept apart so the turn loop stays readable."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from agent_core.engine import live_flows, team, turn_scope
from agent_core.engine import side as side_questions
from agent_core.engine.auxiliary import Auxiliaries
from agent_core.engine.events import new_id
from agent_core.engine.input import normalise_input
from agent_core.engine.receipts import Receipts
from agent_core.engine.roster import AudienceGuard, audiences
from agent_core.engine.safety import redact_personal
from agent_core.engine.steering import Steer, SteeringQueue
from agent_core.engine.ui import HandlerError, confirm_card, hint_card, plan_card, question_card, reflection_card
from agent_core.prompts.builder import Message

if TYPE_CHECKING:
    from agent_core.engine.turn import TurnIds, TurnResult


class Coordination:
    """Mixed into `Engine`: every attribute and helper used here is the engine's own."""

    def _init_coordination(self: Any, deps: Any, state: Any, allowed_cards: tuple[str, ...]) -> None:
        """Only the Coach addresses the student (AGT-01), the auxiliary roles (AGT-02), what the Diagnoser said
        (AGT-15), steering (AGT-17), open cards and their receipts (TOL-14) and the engine's own copy."""
        self.receipt_texts = Receipts.load(deps.assets.root)
        self.auxiliary = Auxiliaries.load(deps.router, deps.settings, deps.assets.root, state.hardware_class)
        self.audience = AudienceGuard(audiences({**deps.assets.roles, **self.auxiliary.manifests}))
        self.events.part_guard = self.audience
        self.steering = SteeringQueue(deps.settings.steer_queue_max)
        self.diagnosis: team.Diagnosed | None = None
        self.titled = False
        self.title_pending: asyncio.Task[None] | None = None  # the title role naming the session (`team.new_problem`)
        self.open_cards: dict[str, str] = {}  # request id -> card kind, until answered or cancelled
        self.topic_choices: dict[str, list[str]] = {}  # a topic chooser's request id -> the topics it offered
        self.card_receipts: dict[str, str] = {}  # request id -> the receipt of a card a stop cancelled (TOL-14)
        self.allowed_cards: tuple[str, ...] = allowed_cards  # card kinds tutor-context allows this turn (PRM-15)

    def steer(
        self: Any, text: str, message_id: str | None = None, client_message_id: str | None = None
    ) -> Steer | None:
        """A message sent while a turn runs (AGT-17, API-25): queued for the next Prepare, or None when no turn is
        running (the client starts a new turn instead)."""
        if self._current is None or self._current.trace.status != "running":
            return None
        text = redact_personal(normalise_input(text))[0]  # nothing unredacted is queued, sent or kept (PRV-20)
        steer: Steer = self.steering.push(Steer(message_id or new_id("msg"), text, client_message_id))
        if client_message_id is not None:
            self.events.emit(
                {
                    "type": "message.accepted",
                    "client_message_id": client_message_id,
                    "message_id": steer.message_id,
                    "turn_id": self._current.turn_id,
                    "disposition": "steer_queued",
                }
            )
        return steer

    def _inject_steering(self: Any, turn: Any, private: list[Message]) -> None:
        """Queued steering goes in after the newest tool result as an app event around escaped student text, after
        the ordinary input guard; it joins the lesson's transcript as the student's own words (AGT-17). Only an
        in-scope message outside a sitting is injected; anything else is carried to start the next turn, where the
        guard answers it (PRV-13, H7)."""

        def wrapped(text: str) -> str:
            return str(self._sent_message({"role": "user", "content": text}).content)

        def admitted(text: str) -> bool:
            return not self.state.mock_running and bool(self.guard.admits(text, self.state.level))

        for steer, event in self.steering.take(wrapped, admitted, self.app_events):
            private.append(Message("user", event))
            self.state.transcript.append(
                {"role": "user", "content": steer.text, "kind": "steer", "turn_id": turn.turn_id}
            )
            self.events.emit({"type": "steer.applied", "message_id": steer.message_id, "phase": "prepare"})

    async def side_question(self: Any, text: str, ids: TurnIds | None = None) -> TurnResult:
        """A side question (AGT-18, API-26): a fork of the Coach with no tools, two calls at most and the same gates;
        nothing of the main lesson changes."""
        turn = self._begin("side_question", ids, fork=True)
        with turn_scope(turn.turn_id):
            try:
                # The full input guard, as for any message: every category, the mock lock, the truthful answer to
                # "are you a person" and the redaction of personal details (PRV-13, PRV-18, PRV-20, PED-23, H7).
                stopped, text = self._screen(turn, normalise_input(text), model=await self._guard_model(text))
                if stopped is None:
                    live_flows.reply(self, "side_question")  # the side fork returns to the step's rung (AGT-19)
                result = stopped if stopped is not None else await side_questions.answer(self, turn, text)
                return cast("TurnResult", result)
            except Exception:
                self._finish_crashed(turn)
                raise

    async def continue_problem(self: Any, chosen: str, ids: TurnIds | None = None) -> TurnResult:
        """After the topic chooser (AGT-27): the problem goes on to the Solver. The student's choice names the topic
        only when it is one the card offered; their words never enter tutor-context."""
        turn = self._begin("answer", ids)
        with turn_scope(turn.turn_id):
            try:
                # The student's free text is a message like any other: the full input guard first (PRV-13, H7).
                stopped, chosen = self._screen(turn, normalise_input(chosen), model=await self._guard_model(chosen))
                if stopped is not None:
                    return cast("TurnResult", stopped)
                self.state.transcript.append({"role": "user", "content": chosen, "kind": "chat"})
                live_flows.diagnosed(self, False)  # the student's topic stands in for the Diagnoser's (AGT-27)
                plan = await self._plan(turn)
                if self.cancelled.is_set():
                    return cast("TurnResult", self._finish_interrupted(turn))
                if plan is None:
                    blocked = self._finish_template(
                        turn, "tpl.honest_blocker", "completed", degraded_reason="plan_unverified"
                    )
                    return cast("TurnResult", blocked)
                self.state.plan, self.state.step, self.state.rung = plan, 1, 0
                opened = await self._coach_turn(turn, intent_note="opening: restate the goal and ask for an attempt")
                return cast("TurnResult", opened)
            except Exception:
                self._finish_crashed(turn)
                raise

    def card_answered(self: Any, request_id: str) -> list[str] | None:
        """A card was answered: it is no longer open. Returns the topics a topic chooser offered, if it was one."""
        self.open_cards.pop(request_id, None)
        topics: list[str] | None = self.topic_choices.pop(request_id, None)
        return topics

    def _cancel_cards(self: Any, turn: Any) -> None:
        """A stop cancels every open card (API-24): `card.cancel` with `interrupt`, and the shown-but-not-answered
        receipt (TOL-14). A cancelled card records no answer."""
        receipt = self.receipt_texts.text("ask_student", None, "running")
        for request_id in list(self.open_cards):
            self.events.emit({"type": "card.cancel", "request_id": request_id, "reason": "interrupt"})
            self.card_receipts[request_id] = receipt
            self.open_cards.pop(request_id, None)
            self.topic_choices.pop(request_id, None)

    def _card_body(self: Any, card: dict[str, Any]) -> dict[str, Any] | None:
        """The card a tutor turn asks for, through the engine's handlers (API-41): a hint only at the rung the engine
        allows, a question or a plan always, a reflection or a confirm only when tutor-context allows the kind."""
        kind = card.get("card_type")
        try:
            if kind == "hint":
                return hint_card(self.state.rung or 1, self.state.rung or 1, card["prompt"])
            if kind == "question":
                return question_card(card["prompt"], card["options"], card["allow_free_text"], card["multi"])
            if kind == "plan":
                return plan_card(card["prompt"], card["options"])
            if kind == "reflection" and kind in self.allowed_cards:
                return reflection_card(card["prompt"])
            if kind == "confirm" and kind in self.allowed_cards:
                return confirm_card(card["prompt"])
        except HandlerError:
            return None
        return None
