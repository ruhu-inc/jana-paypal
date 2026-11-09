// Imported past papers and exam dates (ASM-27, LRN-44; D119).
//
// Past papers: a learner adds a past paper (and its mark scheme, if they have it) to the study area, as a file there
// or as text pasted here, and imports it (`POST /v1/past-papers`): the core keeps the questions as written and says
// how each part will be marked (checked by tools, against the scheme's key, or judged). The paper is then sat with the
// mock controller (`MockController.choose` with its `ppr_` id), timed and locked like any mock; its report carries
// `past_paper` with how each mark was decided, and never changes mastery. A private study source is read with the
// study area's own open grant (`X-Study-Open`), asked for here and dropped after the import. A link is not taken: the
// app fetches no web page (STU-13, D64), so the learner saves the paper and adds the file.
//
// Exam dates: per subject or paper, `GET/POST /v1/exam-dates` and `PATCH/DELETE /v1/exam-dates/{id}`; the reply
// carries the countdown to the nearest exam and the reminders due now, which are shown on this device only and put
// away with `dismiss`. Everything here belongs to the unlocked profile and is dropped when it closes (SEC-19).
import { errors, type api_commands, type study_workspace } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type PastPaperSummary = api_commands.PastPaperSummary;
export type PastPaperView = api_commands.PastPaperView;
export type ExamDate = api_commands.ExamDate;
export type ExamCountdown = api_commands.ExamCountdown;
export type ExamReminder = api_commands.ExamReminder;
export type ExamDateList = api_commands.ExamDateList;
export type PastPaperSubject = api_commands.PastPaperImport["subject"];

type Listener = () => void;

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as T;
}

function refusal(error: unknown): string {
  if (error instanceof CommandError) return String(error.details?.reason ?? error.code);
  return "unreachable";
}

/** A study-area source a paper can be imported from: read and ready. */
export interface PaperSource {
  sourceId: string;
  revision: number;
  /** Null for a private source, whose title the list never shows. */
  title: string | null;
  private: boolean;
}

export interface PastPaperDraft {
  title: string;
  subject: PastPaperSubject;
  minutes: number;
  /** The paper: a ready study source, or text pasted here (added to the study area as a text source first). */
  paper: { sourceId: string } | { text: string };
  scheme: { sourceId: string } | { text: string } | null;
}

export interface PastPapersView {
  status: "idle" | "loading" | "ready" | "unavailable" | "failed";
  papers: PastPaperSummary[];
  sources: PaperSource[];
  importing: boolean;
  /** The reason code of the last refused import (no_questions_found, feature_disabled, ...). */
  refused: string | null;
  /** The paper just imported: its questions and how each will be marked, shown before it is sat. */
  imported: PastPaperView | null;
}

export const EMPTY_PAST_PAPERS: PastPapersView = { status: "idle", papers: [], sources: [], importing: false, refused: null, imported: null };

export class PastPaperController {
  private view: PastPapersView = EMPTY_PAST_PAPERS;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_PAST_PAPERS));
  }

  getView = (): PastPapersView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: PastPapersView): void {
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<PastPapersView>): void {
    this.set({ ...this.view, ...change });
  }

  /** The imported papers, and the study-area sources a new one can come from. */
  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.status !== "ready") this.patch({ status: "loading" });
    try {
      const listed = checked<api_commands.PastPaperList>("PastPaperList", await this.profiles.commands.get<unknown>("/v1/past-papers"));
      let sources: PaperSource[] = [];
      try {
        const snapshot = await this.profiles.commands.get<study_workspace.StudySnapshot>("/v1/study/workspace");
        // A private source lists no title or state (STU-16): it is offered by its kind, and the import says if unread.
        sources = snapshot.sources.items.flatMap((s): PaperSource[] => s.private === true
          ? [{ sourceId: s.source_id, revision: s.revision, title: null, private: true }]
          : "state" in s && s.state === "ready" ? [{ sourceId: s.source_id, revision: s.revision, title: s.title, private: false }] : []);
      } catch {
        /* no study area here (feature off or offline-only): only pasted text can be imported */
      }
      if (generation !== this.profiles.generation) return;
      this.patch({ status: "ready", papers: listed.papers, sources });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      const missing = error instanceof CommandError && (error.status === 404 || error.status === 405);
      this.patch({ status: missing ? "unavailable" : "failed" });
    }
  }

  /** A selection for the import, adding pasted text to the study area first; with the open grant of a private one. */
  private async selection(choice: { sourceId: string } | { text: string }, title: string, grants: string[]): Promise<api_commands.PastPaperImport["paper"]> {
    if ("text" in choice) {
      const made = await this.profiles.commands.post<study_workspace.Source>("/v1/study/sources", { kind: "text", title: title.slice(0, 200), text: choice.text }, clientId());
      return { source_id: made.source_id, revision: made.revision };
    }
    const source = this.view.sources.find((s) => s.sourceId === choice.sourceId);
    if (source === undefined) throw new CommandError(404, "not_found", false, { reason: "not_found" });
    if (source.private) {
      const opened = await this.profiles.commands.post<study_workspace.OpenedSource>(`/v1/study/sources/${encodeURIComponent(source.sourceId)}/open`, { confirm: true }, clientId());
      grants.push(opened.open_token);
    }
    return { source_id: source.sourceId, revision: source.revision };
  }

  /** Import a paper: the core reads it and says how each part will be marked. True when imported. */
  async importPaper(draft: PastPaperDraft): Promise<boolean> {
    if (this.view.importing) return false;
    const generation = this.profiles.generation;
    this.patch({ importing: true, refused: null, imported: null });
    const grants: string[] = [];
    try {
      const paper = await this.selection(draft.paper, draft.title, grants);
      const scheme = draft.scheme === null ? null : await this.selection(draft.scheme, `${draft.title} (mark scheme)`, grants);
      const body: api_commands.PastPaperImport = {
        client_import_id: clientId(), title: draft.title.trim().slice(0, 200), subject: draft.subject,
        duration_minutes: Math.max(1, Math.min(600, Math.round(draft.minutes))), paper, mark_scheme: scheme, syllabus: null,
      };
      const headers: Record<string, string> = grants.length > 0 ? { "X-Study-Open": grants.join(",") } : {};
      const view = checked<PastPaperView>("PastPaperView", await this.profiles.commands.post<unknown>("/v1/past-papers", body, body.client_import_id, headers));
      if (generation !== this.profiles.generation) return false;
      this.patch({ importing: false, imported: view });
      await this.load();
      return true;
    } catch (error) {
      if (generation === this.profiles.generation) this.patch({ importing: false, refused: refusal(error) });
      return false;
    }
    // The open grants expire on their own (five minutes); nothing here keeps them.
  }

  /** Remove an imported paper; its sittings and reports stay. */
  async remove(paperId: string): Promise<void> {
    try {
      await this.profiles.commands.delete<unknown>(`/v1/past-papers/${encodeURIComponent(paperId)}`, clientId());
    } catch (error) {
      this.patch({ refused: refusal(error) });
    }
    await this.load();
  }

  /** Put away the import's summary. */
  dismiss(): void {
    this.patch({ imported: null, refused: null });
  }
}

