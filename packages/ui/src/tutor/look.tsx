// The open profile's tutor, as everything below the app's root reads it (PRM-03, D103, D105): one tutor per profile,
// with the character and the name the student chose in Settings → Tutor (`tutor_avatar`, `tutor_name`). Its turns are
// labelled with its name, Jana by default, never "Tutor", and marked with its character.
import { createContext, useContext, type ReactNode } from "react";
import type { Settings as SettingsRecord } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { CHARACTER_IDS, DEFAULT_CHARACTER, characterOf, slugOf, type CharacterId } from "./characters.js";

/** The characters the student can give the tutor: the six of TABLE DSN-26a. */
export const TUTOR_AVATARS = CHARACTER_IDS;
export const DEFAULT_TUTOR_AVATAR: CharacterId = DEFAULT_CHARACTER;

/** The tutor's name as the profile's settings hold it, or null when the core does not keep it (before API 1.7). */
export function tutorNameOf(settings: SettingsRecord | null): string | null {
  const name = settings?.tutor_name;
  return typeof name === "string" && name.trim() !== "" ? name : null;
}

/** The tutor's character as the profile's settings hold it; a value outside the set reads as Jana (G119, D103). */
export function tutorAvatarOf(settings: SettingsRecord | null): CharacterId {
  return characterOf(settings?.tutor_avatar);
}

/** A character's default name (TABLE DSN-26a), from the catalogue. */
export function useCharacterName(): (id: CharacterId) => string {
  const t = useT();
  return (id) => t(`tutor.character.${slugOf(id)}.name`);
}

interface Look {
  avatar: CharacterId;
  name: string | null;
}

const TutorLook = createContext<Look>({ avatar: DEFAULT_CHARACTER, name: null });

/** The open profile's tutor for everything below it: its character and its name (null: the character's own name). */
export function TutorLookProvider({ avatar, name = null, children }: { avatar: CharacterId | string; name?: string | null; children: ReactNode }) {
  return <TutorLook.Provider value={{ avatar: characterOf(avatar), name }}>{children}</TutorLook.Provider>;
}

export function useTutorAvatar(): CharacterId {
  return useContext(TutorLook).avatar;
}

/** The name the tutor's turns carry: the student's choice, else the character's default name (Jana by default). */
export function useTutorName(): string {
  const look = useContext(TutorLook);
  const nameOf = useCharacterName();
  return look.name ?? nameOf(look.avatar);
}
