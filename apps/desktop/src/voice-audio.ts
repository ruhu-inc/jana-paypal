import type { VoiceAudio } from "@jana/client-core";
import captureModule from "./voice-capture-worklet.js?url&no-inline";

interface Playback {
  start: number;
  until: number;
  samples: number;
  completedSamples: number;
  nodes: Map<AudioBufferSourceNode, { start: number; samples: number; rate: number }>;
  /** Every frame scheduled for this tag, played or not (one record per received frame), for the heard position. */
  scheduled: { start: number; samples: number; rate: number }[];
  report(playedMs: number, ended: boolean): void;
  flushed: boolean;
}

/** Browser device adapter. No recorder, file, IndexedDB, analytics, or external network path exists here. */
export class BrowserVoiceAudio implements VoiceAudio {
  private rate = 1;
  setRate(rate: number): void { this.rate = Math.min(1.5, Math.max(0.75, rate)); }
  private output: AudioContext | null = null;
  private input: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private worklet: AudioWorkletNode | null = null;
  private pending: Promise<{ echoCancellation: boolean; headphones: boolean }> | null = null;
  private microphoneGeneration = 0;
  private readonly playing = new Map<string, Playback>();

  activate(): void {
    this.output ??= new AudioContext({ sampleRate: 24000 });
    void this.output.resume().catch(() => undefined);
  }
  capture(onPcm: (pcm: ArrayBuffer) => void): Promise<{ echoCancellation: boolean; headphones: boolean }> {
    if (this.pending !== null) return this.pending;
    if (this.stream !== null) return Promise.resolve(this.captureSettings());
    const generation = ++this.microphoneGeneration;
    this.pending = (async () => {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true,
        noiseSuppression: true, autoGainControl: true }, video: false });
      if (generation !== this.microphoneGeneration) {
        for (const track of stream.getTracks()) track.stop();
        return { echoCancellation: false, headphones: false };
      }
      this.stream = stream;
      const context = new AudioContext();
      this.input = context;
      await context.audioWorklet.addModule(captureModule);
      if (generation !== this.microphoneGeneration) { await context.close(); return { echoCancellation: false, headphones: false }; }
      const node = new AudioWorkletNode(context, "jana-voice-capture", { numberOfInputs: 1, numberOfOutputs: 1,
        outputChannelCount: [1] });
      this.worklet = node;
      node.port.onmessage = ({ data }: MessageEvent<unknown>) => {
        if (generation === this.microphoneGeneration && data instanceof ArrayBuffer) onPcm(data);
      };
      context.createMediaStreamSource(stream).connect(node);
      node.connect(context.destination); // the processor writes only silence to this output
      await context.resume();
      return this.captureSettings();
    })().catch((error: unknown) => {
      if (generation === this.microphoneGeneration) this.releaseMic();
      // Keep the DOMException name: the controller maps it to the VOC-16 copy (no device, access denied, other).
      const name = typeof error === "object" && error !== null && "name" in error ? String(error.name) : "";
      throw Object.assign(new Error("voice_microphone_unavailable"), name ? { name } : {});
    }).finally(() => { if (generation === this.microphoneGeneration) this.pending = null; });
    return this.pending;
  }
  private captureSettings(): { echoCancellation: boolean; headphones: boolean } {
    return { echoCancellation: this.stream?.getAudioTracks()[0]?.getSettings().echoCancellation === true, headphones: false };
  }
  releaseMic(): void {
    ++this.microphoneGeneration;
    this.pending = null;
    if (this.worklet !== null) { this.worklet.port.onmessage = null; this.worklet.disconnect(); this.worklet.port.close(); }
    this.worklet = null;
    for (const track of this.stream?.getTracks() ?? []) track.stop();
    this.stream = null;
    if (this.input !== null) void this.input.close().catch(() => undefined);
    this.input = null;
  }
  play(tag: string, pcm: Int16Array, report: (playedMs: number, ended: boolean) => void): void {
    if (pcm.length === 0) return;
    this.activate();
    const context = this.output;
    if (context === null) throw new Error("voice_playback_unavailable");
    if (!this.playing.has(tag)) {
      for (const [key, previous] of this.playing) if (previous.nodes.size === 0) this.playing.delete(key);
    }
    const queue = this.playing.get(tag) ?? { start: context.currentTime + 0.02, until: context.currentTime + 0.02,
      samples: 0, completedSamples: 0, nodes: new Map<AudioBufferSourceNode, { start: number; samples: number; rate: number }>(),
      scheduled: [], report, flushed: false };
    if (queue.until - context.currentTime > 30) throw new Error("voice_playback_queue_limit");
    const buffer = context.createBuffer(1, pcm.length, 24000);
    const values = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) values[i] = (pcm[i] ?? 0) / 32768;
    const node = context.createBufferSource();
    node.buffer = buffer;
    node.playbackRate.value = this.rate;
    node.connect(context.destination);
    this.playing.set(tag, queue);
    const start = Math.max(context.currentTime + 0.01, queue.until);
    queue.nodes.set(node, { start, samples: pcm.length, rate: this.rate });
    queue.scheduled.push({ start, samples: pcm.length, rate: this.rate });
    queue.until = start + buffer.duration / this.rate;
    queue.samples += pcm.length;
    node.onended = () => {
      queue.nodes.delete(node);
      node.disconnect();
      if (queue.flushed) return;
      queue.completedSamples += pcm.length;
      const ended = queue.nodes.size === 0;
      // While more is queued, the report is what has been heard (less the output latency, VOC-52); the last one says
      // the whole response played, which drains it (VOC-04).
      const played = Math.floor(queue.completedSamples / 24);
      queue.report(ended ? played : Math.min(played, this.position(tag) ?? played), ended);
    };
    node.start(start);
  }
  /** What the student has heard of this tag's audio (VOC-51, VOC-52): frames the device has played, less the output
   * device's reported latency (`outputLatency`, else `baseLatency`), in source-audio milliseconds whatever the speech
   * rate; null before the first frame is audible. Read on a short timer while cues are pending, never stored. */
  position(tag: string): number | null {
    const queue = this.playing.get(tag);
    const context = this.output;
    if (queue === undefined || context === null || queue.flushed) return null;
    const latency = (context as AudioContext & { outputLatency?: number }).outputLatency ?? context.baseLatency ?? 0;
    const audible = context.currentTime - (Number.isFinite(latency) ? Math.max(0, latency) : 0);
    if (audible < queue.start) return null;
    let samples = 0;
    for (const chunk of queue.scheduled) {
      samples += Math.min(chunk.samples, Math.max(0, Math.floor((audible - chunk.start) * 24000 * chunk.rate)));
    }
    return Math.floor(samples / 24);
  }
  flush(tag?: string): void {
    for (const [key, queue] of this.playing) {
      if (tag !== undefined && key !== tag) continue;
      // The heard extent at the stop (VOC-52): what the speaker had output, less its latency, never what was queued.
      const played = this.position(key) ?? 0;
      queue.flushed = true;
      queue.report(played, false);
      for (const node of queue.nodes.keys()) { node.onended = null; node.stop(); node.disconnect(); }
      queue.nodes.clear();
      this.playing.delete(key);
    }
  }
  close(): void {
    this.releaseMic();
    this.flush();
    if (this.output !== null) void this.output.close().catch(() => undefined);
    this.output = null;
  }
}
