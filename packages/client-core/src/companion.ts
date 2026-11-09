// The personal learning agent on the client (SPEC §19, W8). Phase 1 is the Last time card on Home and in Study:
// `GET /v1/companion/opener` answers with facts the core chose by code (the last session's day, mode and subject,
// review cards due, the nearest exam's distance, a research pack ready) and the sentence its templates made of them.
// The card shows that sentence; nothing here words a fact or adds a number. A core that does not serve the route (an
// older one) leaves the card out.
//
// Phase 2 is the thread, **Your tutor** in Study (§19.3.3): `POST /v1/companion` opens or resumes the day's episode, an
// ordinary session of mode `study_companion`, and answers with its opener (delivered here only, never on the stream, so
// it is shown exactly once, as the thread's first bubble) and the rolling summary. The thread's turns go through the
// session's own pieces (`SessionController`, the shared store), as the tutor screen's do; its buttons are the durable
// `companion.action` events the store keeps in `actions`, so they survive a reconnect and a restart. The thread lives
// on this computer (JM-D27). Everything here belongs to the unlocked profile and is dropped when it closes (SEC-19).
//
// Phase 3 adds check-ins (§19.3.4, §19.5.5): `GET /v1/checkins/due` names at most one commitment for today, shown as a
// card on Home with the commitment's own words as plain text. Opening it (`POST /v1/checkins/{id}/open`) answers like
// `POST /v1/companion` and shows the thread with the check-in as the opener's `checkin_id`; dismissing it
// (`POST /v1/checkins/{id}/dismiss`) removes the card. A desktop notification (JM-D24) is sent only while the profile's
// `checkin_notifications` is on and the OS permission was granted, and carries the commitment's id and kind only: the
// shell words it from the kind's fixed template, never from the commitment's text. A denied permission turns the
// setting off with a notice; a lock or profile switch drops the card and cancels what the shell showed, by tag.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { SessionController, type SessionOptions, type StoreHolder } from "./session.js";
import type { ActionRec, MessageRec, Store, TurnRec } from "./store.js";
import { CommandError } from "./stream/commands.js";

export type Opener = api_commands.Opener;
export type OpenerFacts = api_commands.OpenerFacts;
export type CompanionSummary = api_commands.CompanionSummary;
export type CompanionOpened = api_commands.CompanionOpened;
export type Commitment = api_commands.Commitment;
export type CommitmentKind = Commitment["kind"];
/** The kinds of check-in, in the contract's order. */
export const CHECKIN_KINDS: readonly CommitmentKind[] = ["event_checkin", "deadline_check", "open_loop", "due_reviews", "pack_ready"];
/** The copy key of each kind's notification body: the shell's fixed template for the kind (`notify_checkin`), kept in
 * step with the catalogue by the shell's own test. The commitment's text never reaches a notification. */
export const CHECKIN_NOTIFICATION_KEYS: Readonly<Record<CommitmentKind, string>> = {
  event_checkin: "home.checkin.notify.event_checkin",
  deadline_check: "home.checkin.notify.deadline_check",
  open_loop: "home.checkin.notify.open_loop",
  due_reviews: "home.checkin.notify.due_reviews",
  pack_ready: "home.checkin.notify.pack_ready",
};
/** How often the due query is asked again while the app is open (§19.5.5's heartbeat runs at the same pace). */
export const CHECKIN_REFRESH_MS = 30 * 60 * 1000;
/** A durable button of the thread: what it opens, with what, and the copy key of its label. */
export type CompanionAction = ActionRec;
export type CompanionActionKind = ActionRec["action"];
/** Every action the engine may send, in the contract's order; each has the copy key `study.companion.action.<action>`. */
export const COMPANION_ACTIONS: readonly CompanionActionKind[] = [
  "open_tutor", "open_practice", "open_study_job", "open_add_source", "open_research", "open_plan", "open_knows", "open_study_deck", "open_moment",
];
/** The longest message the thread sends (the Study ask box's bound). */
export const COMPANION_MESSAGE_MAX = 4000;
type Listener = () => void;

export interface CompanionView {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  opener: Opener | null;
}

export const EMPTY_COMPANION: CompanionView = { status: "idle", opener: null };

/** Why the open asks the online tutor's first-use notice (ARC-10): never answered, or declined on a computer where no
 * tutor can run without the online one. */
