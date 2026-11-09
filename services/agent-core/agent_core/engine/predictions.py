"""Predict before you see, in a tutoring session (VSL-47): the learner's prediction reaches the tutor.

A predict-first simulation on screen (`params.predict`, a scenario of `assets/generated/sim_scenarios.py`) runs only
once the learner has locked in a prediction. The client then reports it (`POST /v1/sessions/{session_id}/visuals/
{visual_id}/prediction`, event `committed`), and later the learner's one-line explanation of what happened (event
`explained`). The engine, never the client and never a model, decides whether the prediction matched the result: the
scenario's choice that is `correct`, or the usual guess (`common_guess`, tied to a pack misconception, whose evidence
goes to the learner model). Each report is a short tutor turn:

- committed: the result the simulation shows is highlighted (`visual.focus` on the scenario's observed readout, e.g.
  `out.range`), and the Coach says in one sentence what it shows, pointing at that part (`<point at="vis_…:out.range"/>`
  when speaking), then asks the learner to explain why; it never explains first.
- explained: the Coach confirms an explanation that holds and names the idea (the misconception's pack fix), or asks
  one question that sends the learner back to the result on screen; it never gives the explanation.

The learner's words are untrusted input: they go through the same intake (scan and input guard) as any message, and
reach the Coach only as the transcript's escaped student text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agent_core.assets.generated.sim_scenarios import SIM_SCENARIOS
from agent_core.engine import turn_scope

if TYPE_CHECKING:
    from agent_core.engine.turn import TurnIds, TurnResult

FOCUS_TTL_MS = 8000
PREDICTED_NOTE = (
    "the student wrote a prediction before the simulation ran ({verdict}); it has now run and the result is on screen "
    "at {target}. In one short sentence say what the simulation shows, pointing at that part, without praise or "
    "blame; then ask the student to explain why in their own words. Do not explain it yourself"
)
EXPLAINED_NOTE = (
    "the student explained the simulation's result in their own words (their last message). If it holds, confirm it "
    "in one sentence and name the idea: {idea} If it does not, ask one short question that sends them back to the "
    "result on screen at {target}; never give the explanation"
)
VERDICTS = {
    "correct": "it matched the result",
    "common_guess": "it was the usual guess, which the result does not bear out",
    "other": "it did not match the result",
}


class PredictionRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def scenario_on_screen(engine: Any, visual_id: str, scenario_id: str) -> dict[str, Any]:
    """The scenario of a predict-first simulation on screen; refused when the visual is not shown or names another."""
    shown = next((v for v in engine.state.shown_visuals if v["visual_id"] == visual_id), None)
    if shown is None:
        raise PredictionRefused("not_found")
    scenario = SIM_SCENARIOS.get(scenario_id)
    if scenario is None or shown.get("predict") != scenario_id:
        raise PredictionRefused("not_predicting")
    return dict(scenario)


def verdict_of(scenario: dict[str, Any], choice: str | None) -> str:
    """`correct`, `common_guess` or `other`, from the scenario's own choices (free text alone is `other`)."""
    found = next((c for c in scenario.get("choices", []) if c.get("id") == choice), None)
    if found is None:
        return "other"
    if found.get("correct"):
        return "correct"
    return "common_guess" if found.get("common_guess") else "other"


def _idea(engine: Any, misconception_id: str) -> str:
    from agent_core.engine.live_flows import misconception_fix

    return misconception_fix(engine, misconception_id) or "the rule the simulation shows."


class Predictions:
    """Mixed into `Engine`: the learner's prediction and explanation for a predict-first simulation (VSL-47)."""

    async def sim_prediction(self: Any, visual_id: str, body: dict[str, Any], ids: TurnIds | None = None) -> TurnResult:
        scenario = scenario_on_screen(self, visual_id, str(body["scenario_id"]))
        if body["event"] == "explained" and visual_id not in self.sim_predictions:
            raise PredictionRefused("not_predicted")  # an explanation follows the prediction it explains
        target = f"{visual_id}:{scenario['observe']}"
        turn = self._begin("answer", ids)
        with turn_scope(turn.turn_id):
            try:
                text = str(body.get("text") or "").strip()
                if text:
                    stopped, text = self._intake(turn, text)
                    if stopped is not None:
                        return cast("TurnResult", stopped)
                    self.state.transcript.append({"role": "user", "content": text, "kind": "chat"})
                if body["event"] == "committed":
                    verdict = verdict_of(scenario, body.get("choice"))
                    self.sim_predictions[visual_id] = {"scenario_id": scenario["id"], "verdict": verdict}
                    if verdict == "common_guess" and scenario.get("misconception_id"):
                        self._prediction_evidence(turn, scenario)
                    self.events.emit({"type": "visual.focus", "visual_id": visual_id,
                                      "part_ids": [scenario["observe"]], "ttl_ms": FOCUS_TTL_MS})  # fmt: skip
                    note = PREDICTED_NOTE.format(verdict=VERDICTS[verdict], target=target)
                else:
                    note = EXPLAINED_NOTE.format(idea=_idea(self, str(scenario.get("misconception_id"))),
                                                 target=target)  # fmt: skip
                return cast("TurnResult", await self._coach_turn(turn, intent_note=note))
            except Exception:
                self._finish_crashed(turn)
                raise

    def _prediction_evidence(self: Any, turn: Any, scenario: dict[str, Any]) -> None:
        """The usual guess is evidence of the scenario's misconception for the learner model (once per simulation)."""
        mc_id = str(scenario["misconception_id"])
        kcs = [str(k) for k in scenario.get("kc_ids") or []]
        knowledge = self.knowledge
        record = knowledge.index.misconceptions.get(mc_id) if knowledge is not None else None
        if record is None or not kcs:
            return
        payload = {"misconception_id": mc_id, "kc_id": kcs[0], "evidence_source": "probe", "weight": 1.0}
        self._learn(turn, [("misconception_evidence", payload, "checker")])
