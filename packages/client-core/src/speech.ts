// Read-aloud and dictation share the session's voice controller. Neither submits a tutor turn.
import type { api_commands, voice_activities } from "@jana/contracts-ts";
import type { Store } from "./store.js";
import { clientId } from "./ids.js";
import type { CapturedQuestion, CapturePurpose } from "./capture.js";
import { CommandClient, CommandError } from "./stream/commands.js";
import type { VoiceController } from "./voice.js";

export type ReadSource = voice_activities.ReadSource;
export type StudyNote = voice_activities.StudyNote;
/** A note as the list shows it (`StudyNoteListed`, API 1.10): a private one without its text until it is opened (D102,
 * D104). */
export type ListedNote = api_commands.StudyNoteListed;

/** A note as the list shows it: a private note's text is never kept in the list, only shown once opened. */
export function listedNote(note: StudyNote): ListedNote {
  const { note_id, text, source, created_at, updated_at } = note;
  return note.private === true ? { note_id, text: null, source, created_at, updated_at, private: true } : { note_id, text, source, created_at, updated_at };
}
export interface SpeechPreferences { speech_rate?: number; speech_voice?: string; read_aloud?: boolean; low_power?: boolean }
export interface LocalVoice { id: string; name: string }
export interface LocalSpeech {
  voices?(): LocalVoice[];
  configure?(rate: number, voice: string): void;
  available(): boolean;
  subscribe(listener: () => void): () => void;
  speak(text: string, callbacks: { start(): void; boundary(charIndex: number, elapsedMs: number): void;
    end(): void; error(): void }): () => void;
}
export interface SpeechView {
  available: boolean;
  partId: string | null;
  reading: boolean;
  text: string;
  charIndex: number;
  dictation: "idle" | "starting" | "listening" | "finishing" | "review";
  draft: CapturedQuestion | null;
  error: string | null;
  transcript?: string;
  tentative?: string;
}
interface Utterance { utterance_id: string; text: string; transport: "sidecar" | "os" }

export class SpeechController {
  private noteSave: { text: string; capture: string | undefined; key: string } | null = null;
  private confirmations = new Map<string, { text: string; key: string }>();
  private automaticRead = false;
  private preferences: SpeechPreferences = {};
  private autoSeen = new Set<string>();
  private autoQueue: string[] = [];
  private observed = false;
  private pumping = false;
  private voiceTurns = new Set<string>();
  private view: SpeechView;
  private listeners = new Set<() => void>();
  private generation = 0;
  private cancelSpeech: (() => void) | null = null;
  private poll: ReturnType<typeof setTimeout> | null = null;
  private held = false;
  private closed = false;
  private utterance: Utterance | null = null;
  private osStarted = false;
  private osElapsed = 0;
  private readResetNeeded = false;
  private retainedDrafts = new Set<string>();
  private dictationOpened = false;
  private reporting = Promise.resolve();
  private unsubscribe: (() => void)[];