// --- exam dates (LRN-44) ----------------------------------------------------------------------------------------------

export interface ExamDatesView {
  status: "idle" | "loading" | "ready" | "unavailable" | "failed";
  exams: ExamDate[];
  countdown: ExamCountdown | null;
  reminders: ExamReminder[];
  busy: boolean;
  refused: string | null;
}

export const EMPTY_EXAM_DATES: ExamDatesView = { status: "idle", exams: [], countdown: null, reminders: [], busy: false, refused: null };

export interface ExamDateDraft {
  subject: ExamDate["subject"];
  name: string;
  /** YYYY-MM-DD, the learner's local date. */
  date: string;
  /** HH:MM, or null. */
  time: string | null;
  reminders: boolean;
}

export class ExamDateController {
  private view: ExamDatesView = EMPTY_EXAM_DATES;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_EXAM_DATES));
  }

  getView = (): ExamDatesView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: ExamDatesView): void {
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<ExamDatesView>): void {
    this.set({ ...this.view, ...change });
  }

  private shown(list: ExamDateList): Partial<ExamDatesView> {
    return { status: "ready", exams: list.exams, countdown: list.countdown, reminders: list.reminders_due };
  }

  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.status !== "ready") this.patch({ status: "loading" });
    try {
      const list = checked<ExamDateList>("ExamDateList", await this.profiles.commands.get<unknown>("/v1/exam-dates"));
      if (generation === this.profiles.generation) this.patch(this.shown(list));
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      const missing = error instanceof CommandError && (error.status === 404 || error.status === 405);
      this.patch({ status: missing ? "unavailable" : "failed" });
    }
  }

  private async act(work: () => Promise<unknown>): Promise<boolean> {
    if (this.view.busy) return false;
    const generation = this.profiles.generation;
    this.patch({ busy: true, refused: null });
    try {
      await work();
      if (generation !== this.profiles.generation) return false;
      this.patch({ busy: false });
      await this.load();
      return true;
    } catch (error) {
      if (generation === this.profiles.generation) this.patch({ busy: false, refused: refusal(error) });
      return false;
    }
  }

  add(draft: ExamDateDraft): Promise<boolean> {
    const body: api_commands.ExamDateCreate = {
      client_exam_date_id: clientId(), subject: draft.subject, name: draft.name.trim().slice(0, 80), date: draft.date, time: draft.time, reminders: draft.reminders,
    };
    return this.act(() => this.profiles.commands.post<unknown>("/v1/exam-dates", body, body.client_exam_date_id).then((v) => checked<ExamDate>("ExamDate", v)));
  }

  change(examId: string, change: api_commands.ExamDatePatch): Promise<boolean> {
    return this.act(() => this.profiles.commands.patch<unknown>(`/v1/exam-dates/${encodeURIComponent(examId)}`, change, clientId()).then((v) => checked<ExamDate>("ExamDate", v)));
  }

  remove(examId: string): Promise<boolean> {
    return this.act(() => this.profiles.commands.delete<unknown>(`/v1/exam-dates/${encodeURIComponent(examId)}`, clientId()));
  }

  /** Put away a reminder that was shown; it does not come back for that day before the exam. */
  dismiss(reminder: ExamReminder): Promise<boolean> {
    return this.change(reminder.exam_id, { dismiss: reminder.kind_id });
  }
}
