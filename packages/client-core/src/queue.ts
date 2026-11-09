// The per-session delta queue (UIA-03, UIA-04). Events are queued and flushed into the reducer in batches on a timer
// (not animation frames, which pause in hidden windows). The interval starts at the floor and doubles, up to the
// ceiling, after a slow commit; it returns to the floor after enough fast commits. Events that need the student flush
// at once. Before a flush the queue coalesces: consecutive deltas of a part merge, and an earlier whole-record update
// is replaced by the later one for the same key. Reducing the coalesced batch equals reducing the raw one.
import type { EventEnvelope } from "@jana/contracts-ts";
import { CLIENT_KEYS } from "./generated/keys.js";

type Ev = Record<string, unknown> & { type: string };
export type Envelope = Omit<EventEnvelope, "event"> & { event: Ev };

const IMMEDIATE = new Set(["card.open", "board.await", "error", "turn.end"]);

export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const systemClock: Clock = {
  now: () => performance.now(),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

function replaceKey(event: Ev, turnId: string | null): string | null {
  switch (event.type) {
    case "board.step":
      return `board.step:${String(event.board_id)}:${String(event.step_id)}`;
    case "tool.state":
      return `tool.state:${String(event.call_id)}`;
    case "status":
      return `status:${turnId ?? ""}`;
    case "visual.step":
      return `visual.step:${String(event.visual_id)}`;
    default:
      return null;
  }
}

/** Coalesce a batch (UIA-04). Merged deltas take the later envelope's position and seq. */
export function coalesce(batch: Envelope[]): Envelope[] {
  const out: Envelope[] = [];
  const latest = new Map<string, number>(); // replace key -> index in `out`
  for (const envelope of batch) {
    const event = envelope.event;
    const previous = out[out.length - 1];
    if (event.type === "text.delta" && previous?.event.type === "text.delta" && previous.event.part_id === event.part_id) {
      out[out.length - 1] = { ...envelope, event: { ...event, delta: String(previous.event.delta) + String(event.delta) } };
      continue;
    }
    if (event.type === "board.await" || event.type === "board.reset") {
      // A board's await or reset reads its steps: no step update is replaced across it.
      const prefix = `board.step:${String(event.board_id)}:`;
      for (const k of [...latest.keys()]) if (k.startsWith(prefix)) latest.delete(k);
    }
    const key = replaceKey(event, envelope.turn_id);
    if (key !== null) {
      const at = latest.get(key);
      // A settled step clears the board's await and a later "awaiting" one does not restore it, so that pair keeps both.
      const clears = event.type === "board.step" && event.status === "awaiting" && at !== undefined && out[at]?.event.status !== "awaiting";
      if (at !== undefined && !clears) out[at] = DROPPED;
      latest.set(key, out.length);
    }
    out.push(envelope);
  }
  return out.filter((e) => e !== DROPPED);
}

const DROPPED = { event: { type: "__dropped__" } } as unknown as Envelope;

export interface QueueOptions {
  clock?: Clock;
  floorMs?: number;
  slowCommitMs?: number;
  ceilingMs?: number;
  fastCommits?: number;
}

export class DeltaQueue {
  private items: Envelope[] = [];
  private timer: unknown = null;
  private readonly clock: Clock;
  private readonly floor: number;
  private readonly slow: number;
  private readonly ceiling: number;
  private readonly fastNeeded: number;
  interval: number;
  private fast = 0;

  constructor(private readonly commit: (batch: Envelope[]) => void, options: QueueOptions = {}) {
    this.clock = options.clock ?? systemClock;
    this.floor = options.floorMs ?? CLIENT_KEYS.ui_flush_floor_ms;
    this.slow = options.slowCommitMs ?? CLIENT_KEYS.ui_flush_slow_commit_ms;
    this.ceiling = options.ceilingMs ?? CLIENT_KEYS.ui_flush_ceiling_ms;
    this.fastNeeded = options.fastCommits ?? CLIENT_KEYS.ui_flush_fast_commits;
    this.interval = this.floor;
  }

  get size(): number {
    return this.items.length;
  }

  push(envelope: Envelope): void {
    const event = envelope.event;
    if (event.type === "text.reset" && this.items.some((e) => e.event.type === "text.delta" && e.event.part_id === event.part_id && e.event.delta !== "")) {
      return; // a reset after accepted text is a protocol error: the text stays (UIA-04)
    }
    this.items.push(envelope);
    if (IMMEDIATE.has(event.type)) {
      this.flush();
      return;
    }
    if (this.timer === null) this.timer = this.clock.setTimeout(() => this.flush(), this.interval);
  }

  /** Commit everything queued now, measuring the commit to adapt the interval. */
  flush(): void {
    if (this.timer !== null) {
      this.clock.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.items.length === 0) return;
    const batch = coalesce(this.items);
    this.items = [];
    const started = this.clock.now();
    this.commit(batch);
    const took = this.clock.now() - started;
    if (took > this.slow) {
      this.interval = Math.min(this.interval * 2, this.ceiling);
      this.fast = 0;
    } else if (this.interval !== this.floor) {
      this.fast += 1;
      if (this.fast >= this.fastNeeded) {
        this.interval = this.floor;
        this.fast = 0;
      }
    }
  }

  clear(): void {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
    this.items = [];
  }
}
