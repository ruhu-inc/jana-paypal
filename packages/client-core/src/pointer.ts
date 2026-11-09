// Speech-linked cues on the client (VOC-10, VOC-51, UXS-63, VSL-45). The engine sends the accepted turn's cues on its
// tutor captions: a character offset into the spoken text and an id on screen (a board step `stp_…`, or a part of a
// visual `vis_…:<part>`). This reducer decides when each cue fires, from the audio the student actually hears: the
// playback position the audio layer reads off its own clock (frames played, less the output device's latency), or
// the operating system synthesiser's word boundaries. A caption arriving, model text or queued audio never fires a
// cue. It also clears the temporary pointer at once on interruption, a flush, a session change, or the removal or
// revision of its target, and after its time to live. It holds ids and times only, never audio or text beyond the
// caption it was given. A cue may also point at a place on the student's own photo (`cap_…:rgn_<n>`, JM-D10): a line
// of their working or a part of their sketch, outlined by SAM 3.1.

/** Where a cue's word time comes from: `measured` (word boundaries or alignment data from the speech engine) or
 * `estimated` (the caption synchroniser's syllable estimate inside a spoken unit). Estimated timing is never
 * reported as measured (VOC-51). */
export type CueTiming = "measured" | "estimated";

export interface CaptionCue { char_offset: number; target: string; action?: "point" | "write"; end_offset?: number }
export interface CaptionWord { w: string; start_ms: number }

/** One thing on screen a cue addresses, by id only (VSL-19): a board line, a part of a visual, or a region of the
 * student's photo (`photo_region`, JM-D10). */
export type PointerTarget = { kind: "step"; step_id: string } | { kind: "part"; visual_id: string; part_id: string }
  | { kind: "photo_region"; capture_id: string; region_id: string } | { kind: "kc"; kc_id: string };

/** The temporary pointer (the ring of TABLE DSN-10a), and a line being written on in time with the words. */
export interface PointerView {
  target: PointerTarget;
  /** The playback position (ms of the response's audio) at which it fired, and its word's scheduled time. */
  fired_ms: number;
  word_ms: number;
  timing: CueTiming;
}
export interface WritingView { step_id: string; progress: number; timing: CueTiming }
/** The tutor's pen (UXS-56): at rest (at the end of the line being written, or nowhere) or at a cue's target, which it
 * reaches before the ring appears. It never carries meaning by itself: the ring and "Showing: …" do. */
export interface PenView { target: PointerTarget | null; since_ms: number }
/** How long before a cue's word the pen sets off, so it arrives before the ring (the pen's travel time, DSN-10). */
export const PEN_LEAD_MS = 240;

/** A fired cue, for the developer latency panel and the measurement script: never text. */
export interface CueRecord { target: string; action: "point" | "write"; word_ms: number; fired_ms: number; timing: CueTiming }

const STEP = /^stp_[0-9A-HJKMNP-TV-Z]{26}$/;
const PART = /^(vis_[0-9A-HJKMNP-TV-Z]{26}):([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)$/;
const PHOTO_REGION = /^(cap_[0-9A-HJKMNP-TV-Z]{26}):(rgn_[0-9]{1,3})$/;
// A skill on the concept map (UXS-22 map view).
const KC_NODE = /^kc:(kc\.(?:mth|phy|chm|bio|acc)\.(?:o|a)\.[a-z0-9_]+\.[a-z0-9_]+)$/;

/** A cue target as an id pair, or null for anything that is not one exact id (never guessed, VSL-45). */
export function parseTarget(target: string): PointerTarget | null {
  if (STEP.test(target)) return { kind: "step", step_id: target };
  const kc = KC_NODE.exec(target);
  if (kc !== null) return { kind: "kc", kc_id: kc[1] ?? "" };
  const photo = PHOTO_REGION.exec(target);
  if (photo !== null) return { kind: "photo_region", capture_id: photo[1] ?? "", region_id: photo[2] ?? "" };
  const part = PART.exec(target);
  return part === null ? null : { kind: "part", visual_id: part[1] ?? "", part_id: part[2] ?? "" };
}

/** The start time of the word at or after `offset` in `text`, from the caption's word timings (in the order of the
 * text's words), or null when the words do not reach it yet. */
export function wordTime(text: string, words: readonly CaptionWord[], offset: number): number | null {
  const pattern = /\S+/g;
  let index = 0;
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    const end = match.index + match[0].length;
    if (end > offset) return words[index]?.start_ms ?? null;
    index += 1;
  }
  return null;
}

