// One open session in the UI: the store holder, the delta queue, the stream and the commands, wired together.
// Sending inserts the student's bubble at once (UIA-05); a command that cannot reach the core (restarting) waits and
// is sent again with the same idempotency key when the stream is back (UIA-07); the banner shows only after the
// outage threshold or during a resync.
import type { ContentBlock, SessionCreated, SessionList, TurnAccepted, CancelResult, CreateSession, UpdateSession } from "@jana/contracts-ts";
import type { api_commands } from "@jana/contracts-ts/client";
import { SessionJournal } from "./journal.js";
import { CLIENT_KEYS } from "./generated/keys.js";
import { assertConfirmed, type CapturedQuestion } from "./capture.js";
import { workingLines } from "./input.js";
import { hydrate, type CoreSnapshot } from "./hydrate.js";
import { clientId } from "./ids.js";
import { DeltaQueue, type Clock, type Envelope } from "./queue.js";
import { readEnvelope } from "./tolerant.js";
import { forgetNote } from "./memory.js";
import { companionMode, lastWorked } from "./recent.js";
import { subjectName, type SubjectName } from "./setup.js";
import { markFailed, markForgotten, markSending, optimistic, reduce, removeSketch } from "./reducer.js";
import { connectionKey, emptyConnection, emptyStore, type ConnectionState, type Store } from "./store.js";
import { StreamClient, type Endpoint, type StreamEnvelope, type StreamTimers } from "./stream/client.js";
import { CommandClient, CommandError, Unreachable } from "./stream/commands.js";

/** The event types this client renders; anything else is ignored and logged once (API-06). */
export const KNOWN_EVENTS = new Set([
  "turn.start", "message.accepted", "steer.applied", "status", "part.start", "text.delta", "text.reset", "reasoning.delta",
  "part.end", "tool.state", "board.reset", "board.knowns", "board.step", "board.highlight", "board.await", "visual.spec",
  "visual.step", "visual.focus", "card.open", "card.answered", "card.cancel", "notice", "error", "egress.notice",
  "session.updated", "memory.saved", "memory.forgotten", "progress.update", "turn.end", "voice.state", "voice.caption",
  // The S3 board performance and sketch events of D103 (VSL-40, VSL-43, VOC-47): known to the contract and to this
  // client, not drawn yet; the reducer leaves the store as it is for them, so no warning is logged.
  "board.annotate", "visual.sketch", "visual.await", "pacing.command",
  // VOC-53, D130: a spoken open-chat question's answer; SpeechTools hands it to Ask anything or the study Chat tab.
  "voice.answer",
  // SPEC §19.3.3: a durable button of the companion thread (Study's "Your tutor"), kept in the store's `actions`.
  "companion.action",
]);

/** The core's address and per-launch token, from the shell; `refresh` asks again after the core restarted. A profile's
 * launch also says how to open the profile again when the core closed it (`renew`, SEC-19). */
export interface Launch {
  endpoint(refresh: boolean): Promise<Endpoint>;
  renew?(): Promise<boolean>;
}

/** Commands for this launch, with profile renewal when the launch has it. */
export function commandsFor(launch: Launch, fetchImpl?: typeof fetch): CommandClient {
  const options: ConstructorParameters<typeof CommandClient>[0] = { endpoint: (refresh) => launch.endpoint(refresh) };
  if (launch.renew !== undefined) options.renew = () => launch.renew?.() ?? Promise.resolve(false);
  if (fetchImpl !== undefined) options.fetch = fetchImpl;
  return new CommandClient(options);
}

/** What a tutor session is for: its mode, subject, level, topic and where it was opened from. */
export type SessionTarget = Omit<CreateSession, "client_session_id">;

type Listener = () => void;

/** A new question in another subject, from the card that names it (AGT-15, D94): the subject, and the student's
 * words from the turn the card belongs to (empty when they are not in the store). */
