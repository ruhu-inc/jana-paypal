"""Auxiliary roles (AGT-02): rule matcher, input guard, vision reader, title, compaction and judge.

Each runs on its own router slot (MDL-06) as one bounded call: its manifest's system prompt, one task, a structured
output validated against its schema, within the manifest's wall-clock limit. None has a student audience, tools or
memory. Whatever goes wrong (no model, a timeout, invalid output, a crashed runtime) the role's fallback answers
instead and the turn goes on: an auxiliary call never blocks or fails a turn.

| Role | Fallback |
| --- | --- |
| Rule matcher | The keyword matcher over the candidate rules' own keywords |
| Input guard | The pattern classifier (`pedagogy.classify_input`); distress patterns always win, before any model |
| Vision reader | No regions: the student types the question; a low-confidence region is never left unmarked |
| Title | The topic's name from the curriculum map |
| Compaction | The deterministic sections from engine state; a failed section is the fixed sentence (MEM-17) |
| Judge | Every criterion `needs_teacher_review`; a mark is never awarded by default |

Each output is validated against the role's auxiliary contract (`agents/aux-*`)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.assets.agent import RoleManifest, load_manifest
from agent_core.config import Settings
from agent_core.contracts import document, validator
from agent_core.engine.pedagogy import classify_input
from agent_core.router import Message, Router

ROLES = ("rule_matcher", "input_guard", "vision", "title", "compaction", "judge")
SCHEMAS: dict[str, str] = {  # each role's structured output is its auxiliary contract (TABLE AGT-02a)
    "rule_matcher": "agents/aux-rule-match",
    "input_guard": "agents/aux-input-guard",
    "vision": "agents/aux-vision-regions",
    "title": "agents/aux-title",
    "compaction": "agents/aux-compaction",
    "judge": "agents/aux-judge",
}
NEEDS_REVIEW = "needs_teacher_review"


@dataclass
class AuxOutcome:
    """One auxiliary call: the validated output, or None with why the fallback answered."""

    role: str
    output: dict[str, Any] | None
    fallback_reason: str | None
    duration_ms: int
    served_by: str | None = None

    @property
    def fell_back(self) -> bool:
        return self.output is None


@dataclass
class Auxiliaries:
    """The auxiliary roles for one engine: their manifests and the router slots they run on."""

    router: Router
    settings: Settings
    manifests: dict[str, RoleManifest]
    hardware_class: str = "mid"
    # Every call's outcome, for the turn trace's delegations and for tests (role, fell back, reason).
    log: list[AuxOutcome] = field(default_factory=list)
    on_outcome: Callable[[AuxOutcome], None] | None = None

    @classmethod
    def load(cls, router: Router, settings: Settings, content_root: Path, hardware_class: str = "mid") -> Auxiliaries:
        folder = content_root / "roles" / "auxiliary"
        manifests = {p.stem: load_manifest(p) for p in sorted(folder.glob("*.md"))}
        return cls(router, settings, manifests, hardware_class)

    # --- the one bounded call ---------------------------------------------------------------------------------

    async def call(
        self, role: str, task: str, *, local_only: bool = False, require_scope: Callable[[], None] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> AuxOutcome:
        manifest = self.manifests[role]
        limits = manifest.meta["limits"]
        hardware = self.hardware_class if self.hardware_class in ("low", "mid", "high") else "cloud"
        schema = output_schema if output_schema is not None else document(SCHEMAS[role])
        checker = Draft202012Validator(schema) if output_schema is not None else validator(SCHEMAS[role])
        messages = [Message("system", manifest.body), Message("user", task)]
        started = time.monotonic()
        served: str | None = None
        reason: str | None = None
        output: dict[str, Any] | None = None

        def request_guard(_candidate: Any) -> None:
            if require_scope is not None:
                require_scope()

        try:
            result = await asyncio.wait_for(
                self.router.complete(role, messages, output=(role, schema, checker),
                                     allow=(lambda c: c.locality == "local") if local_only else None,
                                     request_guard=request_guard if require_scope is not None else None,
                                     max_tokens=int(limits["max_output_tokens"][hardware]),
                                     cloud_max_tokens=int(limits["max_output_tokens"]["cloud"])),
                timeout=float(limits["wall_clock_s"]),
            )  # fmt: skip
            served = result.turn.served_by
            if result.schema_valid and isinstance(result.structured, dict):
                output = result.structured
            else:
                reason = "schema_invalid"
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = "timeout"
        except Exception as error:  # no model, a crashed runtime, a bad reply: the fallback answers (AGT-02)
            reason = getattr(error, "reason", None) or type(error).__name__
        outcome = AuxOutcome(role, output, reason, int((time.monotonic() - started) * 1000), served)
        self.log.append(outcome)
        if self.on_outcome is not None:
            self.on_outcome(outcome)
        return outcome

    # --- the roles ------------------------------------------------------------------------------------------

    async def match_rules(self, situation: str, rules: Sequence[tuple[int, str, Sequence[str]]],
                          student_text: str = "") -> tuple[list[tuple[int, str]], AuxOutcome | None]:  # fmt: skip
        """Which candidate rules apply (one batched yes/no call; PED-15). `rules` are (ref, text, keywords). The
        fallback is the keyword matcher; with no candidates there is no call at all."""
        if not rules:
            return [], None
        listing = "\n".join(f"{ref}. {text}" for ref, text, _ in rules)
        task = (f"Situation: {situation}\n<student-message>\n{_escape(student_text)}\n</student-message>\n\n"
                f"Rules:\n{listing}")  # fmt: skip
        outcome = await self.call("rule_matcher", task)
        refs = {ref for ref, _, _ in rules}
        if outcome.output is not None:
            return [(int(a["rule_ref"]), str(a["reason"])) for a in outcome.output["applies"]
                    if int(a["rule_ref"]) in refs], outcome  # fmt: skip
        return keyword_matches(f"{situation} {student_text}", rules), outcome

    async def judge_rules(self, refs: Sequence[tuple[int, str]], student_text: str) -> list[dict[str, Any]]:
        """The rule matcher as the soft-rule pipeline's judge (PED-15): one batched call over the candidates' judged
        questions, answered for every ref (a ref the reply leaves out does not apply). A failed call raises, so the
        pipeline falls back to the rules' keywords: `TimeoutError` for a timeout, `RuntimeError` otherwise."""
        listing = "\n".join(f"{ref}. {question}" for ref, question in refs)
        task = f"<student-message>\n{_escape(student_text)}\n</student-message>\n\nRules:\n{listing}"
        outcome = await self.call("rule_matcher", task)
        if outcome.output is None:
            if outcome.fallback_reason == "timeout":
                raise TimeoutError("rule matcher timed out")
            raise RuntimeError(outcome.fallback_reason or "rule matcher failed")
        applies = {int(a["rule_ref"]): str(a["reason"]) for a in outcome.output["applies"]}
        return [{"rule_ref": ref, "applies": ref in applies, "reason": applies.get(ref, "")} for ref, _ in refs]

    async def guard_input(self, text: str) -> tuple[str, float, str]:
        """(class, confidence, source) for one student message. The patterns run first and always win for what they
        catch (distress above all, PED-18); the model classifies the rest; its failure falls back to the patterns'
        answer for the rest, which is `in_scope`."""
        caught = classify_input(text)
        if caught is not None:
            return caught, 1.0, "patterns"
        outcome = await self.call("input_guard", f"<student-message>\n{_escape(text)}\n</student-message>")
        if outcome.output is None:
            return "in_scope", 0.0, "patterns_fallback"
        return str(outcome.output["class"]), float(outcome.output["confidence"]), "model"

    async def read_regions(self, task: str, images: Callable[[], Awaitable[Any]] | None = None
                           ) -> tuple[list[dict[str, Any]], bool]:  # fmt: skip
        """The regions of a capture, every one below the confidence threshold marked `needs_confirmation`; and
        whether the student must type it instead (the fallback: nothing is guessed)."""
        outcome = await self.call("vision", task)
        if outcome.output is None:
            return [], True
        return mark_low_confidence(outcome.output["regions"], self.settings.vision_confirm_threshold), False

    async def title(self, question: str, topic_name: str) -> tuple[str, AuxOutcome]:
        """A session title in at most N words with no number in it; the topic's name otherwise."""
        task = f"Topic: {topic_name}\n<student-message>\n{_escape(question)}\n</student-message>"
        outcome = await self.call("title", task)
        if outcome.output is not None:
            title = clean_title(str(outcome.output["title"]), self.settings.title_max_words)
            if title:
                return title, outcome
            outcome.output, outcome.fallback_reason = None, "title_rejected"
        return topic_name, outcome

    def summariser(
        self, *, local_only: bool = False, require_scope: Callable[[], None] | None = None
    ) -> Callable[[str], Awaitable[str]]:
        """The compaction role as the Compactor's `summarise`: a failed call returns an empty text, which fails the
        Compactor's validation and ends in its fixed sentence (MEM-16, MEM-17)."""

        async def summarise(dialogue: str) -> str:
            outcome = await self.call(
                "compaction",
                f"<conversation>\n{_escape(dialogue)}\n</conversation>",
                local_only=local_only,
                require_scope=require_scope,
            )
            return str(outcome.output["agreements"]) if outcome.output is not None else ""

        return summarise

    async def judge(self, criteria: Sequence[tuple[str, str]], working: str, *,
                    explanation: bool = False) -> tuple[list[dict[str, Any]], bool]:
        """Rubric criteria judged with evidence; on failure every criterion is `needs_teacher_review` and no mark is
        awarded. Returns the criteria and whether the fallback answered."""
        listing = "\n".join(f"{cid}: {text}" for cid, text in criteria)
        context = (
            "Task: explain-back of a learning concept, not calculation method marks. "
            "Judge only the stated key points. A correct explanation in words or a stated formula can meet "
            "a key point; do not require a worked numerical example unless the criterion requests it. "
            "Do not invent additional requirements. Quote the learner's exact supporting words only; "
            "one contiguous excerpt or multiple balanced quotations joined by 'and', without commentary.\n"
        ) if explanation else ""
        task = f"{context}Criteria:\n{listing}\n<student-working>\n{_escape(working)}\n</student-working>"
        options: dict[str, Any] = {}
        if explanation:
            # Narrow the existing contract, for every provider: evidence can only be an exact source excerpt.
            # This prevents copying rubric text into a quotation; semantic judgment still belongs to the model.
            schema = deepcopy(document(SCHEMAS["judge"]))
            schema["properties"]["criteria"]["items"]["properties"]["evidence"]["enum"] = evidence_excerpts(working)
            options["output_schema"] = schema
        outcome = await self.call("judge", task, **options)
        wanted = [cid for cid, _ in criteria]
        # A judge's fabricated quotation is a model failure, not a learner mistake. Re-judge once; no
        # criterion passes without its own verified excerpt, and a second invalid response stays unavailable.
        for attempt in range(2 if explanation else 1):
            if outcome.output is None:
                break
            judged = {c["id"]: c for c in outcome.output["criteria"]}
            if set(wanted) <= set(judged):
                if explanation:
                    from agent_core.engine.assessment.explain import quoted

                    invalid = any(judged[cid]["pass"] and not quoted(str(judged[cid]["evidence"]), working)
                                  for cid in wanted)
                    if invalid:
                        if attempt == 0:
                            outcome = await self.call("judge", task + "\nYour previous evidence was not quoted from "
                                                      "the learner. Re-judge every key point from this same "
                                                      "student-working. Copy exact supporting excerpts from "
                                                      "student-working, never from Criteria. Do not insert words "
                                                      "or paraphrase. A point not demonstrated must be false.",
                                                      **options)
                            continue
                        break
                return [{"id": cid, "pass": bool(judged[cid]["pass"]), "evidence": str(judged[cid]["evidence"]),
                         "status": "judged"} for cid in wanted], False  # fmt: skip
            break
        return [{"id": cid, "pass": False, "evidence": "", "status": NEEDS_REVIEW} for cid in wanted], True


