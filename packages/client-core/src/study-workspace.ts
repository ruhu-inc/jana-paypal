/** M42: profile-bound source library and study aids, using the local API rather than a browser provider. */
import { isValid, type study_research as R, type study_workspace as W } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandClient, CommandError } from "./stream/commands.js";
import type { LocalSpeech } from "./speech.js";
import { readSessionNote } from "./session-notes.js";
import { agreesForItself, readConsents, readProfile } from "./studies.js";

export type { W as StudyContracts, R as ResearchContracts };
/** SPEC §20.4 (W9, JM-D31): where a moment of a YouTube video opens. The app's external-link opener, the one the
 * research pack's and Ask anything's sources use (an anchor with target _blank); a test or the app may supply its own. */
/** The line every quiz-help prompt carries before the question it is about (`requestQuizHelp`): the only way a chat
 * aid made for a quiz question can be told from a question the learner typed. */
export const QUIZ_CONTEXT_MARK = "Quiz context (not a request to answer it):";
/** A chat aid's prompt was written by `requestQuizHelp`, not by the learner: coaching for one quiz question, which
 * the chat's history leaves out and the quiz shows beside that question. */
/** The copy key naming a model the gateway or the device reported (`Aid.made_by.model`); null shows the id as sent. */
export function modelNameKey(id: string | undefined): "model.muse_spark" | "model.muse_glimmer" | "model.gemini_3_8_flash" | null {
  const lower = (id ?? "").toLowerCase();
  if (lower === "gemini-3.8-flash") return "model.gemini_3_8_flash";
  if (/^muse-spark(?:[.-]|$)/.test(lower)) return "model.muse_spark";
  if (/^muse-glimmer(?:[.-]|$)/.test(lower)) return "model.muse_glimmer";
  return null;
}

export function isQuizHelpPrompt(prompt: string | undefined): boolean { return prompt !== undefined && prompt.includes(QUIZ_CONTEXT_MARK); }
/** The quiz item a help aid was about: the one whose stem the prompt quotes after the mark; null when none matches
 * (another quiz, or a reworded question). */
export function quizHelpItem(prompt: string | undefined, quiz: Pick<W.QuizView, "items"> | null): W.QuizItem | null {
  if (!isQuizHelpPrompt(prompt) || quiz === null || prompt === undefined) return null;
  const context = prompt.slice(prompt.indexOf(QUIZ_CONTEXT_MARK) + QUIZ_CONTEXT_MARK.length).trim();
  return quiz.items.find((item) => item.stem.text !== "" && context.startsWith(item.stem.text.slice(0, 400))) ?? null;
}
export interface ExternalOpener { open(url: string): void; }
export const BROWSER_OPENER: ExternalOpener = { open: (url) => { if (typeof window !== "undefined") window.open(url, "_blank", "noopener,noreferrer"); } };
/** A video file's suffixes: kind `video` by suffix at create (SPEC §20.4); every other media suffix is a recording. */
export const VIDEO_SUFFIXES = ["mp4", "webm", "mov", "mkv", "avi"] as const;
const AUDIO_SUFFIXES = ["wav", "mp3", "m4a", "ogg", "flac", "aac"] as const;
/** A moment's label as YouTube's panel writes it: `m:ss` under an hour, `h:mm:ss` from one hour. */
export function momentLabel(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000)), hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60), rest = seconds % 60;
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${minutes}:${String(rest).padStart(2, "0")}`;
}
const LINE_MOMENT = /^\[(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]\s?/;
/** The moment a transcript line carries as its `[m:ss]` prefix (SPEC §20.3), or null. */
export function lineMoment(line: string): number | null {
  const found = LINE_MOMENT.exec(line);
  return found ? ((Number(found[1] ?? 0) * 3600 + Number(found[2]) * 60 + Number(found[3])) * 1000) : null;
}
/** A transcript line without its moment prefix. */
export function lineText(line: string): string { return line.replace(LINE_MOMENT, ""); }
/** A timed chunk as its lines, each with its own moment (a recording's segment is one line at the chunk's start). */
export function chunkLines(chunk: W.Chunk): { ms: number; text: string }[] {
  if (chunk.locator.kind !== "time") return [];
  const lines = chunk.text.split("\n").filter((line) => line.trim() !== "");
  if (!lines.some((line) => lineMoment(line) !== null)) return [{ ms: chunk.locator.start_ms, text: chunk.text }];
  let last = chunk.locator.start_ms;
  return lines.map((line) => { const ms = lineMoment(line); if (ms !== null) last = ms; return { ms: last, text: lineText(line) }; });
}
/** The moment a citation into a chunk names: its line's own moment, else the chunk's start (a frame's `at_ms`). */
export function citationMoment(chunk: W.Chunk, citation: Pick<W.Citation, "start">): number | null {
  if (chunk.locator.kind === "frame") return chunk.locator.at_ms;
  if (chunk.locator.kind !== "time") return null;
  const before = Array.from(chunk.text).slice(0, citation.start).join("");
  const lineStart = before.lastIndexOf("\n") + 1;
  const line = chunk.text.slice(lineStart).split("\n")[0] ?? "";
  return lineMoment(line) ?? chunk.locator.start_ms;
}
/** youtube.com/watch?v=<id>&t=<seconds>: the link a moment opens in the learner's browser (SPEC §20.4). */
export function momentUrl(link: NonNullable<W.Source["link"]>, ms = 0): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `https://www.youtube.com/watch?v=${link.video_id}${seconds > 0 ? `&t=${seconds}` : ""}`;
}
export interface PodcastAudio {
  available(): boolean;
  activate(): void;
  play(audio: Blob, rate: number, callbacks: { end(): void; error(): void }): () => void;
  pause?(): void;
  resume?(): void;
}
export interface StudyWorkspaceView {
  archivedSourceIds?: string[];
  journey?: W.StudyJourney | null;
  podcastVoice?: { enabled: boolean; configured: boolean } | null;
  access?: { status: "ready" | "consent_required" | "study_off" | "plan_required"; needsGuardian: boolean } | null;
  status: "idle" | "loading" | "ready" | "failed";
  sources: W.SourceList["items"]; jobs: W.Job[]; source: W.Source | null; chunks: W.Chunk[]; aids: W.Aid[];
  aid: W.Aid | null; deck: W.Deck | null; quiz: W.QuizView | null; result: W.QuizResult | null;
  resultAnswers?: Record<string, string>;
  cleanupPending: boolean; availableRevisions: number[]; busy: boolean; error: string | null; uploadBytes: number; uploadTotal: number;
  selections: W.SourceSelection[]; notes: { id: string; kind: "session_note" | "dictated_note" | "board_note"; title: string | null; private: boolean }[];
  reading: boolean; spokenClaim: number; canSpeak: boolean; audioPreparing?: boolean;
  audioPaused?: boolean; audioCompleted?: boolean;
  quizFeedback?: Record<string, W.QuizResult["results"][number]>;
  /** The answer each verdict in `quizFeedback` marked, so a verdict comes back with the quiz after leaving its tab. */
  quizChecked?: Record<string, string>;
  requestedJobId?: string | null;
  /** Contextual coaching stays beside its quiz; never replaces its draft, feedback or result. */
  quizHelp?: { quizId: string; itemId: string; kind: "hint" | "walk_through"; jobId: string; aid: W.Aid | null } | null;
  followCurrent: boolean;
  topics: W.TopicLinks["items"];
  safetyTemplate?: string | null;
  /** A typed topic's concept map (no document): the pack's curated skills, or AI-generated with no source. */
  conceptTopic?: W.ConceptTopicMap | null;
  conceptTopics?: W.ConceptTopicMapList["maps"];
  /** STU-21 My Sets, newest first, with live progress; a private source's sets carry no title until opened. */
  sets: W.StudySet[];
  /** STU-21: the open material's own topics, for the Generate settings. */
  materialTopics: W.MaterialTopics["items"];
  /** STU-22: a request to move the player to a moment of the open recording (a new `seq` each time). */
  playhead: { ms: number; seq: number } | null;
}
export const EMPTY_STUDY: StudyWorkspaceView = { status: "idle", sources: [], jobs: [], source: null, chunks: [], aids: [], aid: null, deck: null, quiz: null, result: null, cleanupPending: false, availableRevisions: [], busy: false, error: null, uploadBytes: 0, uploadTotal: 0, selections: [], notes: [], reading: false, spokenClaim: 0, canSpeak: false, followCurrent: true, topics: [], sets: [], materialTopics: [], playhead: null };
/** The Generate panel's tools (STU-21), in the order the panel shows them. */
export const GENERATE_TOOLS = ["summary", "quiz", "flashcards", "notes", "audio_overview", "lesson_plan", "concept_map"] as const;
export type GenerateTool = (typeof GENERATE_TOOLS)[number];
/** Kinds that draw on every selected source rather than the open one alone. */
const MULTI_SOURCE: readonly string[] = ["notes", "summary", "chat", "lesson_plan"];
function research<T>(name: string, value: unknown): T {
  if (!isValid(`study/research#/$defs/${name}`, value)) throw new CommandError(200, "invalid_reply", false, undefined);
  return value as T;
}
function valid<T>(name: string, value: unknown): T {
  if (!isValid(`study/workspace#/$defs/${name}`, value)) throw new CommandError(200, "invalid_reply", false, undefined);
  return value as T;
}
async function sha(data: Uint8Array): Promise<string> {
  const result = await crypto.subtle.digest("SHA-256", new Uint8Array(data).buffer);
  return Array.from(new Uint8Array(result), (b) => b.toString(16).padStart(2, "0")).join("");
}
function base64(data: Uint8Array): string {
  let result = "";
  for (let i = 0; i < data.length; i += 8192) result += String.fromCharCode(...data.subarray(i, i + 8192));
  return btoa(result);
}
/** Plain text a course hands out, read as text like .txt: notes, LaTeX, data and code (`TEXT_SUFFIXES` in the core). */
const TEXT_SUFFIXES = ["txt", "md", "tex", "bib", "rst", "csv", "tsv", "py", "r", "m", "jl", "java", "c", "h", "cpp", "cs", "js", "ts", "go", "rs", "sql", "ipynb", "json", "yaml", "yml", "html"];

