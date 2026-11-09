import { errors, type learning_student_records } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type SavedQuestion = learning_student_records.SavedQuestion;
export interface SavedQuestionsView {
  status: "idle" | "loading" | "ready" | "failed" | "unavailable";
  questions: SavedQuestion[];
  selected: string | null;
  busy: boolean;
  error: boolean;
  uncertain: boolean;
  saved: string | null;
}
export const EMPTY_SAVED_QUESTIONS: SavedQuestionsView = { status: "idle", questions: [], selected: null, busy: false, error: false, uncertain: false, saved: null };
export function readSavedQuestion(value: unknown): SavedQuestion {
  const problems = errors("learning/student-records#/$defs/SavedQuestion", value);
  if (problems.length) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as SavedQuestion;
}
type Mutation = { method: "save"; session_id: string; turn_id: string; key: string } | { method: "status"; id: string; status: SavedQuestion["status"]; key: string } | { method: "delete"; id: string; key: string };
export class SavedQuestionsController {
  private view = EMPTY_SAVED_QUESTIONS;
  private listeners = new Set<() => void>();
  private revision = 0;
  private pending: Mutation | null = null;
  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => { this.revision++; this.pending = null; this.set(EMPTY_SAVED_QUESTIONS); });
  }
  getView = (): SavedQuestionsView => this.view;
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  private set(view: SavedQuestionsView) { this.view = view; for (const fn of this.listeners) fn(); }
  private patch(view: Partial<SavedQuestionsView>) { this.set({ ...this.view, ...view }); }
  async load(): Promise<void> {
    if (this.pending !== null || this.view.busy) return;
    const generation = this.profiles.generation, revision = ++this.revision;
    this.patch({ status: "loading", error: false });
    try {
      const raw = await this.profiles.commands.get<unknown>("/v1/saved-questions");
      if (!Array.isArray(raw)) throw new CommandError(200, "invalid_reply", false, undefined);
      const questions = raw.map(readSavedQuestion).sort((a, b) => b.snapshot.created_at.localeCompare(a.snapshot.created_at));
      if (generation !== this.profiles.generation || revision !== this.revision) return;
      this.patch({ status: "ready", questions, selected: questions.some((q) => q.question_id === this.view.selected) ? this.view.selected : null });
    } catch (error) {
      if (generation !== this.profiles.generation || revision !== this.revision) return;
      this.patch({ status: error instanceof CommandError && error.code === "not_found" ? "unavailable" : "failed" });
    }
  }
  select(id: string | null): void { if (!this.view.busy && !this.view.uncertain) this.patch({ selected: id, error: false }); }
  save(session_id: string, turn_id: string): Promise<boolean> { return this.run(this.pending ?? { method: "save", session_id, turn_id, key: clientId() }); }
  status(id: string, status: SavedQuestion["status"]): Promise<boolean> { return this.run(this.pending ?? { method: "status", id, status, key: clientId() }); }
  remove(id: string): Promise<boolean> { return this.run(this.pending ?? { method: "delete", id, key: clientId() }); }
  retry(): Promise<boolean> { return this.pending === null ? Promise.resolve(false) : this.run(this.pending); }
  private async run(mutation: Mutation): Promise<boolean> {
    if (this.view.busy) return false;
    const generation = this.profiles.generation, revision = ++this.revision;
    this.pending = mutation; this.patch({ busy: true, error: false, saved: null });
    try {
      let record: SavedQuestion | null = null;
      const path = mutation.method === "save" ? "/v1/saved-questions" : `/v1/saved-questions/${encodeURIComponent(mutation.id)}`;
      if (mutation.method === "delete") await this.profiles.commands.delete(path, mutation.key);
      else if (mutation.method === "save") record = readSavedQuestion(await this.profiles.commands.post(path, { session_id: mutation.session_id, turn_id: mutation.turn_id }, mutation.key));
      else record = readSavedQuestion(await this.profiles.commands.patch(path, { status: mutation.status }, mutation.key));
      if (record !== null && mutation.method !== "save" && record.question_id !== mutation.id) throw new CommandError(200, "invalid_reply", false, undefined);
      if (generation !== this.profiles.generation || revision !== this.revision) return false;
      this.pending = null;
      if (mutation.method === "save" && record === null) throw new CommandError(200, "invalid_reply", false, undefined);
      const id = mutation.method === "save" ? (record as SavedQuestion).question_id : mutation.id;
      const questions = this.view.questions.filter((q) => q.question_id !== id);
      if (record !== null) questions.unshift(record);
      this.patch({ status: "ready", questions, busy: false, uncertain: false, selected: mutation.method === "delete" && this.view.selected === id ? null : this.view.selected, saved: mutation.method === "save" ? id : null });
      return true;
    } catch (error) {
      if (generation !== this.profiles.generation || revision !== this.revision) return false;
      const definitive = error instanceof CommandError && error.status >= 400 && error.status < 500;
      if (definitive) this.pending = null;
      this.patch({ busy: false, error: true, uncertain: !definitive });
      return false;
    }
  }
}