/** A spoken word's length when no word follows it to say when it ended (UXS-55): about one word at a teaching pace. */
export const LAST_WORD_MS = 300;

/** The time the words up to `offset` have been spoken: the start of the first word after it; when the range ends on
 * the caption's last word, that word's start plus one word's length (so a write-on never stalls short of its end). */
export function wordsEndTime(text: string, words: readonly CaptionWord[], offset: number): number | null {
  const pattern = /\S+/g;
  let index = 0;
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    if (match.index >= offset) return words[index]?.start_ms ?? null;
    index += 1;
  }
  const last = words[index - 1];
  return last === undefined || index > words.length ? null : last.start_ms + LAST_WORD_MS;
}

interface Scheduled { cue: CaptionCue; target: PointerTarget; key: string }

export interface CueSyncState { turnId: string | null; pointer: PointerView | null; writing: WritingView | null; pen: PenView }
const REST: PenView = { target: null, since_ms: 0 };
const IDLE: CueSyncState = { turnId: null, pointer: null, writing: null, pen: REST };

export class CueSync {
  private state: CueSyncState = IDLE;
  private text = "";
  private words: readonly CaptionWord[] = [];
  private cues: Scheduled[] = [];
  private fired = new Set<string>();
  private timing: CueTiming = "estimated";
  private firedAtWall = 0;
  private writeStart = 0;
  private writeEnd: number | null = null;
  private ended = new Set<string>();
  private latex = new Map<string, string>();
  private headed = new Set<string>();
  readonly records: CueRecord[] = [];
  /** Cue targets the pen could not reach because they were not on screen (UXS-56, VSL-45): ids only. */
  readonly penMisses: string[] = [];

  /** `onScreen` says whether a target is drawn now; a pen never heads for one that is not (VSL-45). */
  constructor(private readonly ttlMs: number, private readonly onChange: () => void = () => undefined,
    private readonly onScreen: (target: PointerTarget) => boolean = () => true) {}

  getState = (): CueSyncState => this.state;
  private set(next: Partial<CueSyncState>): void {
    this.state = { ...this.state, ...next };
    this.onChange();
  }

  /** A tutor caption of turn `turnId`: schedules its cues; never fires one. A caption of a turn already stopped or
   * interrupted is ignored, so its cues can never update the screen afterwards (VOC-52). */
  caption(turnId: string, text: string, words: readonly CaptionWord[], cues: readonly CaptionCue[], timing: CueTiming): void {
    if (this.ended.has(turnId)) return;
    if (this.state.turnId !== turnId) {
      this.fired.clear();
      this.headed.clear();
      this.writeEnd = null;
      this.set({ ...IDLE, turnId });
    }
    this.text = text;
    this.words = words;
    this.timing = timing;
    this.cues = [];
    for (const cue of cues) {
      const target = parseTarget(cue.target);
      if (target === null) continue; // not one exact id: nothing is drawn (VSL-45)
      this.cues.push({ cue, target, key: `${cue.char_offset}:${cue.target}:${cue.action ?? "point"}` });
    }
  }

