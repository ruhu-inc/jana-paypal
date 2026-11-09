// The normalised client store (UIA-01): a delta touches one record, so one component re-renders. Components select
// records by id; no component subscribes to a whole slice. The store holds the unlocked profile's data only.
import type { Card, CardAbout, ContentBlock, EvCompanionAction, EvStatus, StepError, Usage, VisualsVisualSpecResolved } from "@jana/contracts-ts";
import type { ConnectionState } from "./stream/client.js";

export type { ConnectionState };

export interface ConnectionSlice {
  state: ConnectionState;
  epoch: string | null;
  last_seq: number;
  retry_at: number | null;
  lagging: boolean;
  /** Set when the core's epoch differs from the cursor's or a cursor expired: reload the snapshot (API-17). */
  resync: boolean;
  /** Server time minus local time, estimated from envelope `ts` (UIA-22). */
  clock_offset_ms: number;
  /** "Reconnecting to the tutor…": only after the outage threshold, or while resyncing (UIA-07). */
  banner: boolean;
  /** Server time of the last event received: silence thresholds count from it (UXS-02, UXS-03). */
  last_event_ts: number | null;
}

export interface SessionRec {
  session_id: string;
  title: string;
  mode: string;
  subject: string;
  topic_id: string | null;
  created_ts: number;
  updated_ts: number;
  archived: boolean;
  turn_ids: string[];
  board_ids: string[];
  hint_rung: number;
  /** Optimistic messages not yet accepted, in the order they were sent (UIA-05). */
  pending_ids: string[];
}

/** A status phase; `unknown` is a phase from a newer core that this build does not know (API-35 open enum). */
export type Phase = Omit<EvStatus, "type" | "phase"> & { phase: EvStatus["phase"] | "unknown" };

export interface TurnRec {
  turn_id: string;
  session_id: string;
  intent: string;
  mode: string;
  user_message_id: string | null;
  client_message_id: string | null;
  status: "streaming" | "complete" | "error" | "interrupted" | "truncated";
  stop_reason: string | null;
  degraded: boolean;
  degraded_reason: string | null;
  error: { code: string; message: string; retryable: boolean; scope: string } | null;
  part_ids: string[];
  message_id: string | null;
  phase: Phase | null;
  usage: Usage | null;
  elapsed_ms: number | null;
  verification: string | null;
  hint_rung: number | null;
  ts: number;
}

export interface MessageRec {
  /** The client id when the message has one, so the optimistic bubble keeps its component (UIA-05). */
  key: string;
  message_id: string | null;
  client_message_id: string | null;
  role: "student";
  session_id: string;
  turn_id: string | null;
  content: ContentBlock[];
  ts: number | null;
  state: "sending" | "accepted" | "failed";
  /** Why a send failed: an error code of TABLE API-32a, shown with its copy key `error.<code>`. */
  error_code?: string;
  error_details?: Record<string, unknown>;
}

export interface ToolRec {
  call_id: string;
  name: string;
  render_class: "card" | "ephemeral";
  state: unknown;
}

export interface PartRec {
  part_id: string;
  turn_id: string | null;
  kind: string;
  index: number;
  status: "started" | "streaming" | "complete" | "error" | "interrupted" | "retracted";
  text: string;
  /** Markdown blocks that can no longer change (UIA-13); `tail` is the block still streaming. */
  settled_blocks: string[];
  tail: string;
  duration_ms?: number;
  tools?: Record<string, ToolRec>;
  visual_id?: string;
  /** An online sketch the engine resolved for this part (VSL-43): drawn only from its kept elements. */
  sketch?: Record<string, unknown> & { sketch_id: string; status: "checked" | "not_checked" };
  /** `about`: what an engine-written card is about (a root-gap card's skills and misconception, for the concept map). */
  card?: { request_id: string; card: Card; state: "open" | "answering" | "answered" | "cancelled"; answer_summary?: string; answered_ts?: number; about?: CardAbout };
  notice?: Record<string, unknown>;
}

export interface RequestRec {
  request_id: string;
  part_id: string;
  card: Card;
  state: "open" | "answering" | "answered" | "cancelled";
  answer_summary?: string;
}

export interface StepRec {
  step_id: string;
  index: number;
  latex: string;
  reason: string | null;
  owner: "tutor" | "student";
  status: string;
  check_id: string | null;
  error: StepError | null;
  /** The engine's split of the line into top-level terms (API-12): what annotations and selection address. */
  terms?: { term_id: string; start: number; end: number }[];
  /** Terms the new line carried across the equals sign or cancelled (UXS-61), from the step checker's comparison of
   * the two checked lines; the client never infers one. */
  term_moves?: TermMoveRec[];
}

/** One term move (UXS-61): `from` is the earlier line's term (`stp_….t2`), `to` the new line's term or null. */
export interface TermMoveRec {
  from: string;
  to: string | null;
  kind: "carry" | "cancel";
}

