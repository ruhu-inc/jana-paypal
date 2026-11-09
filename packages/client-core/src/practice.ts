// Practice (UXS-24, API-27, ASM-02): one question at a time from a practice set, marked at once by the core's
// deterministic marker, with every try kept on screen and the mastery change after each item; "Help me" opens the tutor
// with the item loaded. The set comes from `POST /v1/practice/sets` (adaptive, or the ideas the student chose), each
// answer goes to `.../answers` (`PracticeAnswer` → `PracticeMarked`, with the next item) and help to `.../help`
// (`SessionCreated`). Replies are checked against the command contract at the boundary. A core that does not serve
// practice sets yet answers `not_found`: the view says so (`unavailable`) and lessons' quick checks still work.
// Help before the answer (ASM-28, D120): a first miss keeps the question for a second try and shows no answer; the
// worked answer (with its check) comes with the second miss, or on "Don't know" (`.../reveal`), both proposed in
// contract-additions-m45d.json and checked here against the adopted WorkedExampleView.
// Everything here belongs to the unlocked profile and is dropped when it closes (SEC-19).
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { CLIENT_KEYS } from "./generated/keys.js";
import { clientId } from "./ids.js";
import type { PartAnswer, PracticeItem } from "./learn.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

/** A marked answer as the core sends it: the adopted `PracticeMarked`, plus what M16's proposed contract additions add
 * (contract-additions-assessment.json): a "not_checked" verdict and the reflection card. */
export type PracticeMarked = Omit<api_commands.PracticeMarked, "verdict"> & {
  verdict: api_commands.PracticeMarked["verdict"] | "not_checked";
  reflection?: unknown;
  /** The worked answer, after a second miss only (ASM-28; contract-additions-m45d.json). */
  worked?: api_commands.WorkedExampleView;
};

/** The reply to "Don't know" (ASM-28; contract-additions-m45d.json `PracticeRevealed`). */
export interface PracticeRevealed {
  item_id: string;
  worked: api_commands.WorkedExampleView;
  mastery_change: api_commands.PracticeMarked["mastery_change"];
  next_item: PracticeItem | null;
}

export interface PracticeTry {
  answers: PartAnswer[];
  marked: PracticeMarked;
}

export interface PracticeView {
  status: "idle" | "starting" | "ready" | "done" | "unavailable" | "failed";
  setId: string | null;
  subject: string | null;
  /** The question shown (from 1) and how many the set holds. */
  number: number;
  total: number;
  item: PracticeItem | null;
  /** The tries on the question shown, oldest first. */
  tries: PracticeTry[];
  /** The next question, once the core has given it; null at the end of the set. */
  next: PracticeItem | null;
  marking: boolean;
  /** The last answer could not be marked: it is still there to send again. */
  markFailed: boolean;
  /** The core has moved past the question shown (M16 marks one answer per question, then gives the next): no more
   * tries on it, only "Next question". */
  closed: boolean;
  /** The answer being written for the question shown, kept while the tutor helps ("Help me" returns to it, UXS-24). */
  draft: unknown;
  /** The question's worked answer, once earned: after the second miss or on "Don't know" (ASM-28). */
  worked: api_commands.WorkedExampleView | null;
  /** "Don't know" is being sent. */
  revealing: boolean;
}

export const EMPTY_PRACTICE: PracticeView = { status: "idle", setId: null, subject: null, number: 0, total: 0, item: null, tries: [], next: null, marking: false, markFailed: false, closed: false, draft: null, worked: null, revealing: false };

type Listener = () => void;

function invalid(problems: string[]): CommandError {
  return new CommandError(200, "invalid_reply", false, { fields: problems });
}

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw invalid(problems);
  return value as T;
}

/** The core that serves practice sets (M16) already sends what its proposed contract additions describe: a set with no
 * question to show (`current_item` null), a "not_checked" verdict and a reflection card on a mark. Until the contract
 * adopts them, each of those is checked on its own and everything else against the adopted definition; once adopted,
 * the replies pass the adopted definition unchanged and these allowances never apply. */
