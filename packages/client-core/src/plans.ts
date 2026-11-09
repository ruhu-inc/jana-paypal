// The study plan on Home and the Progress screen (UXS-12, UXS-27, LRN-28 to LRN-35; API 1.7, D99). The plan is the
// core's: computed by code, read with `GET /v1/plans/current`, asked for again with `POST /v1/plans` (at any hour, as
// often as wanted: Keep learning after a finished plan), and answered on the card route of API-14
// (`POST /v1/cards/req_<plan_id>/answer`): Start (`start`), Change (`change` with `alt_0`..`alt_2` or `shorten`) and
// Not today (`skipped: true`). A change makes a new proposal, so the plan is read again after every answer. A retried
// request for a new plan carries the same `client_request_id`, so Keep learning starts one plan, never two. Stopping
// needs nothing from the client: the plan stays started with its finished blocks, and the next read resumes it.
// Progress is `GET /v1/progress`: KC states and bands by topic, the weekly history, time studied, the streak and the
// grade band, which is shown only where the subject's pack carries the exam board's own grade letters (D27, ASM-19):
// the core says `no_scale` for a subject whose pack has none (proposed in contract-additions-grade-bands.json, read
// here before the contract adopts it), and then nothing about a grade is shown.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type PlanView = api_commands.PlanView;
export type PlanBlock = api_commands.PlanBlock;
export type PlanAlternative = api_commands.PlanAlternative;
export type ProgressView = api_commands.ProgressView;
export type ProgressSubject = api_commands.ProgressSubject;
/** The grade as the core gives it, with the proposed status `no_scale`: the subject's pack sets no exam board grade
 * letters, so no band is ever shown (D27; contract-additions-grade-bands.json). */
export type ProgressGrade = Omit<api_commands.ProgressSubjectGrade, "status"> & { status: api_commands.ProgressSubjectGrade["status"] | "no_scale" };
type Listener = () => void;

function invalid(problems: string[]): Error {
  return new Error(`reply does not match the contract: ${problems.slice(0, 3).join("; ")}`);
}

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw invalid(problems);
  return value as T;
}

/** A command the core does not serve (an older core): the screen says so and the rest of the app carries on. */
function missing(error: unknown): boolean {
  return error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
}

export interface PlanState {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  plan: PlanView | null;
  /** An answer or a new proposal in flight: the card's actions wait. */
  busy: boolean;
  /** The last answer or request failed; the card stays as it was and can be answered again. */
  actionFailed: boolean;
}

export const EMPTY_PLAN: PlanState = { status: "idle", plan: null, busy: false, actionFailed: false };

/** Change on the plan card: one of the ranked alternatives for the main block, or the plan shortened (LRN-31). */
export type PlanChoice = { alternative: number } | "shorten";

/** The block the student does next, or null when the plan has none left (done, set aside, or nothing to plan). */
export function nextBlock(plan: PlanView | null): PlanBlock | null {
  if (plan === null || plan.next_block === null) return null;
  return plan.blocks.find((b) => b.index === plan.next_block) ?? null;
}

/** The subject a KC id belongs to, by its curriculum code (`kc.phy.o.motion.speed` → `phy`). */
export function kcSubjectCode(kcId: string): string | null {
  return kcId.split(".")[1] ?? null;
}

export class PlanController {
  private state: PlanState = EMPTY_PLAN;
  private readonly listeners = new Set<Listener>();
  /** The request id of a new plan asked for and not yet answered: a retry sends the same one (API-14 idempotency). */
  private pendingRequest: string | null = null;

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => {
      this.pendingRequest = null;
      this.set(EMPTY_PLAN);
    });
  }

  getView = (): PlanState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: PlanState): void {
    if (next === this.state) return;
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Today's plan: the current one, or a new proposal when there is none yet today (a plan is offered at any hour). */
  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.state.plan === null) this.set({ ...this.state, status: "loading" });
    try {
      const current = checked<api_commands.PlanCurrent>("PlanCurrent", await this.profiles.commands.get<unknown>("/v1/plans/current"));
      if (generation !== this.profiles.generation) return;
      if (current === null) {
        await this.propose();
        return;
      }
      this.set({ ...this.state, status: "ready", plan: current });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.set({ ...this.state, status: missing(error) ? "unavailable" : this.state.plan === null ? "failed" : "ready" });
    }
  }

  /** A new proposal: the first of the day, another after Not today, or Keep learning after a finished plan. */
  async propose(): Promise<void> {
    const generation = this.profiles.generation;
    const request = this.pendingRequest ?? clientId();
    this.pendingRequest = request;
    this.set({ ...this.state, busy: true, actionFailed: false });
    try {
      const body: api_commands.PlanRequest = { client_request_id: request };
      const plan = checked<PlanView>("PlanView", await this.profiles.commands.post<unknown>("/v1/plans", body, request));
      if (generation !== this.profiles.generation) return;
      this.pendingRequest = null;
      this.set({ status: "ready", plan, busy: false, actionFailed: false });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      const status = missing(error) ? "unavailable" : this.state.plan === null ? "failed" : "ready";
      this.set({ ...this.state, status, busy: false, actionFailed: this.state.plan !== null });
    }
  }

  private async answer(answer: api_commands.CardAnswerAnswer): Promise<boolean> {
    const plan = this.state.plan;
    if (plan === null || this.state.busy) return false;
    const generation = this.profiles.generation;
    this.set({ ...this.state, busy: true, actionFailed: false });
    const body: api_commands.CardAnswer = { client_answer_id: clientId(), answer };
    try {
      await this.profiles.commands.post<api_commands.CardAnswerResult>(`/v1/cards/${encodeURIComponent(plan.request_id)}/answer`, body, body.client_answer_id);
      if (generation !== this.profiles.generation) return false;
      this.set({ ...this.state, busy: false });
      await this.load();
      return true;
    } catch {
      if (generation === this.profiles.generation) this.set({ ...this.state, busy: false, actionFailed: true });
      return false;
    }
  }

  /** Start (or Keep learning, on a fresh plan after a finished one): one `plan_decision: start`. */
  start(): Promise<boolean> {
    return this.answer({ action: "start" });
  }

  /** Not today: the plan is set aside and nothing further is asked; learning stays open (LRN-31, LRN-32). */
  notToday(): Promise<boolean> {
    return this.answer({ skipped: true });
  }

  /** Change: an alternative main block or a shorter plan; it changes this proposal only. */
  change(choice: PlanChoice): Promise<boolean> {
    return this.answer({ action: "change", option_ids: [choice === "shorten" ? "shorten" : `alt_${choice.alternative}`] });
  }
}

