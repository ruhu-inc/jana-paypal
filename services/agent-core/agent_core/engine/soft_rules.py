"""The soft rule matching pipeline of each Coach turn (PED-15), with the signals of TABLE PED-14a.

Per turn: (1) filter by scope and status; (2) evaluate code predicates; (3) send the judged candidates, at most the
configured number, to one batched yes/no call (the `rule_matcher` auxiliary role on the local fast tier, AGT-02);
(4) relationships and PED-12; (5) frequency limits; (6) inject at most the configured number of rules within the token
budget into the per-turn tutor-context block (PRM-15). On the low hardware class, with no judge bound, or on a timeout
or a reply that does not parse, judged conditions fall back to the rules' keywords, and high-criticality rules that
cannot be judged default to active. The cloud tier is never needed; matched ids and reasons go to the turn trace."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_core.assets import rules as rulebook
from agent_core.assets.rules import Frequency, Matched, Rule, RuleBook, Situation
from agent_core.config import Settings
from agent_core.engine.pedagogy import load_soft_rule
from agent_core.engine.state import SessionState

# The judged call: the candidates as (integer ref, question) and the student's message; returns
# `[{rule_ref | id, applies, reason}]`. It may raise or time out; matching then falls back to keywords.
Judge = Callable[[list[tuple[int, str]], str], Awaitable[Sequence[Mapping[str, Any]]]]


def _hard_rule_check(raw: Mapping[str, Any]) -> None:
    load_soft_rule(dict(raw))  # raises when the action or a relationship contradicts H1 to H10 (PED-10)


def load(folder: Path) -> RuleBook:
    """The shipped rules, with the hard-rule check of PED-10: no soft rule can contradict H1 to H10."""
    return rulebook.load(folder, check=_hard_rule_check)


def signals(state: SessionState, settings: Settings, *, now_ms: int,
            extra: Mapping[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """The engine-computed signals of TABLE PED-14a from engine state. Signals another component owns (the learner
    model, the input classifier, the Checker's answer flags) come in through `extra`; a signal nobody computed is
    absent, and every comparison on it is false."""
    step_board = [s for s in state.board.steps if s.owner == "student"]
    last = step_board[-1].status if step_board else None
    verdict = {"correct": "correct", "incorrect": "incorrect", "unchecked": "unverifiable"}.get(last or "", "none")
    attempts = state.attempts.get(state.step, 0)
    gap_ms = settings.hint_min_gap_s * 1000
    recent = 0 if state.hint_at_ms is None else int(now_ms - state.hint_at_ms < gap_ms) + int(state.gaming)
    out: dict[str, Any] = {
        "mode": state.mode,
        "subject": state.subject,
        "level": state.level,
        "step.verdict": verdict,
        "step.hint_rung": state.rung,
        "step.wrong_attempts": attempts if verdict == "incorrect" else max(0, attempts - 1),
        "hints.requests_last_10s": recent,
        "engine.bottom_out_shown": state.step in state.bottom_outs,
        "student.bulk_questions": bool(state.question_queue),
    }
    out.update(extra or {})
    return out


@dataclass
class _Ahead:
    """A judged call started before the Coach turn needs it (`RuleMatcher.prefetch`)."""

    text: str
    rule_ids: frozenset[str]
    task: asyncio.Task[tuple[dict[str, tuple[bool, str]] | None, str | None]]


@dataclass
class RuleMatcher:
    book: RuleBook
    settings: Settings
    judge: Judge | None = None
    frequency: Frequency = field(default_factory=Frequency)
    calls: int = 0  # judged calls made, over the matcher's life
    ahead: _Ahead | None = None

    def prefetch(self, situation: Situation, hardware_class: str = "mid") -> None:
        """Start the judged call for this situation's candidates now, while the turn's specialists work, so the Coach
        turn's `match` finds the answers ready (SPEC §12 latency). The call is the one `match` would make: the same
        candidate questions and student message; `match` uses it only when the student message is the same and every
        rule it would send was judged, and otherwise judges afresh."""
        self.cancel_prefetch()
        _, judged, _ = rulebook.candidates(self.book, situation)
        sent = judged[: self.settings.rules_judged_max]
        if not sent or hardware_class == "low" or self.judge is None:
            return
        task = asyncio.ensure_future(self._judge(sent, situation.text, hardware_class))
        self.ahead = _Ahead(situation.text, frozenset(r.id for r in sent), task)

    def cancel_prefetch(self) -> None:
        if self.ahead is not None:
            self.ahead.task.cancel()
            self.ahead = None

    async def _answers(self, sent: list[Rule], text: str,
                       hardware_class: str) -> tuple[dict[str, tuple[bool, str]] | None, str | None]:  # fmt: skip
        ahead, self.ahead = self.ahead, None
        if ahead is not None and sent and ahead.text == text and {r.id for r in sent} <= ahead.rule_ids:
            answers, fallback = await ahead.task
            wanted = {r.id for r in sent}
            return ({k: v for k, v in answers.items() if k in wanted} if answers is not None else None), fallback
        if ahead is not None:
            ahead.task.cancel()
        return await self._judge(sent, text, hardware_class)

    async def match(self, situation: Situation, hardware_class: str = "mid") -> Matched:
        settings = self.settings
        trace: list[dict[str, Any]] = []
        active, judged, allowed = rulebook.candidates(self.book, situation)
        trace += [{"rule": r, "reason": "code predicate"} for r in sorted(active)]
        sent, overflow = judged[: settings.rules_judged_max], judged[settings.rules_judged_max :]
        answers, fallback = await self._answers(sent, situation.text, hardware_class)
        for rule in [*sent, *overflow]:
            if answers is not None and rule.id in answers:
                applies, reason = answers[rule.id]
            elif rulebook.keyword_match(rule, situation.text):
                applies, reason = True, "keyword fallback"
            else:
                applies, reason = rule.high, ("high criticality default" if rule.high else "not matched")
            if applies:
                active.add(rule.id)
                trace.append({"rule": rule.id, "reason": reason[:120]})
        limits = (settings.rules_injected_max, settings.rules_injected_tokens)
        kept, injected = rulebook.finish(self.book, situation, active, allowed, self.frequency, *limits, trace)
        return Matched(kept, injected, trace, 1 if sent and fallback is None else 0, fallback)

    async def _judge(self, sent: list[Rule], text: str,
                     hardware_class: str) -> tuple[dict[str, tuple[bool, str]] | None, str | None]:  # fmt: skip
        if not sent:
            return {}, None
        if hardware_class == "low":
            return None, "low_class"
        if self.judge is None:
            return None, "no_judge"
        refs = [(n, r.question or "") for n, r in enumerate(sent, 1)]
        self.calls += 1
        try:
            reply = await asyncio.wait_for(self.judge(refs, text), self.settings.rules_matcher_timeout_s)
        except TimeoutError:
            return None, "timeout"
        except Exception:
            return None, "error"
        try:
            out: dict[str, tuple[bool, str]] = {}
            for entry in reply:
                ref = entry.get("rule_ref", entry.get("id"))
                rule = sent[int(ref) - 1] if isinstance(ref, int) and 1 <= ref <= len(sent) else None
                if rule is None and isinstance(ref, str):
                    rule = next((r for r in sent if r.id == ref), None)
                if rule is None or not isinstance(entry.get("applies"), bool):
                    raise ValueError("unparsed")
                words = " ".join(str(entry.get("reason", "")).split()[:15])
                out[rule.id] = (entry["applies"], f"judged: {words}" if words else "judged")
            return out, None
        except (ValueError, TypeError, AttributeError):
            return None, "parse_failure"


def tutor_context_lines(matched: Matched) -> list[str]:
    """The rules' part of the per-turn tutor-context block (PRM-15): actions only, never ids or reasons."""
    return ["teaching rules this turn:", rulebook.block(matched.injected)] if matched.injected else []
