"""Safety policy in code: scope, distress, moderation layers, canned templates and volunteered personal data (PED-18,
PED-23, PRV-13, PRV-14, PRV-18, PRV-19, PRV-20).

Moderation runs in three local layers (PRV-18), none of which needs a network or a model:

1. The input guard, before any model call: deterministic patterns (`pedagogy.INPUT_PATTERNS`; distress is decided by
   patterns alone) plus a small local classifier. Every student message gets one TABLE PRV-13a category; anything but
   `in_scope` (and `injection`, which is answered as the question only, PRV-16) ends the turn with its template.
   Personal details are redacted before the text is stored or reaches any prompt (PRV-20).
2. The output critic, before anything reaches the student: the gates check numbers, leaks and ids; this module adds
   the safety classification of the draft (`harmful`, `sexual_or_violent`, distress), and on the high hardware class
   and the cloud tier an optional context-free moderation pass that sees only the draft.
3. Canned templates for every outcome that is not in scope (PED-18), with engine-filled variables only.

Any layer that errors fails closed (PRV-01a item 9): the caller shows a canned turn, never the unchecked draft.

The helplines of the distress template come from the safety-resources pack, which is country configuration (D2):
only entries a child-protection partner verified are ever shown (PRV-14)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from agent_core.config import Settings
from agent_core.config.personal_data import CONTACT, contact_kinds, normalise
from agent_core.engine import pedagogy
from agent_core.engine.input import one_line
from agent_core.engine.pedagogy import Templates

# TABLE PRV-13a. The guard decides distress first and personal data last (after every other category).
CATEGORIES = ("in_scope", "distress", "personal_data", "harmful", "sexual_or_violent", "medical_legal_advice",
              "cheating_request", "beyond_level", "off_topic", "injection")  # fmt: skip
Logged = Literal["none", "count", "safety_flagged", "injection_flagged"]


@dataclass(frozen=True)
class Route:
    template: str | None
    logged: Logged


# Categories, routing and logging (TABLE PRV-13a); the wording of each template is PED-18's.
ROUTES: dict[str, Route] = {
    "in_scope": Route(None, "none"),
    "off_topic": Route("tpl.off_topic", "count"),
    "beyond_level": Route("tpl.beyond_level", "count"),
    "cheating_request": Route("tpl.graded_work", "count"),
    "distress": Route("tpl.distress", "safety_flagged"),
    "harmful": Route("tpl.dangerous_request", "safety_flagged"),
    "sexual_or_violent": Route("tpl.abusive_language", "safety_flagged"),
    "personal_data": Route("tpl.personal_details", "count"),
    "injection": Route(None, "injection_flagged"),
    "medical_legal_advice": Route("tpl.see_trusted_adult", "count"),
}
# Every template of TABLE PED-18a: the guard's and the policy templates (H4, H7).
STRICT_TEMPLATES = (*sorted({r.template for r in ROUTES.values() if r.template}), "tpl.mock_running")
CRITICAL = frozenset({"distress", "harmful", "sexual_or_violent"})
# What the output critic classifies a draft for (PRV-17, PRV-14).
OUTPUT_CATEGORIES = ("distress", "harmful", "sexual_or_violent")

# A classifier is the input guard's second half: text in, (category, confidence) out, the `{class, confidence}` of the
# input-guard role (AGT-02). It may raise; the guard then fails closed.
Classifier = Callable[[str], tuple[str, float]]


class SafetyUnavailable(RuntimeError):
    """A moderation layer could not run: the caller shows the safest canned turn, never the draft (PRV-01a item 9)."""


# --- the small local classifier (PRV-18) -----------------------------------------------------------------------------

# Weighted terms per category: a linear score over the words present, capped at 1. Distress is not scored here: it is
# decided by patterns alone so a pattern miss is never hidden. Terms are our own lists; teachers' and the Safety
# lead's word lists replace them (PRV-17, CUR-27).
LEXICON: dict[str, dict[str, float]] = {
    "off_topic": {
        "football": 0.3,
        "match": 0.2,
        "celebrity": 0.4,
        "movie": 0.3,
        "film": 0.2,
        "song": 0.3,
        "lyrics": 0.4,
        "girlfriend": 0.4,
        "boyfriend": 0.4,
        "game": 0.2,
        "fortnite": 0.5,
        "tiktok": 0.5,
        "instagram": 0.5,
        "essay": 0.4,
        "english literature": 0.5,
        "history": 0.3,
        "geography": 0.3,
        "french": 0.3,
        "religious studies": 0.5,
        "english essay": 0.6,
        "write my essay": 0.6,
        "weather": 0.3,
        "recipe": 0.5,
        "dating": 0.5,
    },
    "harmful": {
        "weapon": 0.4,
        "gun": 0.3,
        "knife": 0.2,
        "explosive": 0.3,
        "bomb": 0.4,
        "detonate": 0.4,
        "poison": 0.3,
        "toxic gas": 0.3,
        "overdose": 0.4,
        "meth": 0.5,
        "cocaine": 0.5,
        "steal": 0.3,
        "hack": 0.3,
        "untraceable": 0.4,
        "without anyone knowing": 0.3,
        "hurt someone": 0.6,
    },
    "sexual_or_violent": {
        "porn": 0.6,
        "nude": 0.5,
        "sexy": 0.5,
        "stupid bot": 0.5,
        "idiot": 0.4,
        "shut up": 0.3,
        "hate you": 0.4,
        "kill you": 0.6,
        "beat you": 0.5,
    },
    "medical_legal_advice": {
        "dose": 0.4,
        "dosage": 0.5,
        "prescription": 0.4,
        "medicine": 0.3,
        "pills": 0.3,
        "tablets": 0.2,
        "lawyer": 0.5,
        "sue": 0.4,
        "court": 0.3,
        "police": 0.2,
        "diagnose": 0.4,
        "symptoms": 0.3,
    },
    "cheating_request": {
        "homework": 0.2,
        "assignment": 0.2,
        "due tomorrow": 0.3,
        "copy": 0.3,
        "just the answers": 0.5,
        "all the answers": 0.5,
        "graded": 0.3,
        "submit": 0.2,
        "exam answers": 0.5,
        "do it for me": 0.5,
    },
}


@dataclass(frozen=True)
class LexiconClassifier:
    """The guard's small local classifier: deterministic, offline, no model."""

    lexicon: Mapping[str, Mapping[str, float]] = field(default_factory=lambda: LEXICON)
    categories: tuple[str, ...] | None = None

    def __call__(self, text: str) -> tuple[str, float]:
        lowered = f" {text.lower()} "
        best, score = "in_scope", 0.0
        for category, terms in self.lexicon.items():
            if self.categories is not None and category not in self.categories:
                continue
            total = min(1.0, sum(w for term, w in terms.items() if re.search(rf"\b{re.escape(term)}\b", lowered)))
            if total > score:
                best, score = category, total
        return best, score


