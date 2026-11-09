import { useEffect, useRef, useState } from "react";
import { useT } from "../i18n.js";
import { useSelect, useSession } from "../store.js";

interface Balance {
  scope: "subscription" | "device";
  remaining: number;
  until_ms: number;
  period_ends: boolean;
  device_remaining?: number | null;
  warning_window?: string;
}

function isBalance(value: Partial<Balance> | null): value is Balance {
  return (value?.scope === "subscription" || value?.scope === "device") &&
    typeof value.remaining === "number" && value.remaining >= 0 && value.remaining <= 1 &&
    typeof value.until_ms === "number" && value.until_ms > Date.now() && typeof value.period_ends === "boolean";
}

/** A passive status occupies the same row at any balance. Only the button submits a chat turn. */
export function TutoringUsage({ sessionId: requested, detail = false }: { sessionId?: string; detail?: boolean }) {
  const session = useSession();
  const t = useT();
  const sessionId = requested ?? session?.sessionId ?? "";
  const refreshNow = useRef<() => void>(() => undefined);
  const [balance, setBalance] = useState<Balance | null>(null);
  const [dismissed, setDismissed] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const checkButton = useRef<HTMLButtonElement>(null);
  const settled = useSelect(s => s.sessions[sessionId]?.turn_ids.findLast(id => s.turns[id]?.status !== "streaming"));
  const busy = useSelect(s => {
    const chat = s.sessions[sessionId];
    const last = chat?.turn_ids.at(-1);
    return !!chat?.pending_ids.length || (last !== undefined && s.turns[last]?.status === "streaming");
  });
  useEffect(() => {
    if (!session?.commands || session.sessionId !== sessionId) return;
    let revision = 0;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      const request = ++revision;
      clearTimeout(timer);
      try {
        const data = await session.commands.get<Partial<Balance> | null>(`/v1/sessions/${sessionId}/tutoring-usage`);
        if (request !== revision) return;
        let current = isBalance(data) ? data : null;
        if (current && typeof current.device_remaining === "number" &&
            current.device_remaining >= 0 && current.device_remaining < current.remaining) {
          current = { ...current, scope: "device", remaining: current.device_remaining };
        }
        setBalance(current);
        if (current) timer = setTimeout(() => {
          setBalance(null);
          void refresh();
        }, Math.min(current.until_ms - Date.now() + 1, 2_147_483_647));
      } catch {
        if (request === revision) setBalance(null);
      }
    };
    refreshNow.current = () => { void refresh(); };
    const resume = () => { if (document.visibilityState === "visible") void refresh(); };
    void refresh();
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    return () => {
      ++revision;
      clearTimeout(timer);
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [session, sessionId, settled]);
  const threshold = balance && balance.remaining <= 0.2 + Number.EPSILON
    ? (balance.remaining === 0 ? "exhausted" : "low") : null;
  const warningKey = threshold && balance
    ? `jana.tutoring.${balance.warning_window ?? sessionId + balance.until_ms}.${threshold}` : "";
  useEffect(() => {
    if (!threshold) { setAnnouncement(""); return; }
    try {
      const prior = sessionStorage.getItem(warningKey);
      if (prior === "dismissed") setDismissed(warningKey);
      if (prior) return;
      sessionStorage.setItem(warningKey, "announced");
    } catch { /* Storage may be disabled in a webview. */ }
    setAnnouncement(t(`tutor.usage.${threshold}`));
  }, [threshold, warningKey, t]);
  if (!session || balance === null) return null;
  const low = detail || (warningKey && dismissed !== warningKey) ? balance : null;
  if (low === null) return null;
  const percent = low && low.remaining > 0 && low.remaining < 0.01 ? "<1" : String(Math.round((low?.remaining ?? 0) * 100));
  const summary = low && t(low.scope === "device" ? "tutor.usage.device_left" : "tutor.usage.left", { percent });
  const boundary = low && t(low.period_ends ? "tutor.usage.ends" : "tutor.usage.resets", { date: new Date(low.until_ms) });
  return <div className="mx-auto flex min-h-9 w-full max-w-(--layout-composerMax) items-center justify-between gap-2 text-meta text-muted">
    <span className="sr-only" aria-live="polite">{low && announcement}</span>
    <span className="min-w-0 truncate">{low && `${summary} · ${boundary}`}</span>
    <div className="flex shrink-0 items-center gap-2">
    {!detail && <button type="button" aria-label={t("tutor.usage.dismiss")} onClick={event => {
      const editor = event.currentTarget.closest("[data-region=dock]")?.querySelector<HTMLElement>("[role=textbox]");
      setDismissed(warningKey);
      try { sessionStorage.setItem(warningKey, "dismissed"); } catch { /* Keep the in-memory choice. */ }
      editor?.focus({ preventScroll: true });
    }}>×</button>}
    {detail && <button ref={checkButton} type="button" className="shrink-0 underline-offset-2 hover:underline" disabled={busy}
      onClick={() => refreshNow.current()}>{t("tutor.usage.check")}</button>}
    </div>
  </div>;
}