  /** The actual playback position of the turn's audio (ms of the response's audio heard so far) at wall time `now`.
   * Fires every cue whose word has been reached, keeps the newest as the pointer, and advances a write-on. */
  progress(playedMs: number, now: number): void {
    if (this.state.turnId === null) return;
    let pointer = this.state.pointer;
    let writing = this.state.writing;
    let pen = this.state.pen;
    let changed = false;
    // The pen sets off for the next cue's target a little before its word, so it is there before the ring (UXS-56).
    for (const item of this.cues) {
      if (this.fired.has(item.key) || this.headed.has(item.key)) continue;
      const at = wordTime(this.text, this.words, item.cue.char_offset);
      if (at === null || at - PEN_LEAD_MS > playedMs) continue;
      this.headed.add(item.key);
      if (!this.onScreen(item.target)) { this.penMisses.push(item.cue.target); continue; } // the pen stays at rest
      pen = { target: item.target, since_ms: playedMs };
      changed = true;
    }
    for (const item of this.cues) {
      if (this.fired.has(item.key)) continue;
      const at = wordTime(this.text, this.words, item.cue.char_offset);
      if (at === null || at > playedMs) continue;
      this.fired.add(item.key);
      const action = item.cue.action === "write" && item.target.kind === "step" ? "write" : "point";
      this.records.push({ target: item.cue.target, action, word_ms: at, fired_ms: playedMs, timing: this.timing });
      if (action === "write" && item.target.kind === "step") {
        const end = item.cue.end_offset === undefined ? null : wordsEndTime(this.text, this.words, item.cue.end_offset);
        this.writeStart = at;
        this.writeEnd = end;
        writing = { step_id: item.target.step_id, progress: 0, timing: this.timing };
      }
      // The pen points at the line it writes too (UXS-56): one pointer at a time, the newest wins.
      pointer = { target: item.target, fired_ms: playedMs, word_ms: at, timing: this.timing };
      this.firedAtWall = now;
      changed = true;
    }
    if (writing !== null) {
      const start = this.writeStart;
      const end = this.writeEnd;
      const progress = end === null || end <= start ? (this.state.writing?.progress ?? 0) : Math.min(1, Math.max(0, (playedMs - start) / (end - start)));
      if (progress !== writing.progress) { writing = { ...writing, progress }; changed = true; }
    }
    if (pointer !== null && now - this.firedAtWall >= this.ttlMs) { pointer = null; changed = true; }
    // With the ring gone the pen returns to rest: the end of the line being written, or out of the way.
    if (pointer === null && this.state.pointer !== null) {
      pen = writing !== null ? { target: { kind: "step", step_id: writing.step_id }, since_ms: playedMs } : REST;
      changed = true;
    } else if (pointer === null && writing !== null && pen.target === null) {
      pen = { target: { kind: "step", step_id: writing.step_id }, since_ms: playedMs };
      changed = true;
    }
    if (changed) this.set({ pointer, writing, pen });
  }

  /** Cues of the current turn that have not fired yet. */
  pending(): number {
    return this.cues.filter((item) => !this.fired.has(item.key)).length;
  }

  /** Time passes without new audio (the pointer's time to live). */
  tick(now: number): void {
    if (this.state.pointer !== null && now - this.firedAtWall >= this.ttlMs) this.set({ pointer: null, pen: REST });
  }

  /** Interruption, cancellation, a flush or a session change (UXS-63): pending cues are dropped and the pointer and
   * write-on clear at once; the stopped turn's later captions are ignored. */
  stop(): void {
    if (this.state.turnId !== null) this.ended.add(this.state.turnId);
    this.cues = [];
    this.writeEnd = null;
    if (this.state.pointer !== null || this.state.writing !== null || this.state.turnId !== null) this.set({ ...IDLE });
  }

  /** A board line arrived (`board.step`): when it revises the pointer's line, the pointer clears (UXS-63). */
  boardStep(stepId: string, latex: string): void {
    const before = this.latex.get(stepId);
    this.latex.set(stepId, latex);
    const pointer = this.state.pointer;
    if (before !== undefined && before !== latex && pointer?.target.kind === "step" && pointer.target.step_id === stepId) this.set({ pointer: null });
  }

  /** The board was reset or a visual replaced: a pointer on an id no longer on screen clears (UXS-63, VSL-45). */
  targetsRemoved(gone: (target: PointerTarget) => boolean): void {
    const pointer = this.state.pointer;
    if (pointer !== null && gone(pointer.target)) this.set({ pointer: null });
    this.cues = this.cues.filter((item) => !gone(item.target));
  }
}

/** Fired cues split by how their word time was known, for timing reports: an estimate is never counted as measured
 * (VOC-51). `max_lag_ms` is the longest a cue fired after its word's time on the playback clock. */
export function cueReport(records: readonly CueRecord[]): Record<CueTiming, { count: number; max_lag_ms: number }> {
  const out: Record<CueTiming, { count: number; max_lag_ms: number }> = {
    measured: { count: 0, max_lag_ms: 0 }, estimated: { count: 0, max_lag_ms: 0 },
  };
  for (const record of records) {
    const side = out[record.timing];
    side.count += 1;
    side.max_lag_ms = Math.max(side.max_lag_ms, record.fired_ms - record.word_ms);
  }
  return out;
}
