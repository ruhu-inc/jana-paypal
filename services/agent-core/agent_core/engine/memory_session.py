"""A session's view of the student's memory (MEM-03, MEM-11, MEM-13, MEM-14, PRM-17, TOL-23).

`SessionMemory` freezes the learner snapshot at the session's first Coach prepare and renders the learner block (tier
6) from it for each call's variant: local (with the notes in their records wrapper) or cloud (ids and bands only, no
notes). The snapshot lives in this object only: it is never checkpointed, so a restored session freezes afresh. The
snapshot is refreshed after a committed explicit memory edit or a durable memory revision changes.

A session reaches memory only through `MemoryAccess`, bound to the profile the session belongs to: when the open
learner database is another profile's (after a switch), every read refuses. Whether saving is on is read again for
every turn, because it governs an action (MEM-03), and reaches the Coach as one line of the per-turn block.

Also here, because both are about what the tutor may claim: the explicit "remember that ..." request (MEM-06,
MEM-11), and the critic that takes a claim of having saved something out of a reply (PRM-17, LRN-04, MEM-23). Only
the engine announces a save, with `memory.saved`; the model's words never do.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from agent_core.config import Settings
from agent_core.learner.service import Learner
from agent_core.memory.capture import CaptureFilter
from agent_core.memory.queries import ContentLabels, memory_saving
from agent_core.memory.recall import recall_sessions, record_recalls
from agent_core.memory.refs import RefTable
from agent_core.memory.render import LearnerBlock, Variant, fit_block
from agent_core.memory.snapshot import FrozenMemory, freeze
from agent_core.memory.visibility import revision

SUBJECTS = frozenset({"mathematics", "physics", "chemistry", "biology", "accounting"})


@dataclass(frozen=True)
class MemorySource:
    """What memory is read from: the open learner database, the installed packs' labels and the loaded capture
    filter (the runtime's, never skipped)."""

    learner: Learner
    content: ContentLabels
    capture: CaptureFilter | None

    @property
    def conn(self) -> sqlite3.Connection:
        return self.learner.conn


@dataclass
class MemoryAccess:
    """A session's way to its own profile's memory: `profile_id` is the internal id of the profile the session belongs
    to; `open_source` returns whatever learner database the core has open now."""

    profile_id: str
    open_source: Callable[[], MemorySource | None]

    def source(self) -> MemorySource | None:
        """The open source when it is this session's profile's, else None (TOL-23: refused on a mismatch)."""
        found = self.open_source()
        if found is None or found.learner.log.profile_id != self.profile_id:
            return None
        return found


@dataclass
class SessionMemory:
    """The frozen snapshot and the blocks rendered from it, for one session."""

    access: MemoryAccess
    settings: Settings
    session_id: str
    frozen: FrozenMemory | None = None
    topic: str | None = None
    _revision: str = ""
    _freeze_args: tuple[str, tuple[str, ...], str] | None = None
    _blocks: dict[tuple[str, int], LearnerBlock] = field(default_factory=dict)
    refs: RefTable = field(default_factory=lambda: RefTable.of(()))  # the note refs the Coach was shown (MEM-08)
    progress_seen: dict[str, float] = field(default_factory=dict)  # kc -> probability before the latest verdict
    _topic_kcs: tuple[str, ...] | None = None
    # SPEC §19.6 (W8 phase 3): `tutor_memory_online` effective for this session's profile now; while it is, a cloud
    # call's block is the scrubbed cloud_notes variant. `terms` are the Redactor's profile terms for the scrub.
    memory_online: Callable[[], bool] = lambda: False
    terms: Callable[[], Sequence[str]] = lambda: ()

    def source(self) -> MemorySource | None:
        return self.access.source()

    def refresh(self) -> None:
        """A committed explicit memory edit invalidates this session's frozen prompt and reference tables."""
        self.frozen = None
        self._blocks.clear()
        self.refs = RefTable.of(())

    def freeze(self, *, subject: str, focus: Sequence[str], session_date: str) -> FrozenMemory | None:
        """Run the fixed queries once (MEM-03, MEM-04); later calls return the same snapshot."""
        source = self.source()
        if source is not None and revision(source.conn) != self._revision:
            self.refresh()
        if self.frozen is not None:
            return self.frozen
        if source is None:
            return None
        self._revision = revision(source.conn)
        self._freeze_args = (subject, tuple(focus), session_date)
        self.topic = self._topic(source, focus)
        self.frozen = freeze(source.conn, source.content, self.settings, subject=_subject(subject),
                             topic_id=self.topic, session_date=session_date, focus=focus)  # fmt: skip
        return self.frozen

    def opened_topic_kcs(self) -> tuple[str, ...]:
        """The KCs of the topic the session was opened on (its lesson context, SKL-08), read once; none when the
        session has no topic or no memory is open."""
        if self._topic_kcs is None:
            source = self.source()
            if source is None:
                return ()
            row = source.conn.execute(
                "SELECT topic_id FROM sessions WHERE session_id = ?", (self.session_id.removeprefix("ses_"),)
            ).fetchone()
            self._topic_kcs = tuple(source.content.kcs_in_topic(str(row[0]))) if row is not None and row[0] else ()
        return self._topic_kcs

    def _topic(self, source: MemorySource, focus: Sequence[str]) -> str | None:
        """The session's topic: the one it was opened on, else the topic of the current item's first KC."""
        row = source.conn.execute(
            "SELECT topic_id FROM sessions WHERE session_id = ?", (self.session_id.removeprefix("ses_"),)
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])
        for kc in focus:
            topic = source.content.topic_of(kc)
            if topic is not None:
                return topic
        return None

    def block(self, variant: Variant, allowance_chars: int) -> LearnerBlock | None:
        """The learner block for a call, rendered once per variant and allowance: the same snapshot always gives the
        same text, so the prompt prefix stays cacheable (MEM-03, LRN-05)."""
        source = self.source()
        if source is None:
            return None
        if self._freeze_args is not None and (self.frozen is None or revision(source.conn) != self._revision):
            subject, focus, session_date = self._freeze_args
            self.freeze(subject=subject, focus=focus, session_date=session_date)
        if self.frozen is None:
            return None
        if variant == "cloud" and self.memory_online():
            variant = "cloud_notes"
        key = (variant, allowance_chars)
        if key not in self._blocks:
            source = self.source()
            capture = source.capture if source is not None else None
            recalls = recall_sessions(source.conn) if source is not None else {}
            self._blocks[key] = fit_block(self.frozen, variant=variant, allowance_chars=allowance_chars,
                                          settings=self.settings, capture=capture,
                                          terms=tuple(self.terms()) if variant == "cloud_notes" else (),
                                          recalls=recalls)  # fmt: skip
            if source is not None and self._blocks[key].shown_note_ids:
                record_recalls(source.conn, self._blocks[key].shown_note_ids, self.session_id,
                               self.frozen.session_date)  # fmt: skip
        built = self._blocks[key]
        if variant in ("local", "cloud_notes"):
            self.refs = built.note_refs
        return built

    def without_note(self, note_id: str) -> bool:
        """A forgotten note leaves the frozen snapshot (MEM-14, UXS-28): the only change it allows. The refs already
        shown keep their numbers; a ref to the gone note now reads nothing."""
        if self.frozen is None or note_id not in self.frozen.note_ids:
            return False
        self.frozen = self.frozen.without_note(note_id)
        self._blocks.clear()
        return True

    def saving_on(self) -> bool | None:
        """Whether the student has saving on, read now (None when the session cannot reach its memory)."""
        source = self.source()
        return memory_saving(source.conn, self.settings) if source is not None else None

    def before_verdict(self, kc_ids: Sequence[str]) -> None:
        """The KCs' probabilities before a verdict is recorded, so `progress_update` can say which way they moved."""
        source = self.source()
        if source is None:
            return
        for kc in kc_ids:
            row = source.conn.execute("SELECT p_mastery FROM kc_mastery WHERE kc_id = ?", (kc,)).fetchone()
            self.progress_seen[kc] = float(row[0]) if row is not None else -1.0