export interface NewSubjectStart {
  subject: SubjectName;
  content: ContentBlock[];
}

/** Whether a listed session continues a target opened from `origin` on `topic`: an asked target with no topic takes
 * any session started by asking (its topic came from its question, D94); every other target matches the topic. A core
 * older than API 1.5 lists no origin, and its sessions match by topic as before. */
function continues(session: SessionList["sessions"][number], origin: string, topic: string | null): boolean {
  const listed = session.origin;
  if (origin === "ask" && topic === null && listed !== undefined) return listed === "ask";
  return (session.topic_id ?? null) === topic && (listed === undefined || listed === origin || origin === "resume");
}

/** The new question a confirm card naming another subject starts, or null for any other card (D94). */
export function newSubjectStart(store: Store, requestId: string): NewSubjectStart | null {
  const request = store.requests[requestId];
  const card = request?.card as { card_type?: string; subject?: string } | undefined;
  if (request === undefined || card?.card_type !== "confirm" || card.subject === undefined) return null;
  const subject = subjectName(card.subject);
  if (subject === null) return null;
  const turnId = store.parts[request.part_id]?.turn_id ?? null;
  const turn = turnId === null ? undefined : store.turns[turnId];
  const asked = (m: Store["messages"][string]) => turn !== undefined && (m.turn_id === turn.turn_id
    || (turn.client_message_id !== null && m.key === turn.client_message_id)
    || (turn.user_message_id !== null && m.message_id === turn.user_message_id));
  const message = Object.values(store.messages).find(asked);
  return { subject, content: message === undefined ? [] : message.content.filter((b) => b.type === "text") };
}

/** The store, for `useSyncExternalStore`: `getState` returns the same object until something changes. */
export class StoreHolder {
  private state: Store = emptyStore();
  private readonly listeners = new Set<Listener>();
  private readonly taps = new Set<(batch: readonly Envelope[]) => void>();

  getState = (): Store => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  update(change: (state: Store) => Store): void {
    const next = change(this.state);
    if (next === this.state) return;
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Every committed batch, after the store has folded it: the app's connectivity slice keeps the egress of a turn
   * there (UIA-23), so the session store stays the projection's fold (UIA-02). */
  onEnvelopes(tap: (batch: readonly Envelope[]) => void): () => void {
    this.taps.add(tap);
    return () => this.taps.delete(tap);
  }

  deliver(batch: readonly Envelope[]): void {
    for (const tap of [...this.taps]) tap(batch);
  }
}

export interface SessionOptions {
  fetch?: typeof fetch;
  timers?: StreamTimers;
  clock?: Clock;
  random?: () => number;
  now?: () => number;
}

interface PendingSend {
  key: string;
  body: { client_message_id: string; intent: string; content: ContentBlock[]; capture_id?: string; ref?: { kind: "board_step" | "visual_part"; id: string } };
}

export class SessionController {
  readonly commands: CommandClient;
  private readonly journal: SessionJournal;
  private readonly queue: DeltaQueue;
  private readonly stream: StreamClient;
  private readonly pending = new Map<string, PendingSend>();
  private readonly delivering = new Set<string>();
  private bannerTimer: unknown = null;
  /** Set by `close`: a snapshot or batch that lands afterwards belongs to a profile that may have gone (SEC-19). */
  private closed = false;
  private readonly timers: StreamTimers;
  private readonly now: () => number;

