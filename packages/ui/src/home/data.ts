// What Home and the sidebar read about the open profile's own work (UXS-01, UXS-12): its recent sessions and today's
// suggested practice from the subject maps' KC states. Both are dropped the moment the profile closes or is switched
// (SEC-19), and a reply that lands after that is ignored.
import { useEffect, useState } from "react";
import { duePractice, readSessions, readSubjectMap, recentSessions, refreshesDue, type DueIdea, type ProfileController, type RecentSession, type SubjectMap } from "@jana/client-core";

/** The profile's recent sessions, newest first; read again whenever `version` changes (a turn ended, a title came). */
export function useRecentSessions(profiles: ProfileController, active: string | null, version: string): RecentSession[] {
  const [sessions, setSessions] = useState<RecentSession[]>([]);
  useEffect(() => profiles.onClear(() => setSessions([])), [profiles]);
  useEffect(() => {
    if (active === null) {
      setSessions([]);
      return;
    }
    let live = true;
    const generation = profiles.generation;
    void readSessions(profiles).then((list) => {
      if (live && generation === profiles.generation && list !== null) setSessions(recentSessions(list));
    });
    return () => {
      live = false;
    };
  }, [profiles, active, version]);
  return sessions;
}

/** Today's practice: the most pressing ideas over the installed subjects' maps, and how many refreshes are due. */
export function useDuePractice(profiles: ProfileController, active: string | null, subjectIds: readonly string[]): { ideas: DueIdea[]; refreshes: number } {
  const [due, setDue] = useState<{ ideas: DueIdea[]; refreshes: number }>(NONE);
  const key = subjectIds.join(",");
  useEffect(() => profiles.onClear(() => setDue(NONE)), [profiles]);
  useEffect(() => {
    if (active === null || key === "") {
      setDue(NONE);
      return;
    }
    let live = true;
    const generation = profiles.generation;
    void Promise.all(key.split(",").map((id) => profiles.commands.get<unknown>(`/v1/curriculum/${encodeURIComponent(id)}/map`).then(readSubjectMap).catch(() => null)))
      .then((maps) => {
        if (!live || generation !== profiles.generation) return;
        const read = maps.filter((m): m is SubjectMap => m !== null);
        setDue({ ideas: duePractice(read), refreshes: refreshesDue(read) });
      });
    return () => {
      live = false;
    };
  }, [profiles, active, key]);
  return due;
}

const NONE = { ideas: [] as DueIdea[], refreshes: 0 };
