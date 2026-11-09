import { formatDate } from "@jana/i18n";
import { createContext, useContext, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { type SavedQuestion, type SavedQuestionsController, type SavedQuestionsView } from "@jana/client-core";
import { ScreenHeader } from "../frame/Frame.js";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { Button, ConfirmDialog, EmptyState, ErrorState, FIELD_LOOK, IconButton, Loader, Notice, type MenuItem } from "../primitives/index.js";
import { useSelect } from "../store.js";
import { PackText } from "./PackText.js";
import { useOpenTopic } from "./link.js";

const SavedContext = createContext<{ controller: SavedQuestionsController; onOpen: () => void } | null>(null);
export function SavedQuestionsProvider({ controller, onOpen, children }: { controller: SavedQuestionsController; onOpen: () => void; children: ReactNode }) {
  return <SavedContext.Provider value={{ controller, onOpen }}>{children}</SavedContext.Provider>;
}
export function SaveQuestion({ sessionId, turnId, compact = false, renderAction }: { sessionId: string; turnId: string; compact?: boolean; renderAction?: (item: MenuItem | null) => ReactNode }) {
  const context = useContext(SavedContext);
  const current = useSelect(s => {
    const chat = s.sessions[sessionId];
    if (!chat || chat.pending_ids.length || s.turns[chat.turn_ids.at(-1) ?? ""]?.status === "streaming") return false;
    return chat.turn_ids.findLast(id => (s.turns[id]?.part_ids.length ?? 0) > 0) === turnId && s.turns[turnId]?.status === "complete";
  });
  return context === null || !current ? renderAction?.(null) ?? null : <SaveQuestionButton controller={context.controller} sessionId={sessionId} turnId={turnId} onOpen={context.onOpen} compact={compact} {...(renderAction ? { renderAction } : {})} />;
}
function SaveQuestionButton({ controller, sessionId, turnId, onOpen, compact, renderAction }: { controller: SavedQuestionsController; sessionId: string; turnId: string; onOpen: () => void; compact: boolean; renderAction?: (item: MenuItem | null) => ReactNode }) {
  const t = useT(), view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const [saved, setSaved] = useState(false);
  const save = () => { void controller.save(sessionId, turnId).then(ok => { if (ok) setSaved(true); }); };
  if (renderAction) return <>
    {renderAction({ id: "save", icon: "flag-review", label: t(saved || view.uncertain ? "saved_questions.title" : "saved_questions.save"), disabled: !saved && !view.uncertain && view.busy, onSelect: saved || view.uncertain ? onOpen : save })}
    {!saved && view.error && <span role="status" className="text-meta text-muted">{t("saved_questions.failed")}</span>}
  </>;
  if (compact) return <span className="inline-flex items-center gap-2">
    <IconButton icon="flag-review" size="sm" label={t(saved ? "saved_questions.title" : "saved_questions.save")} aria-pressed={saved}
      disabled={!saved && (view.busy || view.uncertain)} onClick={saved ? onOpen : save} />
    {view.uncertain && <Button size="sm" tone="ghost" onClick={onOpen}>{t("saved_questions.title")}</Button>}
    {!saved && view.error && <span role="status" className="text-meta text-muted">{t("saved_questions.failed")}</span>}
  </span>;
  return <span className="flex flex-wrap items-center gap-2">
    <Button size="sm" tone="ghost" disabled={view.busy || view.uncertain || saved} onClick={() => { void controller.save(sessionId, turnId).then((ok) => { if (ok) setSaved(true); }); }}>{saved ? t("saved_questions.saved") : t("saved_questions.save")}</Button>
    {(saved || view.uncertain) && <Button size="sm" tone="ghost" onClick={onOpen}>{t("saved_questions.title")}</Button>}
    {!saved && view.error && <span role="status" className="text-meta text-muted">{t("saved_questions.failed")}</span>}
  </span>;
}
export interface SavedQuestionActions {
  onSelect: (id: string | null) => void;
  onStatus: (id: string, status: SavedQuestion["status"]) => void;
  onRemove: (id: string) => void;
  onRetry: () => void;
  onClose: () => void;
  onPrint?: () => void;
}
export function SavedQuestionsContent({ view, actions, subject, topic, header }: {
  view: SavedQuestionsView; actions: SavedQuestionActions; subject: (s: string) => string; topic: (id: string) => string;
  /** The host's header in place of this list's own (Notes and saved draws one header for both its lists). */
  header?: ReactNode;
}) {
  const t = useT(), [query, setQuery] = useState(""), [removing, setRemoving] = useState<string | null>(null);
  const opened = view.questions.find((q) => q.question_id === view.selected);
  const openTopic = useOpenTopic();
  const topicId = opened?.snapshot.topic_id ?? null;
  const topicTitle = topicId === null ? null : topic(topicId);
  const filtered = view.questions.filter((q) => [q.snapshot.problem, subject(q.snapshot.subject), q.snapshot.topic_id === null ? "" : topic(q.snapshot.topic_id)].join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const disabled = view.busy || view.uncertain;
  return <div className="flex min-h-full flex-col">
    {header ?? <ScreenHeader end={<Button tone="ghost" onClick={actions.onClose}>{t("action.close")}</Button>}><h1 className="text-ui font-semibold text-ink">{t("saved_questions.title")}</h1></ScreenHeader>}
    <main className="mx-auto flex w-full max-w-220 flex-col gap-5 p-6">
      <Notice message={t("saved_questions.local")} />
      {view.status === "loading" && <Loader label={t("saved_questions.loading")} />}
      {view.status === "unavailable" && <EmptyState message={t("saved_questions.unavailable")} />}
      {(view.status === "failed" || view.error) && <ErrorState message={t("saved_questions.failed")} action={{ label: t("action.try_again"), onClick: actions.onRetry }} />}
      {opened === undefined ? <>
        <label className="flex flex-col gap-2 text-meta text-muted">{t("saved_questions.search")}<input type="search" className={FIELD_LOOK} value={query} maxLength={200} onChange={(e) => setQuery(e.target.value)} /></label>
        {view.status === "ready" && filtered.length === 0 && <EmptyState message={view.questions.length === 0 ? t("saved_questions.empty") : t("saved_questions.no_matches")} />}
        <ul className="flex flex-col gap-3" aria-label={t("saved_questions.title")}>
          {filtered.map((q) => <li key={q.question_id} className="flex flex-col gap-2 rounded-card border border-line bg-canvas p-5">
            <p className="text-meta text-muted">{subject(q.snapshot.subject)} · {formatDate(q.snapshot.created_at)}</p>
            <Button disabled={disabled} onClick={() => actions.onSelect(q.question_id)}><span className="line-clamp-3 text-start">{q.snapshot.problem || t("saved_questions.untitled")}</span></Button>
            <p className="text-ui text-muted">{t(`saved_questions.status.${q.status}`)}</p>
          </li>)}
        </ul>
      </> : <>
        <Button disabled={disabled} onClick={() => actions.onSelect(null)}>{t("saved_questions.back")}</Button>
        <article data-print-question="" className="flex flex-col gap-5 rounded-card border border-line bg-canvas p-6">
          <p className="text-meta text-muted">{subject(opened.snapshot.subject)} · {formatDate(opened.snapshot.created_at)}</p>
          {topicId !== null && (openTopic !== null && topicTitle !== null && topicTitle !== topicId
            ? <div><Button size="sm" tone="ghost" disabled={disabled} aria-label={t("notes.review_topic", { topic: topicTitle })} onClick={() => openTopic(topicId)}>{topicTitle}</Button></div>
            : <p className="text-ui text-muted">{topicTitle}</p>)}
          <h2 className="text-heading text-ink">{t("saved_questions.problem")}</h2><PackText text={opened.snapshot.problem} />
          <p className="text-ui text-muted">{t(`saved_questions.reason.${opened.snapshot.reason}`)}</p>
          <h3 className="text-label text-faint">{t("saved_questions.working")}</h3>
          {opened.snapshot.board.length === 0 ? <p className="text-ui text-muted">{t("saved_questions.no_working")}</p> : <ol className="flex flex-col gap-4">{opened.snapshot.board.map((line, i) => <li key={i} className="flex flex-col gap-1 text-body text-ink">
            <Tex tex={line.latex} /><PackText text={line.reason} />
          </li>)}</ol>}
          <p className="text-meta text-muted">{t(`saved_questions.status.${opened.status}`)}</p>
        </article>
        <div className="flex flex-wrap gap-2">
          <Button disabled={disabled} onClick={() => actions.onStatus(opened.question_id, opened.status === "kept" ? "sorted" : "kept")}>{opened.status === "kept" ? t("saved_questions.resolve") : t("saved_questions.keep")}</Button>
          {actions.onPrint !== undefined && <Button onClick={actions.onPrint}>{t("saved_questions.print")}</Button>}
          <Button disabled={disabled} onClick={() => setRemoving(opened.question_id)}>{t("saved_questions.remove")}</Button>
        </div>
      </>}
      <ConfirmDialog open={removing !== null} message={t("saved_questions.remove_confirm")} confirm={t("saved_questions.remove")} cancel={t("action.cancel")} destructive
        onConfirm={() => { if (removing !== null) actions.onRemove(removing); setRemoving(null); }} onCancel={() => setRemoving(null)} />
    </main>
  </div>;
}
export function SavedQuestions({ controller, onClose, header, ...names }: { controller: SavedQuestionsController; onClose: () => void; subject: (s: string) => string; topic: (id: string) => string; header?: ReactNode }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => { void controller.load(); }, [controller]);
  return <SavedQuestionsContent view={view} {...names} {...(header !== undefined ? { header } : {})} actions={{ onClose, onSelect: controller.select.bind(controller), onStatus: (id, status) => void controller.status(id, status), onRemove: (id) => void controller.remove(id),
    onRetry: () => { if (view.uncertain) void controller.retry(); else void controller.load(); }, onPrint: () => window.print() }} />;
}
