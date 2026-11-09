"""Speech-linked pointing: the cue markers of an accepted spoken form, resolved to ids on screen (VOC-10, VSL-45).

The Coach marks its spoken form with `<point at="REF"/>` (point at REF from the next word on) and
`<write at="REF">words</write>` (the words that cite a new board line, D103 write cues). This module never accepts a
candidate: it reads the spoken form of a turn Finalise already accepted, after its board and visual operations were
applied, and turns each marker into a cue with a character offset into the exact text sent to speech. A REF resolves
to exactly one element on screen or to nothing:

- a board step: the `stp_` id, or the step's number as tutor-context lists it (`3` or `board 3`, the integer ref of
  MEM-08), on the accepted board; a tutor line that is unchecked or a draft is never pointed at (UXS-64, AGT-10);
- a part of a visual shown for this problem: `vis_<id>:<part>` (the UXS-58 form), or a bare part id when exactly one
  shown visual has that part;
- a place on the student's own photo (JM-D10, kind `photo_region`): `cap_<id>:rgn_<n>`, or `photo 3` (where board line
  3 is on the photo). A board line read from the photo resolves to its place on the photo, so "this line" lights the
  student's own handwriting, outlined by SAM 3.1.

Anything else (unknown, no longer shown, matching several elements) draws nothing and is reported as a miss with a
reason code (VSL-45); the spoken words already name the place, so nothing the student needs is lost. A write cue is
only for a tutor line this turn added; on any other line it is kept as a point. Cue timing is the client's job: it
fires each cue from actual playback progress (VOC-51). Markers never reach speech, a log or a trace.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Any

from agent_core.engine.verbaliser import verbalise

# One marker: an opening point (self-closing), an opening write, or a closing write.
MARKER = re.compile(r'<point\s+at="([^"<>]{1,80})"\s*/>|<write\s+at="([^"<>]{1,80})"\s*>|</write\s*>')
STEP_ID = re.compile(r"stp_[0-9A-HJKMNP-TV-Z]{26}")
STEP_NUMBER = re.compile(r"(?:board\s+|step\s+|line\s+)?([1-9][0-9]?)", re.I)
VISUAL_PART = re.compile(r"(vis_[0-9A-HJKMNP-TV-Z]{26}):([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)")
PHOTO_REGION = re.compile(r"cap_[0-9A-HJKMNP-TV-Z]{26}:rgn_[0-9]{1,3}")
PHOTO_LINE = re.compile(r"photo(?:\s+line)?[\s:]+([1-9][0-9]?)", re.I)
PART_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*")
# A skill on the concept map (UXS-22 map view): `kc:<kc_id>`; the client draws it only when the map is on screen.
KC_NODE = re.compile(r"kc:(kc\.(?:mth|phy|chm|bio|acc)\.(?:o|a)\.[a-z0-9_]+\.[a-z0-9_]+)")
MAX_CUES = 50  # `voice.caption` carries at most 50 cues (events contract)
UNPOINTABLE = ("unchecked", "draft")  # tutor lines whose values are not verified (AGT-10, UXS-64)


def strip_markers(text: str) -> str:
    """The text as it is heard or gated: markers removed, spacing kept single."""
    return re.sub(r"[ \t]{2,}", " ", MARKER.sub(" ", text)).strip()


@dataclass(frozen=True)
class Shown:
    """One visual on screen for the current problem: its id and its part ids (VSL-19)."""

    visual_id: str
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Targets:
    """What a cue may address now: the accepted board (step ids in order, their owner and status) and the visuals
    shown for this problem. `new_steps` are the tutor lines the accepted turn itself added (write cues)."""

    steps: tuple[tuple[str, str, str], ...] = ()  # (step_id, owner, status) in board order
    visuals: tuple[Shown, ...] = ()
    new_steps: frozenset[str] = frozenset()
    # (step_id, cap_<id>:rgn_<n>) for lines read from a photo; a sketch's parts appear as (region, region)
    photo_lines: tuple[tuple[str, str], ...] = ()
    # Skills in play, which a `kc:` cue may point at on the concept map: an open offer's KC and the earlier skill it
    # offers (a root gap's), and an open detour's.
    kcs: frozenset[str] = frozenset()

    @classmethod
    def of(cls, state: Any, new_steps: frozenset[str] = frozenset()) -> Targets:
        steps = tuple((s.step_id, s.owner, s.status) for s in state.board.steps)
        visuals = tuple(
            Shown(str(v["visual_id"]), tuple(str(p) for p in v.get("parts", ())))
            for v in getattr(state, "shown_visuals", ())
        )
        on_board = {s[0] for s in steps}
        photo = tuple((k, v) for k, v in getattr(state, "photo_lines", {}).items() if k in on_board)
        marks = tuple((v, v) for v in getattr(state, "photo_marks", ()))  # a sketch's parts: no board line of their own
        offered = [v for o in (getattr(state, "offers", None) or {}).values() if isinstance(o, dict)
                   for v in (o.get("kc_id"), o.get("prerequisite_kc_id"))]  # fmt: skip
        detour = getattr(state, "detour", None) or {}
        kcs = frozenset(str(k) for k in [*offered, detour.get("kc_id") if isinstance(detour, dict) else None] if k)
        return cls(steps, visuals, new_steps, photo + marks, kcs)


@dataclass(frozen=True)
class Cue:
    char_offset: int
    target: str
    action: str = "point"  # "point" or "write" (D103)
    end_offset: int | None = None

    def event(self, *, actions: bool) -> dict[str, Any]:
        """The cue as `voice.caption` carries it. `actions`: the events contract in force knows `action` and
        `end_offset` (API 1.9); without them a write cue is sent as a point, which every version accepts."""
        out: dict[str, Any] = {"char_offset": self.char_offset, "target": self.target}
        if actions and self.action == "write" and self.end_offset is not None:
            out.update(action="write", end_offset=self.end_offset)
        return out


@dataclass(frozen=True)
class Spoken:
    """The text sent to speech, its cues in offset order, and the misses (reason codes only, never text)."""

    text: str
    cues: tuple[Cue, ...] = ()
    misses: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()


@dataclass
class _Builder:
    text: str = ""
    fallbacks: list[str] = field(default_factory=list)

    def add(self, raw: str) -> int | None:
        """Append one verbalised segment; returns where its first character landed, or None when it was empty."""
        speech = verbalise(raw)
        self.fallbacks.extend(speech.fallbacks)
        piece = speech.text.strip()
        if not piece:
            return None
        if self.text and not self.text.endswith(" ") and piece[0] not in ".,;:!?":
            self.text += " "
        start = len(self.text)
        self.text += piece
        # A cue points from the next word: punctuation after the marker ("Right <point/>, the ...") is skipped.
        word = re.search(r"\w", piece)
        return start + (word.start() if word else 0)


def _resolve(ref: str, targets: Targets) -> tuple[str | None, str | None]:
    """(target, None) for exactly one element on screen, else (None, reason)."""
    ref = ref.strip()
    photo = dict(targets.photo_lines)
    if PHOTO_REGION.fullmatch(ref):
        return (ref, None) if ref in photo.values() else (None, "photo_region_not_shown")
    if (line := PHOTO_LINE.fullmatch(ref)) is not None:
        index = int(line[1])
        if not 1 <= index <= len(targets.steps):
            return None, "step_not_on_board"
        region = photo.get(targets.steps[index - 1][0])
        return (region, None) if region is not None else (None, "photo_region_not_shown")
    step = None
    if STEP_ID.fullmatch(ref):
        step = next((s for s in targets.steps if s[0] == ref), None)
        if step is None:
            return None, "step_not_on_board"
    elif (number := STEP_NUMBER.fullmatch(ref)) is not None:
        index = int(number[1])
        if not 1 <= index <= len(targets.steps):
            return None, "step_not_on_board"
        step = targets.steps[index - 1]
    if step is not None:
        if step[1] == "tutor" and step[2] in UNPOINTABLE:
            return None, "step_unchecked"
        return photo.get(step[0], step[0]), None
    if (node := KC_NODE.fullmatch(ref)) is not None:
        return (ref, None) if node[1] in targets.kcs else (None, "kc_not_in_play")
    if (pair := VISUAL_PART.fullmatch(ref)) is not None:
        shown = next((v for v in targets.visuals if v.visual_id == pair[1]), None)
        if shown is None:
            return None, "visual_not_shown"
        return (ref, None) if pair[2] in shown.parts else (None, "part_unknown")
    if PART_ID.fullmatch(ref):
        having = [v for v in targets.visuals if ref in v.parts]
        if len(having) == 1:
            return f"{having[0].visual_id}:{ref}", None
        return None, "part_ambiguous" if having else "part_unknown"
    return None, "target_unreadable"


def resolve(spoken: str, targets: Targets) -> Spoken:
    """The accepted spoken form (with markers) as speech text and cues; markers that resolve to nothing are misses.

    Each text segment between markers is read by the verbaliser on its own (VOC-08), so the offsets index the exact
    text the sidecar speaks. Markers inside a write span are ignored (one cue at a time), and an unclosed write ends
    at the end of the text."""
    out = _Builder()
    cues: list[Cue] = []
    misses: list[str] = []
    waiting: list[tuple[str, str]] = []  # markers placed before the next word: (action, target)
    writing: tuple[str, int] | None = None  # an open write: (target, index into cues)
    cursor = 0

    def place(start: int | None) -> None:
        nonlocal writing
        if start is None:
            return
        for action, target in waiting:
            if len(cues) >= MAX_CUES:
                misses.append("cue_limit")
                continue
            cues.append(Cue(start, target, action))
            if action == "write":
                writing = (target, len(cues) - 1)
        waiting.clear()

    def close() -> None:
        nonlocal writing
        if writing is not None:
            target, at = writing
            begun = cues[at]
            cues[at] = Cue(begun.char_offset, target, "write", max(begun.char_offset, len(out.text)))
            writing = None

    for match in MARKER.finditer(spoken):
        place(out.add(spoken[cursor : match.start()]))
        cursor = match.end()
        point_ref, write_ref = match[1], match[2]
        if point_ref is None and write_ref is None:  # </write>
            if waiting and waiting[-1][0] == "write":
                waiting.pop()  # an empty write span cites no words
            close()
            continue
        if writing is not None or any(action == "write" for action, _ in waiting):
            misses.append("cue_inside_write")
            continue
        target, reason = _resolve(point_ref or write_ref or "", targets)
        if target is None:
            misses.append(reason or "target_unreadable")
            continue
        action = "point"
        if write_ref is not None:
            if target in targets.new_steps:
                action = "write"
            else:
                misses.append("write_not_new_line")
        waiting.append((action, target))
    place(out.add(spoken[cursor:]))
    close()
    for action, target in waiting:  # markers after the last word point at the end of the text
        if len(cues) < MAX_CUES and action == "point":
            cues.append(Cue(len(out.text), target))
    end = len(out.text)
    fixed = tuple(
        Cue(min(c.char_offset, end), c.target, c.action, None if c.end_offset is None else min(c.end_offset, end))
        for c in cues
    )
    return Spoken(out.text, tuple(sorted(fixed, key=lambda c: c.char_offset)), tuple(misses), tuple(out.fallbacks))


def verbalise_keeping_markers(text: str) -> str:
    """Read the words of a spoken form with the verbaliser (VOC-08) and keep its markers in place, so the accepted
    turn's `say_spoken` still carries them for `resolve` (the contract strips them only before speech)."""
    pieces: list[str] = []
    cursor = 0
    for match in MARKER.finditer(text):
        pieces.append(verbalise(text[cursor : match.start()]).text)
        pieces.append(match[0])
        cursor = match.end()
    pieces.append(verbalise(text[cursor:]).text)
    joined = " ".join(p for p in pieces if p)
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s{2,}", " ", joined)).strip()


