"""Typed, code-rendered facts. Models select references; they cannot change a metric, value, period or subject."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NUMBER_WORDS = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen "
        "eighteen nineteen twenty".split(),
        range(21),
        strict=True,
    )
)
NUMBER_WORDS.update(thirty=30, forty=40, fifty=50, sixty=60, seventy=70, eighty=80, ninety=90)
_WORD_NUMBER = re.compile(r"\b(?:" + "|".join(NUMBER_WORDS) + r")\b", re.I)
METRIC = re.compile(
    r"\b(?:cards?|days?|streak|exam|questions?|answers?|correct|score|percent|quizzes|reviews?)\b",
    re.I,
)
PERSONAL_PROGRESS = re.compile(
    r"\b(?:you(?:'ve| have)?|your)\b[^.!?]{0,80}\b(?:mastered|mastery|improved|secure|stronger|weaker|struggle[ds]?)\b",
    re.I,
)
NEGATIVE_SCHEDULE = re.compile(
    r"\b(?:no|not any|aren't any|isn't any|do not have any|don't have any)\s+"
    r"(?:(?:study|practice|review|flash)\s*)?(?:plans?|cards?|reviews?)\b",
    re.I,
)


def normalise_numbers(text: str) -> str:
    return _WORD_NUMBER.sub(lambda m: str(NUMBER_WORDS[m[0].lower()]), text)


_LINE_HEAD = re.compile(r"^\[\d+\][^:]*?(?:\(at [^)]*\))?:\s*")
_CLOCK = re.compile(r"(?<![\w:.])(\d{1,2}:)?(\d{1,2}):(\d{2})(?![\w:])(?!\.\d)")
_FIGURE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.]*\d)")


def figures(lines: list[str]) -> frozenset[str]:
    """What the learner's own material says in these lines (`[ref] label (at m:ss): text` records) that a reply
    explaining from them may say back: their numbers, spelled-out ones too, and their moments ("4:30", no leading zero),
    never a ref. Where a number stands beside a unit of progress (cards, days, ...) it still needs a source (Ask about
    this moment, live-run fix 2026-11-01)."""
    found: set[str] = set()
    for line in lines:
        for hours, minutes, seconds in (m.groups() for m in _CLOCK.finditer(line)):
            found.add(f"{int(hours.rstrip(':'))}:{minutes}:{seconds}" if hours else f"{int(minutes)}:{seconds}")
        body = _CLOCK.sub(" ", _LINE_HEAD.sub("", line, count=1))
        found.update(m.group(1) for m in _FIGURE.finditer(normalise_numbers(body.lower())))
    return frozenset(found)


def model_claim_errors(text: str) -> list[str]:
    """Progress assertions must come from selected facts, not unconstrained generated prose."""
    normalized = normalise_numbers(text)
    if (
        PERSONAL_PROGRESS.search(text)
        or NEGATIVE_SCHEDULE.search(text)
        or (METRIC.search(text) and re.search(r"\d", normalized))
    ):
        return ["select fact_refs for progress or schedule claims; do not rewrite the fact in text"]
    return []


PROGRESS_SOURCE = "checked practice records"
BAND_WORDS = {"not_started": "not started", "learning": "learning", "secure": "secure"}


def band_words(band: object) -> str:
    """A practice band as the learner reads it elsewhere in the app, never its stored code (live-run fix 2026-11-01)."""
    return BAND_WORDS.get(str(band), str(band).replace("_", " "))


def counted(count: int, noun: str) -> str:
    """A count with its noun, so a fact never says "1 skills"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


STATEMENT_MAX = 100  # a skill statement inside a fact, clipped at a word boundary


def clip(text: str, limit: int = STATEMENT_MAX) -> str:
    """`text` within `limit` characters, cut at the last space before the limit with an ellipsis, so a fact never
    ends mid-word ("…changes its vertica:" on the 2 November rehearsal)."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    cut = head.rfind(" ")
    return (head[:cut] if cut > limit // 2 else head).rstrip() + "…"


@dataclass(frozen=True)
class Fact:
    key: str
    metric: str
    value: int | str
    unit: str
    subject: str | None
    period: str
    source: str
    statement: str

    def view(self, ref: int) -> dict[str, Any]:
        return {
            "ref": ref,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "subject": self.subject,
            "period": self.period,
            "source": self.source,
            "statement": self.statement,
        }


def from_progress(data: dict[str, Any], *, period: str) -> list[Fact]:
    facts = []
    due = data.get("due") or {}
    for key, label in (("learner_cards", "practice review"), ("study_cards", "Study")):
        value = due.get(key)
        if type(value) is int and value >= 0:
            facts.append(
                Fact(
                    key,
                    "due_cards",
                    value,
                    "cards",
                    label,
                    period,
                    "review schedule",
                    f"{label.capitalize()} cards due: {value}.",
                )
            )
    for key, label, source in (
        ("exam_distance_days", "Days until your exam", "saved exam date"),
        ("streak_days", "Learning streak in days", "recorded learning activity"),
    ):
        value = data.get(key)
        if type(value) is int and value >= 0:
            facts.append(Fact(key, key, value, "days", None, period, source, f"{label}: {value}."))
    for band in data.get("bands") or []:
        if not isinstance(band, dict) or band.get("band") not in ("not_started", "learning", "secure"):
            continue
        skill = clip(str(band.get("skill", "")))
        if not skill:
            continue
        facts.append(
            Fact(
                "band:" + str(band.get("ref")),
                "skill_band",
                band["band"],
                "band",
                skill,
                period,
                PROGRESS_SOURCE,
                f"Your practice on {skill}: {band_words(band['band'])}.",
            )
        )
    return facts


def add_facts(current: list[Fact], added: list[Fact]) -> None:
    for fact in added:
        old = next((i for i, item in enumerate(current) if item.key == fact.key), None)
        if old is not None:
            current[old] = fact
        elif len(current) < 64:
            current.append(fact)


def selected_text(facts: list[Fact], refs: list[int]) -> str:
    if any(type(ref) is not int or not 1 <= ref <= len(facts) for ref in refs):
        raise ValueError("fact reference was not supplied in this turn")
    return "\n".join(
        f"{facts[r - 1].statement} Source: {facts[r - 1].source} ({facts[r - 1].period})." for r in dict.fromkeys(refs)
    )
