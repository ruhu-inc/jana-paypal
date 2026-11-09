// The oldest open request owns both its inline card and the single dock.
import { useLayoutEffect, useRef, type ReactNode } from "react";
import type { Card } from "@jana/contracts-ts";
import { useSelect } from "../store.js";
import { useT } from "../i18n.js";
export function useDockRequest(sessionId: string) {
  return useSelect((s) => {
    for (const turn of s.sessions[sessionId]?.turn_ids ?? []) for (const partId of s.turns[turn]?.part_ids ?? []) {
      const part = s.parts[partId];
      if (!part?.card || !["question", "hint"].includes(part.card.card.card_type)) continue;
      const request = s.requests[part.card.request_id];
      if (request && ["open", "answering"].includes(request.state)) return request;
    }
    return null;
  });
}
export function RequestDock({ requestId, kind, children }: { requestId: string; kind: Card["card_type"]; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null); const t = useT();
  useLayoutEffect(() => {
    const node = box.current;
    const key = (event: KeyboardEvent) => {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyD") {
        event.preventDefault(); node?.querySelector<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled)")?.focus();
      }
    };
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("keydown", key);
      if (node?.contains(document.activeElement)) node.closest("[data-region=dock]")?.querySelector<HTMLElement>("[contenteditable=true]")?.focus();
    };
  }, [requestId]);
  return <div ref={box} data-request-dock={requestId} className="w-full max-w-(--layout-columnMax)"><p role="status" className="sr-only">{t(kind === "question" ? "dock.question_waiting" : "dock.hint_waiting")}</p>{children}</div>;
}
