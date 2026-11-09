// Profile-token commands only: learner text never goes into browser storage.
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
interface Draft { document: Record<string, unknown>; revision: number }
export class DraftController {
  private revision = 0;
  private ready = false;
  private closed = false;
  private changed = false;
  private pending: Record<string, unknown> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private running: Promise<void> | null = null;
  constructor(private readonly commands: CommandClient, private readonly path: string, private readonly restore: (doc: Record<string, unknown>) => void, private readonly failed: () => void) {}
  async open(): Promise<void> {
    try {
      const draft = await this.commands.get<Draft>(this.path);
      if (this.closed) return;
      this.revision = draft.revision;
      this.ready = true;
      if (!this.changed && draft.revision > 0) this.restore(draft.document);
      if (this.pending !== null) void this.flush();
    } catch { if (!this.closed) this.failed(); }
  }
  change(document: Record<string, unknown>): void {
    this.changed = true;
    this.pending = document;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => { this.timer = null; void this.flush(); }, 500);
  }
  async flush(): Promise<void> {
    if (this.running !== null) { await this.running; if (this.pending !== null) await this.flush(); return; }
    if (!this.ready || this.closed || this.pending === null) return;
    const document = this.pending;
    this.pending = null;
    this.running = (async () => {
      try {
        const saved = await this.commands.put<Draft>(this.path, { document, expected_revision: this.revision }, clientId());
        this.revision = saved.revision;
      } catch { if (!this.closed) { this.pending ??= document; this.failed(); } }
    })();
    const mine = this.running;
    await mine;
    if (this.running === mine) this.running = null;
  }
  /** The editor was emptied (a question sent, a draft dropped): save the empty `document` now, after any save already
   * on its way, and finish even if the editor closes straight after (Home's Ask box hands its question to the tutor and
   * goes, UXS-12), so the emptied text does not come back from the saved draft. */
  clear(document: Record<string, unknown>): Promise<void> {
    if (this.timer !== null) { clearTimeout(this.timer); this.timer = null; }
    this.pending = null;
    this.changed = true;
    const before = this.running;
    const work = (async () => {
      if (before !== null) await before;
      if (!this.ready) return;
      try {
        const saved = await this.commands.put<Draft>(this.path, { document, expected_revision: this.revision }, clientId());
        this.revision = saved.revision;
      } catch { if (!this.closed) this.failed(); }
    })();
    this.running = work;
    return work.finally(() => { if (this.running === work) this.running = null; });
  }
  close(): void {
    this.closed = true;
    this.pending = null;
    if (this.timer !== null) clearTimeout(this.timer);
  }
}
