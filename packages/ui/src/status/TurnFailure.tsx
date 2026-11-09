// UXS-20 / AGT-27: partial work remains visible; only the last failed turn of an idle session offers retry.
import type { TurnRec } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, Notice } from "../primitives/index.js";
import { useSelect, useSession } from "../store.js";

export function TurnFailure({ turn }: { turn: TurnRec }) {
  const t = useT(), session = useSession();
  const retry = useSelect((s) => {
    const ids = s.sessions[turn.session_id]?.turn_ids ?? [];
    return ids.at(-1) === turn.turn_id && !ids.some((id) => s.turns[id]?.status === "streaming");
  });
  const saved = useSelect((s) => {
    const ids = s.sessions[turn.session_id]?.board_ids ?? [];
    const board = ids.at(-1);
    const steps = board ? Object.values(s.boards[board]?.steps ?? {}) : [];
    return Math.max(0, ...steps.filter((step) => !["draft", "awaiting", "checking"].includes(step.status)).map((step) => step.index + 1));
  });
  if (turn.status !== "error") return null;
  return <div data-turn-failure="" className="flex flex-col items-start gap-2">
    <Notice message={t(saved ? "tutor.crashed_saved" : "tutor.crashed_question", { step: saved })} />
    {retry && turn.error?.retryable !== false && <Button disabled={session === null} onClick={() => void session?.retryTurn(turn.turn_id)}>{t("action.try_again")}</Button>}
  </div>;
}