export interface ProgressState {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  progress: ProgressView | null;
}

export const EMPTY_PROGRESS: ProgressState = { status: "idle", progress: null };

export class ProgressController {
  private state: ProgressState = EMPTY_PROGRESS;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_PROGRESS));
  }

  getView = (): ProgressState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: ProgressState): void {
    if (next === this.state) return;
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Read the open profile's progress; a read again keeps what is shown until the new one arrives. */
  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.state.progress === null) this.set({ status: "loading", progress: null });
    try {
      const progress = checkedProgress(await this.profiles.commands.get<unknown>("/v1/progress"));
      if (generation === this.profiles.generation) this.set({ status: "ready", progress });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.set(this.state.progress !== null ? { ...this.state, status: "ready" } : { status: missing(error) ? "unavailable" : "failed", progress: null });
    }
  }
}

/** The progress view checked against the contract, allowing the one proposed value the core sends before adoption: a
 * subject's grade status `no_scale` (D27). Everything else must pass the adopted definition. */
function checkedProgress(value: unknown): ProgressView {
  const subjects = (value as { subjects?: { grade?: { status?: unknown } }[] } | null)?.subjects;
  const allowed = (problem: string) => {
    const at = /^\/subjects\/(\d+)\/grade\/status:/.exec(problem);
    return at !== null && subjects?.[Number(at[1])]?.grade?.status === "no_scale";
  };
  const problems = errors("api/commands#/$defs/ProgressView", value).filter((p) => !allowed(p));
  if (problems.length > 0) throw invalid(problems);
  return value as ProgressView;
}

/** What the progress screen says about a subject's grade (UXS-27, ASM-19, D27): nothing when the subject's pack sets no
 * exam board grade letters (`no_scale`); otherwise the count still needed below the threshold, "not enough evidence"
 * when the interval is too wide, or the likely band with its range. Never a board-neutral or invented scale. */
export type GradeShown =
  | { kind: "none" }
  | { kind: "gate"; checked: number; needed: number }
  | { kind: "uncertain" }
  | { kind: "band"; band: string; low: string; high: string };

export function gradeShown(grade: ProgressGrade | api_commands.ProgressSubjectGrade): GradeShown {
  if (grade.status === "no_scale") return { kind: "none" };
  if (grade.status === "hidden") return { kind: "gate", checked: grade.checked, needed: grade.needed };
  if (grade.status === "uncertain" || grade.band === undefined) return { kind: "uncertain" };
  return { kind: "band", band: grade.band, low: grade.range?.[0] ?? grade.band, high: grade.range?.[1] ?? grade.band };
}

/** The mock report's band (UXS-26, ASM-19, D27): absent when the paper's pack sets no board letters (no band at all);
 * null when the evidence is not enough yet; otherwise the band and its range in the board's letters. A band the core
 * marks board-neutral is never shown (an older core's invented scale). */
export type MockBandShown = { kind: "none" } | { kind: "evidence" } | { kind: "band"; band: string; low: string; high: string };

export function mockBandShown(result: { grade_band?: api_commands.GradeBand | null }): MockBandShown {
  if (!("grade_band" in result) || result.grade_band === undefined) return { kind: "none" };
  const band = result.grade_band;
  if (band === null) return { kind: "evidence" };
  if (band.board_neutral !== false) return { kind: "none" };
  return { kind: "band", band: band.band, low: band.range[0] ?? band.band, high: band.range[1] ?? band.band };
}
