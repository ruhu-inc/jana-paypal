// The tutor characters' registry (DSN-26, D103, D105): the six ids of TABLE DSN-26a, which are the `tutor_avatar` enum
// of the API Settings contract, in the table's order, with Jana the default. A further character is new art in
// art.tsx and a row here, never new code. Poses and mouth shapes are TABLE DSN-26b's expressions on the shared rig.
import type { Settings } from "@jana/contracts-ts";

export type CharacterId = NonNullable<Settings["tutor_avatar"]>;

export const CHARACTER_IDS = ["avatar.jana", "avatar.orbit", "avatar.prism", "avatar.kite", "avatar.pebble", "avatar.ray"] as const satisfies readonly CharacterId[];

/** The tutor everyone starts with (D47): the Jana character. */
export const DEFAULT_CHARACTER: CharacterId = "avatar.jana";

/** The short name of a character: the part after `avatar.`, used for its tokens and its catalogue keys. */
export type CharacterSlug = "jana" | "orbit" | "prism" | "kite" | "pebble" | "ray";

export function slugOf(id: CharacterId): CharacterSlug {
  return id.slice("avatar.".length) as CharacterSlug;
}

export function isCharacterId(value: unknown): value is CharacterId {
  return typeof value === "string" && (CHARACTER_IDS as readonly string[]).includes(value);
}

/** A stored value outside the set (an earlier picture id, an unknown id, nothing) reads as Jana (G119, D103). */
export function characterOf(value: unknown): CharacterId {
  return isCharacterId(value) ? value : DEFAULT_CHARACTER;
}

/** TABLE DSN-26b's moments on the rig; `silhouette` is the shape alone in ink (a placeholder, no face). */
export const CHARACTER_POSES = ["idle", "listening", "thinking", "speaking", "interrupted", "checked", "hmm", "pointing", "silhouette"] as const;
export type CharacterPose = (typeof CHARACTER_POSES)[number];

/** The four mouth shapes of speaking (DSN-27), from closed (the rest shape) to open. */
export const CHARACTER_MOUTHS = ["closed", "small", "medium", "open"] as const;
export type CharacterMouth = (typeof CHARACTER_MOUTHS)[number];

/** The voice states of VOC-04 (the `ev_voice_state` enum) and the pose each shows (UXS-53). */
export type VoiceStateName = "idle" | "listening" | "thinking" | "speaking" | "interrupted" | "unavailable";

export function poseOfVoiceState(state: VoiceStateName): CharacterPose {
  return state === "unavailable" ? "idle" : state;
}

/** DSN-26's art budget per character (`proposal: 12 KB` of SVG), checked on every pose. */
export const CHARACTER_SVG_BUDGET_BYTES = 12 * 1024;
