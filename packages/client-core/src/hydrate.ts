// Applying a core snapshot (API-17, UIA-02): the session's records are replaced by the snapshot's, which is the core's
// projection of the same events (services/agent-core/agent_core/api/projection.py). Other sessions are untouched.
import type { ContentBlock, EvCompanionAction, Session } from "@jana/contracts-ts";
import { splitBlocks } from "./blocks.js";
import {
  connectionKey,
  emptyConnection,
  type ActionRec,
  type BoardRec,
  type MessageRec,
  type PartRec,
  type RequestRec,
  type Store,
  type TurnRec,
  type VisualRec,
} from "./store.js";

/** The snapshot as the core projects it (the contract leaves these objects open; this is their shape). */
export interface CoreSnapshot {
  session: Session;
  cursor: { epoch: string; seq: number };
  messages: CoreMessage[];
  parts: Record<string, CorePart>;
  boards: Record<string, CoreBoard>;
  visuals: Record<string, CoreVisual>;
  open_requests: { request_id: string; part_id: string; card: unknown }[];
  active_turn: { turn_id: string; status: Record<string, unknown> } | null;
  /** The companion thread's durable buttons by action id (SPEC §19.3.3); absent for any other session. */
  actions?: Record<string, EvCompanionAction>;
}

export type CoreMessage = CoreStudentMessage | CoreTutorMessage;

export interface CoreStudentMessage {
  role: "student";
  message_id: string | null;
  client_message_id: string | null;
  turn_id: string | null;
  content: ContentBlock[];
  ts: number | null;
}

export interface CoreTutorMessage {
  role: "tutor";
  message_id: string | null;
  turn_id: string;
  part_ids: string[];
  status: TurnRec["status"];
  stop_reason: string | null;
  degraded: boolean;
  degraded_reason: string | null;
  error: TurnRec["error"];
  ts: number;
  intent: string;
  mode: string;
  user_message_id: string | null;
  client_message_id: string | null;
  phase: TurnRec["phase"];
  hint_rung: number | null;
  usage: TurnRec["usage"];
  elapsed_ms: number | null;
  verification: string | null;
}

export interface CorePart {
  part_id: string;
  turn_id: string | null;
  kind: string;
  index: number;
  status: PartRec["status"];
  text: string;
  duration_ms?: number;
  tools?: PartRec["tools"];
  visual_id?: string;
  card?: PartRec["card"];
  notice?: Record<string, unknown>;
  sketch?: NonNullable<PartRec["sketch"]>;
}

export interface CoreBoard {
  board_id: string;
  part_id: string | null;
  problem: BoardRec["problem"];
  knowns: unknown[];
  unknowns: unknown[];
  conventions: unknown[];
  steps: BoardRec["steps"];
  order: string[];
  awaiting: BoardRec["await"];
  highlight: BoardRec["highlight"];
  annotation?: BoardRec["annotation"];
  manipulation?: BoardRec["manipulation"];
}

export interface CoreVisual {
  visual_id: string;
  part_id: string;
  spec: VisualRec["spec"];
  step_index: number;
  focus: VisualRec["focus"];
}

function without<T>(record: Record<string, T>, drop: (value: T) => boolean): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([, value]) => !drop(value)));
}

