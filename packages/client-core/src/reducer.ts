// The one way server events enter the store (UIA-02): `reduce(store, envelope)` is pure (no I/O, timers or randomness),
// idempotent by sequence and ignores unknown event types (API-06). `hydrate` applies a core snapshot with the same
// invariants. Records are replaced only when touched, so a component selecting one record re-renders only for it.
//
// The fold mirrors the core's snapshot projection (services/agent-core/agent_core/api/projection.py): a client that
// hydrates a snapshot and then applies the stream ends equal to one that applied every event (UIA-02, API-16).
import type { ContentBlock, EventEnvelope } from "@jana/contracts-ts";
import { splitBlocks } from "./blocks.js";
import { moveVisual, selectBoardLine, syncNewVisual } from "./visuals.js";
import {
  connectionKey,
  emptyConnection,
  type ActionRec,
  type AnnotationRec,
  type BoardRec,
  type MarkTargetRec,
  type MemoryRec,
  type MessageRec,
  type PartRec,
  type SessionRec,
  type Store,
  type TurnRec,
} from "./store.js";

type Ev = Record<string, unknown> & { type: string };
type Envelope = Omit<EventEnvelope, "event"> & { event: Ev };

const OPEN = new Set(["started", "streaming"]);

function withText(part: PartRec, text: string, status: PartRec["status"]): PartRec {
  const done = !OPEN.has(status);
  const blocks = splitBlocks(text, done);
  return { ...part, text, status, settled_blocks: blocks.settled, tail: blocks.tail };
}

function put<T>(record: Record<string, T>, key: string, value: T): Record<string, T> {
  return { ...record, [key]: value };
}

function tutorTurn(store: Store, turnId: string | null): TurnRec | undefined {
  return turnId === null ? undefined : store.turns[turnId];
}

function board(store: Store, boardId: string): BoardRec {
  return (
    store.boards[boardId] ?? {
      board_id: boardId, part_id: null, problem: null, step_ids: [], steps: {}, knowns: [], unknowns: [],
      conventions: [], await: null, highlight: null, current: null,
    }
  );
}

function addBoardToSession(store: Store, sessionId: string, boardId: string): Store {
  const session = store.sessions[sessionId];
  if (session === undefined || session.board_ids.includes(boardId)) return store;
  return { ...store, sessions: put(store.sessions, sessionId, { ...session, board_ids: [...session.board_ids, boardId] }) };
}

/** A spotlight ends at the next utterance or student action (UXS-57): the dimming lifts, the pointer stays. */
export function endSpotlights(store: Store): Store {
  let boards = store.boards;
  for (const [id, b] of Object.entries(store.boards)) {
    if (b.highlight?.spotlight === true) {
      const { spotlight: _lifted, ...rest } = b.highlight;
      boards = put(boards, id, { ...b, highlight: rest });
    }
  }
  let visuals = store.visuals;
  for (const [id, v] of Object.entries(store.visuals)) {
    if (v.focus?.spotlight === true) {
      const { spotlight: _lifted, ...rest } = v.focus;
      visuals = put(visuals, id, { ...v, focus: rest });
    }
  }
  return boards === store.boards && visuals === store.visuals ? store : { ...store, boards, visuals };
}

/** Fold one envelope. Envelopes at or below the cursor are ignored; another epoch flags a resync instead. */
export function reduce(store: Store, envelope: Envelope): Store {
  const sessionId = envelope.session_id;
  if (envelope.channel !== "session" || sessionId === null) return store; // the app channel arrives in S2 (API-22)
  const cursor = store.cursors[sessionId];
  const key = connectionKey(sessionId);
  if (cursor !== undefined && cursor.epoch !== envelope.epoch) {
    const connection = store.connection[key] ?? emptyConnection();
    return { ...store, connection: put(store.connection, key, { ...connection, resync: true }) };
  }
  if (cursor !== undefined && envelope.seq <= cursor.seq) return store;
  let next: Store = { ...store, cursors: put(store.cursors, sessionId, { epoch: envelope.epoch, seq: envelope.seq }) };
  const connection = next.connection[key];
  if (connection !== undefined) {
    next = { ...next, connection: put(next.connection, key, { ...connection, epoch: envelope.epoch, last_seq: envelope.seq }) };
  }
  return apply(next, sessionId, envelope);
}

