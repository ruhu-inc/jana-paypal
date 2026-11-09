// The presence's speaking mouth (DSN-27, D103): one of four shapes picked from the loudness of the audio being played,
// never from phonemes, so it works with any voice, offline and online. Each 20 ms frame of the played audio gives a
// level (0 at -60 dBFS to 1 at full scale); the level is smoothed (a fast attack, a slower release) and a shape changes
// only past its threshold, or back below it by the hysteresis, so the mouth never flickers.
import { CLIENT_KEYS } from "./generated/keys.js";

export type MouthShape = "closed" | "small" | "medium" | "open";

const SHAPES: readonly MouthShape[] = ["closed", "small", "medium", "open"];
/** The tutor's audio: PCM16 mono at 24 kHz (VOC-07), read in 20 ms frames. */
export const PLAYBACK_RATE = 24000;
export const FRAME_MS = 20;
const FLOOR_DB = -60;

/** The level of each 20 ms frame of PCM16 audio, from its RMS in dBFS: 0 at -60 dBFS or below, 1 at full scale. */
export function frameLevels(pcm: Int16Array, rate = PLAYBACK_RATE): number[] {
  const size = Math.max(1, Math.round((rate * FRAME_MS) / 1000));
  const levels: number[] = [];
  for (let start = 0; start < pcm.length; start += size) {
    const end = Math.min(pcm.length, start + size);
    let sum = 0;
    for (let i = start; i < end; i++) sum += (pcm[i] ?? 0) ** 2;
    const rms = Math.sqrt(sum / (end - start)) / 32768;
    const db = rms > 0 ? 20 * Math.log10(rms) : FLOOR_DB;
    levels.push(Math.min(1, Math.max(0, (db - FLOOR_DB) / -FLOOR_DB)));
  }
  return levels;
}

/** The levels of one response's audio in playback order, read at the position the student has heard. */
export class LoudnessTrack {
  private levels: number[] = [];
  append(pcm: Int16Array): void {
    this.levels.push(...frameLevels(pcm));
  }
  at(heardMs: number): number {
    return this.levels[Math.floor(heardMs / FRAME_MS)] ?? 0;
  }
  clear(): void {
    this.levels = [];
  }
}

export interface MouthSettings {
  thresholds: readonly [number, number, number];
  hysteresis: number;
  attackMs: number;
  releaseMs: number;
}

export const MOUTH_SETTINGS: MouthSettings = {
  thresholds: [CLIENT_KEYS.presence_mouth_small_level, CLIENT_KEYS.presence_mouth_medium_level, CLIENT_KEYS.presence_mouth_open_level],
  hysteresis: CLIENT_KEYS.presence_mouth_hysteresis,
  attackMs: CLIENT_KEYS.presence_mouth_attack_ms,
  releaseMs: CLIENT_KEYS.presence_mouth_release_ms,
};

/** The mouth shape over time: `update` takes the level heard now and the milliseconds since the last update. */
export class MouthFollower {
  private envelope = 0;
  private index = 0;
  constructor(private readonly settings: MouthSettings = MOUTH_SETTINGS) {}

  get shape(): MouthShape {
    return SHAPES[this.index] ?? "closed";
  }

  get level(): number {
    return this.envelope;
  }

  update(level: number, elapsedMs: number): MouthShape {
    const tau = level > this.envelope ? this.settings.attackMs : this.settings.releaseMs;
    const step = tau <= 0 ? 1 : 1 - Math.exp(-Math.max(0, elapsedMs) / tau);
    this.envelope += (level - this.envelope) * step;
    const { thresholds, hysteresis } = this.settings;
    // Up a shape when the smoothed level passes its threshold; down when it falls below it by the hysteresis.
    while (this.index < 3 && this.envelope >= (thresholds[this.index] ?? 1)) this.index++;
    while (this.index > 0 && this.envelope < (thresholds[this.index - 1] ?? 0) - hysteresis) this.index--;
    return this.shape;
  }

  reset(): void {
    this.envelope = 0;
    this.index = 0;
  }
}