  constructor(
    private readonly launch: Launch,
    readonly holder: StoreHolder,
    readonly sessionId: string,
    options: SessionOptions = {},
  ) {
    this.timers = options.timers ?? { now: () => Date.now(), setTimeout: (f, ms) => globalThis.setTimeout(f, ms), clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>) };
    this.now = options.now ?? (() => Date.now());
    this.commands = commandsFor(launch, options.fetch);
    this.journal = new SessionJournal(this.commands, sessionId, () => this.holder.getState());
    this.queue = new DeltaQueue((batch) => this.commit(batch), options.clock === undefined ? {} : { clock: options.clock });
    const streamOptions: ConstructorParameters<typeof StreamClient>[0] = {
      sessionId,
      endpoint: (refresh) => launch.endpoint(refresh),
      cursor: () => {
        const cursor = this.holder.getState().cursors[sessionId];
        return cursor === undefined ? null : `${cursor.epoch}:${cursor.seq}`;
      },
      onEnvelope: (raw) => this.receive(raw),
      resync: () => this.loadSnapshot(),
      onState: (state, detail) => this.onState(state, detail.outage_ms),
      onAuthLost: () => {
        if (this.closed) return;
        for (const listener of [...this.authListeners]) listener();
      },
      timers: this.timers,
    };
    if (launch.renew !== undefined) streamOptions.renew = () => launch.renew?.() ?? Promise.resolve(false);
    if (options.fetch !== undefined) streamOptions.fetch = options.fetch;
    if (options.random !== undefined) streamOptions.random = options.random;
    this.stream = new StreamClient(streamOptions);
  }

  static async create(launch: Launch, body: SessionTarget, options: SessionOptions = {}): Promise<string> {
    const commands = commandsFor(launch, options.fetch);
    const key = clientId();
    const created = await commands.post<SessionCreated>("/v1/sessions", { ...body, client_session_id: key }, key);
    return created.session_id;
  }

  /** The session to continue for `body`: the profile's newest open session with the same subject and topic, from
   * `GET /v1/sessions` (API-23), so reopening a profile, or the core closing it after a pause, never starts the
   * student's work again; a new session only when there is none, or the list cannot be read. An asked session's topic
   * is the one its question was diagnosed in (D94), so a new question with no topic continues the subject's newest
   * asked session whatever its topic. */
  static async resume(launch: Launch, body: SessionTarget, options: SessionOptions = {}): Promise<string> {
    const topic = body.topic_id ?? null;
    const listed = await commandsFor(launch, options.fetch).get<SessionList>("/v1/sessions").catch((error: unknown) => {
      if (error instanceof CommandError && error.code === "profile_token_expired") throw error;
      return null;
    });
    const found = listed?.sessions.find((s) => !s.archived && !companionMode(s) && s.subject === body.subject && continues(s, body.origin.kind, topic));
    return found !== undefined ? found.session_id : SessionController.create(launch, body, options);
  }

  /** The open session the student last worked on, whatever its subject: the tutor's subject starts from it. */
  static async latest(launch: Launch, options: SessionOptions = {}): Promise<SessionList["sessions"][number] | null> {
    const listed = await commandsFor(launch, options.fetch).get<SessionList>("/v1/sessions").catch(() => null);
    return lastWorked(listed?.sessions ?? []);
  }

  /** The profile is over here (the core closed it and it could not be opened again): the stream has stopped. */
  onAuthLost(listener: () => void): () => void {
    this.authListeners.add(listener);
    return () => this.authListeners.delete(listener);
  }

  async open(): Promise<void> {
    let fresh = false;
    void this.journal.paint(this.holder.getState()).then((cached) => {
      if (cached !== null && !fresh && !this.closed) this.holder.update(() => cached);
    });
    try { await this.loadSnapshot(() => { fresh = true; }); } finally { fresh = true; }
    if (!this.closed) {
      this.recoverCompanionSends();
      this.stream.start();
    }
  }

  /** A journal preserves an interrupted message, but not the controller's retry map. Only companion text has
   * an unambiguous command to reconstruct. Reconcile with the core first, then let the learner explicitly retry
   * with the original idempotency key; never automatically resend an old memory command on opening Study. */
  private recoverCompanionSends(): void {
    const store = this.holder.getState();
    const session = store.sessions[this.sessionId];
    if (session?.mode !== "study_companion") return;
    for (const key of session.pending_ids) {
      const message = store.messages[key];
      if (message === undefined || message.message_id !== null || message.state === "accepted"
        || message.client_message_id !== key || message.content.length === 0
        || !message.content.every((block) => block.type === "text") || this.pending.has(key)) continue;
      this.pending.set(key, { key, body: { client_message_id: key, intent: "ask", content: message.content } });
      this.holder.update((s) => markFailed(s, key, "interrupted"));
    }
  }

