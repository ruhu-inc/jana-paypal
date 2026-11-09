// The Ask box confirms a typed capture on Send; all input uses the shared rich editor (INP-01/03). Once a session has
// turns, what the student sends answers the tutor (`answer`); "New question" starts another problem in the same session,
// so the next message, typed, photographed or dictated, is a question (`ask`), not an attempt at the last one (PED-01).
// Home's Ask box (`asking`, inline) hands the question to the tutor and goes: it keeps its draft in a slot of its own
// (`home_ask`, not the tutor composer's `ask`) and empties it once sent, so the tutor's composer never opens with the
// question already sent in it and the next visit to Home starts with an empty box. While the tutor model is still
// loading, Home's Ask box still takes a question (`hold`): it is kept and sent by the app once the tutor is ready (UXS-12).
// Home's box is the one input (UXS-67): with `route`, a typed question is first routed by the app, and only a question
// for the tutor is sent there; the study area and Ask anything take the rest.
import { useCallback, useEffect, useRef, useState } from "react";
import { captureRegionText, PhotoCommands, typedCapture, reviseTypedCapture, confirmTypedCapture, cancelTypedCapture, type CapturedQuestion } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, IconButton, MenuButton, SuggestionChip } from "../primitives/index.js";
import { ComposerShell } from "../primitives/ComposerShell.js";
import { useSelect, useSession } from "../store.js";
import { CaptureDiagrams } from "./CaptureDiagrams.js";
import { DiagramDrill } from "./DiagramDrill.js";
import { PhotoCapture } from "./PhotoCapture.js";
import { QuestionDock } from "./QuestionDock.js";
import { useTutorLearning } from "../tutor/LearningState.js";
import { TutoringUsage } from "./TutoringUsage.js";
import { useDockRequest } from "./requests.js";
import { HintDock, useOpenHintRequest } from "./HintDock.js";
import { EntryEditor, type EntryHandle } from "./EntryEditor.js";
import { DictationControl, DictationStatus, useSpeechTools, useSpeechView } from "../voice/SpeechTools.js";

/** The hint ladder's length: a hint card's `of` (PED-03). */
const HINT_LADDER = 5;

/** `dock` docks it at the foot of the conversation with the suggestions above it and `note` below it (UXS-14);
 * `inline` sets it into a page, as Home's Ask box (UXS-12). */
