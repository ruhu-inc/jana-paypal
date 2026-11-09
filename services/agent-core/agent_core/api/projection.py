"""The session's normalised state, folded from its events (API-17 snapshot; the client store of UIA-01 folds the same
events the same way, so a client that loads a snapshot and then the stream ends equal to one that saw every event).

`apply` is pure in effect and idempotent by sequence: an envelope at or below the state's cursor is ignored (UIA-02).
Messages: the student's message is recorded from the command (its content is not in the event union), the tutor's
message collects the turn's parts. Parts are keyed by id with their content so far; boards hold steps by id with an
order; visuals by id; open card requests until answered or cancelled; the active turn with its latest status."""

from __future__ import annotations

import copy
from typing import Any


def empty(session: dict[str, Any], epoch: str) -> dict[str, Any]:
    return {
        "session": session,
        "cursor": {"epoch": epoch, "seq": 0},
        "messages": [],
        "parts": {},
        "boards": {},
        "visuals": {},
        "open_requests": [],
        "active_turn": None,
    }


def student_message(state: dict[str, Any], client_message_id: str, content: list[dict[str, Any]]) -> None:
    """The student's message as the sending client inserts it optimistically (UIA-05): keyed by its client id, with
    the message id, turn and time filled in by `message.accepted`. Its content never travels as an event."""
    state["messages"].append({"message_id": None, "role": "student", "turn_id": None,
                              "client_message_id": client_message_id, "content": content, "ts": None})  # fmt: skip


def _tutor(state: dict[str, Any], turn_id: str | None) -> dict[str, Any] | None:
    for message in reversed(state["messages"]):
        if message["role"] == "tutor" and message["turn_id"] == turn_id:
            found: dict[str, Any] = message
            return found
    return None


def _accept(state: dict[str, Any], event: dict[str, Any], ts: int) -> None:
    """Give the optimistic message its ids and move it just before its turn's tutor message, so the order is the
    server's even when the client sent it while still behind on an earlier turn."""
    messages = state["messages"]
    found = next((m for m in messages if m["role"] == "student"
                  and m.get("client_message_id") == event["client_message_id"]), None)  # fmt: skip
    if found is None:
        return
    found.update(message_id=event["message_id"], turn_id=event["turn_id"], ts=ts)
    messages.remove(found)
    at = next((i for i, m in enumerate(messages) if m["role"] == "tutor" and m["turn_id"] == event["turn_id"]),
              len(messages))  # fmt: skip
    messages.insert(at, found)


def _board(state: dict[str, Any], board_id: str) -> dict[str, Any]:
    boards = state["boards"]
    if board_id not in boards:
        boards[board_id] = {"board_id": board_id, "part_id": None, "problem": None, "knowns": [], "unknowns": [],
                            "conventions": [], "steps": {}, "order": [], "awaiting": None,
                            "highlight": None}  # fmt: skip
    board: dict[str, Any] = boards[board_id]
    return board


