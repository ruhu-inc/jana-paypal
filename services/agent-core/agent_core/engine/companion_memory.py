"""Explicit, locally interpreted memory commands. Only committed writes are acknowledged as saves."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from agent_core.engine.memory_session import declined, memory_request
from agent_core.memory.policy import normalise
from agent_core.memory.queries import stored_notes
from agent_core.memory.write import compact_storage, correct_preference, forget

Operation = Literal["remember", "correct", "forget", "inspect"]


@dataclass(frozen=True)
class Command:
    operation: Operation
    words: str
    target: str | None = None


# These are explicit selections from the existing preference vocabulary, not model-inferred learning styles.
PREFERENCES: tuple[tuple[str, str, str, str], ...] = (
    (r"(?:a )?worked examples?(?: first| before practice)?", "start_style", "worked_example", "a worked example first"),
    (r"(?:a )?hints?(?: first| before practice)?", "start_style", "hint", "a hint first"),
    (r"short explanations?", "pace", "short", "short explanations"),
    (r"(?:normal|normal length) explanations?", "pace", "normal", "normal-length explanations"),
    (r"diagrams?", "representation", "diagram", "diagrams"),
    (r"analog(?:y|ies)", "representation", "analogy", "analogies"),
    (r"simpler numbers", "representation", "simpler_numbers", "simpler numbers"),
)


def preference(words: str) -> tuple[str, str, str] | None:
    clean = normalise(words)
    clean = re.sub(r"^(?:i (?:prefer|like)|my preference (?:is|for)|my preferred explanation is)\s+", "", clean)
    for pattern, key, value, label in PREFERENCES:
        if re.fullmatch(pattern, clean):
            return key, value, label
    return None


def parse(text: str) -> Command | None:
    wanted = memory_request(text)
    if wanted is not None:
        return Command("remember", wanted)
    clean = text.strip().rstrip(".!?")
    if re.fullmatch(r"(?:what do you remember about me|what have you saved about me|show my memories)", clean, re.I):
        return Command("inspect", "")
    matched = re.match(r"^(?:please\s+)?forget\s+(.+)$", clean, re.I | re.S)
    if matched:
        return Command("forget", matched[1].strip())
    matched = re.fullmatch(r'correct (?:my )?memory (mem_[0-9A-HJKMNP-TV-Z]{26}|"[^"\n]+") to (.+)', clean, re.I | re.S)
    if matched:
        return Command("correct", matched[2].strip(), matched[1].strip('"'))
    matched = re.match(r"^(?:actually[,\s]+|correct (?:my preference to|that to)\s+)(.+)$", clean, re.I | re.S)
    if matched and preference(matched[1]) is not None:
        return Command("correct", matched[1])
    if re.match(r"^(?:please\s+)?(?:remember that|save this|correct my memory|correct memory)\b", clean, re.I):
        # Unsupported/oversized commands are still handled locally; they never fall through to a cloud prompt.
        return Command("remember" if not clean.lower().startswith("correct") else "correct", clean)
    return None


async def handle(engine: Any, turn: Any, text: str, command: Command) -> Any:
    from agent_core.engine.companion import _finish, action_event

    context = engine.companion
    context.require_scope()
    context.egress = {"local_only": True}
    memory = engine.memory
    source = memory.source() if memory is not None else None

    def reply(words: str, *, inspect: bool = False) -> Any:
        actions = [action_event("open_knows", {})] if inspect else []
        return _finish(engine, turn, words, actions, user=text, stop="completed", kind="memory_command")

    if source is None:
        return reply("Your saved memory is not available in this session.")
    conn = source.conn
    picked = preference(command.words) if command.target is None else None
    if command.operation == "inspect":
        rows = conn.execute("SELECT key,value FROM preferences ORDER BY key").fetchall()
        parts = [f"{key.replace('_', ' ')}: {json.loads(value).get('override', '')}" for key, value in rows]
        parts += [n.text for n in stored_notes(conn) if n.status == "active"]
        return reply(
            ("Saved learning memory: " + "; ".join(parts))[:550]
            if parts
            else "You have no saved learning preferences or notes.",
            inspect=True,
        )
    if command.operation == "forget":
        if picked is not None:
            key, value, _ = picked
            row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
            if row is None or json.loads(row[0]).get("override") != value:
                return reply("That preference is not currently saved. Inspect the saved items here.", inspect=True)
            correct_preference(source.learner, key, None, "student")
            context.memory_changed()
            memory.refresh()
            return reply("I've removed that saved preference. The original conversation remains in your history.")
        wanted = command.words.removeprefix("memory ").strip()
        matches = [
            n
            for n in stored_notes(conn)
            if n.status == "active" and (wanted == "mem_" + n.note_id or normalise(wanted) == normalise(n.text))
        ]
        if len(matches) != 1:
            return reply(
                "Which item should I forget? Choose it in What your tutor knows, or give its exact text.", inspect=True
            )
        note = matches[0]
        forgotten = forget(source.learner, note.note_id, "student")
        if forgotten is None:
            return reply("That saved item has already changed. Please inspect it again.", inspect=True)
        compact_storage(conn)
        context.note_forgotten(note.note_id)
        context.memory_changed()
        memory.refresh()
        return reply("I've forgotten that saved note. The original conversation remains in your history.")
    if not memory.saving_on():
        return reply("Memory saving is off. You can turn it on in Settings before asking me to save a preference.")
    if declined(command.words, source.capture):
        return reply("I can't save that as learning memory.")
    if picked is not None:
        key, value, label = picked
        row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
        if row is not None and json.loads(row[0]).get("override") == value:
            return reply(f"Your preference for {label} is already saved.")
        if not correct_preference(source.learner, key, value, "student"):
            return reply("That preference could not be saved.")
        context.memory_changed()
        memory.refresh()
        return reply(f"I've {'updated' if row is not None else 'saved'} your preference for {label}.")
    target = None
    if command.operation == "correct":
        matches = [
            n
            for n in stored_notes(conn)
            if n.status == "active"
            and command.target is not None
            and (command.target == "mem_" + n.note_id or normalise(command.target) == normalise(n.text))
        ]
        if len(matches) != 1:
            return reply(
                'Which item should I correct? Use Correct memory "exact saved text" to your new preference.',
                inspect=True,
            )
        target = matches[0]
    _, candidates, _ = await engine.deps.router.candidates("reflector")
    context.require_scope()
    if not any(c.locality == "local" for c in candidates) or engine.on_memory_request is None:
        return reply("I couldn't save that note: it needs the local memory model, which is unavailable.")
    if target is not None:
        queued = context.correct_memory(turn.turn_id, command.words, target.note_id, target.last_event_id)
    else:
        queued = engine.on_memory_request(turn.turn_id, command.words)
    if queued is False:
        return reply("That memory request could not be queued. Nothing has been saved.")
    return reply("Your request is queued for a local memory check. It isn't saved yet; the app will report the result.")
