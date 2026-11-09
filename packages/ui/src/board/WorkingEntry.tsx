import { inputError } from "../composer/input-error.js";
import { useEffect, useRef, useState } from "react";
import { convertEntry, InputError, workingLines, type CaptureSource, type StepRec } from "@jana/client-core";
import { PhotoCapture } from "../composer/PhotoCapture.js";
import { EntryEditor, type EntryHandle } from "../composer/EntryEditor.js";
import { useT } from "../i18n.js";
import { Button, ErrorState } from "../primitives/index.js";
import { useSelect, useSession } from "../store.js";

/** `sketch`: a photo of the student's own graph or free-body diagram, checked against the question's checked diagram
 * (snap your sketch, JM-D10); otherwise a photo of lines of working. */
export interface WorkingPhoto { sessionId: string; file?: Blob; source?: CaptureSource; sketch?: boolean }
export function WorkingEntry({ sessionId, step, onClose, photoInput, onPhotoClose, firstLine = 1 }: { sessionId: string; step?: StepRec; onClose?: () => void; photoInput?: WorkingPhoto | null; onPhotoClose?: () => void; firstLine?: number }) {
  const t = useT();
  const session = useSession();
  const editor = useRef<EntryHandle>(null);
  const [photo, setPhoto] = useState<WorkingPhoto | null>(null);
  useEffect(() => { setPhoto(null); }, [sessionId]);
  useEffect(() => { if (photoInput?.sessionId === sessionId) setPhoto(photoInput); }, [photoInput, sessionId]);
  const [error, setError] = useState<{ code: string; position?: number; word?: string } | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const sending = useSelect((s) => sent !== null && s.messages[sent]?.state === "sending");
  const rejected = useSelect((s) => sent === null || s.messages[sent]?.state !== "failed" ? null : s.messages[sent]?.error_details ?? null);
  const completed = useSelect((s) => {
    const id = sent === null ? null : s.messages[sent]?.turn_id;
    return id == null ? null : s.turns[id] ?? null;
  });
  const live = useSelect((s) => (s.sessions[sessionId]?.turn_ids ?? []).some((id) => s.turns[id]?.status === "streaming"));
  const subject = useSelect((s) => s.sessions[sessionId]?.subject);
  const hasQuestion = useSelect((s) => (s.sessions[sessionId]?.turn_ids.length ?? 0) > 0);
  useEffect(() => {
    if (completed?.status !== "complete" || completed.degraded || completed.error !== null) return;
    editor.current?.clear();
    setSent(null);
    onClose?.();
  }, [completed, onClose]);
  const submit = (text: string): boolean => {
    if (session === null || live || sending || !hasQuestion) return false;
    try {
      if (sent !== null) session.discardFailed?.(sent);
      const blocks = editor.current?.blocks() ?? [];
      if (blocks.some((block) => block.type === "ledger_table")) { setSent(session.send(blocks, "board_line")); return false; }
      const lines = workingLines(text);
      if (step !== undefined && lines.length !== 1) throw new InputError("one_line", 0);
      for (const line of lines) {
        const source = line.replace(/\\\((.*?)\\\)/g, "$1");
        const result = convertEntry(source);
        if (result.error !== null) { setError(result.error); return false; }
      }
      setError(null);
      setSent(step === undefined ? session.sendWorking(text) : session.sendBoardLine(step.step_id, text));
      // Keep the draft until successful completion; a failed worker or model call cannot erase it.
    } catch (err) { setError(err instanceof InputError ? { code: err.code, position: err.position, word: err.word } : { code: "unknown_word" }); }
    return false;
  };
  return <div className="border-t border-line bg-canvas p-3" onClick={(event) => event.stopPropagation()}>
    {photo?.sessionId === sessionId && photo.sketch === true && <PhotoCapture key={`${sessionId}:sketch`} sessionId={sessionId} {...(photo.file === undefined ? {} : { file: photo.file })} {...(photo.source === undefined ? {} : { source: photo.source })} purpose="sketch" onConfirmed={(capture) => { if (session !== null) { if (sent !== null) session.discardFailed?.(sent); setSent(session.sendCapture(capture, "check_my_working")); } }} onTyped={(text) => editor.current?.insert(text)} onDiscardSource={() => { setPhoto({ sessionId, sketch: true }); onPhotoClose?.(); }} onClose={() => { setPhoto(null); onPhotoClose?.(); }} />}
    {photo?.sessionId === sessionId && photo.sketch !== true && <PhotoCapture key={sessionId} {...photo} purpose="working" onConfirmed={(capture) => { if (session !== null) { if (sent !== null) session.discardFailed?.(sent); editor.current?.clear(); editor.current?.insert(capture.text); setSent(session.sendCapture(capture, "board_line")); } }} onTyped={(text) => editor.current?.insert(text)} onDiscardSource={() => { setPhoto({ sessionId }); onPhotoClose?.(); }} onClose={() => { setPhoto(null); onPhotoClose?.(); }} />}
    <EntryEditor ref={editor} accounting={subject === "accounting"} mode={subject === "physics" || subject === "chemistry" || subject === "chem" ? "units" : "maths"} sessionId={sessionId} draftSlot={step === undefined ? "working" : `working:${step.step_id}`} label={t(step === undefined ? "input.working" : "input.edit_line")}
      placeholder={t("input.working_placeholder")} initialText={step?.latex ?? ""} multiline={step === undefined} firstLine={firstLine}
      disabled={session === null || live || sending || !hasQuestion} onSubmit={submit} {...(onClose === undefined ? {} : { onCancel: onClose })} />
    <div className="mt-2 flex flex-wrap items-center gap-2">
      {step === undefined && <Button tone="ghost" size="sm" disabled={session === null || live || sending || !hasQuestion} onClick={() => setPhoto({ sessionId })}>{t("photo.working_action")}</Button>}
      {step === undefined && (subject === "physics" || subject === undefined) && <Button tone="ghost" size="sm" disabled={session === null || live || sending || !hasQuestion} onClick={() => setPhoto({ sessionId, sketch: true })}>{t("photo.sketch_action")}</Button>}
      <span className="flex-1" />
      <Button tone="ghost" size="sm" onClick={() => { editor.current?.clear(); onClose?.(); }}>{t("action.cancel")}</Button>
      <Button tone="primary" size="sm" disabled={session === null || live || sending || !hasQuestion} onClick={() => editor.current?.submit()}>{t("input.check_working")}</Button>
    </div>
    {error !== null && <ErrorState message={inputError(t, error)} />}
    {error === null && rejected !== null && typeof rejected.hint_key === "string" &&
      <ErrorState message={inputError(t, { code: rejected.hint_key, word: String(rejected.word ?? ""), position: Number(rejected.position ?? 0) })} />}
  </div>;
}