  /** `hasPack`: read-aloud goes through the speech pack's voice (the core's `voice` capability), with the OS voice when
   * the sidecar cannot start; `canDictate`: the pack also hears speech (`voice_dictation`). A read-aloud-only pack
   * (TABLE VOC-03a) reads aloud and offers no dictation (VOC-46). */
  constructor(readonly sessionId: string, private commands: CommandClient, readonly voice: VoiceController,
    private local: LocalSpeech, readonly hasPack: boolean, deferSubscriptions = false, readonly canDictate = hasPack) {
    this.view = { available: hasPack || local.available(), partId: null, reading: false, text: "", charIndex: 0,
      dictation: "idle", draft: null, error: null };
    this.unsubscribe = deferSubscriptions ? [] : [local.subscribe(() => this.update({ available: hasPack || local.available() })),
      voice.subscribe(() => this.voiceChanged())];
  }
  getState = (): SpeechView => this.view;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  private update(change: Partial<SpeechView>): void {
    if (this.closed) return;
    this.view = { ...this.view, ...change };
    for (const listener of this.listeners) listener();
    this.pump();
  }
  private path(suffix: string): string { return `/v1/sessions/${encodeURIComponent(this.sessionId)}/voice/${suffix}`; }
  private failure(error: unknown): string {
    return error instanceof CommandError && error.code === "turn_active" ? "voice.wait_for_tutor" : "voice.unavailable";
  }
  private report(phase: string, charIndex: number, elapsedMs: number): void {
    const utterance = this.utterance;
    if (utterance?.transport !== "os") return;
    if (phase === "start") this.osStarted = true;
    if (!this.osStarted) return;
    const elapsed = Math.max(this.osElapsed, elapsedMs);
    this.osElapsed = elapsed;
    this.reporting = this.reporting.then(async () => {
      await this.commands.post(this.path(`utterances/${utterance.utterance_id}/playback`),
        { type: "read_aloud_playback", phase, char_index: charIndex, elapsed_ms: elapsed }, clientId());
    }).catch(() => undefined);
  }
  private voiceChanged(): void {
    const voice = this.voice.getState();
    if (this.view.dictation !== "idle" && (this.view.transcript !== voice.student || this.view.tentative !== voice.tentative)) this.update({ transcript: voice.student, tentative: voice.tentative });
    if (voice.state === "listening" && this.utterance?.transport === "os") this.stopReading();
    if (voice.state === "listening" && this.utterance?.transport === "sidecar") {
      this.utterance = null;
      this.update({ reading: false });
    }
    if (!voice.connected && this.utterance?.transport === "sidecar" && this.view.reading) this.stopReading();
    if (voice.state === "unavailable") {
      if (["starting", "listening", "finishing"].includes(this.view.dictation)) this.cancelDictation();
      if (this.utterance?.transport === "sidecar" && this.view.reading) this.stopReading();
      this.update({ error: voice.reason ?? "voice.unavailable" });
      return;
    }
    if (this.utterance?.transport === "sidecar" && voice.captionItem === this.utterance.utterance_id) {
      const active = voice.words.filter((word) => word.start_ms <= voice.playedMs).length - 1;
      const words = [...this.utterance.text.matchAll(/\S+/g)];
      this.update({ text: this.utterance.text, charIndex: words[Math.max(0, active)]?.index ?? 0,
        reading: voice.state !== "idle" && voice.state !== "interrupted" });
    }
  }
  configure(preferences: SpeechPreferences): void {
    const wasAutomatic = this.preferences.read_aloud && !this.preferences.low_power;
    this.preferences = preferences;
    this.local.configure?.(preferences.speech_rate ?? 1, preferences.speech_voice ?? "");
    this.voice.setRate?.(preferences.speech_rate ?? 1);
    if (wasAutomatic && (!preferences.read_aloud || preferences.low_power)) {
      this.autoQueue = [];
      if (this.automaticRead) this.stopReading();
    }
    if (preferences.low_power && this.view.dictation === "idle" && !this.view.reading) this.voice.close();
    this.pump();
  }
  localVoices(): LocalVoice[] { return this.local.voices?.() ?? []; }
  voiceEvent(event: Record<string, unknown>): void {
    // Conversation captions name a tutor turn; read-aloud captions also have an utterance item id.
    if (event.type === "voice.caption" && event.speaker === "tutor" && typeof event.turn_id === "string" &&
      !(typeof event.item_id === "string" && event.item_id.startsWith("utt_"))) {
      this.voiceTurns.add(event.turn_id);
      this.autoQueue = this.autoQueue.filter((id) => !this.voiceTurns.has(this.lastStore?.parts[id]?.turn_id ?? ""));
    }
    this.voice.event(event);
    if (event.type === "notice" && event.code === "calm_screen") {
      // VOC-20: the lesson flow stops. The voice controller has released the mic; end reading and any dictation
      // without the stop key, which would cut off the core's spoken form of the distress template.
      this.autoQueue = [];
      if (this.view.reading) this.stopReading();
      if (["starting", "listening", "finishing"].includes(this.view.dictation)) this.cancelDictation(false);
    }
  }
  private lastStore: Store | null = null;
  observe(store: Store): void {
    this.lastStore = store;
    const parts = (store.sessions[this.sessionId]?.turn_ids ?? []).flatMap((id) => {
      const turn = store.turns[id];
      return turn?.status === "complete" ? turn.part_ids : [];
    });
    for (const id of parts) {
      const part = store.parts[id];
      if (!part || part.kind !== "text" || part.status !== "complete" || this.autoSeen.has(id)) continue;
      this.autoSeen.add(id);
      if (this.observed && this.preferences.read_aloud && !this.preferences.low_power &&
        !this.voiceTurns.has(part.turn_id ?? "")) this.autoQueue.push(id);
    }
    this.observed = true;
    this.pump();
  }
  private pump(): void {
    if (this.pumping || this.closed || !this.autoQueue.length || !this.view.available || this.view.reading ||
      this.view.dictation !== "idle" || !this.preferences.read_aloud || this.preferences.low_power ||
      ["thinking", "listening", "speaking"].includes(this.voice.getState().state)) return;
    this.pumping = true;
    // Break notification recursion and allow a committed event batch (including voice captions) to settle first.
    void Promise.resolve().then(async () => {
      const next = this.autoQueue.shift();
      if (!this.closed && next && this.preferences.read_aloud && !this.preferences.low_power &&
        !this.voiceTurns.has(this.lastStore?.parts[next]?.turn_id ?? "")) await this.read(next, true);
    }).finally(() => { this.pumping = false; this.pump(); });
  }
  async read(partId: string | ReadSource, automatic = false): Promise<void> {
    const restarting = this.utterance?.transport === "sidecar" && this.view.reading &&
      this.voice.getState().captionItem === this.utterance.utterance_id;
    this.cancelDictation(); this.stopReading(!automatic);
    const generation = ++this.generation;
    this.automaticRead = automatic;
    this.update({ partId: typeof partId === "string" ? partId : JSON.stringify(partId), reading: true, text: "", charIndex: 0, error: null });
    try {
      if (this.hasPack) {
        // Fresh unit: delayed output from a stopped directive must never acquire a new utterance tag.
        if (restarting || this.readResetNeeded) this.voice.close();
        this.readResetNeeded = false;
        this.voice.activate(); // preserve the user's playback gesture before first-use loading/HTTP awaits
        try {
          await this.voice.open();
        } catch (error) {
          // VOC-18: the pack's voice could not start, so the installed OS voice reads instead. With no socket the core
          // mints the utterance for the OS synthesiser; with no OS voice either, the failure stands.
          if (!this.local.available()) throw error;
          this.voice.close();
        }
      }
      if (generation !== this.generation || this.closed) return;
      const utterance = await this.commands.post<Utterance>(this.path("utterances"), typeof partId === "string" ? { type: "read_aloud", part_id: partId } : { type: "read_content", source: partId }, clientId());
      if (generation !== this.generation || this.closed) return;
      this.utterance = utterance; this.osStarted = false; this.osElapsed = 0;
      this.update({ text: utterance.text });
      if (utterance.transport === "sidecar") await this.voice.speak(utterance.utterance_id);
      else {
        if (!this.local.available()) throw new Error("local_voice_unavailable");
        this.cancelSpeech = this.local.speak(utterance.text, {
          start: () => { if (generation === this.generation) { this.update({ error: null }); this.report("start", 0, 0); } },
          boundary: (position, elapsed) => {
            if (generation !== this.generation || position < this.view.charIndex) return;
            this.update({ charIndex: position }); this.report("progress", position, elapsed);
          },
          end: () => {
            if (generation !== this.generation) return;
            this.report("end", utterance.text.length, this.osElapsed);
            this.update({ reading: false, charIndex: utterance.text.length }); this.cancelSpeech = null;
          },
          error: () => {
            if (generation !== this.generation) return;
            this.report("error", this.view.charIndex, this.osElapsed);
            this.update({ reading: false, error: "voice.synthesiser_unavailable" }); this.cancelSpeech = null;
          },
        });
      }
    } catch (error) {
      if (generation === this.generation) { this.stopReading(); this.update({ error: this.failure(error) }); }
    }
  }
  stopReading(clearQueue = true): void {
    if (clearQueue) this.autoQueue = [];
    ++this.generation;
    const sidecar = this.utterance?.transport === "sidecar" && this.view.reading;
    const identity = this.utterance?.utterance_id;
    const audible = identity !== undefined && this.voice.getState().captionItem === identity;
    if (this.utterance?.transport === "os" && this.view.reading) this.report("stop", this.view.charIndex, this.osElapsed);
    this.cancelSpeech?.(); this.cancelSpeech = null; this.utterance = null;
    this.update({ reading: false });
    if (sidecar && identity) { this.readResetNeeded = audible; this.voice.stopRead(identity); }
  }
  async beginDictation(purpose: CapturePurpose, destination: "question" | "note" = "question"): Promise<void> {
    if (!this.canDictate || !["idle", "review"].includes(this.view.dictation)) return;
    this.cancelDictation(); this.stopReading(); this.held = true;
    const generation = ++this.generation;
    this.update({ dictation: "starting", draft: null, error: null, transcript: "", tentative: "" });
    try {
      if (this.readResetNeeded) { this.voice.close(); this.readResetNeeded = false; }
      this.dictationOpened = !this.voice.getState().connected;
      await this.voice.open();
      if (generation !== this.generation || !this.held || this.closed) return;
      const draft = await this.commands.post<CapturedQuestion>(this.path("dictations"),
        { type: "create_dictation", client_capture_id: clientId(), purpose, ...(destination === "note" ? { destination } : {}) }, clientId());
      if (generation !== this.generation || !this.held || this.closed) {
        void this.commands.delete(this.path(`dictations/${draft.capture_id}`), clientId()).catch(() => undefined);
        return;
      }
      this.update({ dictation: "listening", draft });
      await this.voice.dictate(draft.capture_id, true);
      if (generation !== this.generation) return;
      if (this.voice.getState().state === "unavailable") throw new Error("microphone_unavailable");
      this.watchDraft(draft.capture_id, generation, Date.now() + 120_000);
    } catch (error) {
      if (generation !== this.generation) return;
      // Keep the specific microphone copy (VOC-16) the voice controller gave, not a generic failure.
      const voice = this.voice.getState();
      this.cancelDictation();
      this.update({ error: voice.state === "unavailable" && voice.reason ? voice.reason : this.failure(error) });
    }
  }
  endDictation(): void {
    this.held = false;
    const draft = this.view.draft;
    if (this.view.dictation === "starting") { this.cancelDictation(); return; }
    if (draft === null || this.view.dictation !== "listening") return;
    void this.voice.dictate(draft.capture_id, false);
    this.update({ dictation: "finishing" });
  }
  private watchDraft(identity: string, generation: number, deadline: number): void {
    if (this.poll !== null) clearTimeout(this.poll);
    this.poll = setTimeout(() => {
      this.poll = null;
      void this.commands.get<CapturedQuestion>(this.path(`dictations/${identity}`)).then((draft) => {
        if (generation !== this.generation || this.closed) return;
        if (draft.status === "awaiting_confirmation") {
          this.held = false; void this.voice.dictate(identity, false);
          this.update({ dictation: "review", draft });
          // End this unit's generation before a later talk request. Delayed speculative empty responses cannot
          // acquire the identity of the next tutor response, and the next use reopens the same session socket.
          this.voice.close();
        } else if (draft.status === "unreadable" || draft.status === "quality_failed") {
          // VOC-14: a failed dictation transcript.
          this.cancelDictation();
          this.update({ error: "voice.dictation_unheard" });
        } else if (draft.status === "cancelled" || Date.now() > deadline) {
          this.cancelDictation();
          if (draft.status !== "cancelled") this.update({ error: "voice.dictation_timeout" });
        } else this.watchDraft(identity, generation, deadline);
      }).catch((error: unknown) => {
        if (generation === this.generation) { this.cancelDictation(); this.update({ error: this.failure(error) }); }
      });
    }, 100);
  }
  takeDraft(): CapturedQuestion | null {
    const draft = this.view.dictation === "review" ? this.view.draft : null;
    if (draft) {
      this.retainedDrafts.add(draft.capture_id);
      this.update({ dictation: "idle", draft: null });
    }
    return draft;
  }
  async confirmDraft(draft: CapturedQuestion, text: string): Promise<CapturedQuestion> {
    if (this.closed) throw new Error("speech_session_closed");
    let request = this.confirmations.get(draft.capture_id);
    if (request?.text !== text) { request = { text, key: clientId() }; this.confirmations.set(draft.capture_id, request); }
    const confirmed = await this.commands.post<CapturedQuestion>(this.path(`dictations/${draft.capture_id}/confirm`),
      { type: "confirm_dictation", text }, request.key);
    if (this.closed) throw new Error("speech_session_closed");
    for (const identity of this.retainedDrafts) {
      void this.commands.delete(this.path(`dictations/${identity}`), clientId()).catch(() => undefined);
    }
    this.retainedDrafts.clear();
    return confirmed;
  }
  /** The notes, newest first; a private note comes without its text until it is opened (D102). */
  listNotes(query = ""): Promise<api_commands.StudyNoteList> {
    return this.commands.get(`/v1/study/notes?q=${encodeURIComponent(query)}`);
  }
  /** Mark a note private, or not (D102): a courtesy on a shared computer, not a lock. */
  markNotePrivate(identity: string, marked: boolean): Promise<ListedNote> {
    const body: api_commands.StudyNoteMark = { private: marked };
    return this.commands.patch<ListedNote>(`/v1/study/notes/${encodeURIComponent(identity)}`, body, clientId());
  }
  /** A private note's text, after the student confirmed the confirm step (D102). */
  openNote(identity: string): Promise<StudyNote> {
    const body: api_commands.StudyNoteOpen = { confirm: true };
    return this.commands.post(`/v1/study/notes/${encodeURIComponent(identity)}/open`, body, clientId());
  }
  async saveNote(text: string, draft?: CapturedQuestion): Promise<StudyNote> {
    if (this.closed) throw new Error("speech_session_closed");
    if (this.noteSave?.text !== text || this.noteSave?.capture !== draft?.capture_id) this.noteSave = { text, capture: draft?.capture_id, key: clientId() };
    const note = await this.commands.post<StudyNote>(`/v1/sessions/${this.sessionId}/study/notes`,
      { type: "save_note", text, ...(draft ? { capture_id: draft.capture_id } : {}) }, this.noteSave.key);
    this.noteSave = null;
    if (draft) this.retainedDrafts.delete(draft.capture_id);
    if (this.closed) throw new Error("speech_session_closed");
    return note;
  }
  async deleteNote(identity: string): Promise<void> {
    this.stopReading();
    await this.commands.delete(`/v1/study/notes/${encodeURIComponent(identity)}`, clientId());
  }
  discardDraft(draft?: CapturedQuestion): void {
    if (!draft) return;
    this.retainedDrafts.delete(draft.capture_id);
    void this.commands.delete(this.path(`dictations/${draft.capture_id}`), clientId()).catch(() => undefined);
  }
  cancelDictation(stopVoice = true): void {
    ++this.generation; this.held = false;
    if (this.poll !== null) clearTimeout(this.poll);
    this.poll = null;
    const draft = this.view.draft, stop = draft !== null && this.view.dictation !== "review";
    const closeOpening = this.dictationOpened && this.view.dictation === "starting";
    this.dictationOpened = false;
    this.update({ dictation: "idle", draft: null });
    if (closeOpening) this.voice.close();
    if (stop && stopVoice) this.voice.stop();
    if (draft !== null) void this.commands.delete(this.path(`dictations/${draft.capture_id}`), clientId()).catch(() => undefined);
  }
  close(): void {
    if (this.closed) return;
    this.stopReading(); this.cancelDictation();
    this.update({ partId: null, text: "", charIndex: 0, error: null, transcript: "", tentative: "" });
    this.lastStore = null; this.noteSave = null; this.confirmations.clear(); this.voiceTurns.clear();
    this.closed = true;
    for (const unsubscribe of this.unsubscribe) unsubscribe();
    for (const identity of this.retainedDrafts) {
      void this.commands.delete(this.path(`dictations/${identity}`), clientId()).catch(() => undefined);
    }
    this.retainedDrafts.clear();
    this.unsubscribe = []; this.voice.close();
  }
  activate(): void {
    if (!this.closed && this.unsubscribe.length) return;
    this.closed = false;
    this.unsubscribe = [this.local.subscribe(() => this.update({ available: this.hasPack || this.local.available() })),
      this.voice.subscribe(() => this.voiceChanged())];
    this.update({ available: this.hasPack || this.local.available() });
  }
}