# --- distress kinds and languages the guard cannot read (PRV-14, TCH-14) ---------------------------------------------

# Harm by someone else, or not being safe at home: an abuse disclosure, for which no alert is ever written (PRV-14,
# TABLE TCH-14a). Checked before self-harm, so a line with both counts as a disclosure.
ABUSE = re.compile(
    r"\b((he|she|they|someone|somebody|my \w+) (hurts|hurt|hits|hit|beats|beat|touches|touched) me|"
    r"abus(e|ed|ing|es) me|(afraid|scared) to go home|not safe at home|unsafe at home|"
    r"(don'?t|do not|never) feel safe at home|bull(y|ies|ied|ying) me)\b",
    re.I,
)
SELF_HARM = re.compile(
    r"\b((kill|hurt|cut|harm)(t?ing)? myself|want to die|suicid\w*|self[- ]?harm\w*|end (my life|it all)|"
    r"(don'?t|do not) want to (live|be alive)|better off dead)\b",
    re.I,
)
DistressKind = Literal["abuse", "self_harm", "unclassified"]


def distress_kind(text: str) -> DistressKind:
    """What a distress line discloses, decided in code and never stored with the text: `abuse` (harm by someone else
    or not safe at home), `self_harm`, or `unclassified` (a line the guard could not read, which may be either)."""
    if ABUSE.search(text):
        return "abuse"
    return "self_harm" if SELF_HARM.search(text) else "unclassified"


