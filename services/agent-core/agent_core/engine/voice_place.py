"""Where a voice conversation lives (VOC-53, D130): the open chats only.

`ask` is the Home box and its Ask anything answer view (STU-20, UXS-67); `study` is a study set's Chat tab (STU-21),
naming the open source, its revision and the open grant a private source needs (STU-19). A socket with no place serves
read-aloud and dictation only. The spoken forms here are built from accepted replies only: an Ask anything answer that
passed `compose`, a chat aid the study checker kept, or a fixed line; never model text that skipped a gate (VOC-33).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_core.engine.verbaliser import verbalise
from agent_core.engine.voice_bridge import VoiceProtocolError

SOURCE_ID = re.compile(r"src_[0-9A-HJKMNP-TV-Z]{26}")
# proposal: the most words a spoken open-chat answer says before pointing at the screen (VOC-53).
SPOKEN_WORDS = 80
REST_ON_SCREEN = "The rest is on screen."
GENERAL = "This is general knowledge, not checked against your syllabus or your material."
# The words the answer view shows for the same state (`ask.offline`, STU-20), so the voice and the screen agree.
OFFLINE = (
    "General questions need the online plan. You can still ask the tutor about your subjects or study your own "
    "material."
)
MATERIAL = "That looks like material. Open it in the study area to work with it."
NOT_ANSWERED = "Sorry, I could not answer that from your material. Try asking it another way."
FAILED = "Sorry, I could not answer that just now. Please try again."


@dataclass(frozen=True)
class Place:
    kind: str  # "ask", "study" or "tutor" (the tutor screen's own conversation, with Cloud voice on)
    source_id: str | None = None
    revision: int | None = None
    grant: str = ""  # the study source's open grant (x-study-open), held in memory only


def parse_place(control: Mapping[str, Any]) -> Place | None:
    """`{"type":"place","place":null}`, `{"type":"place","place":"ask"}`, `{"type":"place","place":"tutor"}` or
    `{"type":"place","place":"study","source_id":"src_…","revision":n,"open":"…"}` (`open` may be empty)."""
    kind = control.get("place")
    if kind is None and set(control) == {"type", "place"}:
        return None
    if kind == "ask" and set(control) == {"type", "place"}:
        return Place("ask")
    if kind == "tutor" and set(control) == {"type", "place"}:
        return Place("tutor")
    if (
        kind == "study"
        and set(control) == {"type", "place", "source_id", "revision", "open"}
        and isinstance(control["source_id"], str)
        and SOURCE_ID.fullmatch(control["source_id"])
        and type(control["revision"]) is int
        and control["revision"] >= 1
        and isinstance(control["open"], str)
        and len(control["open"]) <= 512
    ):
        return Place("study", control["source_id"], control["revision"], control["open"])
    raise VoiceProtocolError("invalid_place")


def _speakable(text: str) -> str:
    """The verbaliser's spoken form (maths read aloud, PED-26), with no character the bridge refuses to speak."""
    spoken = verbalise(text).text
    return " ".join(re.sub(r"[\\$^_{}<>`*#|]", " ", spoken).split())


def spoken_answer(texts: Iterable[str], *, general: bool = False) -> str:
    """The first sentences of an accepted answer, at most `SPOKEN_WORDS` words, then a pointer to the screen."""
    words: list[str] = []
    cut = False
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", _speakable(text)):
            parts = sentence.split()
            if not parts:
                continue
            if len(words) + len(parts) > SPOKEN_WORDS:
                if not words:  # one long first sentence: its first words, never nothing
                    words = parts[:SPOKEN_WORDS]
                cut = True
                break
            words.extend(parts)
        if cut:
            break
    spoken = " ".join(words)
    if spoken and spoken[-1] not in ".!?":
        spoken += "."
    if general:
        spoken += " " + GENERAL
    if cut:
        spoken += " " + REST_ON_SCREEN
    return spoken.strip()


def ask_spoken(reply: Mapping[str, Any]) -> str:
    """The spoken form of a `POST /v1/ask` reply (VOC-53): the answer, its check question in Teach me, or why not."""
    if reply.get("route") == "study":
        return MATERIAL
    state = reply.get("state")
    if state == "unavailable":
        return OFFLINE
    if state != "answered":
        return FAILED
    answer = reply["answer"]
    texts = [s["text"] for s in answer["answer"]]
    if answer.get("check_question"):
        texts.append(answer["check_question"])
    return spoken_answer(texts, general=bool(answer.get("general_knowledge")))


def aid_spoken(aid: Mapping[str, Any]) -> str:
    """The spoken form of a study chat aid: its kept, cited claims in order (STU-16)."""
    texts = [str(c.get("text", "")) for c in aid.get("claims", []) if isinstance(c, Mapping)]
    return spoken_answer(texts) or NOT_ANSWERED
