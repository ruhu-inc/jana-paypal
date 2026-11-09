"""The note write policy (MEM-07, LRN-38), applied to every candidate before it is stored and again to every stored
note when it is rendered into a prompt (MEM-27).

A candidate passes when it is short enough, states a fact rather than an instruction, carries the session's date
and no relative time words, cites events that exist, are not erased and belong to this profile, quotes nothing the
student wrote, holds no invisible or bidirectional-control characters and nothing shaped like a secret, and is not
already stored as an active note.

The text rules (`text_rejections`) need no database and are what `render_check` runs on a stored note; the evidence,
date and duplicate rules need the session and the log. Every rule is a fixed, deterministic check: no model is asked
whether a note is acceptable. A rejection carries a code and a short detail for the job trace, never the note text.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

from agent_core.config import Settings

if TYPE_CHECKING:  # the filter is optional at render time; importing it here only for the annotation
    from agent_core.memory.capture import CaptureFilter

RejectionCode = Literal[
    "too_long",
    "instruction_shaped",
    "date_mismatch",
    "relative_time",
    "no_evidence",
    "unknown_evidence",
    "erased_evidence",
    "foreign_evidence",
    "quoted_text",
    "copied_words",
    "hidden_characters",
    "secret_like",
    "duplicate",
    "unknown_target",  # an update, drop or contradiction aimed at a note that is not (or no longer) active
    "captured",  # render time only: the do-not-capture filter matches a stored note (MEM-10, MEM-27)
    "review_open",  # a contradiction or proposed change for a note that already has an open review item (MEM-09)
    "review_full",  # the subject already has `notes_max_reviews_per_subject` open review items (MEM-09)
    "forgotten",  # the same text as a note the student deleted while the job ran
]


@dataclass(frozen=True)
class Candidate:
    """A note the Reflector proposes, with its evidence already mapped from integer refs to event ids."""

    text: str
    date: str  # YYYY-MM-DD
    evidence_event_ids: tuple[str, ...]
    subject: str | None = None
    kc_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    """Why a candidate (or a stored note at render time) failed: a fixed code and a short detail for the job trace,
    never the note text."""

    code: RejectionCode
    detail: str = ""


# --- normalised form and the content hash ---------------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def normalise(text: str) -> str:
    """The comparison form of a note for duplicate detection (case, spacing and punctuation folded)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_WORD.findall(folded))


def content_hash(text: str) -> str:
    """sha256 of the normalised text: the key that keeps each fact in one home (MEM-07)."""
    return hashlib.sha256(normalise(text).encode()).hexdigest()


def words(text: str) -> list[str]:
    """The normalised words of a text, in order (the unit of the copied-words check)."""
    return normalise(text).split()


# --- text rules ------------------------------------------------------------------------------------------------------