def _subject(subject: str) -> str | None:
    return subject if subject in SUBJECTS else None


# --- what the Coach is told each turn (PRM-15, PRM-17) ----------------------------------------------------------

RequestOutcome = Literal["queued", "declined", "off", "unbound"]

SAVING_ON = "notes: saving is on; the app decides what goes in the student's notes and tells them itself"
SAVING_OFF = "notes: saving is off; never say you will remember or note anything"
NEVER_CLAIM = "never say you saved, noted or will remember something: only the app says that"
REQUEST_LINES: dict[RequestOutcome, str] = {
    "queued": "notes request: the student asked the app to keep something in their notes; the app checks it after "
    "your reply and tells them; do not promise anything, carry on with the work",
    "declined": "notes request: the student asked the app to keep something it never keeps; a fixed line already "
    "told them; do not repeat or discuss it, carry on with the work",
    "off": "notes request: the student asked the app to keep something, but saving notes is off; say saving is off "
    "and can be switched on in Settings, then carry on",
    "unbound": "notes request: the student asked the app to keep something; notes are not available here; say so "
    "briefly",
}


def memory_lines(saving: bool | None, request: RequestOutcome | None) -> list[str]:
    """The per-turn lines about memory: saving on or off, and one line for an explicit request this turn. No line
    ever carries the student's words."""
    lines: list[str] = []
    if saving is True:
        lines += [SAVING_ON, NEVER_CLAIM]
    elif saving is False:
        lines.append(SAVING_OFF)
    if request is not None:
        lines.append(REQUEST_LINES[request])
    return lines