export function hydrate(store: Store, snapshot: CoreSnapshot): Store {
  const sessionId = snapshot.session.session_id;
  const oldTurns = new Set(store.sessions[sessionId]?.turn_ids ?? []);
  const oldParts = new Set(Object.values(store.parts).filter((p) => p.turn_id !== null && oldTurns.has(p.turn_id)).map((p) => p.part_id));
  const turns = without(store.turns, (t) => t.session_id === sessionId);
  const messages = without(store.messages, (m) => m.session_id === sessionId);
  const parts = without(store.parts, (p) => oldParts.has(p.part_id));
  const requests = without(store.requests, (r) => oldParts.has(r.part_id));
  const oldBoards = new Set(store.sessions[sessionId]?.board_ids ?? []);
  const boards = without(store.boards, (b) => oldBoards.has(b.board_id));
  const visuals = without(store.visuals, (v) => oldParts.has(v.part_id));
  // The snapshot's actions replace the session's: a button folded from the stream equals one read after a restart.
  const actions: Record<string, ActionRec> = without(store.actions, (a) => a.session_id === sessionId);
  for (const [id, action] of Object.entries(snapshot.actions ?? {})) {
    actions[id] = { type: "companion.action", action_id: action.action_id, action: action.action, payload: { ...action.payload }, label_key: action.label_key, session_id: sessionId };
  }

  // Bubbles the student sent that the core has not seen yet survive a snapshot (UIA-05: never silently dropped).
  const unsent = (store.sessions[sessionId]?.pending_ids ?? []).filter((k) => {
    const m = store.messages[k];
    return m !== undefined && m.state !== "accepted" && !snapshot.messages.some((c) => c.role === "student" && c.client_message_id === k);
  });
  const turnIds: string[] = [];
  const pending: string[] = [];
  let hintRung = 0;
  for (const message of snapshot.messages) {
    if (message.role === "tutor") {
      turnIds.push(message.turn_id);
      if (message.hint_rung !== null) hintRung = message.hint_rung;
      const turn: TurnRec = {
        turn_id: message.turn_id, session_id: sessionId, intent: message.intent, mode: message.mode,
        user_message_id: message.user_message_id, client_message_id: message.client_message_id, status: message.status,
        stop_reason: message.stop_reason, degraded: message.degraded, degraded_reason: message.degraded_reason,
        error: message.error, part_ids: [...message.part_ids], message_id: message.message_id, phase: message.phase,
        usage: message.usage, elapsed_ms: message.elapsed_ms, verification: message.verification,
        hint_rung: message.hint_rung, ts: message.ts,
      };
      turns[turn.turn_id] = turn;
    } else {
      const key = message.client_message_id ?? message.message_id;
      if (key === null) continue;
      const record: MessageRec = {
        key, message_id: message.message_id, client_message_id: message.client_message_id, role: "student",
        session_id: sessionId, turn_id: message.turn_id, content: message.content, ts: message.ts,
        state: message.message_id === null ? "sending" : "accepted",
      };
      messages[key] = record;
      if (message.message_id === null) pending.push(key);
    }
  }
  for (const key of unsent) {
    const message = store.messages[key];
    if (message !== undefined) {
      messages[key] = message;
      pending.push(key);
    }
  }
  for (const core of Object.values(snapshot.parts)) {
    const blocks = splitBlocks(core.text, !(core.status === "started" || core.status === "streaming"));
    const { tools, visual_id: visualId, card, notice, duration_ms: duration, ...base } = core;
    const part: PartRec = { ...base, settled_blocks: blocks.settled, tail: blocks.tail };
    if (tools !== undefined) part.tools = tools;
    if (visualId !== undefined) part.visual_id = visualId;
    if (card !== undefined) part.card = card;
    if (notice !== undefined) part.notice = notice;
    if (duration !== undefined) part.duration_ms = duration;
    parts[part.part_id] = part;
    if (core.card !== undefined) {
      const request: RequestRec = { request_id: core.card.request_id, part_id: core.part_id, card: core.card.card, state: core.card.state };
      if (core.card.answer_summary !== undefined) request.answer_summary = core.card.answer_summary;
      requests[request.request_id] = request;
    }
  }
  const boardIds: string[] = [];
  for (const core of Object.values(snapshot.boards)) {
    boardIds.push(core.board_id);
    boards[core.board_id] = {
      board_id: core.board_id, part_id: core.part_id, problem: core.problem, step_ids: [...core.order], steps: core.steps,
      knowns: core.knowns, unknowns: core.unknowns, conventions: core.conventions, await: core.awaiting, highlight: core.highlight,
      current: core.order[core.order.length - 1] ?? null,
      ...(core.annotation !== undefined ? { annotation: core.annotation } : {}),
      ...(core.manipulation !== undefined ? { manipulation: core.manipulation } : {}),
    };
  }
  for (const core of Object.values(snapshot.visuals)) {
    visuals[core.visual_id] = { visual_id: core.visual_id, part_id: core.part_id, spec: core.spec, step: core.step_index, focus: core.focus };
  }
  const s = snapshot.session;
  const key = connectionKey(sessionId);
  const connection = { ...(store.connection[key] ?? emptyConnection()), epoch: snapshot.cursor.epoch, last_seq: snapshot.cursor.seq, resync: false };
  return {
    ...store,
    connection: { ...store.connection, [key]: connection },
    sessions: {
      ...store.sessions,
      [sessionId]: {
        session_id: sessionId, title: s.title, mode: s.mode, subject: s.subject, topic_id: s.topic_id, created_ts: s.created_ts,
        updated_ts: s.updated_ts, archived: s.archived, turn_ids: turnIds, board_ids: boardIds, hint_rung: hintRung, pending_ids: pending,
      },
    },
    turns, messages, parts, requests, boards, visuals, actions,
    cursors: { ...store.cursors, [sessionId]: { ...snapshot.cursor } },
  };
}
