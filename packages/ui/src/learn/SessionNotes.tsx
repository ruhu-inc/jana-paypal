import { formatDate } from "@jana/i18n";
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { NOTE_SECTIONS, readSessionNote, sessionNoteText, type NoteReference, type SessionNote, type SessionNotesController, type SessionNotesView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, EmptyState, ErrorState, FIELD_LOOK, Loader, Notice } from "../primitives/index.js";
import { PackText } from "./PackText.js";
import { useOpenTopic } from "./link.js";

interface NoteTopic { id: string; title: string }
interface NoteNames { subject: (s: string) => string; kc: (id: string) => string; topicForKc?: (id: string) => NoteTopic | null }

export interface SessionNotesActions {
  onSearch: (query: string) => void;
  onOpen: (id: string, confirmed: boolean) => void;
  onClose: () => void;
  onRetry: (id?: string) => void;
  onPrivate: (id: string, marked: boolean) => void;
  onDelete: (id: string) => void;
  onReference: (reference: NoteReference) => Promise<boolean>;
  onExport?: (note: SessionNote) => void;
}
/** The Notes view under the place's one header: its lists by default, the editor only once the learner asks for a new
 * note (the header's one action), and from the editor a way back to the lists. */
export function NotesPlace({ header, editor, children }: {
  /** The place's header, given its one action: New note over the lists, Back to notes over the editor. */
  header: (end: ReactNode) => ReactNode;
  /** The editor, given what it calls when the note is saved or given up. */
  editor: (done: () => void) => ReactNode;
  children: ReactNode;
}) {
  const t = useT(), [editing, setEditing] = useState(false);
  const done = () => setEditing(false);
  return <>
    {header(editing ? <Button tone="ghost" onClick={done}>{t("notes.back_to_notes")}</Button> : <Button tone="primary" onClick={() => setEditing(true)}>{t("notes.new")}</Button>)}
    <div className="mx-auto w-full max-w-240">{editing ? editor(done) : children}</div>
  </>;
}
export function SessionNotesContent({ view, actions, subject, kc, topicForKc, below }: { view: SessionNotesView; actions: SessionNotesActions;
  /** The rest of the Notes view (the learner's own notes), shown with the list and not under an open note. */
  below?: ReactNode;
} & NoteNames) {
  const t = useT(), [confirming, setConfirming] = useState<string | null>(null), [referenceFailed, setReferenceFailed] = useState(false);
  const openTopic = useOpenTopic(), referenceRequest = useRef(0);
  const opened = view.opened === null ? null : readSessionNote(view.opened, true);
  const topics = new Map<string, NoteTopic>();
  for (const id of opened?.kc_ids ?? []) {
    const topic = topicForKc?.(id);
    if (topic) topics.set(topic.id, topic);
  }
  useEffect(() => {
    setReferenceFailed(false);
    referenceRequest.current++;
    return () => { referenceRequest.current++; };
  }, [opened?.note_id]);
  const showReference = async (reference: NoteReference) => {
    const request = ++referenceRequest.current;
    setReferenceFailed(false);
    let ok = false;
    try { ok = await actions.onReference(reference); } catch { /* Keep an unavailable source in the existing error state. */ }
    if (request === referenceRequest.current) setReferenceFailed(!ok);
  };
  return <><section aria-labelledby="session-notes-title" className="flex flex-col gap-4 p-6">
    <h2 id="session-notes-title" className="text-heading text-ink">{t("notes.session_title")}</h2>
    <p className="text-ui text-muted">{t("notes.session_about")}</p>
    {view.status === "loading" && <Loader label={t("notes.loading")} />}
    {view.status === "unavailable" && <Notice message={t("notes.session_unavailable")} />}
    {(view.status === "failed" || view.error) && <ErrorState message={t("notes.error")} action={{ label: t("action.try_again"), onClick: () => actions.onRetry() }} />}
    {view.busy && <Loader label={t("notes.loading")} />}
    {opened === null ? <>
      {/* Nothing to search before the first note. */}
      {(view.notes.length > 0 || view.query !== "") && <label className="flex flex-col gap-2 text-meta text-muted">{t("notes.search")}<input type="search" className={FIELD_LOOK} maxLength={200} value={view.query} onChange={(e) => actions.onSearch(e.target.value)} /></label>}
      {view.status === "ready" && view.notes.length === 0 && <EmptyState message={view.query ? t("notes.no_matches") : t("notes.empty")} />}
      <ul className="flex flex-col gap-3" aria-label={t("notes.session_title")}>
        {view.notes.map((raw) => {
          const note = readSessionNote(raw);
          return <li key={note.note_id} className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5">
            <p className="text-meta text-muted">{t("notes.kind.session")} · {formatDate(note.created_at)}</p>
            {note.private ? <>
              <p className="text-ui text-muted">{t("notes.private_hidden")}</p>
              {confirming === note.note_id ? <div role="group" aria-label={t("notes.open_confirm")} className="flex flex-col items-start gap-2">
                <p className="text-ui text-ink">{t("notes.open_confirm")}</p>
                <p className="text-ui text-muted">{t("notes.private_courtesy")}</p>
                <div className="flex gap-2"><Button disabled={view.busy} onClick={() => { actions.onOpen(note.note_id, true); setConfirming(null); }}>{t("notes.open_yes")}</Button>
                  <Button onClick={() => setConfirming(null)}>{t("action.cancel")}</Button></div>
              </div> : <Button disabled={view.busy} onClick={() => setConfirming(note.note_id)}>{t("notes.open")}</Button>}
            </> : <>
              <Button disabled={view.busy} onClick={() => actions.onOpen(note.note_id, false)}>{note.title || (note.subject === null ? t("notes.kind.session") : subject(note.subject))}</Button>
              {note.kc_ids.length > 0 && <p className="text-meta text-muted">{note.kc_ids.map(kc).join(" · ")}</p>}
            </>}
            {(note.state === "queued" || note.state === "writing") && <p role="status" className="text-ui text-muted">{t("notes.generating")}</p>}
            {note.state === "no_model" && <Notice message={t("notes.no_model")} />}
            {note.state === "failed" && <ErrorState message={t("notes.error")} action={{ label: t("action.try_again"), onClick: () => actions.onRetry(note.note_id) }} />}
          </li>;
        })}
      </ul>
    </> : <>
      <Button onClick={actions.onClose}>{t("notes.back")}</Button>
      <article className="flex flex-col gap-5 rounded-card border border-line bg-canvas p-6">
        <h2 className="text-heading text-ink">{opened.title || t("notes.kind.session")}</h2>
        {openTopic !== null && topics.size > 0 && <div className="flex flex-wrap gap-2">{[...topics.values()].map((topic) => <Button key={topic.id} size="sm" tone="ghost" disabled={view.busy} aria-label={t("notes.review_topic", { topic: topic.title })} onClick={() => openTopic(topic.id)}>{topic.title}</Button>)}</div>}
        {NOTE_SECTIONS.filter((section) => opened.statements.some((statement) => statement.section === section)).map((section) => <section key={section} className="flex flex-col gap-3">
          <h3 className="text-label text-faint">{t(`notes.section.${section}`)}</h3>
          <ul className="flex flex-col gap-3">{opened.statements.filter((s) => s.section === section).map((statement, i) => <li key={i} className="flex flex-col items-start gap-1">
            <PackText text={statement.text} /><Button size="sm" tone="ghost" disabled={view.busy} onClick={() => { void showReference(statement.reference); }}>{t("notes.source_turn")}</Button>
          </li>)}</ul>
        </section>)}
        {opened.statements.length === 0 && <Notice message={t("notes.no_supported_statements")} />}
        {(opened.state === "queued" || opened.state === "writing") && <Loader label={t("notes.generating")} />}
        {opened.state === "no_model" && <Notice message={t("notes.no_model")} />}
        {opened.state === "failed" && <ErrorState message={t("notes.error")} action={{ label: t("action.try_again"), onClick: () => actions.onRetry(opened.note_id) }} />}
        {referenceFailed && <ErrorState message={t("notes.source_unavailable")} />}
        <div className="flex flex-wrap gap-2">
          <Button disabled={view.busy} onClick={() => actions.onPrivate(opened.note_id, !opened.private)}>{opened.private ? t("notes.unmark_private") : t("notes.mark_private")}</Button>
          {actions.onExport !== undefined && opened.statements.length > 0 && <Button onClick={() => actions.onExport?.(opened)}>{t("notes.export")}</Button>}
          <Button disabled={view.busy} onClick={() => actions.onDelete(opened.note_id)}>{t("notes.delete")}</Button>
        </div>
      </article>
    </>}
  </section>{opened === null && below}</>;
}
export function SessionNotes({ controller, ...props }: { controller: SessionNotesController; onReference: SessionNotesActions["onReference"]; below?: ReactNode } & NoteNames) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => { void controller.load(); }, [controller]);
  useEffect(() => {
    if (!view.notes.some((n) => ["queued", "writing", "no_model"].includes(n.state))) return;
    const timer = setTimeout(() => void controller.load(), 2000);
    return () => clearTimeout(timer);
  }, [controller, view.notes]);
  return <SessionNotesContent {...props} view={view} actions={{ onSearch: (q) => void controller.load(q), onOpen: (id, confirm) => void controller.act(id, "open", confirm), onClose: () => controller.close(),
    onRetry: (id) => { if (id === undefined) void controller.load(); else void controller.act(id, "retry"); },
    onPrivate: (id, marked) => void controller.act(id, marked ? "private" : "public"), onDelete: (id) => void controller.act(id, "delete"), onReference: props.onReference,
    onExport: (note) => { const url = URL.createObjectURL(new Blob([sessionNoteText(note)], { type: "text/plain;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = "jana-session-note.txt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); },
  }} />;
}
