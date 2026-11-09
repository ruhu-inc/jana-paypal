"""How every reader of the learner log reads a re-marked verdict (ASM-23, CUR-36, D98): one rule, here, below every
layer that reads verdicts (the projections, the Reflector's input, session summaries, assessment help, research rows).

A re-mark appends a `verdict` naming the verdict it supersedes; the original is never edited. A reader takes a
superseded verdict, at its own place in the log, as the latest verdict that replaces it (following a chain of
re-marks), and skips the replacing verdicts where they stand. A `supersedes` counts only when it names an earlier,
unerased verdict of the same attempt in the same session; any other is an ordinary verdict.

Misconception evidence follows its verdict. Evidence a verdict gave (a `misconception_evidence` naming an attempt)
belongs to the marking that produced it: once an attempt is re-marked, the evidence recorded before its latest re-mark
is withdrawn, and the evidence recorded with that re-mark is read just after the verdict it re-marks, where the
corrected marking would have recorded it. A re-marked log therefore reads exactly as if the corrected verdicts and
their evidence had been recorded at the time, and a replay stays a function of the log alone (DAT-05).

A Reflector note written before a re-mark, whose marking evidence (the verdicts and misconception matches it cites)
came only from markings the re-mark replaced, is withdrawn with them (`note_withdrawn`); a note citing anything the
re-mark left standing, or written after it, stays.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from agent_core.store.events import StoredEvent

EVIDENCE = "misconception_evidence"


def follows_a_verdict(event: StoredEvent) -> bool:
    """Whether an event is what the rule reads: a verdict, or misconception evidence a verdict gave."""
    return event.type == "verdict" or (event.type == EVIDENCE and bool(event.payload.get("attempt_id")))


@dataclass(frozen=True)
class Supersession:
    replaced_by: dict[str, StoredEvent] = field(default_factory=dict)  # superseded verdict id -> latest replacement
    replacing: frozenset[str] = frozenset()  # the replacing verdicts' ids, skipped where they stand
    withdrawn: frozenset[str] = frozenset()  # evidence event ids of an earlier marking of a re-marked attempt
    moved: frozenset[str] = frozenset()  # evidence event ids recorded with a re-mark, read after their verdict
    after: dict[str, tuple[StoredEvent, ...]] = field(default_factory=dict)  # superseded verdict id -> moved evidence
    marking: frozenset[str] = frozenset()  # every verdict and verdict-evidence event id in the log
    stale: dict[str, tuple[str, str, int]] = field(default_factory=dict)  # event id -> where the re-mark replacing it

    @staticmethod
    def of(events: Iterable[StoredEvent]) -> Supersession:
        """From unerased events in merge order (verdicts and verdict evidence are picked out)."""
        verdicts: dict[str, StoredEvent] = {}
        direct: dict[str, StoredEvent] = {}
        evidence: list[StoredEvent] = []
        for event in events:
            if event.erased or not follows_a_verdict(event):
                continue
            if event.type == EVIDENCE:
                evidence.append(event)
                continue
            target = verdicts.get(str(event.payload.get("supersedes")))
            if (
                target is not None
                and target.session_id == event.session_id
                and target.payload["attempt_id"] == event.payload["attempt_id"]
            ):
                direct[target.payload["verdict_id"]] = event  # a later re-mark of the same verdict wins
            verdicts[event.payload["verdict_id"]] = event
        if not direct:
            return Supersession()
        latest: dict[str, StoredEvent] = {}
        for verdict_id, final in direct.items():
            seen = {verdict_id}
            while (step := direct.get(final.payload["verdict_id"])) is not None and final.payload[
                "verdict_id"
            ] not in seen:
                seen.add(final.payload["verdict_id"])
                final = step
            latest[verdict_id] = final
        replacing = frozenset(e.payload["verdict_id"] for e in direct.values())
        # Per re-marked attempt: where its latest re-mark stands, and the last verdict it re-marks (the anchor).
        cut: dict[str, tuple[str, str, int]] = {}
        anchor: dict[str, StoredEvent] = {}
        for verdict_id, final in latest.items():
            if verdict_id in replacing:
                continue
            original = verdicts[verdict_id]
            attempt = str(original.payload["attempt_id"])
            cut[attempt] = max(cut.get(attempt, final.order), final.order)
            if attempt not in anchor or anchor[attempt].order < original.order:
                anchor[attempt] = original
        withdrawn: set[str] = set()
        moved: set[str] = set()
        after: dict[str, list[StoredEvent]] = {}
        stale = {verdicts[verdict_id].event_id: final.order for verdict_id, final in latest.items()}
        for event in evidence:
            attempt = str(event.payload["attempt_id"])
            if attempt not in cut:
                continue
            if event.order < cut[attempt]:
                withdrawn.add(event.event_id)
                stale[event.event_id] = cut[attempt]
                continue
            moved.add(event.event_id)
            at = anchor[attempt]  # read at the anchor's place: same clock, day and folding position
            placed = replace(event, seq=at.seq, hlc=at.hlc, device_id=at.device_id, origin_seq=at.origin_seq)
            after.setdefault(at.payload["verdict_id"], []).append(placed)
        marking = frozenset([*(v.event_id for v in verdicts.values()), *(e.event_id for e in evidence)])
        return Supersession(latest, replacing, frozenset(withdrawn), frozenset(moved),
                            {k: tuple(v) for k, v in after.items()}, marking, stale)  # fmt: skip

    def note_withdrawn(self, note: StoredEvent) -> bool:
        """Whether a `note_written` rests only on markings a later re-mark replaced: it cites at least one verdict
        or misconception match, and each it cites was replaced after the note was written."""
        cited = [s for s in note.payload.get("source_event_ids") or [] if s in self.marking]
        return bool(cited) and all(s in self.stale and note.order < self.stale[s] for s in cited)

    def read_all(self, event: StoredEvent) -> list[StoredEvent]:
        """The events a reader takes in place of `event`: a superseded verdict carries its latest replacement's
        payload and is followed by the evidence recorded with that re-mark; a replacing verdict and withdrawn or
        moved evidence are read as nothing at their own place; anything else is itself."""
        if not self.replaced_by:
            return [event]
        if event.type == EVIDENCE:
            return [] if event.event_id in self.withdrawn or event.event_id in self.moved else [event]
        if event.type != "verdict":
            return [event]
        verdict_id = str(event.payload.get("verdict_id"))
        if verdict_id in self.replacing:
            return []
        replacement = self.replaced_by.get(verdict_id)
        if replacement is None:
            return [event]
        return [replace(event, payload=replacement.payload), *self.after.get(verdict_id, ())]

    def read(self, event: StoredEvent) -> StoredEvent | None:
        """The event itself as a reader takes it (the evidence a re-mark moved after it is left out): None for a
        replacing verdict or withdrawn or moved evidence."""
        found = self.read_all(event)
        return found[0] if found and found[0].event_id == event.event_id else None


def read_log(events: Iterable[StoredEvent]) -> list[StoredEvent]:
    """Unerased events in merge order, as every reader takes them (see the module docstring). A reader that holds a
    session's events, or the whole log, passes them all: supersession never crosses a session, and evidence follows
    its attempt's verdicts."""
    kept = [e for e in events if not e.erased]
    remarks = Supersession.of(kept)
    return [read for event in kept for read in remarks.read_all(event)]
