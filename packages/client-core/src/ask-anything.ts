// The one Home input (UXS-67) and Ask anything (STU-20, D117). The core decides where the box's words go
// (`POST /v1/ask/route`): a problem or question in an installed subject to the guided tutor, material to the study
// area, any other question to Ask anything (`POST /v1/ask`), which the core routes again so homework is never answered
// here. An answer shows its search first ("Looked in your notes", "Looked at 4 trusted sources"), cites every supported
// statement and labels the rest as general knowledge; nothing in this mode is ever shown as checked.
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type InputRoute = "tutor" | "study" | "ask";
/** Why Ask anything cannot answer now: the offline message offers the tutor and the study area instead (STU-20). */
export type AskUnavailable = "offline" | "needs_online_plan" | "consent_required" | "notice_required" | "allowance_exhausted";
export const ASK_UNAVAILABLE: readonly AskUnavailable[] = ["offline", "needs_online_plan", "consent_required", "notice_required", "allowance_exhausted"];
export type AskMode = "teach" | "explain";
export type AskReach = "allow_list" | "wider_web";

export interface RouteDecision {
  route: InputRoute;
  reason: string;
  subject?: string;
  ask: { available: boolean; reason?: AskUnavailable };
}
export interface AskStatement { text: string; refs: string[]; supported: boolean; checked: false }
export interface AskSource {
  ref: string;
  kind: "own_source" | "session_note" | "web";
  title: string;
  url?: string;
  domain?: string;
  source_id?: string;
  note_id?: string;
}
export interface AskAnswer {
  ask_id: string;
  question: string;
  mode: AskMode;
  audience: "under_18" | "adult";
  reach: AskReach;
  steps: { kind: "own" | "trusted" | "web"; count: number }[];
  sources: AskSource[];
  own_excerpts: { ref: string; text: string }[];
  answer: AskStatement[];
  check_question: string | null;
  explanation: AskStatement[];
  general_knowledge: boolean;
  checked: false;
  can_switch: boolean;
}
export type AskReply =
  | { route: "tutor"; reason: string; subject?: string }
  | { route: "study"; reason: string }
  | { route: "ask"; state: "answered"; answer: AskAnswer }
  | { route: "ask"; state: "unavailable"; reason: AskUnavailable }
  | { route: "ask"; state: "failed"; reason: string };

export interface AskView {
  /** `spoken`: a question asked in the voice conversation that Ask anything did not answer itself: the tutor answered it
   * by voice (route `tutor`, D130) or it was material (route `study`); `spoken` holds the words said (VOC-53). */
  status: "idle" | "asking" | "answered" | "unavailable" | "failed" | "blocked" | "spoken";
  question: string;
  answer: AskAnswer | null;
  reason: string | null;
  /** The explanation after a Teach me answer's check question, shown on request. */
  explained: boolean;
  /** For a spoken question (VOC-53): where it went and the words the reply said. */
  route?: InputRoute | null;
  spoken?: string | null;
}
export const EMPTY_ASK: AskView = { status: "idle", question: "", answer: null, reason: null, explained: false, route: null, spoken: null };

/** The `voice.answer` event of a question spoken in an open chat (API-38, VOC-53). */
export interface VoiceAnswer {
  type: "voice.answer";
  place: "ask" | "study";
  turn_id: string;
  question: string;
  route: InputRoute;
  reply: unknown;
  aid_id: string | null;
  text: string;
}
export function readVoiceAnswer(value: unknown): VoiceAnswer | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as VoiceAnswer;
  if (v.type !== "voice.answer" || !["ask", "study"].includes(v.place) || !isString(v.question) || !isString(v.text)
    || !["tutor", "study", "ask"].includes(v.route) || !(v.aid_id === null || isString(v.aid_id))) return null;
  return v;
}

const bad = () => new CommandError(200, "invalid_reply", false, undefined);
const isString = (v: unknown): v is string => typeof v === "string";

export function readRoute(value: unknown): RouteDecision {
  if (typeof value !== "object" || value === null) throw bad();
  const r = value as RouteDecision;
  if (!["tutor", "study", "ask"].includes(r.route) || !isString(r.reason) || (r.subject !== undefined && !isString(r.subject))) throw bad();
  if (typeof r.ask !== "object" || r.ask === null || typeof r.ask.available !== "boolean") throw bad();
  if (r.ask.reason !== undefined && !ASK_UNAVAILABLE.includes(r.ask.reason)) throw bad();
  if (r.route === "tutor" && r.subject === undefined && r.reason !== "tutoring_usage") throw bad();
  return r;
}

function readStatements(value: unknown, refs: Set<string>): AskStatement[] {
  if (!Array.isArray(value)) throw bad();
  return value.map((s: AskStatement) => {
    if (typeof s !== "object" || s === null || !isString(s.text) || !Array.isArray(s.refs) || !s.refs.every(isString)) throw bad();
    const cited = s.refs.filter((r) => refs.has(r));
    // The client shows a statement as supported only with a citation it can show, and never as checked.
    return { text: s.text, refs: cited, supported: cited.length > 0 && s.supported === true, checked: false };
  });
}