export function Composer({ sessionId, asking = false, onSent, placement = "dock", note, placeholder, hold, route }: {
  sessionId: string; asking?: boolean; onSent?: () => void; placement?: "dock" | "inline"; note?: string;
  /** The box's placeholder in place of the usual one ("Tutor warming up · 62%", UXS-12). */
  placeholder?: string;
  /** Keep a question instead of sending it now (the tutor is still loading); the box empties as if it was sent. */
  hold?: (text: string) => void;
  /** Home's one input (UXS-67): where a typed question goes before it is sent. Resolves true to send it to the tutor
   * as usual, false when the app took it elsewhere (the study area, Ask anything); the box then empties. */
  route?: (text: string) => Promise<boolean>;
}) {
  const t = useT();
  const session = useSession();
  const editor = useRef<EntryHandle>(null);
  const speech = useSpeechTools(), speechView = useSpeechView();
  const dictated = useRef<CapturedQuestion | null>(null);
  const [review, setReview] = useState(false);
  const [draggingPhoto, setDraggingPhoto] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  /** Nothing to send yet: Send stays faint until the box has words, as a dead button would mislead (UXS-15). */
  const [empty, setEmpty] = useState(true);
  useEffect(() => { setKeyboard(false); setDraggingPhoto(false); }, [sessionId]);
  const [confirming, setConfirming] = useState(false);
  const [submitError, setSubmitError] = useState(false);
  const pending = useRef(false), mounted = useRef(true);
  const current = useRef({ session, sessionId, speech });
  current.current = { session, sessionId, speech };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { dictated.current = null; pending.current = false; setReview(false); setConfirming(false); setSubmitError(false); }, [session, sessionId, speech]);
  const dictating = ["starting", "listening", "finishing"].includes(speechView.dictation);
  const [sent, setSent] = useState<string | null>(null);
  const completed = useSelect((s) => { const id = sent === null ? null : s.messages[sent]?.turn_id; return id == null ? null : s.turns[id]; });
  useEffect(() => { if (completed?.status === "complete" && !completed.degraded && completed.error === null) { if (submittedText.current !== null && editor.current?.text() === submittedText.current) editor.current.clear(); submittedText.current = null; askNext(false); setSent(null); } }, [completed]);
  const [photo, setPhoto] = useState<{ file?: Blob; source?: "photo_paste" | "photo_drop"; sessionId: string; diagram?: boolean } | null>(null);
  /** A confirmed photo of a labelled diagram, open in its drill (SPEC §5.2): practised here, never sent as a turn. */
  const [drill, setDrill] = useState<CapturedQuestion | null>(null);
  const nextQuestion = useRef(false);
  const [newQuestion, setNewQuestion] = useState(false);
  const askNext = (on: boolean) => { nextQuestion.current = on; setNewQuestion(on); };
  const [upNext, setUpNext] = useState<CapturedQuestion[]>([]);
  useEffect(() => {
    let closed = false;
    setUpNext([]); setPhoto(null); setDrill(null); setSent(null); askNext(false);
    if (session?.commands !== undefined) void new PhotoCommands(session.commands, sessionId).list().then((result) => { if (!closed) setUpNext(result.captures); }).catch(() => undefined);
    return () => { closed = true; };
  }, [session, sessionId]);
  const sending = useSelect((s) => sent !== null && s.messages[sent]?.state === "sending");
  const submittedText = useRef<string | null>(null);
  const draft = useRef<CapturedQuestion | null>(null);
  const begin = () => typedCapture("", { sessionId, purpose: "question", confirmed: false });
  if (draft.current === null || draft.current.session_id !== sessionId) draft.current = begin();
  const currentDraft = () => draft.current ?? begin();
  const liveTurn = useSelect((s) => {
    const ids = s.sessions[sessionId]?.turn_ids ?? [];
    const last = ids[ids.length - 1];
    return last !== undefined && s.turns[last]?.status === "streaming" ? last : null;
  });
  const subject = useSelect((s) => s.sessions[sessionId]?.subject ?? "maths");
  const learning = useTutorLearning();
  const mode = useSelect((s) => s.sessions[sessionId]?.mode ?? "guided");
  const openHint = useOpenHintRequest(sessionId);
  const hintRung = useSelect((s) => s.sessions[sessionId]?.hint_rung ?? 0);
  const openRequest = useDockRequest(sessionId);
  const hasTurns = useSelect((s) => (s.sessions[sessionId]?.turn_ids.length ?? 0) > 0);
  /** What the next message is: an answer to the tutor once there are turns, unless a new question was asked for. */
  const answering = !asking && hasTurns && !newQuestion;
  const canCapture = session !== null && liveTurn === null && !dictating && !confirming;
  /** Home's Ask box: the question went to the tutor, so the box and its saved draft are emptied now (the tutor's own
   * composer reads the same draft). */
  const handedOver = () => {
    if (!asking || placement !== "inline") return;
    submittedText.current = null;
    editor.current?.clear();
  };
  const routing = useRef(false);
  const send = (intent: string, text: string, pasted = false): boolean => {
    if (route !== undefined && !routing.current && intent === "ask" && dictated.current === null) {
      if (text.trim() === "" || liveTurn !== null || dictating || pending.current) return false;
      pending.current = true; setConfirming(true);
      void route(text.trim()).catch(() => true).then((toTutor) => {
        if (!mounted.current) return;
        pending.current = false; setConfirming(false);
        if (!toTutor) { cancelTypedCapture(currentDraft()); draft.current = begin(); editor.current?.clear(); return; }
        routing.current = true;
        try { send(intent, text, pasted); } finally { routing.current = false; }
      });
      return false;
    }
    if (hold !== undefined && intent === "ask" && text.trim() !== "" && !dictating && !pending.current) {
      hold(text.trim());
      editor.current?.clear();
      return false;
    }
    if (session === null || text.trim() === "" || liveTurn !== null || dictating || pending.current) return false;
    if (intent !== "stuck" && dictated.current !== null && speech !== null) {
      pending.current = true; setConfirming(true); setSubmitError(false);
      const context = current.current, spoken = dictated.current;
      const live = () => mounted.current && current.current.session === context.session &&
        current.current.sessionId === context.sessionId && current.current.speech === context.speech;
      void speech.confirmDraft(spoken, text).then((confirmed) => {
        if (!live()) return;
        if (sent !== null) session.discardFailed?.(sent);
        submittedText.current = editor.current?.text() ?? null;
        setSent(session.sendCapture(confirmed, intent));
        handedOver();
        onSent?.();
        dictated.current = null; draft.current = begin(); setReview(false);
      }).catch(() => { if (live()) setSubmitError(true); }).finally(() => {
        if (live()) { pending.current = false; setConfirming(false); }
      });
      return false;
    }
    const capture = intent === "stuck" ? typedCapture(text.trim(), { sessionId, purpose: "answer", confirmed: true }) :
      confirmTypedCapture(reviseTypedCapture(currentDraft(), text.trim(), { purpose: intent === "ask" ? "question" : "answer", pasted }));
    if (sent !== null) session.discardFailed?.(sent);
    const key = session.sendCapture(capture, intent, intent === "stuck" ? undefined : editor.current?.blocks());
    if (intent !== "stuck") { submittedText.current = editor.current?.text() ?? null; setSent(key); }
    handedOver();
    onSent?.();
    if (intent !== "stuck") draft.current = begin();
    return false;
  };
  const stop = () => {
    if (dictating) { speech?.cancelDictation(); return; }
    if (pending.current) return;
    if (speechView.reading) { speech?.stopReading(); return; }
    if (dictated.current) speech?.discardDraft(dictated.current);
    dictated.current = null; setReview(false); setSubmitError(false);
    if (session !== null && liveTurn !== null) void session.cancel(liveTurn);
    else {
      cancelTypedCapture(currentDraft());
      draft.current = begin();
      askNext(false);
      editor.current?.clear();
    }
  };
  const onDraft = useCallback((capture: CapturedQuestion): boolean => {
    if (editor.current === null || capture.session_id !== sessionId) return false;
    editor.current.insert(`${editor.current.text() ? " " : ""}${capture.text}`, capture.regions.some((region) => region.needs_confirmation));
    editor.current.focus();
    dictated.current = capture; setReview(true); setSubmitError(false);
    return true;
  }, [sessionId]);
  const upNextShown = upNext.filter((capture) => capture.session_id === sessionId).some((capture) => capture.questions.some((q) => !q.selected));
  return <div data-region="dock" className={placement === "dock" ? "flex shrink-0 flex-col items-center gap-3 px-6 pt-3 pb-5" : "flex flex-col gap-3"} onKeyDownCapture={(event) => {
    if (event.ctrlKey && event.code === "KeyM" && speech?.canDictate && !event.repeat && liveTurn === null && !sending && !pending.current && photo === null) {
      event.preventDefault(); event.stopPropagation(); void speech.beginDictation(answering ? "answer" : "question");
    }
  }} onKeyUpCapture={(event) => { if (event.code === "KeyM") speech?.endDictation(); }} onPasteCapture={(event) => {
    const file = event.clipboardData.files[0]; if (file?.type.startsWith("image/")) { event.preventDefault(); event.stopPropagation(); if (canCapture) setPhoto({ file, source: "photo_paste", sessionId }); }
  }} onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDraggingPhoto(canCapture); } }} onDragLeave={(event) => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDraggingPhoto(false); }} onDropCapture={(event) => {
    setDraggingPhoto(false);
    const file = event.dataTransfer.files[0]; if (file?.type.startsWith("image/")) { event.preventDefault(); event.stopPropagation(); if (canCapture) setPhoto({ file, source: "photo_drop", sessionId }); }
  }}>
    {draggingPhoto && <p role="status" className="w-full rounded-card border-2 border-dashed border-focus bg-panel p-4 text-ui">{t("photo.drop_here")}</p>}
    {/* The suggestions above the composer (UXS-14, UXS-15); in guided mode the hint dock takes their place while a hint
        card is open (UXS-17). */}
    {hasTurns && !asking && placement === "dock" && <QuestionDock sessionId={sessionId} />}
    {hasTurns && !asking && liveTurn === null && placement === "dock" && mode === "guided" && openHint !== null && (
      <HintDock sessionId={sessionId} onSolution={() => void session?.setMode("solution").catch(() => undefined)} />
    )}
    {hasTurns && !asking && !newQuestion && learning === "open" && liveTurn === null && openHint === null && <div className="flex w-full max-w-(--layout-columnMax) gap-2">
      <SuggestionChip icon="hint" tone="hint" data-hint-chip="" disabled={sending || confirming || dictating} onClick={() => send("stuck", t("composer.stuck"))}>{mode === "guided" && placement === "dock" ? t("dock.hint_chip", { rung: Math.min(HINT_LADDER, hintRung + 1), of: HINT_LADDER }) : t("composer.stuck")}</SuggestionChip>
    </div>}
    {newQuestion && <p role="status" className="w-full max-w-(--layout-composerMax) text-meta text-muted">{t("composer.new_question_hint")}</p>}
    {placement === "dock" && <TutoringUsage key={sessionId} sessionId={sessionId} />}
    <ComposerShell
      above={<>
        {upNext.filter((capture) => capture.session_id === sessionId && capture.status === "confirmed" && capture.attachments.some((a) => a.kind === "diagram_crop")).map((capture) => <CaptureDiagrams key={capture.capture_id} capture={capture} />)}
        {upNextShown && <div role="group" aria-label={t("photo.up_next")} className="flex flex-wrap items-center gap-2"><p className="text-meta">{t("photo.up_next")}</p>{upNext.filter((capture) => capture.session_id === sessionId).flatMap((capture) => capture.questions.filter((q) => !q.selected).map((question) => <Button key={`${capture.capture_id}:${question.label}`} size="sm" disabled={sending || liveTurn !== null || confirming || dictating} onClick={() => {
          editor.current?.clear();
          askNext(true);
          editor.current?.insert(capture.regions.filter((region) => question.region_ids.includes(region.region_id)).map(captureRegionText).join("\n"));
          setUpNext((queue) => queue.map((item) => item.capture_id === capture.capture_id ? { ...item, questions: item.questions.map((q) => q.label === question.label ? { ...q, selected: true } : q) } : item));
        }}>{question.label}: {capture.regions.find((region) => region.region_id === question.region_ids[0])?.text.slice(0, 80)}</Button>))}</div>}
        {drill !== null && drill.session_id === sessionId && <DiagramDrill key={drill.capture_id} sessionId={sessionId} capture={drill} onClose={() => setDrill(null)} />}
        {photo !== null && photo.sessionId === sessionId && photo.diagram === true && <PhotoCapture key={`${sessionId}:diagram`} sessionId={sessionId} {...(photo.file === undefined ? {} : { file: photo.file })} {...(photo.source === undefined ? {} : { source: photo.source })} purpose="diagram" onConfirmed={(capture) => setDrill(capture)} onDiscardSource={() => setPhoto({ sessionId, diagram: true })} onTyped={() => undefined} onClose={() => setPhoto(null)} />}
        {photo !== null && photo.sessionId === sessionId && photo.diagram !== true && <PhotoCapture key={sessionId} purpose={answering ? "answer" : "question"} {...photo} onConfirmed={(capture) => { if (session !== null) { if (sent !== null) session.discardFailed?.(sent); editor.current?.clear(); editor.current?.insert(capture.text); submittedText.current = editor.current?.text() ?? null; setSent(session.sendCapture(capture, answering ? "answer" : "ask")); onSent?.(); setUpNext((queue) => [...queue, capture]); } }} onDiscardSource={() => setPhoto({ sessionId })} onTyped={(text) => editor.current?.insert(text)} onClose={() => setPhoto(null)} />}
      </>}
      attach={placement === "dock"
        ? <MenuButton label={t("photo.add")} placement="above" align="start" trigger={(props) => <IconButton icon="photo" label={t("photo.add")} tip="above" disabled={!canCapture} {...props} />} items={[
          { id: "question", icon: "photo", label: t("photo.title"), onSelect: () => setPhoto({ sessionId }) },
          { id: "diagram", icon: "diagram", label: t("photo.diagram_action"), onSelect: () => { setDrill(null); setPhoto({ sessionId, diagram: true }); } },
        ]} />
        : <IconButton icon="photo" label={t("photo.title")} tip="above" disabled={!canCapture} onClick={() => setPhoto({ sessionId })} />}
      keyboard={<><IconButton icon="maths" label={t("input.keyboard")} tip="above" aria-expanded={keyboard} disabled={sending || confirming || dictating} onClick={() => setKeyboard((open) => !open)} />
        {hasTurns && !asking && placement === "dock" && <MenuButton constrain label={t("composer.actions")} placement="above" align="start"
          trigger={props => <IconButton icon="more" label={t("composer.actions")} tip="above" disabled={liveTurn !== null || sending || confirming || dictating} {...props} />}
          items={[
            { id: "new", icon: "new-question", label: t("composer.new_question"), onSelect: () => {
              askNext(!newQuestion);
              if (!newQuestion) { cancelTypedCapture(currentDraft()); draft.current = begin(); }
              requestAnimationFrame(() => editor.current?.focus());
            } },
            { id: "explain", icon: "question", label: t("followup.explain_step"), onSelect: () => {
              session?.sendCapture(typedCapture(t("followup.ask.explain_step"), { sessionId, purpose: "answer", confirmed: true }), "answer");
            } },
          ]} />}
      </>}

      voice={<DictationControl look="tool" purpose={answering ? "answer" : "question"} disabled={liveTurn !== null || confirming || sending || photo !== null} onDraft={onDraft} />}
      send={liveTurn !== null ? <IconButton tone="action" tip="above" icon="stop" label={t("action.stop")} onClick={stop} /> :
        <IconButton tone="action" tip="above" disabled={confirming || dictating || sending || empty} icon="send" label={t("action.send")} onClick={() => editor.current?.submit()} />}
      below={<>
        <DictationStatus />
        {review && <p role="status" className="text-meta text-muted">{t("voice.review_dictation")}</p>}
        {submitError && <p role="status" className="text-meta text-muted">{t("voice.dictation_submit_failed")}</p>}
      </>}>
      <EntryEditor key={sessionId} ref={editor} sessionId={sessionId} draftSlot={asking && placement === "inline" ? "home_ask" : "ask"} look="composer" palette={keyboard} onPaletteChange={setKeyboard}
        disabled={sending || confirming || dictating} accounting={subject === "accounting"} label={t("composer.label")} placeholder={placeholder ?? t(asking ? "home.ask_placeholder" : openRequest?.card.card_type === "question" ? "dock.answer_placeholder" : "composer.placeholder")}
        mode={subject === "chemistry" || subject === "chem" ? "chem" : subject === "physics" ? "units" : "maths"}
        onChange={(text) => { setEmpty(text.trim() === ""); draft.current = reviseTypedCapture(currentDraft(), text); }}
        onSubmit={(text, pasted) => send(!asking && hasTurns && !nextQuestion.current ? "answer" : "ask", text, pasted)} onCancel={stop} />
    </ComposerShell>
    {note !== undefined && <p className="text-center text-label text-faint">{note}</p>}
  </div>;
}
