// The student's notes on the client (LRN-36, MEM-09, MEM-11, MEM-14, MEM-27, MEM-28, LRN-37, UXS-28): the "What your
// tutor knows about you" page's read and corrections, forgetting a note, and the memory-use line of a turn.
//
// The page reads one reply, the learner view (`GET /v1/profiles/{id}/knows`): the profile's level and subjects, the KCs
// as bands, the misconceptions being watched, the preferences, the notes (each marked when it fails the core's render
// check), the open review items with the versions they were shown at, whether saving notes is on and the sharing
// flags. Every correction answers with the view as it is afterwards. These commands are the G90 contract additions
// (proposed, D72): until the command contract carries them, the types below mirror the proposal and `readKnows` checks
// a reply at the boundary. Everything here belongs to the unlocked profile and is dropped with it (SEC-19).
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { markForgotten } from "./reducer.js";
import type { ToolRec } from "./store.js";
import { CommandError, type CommandClient } from "./stream/commands.js";

type Listener = () => void;

/** The tools that read the student's memory (MEM-28): a completed read that returned at least one record shows the
 * memory-use line. */
export const MEMORY_READ_TOOLS: readonly string[] = ["memory_recall", "session_recall"];

/** How many records a memory read returned, from the engine's own `tool.state` (never from the model's words):
 * `state.output.items` on a completed `memory_recall` or `session_recall`. Anything else counts as nothing read. */
export function memoryReadCount(tool: Pick<ToolRec, "name" | "state">): number {
  if (!MEMORY_READ_TOOLS.includes(tool.name)) return 0;
  const state = tool.state as { status?: unknown; output?: unknown } | null;
  if (state === null || typeof state !== "object" || state.status !== "completed") return 0;
  const output = state.output as { items?: unknown } | null | undefined;
  const items = output !== null && typeof output === "object" ? output.items : undefined;
  return typeof items === "number" && Number.isInteger(items) && items > 0 ? items : 0;
}

// --- the learner view's reply (G90 contract additions) -------------------------------------------------------------

export interface KnowsNote {
  note_id: string;
  /** Rendered as text, never as markup (MEM-27). */
  text: string;
  status: "active" | "pending_review";
  /** The Reflector, the student's own request (MEM-11), or a note the student or adult corrected (theirs, MEM-09). */
  source: "reflector" | "student" | "corrected";
  /** Fails the core's render check (MEM-27): never in a prompt, shown so it can be removed. */
  flagged: boolean;
  subject: string | null;
  created_ts: number;
  updated_ts: number;
  /** Derived by the core's notes projection (SPEC §19.3.5, §19.5.6), never by a model: `supported` once the note's cited
   * evidence spans two sessions. Read through `noteLifecycle` (an older core sends none of these). */
  confidence?: "tentative" | "supported";
  /** No new evidence and no update for 90 days. */
  fading?: boolean;
  /** How many sessions the tutor recalled the note in (device-local, JM-D27). */
  recall_sessions?: number;
  /** The scrub kept the note out of the online tutor's records (§19.6): it is used on this computer only. */
  withheld_online?: boolean;
  explanation?: api_commands.MemoryExplanation;
}

/** A note's lifecycle as the knows page shows it (SPEC §19.5.6), with the defaults for a field the core did not send. */
export interface NoteLifecycle {
  confidence: "tentative" | "supported";
  fading: boolean;
  recall_sessions: number;
  withheld_online: boolean;
}

export function noteLifecycle(note: KnowsNote): NoteLifecycle {
  return {
    confidence: note.confidence ?? "tentative", fading: note.fading ?? false, recall_sessions: note.recall_sessions ?? 0, withheld_online: note.withheld_online ?? false,
  };
}

export type Band = "not_started" | "learning" | "secure";

export interface KnowsKc {
  kc_id: string;
  statement: string;
  state: "not_started" | "learning" | "ready_to_check" | "mastered" | "needs_review" | "moved_on";
  band: Band;
}

export interface KnowsMisconception {
  misconception_id: string;
  name: string;
  status: "active" | "resolving";
}

/** The values each preference can take (the core refuses anything else with 422). */
export const PREFERENCE_VALUES = {
  start_style: ["worked_example", "hint"],
  representation: ["diagram", "simpler_numbers", "analogy", "worked_example"],
  pace: ["short", "normal"],
  unprompted_help: [true, false],
  self_explanation_prompts: [true, false],
} as const satisfies Record<string, readonly (string | boolean)[]>;
export type PreferenceKey = keyof typeof PREFERENCE_VALUES;
export const PREFERENCE_KEYS = Object.keys(PREFERENCE_VALUES) as PreferenceKey[];