function checkedReply<T>(definition: "PracticeSet" | "PracticeMarked", value: unknown): T {
  if (typeof value !== "object" || value === null) return checked<T>(definition, value);
  const reply = value as Record<string, unknown>;
  const { reflection, worked, ...rest } = reply;
  const proposed = definition === "PracticeMarked" && "reflection" in reply;
  if (proposed) {
    const problems = errors("api/events#/$defs/card_reflection", reflection);
    if (problems.length > 0) throw invalid(problems.map((p) => `/reflection${p.startsWith("$") ? p.slice(1) : p}`));
  }
  // The worked answer of a second miss (ASM-28) is proposed: it is checked on its own, as the adopted worked example.
  if (definition === "PracticeMarked" && "worked" in reply) {
    const problems = errors("api/commands#/$defs/WorkedExampleView", worked);
    if (problems.length > 0) throw invalid(problems.map((p) => `/worked${p.startsWith("$") ? p.slice(1) : p}`));
  }
  const allowed = (problem: string) => (definition === "PracticeSet" && reply.current_item === null && problem.startsWith("/current_item:"))
    || (definition === "PracticeMarked" && reply.verdict === "not_checked" && problem.startsWith("/verdict:"));
  const body = definition === "PracticeMarked" ? rest : reply;
  const problems = errors(`api/commands#/$defs/${definition}`, body).filter((p) => !allowed(p));
  if (problems.length > 0) throw invalid(problems);
  return value as T;
}

function bandChange(value: unknown): boolean {
  const change = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  return ["kc_id", "band_before", "band_after"].every((k) => typeof change[k] === "string") && Object.keys(change).length === 3;
}

/** The reply to "Don't know", checked as contract-additions-m45d.json proposes it (`PracticeRevealed`). */
function checkedRevealed(value: unknown, itemId: string): PracticeRevealed {
  const reply = (typeof value === "object" && value !== null ? value : {}) as Record<string, unknown>;
  const problems = [
    ...(reply.item_id === itemId ? [] : ["/item_id: not the question shown"]),
    ...errors("api/commands#/$defs/WorkedExampleView", reply.worked).map((p) => `/worked${p.startsWith("$") ? p.slice(1) : p}`),
    ...(bandChange(reply.mastery_change) ? [] : ["/mastery_change: not a band change"]),
    ...(reply.next_item === null ? [] : errors("api/commands#/$defs/PracticeItem", reply.next_item).map((p) => `/next_item${p.startsWith("$") ? p.slice(1) : p}`)),
  ];
  if (problems.length > 0) throw invalid(problems);
  return reply as unknown as PracticeRevealed;
}

/** Whether a marked try settles the question: right, right but not simplified yet, or not something the marker checks. */
export function settled(marked: PracticeMarked): boolean {
  // An answer the marker cannot check (an explanation in words) settles the question too: trying again changes nothing.
  return marked.verdict === "correct" || marked.verdict === "equivalent_unsimplified" || marked.verdict === "not_checked";
}

