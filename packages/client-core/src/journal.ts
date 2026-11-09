// UIA-06: a disposable profile-store cache. The authoritative snapshot always wins after the first paint.
import type { ClientJournal, Session } from "@jana/contracts-ts";
import { validators } from "@jana/contracts-ts/client";
import { hydrate, type CoreSnapshot } from "./hydrate.js";
import { clientId } from "./ids.js";
import type { Envelope } from "./queue.js";
import { reduce } from "./reducer.js";
import type { Store } from "./store.js";
import { readEnvelope } from "./tolerant.js";
import type { CommandClient } from "./stream/commands.js";

export function journalTail(source: CoreSnapshot): CoreSnapshot {
  const messages = source.messages.slice(-50);
  const turns = new Set(messages.map((m) => m.turn_id).filter((id) => id !== null));
  const parts = Object.fromEntries(Object.entries(source.parts).filter(([, p]) => p.turn_id !== null && turns.has(p.turn_id)));
  const keep = new Set(Object.keys(parts));
  const boards = Object.fromEntries(Object.entries(source.boards).filter(([, b]) => (b.part_id !== null && keep.has(b.part_id)) || b.board_id === Object.keys(source.boards).at(-1)));
  return { ...source, messages, parts, boards, visuals: Object.fromEntries(Object.entries(source.visuals).filter(([, v]) => keep.has(v.part_id))), open_requests: source.open_requests.filter((r) => keep.has(r.part_id)) };
}

/** The session's companion buttons as the snapshot carries them (SPEC §19.3.3), or nothing when it has none. */
function actionsOf(store: Store, sessionId: string): Pick<CoreSnapshot, "actions"> {
  const kept = Object.values(store.actions).filter((a) => a.session_id === sessionId);
  if (kept.length === 0) return {};
  return { actions: Object.fromEntries(kept.map(({ session_id: _session, ...action }) => [action.action_id, action])) };
}

/** Compact a long envelope run into the same public snapshot shape; never persist connections or credentials. */
export function journalSnapshot(store: Store, sessionId: string): CoreSnapshot | null {
  const session = store.sessions[sessionId], cursor = store.cursors[sessionId];
  if (!session || !cursor) return null;
  const turns = session.turn_ids.map((id) => store.turns[id]).filter((t): t is NonNullable<typeof t> => t !== undefined);
  const turnIds = new Set(turns.map((t) => t.turn_id));
  const parts = Object.fromEntries(Object.entries(store.parts).filter(([, p]) => p.turn_id !== null && turnIds.has(p.turn_id)));
  const messages: CoreSnapshot["messages"] = [
    ...Object.values(store.messages).filter((m) => m.session_id === sessionId).map((m) => ({ role: "student" as const, message_id: m.message_id, client_message_id: m.client_message_id, turn_id: m.turn_id, content: m.content, ts: m.ts })),
    ...turns.map((t) => ({ ...t, role: "tutor" as const })),
  ].sort((a, b) => (a.ts ?? 0) - (b.ts ?? 0));
  const boards = Object.fromEntries(session.board_ids.flatMap((id) => {
    const b = store.boards[id]; return b ? [[id, { ...b, order: b.step_ids, awaiting: b.await }]] : [];
  }));
  const active = turns.find((t) => t.status === "streaming");
  return journalTail({ session: { session_id: session.session_id, title: session.title, mode: session.mode as Session["mode"], subject: session.subject, topic_id: session.topic_id, created_ts: session.created_ts, updated_ts: session.updated_ts, archived: session.archived }, cursor, messages, parts, boards, visuals: Object.fromEntries(Object.entries(store.visuals).filter(([, v]) => parts[v.part_id]).map(([id, v]) => [id, { ...v, step_index: v.step }])), open_requests: Object.values(store.requests).filter((r) => parts[r.part_id] && r.state === "open"), active_turn: active ? { turn_id: active.turn_id, status: active.phase ?? {} } : null, ...actionsOf(store, sessionId) });
}

export class SessionJournal {
  private base: CoreSnapshot | null = null;
  private envelopes: Envelope[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private running = false;
  private dirty = false;
  private readonly path: string;
  constructor(private readonly commands: CommandClient, private readonly sessionId: string, private readonly state: () => Store) { this.path = `/v1/sessions/${sessionId}/journal`; }
  async paint(current: Store): Promise<Store | null> {
    try {
      const cached = await this.commands.get<ClientJournal>(this.path);
      if (this.closed || !validators["api/commands#/$defs/ClientJournal"]?.(cached) || !cached.snapshot || cached.snapshot.session.session_id !== this.sessionId) return null;
      let next = hydrate(current, cached.snapshot as CoreSnapshot);
      for (const raw of cached.envelopes) {
        const read = readEnvelope(raw);
        if (read.kind !== "accept" || read.envelope.session_id !== this.sessionId || (read.envelope as unknown as Envelope).event.type === "reasoning.delta") return null;
        next = reduce(next, read.envelope as unknown as Envelope);
      }
      return next;
    } catch { return null; } // missing/old/corrupt cache never prevents normal startup
  }
  fresh(snapshot: CoreSnapshot): void { this.base = journalTail(snapshot); this.envelopes = []; this.schedule(); }
  append(batch: Envelope[]): void {
    this.envelopes.push(...batch.filter((e) => e.event.type !== "reasoning.delta"));
    if (this.envelopes.length > 1024 || batch.some((e) => e.event.type === "turn.end")) { this.base = journalSnapshot(this.state(), this.sessionId); this.envelopes = []; }
    this.schedule();
  }
  private schedule(): void {
    this.dirty = true;
    if (this.closed || this.timer !== null) return;
    this.timer = setTimeout(() => { this.timer = null; void this.write(); }, 500);
  }
  private async write(): Promise<void> {
    if (this.closed || this.running || !this.base || !this.dirty) return;
    this.running = true; this.dirty = false;
    try {
      let body = { snapshot: this.base, envelopes: [...this.envelopes] };
      if (JSON.stringify(body).length > 256 * 1024) {
        this.base = journalSnapshot(this.state(), this.sessionId); this.envelopes = [];
        if (!this.base) return;
        body = { snapshot: this.base, envelopes: [] };
      }
      await this.commands.put(this.path, body, clientId());
    } catch { /* cache loss is recoverable from the core snapshot and editor drafts */ }
    finally { this.running = false; if (this.dirty) this.schedule(); }
  }
  close(): void { this.closed = true; this.base = null; this.envelopes = []; if (this.timer !== null) clearTimeout(this.timer); }
}
