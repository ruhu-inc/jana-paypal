// Private session history (UXS-48): Progress → "Session history" lists the open profile's retained sessions by date,
// subject, topic and duration (`GET /v1/history`), and opening one shows its conversation, board and visuals read-only
// (`GET /v1/history/{session_id}`). The core keeps conversations for the fixed DAT-08 period (no retention setting,
// D53) and says how long; a purged session opens with nothing in it while progress stays. There is no sharing, sending
// or export action here. Neither route is in the command contract yet: the shapes are proposed in
// contract-additions-m33a.json (D72) and each reply is checked against them here. An opened session becomes a store of
// its own (`historyStore`), the snapshot's shape, so the transcript draws it as it draws a live one, with nothing to
// send. Everything belongs to the open profile and is dropped when it closes (SEC-19).
import type { Session } from "@jana/contracts-ts";
import { hydrate, type CoreBoard, type CorePart, type CoreSnapshot, type CoreVisual } from "./hydrate.js";
import { DIGEST_SUBJECTS } from "./parents.js";
import type { ProfileController } from "./profiles.js";
import { count, list, oneOf, orNull, problemsOf, refuse, shape, text } from "./shapes.js";
import { CommandError } from "./stream/commands.js";
import { emptyStore, type Store, type StepRec } from "./store.js";

const MODES = ["guided", "solution", "check_working", "learn", "practice", "mock", "revision", "diagnostic", "mastery_check", "review"] as const;
const SESSION_ID = /^ses_[0-9A-HJKMNP-TV-Z]{26}$/;
const STEP_STATUSES = ["draft", "final", "awaiting", "checking", "correct", "incorrect", "equivalent_unsimplified", "unchecked"] as const;

/** A retained session (`HistoryEntry`). */
export interface HistoryEntry {
  session_id: string;
  mode: (typeof MODES)[number];
  subject: (typeof DIGEST_SUBJECTS)[number] | null;
  topic_id: string | null;
  topic_title: string | null;
  title: string;
  started_at: number;
  duration_ms: number | null;
  /** `purged`: its conversation has gone with the retention period; its progress stays. */
  transcript: "kept" | "purged";
}

export interface HistoryList {
  sessions: HistoryEntry[];
  next_before: string | null;
  retention_days: number;
}

export interface HistoryStep {
  step_id: string;
  index: number;
  latex: string;
  reason: string | null;
  owner: "tutor" | "student";
  status: (typeof STEP_STATUSES)[number];
}

/** One session read-only (`HistorySession`): the student's words, the tutor's parts as stored, each board's steps. */
export interface HistorySession {
  session: HistoryEntry;
  messages: ({ role: "student"; turn_id: string; text: string } | { role: "tutor"; turn_id: string; part_ids: string[] })[];
  parts: Record<string, Record<string, unknown>>;
  boards: { board_id: string; part_id: string | null; steps: HistoryStep[] }[];
}

const ENTRY = shape({
  session_id: text(30, SESSION_ID), mode: oneOf(MODES), subject: orNull(oneOf(DIGEST_SUBJECTS)), topic_id: orNull(text(96)),
  topic_title: orNull(text(200)), title: text(120), started_at: count, duration_ms: orNull(count), transcript: oneOf(["kept", "purged"]),
});

const LIST = shape({ sessions: list(ENTRY), next_before: orNull(text(30, SESSION_ID)), retention_days: count });

const STUDENT = shape({ role: oneOf(["student"]), turn_id: text(64), text: text(Number.MAX_SAFE_INTEGER) });
const TUTOR = shape({ role: oneOf(["tutor"]), turn_id: text(64), part_ids: list(text(64)) });