# A note states what was observed about the student's learning, in the third person. A note that opens with a bare
# verb, addresses someone, or uses a directive modal is telling the tutor what to do: an instruction, not a fact.
_IMPERATIVE_OPENERS = frozenset(
    """always never ensure make remember do don't dont avoid use start begin tell ask explain give show keep stop try
    ignore forget disregard override please let be act pretend respond reply answer say write treat call switch skip
    focus repeat assume consider note mention praise encourage check""".split()
)
_DIRECTIVE = re.compile(
    r"\b(?:you|your|yourself|should|must|ought\s+to|shall|needs?\s+to\s+be\s+(?:told|given|shown|reminded)"
    r"|(?:the\s+)?(?:tutor|assistant|model|ai|system|coach)\s+(?:should|must|will|needs|has\s+to)"
    r"|from\s+now\s+on|going\s+forward|in\s+future|next\s+time|system\s+prompt|developer\s+message"
    r"|(?:previous|prior|above|earlier|all)\s+(?:instructions?|messages?|rules?))\b",
    re.IGNORECASE,
)
# Facts that are instructions in disguise (MEM-07, MEM-27): a permission the note grants ("is allowed to", "has adult
# permission"), the tutor giving or revealing answers ("the tutor gives the full answer", "answers be revealed"),
# hints or working skipped ("hints are skipped", "skip the working"), and guidance set aside ("guidance disregarded").
# A student's own learning stays a fact: "Gives answers to three significant figures", "Solves it without hints".
_ANSWERS = r"(?:the\s+|full\s+|final\s+|whole\s+|complete\s+|worked\s+|model\s+)*(?:answers?|solutions?|working)"
_HELP = r"(?:the\s+|any\s+|all\s+)?(?:hints?|working|steps?|checks?|checking|questions?|explanations?)"
_INSTRUCTION_FACT = re.compile(
    rf"\b(?:(?:has|have|had|got|given|with)\s+(?:an?\s+)?(?:adult|parental|parent|teacher|guardian|special)?\s*"
    rf"(?:permission|approval|consent)"
    rf"|(?:is|are|was|be|being)\s+(?:allowed|permitted|entitled|authori[sz]ed)\s+to"
    rf"|(?:per|according\s+to|as\s+agreed\s+with)\s+(?:their|his|her|the)\s+(?:teacher|parent|parents|guardian|school)"
    rf"|(?:tutor|teacher|app|ai|assistant|model|coach|system)\s+(?:just\s+|always\s+|simply\s+)?"
    rf"(?:gives?|giving|reveals?|revealing|shows?|showing|tells?|telling|provides?|providing|skips?|skipping)\b"
    rf"|{_ANSWERS}\s+(?:be|being|are|is|get|gets)?\s*(?:revealed|given|shown|provided|handed\s+over)\s+"
    rf"(?:straight\s+away|at\s+once|immediately|right\s+away|first|up\s*front|before|without|with\s+no)"
    rf"|(?:be|being)\s+(?:revealed|given|shown|provided|told)\s+(?:the\s+)?{_ANSWERS}"
    rf"|(?:that|wants?|prefers?|likes?)\s+{_ANSWERS}\s+(?:be|being|are)\s+(?:revealed|given|shown|provided)"
    rf"|(?:reveal|revealing|reveals)\s+(?:the\s+)?{_ANSWERS}"
    rf"|(?:full|complete|whole|worked)\s+(?:answers?|solutions?)\s+(?:to|for)\s+(?:every|each|all|any)\s+questions?"
    rf"|no\s+questions\s+asked"
    rf"|{_HELP}\s+(?:are|is|be|being|get|gets)\s+(?:skipped|left\s+out|removed|dropped|not\s+(?:needed|necessary|"
    rf"required|wanted)|unnecessary)"
    rf"|(?:skip|skips|skipping|skipped|drop|drops|dropping|without)\s+(?:the\s+|any\s+|all\s+)?(?:hints?|"
    rf"working)\s+(?:entirely|altogether|completely|always|every\s+time)"
    rf"|(?:permission|allowed|permitted)\s+to\s+(?:skip|see|get|have|copy|use)"
    rf"|(?:guidance|instructions?|rules?|advice|settings?|polic(?:y|ies)|guidelines?)\s+(?:be\s+|being\s+|are\s+|is\s+)?"
    rf"(?:disregarded|ignored|dropped|overridden|bypassed|set\s+aside|switched\s+off|turned\s+off)"
    rf"|(?:disregard|ignore|override|bypass)s?\s+(?:all\s+|any\s+|the\s+)?(?:previous\s+|prior\s+|earlier\s+|"
    rf"existing\s+|other\s+)?(?:guidance|instructions?|rules?|advice|settings?|polic(?:y|ies)|guidelines?))\b",
    re.IGNORECASE,
)
_RELATIVE_TIME = re.compile(
    r"\b(?:today|tonight|yesterday|tomorrow|recently|lately|nowadays|currently|soon|ago|earlier\s+today"
    r"|this\s+(?:morning|afternoon|evening|week|weekend|month|term|year|session)"
    r"|(?:last|next|previous|coming)\s+(?:night|week|weekend|month|term|year|time|session|lesson|class)"
    r"|the\s+other\s+day|these\s+days|right\s+now|just\s+now|a\s+while\s+back)\b",
    re.IGNORECASE,
)
# Quotation marks of any style, or single quotes wrapped around words (an apostrophe inside a word is fine).
_QUOTES = re.compile(
    r"[\"“”„‟«»「」『』]|(?:^|[\s(\[])['\u2018\u201b][^'\u2018\u2019\u201b]+['\u2019](?=$|[\s.,;:!?)\]])"
)
# Secret-shaped text: links and addresses, contact handles, phone numbers, credential words, and key-like tokens.
_SECRET = re.compile(
    r"(?:https?://|www\.|\bftp://|\b[\w.+-]+@[\w-]+\.[\w.]+\b|(?<![\w@])@[A-Za-z0-9_]{3,}"
    r"|\b[a-z0-9-]+\.(?:com|org|net|io|co|ng|gh|ke|za|uk|edu|gov|app|dev|ly|me|info|biz)\b"
    r"|\b(?:password|passcode|passwd|pass\s*word|pin|otp|one[-\s]time\s+code|login|log-in|username|user\s+name"
    r"|api[-\s]?key|access\s+key|secret|token|verification\s+code|security\s+code|wifi\s+key"
    r"|(?:account|access|portal|door|gate|lock|unlock|school|exam|login|entry)\s+codes?|codes?\s+(?:is|was|are))\b"
    r"|(?<![\d-])\+?\d(?:[\s-]?\d){8,}(?![\d-]))",
    re.IGNORECASE,
)
# Long runs mixing letters and digits, or three or more dash-joined groups with digits in them, look like keys and
# codes rather than words.
_KEYLIKE = re.compile(
    r"\b(?=[A-Za-z0-9_-]{16,}\b)(?=[A-Za-z_-]*\d)(?=[0-9_-]*[A-Za-z])[A-Za-z0-9_-]+\b"
    r"|\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]{2,}){2,}\b"
)
_LONG_DIGITS = re.compile(r"(?<![\d.,-])\d{4,}(?![\d.,-])")
# Variation selectors sit in a combining category but change nothing a reader can see.
_INVISIBLE_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF), (0x180B, 0x180F), (0x115F, 0x1160), (0x3164, 0x3164))


