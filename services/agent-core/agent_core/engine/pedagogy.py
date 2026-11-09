"""Pedagogy as engine code: the hint ladder, attempt before hint, the input guard and the output critic (PED-03,
PED-05, PED-10 H3 to H10, PED-18 and PED-20 S1 forms).

The rung is engine state, never a model choice. Hard rules are enforced here and in the stop gates; no prompt and no
soft rule can switch them off (the rule loader rejects soft rules that contradict them)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from agent_core.engine.state import SessionState
from agent_core.engine.verbaliser import verbalise

HARD_RULES = ("H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8", "H9", "H10")
RUNG_INTENT = {
    1: "orient: restate the goal, name the topic, point to the given that matters first and ask about it; "
    "no formula, no new numbers",
    2: "concept: name the rule or formula that links the givens to the unknown, without numbers",
    3: "first step: write the current step's setup with symbols and ask the student to put in the values",
    4: "worked step: show the current step, verified; ask for the next",
    5: "full step: show the current step in full; counts as not mastered",
}
# Small talk on its own, never maths: a greeting or thanks, optionally with the tutor's name or one more word ("hi
# Jana", "good morning", "thanks a lot"). Answered warmly by a template, so it never opens a problem or lands on the
# board as working (the Coach's greeting move, PRM roles; D131).
_GREETING = re.compile(
    r"(hi+|hello+|hey+|hiya|howdy|yo|greetings|good (morning|afternoon|evening|day)|what'?s up|sup)"
    r"( there| again| everyone| [a-z]+)?( how are you| how're you| how is it going)?",
)
_THANKS = re.compile(r"(ok(ay)? )?(thanks|thank you|thank u|thx|ty|cheers)( (so much|a lot|very much|[a-z]+))?")


_EXPRESSION = re.compile("[\\d\\s.+\\-*/^\u00d7\u00f7=()xyzabn,\u00b2\u00b3\u221a\u03c0%]+")
_OPERATION = re.compile("[\\d)a-z]\\s*[+\\-*/^\u00d7\u00f7=]\\s*[\\d(a-z\u221a]")


def as_task(text: str) -> str:
    """A bare expression typed as a question reads as the task a tutor hears in it (D131): "1+1" is "Work out 1+1",
    "2x+3=7" is "Solve 2x+3=7"; anything with words is left as the student wrote it."""
    plain = text.strip()
    low = plain.lower()
    if not re.search(r"\d", plain) or not _OPERATION.search(low) or not _EXPRESSION.fullmatch(low):
        return text
    return f"Solve {plain}" if "=" in plain and re.search(r"[a-z]", plain.lower()) else f"Work out {plain}"


def social_kind(text: str) -> str | None:
    """`greeting` or `thanks` when the whole message is small talk, else None (anything with a digit, a symbol of
    working or more than a few words is never small talk)."""
    plain = re.sub(r"[!.,?\s]+", " ", text.lower()).strip()
    if not plain or len(plain.split()) > 6 or re.search(r"[0-9=+*/^()\[\]]", plain):
        return None
    if _THANKS.fullmatch(plain):
        return "thanks"
    if _GREETING.fullmatch(plain):
        return "greeting"
    return None


STUCK = re.compile(r"\b(i'?m stuck|stuck|hint|help me|i don'?t know|no idea|give me a clue)\b", re.I)
# Asking for the answer or the whole solution (PED-09), anywhere in the message ("<question> just give me the answer").
# "Just tell me what to do first" asks for help, not the solution.
# A request for the full solution (PED-09, D87): "show me the answer", "give me the full worked solution", "tell me
# the whole working". Up to two describing words before the noun, so a natural phrasing is never missed.
SOLUTION_REQUEST = re.compile(
    r"\b(?:show|tell|give)(?: me)? (?:the |your )?"
    r"(?:(?:whole|full|final|worked|complete|entire|step[- ]by[- ]step|worked[- ]out) ){0,2}"
    r"(?:answer|solution|working)s?\b",
    re.I,
)
SOLUTION_NEGATION = re.compile(
    r"\b(?:not|never|don['\u2019]?t|avoid|without)\s+(?:(?:please|just|ever|actually|simply)\s+)*$|"
    r"\b(?:don['\u2019]?t|do not)\s+(?:want|need)\s+(?:you\s+)?to\s+$|"
    r"\bnot\s+asking\s+(?:you\s+)?to\s+$",
    re.I,
)
SOLUTION_COORDINATION = re.compile(r"^[^.!?;\n]*\b(?:or|and)\s+(?:(?:please|just)\s+)*$", re.I)

# H3 input guard, deterministic patterns: the first moderation layer's pattern half (PRV-18; the rest of the
# guard, the classifier and the routing table are in `safety`). Distress is decided by patterns alone.
INPUT_PATTERNS: dict[str, re.Pattern[str]] = {
    "distress": re.compile(
        r"\b((kill|hurt|cut|harm)(t?ing)? myself|want to die|suicid\w*|self[- ]?harm\w*|end (my life|it all)|"
        r"(don'?t|do not) want to (live|be alive)|better off dead|"
        r"(he|she|they|someone|somebody|my \w+) (hurts|hurt|hits|hit|beats|touches) me|"
        r"(afraid|scared) to go home|not safe at home|unsafe at home|(don'?t|do not|never) feel safe at home|"
        r"abus(e|ed|ing) me|bull(y|ies|ied|ying) me)\b",
        re.I,
    ),
    "personal_data": re.compile(
        # A phone number starts with 0 or a country code (a list of numbers or years in a question is not one).
        r"((?<![\w.])(?:\+\d{1,3}[ -]?\d{2,4}|0\d{1,3})[ -]?\d{3,4}[ -]?\d{3,4}(?![\w.])|\bmy (home )?address is\b|"
        r"[\w.+-]+@[\w-]+\.\w+)",
        re.I,
    ),
    "harmful": re.compile(
        r"\b(make|build) (an? )?(bomb|explosive|poison|gun|weapon)\b|\bhow to (make|cook) (drugs|meth)\b", re.I
    ),
    "sexual_or_violent": re.compile(
        r"\b(f+u+c+k+\w*|shit+y?|bitch\w*|bastard|wank\w*|porn\w*|send (me )?nudes|nudes|sexy|sexting|"
        r"have sex with|i('ll| will) (kill|stab|shoot|beat up) (you|him|her|them))\b",
        re.I,
    ),
    "medical_legal_advice": re.compile(r"\b(how many (pills|tablets)|what dose|legal advice|should i sue)\b", re.I),
    "cheating_request": re.compile(
        r"\b(do my (homework|assignment|test) for me|answers to my (exam|test)|answers? so i can copy|answers? to copy|"
        r"switch to answer mode|output (the )?answers for)\b|"
        r"\b(write|generate|give|complete)\b[^.!?\n]{0,100}"
        r"\b(for me to|so (that )?i can) (submit|hand in)\b[^.!?\n]{0,30}\bas (my own|mine)\b",
        re.I,
    ),
    # Non-goal 1 (PRD-20): open web browsing and chat with no study purpose. The S2 scope classifier replaces this.
    "off_topic": re.compile(
        r"\b(search the (web|internet)|browse the (web|internet)|look (it )?up online|be my general assistant|"
        r"write me an? (story|poem|song|joke)|chat with me about (the )?(match|football|music|films?|movies?))\b",
        re.I,
    ),
}
TEMPLATE_FOR = {
    "distress": "tpl.distress",
    "personal_data": "tpl.personal_details",
    "harmful": "tpl.dangerous_request",
    "medical_legal_advice": "tpl.see_trusted_adult",
    "cheating_request": "tpl.graded_work",
    "off_topic": "tpl.off_topic",
}

# H8 to H10 output critic.
PERSONAL_REQUEST = re.compile(
    r"[^.?!]*\b(your|ur) (home )?(address|phone( number)?|school'?s? name|photo|picture of you|"
    r"selfie)\b[^.?!]*[.?!]?",
    re.I,
)
# The team behind the Coach is never named to the student (PED-14): a sentence that credits a tool or a team role with
# a suggestion, or doubts the explanation when no checked result stands beside it, is taken out of the reply.
INTERNALS = re.compile(
    r"\b(?:the|a|my|our)\s+(?:calculation\s+|maths?\s+)?tools?(?:'s)?\s+(?:suggest|say|show|found|find|gave|give|"
    r"return|calculat|comput|check|think|indicat|answer|result|output)\w*|\baccording\s+to\s+the\s+tools?\b|"
    r"\bthe\s+(?:solver|checker|diagnoser)\b",
    re.I,
)
SELF_DOUBT = re.compile(
    r"\b(?:it|this|that|which|the\s+formula|the\s+method|the\s+plan|my\s+explanation|the\s+explanation)\s+"
    r"(?:may|might|could)\s+(?:contain|have|include)\s+(?:an?\s+)?(?:error|mistake)s?\b",
    re.I,
)
BANNED: dict[str, str] = {
    "that's incorrect": "Not quite yet",
    "great question!": "",
    "you're so smart": "",
    "genius": "",
    "obviously": "",
    "simply": "",
    "just do": "do",
    "easy": "",
    "wrong": "not quite",
}


# Variables filled only from verified entries of the safety-resources pack (PRV-14): a sentence holding one with no
# verified value is left out of the template, never filled with a guess.
VERIFIED_ONLY = ("emergency_number", "child_helpline")
_SENTENCE = re.compile(r"[^.?!]*[.?!]+\s*|[^.?!]+$")


@dataclass(frozen=True)
class Templates:
    """The versioned canned turns of one language (PRV-19), with the verified safety values they may use."""

    texts: dict[str, str]
    hints: dict[int, str]
    version: str = ""
    status: str = "draft"
    verified: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Templates:
        data = yaml.safe_load(path.read_text())
        return cls(dict(data["templates"]), {int(k): v for k, v in data["hints"].items()},
                   str(data.get("version", "")), str(data.get("status", "draft")))  # fmt: skip

    def with_verified(self, values: dict[str, str]) -> Templates:
        """The same templates with the safety-resources pack's verified values (PRV-14)."""
        return replace(self, verified={k: v for k, v in values.items() if k in VERIFIED_ONLY and v})

    def render(self, template_id: str, **values: Any) -> str:
        text = self.texts[template_id]
        filled = {**{k: v for k, v in values.items() if k not in VERIFIED_ONLY}, **self.verified}
        missing = [name for name in VERIFIED_ONLY if name not in filled]
        if any("{" + name + "}" in text for name in missing):
            text = "".join(s for s in _SENTENCE.findall(text) if not any("{" + n + "}" in s for n in missing))
        return text.strip().format_map(_Defaults(filled))

    def spoken(self, template_id: str, **values: Any) -> str:
        """The template's keyed spoken form (PED-26, VOC-20): the same text read by the verbaliser, never a model, with
        a verified phone number read digit by digit as a caller dials it ("nine nine nine", not "nine hundred ...")."""
        dialled = {k: re.sub(r"\d+", lambda m: " ".join(m[0]), v) for k, v in self.verified.items()}
        return verbalise(replace(self, verified=dialled).render(template_id, **values)).text

    def spoken_hint(self, rung: int) -> str:
        """The canned hint of a rung (the AGT-08 exhaustion output) in its spoken form (PED-26)."""
        return verbalise(self.hints[rung]).text


