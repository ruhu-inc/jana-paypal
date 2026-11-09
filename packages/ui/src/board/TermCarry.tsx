// Term carry between lines (UXS-61, D103): when a new line moved a term across the equals sign or cancelled it, the
// moved term travels from its place in the line before to its place in the new line, or is struck through there,
// before the line settles. The moves are the engine's `term_moves`, from the step checker's comparison of the two
// checked lines: the client never infers one. Transform and opacity only; the Board leaves it out with reduced motion,
// in low-power mode and for lines already there when it opens. Presentation only, hidden from assistive technology:
// the new line is read as usual.

import { useEffect, useState, type RefObject } from "react";
import type { StepRec, TermMoveRec } from "@jana/client-core";
import { Tex } from "../maths/Tex.js";

export const TERM_CARRY_MS = 600;

interface Placed {
  move: TermMoveRec;
  text: string;
  from: { x: number; y: number };
  to: { x: number; y: number };
}

/** Where term `termId` of a line sits on screen, by its share of the line's LaTeX (KaTeX gives no term boxes). */
function termAt(box: HTMLElement, step: StepRec, termId: string): { x: number; y: number; text: string } | null {
  const term = step.terms?.find((t) => t.term_id === termId);
  const el = box.querySelector<HTMLElement>(`[data-step-id="${step.step_id}"] .board-math`);
  if (term === undefined || el === null || step.latex.length === 0) return null;
  const outer = box.getBoundingClientRect(), inner = el.getBoundingClientRect();
  return { x: inner.left - outer.left + (inner.width * term.start) / step.latex.length, y: inner.top - outer.top,
    text: step.latex.slice(term.start, term.end) };
}

export function TermCarry({ step, steps, container }: { step: StepRec; steps: readonly StepRec[]; container: RefObject<HTMLElement | null> }) {
  const [placed, setPlaced] = useState<Placed[]>([]);
  const [phase, setPhase] = useState<"start" | "moving" | "done">("start");
  // A passive effect: the board's ref is attached by then (as for the pen).
  useEffect(() => {
    const box = container.current;
    if (box === null) return undefined;
    const found: Placed[] = [];
    for (const move of step.term_moves ?? []) {
      const [sourceId, termId] = move.from.split(".");
      const source = steps.find((s) => s.step_id === sourceId);
      const from = source === undefined || termId === undefined ? null : termAt(box, source, termId);
      if (from === null) continue;
      const to = move.to === null ? from : termAt(box, step, move.to);
      if (to === null) continue;
      found.push({ move, text: from.text, from, to });
    }
    setPlaced(found);
    const frame = requestAnimationFrame(() => setPhase("moving"));
    const timer = setTimeout(() => setPhase("done"), TERM_CARRY_MS + 100);
    return () => { cancelAnimationFrame(frame); clearTimeout(timer); };
  }, [container, step, steps]);
  if (phase === "done") return null;
  return (
    <>
      {placed.map(({ move, text, from, to }) => {
        const at = phase === "moving" ? to : from;
        return (
          <span key={move.from} aria-hidden="true" data-term-move={move.kind} data-from={move.from} data-to={move.to ?? ""}
            className={`pointer-events-none absolute start-0 top-0 rounded-control bg-highlight-soft px-0.5 text-ink ${move.kind === "cancel" ? "line-through decoration-2" : ""}`}
            style={{ transform: `translate(${at.x}px, ${at.y}px)`, opacity: move.kind === "cancel" && phase === "moving" ? 0 : 1,
              transition: `transform ${TERM_CARRY_MS}ms cubic-bezier(0.3, 0.7, 0.4, 1), opacity ${TERM_CARRY_MS}ms linear` }}>
            <Tex tex={text} display={false} />
          </span>
        );
      })}
    </>
  );
}
