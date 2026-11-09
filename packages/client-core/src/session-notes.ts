import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export const NOTE_SECTIONS = ["worked_on", "method", "corrections", "revise"] as const;
export interface NoteReference { session_id: string; turn_id: string; trace_id: string | null }
export interface NoteStatement { section: typeof NOTE_SECTIONS[number]; text: string; reference: NoteReference }
export interface SessionNote {
  note_id: string; session_id: string; kind: "session"; title: string | null; subject: string | null; kc_ids: string[];
  statements: NoteStatement[]; state: "queued" | "writing" | "ready" | "failed" | "no_model"; private: boolean;
  source_revision: number; withheld: number; created_at: number; updated_at: number;
}
export interface SessionNotesView {
  status: "idle" | "loading" | "ready" | "unavailable" | "failed";
  notes: SessionNote[]; opened: SessionNote | null; busy: boolean; error: boolean; query: string;
}
export const EMPTY_SESSION_NOTES: SessionNotesView = { status: "idle", notes: [], opened: null, busy: false, error: false, query: "" };
export function readSessionNote(value: unknown, opened = false): SessionNote {
  const bad = () => new CommandError(200, "invalid_reply", false, undefined);
  if (typeof value !== "object" || value === null) throw bad();
  const n = value as SessionNote;
  if (!/^snt_[0-9A-HJKMNP-TV-Z]{26}$/.test(n.note_id) || !/^ses_[0-9A-HJKMNP-TV-Z]{26}$/.test(n.session_id)
    || n.kind !== "session" || !(n.title === null || typeof n.title === "string") || !(n.subject === null || typeof n.subject === "string")
    || !Array.isArray(n.kc_ids) || !n.kc_ids.every((k) => typeof k === "string") || !Array.isArray(n.statements)
    || !["queued", "writing", "ready", "failed", "no_model"].includes(n.state) || typeof n.private !== "boolean"
    || ![n.created_at, n.updated_at].every((t) => Number.isSafeInteger(t) && t >= 0)) throw bad();
  const statements = n.statements.filter((s) => s !== null && typeof s === "object" && NOTE_SECTIONS.includes(s.section)
    && typeof s.text === "string" && s.text.length <= 400 && s.reference !== null && typeof s.reference === "object"
    && s.reference.session_id === n.session_id && /^trn_[0-9A-HJKMNP-TV-Z]{26}$/.test(s.reference.turn_id));
  return n.private && !opened ? { ...n, title: null, kc_ids: [], statements: [] } : { ...n, statements };
}
export class SessionNotesController {
  private view = EMPTY_SESSION_NOTES;
  private listeners = new Set<() => void>();
  private revision = 0;
  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => { this.revision++; this.set(EMPTY_SESSION_NOTES); });
  }
  getView = (): SessionNotesView => this.view;
  subscribe = (fn: () => void): (() => void) => { this.listeners.add(fn); return () => this.listeners.delete(fn); };
  private set(view: SessionNotesView) { this.view = view; for (const fn of this.listeners) fn(); }
  private patch(view: Partial<SessionNotesView>) { this.set({ ...this.view, ...view }); }
  async load(query = this.view.query): Promise<void> {
    const generation = this.profiles.generation, revision = ++this.revision;
    this.patch({ query, status: this.view.status === "idle" ? "loading" : this.view.status });
    try {
      const reply = await this.profiles.commands.get<unknown>(`/v1/session-notes?q=${encodeURIComponent(query.slice(0, 200))}`);
      if (!Array.isArray(reply)) throw new CommandError(200, "invalid_reply", false, undefined);
      const notes = reply.map((n) => readSessionNote(n));
      if (generation === this.profiles.generation && revision === this.revision) {
        const previous = this.view.opened, fresh = notes.find((n) => n.note_id === previous?.note_id);
        const opened = previous === null || fresh === undefined ? null : fresh.private ? (previous.private ? previous : null) : fresh;
        this.patch({ status: "ready", notes, opened, error: false });
      }
    } catch (error) {
      if (generation === this.profiles.generation && revision === this.revision) this.patch({ status: error instanceof CommandError && error.code === "not_found" ? "unavailable" : "failed" });
    }
  }
  close(): void { this.revision++; this.patch({ opened: null, error: false }); }
  async end(sessionId: string): Promise<boolean> {
    const generation = this.profiles.generation;
    try {
      await this.profiles.commands.post(`/v1/sessions/${encodeURIComponent(sessionId)}/end`, {}, clientId());
      if (generation !== this.profiles.generation) return false;
      this.close();
      const loading = this.load("");
      const revision = this.revision;
      await loading;
      if (generation !== this.profiles.generation) return false;
      // Keep private notes behind their existing confirmation. A newer search or
      // navigation action takes precedence over this automatic selection.
      if (revision === this.revision && this.view.status === "ready") {
        const note = this.view.notes.find((n) => n.session_id === sessionId);
        if (note !== undefined && !note.private) await this.act(note.note_id, "open");
      }
      return generation === this.profiles.generation;
    } catch { return false; }
  }
  async act(id: string, action: "open" | "private" | "public" | "retry" | "delete", confirmed = false): Promise<boolean> {
    if (this.view.busy) return false;
    const generation = this.profiles.generation;
    this.revision++; this.patch({ busy: true, error: false });
    const path = `/v1/session-notes/${encodeURIComponent(id)}`, key = clientId();
    try {
      let note: SessionNote | null = null;
      if (action === "delete") await this.profiles.commands.delete(path, key);
      else if (action === "private" || action === "public") note = readSessionNote(await this.profiles.commands.patch(path, { private: action === "private" }, key));
      else note = readSessionNote(await this.profiles.commands.post(`${path}/${action}`, action === "open" ? { confirm: confirmed } : {}, key), action === "open");
      if (generation !== this.profiles.generation) return false;
      if (note !== null && note.note_id !== id) throw new CommandError(200, "invalid_reply", false, undefined);
      if (action === "open") this.patch({ opened: note, busy: false });
      else {
        this.patch({ opened: this.view.opened?.note_id === id ? null : this.view.opened, busy: false });
        await this.load();
      }
      return true;
    } catch {
      if (generation === this.profiles.generation) this.patch({ busy: false, error: true });
      return false;
    }
  }
}
export function sessionNoteText(note: SessionNote): string {
  return [note.title ?? "", ...NOTE_SECTIONS.flatMap((section) => note.statements.filter((s) => s.section === section)
    .map((s) => `${s.text}\n${s.reference.session_id} / ${s.reference.turn_id}`))].join("\n\n");
}