const SESSION = shape({
  session: ENTRY,
  messages: list((value, at, problems) => {
    const role = (value as { role?: unknown } | null)?.role;
    (role === "student" ? STUDENT : TUTOR)(value, at, problems);
  }),
  parts: (value, at, problems) => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) { problems.push(`${at}: not an object`); return; }
    for (const [key, part] of Object.entries(value)) if (typeof part !== "object" || part === null || Array.isArray(part)) problems.push(`${at}/${key}: not an object`);
  },
  boards: list(shape({
    board_id: text(30, /^brd_[0-9A-HJKMNP-TV-Z]{26}$/), part_id: orNull(text(64)),
    steps: list(shape({
      step_id: text(30, /^stp_[0-9A-HJKMNP-TV-Z]{26}$/), index: count, latex: text(4000), reason: orNull(text(300)),
      owner: oneOf(["tutor", "student"]), status: oneOf(STEP_STATUSES),
    })),
  })),
});

/** The history list as contract-additions-m33a.json proposes it (`HistoryList`); one that does not hold is refused. */
export function readHistoryList(value: unknown): HistoryList {
  const problems = problemsOf(LIST, value);
  if (problems.length > 0) throw refuse(problems);
  return value as HistoryList;
}

/** One session as contract-additions-m33a.json proposes it (`HistorySession`), and for the session asked for. */
export function readHistorySession(value: unknown, sessionId: string): HistorySession {
  const problems = problemsOf(SESSION, value);
  if (problems.length === 0 && (value as HistorySession).session.session_id !== sessionId) problems.push("/session/session_id: not the session asked for");
  if (problems.length > 0) throw refuse(problems);
  return value as HistorySession;
}

/** A read-only store holding one opened session, in the snapshot's shape (API-17): the transcript draws it unchanged.
 * A part's stored content is the snapshot's part; a visual part carries its visual. A purged session holds no turn. */
export function historyStore(opened: HistorySession): Store {
  const entry = opened.session;
  const session: Session = {
    session_id: entry.session_id, title: entry.title, mode: entry.mode as Session["mode"], subject: entry.subject ?? "",
    topic_id: entry.topic_id, created_ts: entry.started_at, updated_ts: entry.started_at + (entry.duration_ms ?? 0), archived: false,
  };
  const parts: Record<string, CorePart> = {};
  const recordedBoards: Record<string, CoreBoard> = {};
  const visuals: Record<string, CoreVisual> = {};
  for (const [partId, content] of Object.entries(opened.parts)) {
    const { visual, boards: storedBoards, ...part } = content as Record<string, unknown> & { visual?: CoreVisual; boards?: CoreBoard[] };
    if (Array.isArray(storedBoards)) for (const board of storedBoards) recordedBoards[board.board_id] = board;
    parts[partId] = { ...(part as unknown as CorePart), part_id: partId, text: typeof part.text === "string" ? part.text : "" };
    if (visual !== undefined && typeof visual === "object" && visual !== null) visuals[visual.visual_id] = visual;
  }
  const boards: Record<string, CoreBoard> = {};
  for (const board of opened.boards) {
    const steps: Record<string, StepRec> = {};
    for (const step of board.steps) steps[step.step_id] = { ...step, check_id: null, error: null };
    const order = [...board.steps].sort((a, b) => a.index - b.index || a.step_id.localeCompare(b.step_id)).map((s) => s.step_id);
    boards[board.board_id] = { board_id: board.board_id, part_id: board.part_id, problem: null, knowns: [], unknowns: [], conventions: [], steps, order, awaiting: null, highlight: null };
  }
  for (const [id, board] of Object.entries(recordedBoards)) {
    // SQL step rows carry later checked/revised lines; the part carries the board's visual metadata.
    const stored = boards[id];
    boards[id] = { ...board, ...(stored !== undefined ? { steps: { ...board.steps, ...stored.steps }, order: stored.order } : {}) };
  }
  // Each tutor turn answers the student's message stored just before it: that link places the message in the list.
  let asked: string | null = null;
  const messages = opened.messages.map((m): CoreSnapshot["messages"][number] => {
    if (m.role === "student") {
      asked = m.turn_id;
      return { role: "student", message_id: m.turn_id, client_message_id: null, turn_id: m.turn_id, content: [{ type: "text", text: m.text }], ts: entry.started_at };
    }
    const answers = asked;
    asked = null;
    return {
      role: "tutor", message_id: m.turn_id, turn_id: m.turn_id, part_ids: m.part_ids, status: "complete", stop_reason: null, degraded: false,
      degraded_reason: null, error: null, ts: entry.started_at, intent: "history", mode: entry.mode, user_message_id: answers, client_message_id: null,
      phase: null, hint_rung: null, usage: null, elapsed_ms: null, verification: null,
    };
  });
  const snapshot: CoreSnapshot = { session, cursor: { epoch: "history", seq: 0 }, messages, parts, boards, visuals, open_requests: [], active_turn: null };
  return hydrate(emptyStore(), snapshot);
}