# --- the explicit request (MEM-06, MEM-11) ---------------------------------------------------------------------

REQUEST_MAX_CHARS = 300
_LEAD = r"(?:(?:please|pls|ok|okay|and|also|hey|oh)[\s,]+)*"
_ASK = (r"(?P<ask>(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need|would\s+like)\s+you\s+to\s+"
        r"|please\s+)?")  # fmt: skip
_VERB = (r"(?:remember|note\s+down|make\s+a\s+note(?:\s+of)?|take\s+a\s+note(?:\s+of)?|keep\s+in\s+mind"
         r"|don'?t\s+forget|do\s+not\s+forget|save\s+(?:a\s+note|this|that))")  # fmt: skip
_REQUEST = re.compile(rf"^\s*{_LEAD}{_ASK}{_VERB}\b(?:\s+(?:that|this))?[\s:,-]*(?P<what>.+?)[\s.!?]*$",
                      re.IGNORECASE | re.DOTALL)  # fmt: skip


def memory_request(text: str) -> str | None:
    """What a "remember that ..." message asks the app to keep (the words after the request), or None when the
    message is not such a request. Only a short message that opens with the request counts: a question that
    mentions remembering is not one."""
    if len(text) > REQUEST_MAX_CHARS:
        return None
    found = _REQUEST.match(text)
    if found is None:
        return None
    what = found.group("what").strip()
    # A question ("remember the formula for speed?", "Remember me?") asks the tutor something; it keeps nothing.
    # Only a polite ask ("Can you remember that ...?") may end in a question mark, and what it asks to keep may not
    # itself hold one.
    asked = (found.group("ask") or "").strip() != ""
    if "?" in what or (text.rstrip().endswith("?") and not asked):
        return None
    return what if len(what) >= 3 else None