/** A mark's target, by ids only (VSL-40): a line or a term range of it, or a part of a visual on screen. */
export interface MarkTargetRec {
  step_id: string | null;
  terms: string | null;
  visual_id: string | null;
  part_id: string | null;
}

/** The tutor's one annotation on the board (VSL-40): a new one replaces it and a new line clears it. */
export interface AnnotationRec {
  annotation_id: string;
  mark: "circle" | "underline" | "arrow" | "strike" | "margin_note" | "look_here";
  target: MarkTargetRec;
  to: MarkTargetRec | null;
  note: string | null;
}

export interface BoardRec {
  board_id: string;
  part_id: string | null;
  problem: { text: string; latex: string | null } | null;
  step_ids: string[];
  steps: Record<string, StepRec>;
  knowns: unknown[];
  unknowns: unknown[];
  conventions: unknown[];
  await: { step_id: string; prompt: string; input_kind: string } | null;
  /** `photo_regions`: the same places on the student's own photo (`cap_…:rgn_<n>`, JM-D10), when the lines came from it. */
  highlight: { step_ids: string[]; ttl_ms: number | null; ts: number; spotlight?: boolean; photo_regions?: string[] } | null;
  annotation?: AnnotationRec | null;
  /** A manipulable visual is the student's turn (VSL-42): the awaiting line, the visual and its adjustable parts. */
  manipulation?: { step_id: string; visual_id: string; part_ids: string[]; prompt: string } | null;
  /** The line the board is on (the newest, or the one the student moved to): bound visuals follow it (VSL-20). */
  current: string | null;
}

export interface VisualRec {
  visual_id: string;
  part_id: string;
  spec: VisualsVisualSpecResolved;
  step: number;
  focus: { part_ids: string[]; ttl_ms: number | null; ts: number; spotlight?: boolean } | null;
}

/** A note the tutor saved because the student asked (MEM-11), announced in the conversation with an Undo until
 * `undo_until_ts` (server time). The snapshot does not carry these (G94): after a reload the note stays on the "What
 * your tutor knows" page, where it can still be forgotten. */
export interface MemoryRec {
  note_id: string;
  session_id: string;
  /** The turn the save belongs to, or null for a save announced between turns. */
  turn_id: string | null;
  text: string;
  undo_until_ts: number;
  state: "saved" | "forgotten";
  /** Stream order, so announcements outside a turn keep their order. */
  seq: number;
}

/** A durable button of the companion thread (SPEC §19.3.3): the `companion.action` event as the engine sent it, kept
 * by its action id so it survives a reconnect and a restart (the core's snapshot carries the same map as `actions`). */
export type ActionRec = EvCompanionAction & { session_id: string };

export interface Cursor {
  epoch: string;
  seq: number;
}

export interface Store {
  connection: Record<string, ConnectionSlice>;
  sessions: Record<string, SessionRec>;
  turns: Record<string, TurnRec>;
  messages: Record<string, MessageRec>;
  parts: Record<string, PartRec>;
  requests: Record<string, RequestRec>;
  boards: Record<string, BoardRec>;
  visuals: Record<string, VisualRec>;
  memory: Record<string, MemoryRec>;
  /** The companion thread's durable buttons by action id (SPEC §19.3.3). */
  actions: Record<string, ActionRec>;
  cursors: Record<string, Cursor>;
}

export function emptyStore(): Store {
  return { connection: {}, sessions: {}, turns: {}, messages: {}, parts: {}, requests: {}, boards: {}, visuals: {}, memory: {}, actions: {}, cursors: {} };
}

export function connectionKey(sessionId: string | null): string {
  return sessionId === null ? "app" : `session:${sessionId}`;
}

export function emptyConnection(): ConnectionSlice {
  return { state: "connecting", epoch: null, last_seq: 0, retry_at: null, lagging: false, resync: false, clock_offset_ms: 0, banner: false, last_event_ts: null };
}

/** Elapsed time of a turn's current phase in server time (UIA-22): the clock never jumps after a reconnect. */
export function elapsedMs(phaseStartedTs: number, connection: ConnectionSlice | undefined, now: number = Date.now()): number {
  return Math.max(0, now + (connection?.clock_offset_ms ?? 0) - phaseStartedTs);
}

/** How long the Undo of a saved note is still offered, in server time (MEM-11); 0 once the window has closed. */
export function undoLeftMs(memory: MemoryRec, connection: ConnectionSlice | undefined, now: number = Date.now()): number {
  if (memory.state !== "saved") return 0;
  return Math.max(0, memory.undo_until_ts - (now + (connection?.clock_offset_ms ?? 0)));
}

/** Time since the last event in server time, or null before any event. */
export function silenceMs(connection: ConnectionSlice | undefined, now: number = Date.now()): number | null {
  if (connection?.last_event_ts == null) return null;
  return Math.max(0, now + connection.clock_offset_ms - connection.last_event_ts);
}