def wellbeing_alert_category(kind: DistressKind) -> str | None:
    """The category of the local parent view's `wellbeing_message_shown` entry (TABLE TCH-14a), or None when none is
    written: never for an abuse disclosure (PRV-14), and never for a line whose kind is unknown, which may be one."""
    return "self_harm" if kind == "self_harm" else None


# Common English words: a message in a language the guard reads (English) carries some of them. The guard's patterns
# and classifier read English only, so a message with almost none is in a language it does not support (PRV-14).
# Our own short list of function words and the most common study words; never a word list copied from elsewhere.
ENGLISH = frozenset(
    """a about above after again against all also am an and any are as ask at back be because been before being
    below between both but by can cannot could did do does doing done down during each else even every few find
    for from get give go going good got had has have having he help her here him his how i if in into is it its
    just know learn let like made make many may me mean means might more most much must my need no not now of off
    on once one only or other our out over own please put question really right said same say see she should show
    so some still such sure take tell than thank thanks that the their them then there these they thing think this
    those through to too try under understand until up us use very want was way we well were what when where which
    while who why will with work would wrong yes yet you your answer area calculate change current distance energy
    equation force formula graph mass number problem speed step time value velocity""".split()
)


VOWELS = frozenset("aeiou")


def unsupported_language(text: str, settings: Settings) -> bool:
    """The message is in a language the guard does not read: at least `guard_language_min_words` words, under
    `guard_language_min_share` of them common English words (a plural counts as its word), and at least
    `guard_language_vowel_share` of them ending in a vowel, the shape of the Bantu and West African languages of the
    pilot regions; an English line of uncommon words (technical terms, markup) ends mostly in consonants. Short lines,
    numbers and maths are never judged."""
    words = re.findall(r"[^\W\d_]{2,}", text.lower())
    if len(words) < settings.guard_language_min_words:
        return False
    known = sum(w in ENGLISH or (w.endswith("s") and w[:-1] in ENGLISH) for w in words)
    vowel_final = sum(w[-1] in VOWELS for w in words)
    return (known / len(words) < settings.guard_language_min_share
            and vowel_final / len(words) >= settings.guard_language_vowel_share)  # fmt: skip


# --- beyond the level (PRV-13 `beyond_level`) ------------------------------------------------------------------------

# Topics past the syllabus for each level (university material for A level; A level material too for O level).
UNIVERSITY = ("eigenvalue", "eigenvector", "fourier transform", "laplace transform", "partial differential",
              "lagrangian", "hamiltonian", "schrodinger", "schrödinger", "quantum field", "tensor", "contour integral",
              "group theory", "real analysis", "general relativity", "maxwell's equations in differential form",
              "jacobian", "taylor series of a multivariable", "molecular orbital theory", "nmr coupling constant",
              "consolidated financial statements under ifrs")  # fmt: skip
A_LEVEL_ONLY = ("integration by parts", "differential equation", "complex number", "maclaurin", "implicit "
                "differentiation", "simple harmonic motion", "gravitational potential", "entropy", "enthalpy of "
                "lattice", "electrode potential", "nucleophilic", "partial fraction")  # fmt: skip


def beyond_level(text: str, level: str) -> bool:
    lowered = text.lower()
    terms = UNIVERSITY + (A_LEVEL_ONLY if level == "O" else ())
    return any(term in lowered for term in terms)


# --- volunteered personal data (PRV-20) ------------------------------------------------------------------------------

