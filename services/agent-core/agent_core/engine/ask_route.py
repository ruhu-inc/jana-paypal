"""Where the one Home input sends what the student typed (UXS-67, STU-20, D117).

Three ways out, chosen by the content and never by a model:

- `tutor`: a problem or a question in an installed subject. Homework is never simply answered: the guided tutor takes
  it (PED-01, D87). When in doubt between the tutor and Ask anything, the tutor wins.
- `study`: material (a link, or a pasted passage) opens the study area (STU-01).
- `ask`: any other question goes to the online feature `ask_anything` (STU-20).

The decision is lexical and local, so it works offline and costs nothing: the question's content words are looked up
in the installed packs' curriculum vocabulary (node titles and descriptions, knowledge-component statements, lesson,
worked-example and misconception passages). A question is in a subject when enough of its words are curriculum words
of that subject, or when it is problem-shaped (numbers with units, an equation, an imperative such as "solve") and at
least one of its words is. The packs the index holds are the installed ones, so an uninstalled subject never routes
to the tutor."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from agent_core.engine.tutoring_usage import asks_usage
from agent_core.retrieval.index import SUBJECT_NAMES

RouteKind = Literal["tutor", "study", "ask"]

_WORD = re.compile(r"[a-z]+")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.[a-z0-9-]+\.[a-z]{2,}\S*", re.I)
# Problem shape: an equation or formula, a number with a unit, or a working verb at the start of a clause.
_EQUATION = re.compile(r"[=^\u00d7\u00f7\u221a\u222b]|\d\s*[-+*/]\s*\d|\b\d+\s*[a-z]\b|->|→|\b[A-Z][a-z]?\d")
_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:m/s2?|m|cm|mm|km|kg|g|mg|s|ms|min|h|n|j|kj|w|kw|v|a|ohms?|ω|pa|kpa|hz|°c|k|mol|dm3|cm3|l|ml|"
    r"%|naira|₦|\$|£)(?![a-z])",
    re.I,
)
_WORKING = re.compile(
    r"(?:^|[.;:?!]\s*)(?:please\s+)?(?:solve|calculate|compute|evaluate|simplify|factori[sz]e|expand|differentiate|"
    r"integrate|balance|prepare|draw up|work out|find|determine|derive|prove|show that|convert)\b",
    re.I,
)
# Words too common to say anything about the subject.
_STOP = frozenset(
    """what which who whom whose when where why how is are was were be been being the a an of to in on at for by with
    from and or not no do does did done can could will would shall should may might must this that these those it its
    i me my we our you your he she they them their there here about into than then so if as up out over under tell give
    explain describe state define find show work use using used make many much more most some any all each every one
    two three very just also only between during before after whats please help know mean means meaning example
    thing things first last next way get got like want need good bad new old same other another such well really
    please learn learning understand question answer year years end time times day days start begin become history
    world people person part kind type types""".split()
)


def stem(word: str) -> str:
    """A crude stem shared by the question and the vocabulary: enough to match "accelerates" with "acceleration"."""
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    word = word.rstrip("e") if len(word) > 4 else word
    return word[:7]  # "accelerates" and "acceleration" share "acceler"


def content_terms(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 3 or word in _STOP:
            continue
        term = stem(word)
        if term not in out:
            out.append(term)
    return out[:40]


_VOCAB_PASSAGES = ("lesson", "worked_example", "misconception", "glossary")


@dataclass(frozen=True)
class Vocabulary:
    strong: frozenset[str]  # the curriculum itself: node titles and descriptions, knowledge-component statements
    weak: frozenset[str]  # the words of its lessons, worked examples and misconception fixes, less the strong ones


def vocabulary(index: Any) -> dict[str, Vocabulary]:
    """Per subject slug (`phy`), the stems of its curriculum words; built once per index and cached on it."""
    found: dict[str, Vocabulary] | None = getattr(index, "_ask_vocabulary", None)
    if found is not None:
        return found
    strong: dict[str, set[str]] = {}
    weak: dict[str, set[str]] = {}

    def add(into: dict[str, set[str]], slug: str, text: str) -> None:
        into.setdefault(slug, set()).update(stem(w) for w in _WORD.findall(text.lower()) if len(w) >= 3)

    for node in index.nodes.values():
        add(strong, node.id.split(".")[1], f"{node.title} {node.description}")
    for kc in index.kcs.values():
        add(strong, kc.id.split(".")[1], kc.statement)
    for passage in index.passages:
        if passage.kind in _VOCAB_PASSAGES:
            add(weak, passage.pack_id.split(".")[1], f"{passage.title} {passage.text}")
    stop = {stem(w) for w in _STOP}
    built = {
        slug: Vocabulary(frozenset(strong.get(slug, set()) - stop),
                         frozenset(weak.get(slug, set()) - strong.get(slug, set()) - stop))
        for slug in {*strong, *weak}
    }  # fmt: skip
    object.__setattr__(index, "_ask_vocabulary", built)  # rebuilt with the index when the packs change
    return built


def problem_shaped(text: str) -> bool:
    return bool(_EQUATION.search(text) or _UNIT.search(text) or _WORKING.search(text))


def material_shaped(text: str, min_chars: int) -> bool:
    """A pasted passage, not a question: long, and more statement than question."""
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", stripped) if s]
    questions = stripped.count("?")
    return questions == 0 or (len(sentences) >= 5 and questions * 4 <= len(sentences))


@dataclass(frozen=True)
class Route:
    route: RouteKind
    reason: str  # installed_subject | problem_in_subject | material | link | general | empty
    subject: str | None = None  # the subject's name (`physics`), for `tutor`

    def view(self) -> dict[str, Any]:
        return {"route": self.route, "reason": self.reason, **({"subject": self.subject} if self.subject else {})}


def route_input(text: str, index: Any | None, *, material_min_chars: int = 400) -> Route:
    """The way out for the box's words. `index` is the installed packs' `KnowledgeIndex` (None: nothing installed)."""
    words = text.strip()
    if asks_usage(words):
        return Route("tutor", "tutoring_usage")
    if not words:
        return Route("ask", "empty")
    if _URL.search(words) and len(_URL.sub("", words).strip()) < 200:
        return Route("study", "link")
    if material_shaped(words, material_min_chars):
        return Route("study", "material")
    if index is None:
        return Route("ask", "general")
    terms = content_terms(words)
    vocab = vocabulary(index)
    if not terms or not vocab:
        return Route("ask", "general")
    wanted = set(terms)
    strong = {slug: len(wanted & v.strong) for slug, v in vocab.items()}
    score = {slug: strong[slug] + 0.5 * len(wanted & v.weak) for slug, v in vocab.items()}
    shaped = problem_shaped(words)
    # In a subject: more than half the question is that subject's words, one of them from the curriculum itself; or
    # a problem-shaped question that uses one curriculum word. Ties lean to the tutor (homework is never answered).
    candidates = [slug for slug in sorted(vocab) if strong[slug] >= 1 and (score[slug] * 2 > len(terms) or shaped)]
    if not candidates:
        return Route("ask", "general")
    top = max(score[slug] for slug in candidates)
    subject = _subject(index, words, [slug for slug in candidates if score[slug] == top])
    return Route("tutor", "problem_in_subject" if shaped else "installed_subject", subject)


def _subject(index: Any, text: str, tied: list[str]) -> str:
    """The question's subject: the one whose words it uses most; between equals, the pack search's best topic."""
    if len(tied) > 1:
        try:
            ranked = index.topic_scores(text, subjects=set(tied), limit=1)
        except Exception:  # the first of the tied subjects stands when the search cannot run
            ranked = []
        if ranked:
            tied = [ranked[0][0].split(".")[1]]
    return SUBJECT_NAMES.get(tied[0], tied[0])
