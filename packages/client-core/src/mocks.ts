// Mock exams (UXS-25, UXS-26, ASM-15 to ASM-20, AGT-24, API-27): the papers on this computer and the profile's
// sittings (`GET /v1/mocks`, `MockList`, D93), one sitting at a time from `ready` through
// `in_progress`, `paused` and `interrupted` to `submitted`, `marked` and `reviewed` (the `sitting.state` values; the core
// owns every rule: active time only, the pause budget, time-up, abandonment). The clock shown is the core's: each read
// gives `remaining_ms`, and between reads the screen counts down from it while the sitting runs; it is read again every
// `ui_mock_refresh_ms`, and at zero the core is asked, which submits on time-up. Every change to an answer or a flag is
// saved within `ui_mock_save_ms` as a full replacement (`PUT .../answers/{question_id}`; a null answer clears it, API-27).
// Submitting returns the report (`MockResult`); asking again for a marked sitting returns the same report. While a
// sitting is open (in progress, paused or interrupted) the tutor, voice, capture and lessons are locked by the core
// (423 `tutor_locked`, AGT-24); `examLock` says so to the screens with the time left. Replies are checked against the
// command contract at the boundary; a core that does not list mocks yet answers `not_found` and the place says so.
// Everything here belongs to the unlocked profile and is dropped when it closes (SEC-19).
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { CLIENT_KEYS } from "./generated/keys.js";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type MockExam = api_commands.MockExam;
export type MockResult = api_commands.MockResult;
export type MockAnswer = api_commands.MockAnswer;
export type SittingState = api_commands.LearningMockExamSittingPropertiesState;

/** A paper the installed packs hold. */
export type MockPaper = api_commands.MockPaperSummary;

/** One of the profile's sittings, newest first. */
export type MockSitting = api_commands.MockSittingSummary;

/** The sitting states that lock the tutor until the paper is submitted (AGT-24, API-27). */
export const LOCKED_STATES: readonly SittingState[] = ["in_progress", "paused", "interrupted"];

export interface MockView {
  /** The place's list of papers and sittings. */
  status: "idle" | "loading" | "ready" | "unavailable" | "failed";
  papers: MockPaper[];
  sittings: MockSitting[];
  /** When the list was read (`Date.now()`), for the time left of a listed open sitting. */
  listedAt: number;
  /** The sitting on screen, as the core last gave it, with the answers changed since kept in. */
  exam: MockExam | null;
  /** When `exam` was read: the clock counts down from its `remaining_ms` while it runs. */
  readAt: number;
  opening: boolean;
  openFailed: boolean;
  /** The question shown, and the ones the student has opened (the navigator's "not seen yet"). */
  current: string | null;
  seen: string[];
  /** Answers waiting to be saved, and whether the last save failed (they are kept and sent again). */
  unsaved: string[];
  saveFailed: boolean;
  /** Start, pause, continue or submit on its way; the code of the last one the core refused. */
  acting: boolean;
  refused: string | null;
  /** The report once marked; `marking` while the core marks; `timeUp` when the clock ran out (auto-submit). */
  result: MockResult | null;
  marking: boolean;
  markFailed: boolean;
  timeUp: boolean;
}

export const EMPTY_MOCKS: MockView = {
  status: "idle", papers: [], sittings: [], listedAt: 0, exam: null, readAt: 0, opening: false, openFailed: false, current: null, seen: [],
  unsaved: [], saveFailed: false, acting: false, refused: null, result: null, marking: false, markFailed: false, timeUp: false,
};

/** What locks the app while a sitting is open: the sitting and its time left as of `readAt`. */
export interface ExamLock {
  examId: string;
  state: SittingState;
  remainingMs: number;
  readAt: number;
}

/** The open sitting, if any: the one on screen, else a listed one. */
export function examLock(view: MockView): ExamLock | null {
  if (view.exam !== null && LOCKED_STATES.includes(view.exam.state)) {
    return { examId: view.exam.exam_id, state: view.exam.state, remainingMs: view.exam.remaining_ms, readAt: view.readAt };
  }
  if (view.exam !== null) return null; // the sitting on screen is the newest word on it: submitted, marked or closed
  const open = view.sittings.find((s) => LOCKED_STATES.includes(s.state));
  return open === undefined ? null : { examId: open.exam_id, state: open.state, remainingMs: open.remaining_ms, readAt: view.listedAt };
}

