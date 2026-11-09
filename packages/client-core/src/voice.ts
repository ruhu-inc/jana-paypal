// One voice socket per open tutor session. Audio devices belong to the platform adapter; credentials and frames
// stay in memory, and the UI connects only to the authenticated core origin (VOC-26).
import { CLIENT_KEYS } from "./generated/keys.js";
import { CueSync, type CaptionCue, type CaptionWord, type PenView, type PointerTarget, type PointerView, type WritingView } from "./pointer.js";
import { FRAME_MS, LoudnessTrack, MouthFollower, frameLevels, type MouthShape } from "./mouth.js";

/** Whether a cue's target is drawn now (VSL-45): a board line or a visual part present in the document. Without a
 * document (tests, workers) every target counts as on screen. */
function onScreen(target: PointerTarget): boolean {
  if (typeof document === "undefined") return true;
  const selector = target.kind === "step" ? `[data-step-id="${target.step_id}"]` : target.kind === "kc" ? `[data-kc-node="${target.kc_id}"]`
    : target.kind === "photo_region" ? `[data-photo-region="${target.capture_id}:${target.region_id}"]` : `[data-part="${target.part_id}"]`;
  return document.querySelector(selector) !== null;
}
import type { Endpoint } from "./stream/client.js";

export type TalkMode = "push_to_talk" | "hands_free";
export type VoiceState = "idle" | "listening" | "thinking" | "speaking" | "interrupted" | "unavailable";
export interface VoiceView {
  connected: boolean;
  state: VoiceState;
  mode: TalkMode;
  reason: string | null;
  student: string;
  tentative: string;
  tutor: string;
  words: { w: string; start_ms: number }[];
  playedMs: number;
  captionItem: string | null;
  /** The temporary pointer and a line being written on, fired from actual playback (VOC-51, UXS-63). */
  pointer: PointerView | null;
  /** The tutor's pen (UXS-56): at rest or at the target it reaches before the ring appears; absent means at rest. */
  pen?: PenView;
  writing: WritingView | null;
  /** The presence's speaking mouth (DSN-27), from the loudness of the audio being heard; absent means closed. */
  mouth?: MouthShape;
  /** The microphone level while listening, 0 to 1 (the presence's ring, DSN-27); absent means 0. */
  micLevel?: number;
}
export interface VoiceAudio {
  activate?(): void;
  setRate?(rate: number): void;
  capture(onPcm: (pcm: ArrayBuffer) => void): Promise<{ echoCancellation: boolean; headphones: boolean }>;
  releaseMic(): void;
  play(tag: string, pcm: Int16Array, report: (playedMs: number, ended: boolean) => void): void;
  /** Milliseconds of this tag's audio the student has heard: frames the device has played, less its reported output
   * latency, in source-audio time; null before any has played. Cues fire from this clock (VOC-51). */
  position?(tag: string): number | null;
  flush(tag?: string): void;
  close(): void;
}
export interface VoiceSocket {
  binaryType: BinaryType;
  readyState: number;
  bufferedAmount: number;
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  send(data: string | ArrayBuffer): void;
  close(): void;
}

/** Microphone error reasons (VOC-16). Each one leaves typing usable and the next press tries the microphone again.
 * `voice.too_noisy` has no client detector: it arrives as a core `reason_key`. */
export const MICROPHONE_REASONS: readonly string[] = ["voice.microphone_missing", "voice.microphone_denied",
  "voice.too_noisy", "voice.microphone_unavailable"];
/** Reasons a restart cannot fix: no speech pack fits this computer, or it does not fit in memory (VOC-14). */
export const PERMANENT_REASONS: readonly string[] = ["voice.pack_unavailable", "voice.memory_unavailable"];
/** Every voice reason with student copy: the unavailable reasons of VOC-14 and the microphone errors of VOC-16, from
 * the client or as a core `reason_key` (the keys `voiceErrorKey` in @jana/ui names). Nothing else becomes a reason. */
