// The transcript (UIA-16, UXS-21): sticks to the bottom while the student is within the threshold; any upward scroll
// detaches it. While detached, a reading anchor (the first visible element and its offset) is restored after any
// layout change above it, and "New steps below" appears when content arrives below the viewport.
import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { CLIENT_KEYS, type Store } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { MemoryChips } from "../memory/MemoryChip.js";
import { Button, EmptyState, FIELD_LOOK } from "../primitives/index.js";
import { shallowArray, useSelect, useStoreHolder } from "../store.js";
import { Bubble } from "./Message.js";
import { TurnView } from "./Turn.js";
import { findRows, useTranscriptWindow } from "./window.js";

interface Anchor {
  element: Element;
  offset: number;
}

export function useStickyScroll(container: React.RefObject<HTMLElement | null>, _version: unknown) {
  const attached = useRef(true);
  const anchor = useRef<Anchor | null>(null);
  const [below, setBelow] = useState(false);

  const capture = useCallback(() => {
    const el = container.current;
    if (el === null) return;
    const top = el.getBoundingClientRect().top;
    const nodes = [...el.querySelectorAll("[data-anchor]")];
    // The first item that starts inside the view: a disclosure opening in a partly visible item above it never moves
    // the line being read. Only when no item starts in view is the partly visible one the anchor.
    const first = nodes.find((n) => n.getBoundingClientRect().top >= top) ?? nodes.find((n) => n.getBoundingClientRect().bottom > top);
    anchor.current = first === undefined ? null : { element: first, offset: first.getBoundingClientRect().top - top };
  }, [container]);

  const onScroll = useCallback(() => {
    const el = container.current;
    if (el === null) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    attached.current = distance <= CLIENT_KEYS.ui_stick_bottom_px;
    if (attached.current) setBelow(false);
    capture();
  }, [container, capture]);

  const reconcile = useCallback(() => {
    const el = container.current;
    if (el === null) return;
    if (attached.current) {
      el.scrollTop = el.scrollHeight;
      return;
    }
    const a = anchor.current;
    if (a !== null && a.element.isConnected) {
      const now = a.element.getBoundingClientRect().top - el.getBoundingClientRect().top;
      el.scrollTop += now - a.offset; // keep the line the student is reading where it was
    }
    if (el.scrollHeight - el.scrollTop - el.clientHeight > CLIENT_KEYS.ui_stick_bottom_px) setBelow(true);
  }, [container]);
  useLayoutEffect(reconcile);

  const jump = useCallback(() => {
    const el = container.current;
    if (el === null) return;
    attached.current = true;
    el.scrollTop = el.scrollHeight;
    setBelow(false);
  }, [container]);

  const detach = useCallback(() => { attached.current = false; anchor.current = null; }, []);
  return { onScroll, below, jump, reconcile, detach };
}

/** The transcript as one keyed list: each turn's student message, then the turn; unsent messages last. A message keeps
 * its key (its client id) from the moment it is sent, so accepting it moves the same component and DOM node (UIA-05). */
function items(store: Store, sessionId: string): string[] {
  const session = store.sessions[sessionId];
  if (session === undefined) return NONE;
  const out: string[] = [];
  const placed = new Set<string>();
  const byId = new Map(Object.values(store.messages).map((m) => [m.message_id, m.key]));
  for (const turnId of session.turn_ids) {
    const turn = store.turns[turnId];
    let key: string | null = null;
    if (turn?.client_message_id != null && store.messages[turn.client_message_id] !== undefined) key = turn.client_message_id;
    else if (turn?.user_message_id != null) key = byId.get(turn.user_message_id) ?? null;
    if (key !== null) {
      out.push(`m:${key}`);
      placed.add(key);
    }
    out.push(`t:${turnId}`);
  }
  for (const key of session.pending_ids) if (!placed.has(key)) out.push(`m:${key}`);
  return out;
}