MARKER = "[personal detail removed]"
REDACTIONS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    # A leading 0 or a country code, then two more groups: a list of numbers or years in a question is never one.
    "phone": re.compile(r"(?<![\w.])(?:\+\d{1,3}[ -]?\d{2,4}|0\d{1,3})[ -]?\d{3,4}[ -]?\d{3,4}(?![\w.])"),
    "address": re.compile(
        r"\b(?:my (?:home )?address is|i live (?:at|on|in|near))\b[^.?!\n]*"
        r"|\b\d{1,5},?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Street|St|Road|Rd|Avenue|Ave|Close|Lane|Drive|Crescent|Way)"
        r"\b|\bplot\s+\d+\b",
        re.I,
    ),
    "name": re.compile(r"\b(?:my (?:full )?name is|call me|i am called|i'm called)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z'-]+)+"),
}


# The shared contact patterns (config/personal_data.py, also read by the egress check and the gateway) name a digital
# address as its own kind; here it counts as an address.
_SHARED_KIND = {"digital_address": "address"}


def _redacted(text: str, found: list[str]) -> str:
    out = text
    for kind, pattern in (*REDACTIONS.items(), *CONTACT.items()):
        out, n = pattern.subn(MARKER, out)
        if n:
            found.append(_SHARED_KIND.get(kind, kind))
    return out


def redact_personal(text: str) -> tuple[str, list[str]]:
    """The text with each phone number, email address, street or digital address and name introduction replaced by a
    marker, and the kinds removed. Applied before persistence and before any model call (PRV-20). The text is kept as
    typed (so `x²` stays `x²`) unless a detail is still there once it is normalised (lookalike letters, zero-width
    characters, escapes, fullwidth forms): then the normalised text is redacted, the same reading the egress check
    and the gateway use (MDL-25)."""
    found: list[str] = []
    out = _redacted(text, found)
    if contact_kinds(out):
        out = _redacted(normalise(out), found)
    return out, list(dict.fromkeys(found))


