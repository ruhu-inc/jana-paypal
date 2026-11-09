// The session stream client (API-20, UIA-07). One live stream per session per window. It opens the stream at the
// store's cursor, reconnects with exponential backoff and full jitter (reset only after a healthy period), reloads the
// snapshot on 409, re-acquires the launch token on 401 `unauthorized` (the core restarted), opens the profile again on
// 401 `profile_token_expired` (or says the profile is over when it cannot) and treats a stream with no bytes for the
// dead interval as lost. Reconnecting never re-sends commands.
import { CLIENT_KEYS } from "../generated/keys.js";
import { SseParser } from "./sse.js";

export type ConnectionState = "connecting" | "live" | "replaying" | "reconnecting" | "resyncing" | "closed";

export interface StreamEnvelope {
  v: number;
  channel: "session" | "app";
  seq: number;
  epoch: string;
  session_id: string | null;
  turn_id: string | null;
  ts: number;
  event: Record<string, unknown> & { type: string };
}

export interface StreamTimers {
  now(): number;
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

/** Where the core is and its launch token. A restarted core has a new port and token: `refresh` asks the shell again.
 * `headers` go on every request: the unlocked profile's `X-Profile-Token` (API-04), held in memory only (SEC-19). */
export interface Endpoint {
  baseUrl: string;
  sponsoredStudy?: boolean;
  token: string;
  headers?: Record<string, string>;
}

export interface StreamOptions {
  sessionId: string;
  path?: string;
  endpoint(refresh: boolean): Promise<Endpoint>;
  /** The store's cursor for this session, `<epoch>:<seq>`, or null before the first snapshot. */
  cursor(): string | null;
  onEnvelope(envelope: StreamEnvelope): void;
  /** Reload the snapshot into the store (409, `stream.reset`): resolves when the store holds it. */
  resync(): Promise<void>;
  onState(state: ConnectionState, detail: { outage_ms: number; attempt: number }): void;
  /** Open the profile again after the core closed it (idle auto-lock or a restart, SEC-19); true to reconnect. */
  renew?: () => Promise<boolean>;
  /** The profile could not be opened again: the stream has stopped and the profile is over here too. */
  onAuthLost?(code: string): void;
  fetch?: typeof fetch;
  timers?: StreamTimers;
  random?: () => number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  resetAfterMs?: number;
  deadAfterMs?: number;
}

const realTimers: StreamTimers = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
};

export class StreamClient {
  private stopped = false;
  private attempt = 0;
  private abort: AbortController | null = null;
  private healthyTimer: unknown = null;
  private deadTimer: unknown = null;
  private retryTimer: unknown = null;
  private lostAt: number | null = null;
  private refreshToken = false;
  private readonly timers: StreamTimers;
  private readonly fetchImpl: typeof fetch;
  private readonly random: () => number;
  state: ConnectionState = "connecting";

  constructor(private readonly options: StreamOptions) {
    this.timers = options.timers ?? realTimers;
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.random = options.random ?? Math.random;
  }

  start(): void {
    this.stopped = false;
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.abort?.abort();
    for (const t of [this.healthyTimer, this.deadTimer, this.retryTimer]) if (t !== null) this.timers.clearTimeout(t);
    this.setState("closed");
  }

  /** The next reconnect delay: full jitter over an exponentially growing cap (API-20). */
  delay(attempt: number): number {
    const base = this.options.baseDelayMs ?? CLIENT_KEYS.ui_reconnect_base_ms;
    const max = this.options.maxDelayMs ?? CLIENT_KEYS.ui_reconnect_max_ms;
    return Math.floor(this.random() * Math.min(max, base * 2 ** attempt));
  }

  private setState(state: ConnectionState): void {
    this.state = state;
    const outage = this.lostAt === null ? 0 : this.timers.now() - this.lostAt;
    this.options.onState(state, { outage_ms: outage, attempt: this.attempt });
  }

  private lost(): void {
    if (this.lostAt === null) this.lostAt = this.timers.now();
  }