def hidden_characters(text: str) -> list[str]:
    """Code points a reader cannot see that change what a model reads: controls (line breaks included), format
    characters such as zero-width and bidirectional controls, private-use, unassigned and surrogate code points,
    separators other than a plain space, and variation selectors. Returned as U+XXXX names, never the text."""
    found: list[str] = []
    for char in text:
        point = ord(char)
        category = unicodedata.category(char)
        hidden = category in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp") or (category == "Zs" and char != " ")
        hidden = hidden or any(low <= point <= high for low, high in _INVISIBLE_RANGES)
        if hidden:
            found.append(f"U+{point:04X}")
    return found


def instruction_shaped(text: str) -> bool:
    """A note that tells the tutor what to do: a bare-verb opener, a directive word, or a fact that grants a
    permission, has answers revealed, has hints or working skipped, or sets guidance aside (MEM-07, MEM-27)."""
    first = words(text)[:1]
    if first and first[0] in _IMPERATIVE_OPENERS:
        return True
    return _DIRECTIVE.search(text) is not None or _INSTRUCTION_FACT.search(" ".join(text.split())) is not None


def quoted(text: str) -> bool:
    """Quotation marks of any style, or single quotes around words: the shape of the student's words repeated."""
    return _QUOTES.search(text) is not None


def secret_like(text: str) -> bool:
    if _SECRET.search(text) or _KEYLIKE.search(text):
        return True
    # A run of four or more digits that is not part of a decimal or a quantity reads as a code or a PIN. Years and
    # SI quantities in learning notes are written with units or separators, so this stays narrow.
    for match in _LONG_DIGITS.finditer(text):
        if len(match.group()) == 4 and 1900 <= int(match.group()) <= 2099:
            continue  # a year
        after = text[match.end() : match.end() + 3].strip()
        if not after or not after[0].isalpha():
            return True
    return False


def text_rejections(text: str, settings: Settings) -> list[Rejection]:
    """Every rule that looks only at the text: length, instruction shape, relative time words, quotes, hidden
    characters and secret-like strings. Hidden characters are checked first, on the raw text: the other rules read a
    text a model would read, and that must not hide anything."""
    out: list[Rejection] = []
    hidden = hidden_characters(text)
    if hidden:
        out.append(Rejection("hidden_characters", ",".join(sorted(set(hidden))[:5])))
    stripped = text.strip()
    if not 1 <= len(stripped) <= settings.notes_max_chars or len(text) > settings.notes_max_chars:
        out.append(Rejection("too_long", f"length {len(text)} outside 1..{settings.notes_max_chars}"))
    if stripped and instruction_shaped(stripped):
        out.append(Rejection("instruction_shaped"))
    if _RELATIVE_TIME.search(text):
        out.append(Rejection("relative_time"))
    if quoted(text):
        out.append(Rejection("quoted_text"))
    if secret_like(text):
        out.append(Rejection("secret_like"))
    return out


