// The voice bar (UXS-34, VOC-04, VOC-06, VOC-14, VOC-16; direction A "Studio"): above the composer in voice mode, the
// state label of TABLE VOC-04 for the state the voice is in, "Start a conversation" or "End conversation" (VOC-49, D126:
// hands-free from one action, also Ctrl+Shift+V, as in Codex), the talk mode (push to talk, hands-free), hold to talk
// or hold to think, Stop, the reason when voice cannot run (its VOC-14 or VOC-16 copy, never
// a raw key) and the captions, committed words then the tentative ones. Typing always works, whatever the state. The
// desktop app drives it from its voice controller (apps/desktop/src/VoiceBar.tsx); the gallery from data.
import type { PointerEvent as ReactPointerEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import type { VoiceView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";
import { voiceErrorKey } from "./SpeechTools.js";

export interface VoiceBarActions {
  onToggle: () => void;
  /** VOC-49, D126: start a hands-free conversation (voice off) or end it (voice on). */
  onConversation: () => void;
  onRestart: () => void;
  onMode: (mode: "push_to_talk" | "hands_free") => void;
  /** Hold to talk (push to talk) or hold to think (hands-free): pressed and released. */
  onHold: (pressed: boolean) => void;
  onStop: () => void;
}

/** The voice bar from its view: `starting` while voice is being turned on, `restart` when "Turn voice on again" helps. */
export function VoiceBarView({ view, starting, restart, actions }: { view: VoiceView; starting: boolean; restart: boolean; actions: VoiceBarActions }) {
  const t = useT();
  const reason = view.reason === null ? null : voiceErrorKey(view.reason, "voice.stopped_working");
  const hold = {
    onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId);
      actions.onHold(true);
    },
    onPointerUp: () => actions.onHold(false),
    onPointerCancel: () => actions.onHold(false),
    onBlur: () => actions.onHold(false),
    onKeyDown: (event: ReactKeyboardEvent<HTMLButtonElement>) => {
      if ((event.code === "Enter" || event.code === "Space") && !event.repeat) {
        event.preventDefault();
        actions.onHold(true);
      }
    },
    onKeyUp: (event: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (event.code === "Enter" || event.code === "Space") {
        event.preventDefault();
        actions.onHold(false);
      }
    },
  };
  return (
    <section aria-label={t("voice.tutor")} data-voice-state={view.state} className="mx-auto flex w-full max-w-(--layout-composerMax) flex-col gap-2 rounded-card bg-panel px-4 py-3 text-ink">
      <div className="flex flex-wrap items-center gap-2">
        <span role="status" aria-live="polite" className="me-auto inline-flex items-center gap-2 text-ui font-medium">
          <span aria-hidden="true" className={`size-2 rounded-pill ${view.state === "listening" ? "bg-ink" : view.state === "unavailable" ? "bg-line-strong" : "bg-faint"}`} />
          {starting ? t("voice.starting") : t(`voice.state.${view.state}`)}
        </span>
        {view.connected && (
          <>
            <label className="inline-flex items-center gap-2 text-meta text-muted">
              {t("voice.talk_mode")}
              <select value={view.mode} onChange={(event) => actions.onMode(event.target.value === "hands_free" ? "hands_free" : "push_to_talk")}
                className="min-h-9 rounded-control border border-line-strong bg-canvas px-2 text-ui text-ink">
                <option value="push_to_talk">{t("voice.push_to_talk")}</option>
                <option value="hands_free">{t("voice.hands_free")}</option>
              </select>
            </label>
            <Button size="sm" {...hold}>{t(view.mode === "push_to_talk" ? "voice.hold_to_talk" : "voice.hold_to_think")}</Button>
            <Button size="sm" tone="ghost" onClick={actions.onStop}>{t("action.stop")}</Button>
          </>
        )}
        {view.connected && restart && <Button size="sm" onClick={actions.onRestart}>{t("voice.turn_on_again")}</Button>}
        {starting || view.connected
          ? <Button size="sm" tone="ghost" onClick={actions.onConversation}>{t(starting ? "voice.cancel_start" : "voice.end_conversation")}</Button>
          : !restart && <>
            <Button size="sm" onClick={actions.onConversation}>{t("voice.start_conversation")}</Button>
            <span data-conversation-shortcut="" className="text-meta text-muted">{t("voice.conversation_shortcut", { keys: shortcutKeys() })}</span>
          </>}
        {!view.connected && !starting && <Button size="sm" tone="ghost" onClick={actions.onToggle}>{t(restart ? "voice.turn_on_again" : "voice.on")}</Button>}
      </div>
      {reason !== null && <p role="status" className="text-meta text-muted">{t(reason)}</p>}
      <div role="region" aria-label={t("voice.captions")} className="text-body">
        <p>{view.student}<span className="text-muted">{view.tentative}</span></p>
        <p>{view.tutor}</p>
      </div>
    </section>
  );
}

/** The conversation shortcut as the platform writes it (TABLE LOC-12a: Ctrl reads Cmd on macOS). */
function shortcutKeys(): string {
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  return mac ? "⌘⇧V" : "Ctrl+Shift+V";
}
