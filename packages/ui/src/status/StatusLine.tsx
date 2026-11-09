// The turn progress line (UXS-03, UIA-22): one calm live line under the live turn, from the latest `status` and the
// elapsed time in server time, so the clock never jumps after a reconnect. Labels appear only after the label delay
// (no strobing); progress shows only from real counters; silence thresholds count from the last event.
import { useEffect, useState } from "react";
import { CLIENT_KEYS, UNKNOWN, elapsedMs, silenceMs, connectionKey, type TurnRec } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";
import { useSelect } from "../store.js";
import { familyOf, phaseOf } from "../transcript/tools.js";

function useNow(active: boolean, every = 250): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), every);
    return () => clearInterval(timer);
  }, [active, every]);
  return now;
}

export function StatusLine({ turn, onStop, onRetry, onUseFaster }: {
  turn: TurnRec; onStop?: () => void; onRetry?: () => void;
  /** "Use the faster tutor" (UXS-03): offered once a phase on the strong tier passes `ui_slow_strong_tier_ms`, and
   * only when the app gives a way to remember the choice (a faster tutor is there to take over). */
  onUseFaster?: () => void;
}) {
  const t = useT();
  const live = turn.status === "streaming";
  const connection = useSelect((s) => s.connection[connectionKey(turn.session_id)]);
  const now = useNow(live);
  const activity = useSelect(s => turn.part_ids.flatMap(id => Object.values(s.parts[id]?.tools ?? {})).findLast(tool => phaseOf(tool.state) === "running"));
  const [activitySince, setActivitySince] = useState(() => Date.now());
  useEffect(() => { setActivitySince(Date.now()); }, [activity?.call_id]);
  const [chosen, setChosen] = useState(false);
  if (!live) return <TurnFooter turn={turn} />;
  const phase = turn.phase;
  const silence = silenceMs(connection, now) ?? 0;
  if (silence > CLIENT_KEYS.ui_no_reply_ms) {
    return (
      <div role="status" className="flex items-center gap-2 py-1 text-ui text-muted">
        <span>{t("tutor.status.no_reply", { minutes: Math.floor(silence / 60_000) })}</span>
        {onStop !== undefined && <Button onClick={onStop}>{t("action.stop")}</Button>}
        {onRetry !== undefined && <Button onClick={onRetry}>{t("action.try_again")}</Button>}
      </div>
    );
  }
  if (silence > CLIENT_KEYS.ui_still_working_ms) {
    return <div role="status" className="py-1 text-ui text-muted">{t("tutor.status.still_working")}</div>;
  }
  if (activity && now - activitySince >= CLIENT_KEYS.ui_running_label_after_ms) {
    const label = t(`tool.${familyOf(activity.name)}.running`);
    return <div role="status" aria-live="off" className="flex items-center gap-2 py-1 text-ui">
      <span className="jana-shimmer">{label}</span>
      <span className="sr-only" aria-live="polite" aria-atomic="true" data-status-announcement="">{now - activitySince >= 3_000 ? label : ""}</span>
    </div>;
  }
  if (phase === null) return <div role="status" className="py-1" />;
  const elapsed = elapsedMs(phase.phase_started_ts, connection, now);
  if (elapsed < CLIENT_KEYS.ui_progress_label_after_ms || phase.phase === "streaming") return <div role="status" className="py-1" />;
  let label: string;
  if (phase.label_key === "status.runtime_restarting") {
    label = t("tutor.status.restarting");
  } else if (phase.phase === "loading_model" && phase.progress !== null && phase.progress.total > 0) {
    label = t("tutor.status.loading_model", { percent: phase.progress.done / phase.progress.total });
  } else if (phase.phase === "loading_model") {
    label = t("tutor.status.loading_model_indeterminate");
  } else if (phase.phase === "waiting_first_token" && elapsed > CLIENT_KEYS.ui_first_token_wait_label_ms) {
    label = t("tutor.status.waiting_first_token_long", { seconds: Math.floor(elapsed / 1000) });
  } else if (phase.phase === UNKNOWN) {
    label = t("tutor.status.unknown"); // a phase from a newer core (API-35 tolerant reader): a generic label
  } else {
    label = t(`tutor.status.${phase.phase}`);
  }
  // Visible progress may appear sooner; LOC-14 speech waits for a phase to persist for three seconds.
  // Announce the phase, not a changing elapsed-second or loading-percentage counter.
  const spoken = phase.label_key === "status.runtime_restarting" ? t("tutor.status.restarting")
    : phase.phase === "loading_model" ? t("tutor.status.loading_model_indeterminate")
    : phase.phase === UNKNOWN ? t("tutor.status.unknown") : t(`tutor.status.${phase.phase}`);
  // This computer is slow on the strong tier: say so, and offer the faster tutor (the choice is remembered, UXS-03).
  const slow = phase.model_tier === "strong" && elapsed > CLIENT_KEYS.ui_slow_strong_tier_ms && onUseFaster !== undefined && !chosen;
  return (
    <div role="status" aria-live="off" className="flex flex-wrap items-center gap-2 py-1 text-ui">
      <span className="jana-shimmer">{label}</span>
      <span className="sr-only" aria-live="polite" aria-atomic="true" data-status-announcement="">{elapsed >= 3_000 ? spoken : ""}</span>
      {slow && <span className="text-muted">{t("tutor.status.slow_computer")}</span>}
      {slow && <Button size="sm" tone="ghost" onClick={() => { setChosen(true); onUseFaster(); }}>{t("tutor.status.use_faster")}</Button>}
    </div>
  );
}

/** The turn's timing ("Worked for 4 s"); `inline` is its place in the reply's action row, as faint meta text. */
export function TurnFooter({ turn, inline = false }: { turn: TurnRec; inline?: boolean }) {
  const t = useT();
  const seconds = Math.max(1, Math.round((turn.elapsed_ms ?? 0) / 1000));
  const key = { streaming: null, complete: "tutor.footer.complete", interrupted: "tutor.footer.interrupted", error: "tutor.footer.error", truncated: "tutor.footer.truncated" }[turn.status];
  if (key === null) return null;
  return <p data-turn-footer="" className={inline ? "ms-2 text-meta text-faint" : "py-1 text-meta text-faint"}>{t(key, { seconds })}</p>;
}

export function ConnectionBanner({ sessionId }: { sessionId: string }) {
  const t = useT();
  const connection = useSelect((s) => s.connection[connectionKey(sessionId)]);
  if (connection === undefined || !connection.banner) return null;
  return (
    <div role="status" aria-live="polite" className="sticky top-0 z-banner bg-warn-soft px-4 py-2 text-ui text-warn">
      {t(connection.state === "resyncing" ? "connection.resyncing" : "connection.reconnecting")}
    </div>
  );
}
