// **Your tutor** in Study (SPEC §19, W8 phase 2): one persistent conversation per profile, on this computer (JM-D27).
// The day's episode opens with the opener the core worded from facts it chose by code, shown once as the thread's first
// bubble; the learner's messages and the tutor's replies follow; under them, the buttons the engine sent from a tool
// result (`companion.action`), each labelled with its own copy key and kept in the store, so a reconnect or a restart
// shows them again. Everything the model wrote is shown as text, never as markup: no Markdown, no HTML, no links.
// The same thread is the library's panel and a tab beside Generate and Chat inside a material.
// Where the online tutor serves the profile and it has never answered its first-use notice (ARC-10), the open asks it
// here before the thread's first cloud turn, and the answer goes with the same open (W8 fix 2026-10-29).
// The learner's map opens the thread with a draft (SPEC §21.3, "Ask your tutor about this"): the words wait in the
// composer for the learner to finish and send; nothing is sent and no event is written by opening it.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { COMPANION_MESSAGE_MAX, companionActions, companionThread, momentLabel, type CompanionAction, type CompanionController, type CompanionEntry } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, ErrorState, IconButton, Loader } from "../primitives/index.js";
import { ComposerShell } from "../primitives/ComposerShell.js";
import { useAutoGrow } from "../primitives/autogrow.js";

/** What the thread reads and does: the companion controller, or a stand-in with the same members (the tests). */
export type CompanionThreadSource = Pick<CompanionController, "holder" | "getThread" | "subscribe" | "send" | "retry" | "answerNotice" | "loadEarlier">;

const ACTION_KEY = "study.companion.action.";
const NO_ENTRIES: CompanionEntry[] = [];
const NO_ACTIONS: CompanionAction[] = [];

/** A button's label: the engine's copy key when it is one of the thread's action keys and the catalogue has it, else
 * the action's own key; never text the model wrote. */
function useActionLabel(): (action: CompanionAction) => string {
  const t = useT();
  return (action) => {
    // Open the moment (SPEC §20.4): the button names the moment the engine put in the payload, never model text.
    const args = action.action === "open_moment" && typeof action.payload.at_ms === "number" ? { time: momentLabel(action.payload.at_ms) } : undefined;
    if (action.label_key.startsWith(ACTION_KEY)) {
      const label = t(action.label_key, args);
      if (label !== action.label_key) return label;
    }
    const own = `${ACTION_KEY}${action.action}`;
    const label = t(own);
    return label !== own ? label : t(`${ACTION_KEY}unknown`);
  };
}

function failureKey(error: string | null): string {
  if (error === "model_unavailable") return "study.companion.failed_model";
  if (error === "feature_disabled") return "study.companion.failed_off";
  return "study.companion.failed";
}

function TutorBubble({ entry }: { entry: Extract<CompanionEntry, { kind: "tutor" }> }) {
  const t = useT();
  const notices = entry.notices.map((key) => t(key)).filter((line, i) => line !== entry.notices[i]);
  const running = entry.status === "streaming";
  const phase = entry.phase === null || entry.phase === "streaming" ? null : entry.phase;
  const phaseLabel = phase === null ? t("tutor.status.thinking") : t(`tutor.status.${phase}`) === `tutor.status.${phase}` ? t("tutor.status.unknown") : t(`tutor.status.${phase}`);
  return (
    <li data-said="tutor" data-turn-id={entry.turn_id} data-turn-status={entry.status} className="flex max-w-[85%] flex-col gap-1 self-start">
      <span className="text-meta text-faint">{t("study.companion.tutor")}</span>
      {entry.text !== "" && <p data-companion-text="" className="whitespace-pre-wrap break-words rounded-card bg-panel px-3 py-2 text-body text-ink">{entry.text}</p>}
      {notices.map((line, i) => <p key={i} className="whitespace-pre-wrap rounded-card bg-panel px-3 py-2 text-ui text-ink">{line}</p>)}
      {running && entry.text === "" && <span role="status" className="text-meta text-muted">{phaseLabel}</span>}
      {!running && entry.status !== "complete" && entry.text === "" && notices.length === 0 && <span className="text-meta text-muted">{t("study.companion.no_reply")}</span>}
    </li>
  );
}