@functools.cache
def cue_actions_supported() -> bool:
    """Whether the bundled events contract carries write cues (`action`, `end_offset`; API 1.9, D103).

    HAND-WRITTEN BINDING (point-demo, pending the D103 re-pin): the bundled schemas are generated from the pinned
    specification. Until it is re-pinned to D103 the cue has only `char_offset` and `target`, so write cues travel as
    points. After the re-pin this returns True and write cues are sent as such; nothing else changes."""
    from agent_core.contracts import document

    schema = document("api/events")
    cue = schema["$defs"]["ev_voice_caption"]["properties"]["cues"]["items"]["properties"]
    return "action" in cue and "end_offset" in cue


_UNIT = re.compile(r"[^.?!]+[.?!]+[\"')\]]*|[^.?!]+$")


def units(text: str) -> list[tuple[int, int]]:
    """The teaching units of a spoken text as (start, end) character spans: its sentences (VOC-52)."""
    spans = []
    for match in _UNIT.finditer(text):
        start = match.start() + len(match[0]) - len(match[0].lstrip())
        end = match.start() + len(match[0].rstrip())
        if end > start:
            spans.append((start, end))
    return spans


def first_unheard(spoken: str, heard: str) -> str | None:
    """The first unit of `spoken` the student did not hear in full, given the heard prefix; None when all was heard."""
    reached = len(heard.rstrip()) if spoken.startswith(heard.rstrip()) else 0
    for start, end in units(spoken):
        if end > reached:
            return spoken[start:end]
    return None
