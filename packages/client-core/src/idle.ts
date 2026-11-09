// LRN-20's idle trigger, the client's half (API 1.10, D104): the open step of the session's board has had no input for
// `offer_idle_s` with the window focused all that time, so the client says so once with `POST /v1/sessions/{id}/idle`
// (StepIdle). The core decides by its policy; an offer, if any, arrives as a card on the session's stream. A new step,
// any input or the window losing focus starts the count again, and a step is reported at most once.
import type { api_commands } from "@jana/contracts-ts";
import { CLIENT_KEYS } from "./generated/keys.js";
import { clientId } from "./ids.js";
import type { Store } from "./store.js";
import type { CommandClient } from "./stream/commands.js";

export interface IdleTimers {
  now(): number;
  set(run: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

const REAL: IdleTimers = {
  now: () => Date.now(),
  set: (run, ms) => setTimeout(run, ms),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** The open step of the session's newest board while no turn is running: the line awaited from the student, or the
 * line the board is on; null when there is none (no problem open, or the tutor is answering). */
export function openStepOf(store: Store, sessionId: string): string | null {
  const session = store.sessions[sessionId];
  if (session === undefined) return null;
  if (session.turn_ids.some((id) => store.turns[id]?.status === "streaming") || session.pending_ids.length > 0) return null;
  const boardId = session.board_ids[session.board_ids.length - 1];
  const board = boardId === undefined ? undefined : store.boards[boardId];
  if (board === undefined || board.problem === null) return null;
  const step = board.await?.step_id ?? board.current ?? board.step_ids[board.step_ids.length - 1] ?? "start";
  return `${board.board_id}:${step}`;
}

/** Sends the idle report; a refusal (a turn started meanwhile, an older core without the route) is not the student's
 * concern, so it is dropped. */
export async function reportStepIdle(commands: CommandClient, sessionId: string, idleMs: number, focused: boolean): Promise<void> {
  const body: api_commands.StepIdle = { idle_ms: Math.max(0, Math.min(3_600_000, Math.round(idleMs))), focused };
  try {
    await commands.post<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/idle`, body, clientId());
  } catch {
    /* LRN-20: the offer is a courtesy; nothing to show when it cannot be asked for */
  }
}

export class StepIdleWatch {
  private stepKey: string | null = null;
  private focused: boolean;
  private since: number;
  private timer: unknown = null;
  private reported = new Set<string>();

  constructor(private report: (idleMs: number, focused: boolean) => void, focused = true,
    private readonly thresholdMs: number = CLIENT_KEYS.offer_idle_s * 1000, private readonly timers: IdleTimers = REAL) {
    this.focused = focused;
    this.since = timers.now();
  }

  /** The open step now (`openStepOf`); a different step starts the count again. */
  step(key: string | null): void {
    if (key === this.stepKey) return;
    this.stepKey = key;
    this.restart();
  }

  /** The student did something (typed, clicked, pointed, spoke): the step is not idle. */
  input(): void {
    this.restart();
  }

  /** Window focus: only time with the window focused counts, all of it. */
  focus(focused: boolean): void {
    if (focused === this.focused) return;
    this.focused = focused;
    this.restart();
  }

  close(): void {
    this.cancel();
    this.stepKey = null;
  }

  private cancel(): void {
    if (this.timer !== null) this.timers.clear(this.timer);
    this.timer = null;
  }

  private restart(): void {
    this.cancel();
    this.since = this.timers.now();
    const key = this.stepKey;
    if (key === null || !this.focused || this.reported.has(key)) return;
    this.timer = this.timers.set(() => {
      this.timer = null;
      if (this.stepKey !== key || !this.focused) return;
      this.reported.add(key);
      this.report(this.timers.now() - this.since, true);
    }, this.thresholdMs);
  }
}