const VOICE_REASONS: readonly string[] = ["voice.wait_for_tutor", "voice.dictation_timeout",
  "voice.dictation_unheard", "voice.synthesiser_unavailable", "voice.saving_battery", "voice.unavailable",
  "voice.stopped_working", "voice.disconnected", "voice.protocol_error", "voice.audio_backpressure",
  "voice.endpoint_error", "voice.endpoint_unavailable", ...PERMANENT_REASONS, ...MICROPHONE_REASONS];
/** Map a getUserMedia failure to its VOC-16 reason. */
export function microphoneReason(error: unknown): string {
  const name = typeof error === "object" && error !== null && "name" in error ? String(error.name) : "";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "voice.microphone_missing";
  if (name === "NotAllowedError" || name === "SecurityError") return "voice.microphone_denied";
  return "voice.microphone_unavailable";
}


const EMPTY: VoiceView = { connected: false, state: "idle", mode: "push_to_talk", reason: null,
  student: "", tentative: "", tutor: "", words: [], playedMs: 0, captionItem: null, pointer: null, writing: null, pen: { target: null, since_ms: 0 } };
const hex = (bytes: Uint8Array): string => [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");

/** The open chat a voice conversation lives in (VOC-53, D130): Ask anything on Home, a study set's Chat with the
 * open source and its grant, or the tutor screen (with Cloud voice on: the owner's choice for this entry, replacing
 * D130 there). With none the socket serves read-aloud and dictation only. */
export type VoicePlace = { place: "ask" } | { place: "tutor" } | { place: "study"; source_id: string; revision: number; open: string };

export class VoiceController {
  private view: VoiceView = EMPTY;
  private placed: VoicePlace | null = null;
  private readonly listeners = new Set<() => void>();
  private socket: VoiceSocket | null = null;
  private generation = 0;
  private held = false;
  private opening: Promise<void> | null = null;
  private cancelOpen: (() => void) | null = null;
  private incoming: Promise<void> = Promise.resolve();
  private dictationId: string | null = null;
  private readId: string | null = null;
  private readTag: string | null = null;
  private dictationListening = false;
  private thinkHeld = false;
  // VOC-15: the app does not have focus, so no capture is open whatever the talk mode.
  private unfocused = false;
  // VOC-20: a calm screen (PRV-14) holds voice idle until resumeAfterCalm(); the hold survives a reconnect.
  private calm = false;
  // The tutor response whose audio is playing (its 16-byte tag), and the cues of its accepted turn (VOC-10, VOC-51).
  private tutorTag: string | null = null;
  private ticker: ReturnType<typeof setInterval> | null = null;
  // The speaking mouth (DSN-27): the loudness of the audio playing, read at the heard position every frame while it plays.
  private readonly loudness = new LoudnessTrack();
  private readonly mouthShape = new MouthFollower();
  private loudTag: string | null = null;
  private mouthTimer: ReturnType<typeof setInterval> | null = null;
  readonly cues = new CueSync(CLIENT_KEYS.pointer_ttl_ms, () => {
    const { pointer, writing, pen } = this.cues.getState();
    if (pointer !== this.view.pointer || writing !== this.view.writing || pen !== this.view.pen) this.update({ pointer, writing, pen });
  }, (target) => onScreen(target));

  constructor(readonly sessionId: string, private readonly endpoint: () => Promise<Endpoint>,
    private readonly audio: VoiceAudio,
    private readonly createSocket: (url: string, protocols: string[]) => VoiceSocket =
      (url, protocols) => new WebSocket(url, protocols) as unknown as VoiceSocket) {}

  getState = (): VoiceView => this.view;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  private update(change: Partial<VoiceView>): void {
    // A reason is a translation key the student sees. A transport diagnostic or a reason this build has no copy for
    // never becomes one: it shows the VOC-14 give-up copy, which offers "Turn voice on again".
    if (change.reason != null && !VOICE_REASONS.includes(change.reason)) change = { ...change, reason: "voice.stopped_working" };
    this.view = { ...this.view, ...change };
    for (const listener of this.listeners) listener();
  }
  private send(control: Record<string, unknown>): void {
    if (this.socket?.readyState === 1) this.socket.send(JSON.stringify(control));
  }

  /** VOC-53, D130: name the open chat this conversation is in, or none (then only read-aloud and dictation). Sent now
   * when connected and again on every reconnect; changing it stops what the old place was saying. */
  setPlace(place: VoicePlace | null): void {
    if (JSON.stringify(place) === JSON.stringify(this.placed)) return;
    this.placed = place;
    this.send({ type: "place", ...(place ?? { place: null }) });
  }
  get place(): VoicePlace | null { return this.placed; }

  open(): Promise<void> {
    if (this.socket?.readyState === 1) return Promise.resolve();
    if (this.opening !== null) return this.opening;
    const generation = ++this.generation;
    this.update({ state: "thinking", reason: null });
    this.opening = (async () => {
      const endpoint = await this.endpoint();
      if (generation !== this.generation) return;
      const url = new URL(endpoint.baseUrl);
      // The core on loopback, or the hosted demo's own front door on the page's own HTTPS origin; nothing else.
      const loopback = ["127.0.0.1", "[::1]"].includes(url.hostname) && url.protocol === "http:";
      const hosted = url.protocol === "https:" && url.origin === globalThis.location?.origin;
      if ((!loopback && !hosted) || url.username || url.password) {
        throw new Error("voice_core_origin_required");
      }
      url.protocol = hosted ? "wss:" : "ws:";
      url.pathname = `${url.pathname.replace(/\/$/, "")}/v1/sessions/${encodeURIComponent(this.sessionId)}/voice`;
      url.search = "";
      url.hash = "";
      const profile = Object.entries(endpoint.headers ?? {}).find(([name]) => name.toLowerCase() === "x-profile-token")?.[1];
      const protocols = ["jana.voice.1", `jana.bearer.${endpoint.token}`, ...(profile ? [`jana.profile.${profile}`] : [])];
      const socket = this.createSocket(url.toString(), protocols);
      this.socket = socket;
      socket.binaryType = "arraybuffer";
      await new Promise<void>((resolve, reject) => {
        this.cancelOpen = resolve;
        socket.onopen = () => {
          if (generation !== this.generation) { socket.close(); resolve(); return; }
          this.cancelOpen = null;
          this.update({ connected: true, reason: null, state: "idle" });
          if (this.placed !== null) this.send({ type: "place", ...this.placed });
          if (!this.calm) this.send({ type: "mode", mode: this.view.mode });
          resolve();
        };
        socket.onclose = () => {
          if (generation !== this.generation) return;
          this.audio.close();
          this.socket = null;
          this.update({ connected: false, state: "unavailable", reason: "voice.disconnected" });
          reject(new Error("voice_disconnected"));
        };
        socket.onerror = () => {
          if (generation !== this.generation) return;
          this.audio.close();
          this.update({ connected: false, state: "unavailable", reason: "voice.unavailable" });
          reject(new Error("voice_unavailable"));
        };
        socket.onmessage = ({ data }) => {
          this.incoming = this.incoming.then(async () => {
            if (generation !== this.generation) return;
            if (data instanceof ArrayBuffer && data.byteLength >= 18 && data.byteLength <= 240_016 && data.byteLength % 2 === 0) {
              const tag = hex(new Uint8Array(data, 0, 16));
              if (tag !== this.readTag) { this.tutorTag = tag; this.startTicker(); }
              const pcm = new Int16Array(data.slice(16));
              this.follow(tag, pcm);
              this.audio.play(tag, pcm, (playedMs, ended) => {
                if (generation === this.generation) {
                  if (ended && tag === this.loudTag) this.stopMouth();
                  // A chunk finished playing: actual progress even where the device offers no finer clock. An
                  // empty queue may only be a gap before more audio, so it fires nothing beyond what has played.
                  if (tag === this.tutorTag) this.cues.progress(playedMs, Date.now());
                  this.update({ playedMs });
                  this.send({ type: "playback", ...(tag === this.readTag ? { utterance_id: tag } : { response_id: tag }), played_ms: playedMs, ended });
                }
              });
            } else if (typeof data === "string") {
              const control = JSON.parse(data) as Record<string, unknown>;
              const identity = control.utterance_id ?? control.response_id;
              if (control.type === "flush" && typeof identity === "string") {
                const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity));
                const flushed = hex(new Uint8Array(digest, 0, 16));
                if (generation === this.generation) {
                  this.audio.flush(flushed);
                  if (flushed === this.loudTag) this.stopMouth();
                  if (flushed === this.tutorTag) this.stopCues(); // barge-in, cancel or stop: no stale pointer (UXS-63)
                }
              }
            }
          }).catch(() => {
            if (generation === this.generation) {
              this.close(); this.update({ state: "unavailable", reason: "voice.protocol_error" });
            }
          });
        };
      });
    })().catch(() => {
      if (generation === this.generation) {
        this.audio.close(); this.socket?.close(); this.socket = null;
        this.update({ state: "unavailable", reason: "voice.unavailable" });
      }
      throw new Error("voice_unavailable");
    }).finally(() => { if (generation === this.generation) this.opening = null; });
    return this.opening;
  }

  /** Whether audio may flow now: a held control or hands-free while the app has focus, never in a calm hold. Nothing
   * listens before the student starts voice (no wake word, VOC-49, D126). */
  private capturing(): boolean {
    return !this.calm && (this.held || (this.view.mode === "hands_free" && !this.unfocused));
  }
  private async capture(): Promise<void> {
    const generation = this.generation;
    try {
      const settings = await this.audio.capture((pcm) => {
        if (generation !== this.generation || !this.capturing()) return;
        if (pcm.byteLength > 4800 || pcm.byteLength % 2 !== 0 || (this.socket?.bufferedAmount ?? 0) > 48_000) {
          this.close();
          this.update({ state: "unavailable", reason: "voice.audio_backpressure" });
        } else if (this.socket?.readyState === 1) {
          this.socket.send(pcm);
          this.hearMicrophone(pcm);
        }
      });
      if (generation !== this.generation || !this.capturing()) { this.audio.releaseMic(); return; }
      this.send({ type: "capture", echo_cancellation: settings.echoCancellation, headphones: settings.headphones });
    } catch (error) {
      if (generation === this.generation && this.capturing()) {
        const held = this.held;
        this.held = false;
        if (this.dictationId) {
          this.send({ type: "dictate", capture_id: this.dictationId, pressed: false });
          this.send({ type: "stop" });
          this.dictationId = null;
        } else if (held) this.send({ type: "ptt", pressed: false });
        else {
          // Hands-free cannot listen without a microphone: back to push-to-talk so the core stops expecting audio.
          this.update({ mode: "push_to_talk" });
          this.send({ type: "mode", mode: "push_to_talk" });
        }
        this.audio.releaseMic();
        this.update({ state: "unavailable", reason: microphoneReason(error) });
      }
    }
  }
  /** A new press after a microphone error (VOC-16) starts afresh: the error copy goes and capture is tried again. */
  private retryAfterMicrophoneError(): void {
    if (this.view.state === "unavailable" && MICROPHONE_REASONS.includes(this.view.reason ?? "")) {
      this.update({ state: "idle", reason: null });
    }
  }
  /** Read the audio clock while a tutor response plays, so cues fire on what is heard, within the VOC-51 budget. */
  private startTicker(): void {
    if (this.ticker !== null) return;
    this.ticker = setInterval(() => {
      const now = Date.now();
      const heard = this.tutorTag === null ? null : (this.audio.position?.(this.tutorTag) ?? null);
      if (heard !== null) this.cues.progress(heard, now);
      this.cues.tick(now);
      // Nothing left to fire and no pointer to clear: stop reading the clock until more audio arrives.
      if (this.cues.pending() === 0 && this.cues.getState().pointer === null) this.stopTicker();
    }, CLIENT_KEYS.cue_tick_ms);
  }
  private stopTicker(): void {
    if (this.ticker !== null) clearInterval(this.ticker);
    this.ticker = null;
  }
  /** Follow the loudness of a response's audio from its first frame; a new response starts a new track. */
  private follow(tag: string, pcm: Int16Array): void {
    if (tag !== this.loudTag) {
      this.stopMouth();
      this.loudTag = tag;
    }
    this.loudness.append(pcm);
    if (this.mouthTimer !== null) return;
    let last = Date.now();
    this.mouthTimer = setInterval(() => {
      const now = Date.now();
      const heard = this.loudTag === null ? null : (this.audio.position?.(this.loudTag) ?? this.view.playedMs);
      const shape = this.mouthShape.update(heard === null ? 0 : this.loudness.at(heard), now - last);
      last = now;
      if (shape !== (this.view.mouth ?? "closed")) this.update({ mouth: shape });
    }, FRAME_MS);
  }
  /** The audio ended, was flushed or the session closed: the mouth rests. */
  private stopMouth(): void {
    if (this.mouthTimer !== null) clearInterval(this.mouthTimer);
    this.mouthTimer = null;
    this.loudTag = null;
    this.loudness.clear();
    this.mouthShape.reset();
    if (this.view.mouth !== undefined && this.view.mouth !== "closed") this.update({ mouth: "closed" });
  }
  /** The microphone's level for the listening ring, updated only when it moves visibly. */
  private hearMicrophone(pcm: ArrayBuffer): void {
    const levels = frameLevels(new Int16Array(pcm), 16000);
    const level = levels.length === 0 ? 0 : Math.max(...levels);
    if (Math.abs(level - (this.view.micLevel ?? 0)) >= 0.05) this.update({ micLevel: Math.round(level * 100) / 100 });
  }
  /** Pending cues are dropped and the pointer clears at once (VOC-52, UXS-63). */
  private stopCues(): void {
    this.cues.stop();
    this.tutorTag = null;
    this.stopTicker();
  }
  async ptt(pressed: boolean): Promise<void> {
    if (pressed) this.stopCues(); // speaking over the tutor stops its audio and its cues
    if (this.dictationId !== null || (pressed && this.calm)) return;
    if (pressed && this.readId !== null) this.stopRead(this.readId);
    this.readId = null;
    this.readTag = null;
    this.held = pressed;
    if (!pressed) {
      this.send({ type: "ptt", pressed: false });
      this.audio.releaseMic();
      return;
    }
    this.unfocused = false;
    this.audio.activate?.();
    await this.open();
    if (!this.held || this.calm) return;
    this.retryAfterMicrophoneError();
    this.send({ type: "ptt", pressed: true });
    await this.capture();
  }
  async mode(mode: TalkMode): Promise<void> {
    if (this.calm) return;
    this.unfocused = false;
    this.audio.activate?.();
    await this.open();
    if (this.socket?.readyState !== 1 || this.calm) return;
    this.retryAfterMicrophoneError();
    if (this.readId !== null) this.stopRead(this.readId);
    this.readId = this.readTag = null;
    this.held = false;
    this.update({ mode });
    this.send({ type: "mode", mode });
    if (mode === "hands_free") await this.capture();
    else this.audio.releaseMic();
  }
  /** VOC-49, D126 (as in Codex): the start button or Ctrl+Shift+V starts a hands-free conversation, and again ends it,
   * closing the microphone. */
  async conversation(): Promise<void> {
    if (this.socket?.readyState === 1 || this.opening !== null) { this.close(); return; }
    if (this.calm) return;
    await this.mode("hands_free");
  }
  think(pressed: boolean): void {
    if (pressed && this.calm) return;
    this.thinkHeld = pressed;
    this.send({ type: "think", pressed });
  }
  setRate(rate: number): void { this.audio.setRate?.(Math.min(1.5, Math.max(0.75, rate))); }
  activate(): void { this.audio.activate?.(); }
  async speak(utteranceId: string): Promise<void> {
    this.audio.activate?.();
    await this.open();
    this.held = false;
    this.dictationId = null;
    this.audio.releaseMic();
    this.update({ mode: "push_to_talk", playedMs: 0 });
    if (!this.calm) this.send({ type: "mode", mode: "push_to_talk" });
    const generation = this.generation;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(utteranceId));
    if (generation !== this.generation) return;
    this.readId = utteranceId;
    this.readTag = hex(new Uint8Array(digest, 0, 16));
    this.send({ type: "speak", utterance_id: utteranceId });
  }
  stopRead(utteranceId: string): void {
    if (this.readId === utteranceId && this.readTag) this.audio.flush(this.readTag);
    this.send({ type: "stop", utterance_id: utteranceId });
  }
  async dictate(captureId: string, pressed: boolean): Promise<void> {
    if (!pressed) {
      if (this.dictationId !== captureId) return;
      // Focus loss may already have released the control (VOC-15); the core hears the release once.
      if (this.held) this.send({ type: "dictate", capture_id: captureId, pressed: false });
      this.held = false;
      this.audio.releaseMic();
      return;
    }
    if (this.calm) return;
    this.unfocused = false;
    this.audio.activate?.();
    await this.open();
    if (this.calm) return;
    this.retryAfterMicrophoneError();
    this.audio.releaseMic();
    this.readId = null;
    this.dictationId = captureId;
    this.dictationListening = false;
    this.held = true;
    this.update({ mode: "push_to_talk", student: "", tentative: "" });
    this.send({ type: "mode", mode: "push_to_talk" });
    this.send({ type: "dictate", capture_id: captureId, pressed: true });
    await this.capture();
  }
  stop(): void {
    if (this.dictationId !== null) { this.held = false; this.dictationId = null; this.audio.releaseMic(); }
    this.audio.flush(); this.stopCues(); this.send({ type: "stop" });
  }
  /** VOC-15: the app lost focus (window blur or a hidden document). Release the microphone in every mode and tell
   * the core every held control is up; hands-free stays the chosen mode and resumes on focusGained(). */
  focusLost(): void {
    this.unfocused = true;
    if (this.dictationId !== null && this.held) this.send({ type: "dictate", capture_id: this.dictationId, pressed: false });
    else if (this.held) this.send({ type: "ptt", pressed: false });
    if (this.thinkHeld) this.send({ type: "think", pressed: false });
    if (this.view.mode === "hands_free" && !this.calm) this.send({ type: "mode", mode: "push_to_talk" });
    this.held = this.thinkHeld = false;
    this.audio.releaseMic();
  }
  /** The app has focus again: hands-free listens again; push-to-talk and dictation wait for the next press. */
  async focusGained(): Promise<void> {
    if (!this.unfocused) return;
    this.unfocused = false;
    if (this.calm || this.view.mode !== "hands_free" || this.socket?.readyState !== 1) return;
    this.send({ type: "mode", mode: "hands_free" });
    await this.capture();
  }
  /** VOC-14 "Turn voice on again": a fresh connection after a give-up or failure. */
  restart(): Promise<void> {
    this.close();
    return this.open();
  }
  /** VOC-20, PRV-14: the calm screen is up. The mic closes and voice rests idle; nothing reopens capture or sends a
   * control that would end the core's calm hold until resumeAfterCalm(). Tutor audio already queued (the keyed
   * spoken form of the distress template) is left for the core to flush; read-aloud stops. */
  private holdForCalm(): void {
    this.calm = true;
    if (this.dictationId !== null && this.held) this.send({ type: "dictate", capture_id: this.dictationId, pressed: false });
    if (this.thinkHeld) this.send({ type: "think", pressed: false });
    if (this.readId !== null && this.readTag) this.audio.flush(this.readTag);
    this.held = this.thinkHeld = this.dictationListening = false;
    this.dictationId = this.readId = this.readTag = null;
    this.audio.releaseMic();
    this.update({ state: "idle", reason: null, tentative: "" });
  }
  /** "Back to my lesson when I'm ready" (UXS-43): re-send the talk mode so the core leaves its calm hold. */
  async resumeAfterCalm(): Promise<void> {
    if (!this.calm) return;
    this.calm = false;
    if (this.socket?.readyState !== 1) return;
    this.send({ type: "mode", mode: this.view.mode });
    if (this.view.mode === "hands_free" && !this.unfocused) await this.capture();
  }
  event(event: Record<string, unknown>): void {
    if (event.type === "notice" && event.code === "calm_screen") { this.holdForCalm(); this.stopCues(); return; }
    // Board changes clear a pointer on a revised or removed target whatever the socket's state (UXS-63).
    if (event.type === "board.step" && typeof event.step_id === "string") this.cues.boardStep(event.step_id, String(event.latex ?? ""));
    if (event.type === "board.reset") this.cues.targetsRemoved((target) => target.kind === "step");
    if (this.socket?.readyState !== 1) return;
    if (event.type === "voice.state" && event.state === "interrupted") this.stopCues();
    if (event.type === "voice.state" && ["idle", "listening", "thinking", "speaking", "interrupted", "unavailable"].includes(String(event.state))) {
      if (event.state === "unavailable") {
        // A core-side error ends every held capture too; stale audio must not continue while typing recovers.
        if (this.dictationId && this.held) this.send({ type: "dictate", capture_id: this.dictationId, pressed: false });
        else if (this.held) this.send({ type: "ptt", pressed: false });
        if (this.thinkHeld) this.send({ type: "think", pressed: false });
        if (this.view.mode === "hands_free") this.send({ type: "mode", mode: "push_to_talk" });
        this.held = this.thinkHeld = false;
        this.dictationId = null; this.dictationListening = false;
        this.audio.releaseMic(); this.stopCues();
        this.update({ mode: "push_to_talk" });
      }
      if (this.dictationId && event.state === "listening") this.dictationListening = true;
      if (this.dictationId && this.dictationListening && event.state === "idle") {
        this.dictationId = null; this.dictationListening = false;
      }
      this.update({ state: event.state as VoiceState, reason: typeof event.reason_key === "string" ? event.reason_key : null });
    } else if (event.type === "voice.caption") {
      if (event.speaker === "student") this.update({ student: String(event.committed_text ?? ""), tentative: String(event.tentative_text ?? "") });
      else if (event.speaker === "tutor") {
        const words = Array.isArray(event.words) ? event.words as CaptionWord[] : [];
        // A tutor turn's cues (not read-aloud): the sidecar gives no word alignment, so their word times are the
        // synchroniser's estimate and are labelled so (VOC-51); they still fire only on heard playback.
        if (typeof event.turn_id === "string" && event.item_id === null && Array.isArray(event.cues)) {
          this.cues.caption(event.turn_id, String(event.committed_text ?? ""), words, event.cues as CaptionCue[], "estimated");
        }
        this.update({ tutor: String(event.committed_text ?? ""),
          captionItem: typeof event.item_id === "string" ? event.item_id : null, words });
      }
    }
  }
  close(): void {
    this.audio.flush();
    this.stopCues(); // a session change leaves no pointer behind (UXS-63)
    this.stopMouth();
    this.send({ type: "stop" });
    ++this.generation;
    this.cancelOpen?.();
    this.cancelOpen = null;
    this.held = this.thinkHeld = false;
    this.dictationId = this.readId = null;
    this.readTag = null;
    this.dictationListening = false;
    this.opening = null;
    this.socket?.close();
    this.socket = null;
    this.audio.close();
    this.update(EMPTY);
  }
}