def apply(state: dict[str, Any], envelope: dict[str, Any]) -> bool:
    """Fold one envelope into the state. Returns False if it was already applied (seq at or below the cursor)."""
    cursor = state["cursor"]
    if envelope["epoch"] == cursor["epoch"] and envelope["seq"] <= cursor["seq"]:
        return False
    cursor["epoch"], cursor["seq"] = envelope["epoch"], envelope["seq"]
    event, turn_id, ts = envelope["event"], envelope["turn_id"], envelope["ts"]
    kind = event["type"]
    parts = state["parts"]
    if kind.startswith("board.") or kind == "visual.await":
        # Preserve the actual board event sequence for replay after a restart.
        part = next(
            (
                p
                for p in reversed(list(parts.values()))
                if p["kind"] == "board" and p["turn_id"] == turn_id and p["status"] in ("started", "streaming")
            ),
            None,
        )
        if part is not None:
            part.setdefault("board_events", []).append(copy.deepcopy(event))
    if kind == "turn.start":
        state["active_turn"] = {"turn_id": event["turn_id"], "status": {}}  # the latest status event, once one arrives
        state["messages"].append({
            "message_id": None, "role": "tutor", "turn_id": event["turn_id"], "part_ids": [], "status": "streaming",
            "stop_reason": None, "degraded": False, "degraded_reason": None, "error": None, "ts": ts,
            "intent": event["intent"], "mode": event["mode"], "user_message_id": event["user_message_id"],
            "client_message_id": event["client_message_id"], "phase": None, "hint_rung": None, "usage": None,
            "elapsed_ms": None, "verification": None,
        })  # fmt: skip
    elif kind == "message.accepted":
        _accept(state, event, ts)
    elif kind == "status":
        status = {k: v for k, v in event.items() if k != "type"}
        if state["active_turn"] is not None:
            state["active_turn"]["status"] = status
        tutor = _tutor(state, turn_id)
        if tutor is not None:
            tutor["phase"] = status
    elif kind == "part.start":
        parts[event["part_id"]] = {"part_id": event["part_id"], "turn_id": turn_id, "kind": event["kind"],
                                   "index": event["index"], "status": "started", "text": ""}  # fmt: skip
        tutor = _tutor(state, turn_id)
        if tutor is not None:
            tutor["part_ids"].append(event["part_id"])
    elif kind == "text.delta":
        part = parts.get(event["part_id"])
        if part is not None and part["status"] in ("started", "streaming"):
            part["text"] += event["delta"]
            part["status"] = "streaming"
    elif kind == "text.reset":
        part = parts.get(event["part_id"])
        if part is not None and part["text"] == "":
            part["status"] = "started"  # only an empty placeholder resets; accepted text is never cleared (API-08)
    elif kind == "part.end":
        part = parts.get(event["part_id"])
        if part is not None:
            part["status"] = event["status"]
            part["duration_ms"] = event["duration_ms"]
    elif kind == "tool.state":
        part = parts.get(event["part_id"])
        if part is not None:
            part.setdefault("tools", {})[event["call_id"]] = {
                "call_id": event["call_id"], "name": event["name"], "render_class": event["render_class"],
                "state": event["state"],
            }  # fmt: skip
            if part["status"] == "started":
                part["status"] = "streaming"
    elif kind == "board.reset":
        board = _board(state, event["board_id"])
        board.update(part_id=event["part_id"], problem=event["problem"], knowns=[], unknowns=[], conventions=[],
                     steps={}, order=[], awaiting=None, highlight=None)  # fmt: skip
        board.pop("annotation", None)
        board.pop("manipulation", None)
    elif kind == "board.knowns":
        board = _board(state, event["board_id"])
        board.update(knowns=event["knowns"], unknowns=event["unknowns"], conventions=event["conventions"])
    elif kind == "board.step":
        board = _board(state, event["board_id"])
        step = {k: v for k, v in event.items() if k not in ("type", "board_id")}
        if event["step_id"] not in board["steps"]:
            board.pop("annotation", None)
        if (board.get("manipulation") or {}).get("step_id") == event["step_id"] and event["status"] != "awaiting":
            board.pop("manipulation", None)
        board["steps"][event["step_id"]] = step
        board["order"] = sorted(board["steps"], key=lambda s: (board["steps"][s]["index"], s))
        if board["awaiting"] is not None and board["awaiting"]["step_id"] == event["step_id"] \
                and event["status"] != "awaiting":  # fmt: skip
            board["awaiting"] = None
    elif kind == "board.highlight":
        _board(state, event["board_id"])["highlight"] = {"step_ids": event["step_ids"], "ttl_ms": event["ttl_ms"],
                                                          "ts": ts}  # fmt: skip
        if event.get("spotlight"):
            _board(state, event["board_id"])["highlight"]["spotlight"] = True
    elif kind == "board.annotate":
        _board(state, event["board_id"])["annotation"] = {
            k: copy.deepcopy(event.get(k)) for k in ("annotation_id", "mark", "target", "to", "note")
        }
    elif kind == "visual.await":
        _board(state, event["board_id"])["manipulation"] = {
            k: copy.deepcopy(event[k]) for k in ("step_id", "visual_id", "part_ids", "prompt")
        }
    elif kind == "board.await":
        _board(state, event["board_id"])["awaiting"] = {"step_id": event["step_id"], "prompt": event["prompt"],
                                                         "input_kind": event["input_kind"]}  # fmt: skip
    elif kind == "visual.spec":
        state["visuals"][event["visual_id"]] = {"visual_id": event["visual_id"], "part_id": event["part_id"],
                                                "spec": event["spec"], "step_index": 0, "focus": None}  # fmt: skip
        part = parts.get(event["part_id"])
        if part is not None:
            part["visual_id"] = event["visual_id"]
    elif kind == "visual.sketch":
        part = parts.get(event["part_id"])
        if part is not None:
            part["sketch"] = copy.deepcopy(event["sketch"])
    elif kind == "visual.step":
        visual = state["visuals"].get(event["visual_id"])
        if visual is not None:
            visual["step_index"] = event["step_index"]
    elif kind == "visual.focus":
        visual = state["visuals"].get(event["visual_id"])
        if visual is not None:
            visual["focus"] = {"part_ids": event["part_ids"], "ttl_ms": event["ttl_ms"], "ts": ts}
    elif kind == "card.open":
        state["open_requests"] = [r for r in state["open_requests"] if r["request_id"] != event["request_id"]]
        about = {"about": event["about"]} if event.get("about") else {}
        state["open_requests"].append({"request_id": event["request_id"], "part_id": event["part_id"],
                                       "card": event["card"]})  # fmt: skip
        part = parts.get(event["part_id"])
        if part is not None:
            part["card"] = {"request_id": event["request_id"], "card": event["card"], "state": "open", **about}
    elif kind in ("card.answered", "card.cancel"):
        for request in state["open_requests"]:
            if request["request_id"] == event["request_id"]:
                part = parts.get(request["part_id"])
                if part is not None and "card" in part:
                    part["card"]["state"] = "answered" if kind == "card.answered" else "cancelled"
                    if kind == "card.answered":
                        part["card"]["answer_summary"] = event["answer_summary"]
                        part["card"]["answered_ts"] = event["answered_ts"]
        state["open_requests"] = [r for r in state["open_requests"] if r["request_id"] != event["request_id"]]
    elif kind == "notice":
        part_id = event["notice_id"]
        parts[part_id] = {"part_id": part_id, "turn_id": turn_id, "kind": "notice", "index": -1, "status": "complete",
                          "text": "", "notice": {k: v for k, v in event.items() if k != "type"}}  # fmt: skip
    elif kind == "error":
        tutor = _tutor(state, turn_id)
        if tutor is not None:
            tutor["error"] = {k: v for k, v in event.items() if k != "type"}
    elif kind == "session.updated":
        session = state["session"]
        for key in ("title", "mode", "subject", "topic_id"):
            if key in event:
                session[key] = event[key]
        session["updated_ts"] = ts
        tutor = _tutor(state, turn_id)
        if "hint_rung" in event and tutor is not None:
            tutor["hint_rung"] = event["hint_rung"]  # the Session record has no rung; the turn that set it keeps it
    elif kind == "turn.end":
        tutor = _tutor(state, turn_id)
        if tutor is not None:
            tutor.update(message_id=event["persisted"]["message_id"], status=event["status"],
                         stop_reason=event["stop_reason"], degraded=event["degraded"],
                         degraded_reason=event["degraded_reason"], usage=event["usage"],
                         elapsed_ms=event["elapsed_ms"], verification=event["verification"])  # fmt: skip
        state["active_turn"] = None
        state["session"]["updated_ts"] = ts
    elif kind == "companion.action":
        # A durable button of the companion's thread (SPEC §19.3.3): kept by id in the snapshot, so it survives a
        # reconnect and a restart (the checkpoint holds this state).
        state.setdefault("actions", {})[event["action_id"]] = dict(event)
    elif kind in ("memory.saved", "memory.forgotten", "progress.update"):
        # The Snapshot contract has no place for these (G94): a saved note's announcement and Undo live in the
        # client's store while it is connected, and the note itself on the "What your tutor knows" page. Keeping the
        # note's text out of the snapshot also keeps it out of the checkpoint, so a forget leaves no copy (LRN-37).
        pass
    # Unknown or debug-only events (reasoning.delta, future types) change nothing (API-06).
    return True


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """A deep copy, safe to serialise while the engine keeps folding events."""
    return copy.deepcopy(state)


