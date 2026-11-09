import type { PodcastAudio } from "@jana/client-core";

/** Plays only the native API's validated neural WAV. No microphone, system synthesis or external fetch. */
export class BrowserPodcastAudio implements PodcastAudio {
  private context: AudioContext | null = null;
  available(): boolean { return typeof AudioContext !== "undefined"; }
  activate(): void {
    this.context ??= new AudioContext();
    void this.context.resume().catch(() => undefined);
  }
  pause(): void { void this.context?.suspend().catch(() => undefined); }
  resume(): void { void this.context?.resume().catch(() => undefined); }
  play(audio: Blob, rate: number, callbacks: { end(): void; error(): void }): () => void {
    let live = true, node: AudioBufferSourceNode | null = null;
    void (async () => {
      this.activate();
      const context = this.context;
      if (!context) throw new Error("audio unavailable");
      const buffer = await context.decodeAudioData(await audio.arrayBuffer());
      if (!live) return;
      if (buffer.numberOfChannels !== 1 || buffer.duration <= 0 || buffer.duration > 120) throw new Error("invalid audio");
      node = context.createBufferSource();
      node.buffer = buffer;
      node.playbackRate.value = Math.max(0.75, Math.min(1.5, rate));
      node.connect(context.destination);
      node.onended = () => { if (live) { live = false; node?.disconnect(); callbacks.end(); } };
      node.start();
    })().catch(() => { if (live) { live = false; node?.disconnect(); callbacks.error(); } });
    return () => {
      live = false;
      if (node) { node.onended = null; try { node.stop(); } catch { /* already ended */ } node.disconnect(); }
    };
  }
}