class _Defaults(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return {
            "current_topic": "this problem",
            "level": "your level",
            "nearest_topic": "the closest topic in your course",
            "safe_topic": "the reactions on your syllabus",
            "n": "1",
        }.get(key, "")


def classify_input(text: str, skip: tuple[str, ...] = ()) -> str | None:
    """H3: a PRV-13a category that routes to a template before any model runs, or None for in-scope input. Personal
    data is decided last, so a phone number or an email address never hides a safety category (PRV-13)."""
    for category, pattern in INPUT_PATTERNS.items():
        if category != "personal_data" and category not in skip and pattern.search(text):
            return category
    if "personal_data" not in skip and INPUT_PATTERNS["personal_data"].search(text):
        return "personal_data"
    return None


def wants_hint(text: str) -> bool:
    return bool(STUCK.search(text))


def wants_solution(text: str) -> bool:
    declined_end: int | None = None
    for match in SOLUTION_REQUEST.finditer(text):
        declined = SOLUTION_NEGATION.search(text[: match.start()]) or (
            declined_end is not None and SOLUTION_COORDINATION.search(text[declined_end : match.start()])
        )
        if not declined:
            return True
        declined_end = match.end()
    return False


# "Why do we take away three?", "How does that line follow?": a question about the working, never an answer to it.
METHOD_QUESTION = re.compile(r"^\s*(?:but\s+|so\s+|and\s+)?(?:why|how\s+(?:come|does|did))\b[^=]*\?\s*$", re.I)
EXPLAIN_REQUEST = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)?"
    r"explain\s+(?:why|how)\b[^=]+$", re.I,
)

