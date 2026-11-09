import { createContext, useContext, useEffect, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { CapturedQuestion, CapturePurpose, PointerView, SpeechController, SpeechView, ReadSource, WritingView, PenView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, IconButton, type MenuItem } from "../primitives/index.js";

const Context = createContext<SpeechController | null>(null);
const EMPTY: SpeechView = { available: false, partId: null, reading: false, text: "", charIndex: 0,
  dictation: "idle", draft: null, error: null };
// Every voice reason with student copy: the unavailable reasons of VOC-14 and the microphone errors of VOC-16, from
// the client or as a core `reason_key`.
const ERRORS = ["voice.wait_for_tutor", "voice.dictation_timeout", "voice.dictation_unheard", "voice.synthesiser_unavailable",
  "voice.unavailable", "voice.stopped_working", "voice.disconnected", "voice.protocol_error", "voice.audio_backpressure",
  "voice.endpoint_error", "voice.endpoint_unavailable", "voice.pack_unavailable", "voice.memory_unavailable",
  "voice.microphone_unavailable", "voice.microphone_missing", "voice.microphone_denied", "voice.too_noisy"];
/** The catalogue key for a voice error or reason; anything without copy uses one stable, localized fallback, so no
 * raw key reaches the student. */
export const voiceErrorKey = (error: string, fallback = "voice.unavailable"): string => ERRORS.includes(error) ? error : fallback;
const errorKey = (error: string) => voiceErrorKey(error);
const none = () => () => undefined;
const empty = () => EMPTY;
export function SpeechToolsProvider({ controller, children }: { controller: SpeechController; children: ReactNode }) {
  return <Context.Provider value={controller}>{children}</Context.Provider>;
}
export function useSpeechTools(): SpeechController | null { return useContext(Context); }
export function useSpeechView(): SpeechView {
  const controller = useSpeechTools();
  return useSyncExternalStore(controller?.subscribe ?? none, controller?.getState ?? empty, empty);
}
const noPointer = (): PointerView | null => null;
const noWriting = (): WritingView | null => null;
const REST_PEN: PenView = { target: null, since_ms: 0 };
const restPen = (): PenView => REST_PEN;
/** The tutor's pen (UXS-56): where it is now, by id. */
export function useVoicePen(): PenView {
  const voice = useSpeechTools()?.voice ?? null;
  return useSyncExternalStore(voice?.subscribe ?? none, voice === null ? restPen : () => voice.getState().pen ?? REST_PEN, restPen);
}
/** The line being written on in time with the words (UXS-55): its step id and how far the reveal has got. */
export function useVoiceWriting(): WritingView | null {
  const voice = useSpeechTools()?.voice ?? null;
  return useSyncExternalStore(voice?.subscribe ?? none, voice === null ? noWriting : () => voice.getState().writing, noWriting);
}
/** The tutor's temporary pointer fired from heard playback (VOC-51, UXS-63), or null: ids only, for the board and the
 * visuals to draw their pointer ring on. */
export function useVoicePointer(): PointerView | null {
  const voice = useSpeechTools()?.voice ?? null;
  return useSyncExternalStore(voice?.subscribe ?? none, voice === null ? noPointer : () => voice.getState().pointer, noPointer);
}

/** Read aloud as a quiet icon button (its words are its name and its tooltip), with Stop while it reads; the caption
 * with the word being spoken, and any error, take a line of their own under the row it sits in. */
export function ReadAloud({ partId, source, renderActions }: { partId?: string; source?: ReadSource; renderActions?: (items: MenuItem[]) => ReactNode }) {
  const t = useT(), controller = useSpeechTools(), view = useSpeechView();
  const identity = partId ?? (source ? JSON.stringify(source) : "");
  useEffect(() => () => { if (controller?.getState().partId === identity) controller.stopReading(); }, [controller, identity]);
  const reference = partId ?? source;
  if (!view.available || !controller || !reference) return renderActions?.([]) ?? null;
  const selected = view.partId === identity, active = selected && view.reading;
  const nextSpace = view.text.slice(view.charIndex).search(/\s/);
  const end = nextSpace < 0 ? view.text.length : view.charIndex + nextSpace;
  return <span data-read-aloud="" className="contents text-meta">
    {renderActions ? renderActions([
      { id: "read", icon: "read-aloud", label: t(active ? "voice.restart_reading" : "voice.read_aloud"), onSelect: () => void controller.read(reference) },
      ...(active ? [{ id: "stop-reading", icon: "stop" as const, label: t("voice.stop_reading"), onSelect: () => controller.stopReading() }] : []),
    ]) : <>
      <IconButton icon="read-aloud" size="sm" label={t(active ? "voice.restart_reading" : "voice.read_aloud")} onClick={() => void controller.read(reference)} />
      {active && <IconButton icon="stop" size="sm" label={t("voice.stop_reading")} onClick={() => controller.stopReading()} />}
    </>}
    {selected && <span role="status" aria-live="polite" className="sr-only">{t(active ? "voice.state.speaking" : "voice.state.idle")}</span>}
    {selected && view.text && <p aria-label={t("voice.reading_caption")} className="mt-1 basis-full text-meta text-muted">
      {view.text.slice(0, view.charIndex)}<mark>{view.text.slice(view.charIndex, end)}</mark>{view.text.slice(end)}
    </p>}
    {selected && view.error && <p role="status" className="basis-full text-meta">{t(errorKey(view.error))}</p>}
    {/* VOC-14: a synthesiser failure keeps the text and offers to read it again. */}
    {selected && !view.reading && view.error === "voice.synthesiser_unavailable" &&
      <Button size="sm" onClick={() => void controller.read(reference)}>{t("action.try_again")}</Button>}
  </span>;
}

/** Hold to dictate (VOC-46). `look="tool"` is the composer's voice slot: the microphone icon button and Cancel only;
 * the transcript and state line are then drawn by `DictationStatus` where the host puts it. */
export function DictationControl({ purpose, disabled, onDraft, destination = "question", look = "button" }: { purpose: CapturePurpose; disabled: boolean; destination?: "question" | "note";
  onDraft: (draft: CapturedQuestion) => boolean; look?: "button" | "tool" }) {
  const t = useT(), controller = useSpeechTools(), view = useSpeechView();
  useEffect(() => {
    if (!controller || view.dictation !== "review") return;
    if (view.draft && onDraft(view.draft)) controller.takeDraft();
  }, [controller, view.dictation, view.draft, onDraft]);
  if (!controller?.canDictate) return null;
  const busy = view.dictation === "starting" || view.dictation === "listening" || view.dictation === "finishing";
  const hold = {
    disabled: disabled || view.dictation === "finishing", "aria-pressed": view.dictation === "listening",
    onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>) => {
      event.currentTarget.setPointerCapture(event.pointerId); void controller.beginDictation(purpose, destination);
    },
    onPointerUp: () => controller.endDictation(), onPointerCancel: () => controller.cancelDictation(),
    onBlur: () => controller.endDictation(),
    onKeyDown: (event: ReactKeyboardEvent<HTMLButtonElement>) => { if ((event.code === "Space" || event.code === "Enter") && !event.repeat) {
      event.preventDefault(); void controller.beginDictation(purpose, destination);
    } },
    onKeyUp: (event: ReactKeyboardEvent<HTMLButtonElement>) => { if (event.code === "Space" || event.code === "Enter") {
      event.preventDefault(); controller.endDictation();
    } },
  };
  if (look === "tool") {
    return <span className="inline-flex items-center gap-1">
      {busy && <Button tone="ghost" size="sm" onClick={() => controller.cancelDictation()}>{t("voice.cancel_dictation")}</Button>}
      <IconButton icon="voice" label={t("voice.hold_to_dictate")} tip="above" {...hold} />
    </span>;
  }
  return <div className="mt-2 text-meta">
    <Button {...hold}>{t("voice.hold_to_dictate")}</Button>
    {busy && <Button onClick={() => controller.cancelDictation()}>{t("voice.cancel_dictation")}</Button>}
    <DictationStatus />
  </div>;
}

/** What dictation is doing: the words heard so far and the state line, or the reason it stopped. */
export function DictationStatus() {
  const t = useT(), controller = useSpeechTools(), view = useSpeechView();
  if (!controller?.canDictate) return null;
  const busy = view.dictation === "starting" || view.dictation === "listening" || view.dictation === "finishing";
  if (!busy && !view.error) return null;
  return <div className="text-meta">
    {busy && <p aria-live="polite">{view.transcript} <span className="text-muted">{view.tentative}</span></p>}
    <span role="status" className="ms-2">
      {busy ? t(`voice.dictation.${view.dictation}`) : view.error ? t(errorKey(view.error)) : ""}
    </span>
  </div>;
}
