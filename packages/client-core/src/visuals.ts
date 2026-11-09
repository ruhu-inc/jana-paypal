// Board-synced visual steps (VSL-20, UIA-20): one action moves a visual whatever moved it (the board, a `visual.step`
// event or the student), so the visual and the board stay in step. When the board's current line becomes a line a
// visual step is bound to, the visual moves to that step; when the visual moves to a step bound to a board line, that
// line becomes the board's current one. Unbound board lines leave visuals where they are.
import type { BoardRec, Store, VisualRec } from "./store.js";

export type VisualMoveSource = "tutor" | "student" | "board";

interface BoundStep {
  board_step?: string;
}

function steps(visual: VisualRec): BoundStep[] {
  return ((visual.spec as { steps?: BoundStep[] }).steps ?? []) as BoundStep[];
}

/** The board line a visual step is bound to, or null. */
export function boundLine(visual: VisualRec, step: number): string | null {
  return steps(visual)[step]?.board_step ?? null;
}

function boardWith(store: Store, stepId: string): BoardRec | undefined {
  return Object.values(store.boards).find((b) => b.steps[stepId] !== undefined || b.step_ids.includes(stepId));
}

/** Move a visual to `step` (clamped to its steps); a bound board line becomes the board's current line. */
export function moveVisual(store: Store, visualId: string, step: number, _source: VisualMoveSource): Store {
  const visual = store.visuals[visualId];
  if (visual === undefined) return store;
  const count = steps(visual).length;
  const target = count === 0 ? 0 : Math.max(-1, Math.min(count - 1, step));
  let next: Store = target === visual.step ? store : { ...store, visuals: { ...store.visuals, [visualId]: { ...visual, step: target } } };
  const line = boundLine(visual, target);
  if (line !== null) {
    // The bound line becomes the board's current one and is highlighted until the next move (VSL-20).
    const board = boardWith(next, line);
    if (board !== undefined) next = { ...next, boards: { ...next.boards, [board.board_id]: { ...board, current: line, highlight: { step_ids: [line], ttl_ms: null, ts: 0 } } } };
  }
  return next;
}

/** The board moves to `stepId` (a new line, Back or Forward, or a click on a line): every visual bound to it follows. */
export function selectBoardLine(store: Store, boardId: string, stepId: string): Store {
  const board = store.boards[boardId];
  if (board === undefined) return store;
  let next: Store = board.current === stepId ? store : { ...store, boards: { ...store.boards, [boardId]: { ...board, current: stepId } } };
  for (const visual of Object.values(store.visuals)) {
    const index = steps(visual).findIndex((s) => s.board_step === stepId);
    if (index >= 0 && visual.step !== index) next = { ...next, visuals: { ...next.visuals, [visual.visual_id]: { ...visual, step: index } } };
  }
  return next;
}

/** A visual that arrives after its board lines starts at the step bound to the board's current line. */
export function syncNewVisual(store: Store, visualId: string): Store {
  const visual = store.visuals[visualId];
  if (visual === undefined) return store;
  for (const board of Object.values(store.boards)) {
    if (board.current === null) continue;
    const index = steps(visual).findIndex((s) => s.board_step === board.current);
    if (index >= 0) return { ...store, visuals: { ...store.visuals, [visualId]: { ...visual, step: index } } };
  }
  return store;
}

/** Back or Forward on the board: the line before or after the current one. */
export function stepBoard(store: Store, boardId: string, delta: -1 | 1): Store {
  const board = store.boards[boardId];
  if (board === undefined || board.step_ids.length === 0) return store;
  const at = board.current === null ? board.step_ids.length - 1 : board.step_ids.indexOf(board.current);
  const to = board.step_ids[Math.max(0, Math.min(board.step_ids.length - 1, at + delta))];
  return to === undefined ? store : selectBoardLine(store, boardId, to);
}