export type CompanionNotice = "notice_required" | "notice_declined";
const NOTICES: readonly string[] = ["notice_required", "notice_declined"] satisfies CompanionNotice[];

/** The thread's episode: the session it runs in, its opener and the rolling summary, as the open answered. */
export interface CompanionThreadView {
  /** `unavailable`: a core without the thread (404); `failed`: it could not open now (`error` says why); `notice`: the
   * open waits for the learner's answer to the online tutor's first-use notice (`notice` says why). */
  status: "idle" | "opening" | "ready" | "failed" | "unavailable" | "notice";
  session_id: string | null;
  thread_id: string | null;
  /** True when today's episode was already open (a second open on one day returns the same session). */
  resumed: boolean;
  opener: Opener | null;
  summary: CompanionSummary | null;
  /** The error code of the last open that failed (`model_unavailable`, `feature_disabled`, …). */
  error: string | null;
  notice: CompanionNotice | null;
  can_reply?: boolean;
  history?: api_commands.CompanionHistory["items"];
  history_cursor?: string | null;
  history_loading?: boolean;
  history_error?: boolean;
  /** Ask about this moment (SPEC §20.5 step 4): the pending turn context the open carried, shown above the composer
   * until the learner's next message consumes it. */
  context?: { source_id: string; at_ms: number; title: string } | null;
}

export const EMPTY_THREAD: CompanionThreadView = { status: "idle", session_id: null, thread_id: null, resumed: false, opener: null, summary: null, error: null, notice: null };

/** The OS side of check-in notifications (JM-D24): the desktop shell, or nothing where there is none (a browser). */
export interface CheckinNotifier {
  /** Ask for (or read) the OS permission to show notifications. */
  permission(): Promise<"granted" | "denied">;
  /** Show the kind's fixed notification, tagged with the commitment's id. Only the id and the kind cross. */
  notify(checkin: { commitment_id: string; kind: CommitmentKind }): Promise<void>;
  /** Withdraw every check-in notification this run showed, by tag (a lock or a profile switch). */
  cancel(): Promise<void>;
}

/** Today's check-in for the Home card: at most one (§19.5.5). */
export interface CheckinView {
  /** `unavailable`: a core without check-ins (404 or 405), so the card is never shown. */
  status: "idle" | "ready" | "unavailable";
  item: Commitment | null;
  /** The OS refused notifications, so `checkin_notifications` was turned off: said once, on the card. */
  notice: "notifications_denied" | null;
}

export const EMPTY_CHECKIN: CheckinView = { status: "idle", item: null, notice: null };

/** A reply that holds as the contract's `Opener`, or a thrown error naming what does not. */
export function readOpener(value: unknown): Opener {
  const problems = errors("api/commands#/$defs/Opener", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems.slice(0, 3) });
  return value as Opener;
}

/** `POST /v1/companion`'s reply as the contract's `CompanionOpened`, or a thrown error naming what does not hold. */
export function readCompanionOpened(value: unknown): CompanionOpened {
  const problems = errors("api/commands#/$defs/CompanionOpened", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems.slice(0, 3) });
  return value as CompanionOpened;
}

/** `GET /v1/companion/summary`'s reply as the contract's `CompanionSummary`, or a thrown error. */
export function readCompanionSummary(value: unknown): CompanionSummary {
  const problems = errors("api/commands#/$defs/CompanionSummary", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems.slice(0, 3) });
  return value as CompanionSummary;
}

/** `GET /v1/checkins/due`'s reply as the contract's `CheckinsDue`, or a thrown error naming what does not hold. */
export function readCheckinsDue(value: unknown): api_commands.CheckinsDue {
  const problems = errors("api/commands#/$defs/CheckinsDue", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems.slice(0, 3) });
  return value as api_commands.CheckinsDue;
}

const checkinPath = (id: string, action: "open" | "dismiss") => `/v1/checkins/${encodeURIComponent(id)}/${action}`;