/** The time left at `now`: counting down only while the sitting runs (active time, ASM-16). */
export function remainingAt(lock: Pick<ExamLock, "state" | "remainingMs" | "readAt">, now: number): number {
  return lock.state === "in_progress" ? Math.max(0, lock.remainingMs - Math.max(0, now - lock.readAt)) : lock.remainingMs;
}

/** The pause budget left (ASM-16): pauses and pause time, the running pause counted at `now`. */
export function pauseBudget(exam: Pick<MockExam, "state" | "pauses">, readAt: number, now: number): { pauses: number; ms: number } {
  const used = exam.pauses.reduce((sum, p) => sum + p.length_ms, 0) + (exam.state === "paused" ? Math.max(0, now - readAt) : 0);
  const ms = Math.max(0, CLIENT_KEYS.mock_pause_minutes * 60_000 - used);
  return { pauses: ms === 0 ? 0 : Math.max(0, CLIENT_KEYS.mock_pause_count - exam.pauses.length), ms };
}

type Listener = () => void;

function invalid(problems: string[]): CommandError {
  return new CommandError(200, "invalid_reply", false, { fields: problems });
}

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw invalid(problems);
  return value as T;
}

/** The answers of `exam` with `local` (changed, not yet saved) laid over them. */
function merged(exam: MockExam, local: MockExam["answers"]): MockExam {
  return { ...exam, answers: { ...exam.answers, ...local } };
}

export class MockController {
  private view: MockView = EMPTY_MOCKS;
  private readonly listeners = new Set<Listener>();
  /** Answers changed and not saved yet, by question. */
  private pending: MockExam["answers"] = {};
  private timer: ReturnType<typeof setTimeout> | null = null;
  private saving: Promise<boolean> | null = null;
  private asking = false;

  constructor(private readonly profiles: ProfileController, private readonly now: () => number = () => Date.now()) {
    profiles.onClear(() => {
      this.drop();
      this.saving = null;
      this.set(EMPTY_MOCKS);
    });
  }