export interface KnowsPreference {
  key: PreferenceKey;
  value: string | boolean;
  source: "override" | "inferred";
}

/** A change the tutor may not make on its own (MEM-09): `replace` (a new note that would take an older one's place;
 * `target` is null when the older one is gone) or `drop` (removing a note that is the student's). The answer quotes
 * both versions and is refused if either has moved on. */
export interface KnowsReview {
  review_id: string;
  kind: "replace" | "drop";
  pending: KnowsNote | null;
  target: KnowsNote | null;
  pending_version: string;
  target_version: string | null;
}

/** What the profile shares beyond this computer (LRN-36): each flag is true only while the consent is in effect. */
export interface Sharing {
  backup: boolean;
  devices: boolean;
  parent_view: boolean;
  cloud_tutor: boolean;
  /** Personal tutor memory online is in effect (SPEC §19.6); absent from an older core. */
  tutor_memory?: boolean;
}

export interface About {
  level: "O" | "A";
  subjects: string[];
  exam_date: string | null;
}

export const RECORD_KINDS = ["mastery", "reviews", "mistakes", "practice", "learning_days", "activity", "subject_progress", "preferences", "notes", "plans"] as const;
export interface KnowsRecord { kind: (typeof RECORD_KINDS)[number]; count: number }
export const PLAN_REASONS = ["revisit_moved_on", "prerequisite", "weak_high_value", "mock", "mock_follow_up"] as const;
export type KnowsPlanReason = (typeof PLAN_REASONS)[number];

/** One thing the learner did in Study (SPEC §19.3.2), as evidence the tutor saw: never mastery (JM-D26). `statement`
 * is the pack's words for the topic the activity's words named, when one matched; `result` is the outcome or rating. */
export interface KnowsActivity {
  activity: "quiz_item" | "card_review" | "aid_made";
  kc_id: string | null;
  statement: string | null;
  result: string | null;
  aid_kind: string | null;
  count: number | null;
  day: string;
  /** The material came from a study pack the research agent made (absent from an older core). */
  from_pack?: boolean;
}
const ACTIVITY_KINDS = ["quiz_item", "card_review", "aid_made"] as const;

/** The companion thread's rolling summary (SPEC §19.3.3, §19.5.3): written by code only, read on this computer. */
export type KnowsCompanionSummary = api_commands.CompanionSummary;