export function readAnswer(value: unknown): AskAnswer {
  if (typeof value !== "object" || value === null) throw bad();
  const a = value as AskAnswer;
  if (!/^ask_[0-9A-HJKMNP-TV-Z]{26}$/.test(a.ask_id) || !isString(a.question) || !["teach", "explain"].includes(a.mode)
    || !["under_18", "adult"].includes(a.audience) || !["allow_list", "wider_web"].includes(a.reach)
    || !Array.isArray(a.steps) || !Array.isArray(a.sources) || !Array.isArray(a.own_excerpts)
    || !(a.check_question === null || isString(a.check_question)) || typeof a.can_switch !== "boolean") throw bad();
  const sources = a.sources.filter((s) => typeof s === "object" && s !== null && isString(s.ref) && isString(s.title)
    && ["own_source", "session_note", "web"].includes(s.kind) && (s.kind !== "web" || (isString(s.url) && s.url.startsWith("https://"))));
  const refs = new Set(sources.map((s) => s.ref));
  const answer = readStatements(a.answer, refs), explanation = readStatements(a.explanation, refs);
  if (answer.length === 0 || (a.mode === "teach" && a.check_question === null)) throw bad();
  const steps = a.steps.filter((s) => ["own", "trusted", "web"].includes(s.kind) && Number.isSafeInteger(s.count) && s.count >= 0);
  return {
    ...a, steps, sources, answer, explanation, checked: false,
    own_excerpts: a.own_excerpts.filter((e) => isString(e.ref) && isString(e.text) && refs.has(e.ref)),
    // Unsupported anywhere means the label shows, whatever the reply said.
    general_knowledge: [...answer, ...explanation].some((s) => !s.supported),
  };
}

export function readAskReply(value: unknown): AskReply {
  if (typeof value !== "object" || value === null) throw bad();
  const r = value as { route?: unknown; state?: unknown; reason?: unknown; subject?: unknown; answer?: unknown };
  if (r.route === "tutor") {
    if (!isString(r.reason) || r.answer !== null) throw bad();
    return { route: "tutor", reason: r.reason, ...(isString(r.subject) ? { subject: r.subject } : {}) };
  }
  if (r.route === "study") {
    if (!isString(r.reason)) throw bad();
    return { route: "study", reason: r.reason };
  }
  if (r.route !== "ask") throw bad();
  if (r.state === "answered") return { route: "ask", state: "answered", answer: readAnswer(r.answer) };
  if (r.state === "unavailable" && ASK_UNAVAILABLE.includes(r.reason as AskUnavailable)) return { route: "ask", state: "unavailable", reason: r.reason as AskUnavailable };
  if (r.state === "failed" && isString(r.reason)) return { route: "ask", state: "failed", reason: r.reason };
  throw bad();
}

/** Where a typed text goes and, for Ask anything, the answer. One controller per app, cleared with the profile. */
export class AskController {
  private view = EMPTY_ASK;
  private listeners = new Set<() => void>();
  private revision = 0;
  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => { this.revision++; this.set(EMPTY_ASK); });
  }
  getView = (): AskView => this.view;
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  private set(view: AskView) { this.view = view; for (const fn of this.listeners) fn(); }
  private patch(view: Partial<AskView>) { this.set({ ...this.view, ...view }); }

  /** Where the words go; null when the core cannot say (then the box keeps its old behaviour: the tutor). */
  async route(text: string): Promise<RouteDecision | null> {
    const generation = this.profiles.generation;
    try {
      const decision = readRoute(await this.profiles.commands.post("/v1/ask/route", { text: text.slice(0, 20000) }, clientId()));
      return generation === this.profiles.generation ? decision : null;
    } catch { return null; }
  }

  /** Ask anything. A reply that routes elsewhere comes back for the app to follow; the view keeps no answer then. */
  async ask(question: string, options: { mode?: AskMode; reach?: AskReach } = {}): Promise<AskReply | null> {
    const generation = this.profiles.generation, revision = ++this.revision;
    const words = question.trim().slice(0, 2000);
    if (words === "") return null;
    this.set({ ...EMPTY_ASK, status: "asking", question: words });
    const live = () => generation === this.profiles.generation && revision === this.revision;
    try {
      const reply = readAskReply(await this.profiles.commands.post("/v1/ask", { question: words, ...options }, clientId()));
      if (!live()) return null;
      if (reply.route !== "ask") this.set(EMPTY_ASK);
      else if (reply.state === "answered") this.patch({ status: "answered", answer: reply.answer });
      else this.patch({ status: reply.state, reason: reply.reason });
      return reply;
    } catch (error) {
      if (live()) {
        const blocked = error instanceof CommandError && error.code === "validation_failed";
        this.patch({ status: blocked ? "blocked" : "failed", reason: blocked ? "safety_template" : "processing_failed" });
      }
      return null;
    }
  }
  /** VOC-53, D130: a question spoken in Ask anything was answered; the view shows it as a typed one would be, or, for
   * the tutor's spoken reply, the words said with "Continue in the tutor". An answer it cannot read shows as failed. */
  voiced(event: VoiceAnswer): void {
    if (event.place !== "ask") return;
    this.revision++;
    const question = event.question.slice(0, 2000);
    if (event.route !== "ask") { this.set({ ...EMPTY_ASK, status: "spoken", question, route: event.route, spoken: event.text }); return; }
    try {
      const reply = readAskReply(event.reply);
      if (reply.route !== "ask") this.set({ ...EMPTY_ASK, status: "spoken", question, route: reply.route, spoken: event.text });
      else if (reply.state === "answered") this.set({ ...EMPTY_ASK, status: "answered", question, answer: reply.answer, route: "ask", spoken: event.text });
      else if (reply.state === "failed" && reply.reason === "safety_template") this.set({ ...EMPTY_ASK, status: "blocked", question, reason: "safety_template" });
      else this.set({ ...EMPTY_ASK, status: reply.state, question, reason: reply.reason });
    } catch { this.set({ ...EMPTY_ASK, status: "failed", question, reason: "processing_failed" }); }
  }
  /** "Show the explanation" after the check question (Teach me). */
  explain(): void { if (this.view.answer !== null) this.patch({ explained: true }); }
  clear(): void { this.revision++; this.set(EMPTY_ASK); }
}