export class StudyWorkspaceController {
  private view = EMPTY_STUDY;
  private listeners = new Set<() => void>();
  private sequence = 0;
  private grant: string | null = null;
  private referenceGrants: { sid: string; token: string; expiry: number }[] = [];
  private grantExpires = 0;
  private selection = 0;
  /** The most recent requested tool: open its complete result once, including its deck or quiz. */
  private pendingAid: string | null = null;
  private speechGeneration = 0;
  private cancelSpeech: (() => void) | null = null;
  private finishSpeech: ((ok: boolean) => void) | null = null;
  private speechExpiry: ReturnType<typeof setTimeout> | null = null;
  private resumeSpeech: (() => void) | null = null;
  private cardReturns = new Map<string, number>();
  private cardReturnTimer: ReturnType<typeof setTimeout> | null = null;
  constructor(private readonly profiles: ProfileController, private readonly speech?: LocalSpeech, private readonly podcastAudio?: PodcastAudio, private readonly opener: ExternalOpener = BROWSER_OPENER) {
    profiles.onClear(() => { this.clearCardReturns(); this.pendingAid = null; this.stopReading(); this.sequence++; this.selection++; this.referenceGrants = []; this.grant = null; this.grantExpires = 0; this.set({ ...EMPTY_STUDY, canSpeak: speech?.available() ?? false }); });
    if (speech) { this.patch({ canSpeak: speech.available() }); speech.subscribe(() => this.patch({ canSpeak: this.view.podcastVoice?.enabled ? !!this.view.podcastVoice.configured && !!this.podcastAudio?.available() : speech.available() })); }
  }
  getView = (): StudyWorkspaceView => this.view;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  /** Explain access before a file is processed; the native guard remains authoritative. */
  async loadAccess(): Promise<void> {
    const generation = this.profiles.generation;
    try {
      const id = this.profiles.scope().profileId, commands = this.commands();
      const [profile, consents, connectivity, plan] = await Promise.all([
        readProfile(commands, id), readConsents(commands, id),
        this.profiles.family.connectivity(), this.profiles.family.subscription(),
      ]);
      if (generation !== this.profiles.generation) return;
      const adult = agreesForItself(profile);
      const permission = (scope: string) => consents.some((c) => c.scope === scope && c.effective && !c.paused && (adult || (c.method === "checkout_guardian" && c.granted_by_role === "guardian")));
      const profilePlan = plan.profiles.find((p) => p.profile_id === id);
      const missing = !permission("study_materials") || !permission("cloud_tier") || profilePlan?.consent === "needed";
      const covered = ["active", "pending_consent"].includes(plan.state) && profilePlan !== undefined;
      const status = !covered || plan.allowance_used_up ? "plan_required" : missing ? "consent_required" : !connectivity.features.study_materials?.enabled || !connectivity.features.cloud_tier?.enabled ? "study_off" : "ready";
      this.patch({ access: { status, needsGuardian: !adult && missing }, ...(status === "ready" && this.view.error === "consent_required" ? { error: null } : {}) });
      await this.loadPodcastVoice();
    } catch { /* A failed status read never substitutes for the native permission check. */ }
  }
  async sponsoredPlace(source: string, revision: number, action?: "claim" | "withdraw" | "activate"): Promise<{ state: string; deadline: number | null } | null> {
    const generation = this.profiles.generation;
    const endpoint = await this.profiles.launch.endpoint(false);
    if (!endpoint.sponsoredStudy || generation !== this.profiles.generation) return null;
    const path = `/study-place/${encodeURIComponent(source)}/${revision}`;
    const commands = this.commands();
    if (action) return commands.post(path, { action }, clientId());
    return commands.get(path);
  }
  async loadPodcastVoice(): Promise<void> {
    const generation = this.profiles.generation;
    try {
      const status = await this.commands().get<{ enabled: boolean; configured: boolean; provider: string }>("/v1/study/podcast-voice");
      if (generation !== this.profiles.generation || typeof status.enabled !== "boolean" || typeof status.configured !== "boolean" || typeof status.provider !== "string") return;
      this.patch({ podcastVoice: { enabled: status.enabled, configured: status.configured }, canSpeak: status.enabled ? status.configured && !!this.podcastAudio?.available() : this.speech?.available() ?? false });
    } catch { /* Older cores retain their installed-voice path; configured neural failures never downgrade silently. */ }
  }
  private set(view: StudyWorkspaceView): void { this.view = view; for (const fn of this.listeners) fn(); }
  private patch(view: Partial<StudyWorkspaceView>): void { this.set({ ...this.view, ...view }); }
  /** VOC-53, D130: the open source as the voice conversation's place in the Chat tab, with its open grant; null when
   * nothing is open or a private source's grant has lapsed. */
  voicePlace(): { place: "study"; source_id: string; revision: number; open: string } | null {
    const source = this.view.source;
    if (!source) return null;
    const live = this.grant !== null && this.grantExpires > Date.now();
    if (source.private && !live) return null;
    return { place: "study", source_id: source.source_id, revision: source.revision, open: live ? this.grant ?? "" : "" };
  }
  private headers(): Record<string, string> {
    const tokens = this.referenceGrants.filter((g) => g.expiry > Date.now()).map((g) => g.token);
    if (this.grant && this.grantExpires > Date.now()) tokens.unshift(this.grant);
    return tokens.length ? { "X-Study-Open": tokens.slice(0, 8).join(",") } : {};
  }
  private commands(): CommandClient {
    const generation = this.profiles.generation;
    return this.profiles.commands.scoped(() => generation === this.profiles.generation);
  }
  private async allAids(sourceId: string): Promise<W.Aid[]> {
    const commands = this.commands();
    const items: W.Aid[] = []; let cursor: string | null = null;
    do {
      const page: W.AidList = valid("AidList", await commands.get(`/v1/study/aids?source_id=${sourceId}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, this.headers()));
      items.push(...page.items); cursor = page.next_cursor;
    } while (cursor !== null);
    return items;
  }
  private async mutate<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown, headers = this.headers()): Promise<T> {
    const commands = this.commands();
    const stableBody = body && typeof body === "object" ? { ...body } as Record<string, unknown> : body;
    if (stableBody && typeof stableBody === "object") { delete (stableBody as Record<string, unknown>).client_review_id; delete (stableBody as Record<string, unknown>).attempt_id; }
    const hash = await sha(new TextEncoder().encode(JSON.stringify({ method, path, body: stableBody ?? null })));
    const identity = valid<W.RetryIdentity>("RetryIdentity", await commands.post(`/v1/study/retries/${hash}`, {}, clientId()));
    const finalBody = path.endsWith("/reviews") ? { ...(stableBody as object), client_review_id: identity.client_key.replace("cli_", "srv_") } : path.endsWith("/attempts") ? { ...(stableBody as object), attempt_id: identity.client_key.replace("cli_", "sqa_") } : body;
    const value = await commands.request<T>(method, path, finalBody, identity.client_key, headers);
    await commands.delete(`/v1/study/retries/${hash}`, identity.client_key).catch(() => undefined);
    return value;
  }
  private async action(work: (current: () => boolean) => Promise<void>): Promise<void> {
    if (this.view.busy) return;
    const generation = this.profiles.generation, selection = this.selection;
    const current = () => generation === this.profiles.generation && selection === this.selection;
    this.patch({ busy: true, error: null, safetyTemplate: null });
    try { await work(current); }
    catch (error) {
      if (current()) {
        const template = error instanceof CommandError && error.details?.reason === "safety_template" ? error.details.template_id : null;
        const safetyTemplate = typeof template === "string" && ["tpl.distress", "tpl.off_topic", "tpl.graded_work", "tpl.personal_details", "tpl.dangerous_request", "tpl.abusive_language", "tpl.see_trusted_adult"].includes(template) ? template : null;
        const reason = safetyTemplate === "tpl.graded_work" ? "graded_work" : error instanceof CommandError ? String(error.details?.reason ?? error.code) : "unreachable";
        if (reason === "grant_expired") this.close();
        this.patch({ error: reason, safetyTemplate });
      }
    } finally { if (current()) this.patch({ busy: false }); }
  }
  async load(): Promise<void> {
    const generation = this.profiles.generation, seq = ++this.sequence;
    const commands = this.commands();
    if (this.view.status === "idle") this.patch({ status: "loading" });
    try {
      let snapshot = valid<W.StudySnapshot>("StudySnapshot", await commands.get("/v1/study/workspace", this.headers()));
      const sources = [...snapshot.sources.items];
      while (snapshot.sources.next_cursor !== null) {
        snapshot = valid<W.StudySnapshot>("StudySnapshot", await commands.get(`/v1/study/workspace?cursor=${encodeURIComponent(snapshot.sources.next_cursor)}`, this.headers()));
        if (generation !== this.profiles.generation || seq !== this.sequence) return;
        sources.push(...snapshot.sources.items);
      }
      if (generation !== this.profiles.generation || seq !== this.sequence) return;
      if ((this.view.source?.private && this.grantExpires <= Date.now()) || this.referenceGrants.some((g) => g.expiry <= Date.now())) this.close();
      // My Sets is a view over the aids: if it cannot be read, the library still opens with the sets it had.
      let sets = this.view.sets;
      try { sets = valid<W.StudySetList>("StudySetList", await commands.get("/v1/study/sets", this.headers())).items; } catch { /* kept */ }
      let archivedSourceIds = this.view.archivedSourceIds ?? [];
      try { archivedSourceIds = this.libraryState(await commands.get("/v1/study/library", this.headers())); } catch { /* Older cores keep the complete library visible. */ }
      if (generation !== this.profiles.generation || seq !== this.sequence) return;
      this.patch({ status: "ready", sources, jobs: snapshot.jobs, cleanupPending: snapshot.cleanup_pending ?? false, sets, archivedSourceIds, journey: snapshot.journey ?? null });
    } catch { if (generation === this.profiles.generation && seq === this.sequence) this.patch({ status: "failed" }); }
  }
  private libraryState(value: unknown): string[] {
    const ids = (value as { archived_source_ids?: unknown } | null)?.archived_source_ids;
    if (!Array.isArray(ids) || ids.some((id) => !isValid("study/workspace#/$defs/SourceId", id))) throw new CommandError(200, "invalid_reply", false, undefined);
    return ids as string[];
  }
  /** A profile-local view preference: no source, revision, set, review or job is deleted or modified. */
  /** The learner's own name for a material (SourcePatch), on every copy of it. */
  async renameSources(ids: string[], title: string): Promise<void> {
    await this.action(async () => {
      for (const id of ids) await this.mutate("PATCH", `/v1/study/sources/${id}`, { title });
      await this.load();
    });
  }
  async archiveSources(ids: string[], archived: boolean): Promise<void> {
    const generation = this.profiles.generation;
    await this.action(async (current) => {
      for (const id of ids) {
        if (!current()) return;
        valid("SourceId", id);
        const state = this.libraryState(await this.mutate(archived ? "POST" : "DELETE", `/v1/study/sources/${id}/archive`, {}));
        if (!current()) return;
        this.patch({ archivedSourceIds: state });
      }
    });
    if (generation === this.profiles.generation) await this.load();
  }
  /** STU-21: open a set where it was left: its material, then the aid (a quiz restores its draft answers). */
  async openSet(set: W.StudySet): Promise<void> {
    const first = set.sources[0];
    if (!first) return;
    const listed = this.view.sources.find((s) => s.source_id === first.source_id);
    await this.openAt(first.source_id, listed && listed.revision !== first.revision ? first.revision : undefined, set.aid_id);
  }
  /** SPEC §19.3.3: a companion button (`open_study_job`, `open_study_deck`) or the plan's due Study cards opens the
   * material a job, aid or deck belongs to, at that aid. A job still being made opens its material, and its result as
   * soon as it is ready. False when there is nothing (any more) to open. */
  async openTarget(target: { job_id?: string; aid_id?: string; deck_id?: string }): Promise<boolean> {
    const generation = this.profiles.generation;
    try {
      let aidId = target.aid_id ?? null;
      let source: W.SourceSelection | null = null;
      let job: W.Job | undefined;
      if (aidId === null && target.deck_id !== undefined) {
        const deck = valid<W.Deck>("Deck", await this.commands().get(`/v1/study/decks/${encodeURIComponent(target.deck_id)}`, this.headers()));
        aidId = deck.aid_id; source = { source_id: deck.source_id, revision: deck.revision };
      }
      if (aidId === null && target.job_id !== undefined) {
        if (!this.view.jobs.some((j) => j.job_id === target.job_id)) await this.load();
        job = this.view.jobs.find((j) => j.job_id === target.job_id);
        aidId = job?.result_ids[0] ?? null;
        if (aidId === null) source = job?.sources[0] ?? null;
      }
      if (aidId !== null && source === null) {
        const aid = valid<W.Aid>("Aid", await this.commands().get(`/v1/study/aids/${encodeURIComponent(aidId)}`, this.headers()));
        source = aid.sources[0] ?? null;
      }
      if (generation !== this.profiles.generation || source === null) return false;
      const listed = this.view.sources.find((s) => s.source_id === source.source_id);
      const revision = listed && listed.revision !== source.revision ? source.revision : undefined;
      if (aidId !== null) await this.openAt(source.source_id, revision, aidId); else await this.open(source.source_id, revision);
      if (generation !== this.profiles.generation || this.view.source?.source_id !== source.source_id) return false;
      if (aidId === null && job !== undefined) { this.pendingAid = job.job_id; this.patch({ requestedJobId: job.job_id }); }
      return true;
    } catch { return false; }
  }
  /** STU-21: the open material's own topics, all kept by default in the Generate settings. */
  async loadMaterialTopics(): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    const generation = this.profiles.generation, selection = this.selection;
    try {
      const result = valid<W.MaterialTopics>("MaterialTopics", await this.commands().get(`/v1/study/sources/${source.source_id}/material-topics?revision=${source.revision}`, this.headers()));
      if (generation === this.profiles.generation && selection === this.selection) this.patch({ materialTopics: result.items });
    } catch { /* Topics are optional: the settings simply show none. */ }
  }
  /** STU-21: keep a quiz's unsubmitted answers and place, so the set opens where it was left. */
  async saveDraft(answers: Record<string, string>, position: number): Promise<void> {
    const quiz = this.view.quiz;
    if (!quiz || quiz.status !== "ready" || this.view.result) return;
    const draft: W.QuizDraft = { quiz_revision: quiz.revision, position: Math.max(0, Math.min(quiz.items.length - 1, position)), answers: Object.entries(answers).filter(([, a]) => a.trim()).map(([item_id, answer]) => ({ item_id, answer })) };
    try {
      const saved = valid<W.QuizDraft>("QuizDraft", await this.mutate("PUT", `/v1/study/quizzes/${quiz.quiz_id}/draft`, draft));
      if (this.view.quiz?.quiz_id === quiz.quiz_id) this.patch({ quiz: { ...this.view.quiz, draft: saved },
        ...(this.view.journey?.quiz_aid_id === quiz.aid_id ? { journey: { ...this.view.journey, phase: "quiz", next_aid_id: quiz.aid_id, retry_quiz: false, questions_answered: saved.answers.length, questions_correct: 0, questions_to_review: 0 } } : {}) });
    } catch { /* A draft is a convenience: the answers on screen stay as they are. */ }
  }
  /** STU-22: the open recording's (or video file's, SPEC §20.4) own bytes for the player; they never leave the device. */
  async loadMedia(): Promise<Blob | null> {
    const source = this.view.source;
    if (!source || (source.kind !== "recording" && source.kind !== "video")) return null;
    let result: Blob | null = null;
    await this.action(async (current) => {
      const blob = await this.commands().request<Blob>("GET", `/v1/study/sources/${source.source_id}/media?revision=${source.revision}`, undefined, null, this.headers(), true);
      if (current()) result = blob;
    });
    return result;
  }
  /** STU-22: move the player to a moment of the open recording (a chapter, a transcript line, a citation). */
  seek(ms: number): void { this.patch({ playhead: { ms: Math.max(0, ms), seq: (this.view.playhead?.seq ?? 0) + 1 } }); }
  /** SPEC §20.4 (W9): open a moment of the open material. A YouTube video (no player in this release, JM-D42) opens
   * `youtube.com/watch?v=<id>&t=<s>` in the learner's browser through the app's external-link opener; a file seeks
   * its player. Returns the link opened, or null when the player was moved instead. */
  openMoment(ms: number): string | null {
    const source = this.view.source;
    if (!source) return null;
    if (source.kind === "video_link" && source.link) { const url = momentUrl(source.link, ms); this.opener.open(url); this.seek(ms); return url; }
    this.seek(ms);
    return null;
  }
  /** The Study screen opens (SPEC §19.3.2): the core opens or resumes the day's Study session, which every activity
   * here cites. A core without the route (an older one) records nothing and the screen works as before. */
  async enter(): Promise<void> {
    try { await this.commands().post("/v1/study/session", {}, clientId()); } catch { /* the screen opens regardless */ }
  }

  /** The Study screen closes: its session ends and the tutor reflects on what was done there (§19.4). */
  async leave(): Promise<void> {
    try { await this.commands().delete("/v1/study/session", clientId()); } catch { /* it ends when idle anyway */ }
  }

  close(): void {
    this.clearCardReturns();
    this.pendingAid = null;
    this.stopReading();
    const headers = this.headers(), ids = new Set(this.referenceGrants.map((g) => g.sid));
    if (this.view.source) ids.add(this.view.source.source_id);
    for (const id of ids) void this.commands().delete(`/v1/study/sources/${id}/open`, clientId(), headers).catch(() => undefined);
    this.referenceGrants = []; this.selection++; this.grant = null; this.grantExpires = 0; this.patch({ source: null, chunks: [], aids: [], aid: null, deck: null, quiz: null, result: null, resultAnswers: {}, journey: null, quizFeedback: {}, quizHelp: null, requestedJobId: null, selections: [], topics: [], materialTopics: [], playhead: null, error: null, busy: false }); }
  async open(id: string, revision?: number): Promise<void> {
    this.close();
    await this.action((current) => this.openWork(current, id, revision));
  }
  /** Open a material and show one of its aids as one action: the open's first render may start a read of its own
   * (a PDF's first page), and a separate `showAid` after the open would find the lock taken and do nothing. */
  async openAt(id: string, revision: number | undefined, aidId: string): Promise<void> {
    this.close();
    await this.action(async (current) => {
      await this.openWork(current, id, revision);
      if (!current() || this.view.source?.source_id !== id) return;
      this.stopReading();
      await this.showAidWork(current, aidId);
    });
  }
  private async openWork(current: () => boolean, id: string, revision?: number): Promise<void> {
    const opened = valid<W.OpenedSource>("OpenedSource", await this.commands().post(`/v1/study/sources/${id}/open`, { confirm: true, ...(revision ? { revision } : {}) }, clientId()));
    if (!current()) return;
    this.grant = opened.open_token; this.grantExpires = Date.parse(opened.expires_at);
    const chunks: W.Chunk[] = [];
    let cursor: string | null = null;
    do {
      const result: W.ChunkList = valid("ChunkList", await this.commands().get(`/v1/study/sources/${id}/content?revision=${opened.source.revision}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, this.headers()));
      if (!current()) return;
      chunks.push(...result.items); cursor = result.next_cursor;
    } while (cursor !== null);
    const aids = await this.allAids(id);
    if (current()) this.patch({ source: opened.source, followCurrent: revision === undefined, selections: [{ source_id: id, revision: opened.source.revision }], availableRevisions: opened.available_revisions ?? [opened.source.revision], chunks, aids });
  }
  /** `POST /v1/study/sources` with any `SourceCreate` variant that needs no upload (pasted text, a YouTube link with its
   * transcript, SPEC §20.3): the source is ready at once, the library reloads and the source opens. */
  async addSource(body: W.SourceCreate): Promise<W.Source | null> {
    let source: W.Source | null = null;
    await this.action(async (current) => {
      const result = valid<W.Source>("Source", await this.mutate("POST", "/v1/study/sources", body));
      if (current()) source = result;
    });
    if (source) { await this.load(); await this.open((source as W.Source).source_id); }
    return source;
  }
  async addText(title: string, text: string): Promise<void> { await this.addSource({ kind: "text", title, text }); }
  /** SPEC §20.1: a YouTube link and the transcript pasted from its panel; a title, or "YouTube video" prefilled. */
  async addLink(url: string, transcript: string, title?: string): Promise<W.Source | null> {
    const named = title?.trim();
    return this.addSource({ kind: "video_link", url: url.trim(), transcript, ...(named ? { title: named.slice(0, 120) } : {}) });
  }
  async upload(file: File, existing?: W.Source, preferCloud = false, replacement = false): Promise<void> {
    let source: W.Source | null = null;
    await this.action(async (current) => {
      if (preferCloud) await this.loadAccess();
      if (!current()) return;
      if (file.size === 0 || file.size > 100 * 1024 * 1024) throw new CommandError(422, "source_too_large", false, undefined);
      const suffix = file.name.split(".").at(-1)?.toLowerCase();
      // A video container is kind `video` by suffix at create (SPEC §20.4); the core downgrades it to a recording
      // in the extracted revision when it holds no video stream.
      const kind = suffix === "pdf" ? "pdf" : ["pptx", "odp"].includes(suffix ?? "") ? "slides" : [...TEXT_SUFFIXES, "docx", "odt"].includes(suffix ?? "") ? "document" : ["jpg", "jpeg", "png", "webp"].includes(suffix ?? "") ? "photo" : (VIDEO_SUFFIXES as readonly string[]).includes(suffix ?? "") ? (existing && existing.kind === "recording" ? "recording" : "video") : (AUDIO_SUFFIXES as readonly string[]).includes(suffix ?? "") ? "recording" : null;
      if (!kind) throw new CommandError(422, "unsupported_format", false, undefined);
      this.patch({ uploadBytes: 0, uploadTotal: file.size });
      const data = new Uint8Array(await file.arrayBuffer()), hash = await sha(data);
      if (!current()) return;
      if (existing && !replacement && existing.sha256 !== hash) throw new CommandError(422, "corrupt_source", false, undefined);
      const metadata = { kind, file_name: file.name, mime_type: file.type || "application/octet-stream", byte_length: file.size, sha256: hash };
      const result = replacement && existing ? valid<W.Source>("Source", await this.mutate("POST", `/v1/study/sources/${existing.source_id}/revisions`, { expected_revision: existing.revision, file_name: file.name, mime_type: metadata.mime_type, byte_length: file.size, sha256: hash })) : existing ?? valid<W.Source>("Source", await this.mutate("POST", "/v1/study/sources", { kind, file_name: file.name, mime_type: file.type || "application/octet-stream", byte_length: file.size, sha256: hash }));
      if (!current()) return;
      if (replacement && result.private) {
        const grant = valid<W.OpenedSource>("OpenedSource", await this.commands().post(`/v1/study/sources/${result.source_id}/open`, { confirm: true, revision: result.revision }, clientId()));
        if (!current()) return;
        this.referenceGrants.push({ sid: result.source_id, token: grant.open_token, expiry: Date.parse(grant.expires_at) });
      }
      for (let offset = 0; offset < data.length; offset += 1024 * 1024) {
        const chunk = data.subarray(offset, offset + 1024 * 1024), chunkHash = await sha(chunk);
        if (!current()) return;
        const receipt = valid<W.UploadReceipt>("UploadReceipt", await this.mutate("PUT", `/v1/study/sources/${result.source_id}/chunks/${offset / (1024 * 1024)}`, { offset, sha256: chunkHash, data_base64: base64(chunk) }));
        if (!current()) return;
        this.patch({ uploadBytes: receipt.received_bytes });
      }
      // Keep a queued, complete upload on this computer while permission is reviewed.
      // Returning from settings processes this same source, without uploading it again.
      if (this.view.access && this.view.access.status !== "ready") { source = result; return; }
      const job = valid<W.Job>("Job", await this.mutate("POST", "/v1/study/jobs", { kind: "extract", execution: preferCloud ? "prefer_cloud" : "local", sources: [{ source_id: result.source_id, revision: result.revision }] }));
      if (current()) { source = result; this.patch({ jobs: [job, ...this.view.jobs] }); }
    });
    if (source) { await this.load(); await this.open((source as W.Source).source_id); }
  }
  /** Start a generation job; `settings` are the Generate panel's choices for that tool (STU-21). */
  async generate(kind: W.AidKind, count = 10, confirmReadableOnly = false, preferCloud = false, prompt?: string, settings?: W.GenerateSettings, contextAidId?: string, learningIntent?: "lesson"): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    const context = contextAidId ? this.view.aid : null;
    if (contextAidId && (context?.aid_id !== contextAidId || (context.kind !== "chat" && !(context.kind === "flashcards" && kind === "quiz")) || context.status !== "ready")) return;
    await this.action(async (current) => {
      const body = { kind, execution: preferCloud ? "prefer_cloud" : "local", ...(confirmReadableOnly ? { confirm_readable_only: true } : {}), sources: context?.sources ?? (MULTI_SOURCE.includes(kind) ? this.view.selections : [{ source_id: source.source_id, revision: source.revision }]), ...(["flashcards", "quiz"].includes(kind) ? { count } : {}), ...(kind === "chat" ? { prompt: prompt?.trim() } : {}), ...(settings && Object.keys(settings).length > 0 ? { settings } : {}), ...(contextAidId ? { context_aid_id: contextAidId } : {}), ...(learningIntent ? { learning_intent: learningIntent } : {}) };
      const job = valid<W.Job>("Job", await this.mutate("POST", "/v1/study/jobs", body));
      if (current()) {
        this.pendingAid = job.job_id;
        this.patch({ aid: null, deck: null, quiz: null, result: null, quizHelp: null, requestedJobId: job.job_id, jobs: [job, ...this.view.jobs] });
      }
    });
  }
  /** The learner's edits to the open concept map (rename, remove, merge, add): kept with the aid. */
  async editConceptMap(ops: W.ConceptEditOp[]): Promise<void> {
    const aid = this.view.aid;
    if (!aid || aid.kind !== "concept_map" || ops.length === 0) return;
    await this.action(async (current) => {
      const edited = valid<W.Aid>("Aid", await this.mutate("PATCH", `/v1/study/aids/${aid.aid_id}/concept-map`, { ops }));
      if (current() && this.view.aid?.aid_id === aid.aid_id) this.patch({ aid: edited, aids: this.view.aids.map((a) => (a.aid_id === edited.aid_id ? edited : a)) });
    });
  }
  /** A concept's own study actions, through the same cited, consented and budgeted jobs as the Generate panel:
   * a chat lesson on it, flashcards or a quiz focused on it. */
  async conceptAction(name: string, action: "ask" | "flashcards" | "quiz", preferCloud = true): Promise<void> {
    const focus = `Only the concept "${name.slice(0, 80)}" and what the material says about it.`;
    if (action === "ask") await this.generate("chat", 10, false, preferCloud, `Explain ${name.slice(0, 80)} using my material, step by step.`);
    else await this.generate(action, action === "quiz" ? 5 : 6, false, preferCloud, undefined, { focus });
  }
  /** A concept map of a typed topic with no document: the pack's when it names a pack topic, else a generated map
   * (no source, labelled so). */
  // --- the research agent's study packs (SPEC §18): the conversation, the approved brief, then the pack ---------------
  async researchChat(body: R.ChatRequest): Promise<R.ChatTurn> {
    return research<R.ChatTurn>("ChatTurn", await this.mutate("POST", "/v1/study/research/chat", body));
  }
  async startResearch(brief: R.Brief, captureId: string | null): Promise<R.Record> {
    return research<R.Record>("Record", await this.mutate("POST", "/v1/study/research", { brief, capture_id: captureId }));
  }
  async researchPacks(): Promise<R.Record[]> {
    return research<R.RecordList>("RecordList", await this.commands().get("/v1/study/research", this.headers())).items;
  }
  async researchPack(id: string): Promise<R.Record> {
    return research<R.Record>("Record", await this.commands().get(`/v1/study/research/${id}`, this.headers()));
  }
  async cancelResearch(id: string): Promise<R.Record> {
    return research<R.Record>("Record", await this.mutate("POST", `/v1/study/research/${id}/cancel`, {}));
  }
  async makeTopicMap(topic: string, preferCloud = true): Promise<void> {
    const asked = topic.trim();
    if (!asked) return;
    await this.action(async (current) => {
      const made = valid<W.ConceptTopicMap>("ConceptTopicMap", await this.mutate("POST", "/v1/study/concept-maps", { topic: asked.slice(0, 120), execution: preferCloud ? "prefer_cloud" : "local" }));
      if (current()) this.patch({ conceptTopic: made, conceptTopics: [{ map_id: made.map_id, topic: made.topic, origin: made.origin, created_at: made.created_at }, ...(this.view.conceptTopics ?? []).filter((m) => m.map_id !== made.map_id)] });
    });
  }
  async loadTopicMaps(): Promise<void> {
    const list = valid<W.ConceptTopicMapList>("ConceptTopicMapList", await this.commands().get("/v1/study/concept-maps", this.headers()));
    this.patch({ conceptTopics: list.maps });
  }
  async openTopicMap(id: string | null): Promise<void> {
    if (id === null) { this.patch({ conceptTopic: null }); return; }
    await this.action(async (current) => {
      const found = valid<W.ConceptTopicMap>("ConceptTopicMap", await this.commands().get(`/v1/study/concept-maps/${id}`, this.headers()));
      if (current()) this.patch({ conceptTopic: found });
    });
  }
  async editTopicMap(ops: W.ConceptEditOp[]): Promise<void> {
    const open = this.view.conceptTopic;
    if (!open || ops.length === 0) return;
    await this.action(async (current) => {
      const edited = valid<W.ConceptTopicMap>("ConceptTopicMap", await this.mutate("PATCH", `/v1/study/concept-maps/${open.map_id}`, { ops }));
      if (current() && this.view.conceptTopic?.map_id === open.map_id) this.patch({ conceptTopic: edited });
    });
  }
  /** Ask for source-grounded coaching on a visible quiz item, without ever sending answer keys. */
  async requestQuizHelp(itemId: string, kind: "hint" | "walk_through", confirmReadableOnly = false, followUp?: string): Promise<void> {
    const quiz = this.view.quiz, aid = this.view.aid, source = this.view.source;
    const item = quiz?.items.find((item) => item.item_id === itemId);
    if (!quiz || !item || !aid || aid.kind !== "quiz" || aid.status !== "ready" || !source) return;
    const existing = this.view.quizHelp;
    if (existing && this.view.jobs.some((j) => j.job_id === existing.jobId && ["queued", "running", "waiting_online", "paused"].includes(j.state))) return;
    const instruction = kind === "hint"
      ? "Give me one short hint: explain one prerequisite term, input or symbol from the material, then ask one guiding question. Do not define the concept being tested, state its resulting effect or evaluate the options. Do not reveal the answer, fill the blank or identify the correct option."
      : "Walk me through only the first reasoning step: clarify a prerequisite quantity or how to read a piece of notation from the material, then ask me to try a comparison. Do not write the target objective or conclude the effect being tested. Do not reveal the final answer or identify the correct option.";
    const context = [item.stem.text, ...(item.options ?? []).map((o, i) => `${i + 1}. ${o.text}`)].join("\n");
    const previous = existing?.quizId === quiz.quiz_id && existing.itemId === itemId ? existing.aid?.claims.map((c) => c.text).join("\n").slice(0, 700) : undefined;
    const follow = followUp?.trim();
    const prefix = follow
      ? `Answer my follow-up ONLY, using the supplied material and keeping the correct quiz option hidden. Give only the next reasoning step if needed. Do not solve the quiz or repeat its target definition.\nMy follow-up:\n${follow.slice(0, 1000)}\n\nPrevious coaching (context, not another request):\n${previous ?? "None"}\n\n${QUIZ_CONTEXT_MARK}\n`
      : `${instruction} Use only the supplied material.\n\n${QUIZ_CONTEXT_MARK}\n`;
    const prompt = `${prefix}${context.slice(0, Math.max(0, 4000 - prefix.length))}`;
    await this.action(async (current) => {
      const job = valid<W.Job>("Job", await this.mutate("POST", `/v1/study/quizzes/${quiz.quiz_id}/items/${encodeURIComponent(itemId)}/help`, { kind: "chat", execution: "prefer_cloud", sources: aid.sources, prompt, ...(confirmReadableOnly ? { confirm_readable_only: true } : {}) }));
      if (current() && this.view.quiz?.quiz_id === quiz.quiz_id && this.view.aid?.aid_id === aid.aid_id) {
        this.patch({ quizHelp: { quizId: quiz.quiz_id, itemId, kind, jobId: job.job_id, aid: null }, jobs: [job, ...this.view.jobs] });
      }
    });
  }
  async jobAction(id: string, action: "cancel" | "retry" | "resume"): Promise<void> {
    await this.action(async (current) => {
      const job = valid<W.Job>("Job", await this.mutate("POST", `/v1/study/jobs/${id}/actions`, { action }));
      if (current()) {
        if (["retry", "resume"].includes(action) && job.kind !== "extract" && this.view.quizHelp?.jobId !== id) this.pendingAid = job.job_id;
        this.patch({ requestedJobId: action === "cancel" ? null : job.job_id, jobs: this.view.jobs.map((j) => j.job_id === id ? job : j) });
      }
    });
  }
  async selectExtra(id: string): Promise<void> {
    if (id === this.view.source?.source_id) return;
    if (this.view.selections.some((s) => s.source_id === id)) { this.patch({ selections: this.view.selections.filter((s) => s.source_id !== id) }); return; }
    if (this.view.selections.length >= 8) return;
    await this.action(async (current) => {
      const opened = valid<W.OpenedSource>("OpenedSource", await this.commands().post(`/v1/study/sources/${id}/open`, { confirm: true }, clientId()));
      if (!current()) return;
      if (opened.source.state !== "ready") throw new CommandError(422, "confirm_needed", false, undefined);
      this.referenceGrants = [...this.referenceGrants.slice(-6), { sid: id, token: opened.open_token, expiry: Date.parse(opened.expires_at) }];
      this.patch({ selections: [...this.view.selections, { source_id: id, revision: opened.source.revision }] });
    });
  }
  async startExtraction(preferCloud: boolean): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    await this.action(async (current) => {
      const job = valid<W.Job>("Job", await this.mutate("POST", "/v1/study/jobs", { kind: "extract", execution: preferCloud ? "prefer_cloud" : "local", sources: [{ source_id: source.source_id, revision: source.revision }] }));
      if (current()) this.patch({ jobs: [job, ...this.view.jobs] });
    });
  }
  async loadTopics(): Promise<boolean> {
    const source = this.view.source;
    if (!source) return false;
    let found = false;
    await this.action(async (current) => {
      const result = valid<W.TopicLinks>("TopicLinks", await this.commands().get(`/v1/study/sources/${source.source_id}/topics`, this.headers()));
      if (current()) { this.patch({ topics: result.items }); found = true; }
    });
    return found;
  }
  /** Bind a real installed objective to the currently authorised, pinned material. */
  async beginJourney(node_id: string, kc_id: string): Promise<boolean> {
    const source = this.view.source;
    if (!source) return false;
    let done = false;
    await this.action(async (current) => {
      const reply = await this.mutate("POST", "/v1/learning-journey", { node_id, kc_id, source: { source_id: source.source_id, revision: source.revision } });
      if (!isValid("learning/journey#/$defs/Reply", reply)) throw new CommandError(200, "invalid_reply", false, undefined);
      done = current();
    });
    return done;
  }
  async loadNotes(): Promise<void> {
    await this.action(async (current) => {
      const [sessions, saved] = await Promise.all([this.commands().get<unknown>("/v1/session-notes"), this.commands().get<unknown>("/v1/study/notes")]);
      if (!Array.isArray(sessions) || !isValid("api/commands#/$defs/StudyNoteList", saved)) throw new CommandError(200, "invalid_reply", false, undefined);
      const local = saved as { notes: { note_id: string; source: string; text: string | null; private?: boolean }[] };
      const notes: StudyWorkspaceView["notes"] = sessions.map((raw) => readSessionNote(raw)).filter((n) => n.state === "ready").map((n) => ({ id: n.note_id, kind: "session_note", title: n.private ? null : n.title, private: n.private }));
      notes.push(...local.notes.map((n) => ({ id: n.note_id, kind: (n.source === "voice" ? "dictated_note" : "board_note") as "dictated_note" | "board_note", title: n.private ? null : n.text?.slice(0, 80) ?? null, private: n.private ?? false })));
      if (current()) this.patch({ notes });
    });
  }
  async importNote(note: StudyWorkspaceView["notes"][number], confirmed: boolean): Promise<void> {
    let added: W.Source | null = null;
    await this.action(async (current) => {
      const result = valid<W.Source>("Source", await this.mutate("POST", "/v1/study/sources", { kind: note.kind, note_id: note.id, ...(confirmed ? { confirm_private: true } : {}) }));
      if (current()) added = result;
    });
    if (added) { await this.load(); await this.open((added as W.Source).source_id); }
  }
  stopReading(): void {
    this.speechGeneration++; this.cancelSpeech?.(); this.cancelSpeech = null;
    this.resumeSpeech?.(); this.resumeSpeech = null;
    this.podcastAudio?.resume?.();
    if (this.speechExpiry !== null) clearTimeout(this.speechExpiry);
    this.speechExpiry = null;
    this.finishSpeech?.(false); this.finishSpeech = null;
    if (this.view.reading || this.view.audioPreparing || this.view.audioPaused || this.view.audioCompleted) this.patch({ reading: false, audioPreparing: false, audioPaused: false, audioCompleted: false });
  }
  pauseOverview(): void {
    if (!this.view.reading || this.view.audioPaused || !this.podcastAudio?.pause) return;
    this.podcastAudio.pause(); this.patch({ audioPaused: true });
  }
  async resumeOverview(): Promise<void> {
    if (!this.view.reading || !this.view.audioPaused) return;
    const generation = this.speechGeneration, profile = this.profiles.generation, aid = this.view.aid;
    const current = () => generation === this.speechGeneration && profile === this.profiles.generation && this.view.aid?.aid_id === aid?.aid_id;
    try {
      if (this.view.podcastVoice?.enabled && aid) valid<W.SpeechPassage>("SpeechPassage", await this.commands().get(`/v1/study/aids/${aid.aid_id}/speech?claim=${this.view.spokenClaim}&limit=600&neural=true`, this.headers()));
      if (!current() || !this.view.audioPaused) return;
      this.podcastAudio?.resume?.(); this.patch({ audioPaused: false });
      this.resumeSpeech?.(); this.resumeSpeech = null;
    } catch (error) {
      if (current()) { this.stopReading(); this.patch({ error: error instanceof CommandError ? String(error.details?.reason ?? error.code) : "neural_speech_unavailable" }); }
    }
  }
  async playOverview(): Promise<void> {
    this.stopReading();
    const aid = this.view.aid, speech = this.speech;
    if (!aid || aid.kind !== "audio_overview" || aid.status !== "ready") return;
    if (this.view.podcastVoice?.enabled && this.podcastAudio?.available()) this.podcastAudio.activate(); // preserve Play before HTTP
    const neural = this.view.podcastVoice?.enabled === true;
    if (neural ? !this.view.podcastVoice?.configured || !this.podcastAudio?.available() : !speech?.available()) return;
    const generation = this.profiles.generation, reading = this.speechGeneration;
    const current = () => generation === this.profiles.generation && reading === this.speechGeneration && this.view.aid?.aid_id === aid.aid_id;
    const settings = this.profiles.getView().settings;
    const rate = settings?.speech_rate ?? 1, voice = settings?.speech_voice ?? "";
    // A two-voice podcast (STU-21) alternates the learner's voice with another voice on the device, if there is one.
    const second = aid.settings?.voices === 2 ? speech?.voices?.().find((v) => v.id !== voice)?.id : undefined;
    if (!neural) speech?.configure?.(rate, voice);
    this.patch({ reading: true, audioPreparing: neural, error: null });
    const expiries = aid.sources.flatMap((s) => {
      if (s.source_id === this.view.source?.source_id && this.view.source.private) return [this.grantExpires];
      return this.referenceGrants.filter((g) => g.sid === s.source_id).map((g) => g.expiry);
    });
    if (expiries.length) this.speechExpiry = setTimeout(() => this.stopReading(), Math.max(0, Math.min(...expiries) - Date.now()));
    type Prepared = { passage: W.SpeechPassage; audio: Blob | null; offset: number };
    const prepare = async (claim: number, offset: number): Promise<Prepared | null> => {
      if (!current()) return null;
      const passage = valid<W.SpeechPassage>("SpeechPassage", await this.commands().get(`/v1/study/aids/${aid.aid_id}/speech?claim=${claim}&offset=${offset}${neural ? "&limit=600&neural=true" : ""}`, this.headers()));
      if (!current()) return null;
      if (passage.aid_id !== aid.aid_id || passage.claim_index !== claim) throw new Error("invalid speech binding");
      let audio: Blob | null = null;
      if (neural) {
        const text_sha256 = await sha(new TextEncoder().encode(passage.text));
        if (!current()) return null;
        audio = await this.commands().request<Blob>("POST", `/v1/study/aids/${aid.aid_id}/neural-speech`, { claim, offset, text_sha256 }, clientId(), this.headers(), true);
        if (!current()) return null;
        if (audio.type !== "audio/wav" || audio.size < 44 || audio.size > 6000000) throw new Error("invalid neural audio");
      }
      return { passage, audio, offset };
    };
    // Observe speculative errors immediately, including a response that arrives after Stop/profile clear.
    const safely = (work: Promise<Prepared | null>) => work.then((value) => ({ value, error: null as unknown }), (error: unknown) => ({ value: null, error }));
    let pending = safely(prepare(0, 0)), complete = false;
    try {
      while (current()) {
        const loaded = await pending;
        if (!current()) return;
        if (loaded.error) throw loaded.error;
        if (!loaded.value) return;
        const { passage, audio, offset } = loaded.value;
        // A pause during the permission read starts a fresh check on Resume, rather than using an old approval.
        while (current()) {
          if (this.view.audioPaused) await new Promise<void>((resolve) => { this.resumeSpeech = resolve; });
          if (!current()) return;
          if (neural) {
            const fresh = valid<W.SpeechPassage>("SpeechPassage", await this.commands().get(`/v1/study/aids/${aid.aid_id}/speech?claim=${passage.claim_index}&offset=${offset}&limit=600&neural=true`, this.headers()));
            if (!current()) return;
            if (fresh.text !== passage.text) throw new Error("stale speech");
          }
          if (!this.view.audioPaused) break;
        }
        if (!current()) return;
        this.patch({ spokenClaim: passage.claim_index, audioPreparing: false });
        if (!neural && second && offset === 0) speech?.configure?.(rate, passage.claim_index % 2 === 1 ? second : voice);
        const playing = new Promise<boolean>((resolve) => {
          this.finishSpeech = resolve;
          const callbacks = { end: () => resolve(true), error: () => resolve(false) };
          if (audio && this.podcastAudio) this.cancelSpeech = this.podcastAudio.play(audio, rate, callbacks);
          else if (speech) this.cancelSpeech = speech.speak(passage.text, { start: () => {}, boundary: () => {}, ...callbacks });
          else throw new Error("voice unavailable");
        });
        const next: [number, number] | null = passage.next_offset !== null ? [passage.claim_index, passage.next_offset] : !passage.last_claim ? [passage.claim_index + 1, 0] : null;
        // At most one passage ahead; never synthesize an entire podcast speculatively. No persistent audio cache.
        if (neural && next) pending = safely(prepare(next[0], next[1]));
        const ok = await playing;
        if (!current()) return;
        this.finishSpeech = null; this.cancelSpeech = null;
        if (!ok) throw new Error("voice unavailable");
        if (!next) { complete = true; break; }
        if (!neural) pending = safely(prepare(next[0], next[1]));
        this.patch({ audioPreparing: true });
      }
    } catch (error) { if (current()) this.patch({ error: error instanceof CommandError ? String(error.details?.reason ?? error.code) : neural ? "neural_speech_unavailable" : "speech_unavailable" }); }
    finally { if (current()) { if (this.speechExpiry !== null) clearTimeout(this.speechExpiry); this.speechExpiry = null; this.resumeSpeech = null; this.patch({ reading: false, audioPreparing: false, audioPaused: false, audioCompleted: complete }); } }
  }
  /** Back from an aid to the material's study tools. */
  hideAid(): void { this.stopReading(); this.patch({ aid: null, deck: null, quiz: null, result: null, resultAnswers: {}, quizHelp: null }); }
  /** VOC-53, D130: a question spoken in the Chat tab was answered by a chat aid; the list gains it and it opens, as a
   * typed Chat question's answer would. Only an answer about the open source is shown. */
  async voiced(answer: { place: string; aid_id: string | null }): Promise<void> {
    const source = this.view.source;
    if (answer.place !== "study" || answer.aid_id === null || !source) return;
    try {
      const aids = await this.allAids(source.source_id);
      if (this.view.source?.source_id !== source.source_id || !aids.some((a) => a.aid_id === answer.aid_id)) return;
      this.patch({ aids });
    } catch { return; }
    await this.showAid(answer.aid_id);
  }
  async showAid(id: string): Promise<void> {
    this.stopReading();
    await this.action((current) => this.showAidWork(current, id));
  }
  private async showAidWork(current: () => boolean, id: string): Promise<void> {
    const aid = valid<W.Aid>("Aid", await this.commands().get(`/v1/study/aids/${id}`, this.headers()));
    if (!current()) return;
    const deck = aid.deck_id ? valid<W.Deck>("Deck", await this.commands().get(`/v1/study/decks/${aid.deck_id}`, this.headers())) : null;
    if (!current()) return;
    const quiz = aid.quiz_id ? valid<W.QuizView>("QuizView", await this.commands().get(`/v1/study/quizzes/${aid.quiz_id}`, this.headers())) : null;
    if (!current()) return;
    const saved = quiz ? valid<W.QuizSavedResult>("QuizSavedResult", await this.commands().get(`/v1/study/quizzes/${quiz.quiz_id}/saved-result`, this.headers())) : null;
    if (!current()) return;
    const journey = valid<W.StudyJourneyReply>("StudyJourneyReply", await this.mutate("POST", `/v1/study/aids/${id}/journey`, { action: "resume" })).journey;
    if (current()) this.patch({ aid, deck, quiz, result: saved?.result ?? null,
      resultAnswers: Object.fromEntries((saved?.answers ?? []).map((a) => [a.item_id, a.answer])), journey, quizFeedback: {}, quizChecked: {}, quizHelp: null, requestedJobId: null });
    if (current() && aid.kind === "audio_overview") await this.loadPodcastVoice();
  }
  /** Explicit actions update only the saved journey; generated aids and assessment evidence stay unchanged. */
  async saveJourney(action: W.StudyJourneyAction["action"]): Promise<void> {
    const aid = this.view.aid;
    if (!aid) return;
    await this.action(async (current) => {
      const reply = valid<W.StudyJourneyReply>("StudyJourneyReply", await this.mutate("POST", `/v1/study/aids/${aid.aid_id}/journey`, { action }));
      if (current()) this.patch({ journey: reply.journey });
    });
  }
  async continueJourney(target?: "lesson" | "quiz"): Promise<void> {
    const journey = this.view.journey;
    if (!journey) return;
    const aidId = target === "lesson" ? journey.lesson_aid_id : target === "quiz" ? journey.quiz_aid_id : journey.next_aid_id;
    const source = journey.sources[0];
    if (!aidId || !source) return;
    const currentRevision = this.view.sources.some((s) => s.source_id === source.source_id && s.revision === source.revision);
    await this.openAt(source.source_id, currentRevision ? undefined : source.revision, aidId);
    if (this.view.source?.source_id !== source.source_id || this.view.source.revision !== source.revision) return;
    if (!target && this.view.aid?.aid_id === aidId && !this.view.error) {
      if (journey.phase === "flashcards" && this.view.deck) this.patch({ deck: { ...this.view.deck, cards: this.view.deck.cards.filter((c) => journey.review_card_ids.includes(c.card_id)) } });
      if (journey.retry_quiz && this.view.result) { this.clearResult(); await this.saveDraft({}, 0); }
    }
  }
  async rate(card: W.Flashcard, rating: "again" | "hard" | "good" | "easy"): Promise<void> {
    const deck = this.view.deck;
    if (!deck) return;
    await this.action(async (current) => {
      const result = valid<W.ReviewResult>("ReviewResult", await this.mutate("POST", `/v1/study/decks/${deck.deck_id}/reviews`, { card_id: card.card_id, card_revision: card.revision, rating, revealed: true }));
      if (current()) {
        this.cardReturns.delete(card.card_id);
        if (rating === "again") this.cardReturns.set(card.card_id, Date.parse(result.due_at));
        this.scheduleCardReturn();
        this.patch({ deck: { ...deck, cards: deck.cards.filter((c) => c.card_id !== card.card_id) } });
      }
    });
    await this.load();
  }
  private clearCardReturns(): void {
    if (this.cardReturnTimer !== null) clearTimeout(this.cardReturnTimer);
    this.cardReturnTimer = null; this.cardReturns.clear();
  }
  /** One wake-up per due time, rather than polling an idle open deck and keeping its profile awake. */
  private scheduleCardReturn(): void {
    if (this.cardReturnTimer !== null) clearTimeout(this.cardReturnTimer);
    this.cardReturnTimer = null;
    if (!this.cardReturns.size) return;
    const delay = Math.max(this.view.busy ? 1000 : 0, Math.min(...this.cardReturns.values()) - Date.now() + 50);
    this.cardReturnTimer = setTimeout(() => {
      this.cardReturnTimer = null;
      if (this.view.busy) { this.scheduleCardReturn(); return; }
      const due = [...this.cardReturns].filter(([, at]) => at <= Date.now()).map(([id]) => id);
      for (const id of due) this.cardReturns.delete(id);
      void this.refreshDueReview(due);
      this.scheduleCardReturn();
    }, Math.min(delay, 2_147_483_647));
  }
  /** Restore due cards in the native review order, without changing any saved schedule or quiz progress. */
  async refreshDueReview(returned: string[] = []): Promise<void> {
    const generation = this.profiles.generation, selection = this.selection;
    await this.load();
    if (generation !== this.profiles.generation || selection !== this.selection || this.view.busy) return;
    const { deck, journey, aid } = this.view;
    if (!deck) return;
    const due = journey && aid?.aid_id === journey.cards_aid_id ? journey.review_card_ids : [];
    const missing = new Set([...due, ...returned].filter((id) => !deck.cards.some((c) => c.card_id === id)));
    if (!missing.size) return;
    try {
      const fresh = valid<W.Deck>("Deck", await this.commands().get(`/v1/study/decks/${deck.deck_id}`, this.headers()));
      if (generation === this.profiles.generation && selection === this.selection && !this.view.busy && this.view.deck === deck) {
        const queued = new Set([...deck.cards.map((c) => c.card_id), ...missing]);
        // Due learning cards precede untouched cards in the native queue, including timer-driven returns.
        this.patch({ deck: { ...deck, cards: fresh.cards.filter((c) => queued.has(c.card_id) && !c.suspended) } });
      }
    } catch { /* Saved schedules remain authoritative; reopening retries this read. */ }
  }
  /** Back to the questions after a result, keeping the answers, for a second attempt (ASM-28). */
  clearResult(): void { if (this.view.result) this.patch({ result: null }); }
  /** A finished quiz, taken again from the start: its questions stay, the saved answers go; earlier attempts remain. */
  async retakeQuiz(): Promise<void> { this.clearResult(); this.patch({ quizFeedback: {}, quizChecked: {} }); await this.saveDraft({}, 0); }
  /** Mark only the requested question through the existing native marker; no answer keys reach the client. */
  async checkAnswer(itemId: string, answer: string): Promise<void> {
    const quiz = this.view.quiz;
    if (!quiz || quiz.status !== "ready" || !answer.trim() || !quiz.items.some((i) => i.item_id === itemId)) return;
    await this.action(async (current) => {
      const result = valid<W.QuizResult>("QuizResult", await this.mutate("POST", `/v1/study/quizzes/${quiz.quiz_id}/attempts`, { quiz_revision: quiz.revision, submission_kind: "check", answers: [{ item_id: itemId, answer }] }));
      if (current() && this.view.quiz?.quiz_id === quiz.quiz_id) {
        const feedback = result.results.find((r) => r.item_id === itemId);
        if (feedback) this.patch({ quizFeedback: { ...this.view.quizFeedback, [itemId]: feedback }, quizChecked: { ...this.view.quizChecked, [itemId]: answer } });
      }
    });
  }
  async submit(answers: Record<string, string>): Promise<void> {
    const quiz = this.view.quiz;
    if (!quiz) return;
    await this.action(async (current) => {
      const result = valid<W.QuizResult>("QuizResult", await this.mutate("POST", `/v1/study/quizzes/${quiz.quiz_id}/attempts`, { quiz_revision: quiz.revision, submission_kind: "submit", answers: Object.entries(answers).filter(([, a]) => a.trim()).map(([item_id, answer]) => ({ item_id, answer })) }));
      if (current()) { const { draft: _, ...rest } = quiz; this.patch({ result, resultAnswers: answers, quiz: rest }); }
    });
    await this.load();
  }
  async correctText(text: string): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    let saved = false;
    await this.action(async (current) => {
      valid<W.Source>("Source", await this.mutate("POST", `/v1/study/sources/${source.source_id}/revisions`, { expected_revision: source.revision, text }));
      saved = current();
    });
    if (saved) await this.open(source.source_id);
  }
  async markPrivate(): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    await this.action(async (current) => {
      valid<W.Source>("Source", await this.mutate("PATCH", `/v1/study/sources/${source.source_id}`, { private: !source.private }));
      if (current()) this.close();
    });
    await this.load();
  }
  async remove(): Promise<void> {
    const source = this.view.source;
    if (!source) return;
    await this.action(async (current) => {
      const receipt = valid<W.DeleteReceipt>("DeleteReceipt", await this.mutate("DELETE", `/v1/study/sources/${source.source_id}`));
      if (current()) { this.close(); this.patch({ cleanupPending: receipt.cloud_cleanup === "pending" }); }
    });
    await this.load();
  }
  async loadPage(page: number, revision?: number, sourceId?: string): Promise<Blob | null> {
    const source = this.view.source;
    if (!source) return null;
    // A page read takes no lock: it is started by the material's own render, and must never stop the aid the learner
    // asked for (a saved quiz) from showing meanwhile.
    const generation = this.profiles.generation, selection = this.selection;
    try {
      const blob = await this.commands().request<Blob>("GET", `/v1/study/sources/${sourceId ?? source.source_id}/pages/${page}?revision=${revision ?? source.revision}`, undefined, null, this.headers(), true);
      if (blob.type !== "image/png" || blob.size > 2500000) return null;
      return generation === this.profiles.generation && selection === this.selection ? blob : null;
    } catch { return null; }
  }
  async readCitation(citation: W.Citation): Promise<boolean> {
    let ok = false;
    await this.action(async (current) => {
      if (citation.source_id !== this.view.source?.source_id || citation.revision !== this.view.source.revision) {
        const opened = valid<W.OpenedSource>("OpenedSource", await this.commands().post(`/v1/study/sources/${citation.source_id}/open`, { confirm: true, revision: citation.revision }, clientId()));
        if (!current()) return;
        this.referenceGrants = [...this.referenceGrants.slice(-6), { sid: citation.source_id, token: opened.open_token, expiry: Date.parse(opened.expires_at) }];
      }
      const chunks: W.Chunk[] = [];
      let cursor: string | null = null;
      do {
        const reply: W.ChunkList = valid("ChunkList", await this.commands().get(`/v1/study/sources/${citation.source_id}/content?revision=${citation.revision}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, this.headers()));
        if (!current()) return;
        chunks.push(...reply.items); cursor = reply.next_cursor;
      } while (cursor !== null);
      const cited = chunks.find((c) => c.chunk_id === citation.chunk_id && Array.from(c.text).slice(citation.start, citation.end).join("") === citation.quote);
      if (!cited) throw new CommandError(200, "invalid_reply", false, undefined);
      if (current()) {
        this.patch({ chunks }); ok = true;
        // A citation to a recording opens the player at that moment (STU-22): the cited line's own moment, or a
        // frame's (SPEC §20.4); for a YouTube video the transcript pane marks the line (the browser opens on Open at).
        const moment = citation.source_id === this.view.source?.source_id ? citationMoment(cited, citation) : null;
        if (moment !== null) this.seek(moment);
      }
    });
    return ok;
  }
  async refreshOpened(): Promise<void> {
    await this.load();
    const source = this.view.source;
    if (!source || this.view.busy) return;
    const generation = this.profiles.generation, selection = this.selection;
    try {
      const raw = await this.commands().get(`/v1/study/sources/${source.source_id}`);
      const summary = valid<W.SourceList>("SourceList", { items: [raw], next_cursor: null }).items[0];
      if (!summary) throw new CommandError(200, "invalid_reply", false, undefined);
      const fresh = this.view.followCurrent && "title" in summary ? summary : source;
      const chunks: W.Chunk[] = [];
      if (this.view.chunks.length === 0 || fresh.revision !== source.revision) {
        let cursor: string | null = null;
        do {
          const result: W.ChunkList = valid("ChunkList", await this.commands().get(`/v1/study/sources/${source.source_id}/content?revision=${fresh.revision}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, this.headers()));
          if (generation !== this.profiles.generation || selection !== this.selection) return;
          chunks.push(...result.items); cursor = result.next_cursor;
        } while (cursor !== null);
      }
      const aids = await this.allAids(source.source_id);
      if (generation === this.profiles.generation && selection === this.selection) {
        // Only the tool requested in this open material may take focus; never reopen old results on each poll.
        const asked = this.pendingAid !== null ? aids.find((a) => a.job_id === this.pendingAid && a.status === "ready") : undefined;
        const aid = (this.view.aid ? aids.find((a) => a.aid_id === this.view.aid?.aid_id) ?? null : null);
        if (!aid || aid.status === "stale" || fresh.revision !== source.revision) this.stopReading();
        if (fresh.revision !== source.revision) this.patch({ topics: [] });
        if (!aid) this.patch({ deck: null, quiz: null, result: null });
        const help = this.view.quizHelp;
        const helpAid = help ? aids.find((a) => a.job_id === help.jobId && a.kind === "chat" && a.status === "ready") : null;
        const quizHelp = help && this.view.quiz?.quiz_id === help.quizId && aid?.status === "ready" && fresh.revision === source.revision ? { ...help, aid: helpAid ?? null } : null;
        this.patch({ source: fresh, chunks: this.view.chunks.length === 0 || fresh.revision !== source.revision ? chunks : this.view.chunks, aids, aid, quizHelp,
          quiz: this.view.quiz && aid?.status === "stale" ? { ...this.view.quiz, status: "stale" } : this.view.quiz });
        if (asked) {
          await this.showAid(asked.aid_id);
          if (generation === this.profiles.generation && selection === this.selection && this.view.aid?.aid_id === asked.aid_id) this.pendingAid = null;
        }
      }
    } catch (error) {
      if (generation === this.profiles.generation && selection === this.selection) {
        if (error instanceof CommandError && error.details?.reason === "grant_expired") this.close();
        if (error instanceof CommandError && error.code === "not_found") { this.close(); return; }
        this.patch({ error: error instanceof CommandError ? String(error.details?.reason ?? error.code) : "unreachable" });
      }
    }
  }
}
