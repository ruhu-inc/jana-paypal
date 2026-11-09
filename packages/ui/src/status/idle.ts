// LRN-20's idle trigger in the tutor screen (API 1.10, D104): while a step of the session's board is open, the app
// counts the time with no input and the window focused, and after `offer_idle_s` reports it once for that step. The
// core decides whether to offer a hint; the offer arrives as a card like any other.
import { useEffect, useRef } from "react";
import { StepIdleWatch, openStepOf, reportStepIdle, type SessionController } from "@jana/client-core";
import { useSelect } from "../store.js";

const INPUT_EVENTS = ["keydown", "pointerdown", "input", "paste"] as const;

export function useStepIdle(sessionId: string, session: SessionController | null, paused = false): void {
  const step = useSelect((s) => (paused ? null : openStepOf(s, sessionId)));
  const watch = useRef<StepIdleWatch | null>(null);
  useEffect(() => {
    if (session === null) return undefined;
    const focused = typeof document === "undefined" || document.hasFocus();
    const created = new StepIdleWatch((idleMs, inFocus) => void reportStepIdle(session.commands, sessionId, idleMs, inFocus), focused);
    watch.current = created;
    const onInput = () => created.input();
    const onFocus = () => created.focus(true);
    const onBlur = () => created.focus(false);
    for (const name of INPUT_EVENTS) window.addEventListener(name, onInput, { capture: true, passive: true });
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    return () => {
      for (const name of INPUT_EVENTS) window.removeEventListener(name, onInput, { capture: true });
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      created.close();
      watch.current = null;
    };
  }, [session, sessionId]);
  useEffect(() => {
    watch.current?.step(step);
  }, [step, session]);
}