function apply(store: Store, sessionId: string, envelope: Envelope): Store {
  const event = envelope.event;
  const turnId = envelope.turn_id;
  const ts = envelope.ts;
  switch (event.type) {
    case "turn.start": {
      const id = event.turn_id as string;
      const turn: TurnRec = {
        turn_id: id, session_id: sessionId, intent: event.intent as string, mode: event.mode as string,
        user_message_id: event.user_message_id as string | null, client_message_id: event.client_message_id as string | null,
        status: "streaming", stop_reason: null, degraded: false, degraded_reason: null, error: null, part_ids: [],
        message_id: null, phase: null, usage: null, elapsed_ms: null, verification: null, hint_rung: null, ts,
      };
      const session = store.sessions[sessionId];
      const sessions = session === undefined ? store.sessions : put(store.sessions, sessionId, { ...session, turn_ids: [...session.turn_ids, id] });
      return { ...store, turns: put(store.turns, id, turn), sessions };
    }
    case "message.accepted": {
      const cli = event.client_message_id as string;
      const message = store.messages[cli];
      if (message === undefined) return store;
      const accepted: MessageRec = { ...message, message_id: event.message_id as string, turn_id: event.turn_id as string, ts, state: "accepted" };
      const session = store.sessions[sessionId];
      const sessions = session === undefined ? store.sessions
        : put(store.sessions, sessionId, { ...session, pending_ids: session.pending_ids.filter((p) => p !== cli) });
      return { ...store, messages: put(store.messages, cli, accepted), sessions };
    }
    case "status": {
      const turn = tutorTurn(store, turnId);
      if (turn === undefined) return store;
      const { type: _type, ...phase } = event;
      return { ...store, turns: put(store.turns, turn.turn_id, { ...turn, phase: phase as unknown as TurnRec["phase"] }) };
    }
    case "part.start": {
      // The next utterance restores a spotlight (UXS-57).
      const lit = event.kind === "text" ? endSpotlights(store) : store;
      const id = event.part_id as string;
      const part: PartRec = { part_id: id, turn_id: turnId, kind: event.kind as string, index: event.index as number, status: "started", text: "", settled_blocks: [], tail: "" };
      let next: Store = { ...lit, parts: put(lit.parts, id, part) };
      const turn = tutorTurn(lit, turnId);
      if (turn !== undefined) next = { ...next, turns: put(next.turns, turn.turn_id, { ...turn, part_ids: [...turn.part_ids, id] }) };
      return next;
    }
    case "text.delta": {
      const part = store.parts[event.part_id as string];
      if (part === undefined || !OPEN.has(part.status)) return store;
      return { ...store, parts: put(store.parts, part.part_id, withText(part, part.text + (event.delta as string), "streaming")) };
    }
    case "text.reset": {
      const part = store.parts[event.part_id as string];
      // Only an empty placeholder resets; accepted text is never cleared (API-08, UIA-04).
      if (part === undefined || part.text !== "") return store;
      return { ...store, parts: put(store.parts, part.part_id, { ...part, status: "started" }) };
    }
    case "part.end": {
      const part = store.parts[event.part_id as string];
      if (part === undefined) return store;
      const ended = withText(part, part.text, event.status as PartRec["status"]);
      return { ...store, parts: put(store.parts, part.part_id, { ...ended, duration_ms: event.duration_ms as number }) };
    }
    case "tool.state": {
      const part = store.parts[event.part_id as string];
      if (part === undefined) return store;
      const callId = event.call_id as string;
      const tools = { ...(part.tools ?? {}), [callId]: { call_id: callId, name: event.name as string, render_class: event.render_class as "card" | "ephemeral", state: event.state } };
      return { ...store, parts: put(store.parts, part.part_id, { ...part, tools, status: part.status === "started" ? "streaming" : part.status }) };
    }
    case "board.reset": {
      const id = event.board_id as string;
      const { annotation: _cleared, ...prior } = board(store, id);
      const fresh: BoardRec = { ...prior, part_id: event.part_id as string, problem: event.problem as BoardRec["problem"],
        step_ids: [], steps: {}, knowns: [], unknowns: [], conventions: [], await: null, highlight: null, current: null };
      return addBoardToSession({ ...store, boards: put(store.boards, id, fresh) }, sessionId, id);
    }
    case "board.knowns": {
      const id = event.board_id as string;
      const updated = { ...board(store, id), knowns: event.knowns as unknown[], unknowns: event.unknowns as unknown[], conventions: event.conventions as unknown[] };
      return addBoardToSession({ ...store, boards: put(store.boards, id, updated) }, sessionId, id);
    }
    case "board.step": {
      const id = event.board_id as string;
      const current = board(store, id);
      const { type: _type, board_id: _board, ...step } = event;
      const steps = put(current.steps, event.step_id as string, step as unknown as BoardRec["steps"][string]);
      const stepIds = Object.keys(steps).sort((a, b) => {
        const byIndex = (steps[a]?.index ?? 0) - (steps[b]?.index ?? 0);
        return byIndex !== 0 ? byIndex : a < b ? -1 : a > b ? 1 : 0;
      });
      const awaiting = current.await !== null && current.await.step_id === event.step_id && event.status !== "awaiting" ? null : current.await;
      // A new line clears the tutor's annotation (VSL-40); the key is dropped, so a replay equals a snapshot (UIA-02).
      const isNew = current.steps[event.step_id as string] === undefined;
      const { annotation: _old, ...kept } = current;
      const base0 = isNew ? kept : current;
      // The awaiting line answered (checked or not) ends the manipulation (VSL-42); the key is dropped, not nulled.
      const { manipulation: _done, ...open } = base0;
      const base = base0.manipulation?.step_id === event.step_id && event.status !== "awaiting" ? open : base0;
      const added = addBoardToSession({ ...store, boards: put(store.boards, id, { ...base, steps, step_ids: stepIds, await: awaiting }) }, sessionId, id);
      // A new line at the end of the board is its current one, and visuals bound to it follow (VSL-20). Going by the
      // board's order rather than arrival keeps a coalesced batch equal to the raw one (UIA-04).
      return isNew && stepIds[stepIds.length - 1] === event.step_id ? selectBoardLine(added, id, event.step_id as string) : added;
    }
    case "board.highlight": {
      const id = event.board_id as string;
      const photo = Array.isArray(event.photo_regions) && event.photo_regions.length > 0 ? { photo_regions: event.photo_regions as string[] } : {};
      const updated = { ...board(store, id), highlight: { step_ids: event.step_ids as string[], ttl_ms: event.ttl_ms as number | null, ts, ...(event.spotlight === true ? { spotlight: true } : {}), ...photo } };
      return addBoardToSession({ ...store, boards: put(store.boards, id, updated) }, sessionId, id);
    }
    case "visual.await": {
      const id = event.board_id as string;
      const manipulation = { step_id: event.step_id as string, visual_id: event.visual_id as string, part_ids: event.part_ids as string[], prompt: event.prompt as string };
      return addBoardToSession({ ...store, boards: put(store.boards, id, { ...board(store, id), manipulation }) }, sessionId, id);
    }
    case "board.annotate": {
      // One annotation at a time: a new one replaces it (VSL-40).
      const id = event.board_id as string;
      const annotation: AnnotationRec = { annotation_id: event.annotation_id as string, mark: event.mark as AnnotationRec["mark"],
        target: event.target as MarkTargetRec, to: (event.to ?? null) as MarkTargetRec | null, note: (event.note ?? null) as string | null };
      return addBoardToSession({ ...store, boards: put(store.boards, id, { ...board(store, id), annotation }) }, sessionId, id);
    }
    case "board.await": {
      const id = event.board_id as string;
      const updated = { ...board(store, id), await: { step_id: event.step_id as string, prompt: event.prompt as string, input_kind: event.input_kind as string } };
      return addBoardToSession({ ...store, boards: put(store.boards, id, updated) }, sessionId, id);
    }
    case "visual.spec": {
      const id = event.visual_id as string;
      const partId = event.part_id as string;
      let next: Store = { ...store, visuals: put(store.visuals, id, { visual_id: id, part_id: partId, spec: event.spec as never, step: 0, focus: null }) };
      const part = next.parts[partId];
      if (part !== undefined) next = { ...next, parts: put(next.parts, partId, { ...part, visual_id: id }) };
      return syncNewVisual(next, id);
    }
    case "visual.sketch": {
      // An online sketch (VSL-43): kept on its part; "Remove sketch" drops it (removeSketch).
      const part = store.parts[event.part_id as string];
      if (part === undefined) return store;
      return { ...store, parts: put(store.parts, part.part_id, { ...part, sketch: event.sketch as NonNullable<PartRec["sketch"]> }) };
    }
    case "visual.step": {
      const visual = store.visuals[event.visual_id as string];
      if (visual === undefined) return store;
      return moveVisual(store, visual.visual_id, event.step_index as number, "tutor");
    }
    case "visual.focus": {
      const visual = store.visuals[event.visual_id as string];
      if (visual === undefined) return store;
      return { ...store, visuals: put(store.visuals, visual.visual_id, { ...visual, focus: { part_ids: event.part_ids as string[], ttl_ms: event.ttl_ms as number | null, ts, ...(event.spotlight === true ? { spotlight: true } : {}) } }) };
    }
    case "card.open": {
      const requestId = event.request_id as string;
      const partId = event.part_id as string;
      const card = event.card as NonNullable<PartRec["card"]>["card"];
      let next: Store = { ...store, requests: put(store.requests, requestId, { request_id: requestId, part_id: partId, card, state: "open" }) };
      const part = next.parts[partId];
      const about = event.about === undefined ? {} : { about: event.about as NonNullable<NonNullable<PartRec["card"]>["about"]> };
      if (part !== undefined) next = { ...next, parts: put(next.parts, partId, { ...part, card: { request_id: requestId, card, state: "open", ...about } }) };
      return next;
    }
    case "card.answered":
    case "card.cancel": {
      const request = store.requests[event.request_id as string];
      if (request === undefined) return store;
      const state = event.type === "card.answered" ? "answered" : "cancelled";
      const summary = event.type === "card.answered" ? { answer_summary: event.answer_summary as string } : {};
      const answered = event.type === "card.answered" ? { answered_ts: event.answered_ts as number } : {};
      let next: Store = { ...store, requests: put(store.requests, request.request_id, { ...request, state, ...summary }) };
      const part = next.parts[request.part_id];
      if (part?.card !== undefined) next = { ...next, parts: put(next.parts, part.part_id, { ...part, card: { ...part.card, state, ...summary, ...answered } }) };
      return next;
    }
    case "notice": {
      const id = event.notice_id as string;
      const { type: _type, ...notice } = event;
      return { ...store, parts: put(store.parts, id, { part_id: id, turn_id: turnId, kind: "notice", index: -1, status: "complete", text: "", settled_blocks: [], tail: "", notice }) };
    }
    case "error": {
      const turn = tutorTurn(store, turnId);
      if (turn === undefined) return store;
      const { type: _type, ...error } = event;
      return { ...store, turns: put(store.turns, turn.turn_id, { ...turn, error: error as unknown as TurnRec["error"] }) };
    }
    case "session.updated": {
      const session = store.sessions[sessionId];
      if (session === undefined) return store;
      const changed: Partial<SessionRec> = { updated_ts: ts };
      for (const field of ["title", "mode", "subject", "topic_id"] as const) {
        if (field in event) (changed as Record<string, unknown>)[field] = event[field];
      }
      let next: Store = store;
      if ("hint_rung" in event) {
        changed.hint_rung = event.hint_rung as number;
        const turn = tutorTurn(store, turnId);
        if (turn !== undefined) next = { ...next, turns: put(next.turns, turn.turn_id, { ...turn, hint_rung: event.hint_rung as number }) };
      }
      return { ...next, sessions: put(next.sessions, sessionId, { ...session, ...changed }) };
    }
    case "memory.saved": {
      // A note saved at the student's request (MEM-11): the conversation shows it with Undo until the window ends.
      const id = event.note_id as string;
      if (store.memory[id]?.state === "forgotten") return store; // a forget is final
      const saved: MemoryRec = {
        note_id: id, session_id: sessionId, turn_id: turnId, text: event.text as string,
        undo_until_ts: event.undo_until_ts as number, state: "saved", seq: envelope.seq,
      };
      return { ...store, memory: put(store.memory, id, saved) };
    }
    case "memory.forgotten":
      return markForgotten(store, event.note_id as string);
    case "companion.action": {
      // A durable button of the companion thread (SPEC §19.3.3): from a tool result, never from model text. The same
      // action id again replaces it, as the core's projection folds it into `actions`.
      const id = event.action_id as string;
      const action: ActionRec = {
        type: "companion.action", action_id: id, action: event.action as ActionRec["action"], payload: { ...(event.payload as ActionRec["payload"]) },
        label_key: event.label_key as string, session_id: sessionId,
      };
      return { ...store, actions: put(store.actions, id, action) };
    }
    case "turn.end": {
      const turn = tutorTurn(store, turnId);
      let next: Store = store;
      if (turn !== undefined) {
        const persisted = event.persisted as { message_id: string };
        next = { ...next, turns: put(next.turns, turn.turn_id, {
          ...turn, message_id: persisted.message_id, status: event.status as TurnRec["status"], stop_reason: event.stop_reason as string,
          degraded: event.degraded as boolean, degraded_reason: event.degraded_reason as string | null, usage: event.usage as TurnRec["usage"],
          elapsed_ms: event.elapsed_ms as number, verification: event.verification as string,
        }) };
      }
      const session = next.sessions[sessionId];
      if (session !== undefined) next = { ...next, sessions: put(next.sessions, sessionId, { ...session, updated_ts: ts }) };
      return next;
    }
    default:
      return store; // unknown and debug-only events change nothing (API-06)
  }
}