# --- rules that need the session and the log ------------------------------------------------------------------------

# Events that are not learning evidence: notes and corrections of notes cannot support a note.
_NOT_EVIDENCE = frozenset({"note_written", "note_deleted"})


def evidence_rejections(conn: sqlite3.Connection, profile_id: str, event_ids: Sequence[str]) -> list[Rejection]:
    """The event-log check of LRN-38: at least one cited event, and every cited event exists in this profile's log,
    is learning evidence and is not erased."""
    if not event_ids:
        return [Rejection("no_evidence")]
    out: list[Rejection] = []
    for position, event_id in enumerate(event_ids, start=1):
        row = conn.execute(
            "SELECT profile_id, type, erased_at, json_extract(payload, '$.target') FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            out.append(Rejection("unknown_evidence", f"evidence {position} is not in the log"))
        elif row[0] != profile_id:
            out.append(Rejection("foreign_evidence", f"evidence {position} belongs to another profile"))
        elif row[2] is not None:
            out.append(Rejection("erased_evidence", f"evidence {position} was erased"))
        elif row[1] in _NOT_EVIDENCE or (row[1] == "correction" and row[3] == "note"):
            out.append(Rejection("unknown_evidence", f"evidence {position} is not learning evidence"))
    return out


def _parse_date(text: str) -> date | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def date_rejections(note_date: str, session_date: str) -> list[Rejection]:
    """The note's date is the session date, or one day either side (a session that crosses midnight, G95)."""
    seen, session = _parse_date(note_date), _parse_date(session_date)
    if seen is None or session is None or abs(seen - session) > timedelta(days=1):
        return [Rejection("date_mismatch")]
    return []


def copied_run(text: str, student_texts: Iterable[str], run: int) -> bool:
    """Whether `text` repeats `run` or more consecutive words of anything the student wrote (MEM-10: quoted free
    text is never captured, whatever the quotation marks)."""
    note = words(text)
    if len(note) < run:
        return False
    shingles = {tuple(note[i : i + run]) for i in range(len(note) - run + 1)}
    for source in student_texts:
        said = words(source)
        if any(tuple(said[i : i + run]) in shingles for i in range(len(said) - run + 1)):
            return True
    return False


def validate_candidate(
    candidate: Candidate,
    *,
    conn: sqlite3.Connection,
    profile_id: str,
    session_date: str,
    active_hashes: Collection[str],
    student_texts: Collection[str],
    settings: Settings,
) -> list[Rejection]:
    """Every rule of MEM-07 and LRN-38 against one candidate; an empty list means it may be stored. The date may be
    the session date or one day either side. `student_texts` are the student's own words in the window, for the
    quote check (`capture_quote_max_words`)."""
    out = text_rejections(candidate.text, settings)
    out += date_rejections(candidate.date, session_date)
    out += evidence_rejections(conn, profile_id, candidate.evidence_event_ids)
    if copied_run(candidate.text, student_texts, settings.capture_quote_max_words):
        out.append(Rejection("copied_words", f"repeats {settings.capture_quote_max_words}+ of the student's words"))
    if content_hash(candidate.text) in active_hashes:
        out.append(Rejection("duplicate"))
    return out


def render_check(text: str, settings: Settings, capture: CaptureFilter | None = None) -> list[Rejection]:
    """The same text rules at render time (MEM-27): length, instruction shape, relative time, quotes, hidden
    characters and secrets, and the do-not-capture filter when one is given. A note that fails is left out of the
    prompt and flagged on the knows page."""
    out = text_rejections(text, settings)
    if capture is not None:
        verdict = capture.check(text)
        if verdict.blocked:
            out.append(Rejection("captured", str(verdict.category)))
    return out