EXPLAIN_STEP_REQUEST = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+)?"
    r"explain\s+(?:this|that|the)\s+(?:step|line|method)(?:\s+again)?"
    r"(?:,?\s+(?:in\s+)?a different way)?[?.]?\s*$", re.I,
)


def asks_why(text: str) -> bool:
    """A why or how question about the working (UXS-15, UXS-64): asked like "Why?" on a line, it is explained, never
    marked as an attempt or written on the board. A line with an equals sign is working, so it is never one."""
    return len(text) <= 300 and bool(METHOD_QUESTION.match(text) or EXPLAIN_REQUEST.fullmatch(text)
                                     or EXPLAIN_STEP_REQUEST.fullmatch(text))


def asks_method(text: str) -> bool:
    """A request for the next method, possibly preceded by prose givens, is chat rather than working.

    Proposed equations remain attempts, even when followed by a question about their correctness.
    """
    return (
        len(text) <= 600
        and "=" not in text
        and bool(
            re.search(
                r"(?:^|[.!?]\s+)(?:(?:which|what)\s+(?:equation|formula|method)\b[^?]*|"
                r"how\s+(?:do|can|should)\s+I\s+(?:start|proceed|continue|choose)\b[^?]*)\?\s*$",
                text,
                re.I,
            )
        )
    )