  close(): void {
    this.closed = true;
    this.pending.clear();
    this.journal.close();
    this.stream.stop();
    this.queue.clear();
    if (this.bannerTimer !== null) this.timers.clearTimeout(this.bannerTimer);
  }

  /** Start a turn from a confirmed capture (INP-01): the capture's text, with \(…\) maths, is the message. */
  sendCapture(capture: CapturedQuestion, intent: string, content?: ContentBlock[]): string {
    assertConfirmed(capture);
    if (capture.session_id !== this.sessionId) throw new Error("capture_wrong_session");
    return this.send(content ?? [{ type: "text", text: capture.text }], intent, ["typed", "pasted_text"].includes(capture.source) ? undefined : capture.capture_id);
  }

  /** Send a student message (UIA-05). Returns the client id the bubble is keyed by. */
  send(content: ContentBlock[], intent = "answer", captureId?: string): string {
    const key = clientId();
    const send: PendingSend = { key, body: { client_message_id: key, intent, content, ...(captureId === undefined ? {} : { capture_id: captureId }) } };
    this.holder.update((s) => optimistic(s, this.sessionId, key, content));
    void this.deliver(send);
    return key;
  }

  /** A stable student step id distinguishes an edit from a new working line (INP-11). */
  sendBoardLine(stepId: string, latex: string): string {
    const lines = workingLines(latex);
    const line = lines[0];
    if (lines.length !== 1 || line === undefined) throw new Error("one_working_line_required");
    return this.send([{ type: "board_line", step_id: stepId, latex: line }], "board_line");
  }

  /** What the student has selected or hovers (UXS-58): `stp_….t2-t4` or `<visual_id>:<part_id>`, so "this" in a
   * message refers to it; null clears it. Ids only, never coordinates. */
  /** "Remove sketch" (VSL-43): the core removes it from the board, the history and replay; the store drops it too. */
  async removeSketch(sketchId: string): Promise<void> {
    if (this.closed) return;
    await this.commands.delete<unknown>(`/v1/sketches/${encodeURIComponent(sketchId)}`, clientId());
    this.holder.update((s) => removeSketch(s, sketchId));
  }

  async setSelection(ref: string | null): Promise<void> {
    if (this.closed) return;
    const key = clientId();
    const body = { ref: ref === null ? null : { kind: ref.includes(":") ? "visual_part" : "board_step", id: ref } };
    await this.commands.put<null>(`/v1/sessions/${this.sessionId}/selection`, body, key);
  }

  /** "Why?" on a board line (UXS-15): the tutor says why that line holds, with the rule it uses (SKL-08). With a term
   * range (`stp_….t2-t4`, UXS-58) it asks about exactly those terms. */
  askWhy(stepId: string, text: string): string {
    const key = clientId();
    const content: ContentBlock[] = [{ type: "text", text }];
    const send: PendingSend = { key, body: { client_message_id: key, intent: "why", content, ref: { kind: stepId.includes(":") ? "visual_part" : "board_step", id: stepId } } };
    this.holder.update((s) => optimistic(s, this.sessionId, key, content));
    void this.deliver(send);
    return key;
  }

  /** Up to thirty lines travel in one bounded text block; the core mints and checks their ordered steps. */
  sendWorking(text: string): string {
    const lines = workingLines(text);
    if (lines.length === 0) throw new Error("empty_working");
    return this.send([{ type: "text", text: lines.join("\n") }], "board_line");
  }