export interface KnowsReply {
  teaching_observations?: api_commands.TeachingObservation[];
  activity?: KnowsActivity[];
  /** The companion thread's rolling summary, when the thread has one (SPEC §19.3.3). */
  companion_summary?: KnowsCompanionSummary | null;
  records?: KnowsRecord[];
  plan_reason?: KnowsPlanReason | null;
  profile_id: string;
  about: About | null;
  kcs: KnowsKc[];
  misconceptions: KnowsMisconception[];
  preferences: KnowsPreference[];
  notes: KnowsNote[];
  reviews: KnowsReview[];
  memory_saving: boolean;
  sharing: Sharing | null;
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === "string";
const oneOf = (v: unknown, values: readonly unknown[]) => values.includes(v);
const isNote = (v: unknown): v is KnowsNote =>
  isObject(v) && isString(v.note_id) && /^mem_[0-9A-HJKMNP-TV-Z]{26}$/.test(v.note_id) && isString(v.text) && oneOf(v.status, ["active", "pending_review"]) &&
  oneOf(v.source, ["reflector", "student", "corrected"]) && typeof v.flagged === "boolean" && (v.subject === null || isString(v.subject)) &&
  Number.isInteger(v.created_ts) && Number.isInteger(v.updated_ts) &&
  (v.confidence === undefined || oneOf(v.confidence, ["tentative", "supported"])) && (v.fading === undefined || typeof v.fading === "boolean") &&
  (v.recall_sessions === undefined || (Number.isSafeInteger(v.recall_sessions) && (v.recall_sessions as number) >= 0)) &&
  (v.withheld_online === undefined || typeof v.withheld_online === "boolean");
const isVersion = (v: unknown) => isString(v) && /^[0-9A-HJKMNP-TV-Z]{26}$/.test(v);
const isReview = (r: unknown): r is KnowsReview =>
  isObject(r) && isString(r.review_id) && /^rvw_[0-9A-HJKMNP-TV-Z]{26}$/.test(r.review_id) && oneOf(r.kind, ["replace", "drop"]) &&
  (r.pending === null || isNote(r.pending)) && (r.target === null || isNote(r.target)) && isVersion(r.pending_version) && (r.target_version === null || isVersion(r.target_version));
const isPreference = (p: unknown): p is KnowsPreference =>
  isObject(p) && isString(p.key) && p.key in PREFERENCE_VALUES && (PREFERENCE_VALUES[p.key as PreferenceKey] as readonly unknown[]).includes(p.value) && oneOf(p.source, ["override", "inferred"]);

/** The problems of a learner-view reply, by field (empty when it is well formed): the boundary check while the command
 * contract has no generated validator for it (G90). A malformed reply never reaches the page. */
export function knowsReplyProblems(value: unknown): string[] {
  if (!isObject(value)) return ["reply"];
  const problems: string[] = [];
  const check = (ok: boolean, field: string) => ok || problems.push(field);
  const list = (field: string, item: (v: unknown) => boolean) => check(Array.isArray(value[field]) && (value[field] as unknown[]).every(item), field);
  check(isString(value.profile_id) && /^prf_[0-9A-HJKMNP-TV-Z]{26}$/.test(value.profile_id), "profile_id");
  check(!("viewer" in value), "viewer");
  const about = value.about;
  check(about === null || (isObject(about) && oneOf(about.level, ["O", "A"]) && Array.isArray(about.subjects) && about.subjects.every(isString) && (about.exam_date === null || isString(about.exam_date))), "about");
  list("kcs", (k) => isObject(k) && isString(k.kc_id) && isString(k.statement) && isString(k.state) && oneOf(k.band, ["not_started", "learning", "secure"]));
  list("misconceptions", (m) => isObject(m) && isString(m.misconception_id) && isString(m.name) && oneOf(m.status, ["active", "resolving"]));
  list("preferences", isPreference);
  list("notes", isNote);
  check(Array.isArray(value.notes) && value.notes.every((n) => isObject(n) && (n.explanation === undefined
    || errors("api/commands#/$defs/MemoryExplanation", n.explanation).length === 0)), "note_explanations");
  if (value.teaching_observations !== undefined) {
    list("teaching_observations", (o) => errors("api/commands#/$defs/TeachingObservation", o).length === 0);
    check(Array.isArray(value.teaching_observations) && value.teaching_observations.length <= 12, "teaching_observations");
  }
  list("reviews", isReview);
  if (value.records !== undefined) {
    list("records", (r) => isObject(r) && oneOf(r.kind, RECORD_KINDS) && Number.isSafeInteger(r.count) && (r.count as number) > 0);
    if (Array.isArray(value.records)) check(value.records.length <= 10 && new Set(value.records.map((r) => isObject(r) ? r.kind : null)).size === value.records.length, "records");
  }
  check(value.plan_reason === undefined || value.plan_reason === null || oneOf(value.plan_reason, PLAN_REASONS), "plan_reason");
  const orNull = (v: unknown, ok: (x: unknown) => boolean) => v === null || ok(v);
  if (value.activity !== undefined) {
    list("activity", (a) => isObject(a) && oneOf(a.activity, ACTIVITY_KINDS) && orNull(a.kc_id, isString) && orNull(a.statement, isString) && orNull(a.result, isString)
      && orNull(a.aid_kind, isString) && orNull(a.count, Number.isSafeInteger) && isString(a.day) && /^\d{4}-\d{2}-\d{2}$/.test(a.day));
  }
  check(value.companion_summary === undefined || value.companion_summary === null
    || errors("api/commands#/$defs/CompanionSummary", value.companion_summary).length === 0, "companion_summary");
  check(typeof value.memory_saving === "boolean", "memory_saving");
  const sharing = value.sharing;
  check(sharing === null || (isObject(sharing) && ["backup", "devices", "parent_view", "cloud_tutor"].every((f) => typeof sharing[f] === "boolean")), "sharing");
  return problems;
}

/** A well-formed learner-view reply, or a thrown `CommandError` (`invalid_reply`) naming the bad fields. */
export function readKnows(value: unknown): KnowsReply {
  const problems = knowsReplyProblems(value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as KnowsReply;
}

// --- the page's view -------------------------------------------------------------------------------------------------

export interface KnowsView {
  teaching_observations?: api_commands.TeachingObservation[];
  /** What the learner did in Study, newest first (SPEC §19.3.2). */
  activity?: KnowsActivity[];
  /** The companion thread's rolling summary (SPEC §19.3.3), or null when there is none yet. */
  companion_summary?: KnowsCompanionSummary | null;
  records?: KnowsRecord[];
  plan_reason?: KnowsPlanReason | null;
  status: "idle" | "loading" | "ready" | "failed";
  /** Newest first. */
  notes: KnowsNote[];
  about: About | null;
  /** Null when the core could not read the consents: the page then makes no claim about sharing. */
  sharing: Sharing | null;
  memory_saving: boolean | null;
  kcs: KnowsKc[];
  misconceptions: KnowsMisconception[];
  preferences: KnowsPreference[];
  /** Oldest first, as the core lists them. */
  reviews: KnowsReview[];
  /** Set right after a forget succeeds, for the "forgotten" line. */
  forgotten: string | null;
  /** The error code of the last correction that failed; whatever it changed on the page is back. */
  error: string | null;
  /** What the failed correction was, for its line. */
  errorOn: Correction | null;
  /** A review item that changed while it was shown: it is shown again as it is now, with a line saying so (MEM-09). */
  changed: string | null;
}

export const EMPTY_KNOWS: KnowsView = {
  status: "idle", notes: [], about: null, sharing: null, memory_saving: null, kcs: [], misconceptions: [], preferences: [], reviews: [],
  forgotten: null, error: null, errorOn: null, changed: null,
};

const newestFirst = (a: KnowsNote, b: KnowsNote) => b.created_ts - a.created_ts || (a.note_id < b.note_id ? 1 : -1);

/** The page's view from a learner-view reply (the gallery builds it the same way). */
export function knowsViewOf(reply: KnowsReply): KnowsView {
  return {
    status: "ready", notes: [...reply.notes].sort(newestFirst), about: reply.about === null ? null : { ...reply.about, subjects: [...reply.about.subjects] },
    sharing: reply.sharing, memory_saving: reply.memory_saving, kcs: [...reply.kcs], misconceptions: [...reply.misconceptions],
    records: [...(reply.records ?? [])], plan_reason: reply.plan_reason ?? null, activity: [...(reply.activity ?? [])],
    ...(reply.companion_summary !== undefined ? { companion_summary: reply.companion_summary } : {}),
    ...(reply.teaching_observations !== undefined ? { teaching_observations: reply.teaching_observations } : {}),
    preferences: [...reply.preferences], reviews: [...reply.reviews], forgotten: null, error: null, errorOn: null, changed: null,
  };
}

/** Forget one note (MEM-14, MEM-11 Undo, LRN-37): the core deletes it and erases its text. A note the core no longer
 * has (404) is already forgotten. */
export async function forgetNote(commands: CommandClient, noteId: string): Promise<void> {
  try {
    await commands.delete<unknown>(`/v1/memory/${encodeURIComponent(noteId)}`, clientId());
  } catch (error) {
    if (error instanceof CommandError && error.code === "not_found") return;
    throw error;
  }
}

/** "Forget this", or "This isn't right" (recorded as a dispute of the note, then the same deletion). */
export type ForgetReason = "forget" | "not_right";

/** The page's corrections, to say which one failed. */
export type Correction = "note" | "review" | "misconception" | "preference";

/** The "What your tutor knows" page's data for the unlocked profile. */
export class KnowsController {
  private view: KnowsView = EMPTY_KNOWS;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_KNOWS));
  }

  getView = (): KnowsView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: KnowsView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private path(profileId: string, rest: string): string {
    return `/v1/profiles/${encodeURIComponent(profileId)}${rest}`;
  }

  /** Read the learner view. A reply that lands after a lock or switch is dropped (SEC-19). */
  async load(): Promise<void> {
    const { profileId, generation } = this.profiles.scope();
    this.set({ ...this.view, status: this.view.status === "ready" ? "ready" : "loading", forgotten: null, error: null, errorOn: null, changed: null });
    try {
      const reply = readKnows(await this.profiles.commands.get<unknown>(this.path(profileId, "/knows")));
      if (generation !== this.profiles.generation) return;
      this.set(knowsViewOf(reply));
    } catch {
      if (generation === this.profiles.generation) this.set({ ...this.view, status: "failed" });
    }
  }

  /** Send one correction; on success the page takes the view the core answers with (plus `extra`), and on a refusal
   * the page goes back to what it showed before, with the error code. Returns the error, or null. */
  private async correct(before: KnowsView, on: Correction, send: () => Promise<unknown>, extra: Partial<KnowsView> = {}): Promise<CommandError | null> {
    const { generation } = this.profiles.scope();
    try {
      const reply = readKnows(await send());
      if (generation === this.profiles.generation) this.set({ ...knowsViewOf(reply), ...extra });
      return null;
    } catch (error) {
      const failed = error instanceof CommandError ? error : new CommandError(0, "core_starting", true, undefined);
      if (generation === this.profiles.generation) this.set({ ...before, forgotten: null, changed: null, error: failed.code, errorOn: on });
      return failed;
    }
  }

  /** "Forget this" and "This isn't right" on a note: it leaves the page at once; if the core refuses, it comes back with
   * the error. The conversation's announcement of the same note, if any, turns to "forgotten" too. */
  async forget(noteId: string, reason: ForgetReason = "forget"): Promise<boolean> {
    const { profileId, generation } = this.profiles.scope();
    const before = { ...this.view, forgotten: null, error: null, errorOn: null, changed: null };
    if (!before.notes.some((n) => n.note_id === noteId)) return false;
    this.set({ ...before, notes: before.notes.filter((n) => n.note_id !== noteId) });
    let failed: CommandError | null;
    if (reason === "not_right") {
      failed = await this.correct(before, "note", () => this.profiles.commands.post<unknown>(this.path(profileId, `/memory/${encodeURIComponent(noteId)}/dispute`), undefined, clientId()), { forgotten: noteId });
      if (failed?.code === "not_found" && generation === this.profiles.generation) {
        failed = null; // already gone at the core
        this.set({ ...before, notes: before.notes.filter((n) => n.note_id !== noteId), forgotten: noteId });
      }
    } else {
      failed = null;
      try {
        await forgetNote(this.profiles.commands, noteId);
        if (generation === this.profiles.generation) this.set({ ...this.view, forgotten: noteId });
      } catch (error) {
        failed = error instanceof CommandError ? error : new CommandError(0, "core_starting", true, undefined);
        if (generation === this.profiles.generation) this.set({ ...before, error: failed.code, errorOn: "note" });
      }
    }
    if (failed !== null || generation !== this.profiles.generation) return false;
    this.profiles.holder.update((s) => markForgotten(s, noteId));
    return true;
  }

  /** Answer a review item for exactly the versions shown (MEM-09). If it moved on meanwhile the core refuses, and the
   * item is shown again as it is now; if it is gone, it leaves the page. */
  async resolveReview(reviewId: string, accept: boolean): Promise<boolean> {
    const { profileId, generation } = this.profiles.scope();
    const before = { ...this.view, forgotten: null, error: null, errorOn: null, changed: null };
    const item = before.reviews.find((r) => r.review_id === reviewId);
    if (item === undefined) return false;
    this.set({ ...before, reviews: before.reviews.filter((r) => r.review_id !== reviewId) });
    const body = { accept, pending_version: item.pending_version, target_version: item.target_version };
    const failed = await this.correct(before, "review", () => this.profiles.commands.post<unknown>(this.path(profileId, `/memory/reviews/${encodeURIComponent(reviewId)}`), body, clientId()));
    if (failed === null || generation !== this.profiles.generation) return failed === null;
    const current = failed.details?.review;
    if (failed.code === "review_changed" && isReview(current) && current.review_id === reviewId) {
      this.set({ ...before, reviews: before.reviews.map((r) => (r.review_id === reviewId ? current : r)), changed: reviewId });
    } else if (failed.code === "not_found") {
      this.set({ ...before, reviews: before.reviews.filter((r) => r.review_id !== reviewId) });
      void this.load();
    }
    return false;
  }

  /** "This isn't right" on a mistake being watched (LRN-37): it leaves the list until new evidence brings it back. */
  async disputeMisconception(misconceptionId: string): Promise<boolean> {
    const { profileId } = this.profiles.scope();
    const before = { ...this.view, forgotten: null, error: null, errorOn: null, changed: null };
    this.set({ ...before, misconceptions: before.misconceptions.filter((m) => m.misconception_id !== misconceptionId) });
    const path = this.path(profileId, `/misconceptions/${encodeURIComponent(misconceptionId)}/correction`);
    return (await this.correct(before, "misconception", () => this.profiles.commands.post<unknown>(path, { action: "dispute" }, clientId()))) === null;
  }

  /** Set how the student likes to learn, or with null let the tutor decide again (LRN-37). */
  async setPreference(key: PreferenceKey, value: string | boolean | null): Promise<boolean> {
    const { profileId } = this.profiles.scope();
    const before = { ...this.view, forgotten: null, error: null, errorOn: null, changed: null };
    const path = this.path(profileId, `/preferences/${encodeURIComponent(key)}`);
    return (await this.correct(before, "preference", () => this.profiles.commands.patch<unknown>(path, { value }, clientId()))) === null;
  }
}
