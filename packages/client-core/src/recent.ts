// What the app frame and Home show about the student's own work (UXS-01, UXS-12): the recent sessions of the sidebar,
// newest first and grouped by day, the sessions Home offers to pick up, and today's suggested practice, which comes from
// the learner model's KC states on the subject maps (a refresh that is due first, then ideas nearly secure, then ideas
// being learnt). All of it is read from the core; nothing here is a second learner model.
import type { Session, SessionList } from "@jana/contracts-ts";
import { CLIENT_KEYS } from "./generated/keys.js";
import type { KcState, MapKc, SubjectMap } from "./learn.js";
import type { ProfileController } from "./profiles.js";

export type RecentSession = Pick<Session, "session_id" | "title" | "subject" | "topic_id" | "mode" | "updated_ts">;

/** The day groups of the sidebar's recent sessions, in the order they are shown. */
export const SESSION_GROUPS = ["today", "yesterday", "week", "older"] as const;
export type SessionGroup = (typeof SESSION_GROUPS)[number];

/** Study's companion thread (SPEC §19.3.3) is a session too, but it is not tutoring work: it opens in Study only, so
 * the recent lists and the tutor's own subject never pick it. */
export function companionMode(session: { mode?: string }): boolean {
  return session.mode === "study_companion";
}

/** The profile's open sessions, newest first, at most `max`. The core titles a session from its first question; a
 * session with no title that was never used since it was opened (a blank "New question") is not work to go back to,
 * so it is left out. */
export function recentSessions(list: SessionList | null, max: number = CLIENT_KEYS.ui_recent_sessions_max): RecentSession[] {
  return (list?.sessions ?? [])
    .filter((s) => !s.archived && !companionMode(s) && (s.title.trim() !== "" || s.updated_ts > s.created_ts))
    .sort((a, b) => b.updated_ts - a.updated_ts)
    .slice(0, max)
    .map(({ session_id, title, subject, topic_id, mode, updated_ts }) => ({ session_id, title, subject, topic_id, mode, updated_ts }));
}

/** The session the student last worked on: the open one with the newest `updated_ts` (a turn ending moves it), not
 * the one created last, which is the order `GET /v1/sessions` lists them in. The tutor's subject starts from it. */
export function lastWorked<T extends Pick<Session, "archived" | "updated_ts"> & { mode?: string }>(sessions: readonly T[]): T | null {
  let newest: T | null = null;
  for (const s of sessions) if (!s.archived && !companionMode(s) && (newest === null || s.updated_ts > newest.updated_ts)) newest = s;
  return newest;
}

function dayStart(ms: number): number {
  const day = new Date(ms);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
}

/** Which day group a session last worked on at `updatedTs` falls in, by the computer's local calendar. */
export function sessionGroupOf(updatedTs: number, now: number): SessionGroup {
  const today = dayStart(now);
  const days = Math.round((today - dayStart(updatedTs)) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return days <= 7 ? "week" : "older";
}

/** The sessions in their day groups, keeping their order; empty groups are left out. */
export function groupSessions<T extends Pick<Session, "updated_ts">>(sessions: readonly T[], now: number): { group: SessionGroup; sessions: T[] }[] {
  const groups = new Map<SessionGroup, T[]>();
  for (const session of sessions) {
    const group = sessionGroupOf(session.updated_ts, now);
    groups.set(group, [...(groups.get(group) ?? []), session]);
  }
  return SESSION_GROUPS.filter((g) => groups.has(g)).map((group) => ({ group, sessions: groups.get(group) ?? [] }));
}

/** The KC states today's practice suggests, most pressing first: a refresh that is due, then nearly secure, then learning. */
export const DUE_STATES: readonly KcState[] = ["needs_review", "ready_to_check", "learning"];

export interface DueIdea {
  kc: MapKc;
  subjectId: string;
  subject: SubjectMap["subject"];
  topicId: string;
}

/** Today's suggested practice from the subject maps: the ideas in DUE_STATES, most pressing first, at most `max`. */
export function duePractice(maps: readonly SubjectMap[], max: number = CLIENT_KEYS.ui_home_practice_max): DueIdea[] {
  const ideas: DueIdea[] = [];
  for (const map of maps) {
    if (map.status !== "ready") continue;
    for (const section of map.sections) {
      for (const topic of section.topics) {
        for (const kc of topic.kcs) if (DUE_STATES.includes(kc.state)) ideas.push({ kc, subjectId: map.subject_id, subject: map.subject, topicId: topic.node_id });
      }
    }
  }
  return ideas.sort((a, b) => DUE_STATES.indexOf(a.kc.state) - DUE_STATES.indexOf(b.kc.state)).slice(0, max);
}

/** How many ideas in the maps have a refresh due. */
export function refreshesDue(maps: readonly SubjectMap[]): number {
  return maps.reduce((n, map) => n + map.sections.reduce((m, s) => m + s.topics.reduce((k, t) => k + t.kcs.filter((kc) => kc.state === "needs_review").length, 0), 0), 0);
}

/** The open profile's session list (`GET /v1/sessions`, API-23), or null when it cannot be read. */
export async function readSessions(profiles: ProfileController): Promise<SessionList | null> {
  return profiles.commands.get<SessionList>("/v1/sessions").catch(() => null);
}