  /** A corrected draft replaces a refused command; it must not be replayed alongside the correction. */
  discardFailed(clientMessageId: string): void {
    if (this.holder.getState().messages[clientMessageId]?.state === "failed") this.pending.delete(clientMessageId);
  }

  /** "Try again" on a failed bubble: the same id, so the core never starts the turn twice (API-31). */
  retry(clientMessageId: string): void {
    const send = this.pending.get(clientMessageId);
    if (send === undefined) return;
    this.holder.update((s) => markSending(s, clientMessageId));
    void this.deliver(send);
  }

  /** Send the values the student set on a manipulable visual (VSL-42): checked by the verification tools, the result
   * arrives as the student's board line too. */
  async startManipulation(visualId: string, partId: string): Promise<{ step_id: string }> {
    const key = clientId();
    return this.commands.post<{ step_id: string }>(`/v1/visuals/${visualId}/practice`, { part_id: partId }, key);
  }

  async answerManipulation(visualId: string, stepId: string, values: { part_id: string; value: string }[]): Promise<{ status: string; check_id: string | null }> {
    const key = clientId();
    return this.commands.post<{ status: string; check_id: string | null }>(`/v1/visuals/${visualId}/answer`, { client_answer_id: key, step_id: stepId, values }, key);
  }

  /** Predict before you see (VSL-47): the learner's prediction for a predict-first simulation in this session, sent
   * once it is locked in (or their explanation of what it showed). The core decides whether it matched; the tutor's
   * reply, pointing at the result, arrives on the stream. A simulation shown outside a session sends nothing. */
  async simPrediction(visualId: string, event: api_commands.SimPrediction["event"], scenarioId: string, choice: string | null, text: string): Promise<void> {
    const key = clientId();
    const body: api_commands.SimPrediction = { client_event_id: key, event, scenario_id: scenarioId, choice, text: text.slice(0, 500) };
    await this.commands.post<unknown>(`/v1/sessions/${encodeURIComponent(this.sessionId)}/visuals/${encodeURIComponent(visualId)}/prediction`, body, key);
  }

  /** Answer an open card (API-14): the client answer id makes a repeat harmless. Confirming the card that names
   * another subject (AGT-15, D94) tells `onNewSubject` once the core has taken the answer. */
  async answerCard(requestId: string, answer: Record<string, unknown>): Promise<string> {
    if (this.closed || this.holder.getState().requests[requestId]?.state !== "open") return "ignored";
    const key = clientId();
    const start = answer.action === "confirm" ? newSubjectStart(this.holder.getState(), requestId) : null;
    this.holder.update((s) => {
      const request = s.requests[requestId];
      return request === undefined || request.state !== "open" ? s : { ...s, requests: { ...s.requests, [requestId]: { ...request, state: "answering" } } };
    });
    let result: { status: string };
    try {
      result = await this.commands.post<{ status: string }>(`/v1/cards/${requestId}/answer`, { client_answer_id: key, answer }, key);
    } catch (error) {
      if (!this.closed) this.holder.update((s) => {
        const request = s.requests[requestId];
        return request?.state !== "answering" ? s : { ...s, requests: { ...s.requests, [requestId]: { ...request, state: "open" } } };
      });
      throw error;
    }
    if (start !== null && result.status === "accepted" && !this.closed) {
      for (const listener of [...this.newSubjectListeners]) listener(start);
    }
    return result.status;
  }

  /** The student chose to start their question again in another subject (AGT-15, D94): the app opens a new session
   * there, started by asking, and sends the same words. */
  onNewSubject(listener: (start: NewSubjectStart) => void): () => void {
    this.newSubjectListeners.add(listener);
    return () => this.newSubjectListeners.delete(listener);
  }