// --- the controller ------------------------------------------------------------------------------------------------

export interface HistoryView {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  sessions: HistoryEntry[];
  nextBefore: string | null;
  retentionDays: number | null;
  /** The session open read-only, while it loads and once it has. `locked` during a mock sitting (ASM-18). */
  opened: { sessionId: string; status: "loading" | "ready" | "failed" | "locked"; session: HistorySession | null } | null;
  loadingMore: boolean;
}

export const EMPTY_HISTORY: HistoryView = { status: "idle", sessions: [], nextBefore: null, retentionDays: null, opened: null, loadingMore: false };

function missing(error: unknown): boolean {
  return error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
}

export class HistoryController {
  private view: HistoryView = EMPTY_HISTORY;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_HISTORY));
  }

  getView = (): HistoryView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: HistoryView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<HistoryView>): void {
    this.set({ ...this.view, ...change });
  }

  /** The newest page of sessions. */
  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.status !== "ready") this.patch({ status: "loading" });
    try {
      const page = readHistoryList(await this.profiles.commands.get<unknown>("/v1/history"));
      if (generation !== this.profiles.generation) return;
      this.patch({ status: "ready", sessions: page.sessions, nextBefore: page.next_before, retentionDays: page.retention_days });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      if (this.view.status !== "ready") this.patch({ status: missing(error) ? "unavailable" : "failed" });
    }
  }

  /** The next page, older sessions after the ones shown. */
  async more(): Promise<void> {
    const before = this.view.nextBefore;
    if (before === null || this.view.loadingMore) return;
    const generation = this.profiles.generation;
    this.patch({ loadingMore: true });
    try {
      const page = readHistoryList(await this.profiles.commands.get<unknown>(`/v1/history?before=${encodeURIComponent(before)}`));
      if (generation !== this.profiles.generation) return;
      const known = new Set(this.view.sessions.map((s) => s.session_id));
      this.patch({ sessions: [...this.view.sessions, ...page.sessions.filter((s) => !known.has(s.session_id))], nextBefore: page.next_before, loadingMore: false });
    } catch {
      if (generation === this.profiles.generation) this.patch({ loadingMore: false });
    }
  }

  /** Open one session read-only. */
  async open(sessionId: string): Promise<void> {
    const generation = this.profiles.generation;
    this.patch({ opened: { sessionId, status: "loading", session: null } });
    try {
      const session = readHistorySession(await this.profiles.commands.get<unknown>(`/v1/history/${encodeURIComponent(sessionId)}`), sessionId);
      if (generation === this.profiles.generation && this.view.opened?.sessionId === sessionId) this.patch({ opened: { sessionId, status: "ready", session } });
    } catch (error) {
      if (generation !== this.profiles.generation || this.view.opened?.sessionId !== sessionId) return;
      const locked = error instanceof CommandError && error.code === "tutor_locked";
      this.patch({ opened: { sessionId, status: locked ? "locked" : "failed", session: null } });
    }
  }

  close(): void {
    this.patch({ opened: null });
  }
}