def redact_blocks(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Command content blocks with the student's text redacted (PRV-20): what the API stores and the engine reads."""
    out = []
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            block = {**block, "text": redact_personal(block["text"])[0]}
        if block.get("type") == "ledger_table":
            # A cell is one line: its breaks would otherwise start rows of their own in the text form (PRM-15).
            block = {**block, "rows": [[redact_personal(one_line(cell))[0] for cell in row] for row in block["rows"]]}
            block["text_fallback"] = "\n".join(
                [" | ".join(block["columns"]), *(" | ".join(row) for row in block["rows"])]
            )
        out.append(block)
    return out


# --- academic honesty (PED-23) ---------------------------------------------------------------------------------------

ASKS_IF_HUMAN = re.compile(
    r"\b(are you (an? )?(real|human|person|robot|bot|ai|computer|machine)\b|are you (a )?real (person|human|teacher)|"
    r"am i (talking|chatting|speaking) (to|with) (a )?(real )?(person|human|bot|robot|ai|computer)|"
    r"is (this|there) a (real )?(person|human)|is this (an? )?(ai|bot|robot|computer))",
    re.I,
)


def asks_if_human(text: str) -> bool:
    """The student asks whether the tutor is a person: always answered truthfully by `tpl.ai_disclosure`."""
    return bool(ASKS_IF_HUMAN.search(text))


# --- the input guard (layer 1) ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardResult:
    category: str  # a TABLE PRV-13a category; "unavailable" when the guard failed closed
    template: str | None
    text: str  # the message with personal details redacted
    redacted: tuple[str, ...] = ()
    layer: str = "none"  # pattern, classifier, error or none
    injection: str | None = None  # the scanner's pattern id when the message carries instructions (PRV-16)

    @property
    def failed(self) -> bool:
        return self.category == "unavailable"

    @property
    def logged(self) -> Logged:
        return ROUTES[self.category].logged if self.category in ROUTES else "none"


def first_injection(text: str) -> str | None:
    """The untrusted-input scanner's first pattern id in the text, or None (PRM-18, PRV-16)."""
    from agent_core.prompts.untrusted import scan

    hits = scan(text)
    return hits[0].pattern_id if hits else None


@dataclass
class InputGuard:
    """The input guard (PRV-13, PRV-18 layer 1): runs on every student message before any model call.

    Every safety category is decided before `personal_data`: a phone number, an email address or the API's redaction
    marker in a message never hides what else it says (PRV-13, PRV-20). Personal data routes to its notice only when
    nothing else fired."""

    settings: Settings
    classifier: Classifier | None = field(default_factory=LexiconClassifier)
    # The untrusted-input scanner (PRM-18, PRV-16): text in, a pattern id or None out.
    injection_scan: Callable[[str], str | None] | None = first_injection
    # PRV-14: on the way to a tutoring model, a message the guard cannot read fails closed to the distress template.
    # Answer boxes and notes, which reach no tutoring model, leave this off: a short answer is not a language.
    unreadable_fails_closed: bool = False

    def check(self, text: str, level: str = "O", model: tuple[str, float] | None = None) -> GuardResult:
        """`model` is the input-guard role's `(class, confidence)` for this message (AGT-02), when its call answered:
        it decides after the patterns, which always win (distress above all, PED-18); without it, or when it finds
        the message in scope, the local classifier decides as before."""
        redacted, kinds = redact_personal(text)
        # The patterns run on the redacted text too, so a detail removed earlier can never be what hides a category.
        category = next((c for c in (pedagogy.classify_input(t, skip=("personal_data",)) for t in (text, redacted))
                         if c is not None), None)  # fmt: skip
        layer = "pattern" if category else "none"
        if category is None and beyond_level(text, level):
            category, layer = "beyond_level", "pattern"
        if category is None and model is not None:
            found, confidence = model
            if found in ROUTES and found != "in_scope" and confidence >= self.settings.guard_classifier_threshold:
                category, layer = found, "model"
        # PRV-14 (JRN-10 F2): a message in a language the guard's patterns and classifier cannot read, which the
        # input-guard role did not classify with confidence either, fails closed to the distress template: it never
        # reaches a tutoring model.
        confident = model is not None and model[1] >= self.settings.guard_classifier_threshold
        unreadable = self.unreadable_fails_closed and not confident
        if category is None and unreadable and unsupported_language(redacted, self.settings):
            category, layer = "distress", "language"
        if category is None and self.classifier is not None:
            try:
                found, confidence = self.classifier(redacted)
            except Exception:
                return GuardResult("unavailable", None, redacted, tuple(kinds), "error")
            if found in ROUTES and found != "in_scope" and confidence >= self.settings.guard_classifier_threshold:
                category, layer = found, "classifier"
        personal = bool(kinds) or MARKER in text or pedagogy.INPUT_PATTERNS["personal_data"].search(text) is not None
        if category is None and personal:  # last: the API may have redacted it already (PRV-20)
            category, layer = "personal_data", "pattern"
        injection = self.injection_scan(text) if self.injection_scan is not None else None
        if category is None and injection is not None:
            category, layer = "injection", "pattern"
        category = category or "in_scope"
        return GuardResult(category, ROUTES[category].template, redacted, tuple(kinds), layer, injection)

    def admits(self, text: str, level: str = "O") -> bool:
        """The message may go to a model as it is: in scope, and not a question about whether the tutor is human."""
        result = self.check(text, level)
        return result.category == "in_scope" and not asks_if_human(result.text)


# --- the output critic's safety classification (layer 2) -------------------------------------------------------------


@dataclass
class OutputCritic:
    """The safety half of the output critic (PRV-18 layer 2, PRV-17, PRV-14): returns the template that replaces a
    draft in a risky category, or None. A failing check raises `SafetyUnavailable`."""

    classifier: Classifier | None = field(
        default_factory=lambda: LexiconClassifier(categories=("harmful", "sexual_or_violent"))
    )
    # The context-free moderation pass of the high class and the cloud tier: sees only the draft (Aila pattern).
    context_free: Classifier | None = None
    threshold: float = 0.5

    def check(self, draft: str, hardware_class: str) -> str | None:
        for category in OUTPUT_CATEGORIES:
            if pedagogy.INPUT_PATTERNS[category].search(draft):
                return ROUTES[category].template
        passes: list[Classifier] = [c for c in (self.classifier,) if c is not None]
        if hardware_class in ("high", "cloud") and self.context_free is not None:
            passes.append(self.context_free)
        for classify in passes:
            try:
                found, confidence = classify(draft)
            except Exception as error:
                raise SafetyUnavailable(type(error).__name__) from None
            if found in OUTPUT_CATEGORIES and confidence >= self.threshold:
                return ROUTES[found].template
        return None


# --- the safety-resources pack (PRV-14, D2) --------------------------------------------------------------------------


@dataclass(frozen=True)
class Helpline:
    kind: str  # emergency or child_helpline
    name: str
    number: str
    hours: str


@dataclass(frozen=True)
class SafetyResources:
    """The pilot country's verified entries. An entry is shown only when it is marked verified and names who verified
    it; everything else is dropped on load."""

    country: str | None
    entries: tuple[Helpline, ...] = ()

    @classmethod
    def load(cls, path: Path) -> SafetyResources:
        if not path.is_file():
            return cls(None)
        data = yaml.safe_load(path.read_text()) or {}
        return cls.from_data(data)

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> SafetyResources:
        kept = []
        for raw in data.get("entries") or []:
            if raw.get("verified") is not True or not raw.get("verified_by"):
                continue
            if raw.get("kind") not in ("emergency", "child_helpline") or not raw.get("number"):
                continue
            kept.append(Helpline(str(raw["kind"]), str(raw.get("name") or ""), str(raw["number"]),
                                 str(raw.get("hours") or "")))  # fmt: skip
        return cls(data.get("country"), tuple(kept))

    def values(self) -> dict[str, str]:
        """The distress template's verified variables; a missing one leaves its sentence out."""
        out: dict[str, str] = {}
        emergency = next((e for e in self.entries if e.kind == "emergency"), None)
        helpline = next((e for e in self.entries if e.kind == "child_helpline"), None)
        if emergency is not None:
            out["emergency_number"] = emergency.number
        if helpline is not None:
            out["child_helpline"] = f"{helpline.name} on {helpline.number}" if helpline.name else helpline.number
        return out

    def helplines(self) -> list[Helpline]:
        return [e for e in self.entries if e.kind == "child_helpline"]


def calm_screen_notice(notice_id: str) -> dict[str, Any]:
    """The `notice` that tells the client to replace the workspace with the calm screen (PRV-14, UXS-43). It carries
    no text of the student's and never names the category; the client shows the verified helplines it holds."""
    return {"type": "notice", "notice_id": notice_id, "level": "info", "code": "calm_screen",
            "message_key": "safety.calm_screen", "params": {}, "action": None}  # fmt: skip


# --- localised template sets (PRV-19) --------------------------------------------------------------------------------

REVIEWED = "reviewed"


@dataclass(frozen=True)
class TemplateSet:
    """Every shipped language's templates. A language whose file is not reviewed, or that lacks a template, falls back
    to English for it (PRV-19)."""

    languages: dict[str, Templates]

    @classmethod
    def load(cls, folder: Path) -> TemplateSet:
        # One `<language>.yaml` per language; other copy in the folder (`engine.<language>.yaml`) is not a template set.
        return cls({p.stem: Templates.load(p) for p in sorted(folder.glob("*.yaml")) if "." not in p.stem})

    def resolve(self, template_id: str, language: str) -> Templates:
        chosen = self.languages.get(language)
        if chosen is not None and chosen.status == REVIEWED and template_id in chosen.texts:
            return chosen
        english = self.languages["en"]
        if template_id not in english.texts:
            raise KeyError(template_id)
        return english


TEMPLATE_ID = re.compile(r"[\"'](tpl\.[a-z0-9_]+)[\"']")


def referenced_template_ids(root: Path) -> set[str]:
    """Every template id the core's code names (PRV-19)."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        if "/contracts/" in path.as_posix():
            continue
        found.update(TEMPLATE_ID.findall(path.read_text()))
    return found