  /** "Try again" on a turn with no reply (UXS-02 long-stalled): stop it, keep what was shown, and ask again with the
   * same words as a new message. */
  async retryTurn(turnId: string): Promise<void> {
    const state = this.holder.getState();
    const turn = state.turns[turnId];
    if (turn === undefined) return;
    if (turn.status === "streaming") await this.cancel(turnId).catch(() => undefined);
    const message = Object.values(state.messages).find((m) => (turn.client_message_id !== null && m.key === turn.client_message_id) || (turn.user_message_id !== null && m.message_id === turn.user_message_id));
    if (message !== undefined) this.send(message.content, turn.intent === "ask" ? "ask" : "answer");
  }

  /** Undo a note saved at the student's request (MEM-11): the core deletes it and erases its text; the announcement
   * turns to "forgotten" at once, before the core's `memory.forgotten` arrives. */
  async forgetMemory(noteId: string): Promise<void> {
    await forgetNote(this.commands, noteId);
    if (!this.closed) this.holder.update((s) => markForgotten(s, noteId));
  }

  /** Switch the session's mode (UXS-18, PED-02): the board and transcript stay; Solution is open whenever asked (D87),
   * only a running mock exam refuses it. The new mode arrives on the stream as `session.updated`. */
  async setMode(mode: UpdateSession["mode"] & string): Promise<void> {
    const body: UpdateSession = { mode };
    await this.commands.patch<unknown>(`/v1/sessions/${encodeURIComponent(this.sessionId)}`, body, clientId());
  }

  /** Rename the chat (UXS-68): its automatic title stays until the student renames it. The words are guarded by the
   * core like any text the student writes; the title shows at once and the core confirms it as `session.updated`. */
  async rename(title: string): Promise<void> {
    const words = title.replace(/\s+/g, " ").trim().slice(0, 120);
    if (words === "") return;
    const body: UpdateSession = { title: words };
    await this.commands.patch<unknown>(`/v1/sessions/${encodeURIComponent(this.sessionId)}`, body, clientId());
    this.holder.update((s) => {
      const rec = s.sessions[this.sessionId];
      return rec === undefined ? s : { ...s, sessions: { ...s.sessions, [this.sessionId]: { ...rec, title: words } } };
    });
  }

  async cancel(turnId: string): Promise<CancelResult> {
    this.queue.flush();
    return this.commands.post<CancelResult>(`/v1/turns/${turnId}/cancel`, {}, clientId());
  }

  private async deliver(send: PendingSend): Promise<void> {
    if (this.closed || this.delivering.has(send.key)) return;
    this.delivering.add(send.key);
    this.pending.set(send.key, send);
    try {
      await this.commands.post<TurnAccepted>(`/v1/sessions/${this.sessionId}/turns`, send.body, send.key);
      this.pending.delete(send.key);
    } catch (error) {
      if (this.closed) return;
      if (error instanceof Unreachable) {
        const state = this.holder.getState().connection[connectionKey(this.sessionId)]?.state;
        // A command can fail while the independently opened event stream remains healthy. In that case no
        // reconnect event will arrive to retry it, so expose the existing retry instead of spinning forever.
        if (state === "live" || state === "replaying") this.holder.update((s) => markFailed(s, send.key, "send_unconfirmed"));
        return; // retained for explicit retry, or reconnection when the stream is also unavailable
      }
      this.holder.update((s) => markFailed(s, send.key, error instanceof CommandError ? error.code : "internal_error",
        error instanceof CommandError ? error.details : undefined));
    } finally {
      this.delivering.delete(send.key);
    }
  }

  private async loadSnapshot(beforeApply?: () => void): Promise<void> {
    this.queue.clear();
    const snapshot = await this.commands.get<CoreSnapshot>(`/v1/sessions/${this.sessionId}/snapshot`);
    if (this.closed) return;
    beforeApply?.();
    this.holder.update((s) => hydrate(s, snapshot));
    this.journal.fresh(snapshot);
  }

  private readonly warned = new Set<string>();
  private readonly authListeners = new Set<() => void>();
  private readonly progressListeners = new Set<(kcId: string) => void>();
  private readonly newSubjectListeners = new Set<(start: NewSubjectStart) => void>();

