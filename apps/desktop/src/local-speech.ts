import type { LocalSpeech } from "@jana/client-core";

type Voice = Pick<SpeechSynthesisVoice, "voiceURI" | "name" | "lang" | "localService" | "default">;

/** How natural a system voice sounds, from the name the OS gives it: macOS marks its downloaded voices "(Premium)" and
 * "(Enhanced)", Windows its "Natural" voices, other systems "Neural"; anything else is a compact voice. */
export function voiceQuality(voice: Pick<Voice, "name">): number {
  if (/\(premium\)/i.test(voice.name)) return 3;
  if (/\(enhanced\)|\bnatural\b|\bneural\b/i.test(voice.name)) return 2;
  return 0;
}

/** The installed English voices, best first: the most natural ones, then the voice the computer's own settings chose
 * (`default`), then the rest in the order the OS lists them. A network voice is never among them (no text may leave
 * the computer this way), whatever its quality. */
export function rankVoices<V extends Voice>(voices: readonly V[]): V[] {
  const local = voices.filter((v) => v.localService && /^en(?:-|$)/i.test(v.lang));
  const score = (v: V) => voiceQuality(v) * 2 + (v.default ? 1 : 0);
  return local.map((v, i) => ({ v, i })).sort((a, b) => score(b.v) - score(a.v) || a.i - b.i).map(({ v }) => v);
}

/** Whether every installed English voice is a compact one (Settings → Voice then says how to add a better one). */
export function onlyCompactVoices(voices: readonly Voice[]): boolean {
  const ranked = rankVoices(voices);
  return ranked.length > 0 && ranked.every((v) => voiceQuality(v) === 0);
}

/** Never allow a default/network voice: only an installed English voice may receive accepted verbaliser text. The voice
 * the student chose in Settings → Voice is used while it is installed; otherwise the best installed one (`rankVoices`). */
export class BrowserLocalSpeech implements LocalSpeech {
  constructor(private readonly synthesis: SpeechSynthesis | undefined = globalThis.speechSynthesis,
    private readonly createUtterance: (text: string) => SpeechSynthesisUtterance = (text) => new SpeechSynthesisUtterance(text)) {}

  private rate = 1;
  private selected = "";
  configure(rate: number, voice: string): void { this.rate = Math.min(1.5, Math.max(0.75, rate)); this.selected = voice; }
  voices(): { id: string; name: string }[] {
    return rankVoices(this.synthesis?.getVoices() ?? []).map((v) => ({ id: v.voiceURI, name: `${v.name} (${v.lang})` }));
  }
  /** Every installed English voice is a compact one. */
  onlyCompact(): boolean { return onlyCompactVoices(this.synthesis?.getVoices() ?? []); }
  private voice(): SpeechSynthesisVoice | undefined {
    const voices = rankVoices(this.synthesis?.getVoices() ?? []);
    return voices.find((v) => v.voiceURI === this.selected) ?? voices[0];
  }
  available(): boolean { return this.voice() !== undefined; }
  subscribe(listener: () => void): () => void {
    this.synthesis?.addEventListener("voiceschanged", listener);
    return () => this.synthesis?.removeEventListener("voiceschanged", listener);
  }
  speak(text: string, callbacks: Parameters<LocalSpeech["speak"]>[1]): () => void {
    const voice = this.voice(), synthesis = this.synthesis;
    if (!voice || !synthesis) throw new Error("local_voice_unavailable");
    const utterance = this.createUtterance(text);
    utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = this.rate;
    const rate = this.rate;
    let live = true, start = 0, timer: ReturnType<typeof setInterval> | null = null, sawBoundary = false;
    let lastPosition = 0;
    const finish = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
      utterance.onstart = utterance.onboundary = utterance.onend = utterance.onerror = null;
      live = false;
    };
    utterance.onstart = () => {
      if (!live) return;
      start = performance.now(); callbacks.start();
      // Engines without boundary callbacks use the same conservative 3.83 syllables/second fallback.
      const words = [...text.matchAll(/\S+/g)];
      timer = setInterval(() => {
        if (!live || sawBoundary || synthesis.paused) return;
        const elapsed = performance.now() - start;
        let syllables = 0, position = 0;
        for (const word of words) {
          if (syllables * 1000 / (3.83 * rate) > elapsed) break;
          position = word.index;
          syllables += Math.max(1, (word[0].match(/[aeiouy]+/gi) ?? []).length);
        }
        if (position > lastPosition) {
          lastPosition = position; callbacks.boundary(position, Math.round(elapsed));
        }
      }, 100);
    };
    utterance.onboundary = (event) => {
      if (!live || event.name !== "word" || event.charIndex < lastPosition || event.charIndex > text.length) return;
      sawBoundary = true; lastPosition = event.charIndex;
      callbacks.boundary(event.charIndex, Math.max(0, Math.round(event.elapsedTime * 1000)));
    };
    utterance.onend = () => { if (live) { finish(); callbacks.end(); } };
    utterance.onerror = () => { if (live) { finish(); callbacks.error(); } };
    synthesis.cancel();
    synthesis.speak(utterance);
    return () => { if (live) { finish(); synthesis.cancel(); } };
  }
}