type TranscriptProps = { sessionId: string; footer?: ReactNode; focusTurnId?: string };
export function Transcript(props: TranscriptProps) { return <TranscriptSession key={props.sessionId} {...props} />; }
function TranscriptSession({ sessionId, footer, focusTurnId }: TranscriptProps) {
  const t = useT();
  const list = useSelect((s) => items(s, sessionId), shallowArray);
  const version = useSelect((s) => s.cursors[sessionId]?.seq ?? 0);
  const ref = useRef<HTMLDivElement>(null);
  const sticky = useStickyScroll(ref, `${version}:${list.length}`);
  const holder = useStoreHolder();
  const [query, setQuery] = useState(""); const [match, setMatch] = useState(0);
  const hits = findRows(holder.getState(), list, query);
  const hit = hits[Math.min(match, Math.max(0, hits.length - 1))] ?? null;
  const [browseTarget, setBrowseTarget] = useState<string | null>(null);
  const target = focusTurnId !== undefined ? `t:${focusTurnId}` : browseTarget ?? hit;
  useLayoutEffect(() => { if (target !== null) sticky.detach(); }, [target, sticky.detach]);
  const windowed = useTranscriptWindow({ root: ref, list, store: holder.getState(), target, onPositioned: sticky.onScroll, focusTarget: browseTarget !== null });
  const onScroll = () => { sticky.onScroll(); windowed.onScroll(); };
  const browse = (key: string | null) => { if (key === null) return; sticky.detach(); setBrowseTarget(key); };
  const move = (delta: number) => { sticky.detach(); setBrowseTarget(null); setMatch((n) => (n + delta + hits.length) % hits.length); };

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {list.filter((key) => key.startsWith("t:")).length > 40 && <div role="search" className="flex flex-wrap items-end gap-2 border-b border-line px-6 py-2">
        <label className="flex flex-1 flex-col gap-1 text-meta text-muted">{t("transcript.find")}
          <input className={FIELD_LOOK} value={query} maxLength={400} onChange={(e) => { sticky.detach(); setBrowseTarget(null); setMatch(0); setQuery(e.target.value); }} />
        </label>
        {query.trim() !== "" && <><span role="status" className="text-meta text-muted">{t("transcript.matches", { number: hits.length === 0 ? 0 : Math.min(match + 1, hits.length), count: hits.length })}</span>
          <Button size="sm" disabled={hits.length === 0} onClick={() => move(-1)}>{t("transcript.previous_match")}</Button>
          <Button size="sm" disabled={hits.length === 0} onClick={() => move(1)}>{t("transcript.next_match")}</Button></>}
        <Button size="sm" disabled={windowed.previous === null} onClick={() => browse(windowed.previous)}>{t("transcript.earlier")}</Button>
        <Button size="sm" disabled={windowed.next === null} onClick={() => browse(windowed.next)}>{t("transcript.later")}</Button>
      </div>}

      {/* Keep positioned message/accessibility content inside this scroller;
          otherwise long turns can create overflow in the outer workspace. */}
      <div ref={ref} onScroll={onScroll} className="relative min-h-0 flex-1 overflow-y-auto px-6" role="log" aria-live="off" aria-label={t("transcript.label")} data-region="conversation" tabIndex={-1}>
        {/* The conversation's column: centred, at most `columnMax` wide (DSN-17), 32 px between turns (DSN-08). */}
        <div className="mx-auto flex w-full max-w-(--layout-columnMax) flex-col pt-6 pb-4">
          {list.length === 0 && <EmptyState message={t("transcript.empty")} />}
          {windowed.entries.map((entry) => entry.spacer !== undefined ? <div key={entry.key} aria-hidden="true" data-virtual-spacer style={{ height: entry.spacer, flexShrink: 0 }} /> :
            entry.key.startsWith("m:") ? (
              <div key={entry.key} data-row-key={entry.key} data-anchor className="pb-8">
                <Bubble messageKey={entry.key.slice(2)} />
              </div>
            ) : (
              <div key={entry.key} data-row-key={entry.key} data-anchor className="pb-8">
                <TurnView turnId={entry.key.slice(2)} />
              </div>
            ),
          )}
          <MemoryChips sessionId={sessionId} turnId={null} />
          {footer}
        </div>
      </div>
      {sticky.below && (
        <JumpToLatest onClick={() => { sticky.jump(); windowed.onScroll(); }} />
      )}
    </div>
  );
}

const NONE: string[] = [];


export function JumpToLatest({ onClick }: { onClick: () => void }) {
  const t = useT();
  return <button type="button" data-jump-to-latest="" onClick={onClick} className="absolute bottom-3 left-1/2 inline-flex min-h-11 -translate-x-1/2 items-center gap-1 rounded-pill bg-ink px-3 py-1 text-meta text-canvas shadow-overlay-1"><Icon name="new-below" size={16} />{t("transcript.new_below")}</button>;
}