def evidence_excerpts(text: str) -> list[str]:
    """Bounded exact spans for the 400-character evidence field; no generated or rubric-derived words.

    Adjacent sentences support compound points. Overlapping word windows also cover long unpunctuated prose.
    Empty evidence is available for a failed criterion; passing it still fails the host's quote check.
    """
    excerpts = [""]
    boundaries = [0, *(m.end() for m in re.finditer(r"(?<=[.!?])\s+|\n+", text)), len(text)]
    for index, start in enumerate(boundaries[:-1]):
        for end in boundaries[index + 1:index + 4]:
            span = text[start:end].strip()
            if 3 <= len(span.split()) and len(span) <= 400 and span not in excerpts:
                excerpts.append(span)
            if len(excerpts) >= 33:
                break
        if len(excerpts) >= 33:
            break
    words = list(re.finditer(r"\S+", text))
    for start in range(0, len(words), 20):
        end = min(start + 40, len(words))
        while end > start and words[end - 1].end() - words[start].start() > 400:
            end -= 1
        if end - start >= 3:
            span = text[words[start].start():words[end - 1].end()]
            if span not in excerpts:
                excerpts.append(span)
        if len(excerpts) >= 65:
            break
    return excerpts


def keyword_matches(text: str, rules: Sequence[tuple[int, str, Sequence[str]]]) -> list[tuple[int, str]]:
    """The rule matcher's fallback: a rule applies when one of its own keywords appears as a word in the situation."""
    lowered = text.lower()
    found = []
    for ref, _, keywords in rules:
        hit = next((k for k in keywords if re.search(rf"\b{re.escape(k.lower())}\b", lowered)), None)
        if hit is not None:
            found.append((ref, f"keyword: {hit}"))
    return found


def mark_low_confidence(regions: Sequence[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """A region below the threshold, or with an unreadable `[?]`, is always marked `needs_confirmation`; the reader's
    own marking can add confirmations, never remove them."""
    marked = []
    for region in regions:
        low = float(region.get("confidence", 0)) < threshold or "[?]" in str(region.get("text", ""))
        marked.append({**region, "needs_confirmation": bool(region.get("needs_confirmation")) or low})
    return marked


def clean_title(title: str, max_words: int) -> str:
    """At most `max_words` words and no digits (a title never carries a value from the question or an answer)."""
    words = [w for w in re.sub(r"[\r\n\t]+", " ", title).strip().strip('"').split() if not re.search(r"\d", w)]
    return " ".join(words[:max_words])


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")
