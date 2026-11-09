// Presentation follows the core's checkpoint, never a guessed answer or a single checked intermediate line.
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useSelect, useSession } from "../store.js";

type Stage = "none" | "open" | "complete" | "unknown";
const Learning = createContext<Stage>("open");
export function useTutorLearning(): Stage { return useContext(Learning); }
/** Gallery fixtures only: live sessions always use their protected API state. */
export function TutorLearningPreview({ stage, children }: { stage: Stage; children: ReactNode }) { return <Learning.Provider value={stage}>{children}</Learning.Provider>; }
export function TutorLearningProvider({ sessionId, children }: { sessionId: string; children: ReactNode }) {
  const session = useSession();
  const preview = useTutorLearning();
  const last = useSelect(s => s.sessions[sessionId]?.turn_ids.at(-1) ?? null);
  const busy = useSelect(s => !!s.sessions[sessionId]?.pending_ids.length || (last !== null && s.turns[last]?.status === "streaming"));
  const [read, setRead] = useState<{ sessionId: string; turn: string | null; stage: Stage } | null>(null);
  useEffect(() => {
    let closed = false;
    if (!session?.commands || session.sessionId !== sessionId || busy) return;
    void session.commands.get<unknown>(`/v1/sessions/${sessionId}/learning-state`).then(raw => {
      const data = raw as { session_id?: unknown; at_turn?: unknown; problem_status?: unknown } | null;
      if (!closed && data?.session_id === sessionId && data.at_turn === last &&
        ["none", "open", "complete"].includes(String(data.problem_status))) {
        setRead({ sessionId, turn: last, stage: data.problem_status as Stage });
      }
    }).catch(() => undefined);
    return () => { closed = true; };
  }, [session, sessionId, last, busy]);
  // Session-free gallery fixtures cannot send. Live callers fail closed while their checkpoint is unknown.
  const stage = session === null ? preview : !busy && read?.sessionId === sessionId && read.turn === last ? read.stage : "unknown";
  return <Learning.Provider value={stage}>{children}</Learning.Provider>;
}
