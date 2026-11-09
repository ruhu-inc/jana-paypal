import { useEffect, useSyncExternalStore } from "react";
import { MICROPHONE_REASONS, PERMANENT_REASONS, type VoiceController } from "@jana/client-core";
import { VoiceBarView, voiceErrorKey } from "@jana/ui";

/** English desktop voice controls (S3, UXS-34): the voice bar of the UI package on this computer's voice controller, and
 * the bar's keys (hold Space to talk, hold T to think, Esc to stop, Ctrl+Shift+V to start or end a hands-free
 * conversation, VOC-49, D126). Microphone permission comes exclusively from the operating system. */
export function VoiceBar({ controller }: { controller: VoiceController }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getState);
  // VOC-49, D126 (as in Codex): Ctrl+Shift+V (Cmd+Shift+V on macOS) starts or ends a hands-free conversation while the
  // window has focus. Inside a text field it stays the platform's paste-as-plain-text. Never a system-wide hotkey.
  useEffect(() => {
    const toggle = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.repeat || event.code !== "KeyV" || !event.shiftKey ||
        event.altKey || !(event.ctrlKey || event.metaKey) || editable(event)) return;
      event.preventDefault();
      void controller.conversation().catch(() => undefined);
    };
    window.addEventListener("keydown", toggle);
    return () => window.removeEventListener("keydown", toggle);
  }, [controller]);
  useEffect(() => {
    if (!view.connected) return;
    let held = false;
    const interactive = (event: KeyboardEvent) => event.composedPath().some((target) => target instanceof HTMLElement &&
      (target.isContentEditable || target.closest("input,textarea,select,button,a[href],summary,[role=button]," +
        "[role=textbox],[role=checkbox],[role=radio],[role=switch],[role=slider],[role=combobox]," +
        "[role=menuitem],[role=tab],[role=dialog],[role=alertdialog],[role=menu],[role=listbox],[role=tree],[role=grid],[tabindex],dialog")));
    const down = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.code === "Escape") { if (!interactive(event)) controller.stop(); return; }
      if (event.repeat || interactive(event) || event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
      if (event.code === "Space" && view.mode === "push_to_talk") {
        event.preventDefault(); held = true;
        void controller.ptt(true).catch(() => undefined);
      } else if (event.code === "KeyT" && view.mode === "hands_free") controller.think(true);
    };
    const up = (event: KeyboardEvent) => {
      if (event.code === "Space" && held) { held = false; event.preventDefault(); void controller.ptt(false); }
      if (event.code === "KeyT") controller.think(false);
    };
    const blur = () => { if (held) { held = false; void controller.ptt(false); } controller.think(false); };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down); window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur); blur();
    };
  }, [controller, view.connected, view.mode]);
  const starting = !view.connected && view.state === "thinking";
  // Unknown core reasons fall back to the VOC-14 copy; no raw key reaches the student.
  const reason = view.reason === null ? null : voiceErrorKey(view.reason, "voice.stopped_working");
  // VOC-14: after a give-up or failure, "Turn voice on again" reconnects. A microphone error (VOC-16) retries on the
  // next press instead, and a missing pack or memory cannot be fixed by reconnecting; typing works throughout.
  const restart = view.state === "unavailable" && reason !== null && !MICROPHONE_REASONS.includes(reason) &&
    !PERMANENT_REASONS.includes(reason);
  return <VoiceBarView view={view} starting={starting} restart={restart} actions={{
    onToggle: () => {
      if (view.connected || starting) controller.close(); else void controller.open().catch(() => undefined);
    },
    onConversation: () => void controller.conversation().catch(() => undefined),
    onRestart: () => void controller.restart().catch(() => undefined),
    onMode: (mode) => void controller.mode(mode).catch(() => undefined),
    onHold: (pressed) => {
      if (view.mode === "push_to_talk") void controller.ptt(pressed).catch(() => undefined);
      else controller.think(pressed);
    },
    onStop: () => controller.stop(),
  }} />;
}

/** A text field has focus: where Ctrl+Shift+V pastes as plain text. */
function editable(event: KeyboardEvent): boolean {
  return event.composedPath().some((target) => target instanceof HTMLElement &&
    (target.isContentEditable || target.closest("input,textarea,[role=textbox],[contenteditable=true]") !== null));
}