  /** A KC's mastery moved (`progress.update`, LRN-34): told as the event arrives, before the batch is committed, so
   * the subject map can re-read at once (UXS-22). */
  onProgress(listener: (kcId: string) => void): () => void {
    this.progressListeners.add(listener);
    return () => this.progressListeners.delete(listener);
  }

  /** Every envelope passes the tolerant reader (API-35) before it is queued; a rejected one asks for a resync. */
  private receive(raw: unknown): void {
    const read = readEnvelope(raw);
    if (read.kind === "accept") {
      this.queue.push(read.envelope as unknown as Envelope);
      const event = (read.envelope as { event?: { type?: unknown; kc_id?: unknown } }).event;
      if (event?.type === "progress.update" && typeof event.kc_id === "string" && !this.closed) {
        for (const listener of [...this.progressListeners]) listener(event.kc_id);
      }
    } else if (read.kind === "skip") {
      this.warnOnce("unknown_event_ignored", read.type); // a newer core never breaks an older client (API-06)
    } else {
      this.warnOnce("event_rejected", read.errors[0] ?? "");
      void this.loadSnapshot();
    }
  }

  private warnOnce(code: string, detail: string): void {
    if (this.warned.has(`${code}:${detail}`)) return;
    this.warned.add(`${code}:${detail}`);
    console.warn(code, detail);
  }

  private commit(batch: Envelope[]): void {
    if (this.closed) return;
    for (const envelope of batch) {
      const type = envelope.event.type;
      if (!KNOWN_EVENTS.has(type)) this.warnOnce("unknown_event_ignored", type); // in the contract, not rendered here
    }
    const last = batch[batch.length - 1];
    this.holder.update((state) => {
      let next = state;
      for (const envelope of batch) next = reduce(next, envelope);
      if (last !== undefined) next = this.withConnection(next, (c) => ({ ...c, clock_offset_ms: last.ts - this.now(), last_event_ts: last.ts }));
      return next;
    });
    this.holder.deliver(batch);
    this.journal.append(batch);
    if (this.holder.getState().connection[connectionKey(this.sessionId)]?.resync) {
      void this.loadSnapshot();
    }
  }

  private withConnection(state: Store, change: (c: Store["connection"][string]) => Store["connection"][string]): Store {
    const key = connectionKey(this.sessionId);
    return { ...state, connection: { ...state.connection, [key]: change(state.connection[key] ?? emptyConnection()) } };
  }

  private onState(state: ConnectionState, outageMs: number): void {
    if (this.closed) return;
    this.holder.update((s) => this.withConnection(s, (c) => ({ ...c, state, banner: c.banner && state !== "live" && state !== "replaying" })));
    if (state === "live" || state === "replaying") {
      if (this.bannerTimer !== null) this.timers.clearTimeout(this.bannerTimer);
      this.bannerTimer = null;
      // Sends the stream dropped go again with their key; a send the core refused waits for "Try again" (§10.4).
      const messages = this.holder.getState().messages;
      for (const send of [...this.pending.values()]) if (messages[send.key]?.state !== "failed") void this.deliver(send);
      return;
    }
    // Unreachable or resyncing: the banner appears only if it lasts past the threshold (UIA-07).
    if ((state === "reconnecting" || state === "connecting" || state === "resyncing") && this.bannerTimer === null) {
      const wait = Math.max(0, CLIENT_KEYS.ui_banner_after_ms - outageMs);
      this.bannerTimer = this.timers.setTimeout(() => {
        this.bannerTimer = null;
        const current = this.holder.getState().connection[connectionKey(this.sessionId)];
        if (current !== undefined && current.state !== "live" && current.state !== "replaying") {
          this.holder.update((s) => this.withConnection(s, (c) => ({ ...c, banner: true })));
        }
      }, wait);
    }
  }
}

export type { StreamEnvelope };
