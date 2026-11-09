import { errors, type learning_student_records } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { PartAnswer, PracticeItem } from "./learn.js";
import type { ProfileController } from "./profiles.js";
import type { KcState } from "./learn.js";
import { CommandError } from "./stream/commands.js";

export type AssessmentStart = learning_student_records.AssessmentStart;
export interface Assessment {
  assessment_id: string;
  kind: AssessmentStart["kind"];
  subject: AssessmentStart["subject"];
  kc_id: string | null;
  state: "in_progress" | "waiting" | "completed" | "failed" | "stopped" | "explaining";
  answered_count: number;
  current_item: PracticeItem | null;
  available_at: string | null;
  retry_at: string | null;
  outcome_topics: { topic_id: string; status: KcState }[];
  answered_items: { item_id: string; attempt_id: string | null; answered_at: string }[];
  last_mark?: { verdict: "correct" | "partially_correct" | "incorrect"; marks_awarded: number; marks_total: number } | null;
  /** Explain it back: the learner's own explanation that confirms a passed mastery check (absent when none is asked). */
  explain?: learning_student_records.AssessmentState["explain"];
}
/** How the explanation reached the core: typed, or the transcript of what the learner said (judged the same way). */
export type ExplainModality = learning_student_records.ExplainBack["modality"];
export interface AssessmentView {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  assessments: Assessment[];
  current: Assessment | null;
  acting: boolean;
  error: string | null;
  uncertain: boolean;
  saved: boolean;
  resumed: boolean;
}
export const EMPTY_ASSESSMENTS: AssessmentView = { status: "idle", assessments: [], current: null, acting: false, error: null, uncertain: false, saved: false, resumed: false };

/** M16's response predates an adopted response definition. Validate public fields and redacted items; never render
 * the engine's private selection/answer records. Diagnostic responses containing marks are refused. */
export function readAssessment(value: unknown): Assessment {
  const bad = () => new CommandError(200, "invalid_reply", false, undefined);
  if (typeof value !== "object" || value === null) throw bad();
  const a = value as Assessment;
  if (typeof a.assessment_id !== "string" || !/^[0-9A-HJKMNP-TV-Z]{26}$/.test(a.assessment_id)
    || !["diagnostic", "mastery_check"].includes(a.kind) || !["mathematics", "physics", "chemistry", "biology", "accounting"].includes(a.subject)
    || !(a.kc_id === null || typeof a.kc_id === "string") || !["in_progress", "waiting", "completed", "failed", "stopped", "explaining"].includes(a.state)
    || !Number.isSafeInteger(a.answered_count) || a.answered_count < 0
    || ![a.available_at, a.retry_at].every((d) => d === null || typeof d === "string" && Number.isFinite(Date.parse(d)))
    || !Array.isArray(a.outcome_topics) || !a.outcome_topics.every((t) => typeof t.topic_id === "string" && ["not_started", "learning", "ready_to_check", "mastered", "needs_review", "moved_on"].includes(t.status))
    || !Array.isArray(a.answered_items) || a.answered_items.length !== a.answered_count
    || !a.answered_items.every((i) => typeof i.item_id === "string" && (i.attempt_id === null || typeof i.attempt_id === "string") && typeof i.answered_at === "string")
    || a.current_item !== null && errors("api/commands#/$defs/PracticeItem", a.current_item).length > 0
    || a.state === "in_progress" && a.current_item === null) throw bad();
  if (a.kind === "diagnostic" && a.current_item !== null && ("marks" in a.current_item || a.current_item.parts.some((p) => "marks" in p))) throw bad();
  const e = a.explain;
  if (e !== undefined && (a.kind !== "mastery_check" || typeof e !== "object" || e === null || typeof e.statement !== "string" || !(e.follow_up === null || typeof e.follow_up === "string")
    || !Number.isSafeInteger(e.attempts) || !["awaiting", "follow_up", "passed", "not_yet", "unavailable"].includes(e.outcome))) throw bad();
  if (a.state === "explaining" && a.explain === undefined) throw bad();
  if (a.last_mark != null && (!["correct", "partially_correct", "incorrect"].includes(a.last_mark.verdict) || !Number.isFinite(a.last_mark.marks_awarded) || !Number.isFinite(a.last_mark.marks_total))) throw bad();
  return a.kind === "diagnostic" ? { ...a, last_mark: null } : a;
}

