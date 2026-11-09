// Progress → "Session history" (UXS-48): the open student's retained sessions, newest first, by date, subject, topic and
// duration, and one opened read-only with its conversation, board and visuals as the transcript draws them, with
// nothing to answer, report or save. One line says how long conversations are kept (the fixed DAT-08 period, D53) and
// that progress stays after older chats are deleted; a session whose conversation has gone says so and opens with
// nothing in it. History has no sharing, sending or export action of any kind. States: loading, canonical, empty,
// error-retriable, not served; an opened session can be loading, failed or locked during a mock sitting (ASM-18).
import { useEffect, useMemo, useSyncExternalStore } from "react";
import { StoreHolder, historyStore, type HistoryController, type HistoryEntry, type HistoryView } from "@jana/client-core";
import { Board } from "../board/Board.js";
import { ScreenHeader } from "../frame/Frame.js";
import { useT } from "../i18n.js";
import { Button, EmptyState, ErrorState, Loader, Notice, SubjectDot } from "../primitives/index.js";
import { StoreProvider } from "../store.js";
import { Transcript } from "../transcript/Transcript.js";

export interface HistoryActions {
  onOpen: (sessionId: string) => void;
  onBack: () => void;
  onClose: () => void;
  onRetry: () => void;
  onMore: () => void;
}

function Entry({ entry, subject, onOpen }: { entry: HistoryEntry; subject: (s: string) => string; onOpen: () => void }) {
  const t = useT();
  const minutes = entry.duration_ms === null ? null : Math.max(1, Math.round(entry.duration_ms / 60_000));
  const what = [entry.subject !== null ? subject(entry.subject) : null, entry.topic_title, entry.title || null].filter((x): x is string => x !== null && x !== "");
  return (
    <li data-history-session={entry.session_id} data-transcript={entry.transcript}>
      <button type="button" onClick={onOpen} className="jana-press flex w-full flex-wrap items-center justify-between gap-x-6 gap-y-1 rounded-control px-3 py-3 text-start hover:bg-wash">
        <span className="flex min-w-0 items-center gap-3">
          {entry.subject !== null && <SubjectDot subject={entry.subject} />}
          <span className="flex min-w-0 flex-col gap-0.5">
            <span className="text-ui font-medium text-ink">{what.length > 0 ? what.join(" · ") : t("history.untitled")}</span>
            <span className="text-meta text-muted">
              {t("history.when", { date: new Date(entry.started_at) })}
              {minutes !== null && <>{" · "}{t("history.duration", { minutes })}</>}
            </span>
          </span>
        </span>
        {entry.transcript === "purged" && <span className="text-meta text-muted">{t("history.purged_short")}</span>}
      </button>
    </li>
  );
}

/** The opened session, drawn from a store of its own (read-only): no session controller, so nothing can be sent. */
function Opened({ opened }: { opened: NonNullable<HistoryView["opened"]> }) {
  const t = useT();
  const holder = useMemo(() => {
    const made = new StoreHolder();
    if (opened.session !== null) {
      const store = historyStore(opened.session);
      made.update(() => store);
    }
    return made;
  }, [opened.session]);
  if (opened.status === "loading") return <Loader label={t("history.opening")} />;
  if (opened.status === "locked") return <Notice message={t("history.locked")} />;
  if (opened.status === "failed" || opened.session === null) return <ErrorState message={t("history.open_failed")} />;
  if (opened.session.session.transcript === "purged") return <EmptyState message={t("history.purged")} />;
  return (
    <div data-history-opened={opened.sessionId} className="flex min-h-0 flex-1 flex-col">
      <StoreProvider holder={holder} session={null} readOnly>
        <Transcript sessionId={opened.sessionId} />
        {opened.session.boards.length > 0 && <div className="min-h-80 border-t border-line" data-history-board=""><Board sessionId={opened.sessionId} /></div>}
      </StoreProvider>
    </div>
  );
}

export function SessionHistoryContent({ view, subject, actions }: { view: HistoryView; subject: (s: string) => string; actions: HistoryActions }) {
  const t = useT();
  const opened = view.opened;
  const entry = opened === null ? undefined : view.sessions.find((s) => s.session_id === opened.sessionId);
  return (
    <div className="flex min-h-full flex-col">
      <ScreenHeader end={<Button tone="ghost" onClick={opened !== null ? actions.onBack : actions.onClose}>{t(opened !== null ? "history.back" : "action.back")}</Button>}>
        <h1 className="text-ui font-semibold text-ink">{opened !== null && entry !== undefined ? t("history.opened_title", { date: new Date(entry.started_at) }) : t("history.title")}</h1>
      </ScreenHeader>
      <main className="mx-auto flex w-full max-w-220 flex-1 flex-col gap-5 p-6">
        {opened !== null ? <Opened opened={opened} /> : (
          <>
            {view.retentionDays !== null && <Notice message={t("history.retention", { days: view.retentionDays })} />}
            {(view.status === "loading" || view.status === "idle") && <Loader label={t("history.loading")} />}
            {view.status === "unavailable" && <EmptyState message={t("history.unavailable")} />}
            {view.status === "failed" && <ErrorState message={t("history.failed")} action={{ label: t("action.try_again"), onClick: actions.onRetry }} />}
            {view.status === "ready" && view.sessions.length === 0 && <EmptyState message={t("history.empty")} />}
            {view.sessions.length > 0 && (
              <ul aria-label={t("history.title")} className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-2">
                {view.sessions.map((s) => <Entry key={s.session_id} entry={s} subject={subject} onOpen={() => actions.onOpen(s.session_id)} />)}
              </ul>
            )}
            {view.nextBefore !== null && <div><Button disabled={view.loadingMore} onClick={actions.onMore}>{t("history.more")}</Button></div>}
          </>
        )}
      </main>
    </div>
  );
}

export function SessionHistory({ controller, subject, onClose }: { controller: HistoryController; subject: (s: string) => string; onClose: () => void }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => { void controller.load(); }, [controller]);
  return <SessionHistoryContent view={view} subject={subject} actions={{
    onOpen: (id) => void controller.open(id), onBack: () => controller.close(), onClose, onRetry: () => void controller.load(), onMore: () => void controller.more(),
  }} />;
}