def declined(what: str, capture: CaptureFilter | None) -> bool:
    """The pre-check of an explicit request (MEM-10, PRV-04): anything the never-capture list covers (instructions
    to the tutor, secrets and quotes included) is declined with no job. Without a loaded filter nothing can be
    checked, so the request is declined: the filter is never skipped."""
    if capture is None:
        return True
    return capture.check(what).blocked


# --- the save-claim critic (PRM-17, LRN-04, MEM-23) ----------------------------------------------------------

# A claim is in the first person (I, I'll, I've, I'm going to, I will make sure to ...) with a keeping verb ("remember",
# "keep that in mind", "bear that in mind", "log that", "note it", "save it", "hold on to that", "take note", "won't
# forget"), or a bare "noted that", "saved it", "consider it remembered", "in my notes", "next time ... I'll know".
# A sentence that tells the student to remember or note something is teaching, not a claim: "Remember that ...",
# "Write this in your notes: ...", "Take note of the units".
_IT = r"(?:that|this|it|them|those)"
_FIRST = (r"\bi(?:'ll|'ve|'m|'d|\s+will|\s+shall|\s+have|\s+had|\s+am|\s+would|\s+can)?"
          r"(?:\s+(?:going\s+to|be\s+sure\s+to|make\s+sure\s+to|definitely|also|always|now|just|then|be|have|"
          r"surely|certainly|already|gladly|happily|totally))*\s+")  # fmt: skip
_KEEP = (rf"(?:remember\w*|recall(?:ed)?|not(?:e|ed)\s+(?:{_IT}|down)|(?:take|taken|took|make|made)\s+(?:a\s+)?note"
         rf"|log(?:ged)?(?:\s+{_IT})?|sav(?:e|ed)\s+{_IT}|stor(?:e|ed)\s+{_IT}|record(?:ed)?\s+{_IT}"
         rf"|(?:keep|kept|bear|borne|keeping|bearing)\s+{_IT}\s+in\s+(?:mind|my\s+memory|memory|my\s+head)"
         rf"|hold(?:ing)?\s+on\s+to\s+{_IT}|held\s+on\s+to\s+{_IT}|(?:write|wrote|written)\s+{_IT}\s+down"
         rf"|add(?:ed)?\s+{_IT}\s+to\s+(?:my|your)\s+(?:notes|memory|records|file)|won't\s+forget|will\s+not\s+forget"
         rf"|not\s+forget|never\s+forget|memori[sz]e(?:d)?|file(?:d)?\s+{_IT}\s+away)")  # fmt: skip
_SAVE_CLAIM = re.compile(
    rf"{_FIRST}{_KEEP}\b"
    rf"|\b(?:noted|saved|stored|logged|recorded|filed)\s+(?:{_IT}|for\s+next\s+time|for\s+later|for\s+the\s+future)"
    rf"|\bconsider\s+{_IT}\s+(?:remembered|noted|saved|stored|logged|recorded|kept)"
    rf"|\b(?:in|to|into)\s+my\s+(?:notes|memory|records)\b|\bgot\s+{_IT}\s+(?:saved|noted|logged|stored)"
    rf"|\bnext\s+time\b.*\bi(?:'ll|\s+will)\s+(?:know|remember|recall)|\bi(?:'ll|\s+will)\s+know\b.*\bnext\s+time"
    rf"|\bj'ai\s+not[eé]",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"[^.!?]+[.!?]*")
_QUOTE_MARKS = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def remove_save_claims(say: str) -> tuple[str, bool]:
    """Take every sentence that claims a save out of a reply. A model never saves anything (only the Reflector's
    checked notes are stored), so such a claim is false whatever the setting; the engine's `memory.saved` is the only
    announcement."""
    kept, removed = [], False
    for sentence in _SENTENCES.findall(say):
        if _SAVE_CLAIM.search(" ".join(sentence.translate(_QUOTE_MARKS).split())):
            removed = True
            continue
        kept.append(sentence)
    return ("".join(kept).strip() if removed else say), removed