  private schedule(immediately = false): void {
    if (this.stopped) return;
    this.lost();
    this.setState("reconnecting");
    const wait = immediately ? 0 : this.delay(this.attempt);
    this.attempt += 1;
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, wait);
  }

  private armDead(): void {
    if (this.deadTimer !== null) this.timers.clearTimeout(this.deadTimer);
    this.deadTimer = this.timers.setTimeout(() => this.abort?.abort(), this.options.deadAfterMs ?? CLIENT_KEYS.ui_stream_dead_ms);
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    if (this.state !== "reconnecting" && this.state !== "resyncing") this.setState("connecting");
    const controller = new AbortController();
    this.abort = controller;
    let response: Response;
    try {
      const endpoint = await this.options.endpoint(this.refreshToken);
      this.refreshToken = false;
      const headers: Record<string, string> = { ...endpoint.headers, authorization: `Bearer ${endpoint.token}`, accept: "text/event-stream" };
      const cursor = this.options.cursor();
      if (cursor !== null) headers["last-event-id"] = cursor;
      const path = this.options.path ?? `/v1/sessions/${this.options.sessionId}/events`;
      response = await this.fetchImpl(`${endpoint.baseUrl}${path}`, { headers, signal: controller.signal });
    } catch {
      this.refreshToken = true; // unreachable: the core may have restarted on another port
      this.schedule();
      return;
    }
    if (response.status === 409) {
      await response.body?.cancel();
      await this.resync();
      return;
    }
    if (response.status === 401) {
      const code = await errorCode(response);
      if (code === "profile_token_expired") {
        const renewed = this.options.renew === undefined ? false : await this.options.renew().catch(() => false);
        if (renewed && !this.stopped) {
          this.schedule(); // with backoff, so a core that keeps refusing is never asked in a tight loop
          return;
        }
        if (this.stopped) return;
        this.options.onAuthLost?.(code);
        this.stop();
        return;
      }
      this.refreshToken = true; // the core restarted with a new launch token
      this.schedule();
      return;
    }
    if (!response.ok || response.body === null) {
      await response.body?.cancel();
      this.schedule();
      return;
    }
    await this.read(response.body);
  }

  private async resync(): Promise<void> {
    this.lost();
    this.setState("resyncing");
    try {
      await this.options.resync();
    } catch {
      this.schedule();
      return;
    }
    void this.connect();
  }

  private async read(body: ReadableStream<Uint8Array>): Promise<void> {
    const parser = new SseParser();
    const reader = body.getReader();
    let replayLeft = 0;
    let closing: string | null = null;
    this.armDead();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        this.armDead();
        for (const item of parser.push(value)) {
          if (item.kind === "heartbeat") continue;
          const payload = JSON.parse(item.data) as Record<string, unknown>;
          if (item.event === "control") {
            if (payload.type === "stream.hello") {
              replayLeft = Number(payload.replayed ?? 0);
              this.healthy(replayLeft > 0 ? "replaying" : "live");
            } else if (payload.type === "stream.closing") {
              closing = String(payload.reason);
            } else if (payload.type === "stream.reset") {
              closing = "reset";
            }
            continue;
          }
          this.options.onEnvelope(payload as unknown as StreamEnvelope);
          if (replayLeft > 0) {
            replayLeft -= 1;
            if (replayLeft === 0) this.setState("live");
          }
        }
      }
    } catch {
      // aborted (dead stream or stop) or the connection dropped: reconnect below
    } finally {
      if (this.deadTimer !== null) this.timers.clearTimeout(this.deadTimer);
      this.deadTimer = null;
      if (this.healthyTimer !== null) this.timers.clearTimeout(this.healthyTimer);
      this.healthyTimer = null;
    }
    if (this.stopped) return;
    if (closing === "reset") {
      await this.resync();
      return;
    }
    this.schedule(closing === "lagging" || closing === "shutdown");
  }

  private healthy(state: ConnectionState): void {
    this.lostAt = null;
    this.setState(state);
    this.healthyTimer = this.timers.setTimeout(() => {
      this.attempt = 0; // backoff resets only after a healthy period (API-20)
    }, this.options.resetAfterMs ?? CLIENT_KEYS.ui_reconnect_reset_ms);
  }
}

async function errorCode(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: { code?: string } };
    return body.error?.code ?? "";
  } catch {
    return "";
  }
}
