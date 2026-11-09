import { useCallback, useEffect, useRef, useState } from "react";
import { listedNote, type CapturedQuestion, type ListedNote } from "@jana/client-core";
import { EntryEditor, type EntryHandle } from "../composer/EntryEditor.js";
import { PackText } from "../learn/PackText.js";
import { useT } from "../i18n.js";
import { Button, EmptyState, FIELD_LOOK } from "../primitives/index.js";
import { DictationControl, ReadAloud, useSpeechTools, useSpeechView } from "./SpeechTools.js";

/** A note enters storage only on Save. The volatile transcription is never a tutor input. A note marked private shows
 * without its text until the student opens it through a confirm step (D102): a courtesy on a shared computer, not a
 * lock, and the copy says so. */
export function StudyNotes({ embedded = false, show = "all", onDone }: {
  embedded?: boolean;
  /** Which part to draw: the editor with the list below it (standalone), the list alone, or the editor alone (the
   * Notes place opens it only on New note). */
  show?: "all" | "list" | "editor";
  /** Called by the editor alone once the note is saved or given up: the place goes back to its lists. */
  onDone?: () => void;
}) {
  const t = useT(), speech = useSpeechTools(), view = useSpeechView();
  const editor = useRef<EntryHandle>(null), capture = useRef<CapturedQuestion | undefined>(undefined);
  const [notes, setNotes] = useState<ListedNote[] | null>(null), [query, setQuery] = useState("");
  const [opened, setOpened] = useState<Record<string, string>>({}), [confirming, setConfirming] = useState<string | null>(null);
  const [review, setReview] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState(false);
  const live = useRef(true), revision = useRef(0), pending = useRef(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => {
    const mine = ++revision.current;
    void speech?.listNotes(query).then((result) => { if (live.current && mine === revision.current) setNotes(result.notes); })
      .catch(() => { if (live.current && mine === revision.current) setError(true); });
  }, [speech, query]);
  const receive = useCallback((draft: CapturedQuestion): boolean => {
    if (!editor.current) return false;
    editor.current.insert(`${editor.current.text() ? " " : ""}${draft.text}`, draft.regions.some((r) => r.needs_confirmation));
    capture.current = draft; setReview(true); editor.current.focus(); return true;
  }, []);
  const busy = ["starting", "listening", "finishing"].includes(view.dictation);
  const save = () => {
    const text = editor.current?.text().trim();
    if (!speech || !text || pending.current || busy) return;
    pending.current = true;
    setSaving(true); setError(false);
    void speech.saveNote(text, capture.current).then((note) => {
      if (!live.current) return;
      capture.current = undefined; editor.current?.clear(); setReview(false); setQuery("");
      setNotes((current) => [listedNote(note), ...(current ?? [])]);
      if (show === "editor") onDone?.();
    }).catch(() => { if (live.current) setError(true); }).finally(() => { pending.current = false; if (live.current) setSaving(false); });
  };
  const discard = () => { speech?.cancelDictation(); speech?.discardDraft(capture.current); editor.current?.clear(); capture.current = undefined; setReview(false); };
  const Root = embedded ? "section" : "main", Heading = embedded ? "h2" : "h1";
  const listed = notes ?? [];
  return <Root className="flex flex-col gap-3 p-6">
    <Heading className="text-heading">{show === "editor" ? t("notes.new") : embedded ? t("notes.student_written") : t("notes.title")}</Heading>
    {show !== "list" && <>
      {/* The dictation line only where dictation is installed: copy about a control that is not there confuses. */}
      {speech?.canDictate === true && <p className="text-ui text-muted">{t("notes.own_voice")}</p>}
      <EntryEditor ref={editor} label={t("notes.draft")} placeholder={t("notes.placeholder")} multiline numbered={false} disabled={busy || saving}
        onSubmit={() => { save(); return false; }}
        onDictate={(pressed) => { if (pressed && !saving) void speech?.beginDictation("question", "note"); else speech?.endDictation(); }}
        onCancel={discard} />
      <DictationControl purpose="question" destination="note" disabled={saving} onDraft={receive} />
      {review && <p role="status" className={capture.current?.regions.some((r) => r.needs_confirmation) ? "underline decoration-dotted" : ""}>{t("notes.review")}</p>}
      <div className="flex gap-2">
        <Button disabled={saving || busy} onClick={save}>{t("notes.save")}</Button>
        <Button disabled={saving} onClick={() => { discard(); if (show === "editor") onDone?.(); }}>{t("action.cancel")}</Button>
      </div>
    </>}
    {error && <p role="status">{t("notes.failed")}</p>}
    {show === "editor" ? null : <>
    {(listed.length > 0 || query !== "") && <label className="mt-3 flex flex-col gap-2 text-meta text-muted">{t("notes.search")}<input type="search" className={FIELD_LOOK} value={query} maxLength={200} onChange={(e) => setQuery(e.target.value)} /></label>}
    {show === "list" && notes !== null && notes.length === 0 && query === "" && <EmptyState message={t("notes.own_empty")} />}
    {listed.map((note) => {
      const text = note.private ? opened[note.note_id] ?? null : note.text;
      return <article key={note.note_id} className="rounded-card border border-line p-3" data-note-private={note.private ? "" : undefined}>
        {note.private && <p className="text-meta text-muted">{t("notes.private")}</p>}
        {text !== null ? <PackText text={text} /> : <p className="text-muted">{t("notes.private_hidden")}</p>}
        {text === null && (confirming === note.note_id ? (
          <div role="group" aria-label={t("notes.open_confirm")} className="flex flex-wrap items-center gap-2">
            <p>{t("notes.open_confirm")}</p>
            <Button tone="primary" onClick={() => { void speech?.openNote(note.note_id).then((found) => { if (live.current) { setOpened((all) => ({ ...all, [note.note_id]: found.text })); setConfirming(null); } }).catch(() => { if (live.current) setError(true); }); }}>{t("notes.open_yes")}</Button>
            <Button onClick={() => setConfirming(null)}>{t("action.cancel")}</Button>
          </div>
        ) : <Button onClick={() => setConfirming(note.note_id)}>{t("notes.open")}</Button>)}
        {text !== null && <ReadAloud source={{ kind: "note", note_id: note.note_id }} />}
        <Button onClick={() => { void speech?.markNotePrivate(note.note_id, !note.private).then((marked) => { if (live.current) { setNotes((current) => (current ?? []).map((n) => (n.note_id === note.note_id ? marked : n))); if (marked.private === true) setOpened((all) => Object.fromEntries(Object.entries(all).filter(([id]) => id !== note.note_id))); } }).catch(() => { if (live.current) setError(true); }); }}>{note.private ? t("notes.unmark_private") : t("notes.mark_private")}</Button>
        <Button onClick={() => { void speech?.deleteNote(note.note_id).then(() => { if (live.current) setNotes((current) => (current ?? []).filter((n) => n.note_id !== note.note_id)); }).catch(() => { if (live.current) setError(true); }); }}>{t("notes.delete")}</Button>
      </article>;
    })}
    </>}
  </Root>;
}