type Pending = { path: string; body: unknown; key: string; answering: boolean };
export class AssessmentController {
  private view = EMPTY_ASSESSMENTS;
  private listeners = new Set<() => void>();
  private revision = 0;
  private pending: Pending | null = null;
  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => { this.revision++; this.pending = null; this.set(EMPTY_ASSESSMENTS); });
  }
  getView = (): AssessmentView => this.view;
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  private set(view: AssessmentView) { this.view = view; for (const fn of this.listeners) fn(); }
  private patch(view: Partial<AssessmentView>) { this.set({ ...this.view, ...view }); }
  private accept(current: Assessment, flags: Partial<AssessmentView> = {}) {
    this.patch({ status: "ready", current, assessments: [current, ...this.view.assessments.filter((a) => a.assessment_id !== current.assessment_id)], acting: false, error: null, uncertain: false, ...flags });
  }
  async load(): Promise<void> {
    if (this.view.acting || this.pending !== null) return;
    const generation = this.profiles.generation, revision = ++this.revision;
    this.patch({ status: "loading", error: null });
    try {
      const reply = await this.profiles.commands.get<unknown>("/v1/assessments");
      if (!Array.isArray(reply)) throw new CommandError(200, "invalid_reply", false, undefined);
      const assessments = reply.map(readAssessment);
      if (generation === this.profiles.generation && revision === this.revision) this.patch({ status: "ready", assessments });
    } catch (error) {
      if (generation === this.profiles.generation && revision === this.revision) this.patch({ status: error instanceof CommandError && error.code === "not_found" ? "unavailable" : "failed" });
    }
  }
  async open(id: string, resumed = true): Promise<void> {
    if (this.view.acting || this.pending !== null) return;
    const generation = this.profiles.generation, revision = ++this.revision;
    this.patch({ acting: true, error: null });
    try {
      const current = readAssessment(await this.profiles.commands.get(`/v1/assessments/${encodeURIComponent(id)}`));
      if (current.assessment_id !== id) throw new CommandError(200, "invalid_reply", false, undefined);
      if (generation === this.profiles.generation && revision === this.revision) this.accept(current, { resumed, saved: false });
    } catch (error) {
      if (generation === this.profiles.generation && revision === this.revision) this.patch({ acting: false, error: error instanceof CommandError ? error.code : "unreachable" });
    }
  }
  private async run(pending: Pending): Promise<boolean> {
    if (this.view.acting) return false;
    const generation = this.profiles.generation, revision = ++this.revision;
    this.pending = pending;
    this.patch({ acting: true, error: null });
    try {
      const current = readAssessment(await this.profiles.commands.post(pending.path, pending.body, pending.key));
      if (generation !== this.profiles.generation || revision !== this.revision) return false;
      this.pending = null;
      this.accept(current, { saved: pending.answering, resumed: pending.path.endsWith("/resume") });
      return true;
    } catch (error) {
      if (generation !== this.profiles.generation || revision !== this.revision) return false;
      const definitive = error instanceof CommandError && error.status >= 400 && error.status < 500;
      if (definitive) this.pending = null;
      this.patch({ acting: false, uncertain: !definitive, error: error instanceof CommandError ? (typeof error.details?.reason === "string" ? error.details.reason : error.code) : "unreachable" });
      return false;
    }
  }
  start(body: AssessmentStart): Promise<boolean> {
    if (this.pending !== null) return this.run(this.pending);
    return this.run({ path: "/v1/assessments", body, key: clientId(), answering: false });
  }
  answer(parts: PartAnswer[] | null): Promise<boolean> {
    if (this.pending !== null) return this.run(this.pending);
    const current = this.view.current;
    if (current?.state !== "in_progress" || current.current_item === null || parts === null && current.kind !== "diagnostic") return Promise.resolve(false);
    const key = clientId();
    const body: learning_student_records.AssessmentAnswer = { client_answer_id: key, item_id: current.current_item.item_id, action: parts === null ? "not_learned" : "answer", ...(parts === null ? {} : { answer: { parts } }) };
    return this.run({ path: `/v1/assessments/${current.assessment_id}/answers`, body, key, answering: true });
  }
  /** Explain it back: the learner's explanation of the KC of a check waiting for one. */
  explain(text: string, modality: ExplainModality = "typed"): Promise<boolean> {
    if (this.pending !== null) return this.run(this.pending);
    const current = this.view.current;
    const words = text.trim();
    if (current?.state !== "explaining" || words === "") return Promise.resolve(false);
    const key = clientId();
    const body: learning_student_records.ExplainBack = { client_explain_id: key, text: words.slice(0, 1200), modality };
    return this.run({ path: `/v1/assessments/${current.assessment_id}/explain`, body, key, answering: true });
  }
  action(action: "stop" | "resume"): Promise<boolean> {
    if (this.pending !== null) return this.run(this.pending);
    const id = this.view.current?.assessment_id;
    return id === undefined ? Promise.resolve(false) : this.run({ path: `/v1/assessments/${id}/${action}`, body: {}, key: clientId(), answering: false });
  }
  retry(): Promise<boolean> { return this.pending === null ? Promise.resolve(false) : this.run(this.pending); }
  back(): void { if (!this.view.acting && this.pending === null) this.patch({ current: null, saved: false, resumed: false, error: null }); }
}