function StudentBubble({ entry, errorCode, onRetry }: { entry: Extract<CompanionEntry, { kind: "student" }>; errorCode: string | undefined; onRetry: () => void }) {
  const t = useT();
  const failureKey = errorCode === "interrupted" || errorCode === "send_unconfirmed" ? `error.${errorCode}` : "study.companion.not_sent";
  return (
    <li data-said="student" data-message-state={entry.state} className="flex max-w-[85%] flex-col items-end gap-1 self-end">
      <span className="text-meta text-faint">{t("study.companion.you")}</span>
      <p className="whitespace-pre-wrap break-words rounded-card border border-line px-3 py-2 text-body text-ink">{entry.text}</p>
      {entry.state === "sending" && <span className="text-meta text-muted">{t("study.companion.sending")}</span>}
      {entry.state === "failed" && <span className="flex items-center gap-2 text-meta text-muted">{t(failureKey)}<Button size="xs" tone="ghost" onClick={onRetry}>{t("action.try_again")}</Button></span>}
    </li>
  );
}

export function CompanionThread({ companion, onAction, place = "panel", draft: given = null, onDraftShown }: {
  companion: CompanionThreadSource;
  /** A button was pressed: the app opens the place it names (the tutor, practice, a Study job, …). */
  onAction: (action: CompanionAction) => void;
  /** `panel` above the library's sources, `tab` beside Generate and Chat inside a material. */
  place?: "panel" | "tab";
  /** Words to start the message with (a deep link from the map): put in the composer, never sent. */
  draft?: string | null;
  /** The draft is in the composer: the app forgets it, so it is offered once. */
  onDraftShown?: () => void;
}) {
  const t = useT();
  const label = useActionLabel();
  const thread = useSyncExternalStore(companion.subscribe, companion.getThread, companion.getThread);
  const store = useSyncExternalStore(companion.holder.subscribe, companion.holder.getState, companion.holder.getState);
  const sessionId = thread.session_id;
  const entries = useMemo(() => (sessionId === null ? NO_ENTRIES : companionThread(store, sessionId)), [store, sessionId]);
  const visibleIds = new Set(entries.flatMap((entry) => entry.kind === "tutor"
    ? [entry.turn_id] : [entry.key, store.messages[entry.key]?.message_id ?? ""]));
  const earlier = (thread.history ?? []).filter((item) => !visibleIds.has(item.id));
  const actions = useMemo(() => (sessionId === null ? NO_ACTIONS : companionActions(store, sessionId)), [store, sessionId]);
  const [draft, setDraft] = useState(given ?? "");
  const [answering, setAnswering] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(composer, draft);
  const draftShown = useRef(onDraftShown);
  draftShown.current = onDraftShown;
  useEffect(() => {
    if (given === null || given === "") return;
    setDraft(given.slice(0, COMPANION_MESSAGE_MAX));
    composer.current?.focus({ preventScroll: true });
    composer.current?.setSelectionRange?.(given.length, given.length);
    draftShown.current?.();
  }, [given]);
  const last = entries[entries.length - 1];
  const lastText = last === undefined ? "" : `${last.key}:${last.kind === "tutor" ? `${last.text.length}:${last.status}` : last.state}`;
  useEffect(() => {
    const el = list.current;
    if (el !== null) el.scrollTop = el.scrollHeight;
  }, [entries.length, lastText, actions.length]);
  const ready = thread.status === "ready" && thread.notice === null && sessionId !== null;
  // The thread can reopen while a message is being typed (a greeting loading): Enter then sends it once it is ready.
  const [queued, setQueued] = useState(false);
  const send = () => {
    if (!ready) { setQueued(true); return; }
    if (companion.send(draft) !== null) setDraft("");
  };
  useEffect(() => {
    if (!queued || !ready) return;
    setQueued(false);
    send();
  }, [queued, ready]);
  const heading = place === "tab" ? "companion-title-tab" : "companion-title";
  const answer = (choice: "accepted" | "declined") => {
    setAnswering(true);
    void companion.answerNotice(choice).finally(() => setAnswering(false));
  };
  return (
    <section aria-labelledby={heading} data-companion-thread={sessionId ?? ""} data-companion-place={place}
      className={`flex flex-col gap-3 ${place === "panel" ? "rounded-card border border-line bg-canvas px-5 py-4" : ""}`}>
      {/* Inside a material the tab already names the thread: the heading stays for assistive technology only. */}
      <header className="flex flex-col gap-1">
        <h2 id={heading} className={place === "tab" ? "sr-only" : "flex items-center gap-2 text-ui font-semibold text-ink"}>{place === "tab" ? null : <Icon name="study-area" size={16} />}{t("study.companion.title")}</h2>
        <p data-companion-local="" className="text-meta text-muted">{t("study.companion.on_this_computer")}</p>
      </header>
      {(thread.status === "idle" || thread.status === "opening") && thread.opener === null && <Loader label={t("study.companion.opening")} />}
      {thread.status === "unavailable" && <p className="text-ui text-muted">{t("study.companion.unavailable")}</p>}
      {thread.status === "failed" && <ErrorState message={t(failureKey(thread.error))} />}
      {thread.notice !== null && (
        <div role="group" aria-labelledby={`${heading}-notice`} data-companion-notice={thread.notice ?? ""} className="flex flex-col gap-2 rounded-card border border-line p-3">
          <p id={`${heading}-notice`} className="text-ui font-semibold text-ink">{t("study.companion.notice.title")}</p>
          <p className="text-ui text-ink">{t(thread.notice === "notice_declined" ? "study.companion.notice.declined" : "study.companion.notice.body")}</p>
          <div className="flex flex-wrap gap-2">
            <Button tone="primary" disabled={answering} onClick={() => answer("accepted")}>{t("egress.first_use.ok")}</Button>
            {thread.notice !== "notice_declined" && <Button tone="ghost" disabled={answering} onClick={() => answer("declined")}>{t("egress.first_use.keep_local")}</Button>}
          </div>
        </div>
      )}
      {thread.can_reply === false && thread.notice === null && <p className="text-ui text-muted">{t("study.companion.offline_history")}</p>}
      {(thread.history_cursor || thread.history_error) && <Button tone="ghost" disabled={thread.history_loading} onClick={() => void companion.loadEarlier()}>{t(thread.history_error ? "study.companion.history_failed" : "study.companion.history_more")}</Button>}
      {(thread.opener !== null || entries.length > 0 || earlier.length > 0) && (
        <ol ref={list} role="log" aria-live="polite" aria-label={t("study.companion.title")} className={`flex flex-col gap-3 overflow-y-auto ${place === "panel" ? "max-h-96" : "max-h-[60vh]"}`}>
          {earlier.map((item) => <li key={item.id} data-history-id={item.id} data-said={item.role === "user" ? "student" : "tutor"} className={`flex max-w-[85%] flex-col gap-1 ${item.role === "user" ? "self-end" : "self-start"}`}>
            <span className="text-meta text-faint">{t(item.role === "user" ? "study.companion.you" : "study.companion.tutor")} · {new Date(item.created_at).toLocaleDateString()}</span>
            <p className="whitespace-pre-wrap break-words rounded-card bg-panel px-3 py-2 text-body text-ink">{item.text}</p>
          </li>)}
          {thread.opener !== null && (
            <li data-said="tutor" data-opener={thread.opener.opener_id} className="flex max-w-[85%] flex-col gap-1 self-start">
              <span className="text-meta text-faint">{t("study.companion.tutor")}</span>
              <p data-companion-text="" className="whitespace-pre-wrap break-words rounded-card bg-panel px-3 py-2 text-body text-ink">{thread.opener.text}</p>
            </li>
          )}
          {entries.map((entry) => entry.kind === "tutor"
            ? <TutorBubble key={entry.key} entry={entry} />
            : <StudentBubble key={entry.key} entry={entry} errorCode={store.messages[entry.key]?.error_code} onRetry={() => companion.retry(entry.key)} />)}
        </ol>
      )}
      {actions.length > 0 && (
        <div role="group" aria-label={t("study.companion.actions")} className="flex flex-wrap gap-2">
          {actions.map((action) => <Button key={action.action_id} size="sm" data-companion-action={action.action} onClick={() => onAction(action)}>{label(action)}</Button>)}
        </div>
      )}
      {thread.status === "ready" && entries.length === 0 && <p className="text-meta text-muted">{t("study.companion.intro")}</p>}
      {thread.context && <p data-companion-context={thread.context.at_ms} className="flex items-center gap-2 text-meta text-muted"><Icon name="look-here" size={16} />{t("study.companion.about_moment", { time: momentLabel(thread.context.at_ms), title: thread.context.title })}</p>}
      <form onSubmit={(e) => { e.preventDefault(); send(); }}>
        <ComposerShell send={<IconButton type="submit" tone="action" icon="send" tip="above" label={t("study.companion.send")} disabled={!ready || draft.trim() === ""} />}>
          <label htmlFor={`${heading}-message`} className="sr-only">{t("study.companion.message_label")}</label>
          <textarea ref={composer} id={`${heading}-message`} data-companion-draft={draft !== "" ? "" : undefined} rows={1} maxLength={COMPANION_MESSAGE_MAX} disabled={!ready && !(draft !== "" && (thread.status === "idle" || thread.status === "opening"))} placeholder={t("study.companion.placeholder")} value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
            className="w-full resize-none bg-transparent px-1 text-body text-ink outline-none placeholder:text-placeholder" />
        </ComposerShell>
      </form>
    </section>
  );
}