/** Insert the student's message the moment it is sent (UIA-05): keyed by its client id, state `sending`. */
export function optimistic(store: Store, sessionId: string, clientMessageId: string, content: ContentBlock[]): Store {
  if (store.messages[clientMessageId] !== undefined) return store;
  const message: MessageRec = { key: clientMessageId, message_id: null, client_message_id: clientMessageId, role: "student", session_id: sessionId, turn_id: null, content, ts: null, state: "sending" };
  const session = store.sessions[sessionId];
  const sessions = session === undefined ? store.sessions : put(store.sessions, sessionId, { ...session, pending_ids: [...session.pending_ids, clientMessageId] });
  return { ...store, messages: put(store.messages, clientMessageId, message), sessions };
}

/** A note is gone (Undo, "Forget this", or the core's `memory.forgotten`). Only a note announced in this conversation
 * changes: one forgotten from the knows page that was never announced here adds nothing to the conversation. */
export function markForgotten(store: Store, noteId: string): Store {
  const memory = store.memory[noteId];
  if (memory === undefined || memory.state === "forgotten") return store;
  // The text goes with the note (LRN-37): a forgotten announcement keeps only its place in the conversation.
  return { ...store, memory: put(store.memory, noteId, { ...memory, state: "forgotten", text: "" }) };
}

export function markFailed(store: Store, clientMessageId: string, code = "internal_error", details?: Record<string, unknown>): Store {
  const message = store.messages[clientMessageId];
  if (message === undefined || message.state === "accepted") return store;
  return { ...store, messages: put(store.messages, clientMessageId, { ...message, state: "failed", error_code: code,
    ...(details === undefined ? {} : { error_details: details }) }) };
}

export function markSending(store: Store, clientMessageId: string): Store {
  const message = store.messages[clientMessageId];
  if (message === undefined || message.state !== "failed") return store;
  return { ...store, messages: put(store.messages, clientMessageId, { ...message, state: "sending" }) };
}

/** "Remove sketch" (VSL-43): the part keeps its place but no longer draws the sketch. */
export function removeSketch(store: Store, sketchId: string): Store {
  const part = Object.values(store.parts).find((p) => p.sketch?.sketch_id === sketchId);
  if (part === undefined) return store;
  const { sketch: _gone, ...kept } = part;
  return { ...store, parts: put(store.parts, part.part_id, kept) };
}
