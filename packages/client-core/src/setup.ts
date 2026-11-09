// The subjects on this computer and first run's defaults (UXS-10, UXS-22, PLT-17, TABLE PLT-17b). The core lists each
// installed pack with its subject as the pack names it (the curriculum code, `phy`, CUR-02); profiles and sessions name
// subjects in full (`physics`). Nothing here is hard-coded per computer: the subjects come from the installed packs.
import type { CreateProfile, PackList } from "@jana/contracts-ts";

/** Profile subject names and their curriculum codes (CUR-02), in the order subjects are listed. */
export const SUBJECT_CODES = { mathematics: "mth", physics: "phy", chemistry: "chm", biology: "bio", accounting: "acc" } as const;
export type SubjectName = keyof typeof SUBJECT_CODES;
export const SUBJECT_NAMES = Object.keys(SUBJECT_CODES) as SubjectName[];

/** The profile subject name for a pack's or session's subject, given as its code or its name; null when unknown. */
export function subjectName(subject: string): SubjectName | null {
  if (subject in SUBJECT_CODES) return subject as SubjectName;
  return SUBJECT_NAMES.find((name) => SUBJECT_CODES[name] === subject) ?? null;
}

export interface InstalledSubject {
  subject: SubjectName;
  level: "O" | "A";
  /** The curriculum map's subject node id (`cur.phy.o`). */
  subjectId: string;
}

/** The installed subjects, one per subject and level, in the order of SUBJECT_NAMES (O level first). A pack that failed
 * verification is still listed: the map and the tutor say it is switched off (ARC-26). */
export function installedSubjects(packs: PackList["packs"]): InstalledSubject[] {
  const found = new Map<string, InstalledSubject>();
  for (const pack of packs) {
    const subject = subjectName(pack.subject);
    if (subject === null) continue;
    const subjectId = `cur.${SUBJECT_CODES[subject]}.${pack.level.toLowerCase()}`;
    found.set(subjectId, { subject, level: pack.level, subjectId });
  }
  const rank = (s: InstalledSubject) => SUBJECT_NAMES.indexOf(s.subject) * 2 + (s.level === "O" ? 0 : 1);
  return [...found.values()].sort((a, b) => rank(a) - rank(b));
}

/** Adults consent for themselves; academic level remains independent of age. */
export const ADULT_BAND = "18_plus" as const;
export const MINIMUM_AGE = 13;
export type ProfileAgeBand = "13_15" | "16_17" | typeof ADULT_BAND;

/** The boundary for self-managed adult consent. */
export const ADULT_AGE = 18;
/** The oldest age the sign-up field accepts; anything above it is a mistake, not a learner. */
export const MAX_AGE = 120;

/** Whether an age typed as a whole number of years (`"34"`, as a number field gives it) is 18 or more, or null when it
 * is not a whole number from 1 to 120. The age is only checked here: it is never stored or sent. */
export function isAdultAge(age: string): boolean | null {
  if (!/^\d{1,3}$/.test(age)) return null;
  const years = Number(age);
  if (years < 1 || years > MAX_AGE) return null;
  return years >= ADULT_AGE;
}

/** Only this coarse band leaves the form; the entered age is never sent or saved. */
export function profileAgeBand(age: string): ProfileAgeBand | null {
  if (isAdultAge(age) === null) return null;
  const years = Number(age);
  if (years < MINIMUM_AGE) return null;
  return years < 16 ? "13_15" : years < ADULT_AGE ? "16_17" : ADULT_BAND;
}

/** First-run defaults come from installed packs and the chosen course mode, independently of age. */
export function firstRunProfile(name: string, packs: PackList["packs"], order: number, curriculum = true, band: ProfileAgeBand = ADULT_BAND): CreateProfile {
  // No curriculum: no subjects, so the app leads with the learner's own materials and asks nothing about a syllabus.
  if (!curriculum) return { display_name: name.trim(), avatar: `avatar-${(order % 8) + 1}`, level: "O", subjects: [], age_band: band };
  const installed = installedSubjects(packs);
  const level = installed.length === 0 || installed.some((s) => s.level === "O") ? "O" : "A";
  const subjects = [...new Set(installed.filter((s) => s.level === level).map((s) => s.subject))];
  return {
    display_name: name.trim(),
    avatar: `avatar-${(order % 8) + 1}`,
    level,
    subjects: subjects.length > 0 ? subjects : [...SUBJECT_NAMES],
    age_band: band,
  };
}