const unavailable = (error: unknown) => error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
const codeOf = (error: unknown) => (error instanceof CommandError ? error.code : "unreachable");
const noticeOf = (error: unknown): CompanionNotice | null => {
  const reason = error instanceof CommandError && error.code === "feature_disabled" ? error.details?.reason : undefined;
  return typeof reason === "string" && NOTICES.includes(reason) ? (reason as CompanionNotice) : null;
};
/** An open's request, with the learner's answer to the online tutor's notice when it carries one. */
type OpenRequest = (cloudNotice?: "accepted" | "declined") => Promise<unknown>;

/** One bubble of the thread, as plain text: the student's words, or the tutor's reply to them. Nothing here is markup;
 * the thread renders every string as text (the model's words never become elements). */
export type CompanionEntry =
  | { kind: "student"; key: string; text: string; state: MessageRec["state"]; error_code: string | null }
  | {
    kind: "tutor"; key: string; turn_id: string; text: string;
    /** The copy keys of the turn's notices (a refused message's fixed notice, the wellbeing template's line). */
    notices: string[];
    status: TurnRec["status"];
    /** The turn's current phase while it runs (`recalling`, `tool`, …), for its status line. */
    phase: string | null;
    error: string | null;
  };

const studentText = (message: MessageRec) => message.content.flatMap((b) => (b.type === "text" ? [b.text] : [])).join("\n");

/** The thread's bubbles in order: each turn's student message, then the turn; messages not yet answered last (as the
 * tutor's transcript orders them, UIA-05). */
export function companionThread(store: Store, sessionId: string): CompanionEntry[] {
  const session = store.sessions[sessionId];
  if (session === undefined) return [];
  const out: CompanionEntry[] = [];
  const placed = new Set<string>();
  const byId = new Map(Object.values(store.messages).map((m) => [m.message_id, m.key]));
  const student = (key: string) => {
    const message = store.messages[key];
    if (message === undefined || placed.has(key)) return;
    placed.add(key);
    out.push({ kind: "student", key, text: studentText(message), state: message.state, error_code: message.error_code ?? null });
  };
  const notices = new Map<string, string[]>();
  for (const part of Object.values(store.parts)) {
    if (part.kind !== "notice" || part.turn_id === null) continue;
    const key = part.notice?.message_key;
    if (typeof key === "string") notices.set(part.turn_id, [...(notices.get(part.turn_id) ?? []), key]);
  }
  for (const turnId of session.turn_ids) {
    const turn = store.turns[turnId];
    if (turn === undefined) continue;
    const key = turn.client_message_id !== null && store.messages[turn.client_message_id] !== undefined ? turn.client_message_id
      : turn.user_message_id !== null ? byId.get(turn.user_message_id) ?? null : null;
    if (key !== null) student(key);
    const text = turn.part_ids.flatMap((id) => {
      const part = store.parts[id];
      return part !== undefined && part.kind === "text" && part.status !== "retracted" && part.text !== "" ? [part.text] : [];
    }).join("\n\n");
    out.push({
      kind: "tutor", key: turnId, turn_id: turnId, text, notices: notices.get(turnId) ?? [], status: turn.status,
      phase: turn.status === "streaming" ? turn.phase?.phase ?? null : null, error: turn.error?.code ?? null,
    });
  }
  for (const key of session.pending_ids) student(key);
  return out;
}

/** Durable buttons, oldest first. Repeated turns may suggest the same destination: render it once while keeping
 * every event in the store. Different jobs, questions or other payloads remain distinct. */
