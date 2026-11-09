// The hint dock (UXS-17, PED-03, PED-07, PED-09): in guided mode, while the tutor's hint card is open, the dock replaces
// the action row above the composer with the card's own actions, "Try it", "Next hint (2 of 5)" and "Show this step
// (counts as not yet mastered)" (Alt+T, Alt+H, Alt+S), and "Show solution", which opens solution mode at any time
// (D87). Each answers the same request as the card in the conversation, so the dock and the card stay in step: once
// either is pressed both wait for the tutor. The dock is never folded.
import { useEffect } from "react";
import type { Card } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { SuggestionChip } from "../primitives/index.js";
import { useSession } from "../store.js";

import { RequestDock, useDockRequest } from "./requests.js";

type HintCard = Extract<Card, { card_type: "hint" }>;

/** The session's newest hint card still waiting for an answer, with its request's state. */
export function useOpenHintRequest(sessionId: string): { requestId: string; card: HintCard; answering: boolean } | null {
  const request = useDockRequest(sessionId);
  return request?.card.card_type === "hint" ? { requestId: request.request_id, card: request.card, answering: request.state === "answering" } : null;
}

const KEYS: Record<string, string> = { KeyT: "try_it", KeyH: "next_hint", KeyS: "show_step" };

export function HintDock({ sessionId, onSolution }: { sessionId: string; onSolution?: () => void }) {
  const t = useT();
  const session = useSession();
  const open = useOpenHintRequest(sessionId);
  const actions = open?.card.actions ?? [];
  const answer = (action: string) => {
    if (open === null || open.answering || session === null) return;
    void session.answerCard(open.requestId, { action });
  };
  useEffect(() => {
    if (open === null) return;
    const onKey = (event: KeyboardEvent) => {
      const action = KEYS[event.code];
      if (!event.altKey || event.ctrlKey || event.metaKey || action === undefined || !actions.includes(action as never)) return;
      event.preventDefault();
      answer(action);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  if (open === null) return null;
  const label = (action: string) => {
    if (action === "next_hint") return t("dock.next_hint", { rung: Math.min(open.card.of, open.card.rung + 1), of: open.card.of });
    if (action === "show_step") return t("dock.show_step");
    return t("dock.try_it");
  };
  const keys: Record<string, string> = { try_it: "Alt+T", next_hint: "Alt+H", show_step: "Alt+S" };
  return (
    <RequestDock requestId={open.requestId} kind="hint"><div role="group" aria-label={t("dock.label")} data-hint-dock={open.requestId} className="flex w-full max-w-(--layout-columnMax) flex-wrap gap-2">
      {actions.map((action) => (
        <SuggestionChip key={action} icon={action === "next_hint" ? "hint" : action === "show_step" ? "go-next" : "stuck"} aria-keyshortcuts={keys[action]}
          disabled={open.answering} onClick={() => answer(action)}>
          {label(action)}
        </SuggestionChip>
      ))}
      {onSolution !== undefined && <SuggestionChip icon="learn" disabled={open.answering} onClick={onSolution}>{t("dock.show_solution")}</SuggestionChip>}
    </div></RequestDock>
  );
}