  getView = (): MockView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: MockView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<MockView>): void {
    this.set({ ...this.view, ...change });
  }

  private drop(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.pending = {};
  }

  private path(examId: string, rest = ""): string {
    return `/v1/mocks/${encodeURIComponent(examId)}${rest}`;
  }

  /** The papers on this computer and the profile's sittings. */
  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.status !== "ready") this.patch({ status: "loading" });
    try {
      const listed = checked<api_commands.MockList>("MockList", await this.profiles.commands.get<unknown>("/v1/mocks"));
      if (generation !== this.profiles.generation) return;
      this.patch({ status: "ready", papers: listed.papers, sittings: listed.sittings, listedAt: this.now() });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      const missing = error instanceof CommandError && (error.status === 404 || error.status === 405);
      this.patch({ status: missing ? "unavailable" : "failed" });
    }
  }

  private shown(exam: MockExam, keepCurrent: boolean): Partial<MockView> {
    const ids = exam.question_ids ?? exam.questions.map((q) => q.item_id);
    const current = keepCurrent && this.view.current !== null && ids.includes(this.view.current) ? this.view.current : ids[0] ?? null;
    return { exam: merged(exam, this.pending), readAt: this.now(), current };
  }

  /** Choose a paper: a `ready` sitting with the ASM-16 notice and Start. */
  async choose(paperId: string): Promise<void> {
    const generation = this.profiles.generation;
    this.drop();
    this.patch({ ...EMPTY_MOCKS, status: this.view.status, papers: this.view.papers, sittings: this.view.sittings, listedAt: this.view.listedAt, opening: true });
    const body: api_commands.StartMock = { client_exam_id: clientId(), paper_id: paperId };
    try {
      const exam = checked<MockExam>("MockExam", await this.profiles.commands.post<unknown>("/v1/mocks", body, body.client_exam_id));
      if (generation !== this.profiles.generation) return;
      this.patch({ ...this.shown(exam, false), opening: false });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.patch({ opening: false, openFailed: true, refused: error instanceof CommandError ? error.code : null });
    }
  }

  /** Open one of the profile's sittings where it stands; a marked one opens on its report. */
  async open(examId: string): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.exam?.exam_id !== examId) {
      this.drop();
      this.patch({ ...EMPTY_MOCKS, status: this.view.status, papers: this.view.papers, sittings: this.view.sittings, listedAt: this.view.listedAt, opening: true });
    }
    try {
      const exam = checked<MockExam>("MockExam", await this.profiles.commands.get<unknown>(this.path(examId)));
      if (generation !== this.profiles.generation) return;
      this.patch({ ...this.shown(exam, true), opening: false, openFailed: false });
      if (exam.state === "submitted" || exam.state === "marked" || exam.state === "reviewed") await this.mark(exam.state === "submitted");
    } catch {
      if (generation === this.profiles.generation) this.patch({ opening: false, openFailed: true });
    }
  }

  /** Read the sitting again: the core's clock, and on time-up its automatic submission (then the report). */
  async refresh(): Promise<void> {
    const exam = this.view.exam;
    if (exam === null || this.asking) return;
    const generation = this.profiles.generation;
    this.asking = true;
    try {
      const read = checked<MockExam>("MockExam", await this.profiles.commands.get<unknown>(this.path(exam.exam_id)));
      if (generation !== this.profiles.generation || this.view.exam?.exam_id !== read.exam_id) return;
      const wasOpen = LOCKED_STATES.includes(exam.state);
      this.patch(this.shown(read, true));
      if (wasOpen && read.state === "submitted") {
        // Time up: the core kept every saved answer and submitted the paper (ASM-16). Anything not saved yet is lost
        // to the core's lock on answers, so it is dropped here too.
        this.drop();
        this.patch({ timeUp: true, unsaved: [] });
        await this.mark(true);
      }
    } catch {
      /* the next read tries again */
    } finally {
      this.asking = false;
    }
  }

  /** The clock reached zero on screen: ask the core, which submits the paper when the time is up. */
  async timeUp(): Promise<void> {
    const exam = this.view.exam;
    if (exam === null || exam.state !== "in_progress") return;
    await this.flush();
    await this.refresh();
  }

  private async act(action: "start" | "pause" | "resume"): Promise<void> {
    const exam = this.view.exam;
    if (exam === null || this.view.acting) return;
    const generation = this.profiles.generation;
    this.patch({ acting: true, refused: null });
    if (action === "pause") await this.flush();
    try {
      const next = checked<MockExam>("MockExam", await this.profiles.commands.post<unknown>(this.path(exam.exam_id, `/${action}`), {}, clientId()));
      if (generation !== this.profiles.generation) return;
      this.patch({ ...this.shown(next, true), acting: false });
      if (next.state === "in_progress" && this.view.current !== null) this.show(this.view.current);
      void this.load();
    } catch (error) {
      if (generation === this.profiles.generation) this.patch({ acting: false, refused: error instanceof CommandError ? error.code : "internal_error" });
    }
  }

  /** "Start": the clock starts and the tutor is locked until the paper is submitted. */
  start(): Promise<void> {
    return this.act("start");
  }

  /** "Pause", while pauses and pause time remain (ASM-16). */
  pause(): Promise<void> {
    return this.act("pause");
  }

  /** "Continue" after a pause, or "Continue the mock" after the app was closed. */
  resume(): Promise<void> {
    return this.act("resume");
  }

  /** Show a question: it counts as seen, and the core is told for the time per section (ASM-20). */
  show(questionId: string): void {
    const exam = this.view.exam;
    if (exam === null) return;
    this.patch({ current: questionId, seen: this.view.seen.includes(questionId) ? this.view.seen : [...this.view.seen, questionId] });
    if (exam.state !== "in_progress") return;
    const body: api_commands.MockNavigate = { question_id: questionId };
    void this.profiles.commands.post<unknown>(this.path(exam.exam_id, "/navigate"), body, clientId()).catch(() => undefined);
  }

  /** Change a question's answer (null clears it) or its flag; saved within `ui_mock_save_ms` (ASM-16, API-27). */
  setAnswer(questionId: string, change: Partial<MockAnswer>): void {
    const exam = this.view.exam;
    if (exam === null || exam.state !== "in_progress") return;
    const before = exam.answers[questionId] ?? { answer: null, flagged: false };
    const next: MockAnswer = { answer: change.answer !== undefined ? change.answer : before.answer, flagged: change.flagged ?? before.flagged };
    this.pending = { ...this.pending, [questionId]: next };
    this.patch({ exam: merged(exam, { [questionId]: next }), unsaved: Object.keys(this.pending) });
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, CLIENT_KEYS.ui_mock_save_ms);
  }

  /** Flag or unflag a question for review: independent of its answer (ASM-16). */
  toggleFlag(questionId: string): void {
    const flagged = this.view.exam?.answers[questionId]?.flagged ?? false;
    this.setAnswer(questionId, { flagged: !flagged });
  }

  /** Clear a question's answer: it stays unanswered at submission (API-27). */
  clear(questionId: string): void {
    this.setAnswer(questionId, { answer: null });
  }

  /** Save every changed answer now, one request per question. */
  async flush(): Promise<boolean> {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.saving !== null) return await this.saving ? this.flush() : false;
    const exam = this.view.exam;
    const waiting = Object.entries(this.pending);
    if (exam === null || waiting.length === 0) return waiting.length === 0;
    const generation = this.profiles.generation;
    this.saving = (async () => {
      let failed = false;
      for (const [questionId, answer] of waiting) {
        if (generation !== this.profiles.generation || this.view.exam?.exam_id !== exam.exam_id) return false;
        if (answer === undefined) continue;
        try {
          checked<api_commands.Saved>("Saved", await this.profiles.commands.put<unknown>(this.path(exam.exam_id, `/answers/${encodeURIComponent(questionId)}`), answer, clientId()));
          if (this.pending[questionId] === answer) {
            const { [questionId]: _saved, ...rest } = this.pending;
            this.pending = rest;
          }
        } catch {
          failed = true;
        }
      }
      if (generation === this.profiles.generation) this.patch({ unsaved: Object.keys(this.pending), saveFailed: failed });
      return !failed && generation === this.profiles.generation && this.view.exam?.exam_id === exam.exam_id;
    })();
    const work = this.saving;
    const saved = await work;
    if (this.saving === work) this.saving = null;
    // An answer can change while its previous revision is in flight. Drain those revisions before submitting.
    return saved && Object.keys(this.pending).length > 0 ? this.flush() : saved;
  }

  /** "Submit" (after the confirmation): every answer saved first, then the core marks the paper. */
  async submit(): Promise<void> {
    const exam = this.view.exam;
    if (exam === null || !LOCKED_STATES.includes(exam.state) || this.view.marking || this.view.acting) return;
    const generation = this.profiles.generation;
    this.patch({ acting: true });
    try {
      if (!await this.flush() || generation !== this.profiles.generation || this.view.exam?.exam_id !== exam.exam_id) return;
      await this.mark(false);
    } finally {
      if (generation === this.profiles.generation) this.patch({ acting: false });
    }
  }

  /** Mark the sitting (or read its report again): `POST .../submit` returns the report (UXS-26). */
  private async mark(timeUp: boolean): Promise<void> {
    const exam = this.view.exam;
    if (exam === null) return;
    const generation = this.profiles.generation;
    this.patch({ marking: true, markFailed: false, ...(timeUp ? { timeUp: true } : {}) });
    try {
      const result = checked<MockResult>("MockResult", await this.profiles.commands.post<unknown>(this.path(exam.exam_id, "/submit"), {}, clientId()));
      if (generation !== this.profiles.generation) return;
      const read = checked<MockExam>("MockExam", await this.profiles.commands.get<unknown>(this.path(exam.exam_id)));
      if (generation !== this.profiles.generation) return;
      this.drop();
      this.patch({ ...this.shown(read, true), result, marking: false, unsaved: [] });
      void this.load();
    } catch {
      if (generation === this.profiles.generation) this.patch({ marking: false, markFailed: true });
    }
  }

  /** Try marking again after it failed. */
  retryMark(): Promise<void> {
    return !this.view.timeUp && this.view.exam !== null && LOCKED_STATES.includes(this.view.exam.state)
      ? this.submit() : this.mark(this.view.timeUp);
  }

  /** Focus away for a while or a large paste during a sitting: counts only, the core keeps those over its thresholds (ASM-18). */
  integrity(kind: api_commands.MockIntegrity["kind"], amount: number): void {
    const exam = this.view.exam;
    if (exam === null || !LOCKED_STATES.includes(exam.state)) return;
    const body: api_commands.MockIntegrity = { kind, amount: Math.max(0, Math.round(amount)) };
    void this.profiles.commands.post<unknown>(this.path(exam.exam_id, "/integrity"), body, clientId()).catch(() => undefined);
  }

  /** The page of an imported past paper that holds a question's diagram (ASM-27), or null when it cannot be shown. */
  async figure(examId: string, questionId: string): Promise<Blob | null> {
    try {
      return await this.profiles.commands.request<Blob>("GET", this.path(examId, `/figures/${encodeURIComponent(questionId)}`), undefined, null, {}, true);
    } catch {
      return null;
    }
  }

  /** Back to the place: the sitting on screen is put down (an open one stays open in the core). */
  leave(): void {
    void this.flush();
    this.patch({ exam: null, result: null, current: null, seen: [], marking: false, markFailed: false, timeUp: false, refused: null, openFailed: false });
    void this.load();
  }
}
