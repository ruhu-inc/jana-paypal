// The announcement of a note saved because the student asked (MEM-11, UXS-28): the saved wording with Undo while the
// core's undo window is open (`undo_until_ts`, server time); after Undo, or a forget from the knows page, a short line
// says it is gone. Background notes are never announced here; they are listed on the knows page. When the window has
// closed the note can still be forgotten there, so the chip links to it (G94).
import { useEffect, useState } from "react";
import { connectionKey, undoLeftMs, type MemoryRec, type Store } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, ErrorState, Loader } from "../primitives/index.js";
import { shallowArray, useSelect, useSession } from "../store.js";
import { useOpenKnows } from "./link.js";

export type UndoState = "idle" | "undoing" | "failed";

export function MemoryChipView({ memory, undoLeftMs: left, state, onUndo, onOpen }: { memory: MemoryRec; undoLeftMs: number; state: UndoState; onUndo?: (() => void) | undefined; onOpen?: (() => void) | null | undefined }) {
  const t = useT();
  if (memory.state === "forgotten") {
    return (
      <p role="status" className="my-2 flex items-center gap-2 text-ui text-muted" data-memory-note={memory.note_id} data-memory-state="forgotten">
        <Icon name="verified" size={16} />
        {t("memory.forgotten")}
      </p>
    );
  }
  const canUndo = left > 0 && onUndo !== undefined;
  return (
    <div className="my-2 flex flex-col gap-2 rounded-card border border-line bg-canvas px-3 py-2 text-ui" data-memory-note={memory.note_id} data-memory-state={state}>
      <div className="flex flex-wrap items-center gap-2">
        <Icon name="notes" size={16} />
        <span className="flex-1">{t("memory.saved", { text: memory.text })}</span>
        {state === "undoing" ? (
          <Loader label={t("memory.undoing")} />
        ) : canUndo && state === "idle" ? (
          <Button onClick={onUndo}>{t("memory.undo")}</Button>
        ) : (
          onOpen != null && (
            <button type="button" onClick={onOpen} className="jana-press min-h-9 rounded-control font-medium text-ink underline">
              {t("knows.open")}
            </button>
          )
        )}
      </div>
      {state === "failed" && <ErrorState message={t("memory.undo_failed")} {...(canUndo && onUndo !== undefined ? { action: { label: t("action.try_again"), onClick: onUndo } } : {})} />}
    </div>
  );
}

/** One announced note, from the store; Undo goes through the open session (`DELETE /v1/memory/{id}`). */
export function MemoryChip({ noteId }: { noteId: string }) {
  const memory = useSelect((s) => s.memory[noteId]);
  const connection = useSelect((s) => (memory === undefined ? undefined : s.connection[connectionKey(memory.session_id)]));
  const session = useSession();
  const open = useOpenKnows();
  const [state, setState] = useState<UndoState>("idle");
  const [now, setNow] = useState(() => Date.now());
  const left = memory === undefined ? 0 : undoLeftMs(memory, connection, now);
  useEffect(() => {
    if (left <= 0) return;
    const timer = setTimeout(() => setNow(Date.now()), left + 1); // the Undo goes when the window closes
    return () => clearTimeout(timer);
  }, [left]);
  if (memory === undefined) return null;
  const undo =
    session === null
      ? undefined
      : () => {
          setState("undoing");
          session.forgetMemory(noteId).then(
            () => setState("idle"),
            () => setState("failed"),
          );
        };
  return <MemoryChipView memory={memory} undoLeftMs={left} state={state} onUndo={undo} onOpen={open} />;
}

function noteIds(store: Store, sessionId: string, turnId: string | null): string[] {
  const turns = store.sessions[sessionId]?.turn_ids ?? [];
  return Object.values(store.memory)
    .filter((m) => m.session_id === sessionId && (turnId === null ? m.turn_id === null || !turns.includes(m.turn_id) : m.turn_id === turnId))
    .sort((a, b) => a.seq - b.seq)
    .map((m) => m.note_id);
}

/** The notes announced in one turn, or (with `turnId` null) those announced between turns. */
export function MemoryChips({ sessionId, turnId }: { sessionId: string; turnId: string | null }) {
  const ids = useSelect((s) => noteIds(s, sessionId, turnId), shallowArray);
  return (
    <>
      {ids.map((id) => (
        <MemoryChip key={id} noteId={id} />
      ))}
    </>
  );
}