export class PracticeController {
  private view: PracticeView = EMPTY_PRACTICE;
  private pendingAnswer: { setId: string; item: PracticeItem; body: api_commands.PracticeAnswer } | null = null;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => { this.pendingAnswer = null; this.set(EMPTY_PRACTICE); });
  }

  getView = (): PracticeView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: PracticeView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Start a set for a subject: adaptive (today's practice), or on the ideas the student chose (`kcIds`). */
  async start(subject: string, kcIds: string[] = [], size: number = CLIENT_KEYS.ui_practice_set_size): Promise<void> {
    this.pendingAnswer = null;
    const generation = this.profiles.generation;
    this.set({ ...EMPTY_PRACTICE, status: "starting", subject });
    const body: api_commands.CreatePracticeSet = { client_set_id: clientId(), subject, size, source: kcIds.length > 0 ? "topic" : "adaptive", ...(kcIds.length > 0 ? { kc_ids: kcIds.slice(0, 20) } : {}) };
    try {
      const made = checkedReply<Omit<api_commands.PracticeSet, "current_item"> & { current_item: PracticeItem | null }>("PracticeSet", await this.profiles.commands.post<unknown>("/v1/practice/sets", body, body.client_set_id));
      if (generation !== this.profiles.generation) return;
      this.set({ ...EMPTY_PRACTICE, status: made.items_total === 0 || made.current_item === null ? "done" : "ready", setId: made.set_id, subject, number: 1, total: made.items_total, item: made.current_item });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      const missing = error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
      this.set({ ...EMPTY_PRACTICE, status: missing ? "unavailable" : "failed", subject });
    }
  }

  /** Open a set the core already made: the follow-up practice a mock report offers on its weakest ideas ("Practise
   * these topics", UXS-26, ASM-20; `MockResult.practice_set_ref`), from its next unanswered question. */
  async open(setId: string, subject: string): Promise<void> {
    this.pendingAnswer = null;
    const generation = this.profiles.generation;
    this.set({ ...EMPTY_PRACTICE, status: "starting", subject });
    try {
      const made = checkedReply<Omit<api_commands.PracticeSet, "current_item"> & { current_item: PracticeItem | null }>(
        "PracticeSet", await this.profiles.commands.get<unknown>(`/v1/practice/sets/${encodeURIComponent(setId)}`));
      if (generation !== this.profiles.generation) return;
      this.set({ ...EMPTY_PRACTICE, status: made.items_total === 0 || made.current_item === null ? "done" : "ready", setId: made.set_id, subject, number: 1, total: made.items_total, item: made.current_item });
    } catch {
      if (generation === this.profiles.generation) this.set({ ...EMPTY_PRACTICE, status: "failed", subject });
    }
  }

  /** Answer the question shown; the core marks it at once (ASM-02). A failed send keeps the answer to send again. */
  async answer(answers: PartAnswer[], timeMs: number, hintsUsed = 0): Promise<PracticeMarked | null> {
    const { setId, item } = this.view;
    if (setId === null || item === null || this.view.marking || this.view.closed || this.view.revealing) return null;
    const generation = this.profiles.generation;
    this.set({ ...this.view, marking: true, markFailed: false });
    const pending = this.pendingAnswer;
    const body: api_commands.PracticeAnswer = pending?.setId === setId && pending.item === item ? pending.body :
      { client_attempt_id: clientId(), item_id: item.item_id, answer: { parts: structuredClone(answers) }, time_ms: Math.max(0, Math.round(timeMs)), hints_used: Math.min(5, Math.max(0, hintsUsed)) };
    this.pendingAnswer = { setId, item, body };
    try {
      const marked = checkedReply<PracticeMarked>("PracticeMarked", await this.profiles.commands.post<unknown>(`/v1/practice/sets/${encodeURIComponent(setId)}/answers`, body, body.client_attempt_id));
      if (generation !== this.profiles.generation || this.view.item !== item) return null;
      this.pendingAnswer = null;
      // The question stays open for another try only while the core still gives it as the one to answer.
      const closed = settled(marked) || marked.next_item?.item_id !== item.item_id;
      this.set({ ...this.view, marking: false, tries: [...this.view.tries, { answers: body.answer.parts, marked }], next: closed ? marked.next_item : null, closed, worked: marked.worked ?? this.view.worked });
      return marked;
    } catch (error) {
      if (generation === this.profiles.generation && this.view.item === item) {
        // A refused request can be corrected. An uncertain outcome must recover the original committed result.
        if (error instanceof CommandError && error.status >= 400 && error.status < 500 && !error.retryable) this.pendingAnswer = null;
        this.set({ ...this.view, marking: false, markFailed: true });
      }
      return null;
    }
  }

  /** "Don't know" (ASM-28): the question's worked answer with its check, and the set moves on; nothing is marked. A
   * core without the route answers `not_found`, and the question simply stays as it was. */
  async reveal(): Promise<boolean> {
    const { setId, item } = this.view;
    if (setId === null || item === null || this.view.marking || this.view.closed || this.view.revealing) return false;
    const generation = this.profiles.generation;
    this.set({ ...this.view, revealing: true });
    try {
      const shown = checkedRevealed(await this.profiles.commands.post<unknown>(`/v1/practice/sets/${encodeURIComponent(setId)}/reveal`, { item_id: item.item_id }, clientId()), item.item_id);
      if (generation !== this.profiles.generation || this.view.item !== item) return false;
      this.pendingAnswer = null;
      this.set({ ...this.view, revealing: false, worked: shown.worked, next: shown.next_item, closed: true });
      return true;
    } catch {
      if (generation === this.profiles.generation && this.view.item === item) this.set({ ...this.view, revealing: false });
      return false;
    }
  }

  /** Keep the answer being written, so it is still there after the tutor has helped. */
  keepDraft(draft: unknown): void {
    if (this.view.item !== null) this.set({ ...this.view, draft });
  }

  /** "Next question": the one the core gave with the last mark, or the end of the set. */
  advance(): void {
    const { next } = this.view;
    if (next === null) {
      this.set({ ...this.view, status: "done", item: null, tries: [], draft: null, worked: null, revealing: false });
      return;
    }
    this.set({ ...this.view, item: next, next: null, tries: [], number: Math.min(this.view.total, this.view.number + 1), markFailed: false, closed: false, draft: null, worked: null, revealing: false });
  }

  /** "Help me": the tutor opens with the item loaded (UXS-24); the reply is the session to open. */
  async help(reason: "help" | "why_right" = "help"): Promise<string | null> {
    const { setId, item } = this.view;
    if (setId === null || item === null) return null;
    const body: api_commands.PracticeHelp = reason === "why_right" ? { item_id: item.item_id, reason } : { item_id: item.item_id };
    try {
      const created = await this.profiles.commands.post<{ session_id: string }>(`/v1/practice/sets/${encodeURIComponent(setId)}/help`, body, clientId());
      return created.session_id;
    } catch {
      return null;
    }
  }

  /** End the set: nothing more is asked; the marks already given stay with the profile. */
  end(): void {
    this.pendingAnswer = null;
    this.set(EMPTY_PRACTICE);
  }
}