def policy_template(state: SessionState, text: str) -> str | None:
    """H4 and H7: the answer policy and the mock lock, checked by the engine, not the prompt."""
    if state.mock_running:
        return "tpl.mock_running"
    return None


def next_rung(state: SessionState, attempted: bool) -> tuple[int | None, str]:
    """PED-03, PED-05: the rung for a hint request now. Returns (rung or None, reason). None means the one entry
    question of PED-05 is asked instead (the reply then counts as the attempt)."""
    if not attempted and state.step not in state.deflected:
        state.deflected.add(state.step)
        return None, "entry_question"
    rung = min(max(state.rung, 0) + 1, 5)  # rises by one per request; the start rung is 1 in S1 (LRN-18)
    return rung, "hint"


# --- output critic (H8, H9, H10) -------------------------------------------------------------------------------------


def question_count(text: str) -> int:
    return text.count("?")


def truncate_at_first_question(text: str) -> str:
    at = text.find("?")
    return text if at < 0 else text[: at + 1]


def remove_personal_requests(text: str) -> tuple[str, bool]:
    cleaned, n = PERSONAL_REQUEST.subn("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(), n > 0


def remove_internals(text: str, doubt_allowed: bool = False) -> tuple[str, bool]:
    """Take out every sentence that names a tool or a team role, and, unless a checked result stands beside the reply
    (the H1 disagreement, `doubt_allowed`), every sentence that says the explanation may contain an error."""
    kept, removed = [], False
    for sentence in _SENTENCE.findall(text):
        if INTERNALS.search(sentence) or (not doubt_allowed and SELF_DOUBT.search(sentence)):
            removed = True
            continue
        kept.append(sentence)
    return ("".join(kept).strip() if removed else text), removed


def apply_lexicon(text: str) -> tuple[str, list[str]]:
    replaced = []
    out = text
    for phrase, replacement in BANNED.items():
        pattern = re.compile(rf"\b{re.escape(phrase)}", re.I)
        if pattern.search(out):
            out = pattern.sub(replacement, out)
            replaced.append(phrase)
    return re.sub(r"\s{2,}", " ", out).strip(), replaced


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


# --- soft rules cannot override hard rules (PED-10) ------------------------------------------------------------------

CONTRADICTIONS = {
    "reveal_answer": "H2",
    "skip_verification": "H1",
    "show_unchecked_number": "H1",
    "ignore_scope": "H3",
    "unlock_solution": "H4",
    "allow_drawing": "H5",
    "unbounded_retries": "H6",
    "tutor_during_mock": "H7",
    "multiple_questions": "H8",
    "ask_personal_data": "H9",
    "allow_banned_phrases": "H10",
}


class SoftRuleError(ValueError):
    pass


def load_soft_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """A soft rule's action must never contradict a hard rule; the loader rejects it."""
    action = str(rule.get("action", ""))
    if action in CONTRADICTIONS:
        raise SoftRuleError(f"soft rule {rule.get('id')} contradicts hard rule {CONTRADICTIONS[action]}")
    for rule_id in rule.get("overrides", []):
        if rule_id in HARD_RULES:
            raise SoftRuleError(f"soft rule {rule.get('id')} tries to override hard rule {rule_id}")
    return rule