export function companionActions(store: Store, sessionId: string): CompanionAction[] {
  const seen = new Set<string>();
  return Object.values(store.actions).filter((a) => a.session_id === sessionId)
    .sort((a, b) => (a.action_id < b.action_id ? -1 : a.action_id > b.action_id ? 1 : 0))
    .filter((a) => {
      const key = JSON.stringify([a.action, a.label_key, Object.entries(a.payload).sort(([x], [y]) => x.localeCompare(y))]);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export class CompanionController {
  private view: CompanionView = EMPTY_COMPANION;
  private thread: CompanionThreadView = EMPTY_THREAD;
  private session: SessionController | null = null;
  private opening: Promise<void> | null = null;
  /** The open the online tutor's notice was asked for, sent again with the answer. */
  private asked: OpenRequest | null = null;
  private checkin: CheckinView = EMPTY_CHECKIN;
  /** The check-ins this run has notified about, so a refresh never shows one twice. */
  private readonly notified = new Set<string>();
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController, private readonly options: SessionOptions & { notifier?: CheckinNotifier } = {}) {
    profiles.onClear(() => {
      this.session?.close();
      this.session = null;
      this.opening = null;
      this.asked = null;
      this.set(EMPTY_COMPANION);
      this.setThread(EMPTY_THREAD);
      this.setCheckin(EMPTY_CHECKIN);
      // What the shell showed for the profile that closed is withdrawn (§19.3.4); a pending notify is dropped by the
      // generation check in `notify`.
      if (this.notified.size > 0) void this.options.notifier?.cancel().catch(() => undefined);
      this.notified.clear();
    });
  }

  /** The store the thread's session folds into: the app's one store (UIA-02). */
  get holder(): StoreHolder {
    return this.profiles.holder;
  }

  getView = (): CompanionView => this.view;
  getThread = (): CompanionThreadView => this.thread;
  getCheckin = (): CheckinView => this.checkin;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private emit(): void {
    for (const listener of [...this.listeners]) listener();
  }

  private set(next: CompanionView): void {
    if (next === this.view) return;
    this.view = next;
    this.emit();
  }

  private setThread(next: CompanionThreadView): void {
    if (next === this.thread) return;
    this.thread = next;
    this.emit();
  }

  private setCheckin(next: CheckinView): void {
    if (next === this.checkin) return;
    this.checkin = next;
    this.emit();
  }

  /** Read the card again: the facts are recomputed on every open. What is shown stays until the new reply lands, and
   * a reply that lands after a lock or switch is dropped. */
  async loadOpener(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.opener === null) this.set({ status: "loading", opener: null });
    try {
      const opener = readOpener(await this.profiles.commands.get<unknown>("/v1/companion/opener"));
      if (generation === this.profiles.generation) this.set({ status: "ready", opener });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.set(this.view.opener !== null ? { ...this.view, status: "ready" } : { status: unavailable(error) ? "unavailable" : "failed", opener: null });
    }
  }

  /** Open or resume today's episode (`POST /v1/companion`) and its stream. Two opens at once share one request; an
   * open on the same day returns the same session, so the stream already running is kept. */
  open(): Promise<void> {
    this.opening ??= this.openOnce((cloudNotice) => this.profiles.commands.post<unknown>("/v1/companion", cloudNotice ? { cloud_notice: cloudNotice } : {}, clientId())).finally(() => { this.opening = null; });
    return this.opening;
  }

  /** Ask about this moment (SPEC §20.1, §20.5 step 4): open or resume today's episode with the moment as its pending
   * turn context (`CompanionOpen.context`). The core keeps the ±30 s lines for the learner's next message; nothing is
   * sent until they send. `title` is shown above the composer ("About 4:12 in <title>"). */
  openWithContext(context: { source_id: string; at_ms: number }, title: string): Promise<void> {
    const before = this.opening;
    const generation = this.profiles.generation;
    const settled: Promise<void> = (async () => {
      await before?.catch(() => undefined);
      if (generation !== this.profiles.generation) return;
      await this.openOnce((cloudNotice) => this.profiles.commands.post<unknown>("/v1/companion", { context, ...(cloudNotice ? { cloud_notice: cloudNotice } : {}) }, clientId()));
      if (generation === this.profiles.generation && this.thread.status === "ready") this.setThread({ ...this.thread, context: { ...context, title } });
    })().finally(() => { if (this.opening === settled) this.opening = null; });
    this.opening = settled;
    return settled;
  }

  /** The learner's answer to the online tutor's first-use notice (ARC-10) the open asked: the same open again, with
   * the answer, which the core records for the profile. Declined, the thread stays on this computer where a tutor can
   * run here; where none can, the notice is shown again saying why. */
  answerNotice(answer: "accepted" | "declined"): Promise<void> {
    const request = this.asked;
    if (request === null || this.thread.notice === null) return Promise.resolve();
    const before = this.opening;
    const generation = this.profiles.generation;
    const settled: Promise<void> = (async () => {
      await before?.catch(() => undefined);
      if (generation !== this.profiles.generation) return;
      await this.openOnce(request, answer);
    })().finally(() => { if (this.opening === settled) this.opening = null; });
    this.opening = settled;
    return settled;
  }

  /** Open the check-in `commitmentId` (`POST /v1/checkins/{id}/open`): the reply is the thread's episode, as `open()`'s,
   * with the check-in as its opener's `checkin_id`, and the Home card leaves. An open already running is waited for
   * first, so the check-in's opener is the one shown. A check-in the core no longer has leaves the card too. */
  openCheckin(commitmentId: string): Promise<void> {
    const before = this.opening;
    const generation = this.profiles.generation;
    if (this.checkin.item?.commitment_id === commitmentId) this.setCheckin({ ...this.checkin, item: null });
    const settled: Promise<void> = (async () => {
      await before?.catch(() => undefined);
      if (generation !== this.profiles.generation) return;
      await this.openOnce((cloudNotice) => this.profiles.commands.post<unknown>(checkinPath(commitmentId, "open"), cloudNotice ? { cloud_notice: cloudNotice } : undefined, clientId()));
    })().finally(() => { if (this.opening === settled) this.opening = null; });
    // An `open()` while this one runs waits for it, so the check-in's opener is the one shown.
    this.opening = settled;
    return settled.then(() => {
      // Not opened: a check-in that is still due comes back on the card.
      if (generation === this.profiles.generation && this.thread.status === "failed") void this.loadCheckins();
    });
  }

  private async openOnce(request: OpenRequest, cloudNotice?: "accepted" | "declined"): Promise<void> {
    const generation = this.profiles.generation;
    if (this.thread.session_id === null) this.setThread({ ...this.thread, status: "opening", error: null, notice: null });
    try {
      const opened = readCompanionOpened(await request(cloudNotice));
      if (generation !== this.profiles.generation) return;
      this.asked = opened.notice ? request : null;
      const same = this.thread.thread_id === opened.thread_id;
      this.setThread({
        status: "ready", session_id: opened.session_id, thread_id: opened.thread_id, resumed: opened.resumed, opener: opened.opener, summary: opened.summary, error: null, notice: opened.notice ?? null,
        can_reply: opened.can_reply ?? true, history: same ? this.thread.history ?? [] : [],
        history_cursor: same ? this.thread.history_cursor ?? null : null, history_loading: false, history_error: false,
      });
      void this.loadHistory(false);
      if (this.session?.sessionId === opened.session_id) return;
      this.session?.close();
      const session = new SessionController(this.profiles.launch, this.profiles.holder, opened.session_id, this.options);
      session.onAuthLost(() => void this.profiles.expire()); // the profile could not be opened again (SEC-19)
      this.session = session;
      await session.open();
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      // The episode answered but its snapshot could not be read: no stream runs, and the next open tries again.
      if (this.session !== null && this.session.sessionId === this.thread.session_id) { this.session.close(); this.session = null; }
      // The online tutor's first-use notice (ARC-10): the thread shows it, and its answer goes with the same open.
      const notice = noticeOf(error);
      this.asked = notice !== null ? request : null;
      if (notice !== null) { this.setThread({ ...this.thread, status: "notice", error: null, notice }); return; }
      this.setThread({ ...this.thread, status: unavailable(error) ? "unavailable" : "failed", error: codeOf(error), notice: null });
    }
  }

  /** Page older retained conversation without opening a model or changing the current episode. */
  loadEarlier = (): Promise<void> => this.loadHistory(true);

  private async loadHistory(older: boolean): Promise<void> {
    const generation = this.profiles.generation;
    const threadId = this.thread.thread_id;
    if (threadId === null || this.thread.history_loading) return;
    const cursor = older ? this.thread.history_cursor : null;
    this.setThread({ ...this.thread, history_loading: true, history_error: false });
    try {
      const found = await this.profiles.commands.get<unknown>(`/v1/companion/history?limit=50${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`);
      if (generation !== this.profiles.generation || this.thread.thread_id !== threadId) return;
      if (errors("api/commands#/$defs/CompanionHistory", found).length) throw new Error("invalid history");
      const page = found as api_commands.CompanionHistory;
      const seen = new Set<string>();
      const items = [...page.items, ...(older ? this.thread.history ?? [] : [])].filter((item) => {
        if (seen.has(item.id)) return false;
        seen.add(item.id);
        return true;
      });
      this.setThread({ ...this.thread, history: items, history_cursor: page.next_cursor, history_loading: false });
    } catch {
      if (generation === this.profiles.generation && this.thread.thread_id === threadId)
        this.setThread({ ...this.thread, history_loading: false, history_error: true });
    }
  }

  /** Read today's check-in (`GET /v1/checkins/due`): on Home's open and every `CHECKIN_REFRESH_MS` while the app is
   * open. A core without the route hides the card; a failed read keeps what is shown. A reply that lands after a lock
   * or switch is dropped. A new check-in is notified once, when the settings and the OS allow it. */
  async loadCheckins(): Promise<void> {
    let generation: number;
    try { generation = this.profiles.scope().generation; } catch { return; }
    try {
      const due = readCheckinsDue(await this.profiles.commands.get<unknown>("/v1/checkins/due"));
      if (generation !== this.profiles.generation) return;
      const item = due.items[0] ?? null;
      this.setCheckin({ ...this.checkin, status: "ready", item });
      if (item !== null) await this.notify(item, generation);
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      if (unavailable(error)) this.setCheckin({ ...EMPTY_CHECKIN, status: "unavailable" });
    }
  }

  /** "Not now" on the card (`POST /v1/checkins/{id}/dismiss`): it leaves at once and stays gone (the core remembers the
   * dismissal); a refusal other than "gone already" brings it back. */
  async dismissCheckin(commitmentId: string): Promise<boolean> {
    const generation = this.profiles.generation;
    const before = this.checkin;
    if (before.item?.commitment_id !== commitmentId) return false;
    this.setCheckin({ ...before, item: null, notice: null });
    try {
      await this.profiles.commands.post<unknown>(checkinPath(commitmentId, "dismiss"), undefined, clientId());
      return true;
    } catch (error) {
      if (generation === this.profiles.generation && !(error instanceof CommandError && error.code === "not_found")) this.setCheckin(before);
      return false;
    }
  }

  /** The notification for a due check-in (JM-D24): only with `checkin_notifications` on and the OS permission granted;
   * a refused permission turns the setting off and says so on the card. */
  private async notify(item: Commitment, generation: number): Promise<void> {
    const notifier = this.options.notifier;
    if (notifier === undefined || this.notified.has(item.commitment_id)) return;
    let settings = this.profiles.getView().settings;
    if (settings === null) settings = await this.profiles.loadSettings().catch(() => null);
    if (generation !== this.profiles.generation || settings?.checkin_notifications !== true) return;
    this.notified.add(item.commitment_id);
    const permission = await notifier.permission().catch(() => "denied" as const);
    if (generation !== this.profiles.generation) return;
    if (permission !== "granted") {
      this.notified.delete(item.commitment_id);
      await this.profiles.setPreferences({ checkin_notifications: false }).catch(() => undefined);
      if (generation === this.profiles.generation) this.setCheckin({ ...this.checkin, notice: "notifications_denied" });
      return;
    }
    await notifier.notify({ commitment_id: item.commitment_id, kind: item.kind }).catch(() => undefined);
  }

  /** Read the rolling summary again (`GET /v1/companion/summary`); a failed read keeps the one shown. */
  async loadSummary(): Promise<void> {
    const generation = this.profiles.generation;
    try {
      const summary = readCompanionSummary(await this.profiles.commands.get<unknown>("/v1/companion/summary"));
      if (generation === this.profiles.generation) this.setThread({ ...this.thread, summary });
    } catch { /* the summary is the device opener's to read; the thread works without it */ }
  }

  /** Send a message to the thread: the bubble shows at once and is sent with intent `ask` (UIA-05). Null when the
   * thread is not open or the message is empty. */
  send(text: string): string | null {
    const words = text.trim().slice(0, COMPANION_MESSAGE_MAX);
    if (this.session === null || words === "") return null;
    const id = this.session.send([{ type: "text", text: words }], "ask");
    // The pending moment goes with this message (the core discards it after the turn): the line above the composer leaves.
    if (id !== null && this.thread.context) this.setThread({ ...this.thread, context: null });
    return id;
  }

  /** "Try again" on a message the core refused or never took: the same id, so the turn never starts twice. */
  retry(clientMessageId: string): void {
    this.session?.retry(clientMessageId);
  }

  /** Study closes: the thread's stream stops. What was shown stays in the store; the next open resumes the episode. */
  close(): void {
    this.session?.close();
    this.session = null;
  }
}