def interrupt(state: dict[str, Any]) -> None:
    """After a restart: in-flight parts end `interrupted` with their text kept, and no turn is active (API-17). Open
    card requests stay: they are answerable after a restart (API-14)."""
    for part in state["parts"].values():
        if part["status"] in ("started", "streaming"):
            part["status"] = "interrupted"
    for message in state["messages"]:
        if message["role"] == "tutor" and message.get("status") == "streaming":
            message["status"] = "interrupted"
    state["active_turn"] = None


def remove_part(state: dict[str, Any], part_id: str) -> None:
    """A part the student removed ("Remove sketch", VSL-43): gone from the parts and from its message."""
    state["parts"].pop(part_id, None)
    for message in state["messages"]:
        if part_id in message.get("part_ids", []):
            message["part_ids"] = [p for p in message["part_ids"] if p != part_id]


def replay_parts(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Recorded tutor parts in transcript order, including board events, without creating an engine."""
    out = {}
    for message in state["messages"]:
        if message["role"] != "tutor":
            continue
        for part_id in message["part_ids"]:
            part = state["parts"].get(part_id)
            if part is None or part["status"] == "retracted":
                continue
            kind = part["kind"]
            content = part.get("board_events", []) if kind == "board" else part.get("text", "")
            out[part_id] = {"kind": kind, "content": content}
    return out
