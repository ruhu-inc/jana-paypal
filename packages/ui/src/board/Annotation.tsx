// The tutor's mark on a board line (VSL-40, D103): drawn round, under or beside the line by id, never over the
// student's writing; a mark on the student's line is in the misconception colour with the "Look here" word, so in
// greyscale it keeps its word (DSN-04); every mark is announced in words. Coordinates never come from the model: the
// mark takes its place from the line it targets.

import type { AnnotationRec, StepRec } from "@jana/client-core";
import { useT } from "../i18n.js";

/** The LaTeX of a term range ("t2" or "t2-t4") of a line, from the engine's term split; null for the whole line. */
export function termText(step: StepRec, terms: string | null): string | null {
  if (terms === null || step.terms === undefined) return null;
  const [first, last = first] = terms.split("-");
  const from = step.terms.find((t) => t.term_id === first);
  const to = step.terms.find((t) => t.term_id === last);
  return from === undefined || to === undefined ? null : step.latex.slice(from.start, to.end);
}

export function LineAnnotation({ step, annotation }: { step: StepRec; annotation: AnnotationRec }) {
  const t = useT();
  const student = step.owner === "student";
  const onTarget = annotation.target.step_id === step.step_id;
  const terms = onTarget ? annotation.target.terms : (annotation.to?.terms ?? null);
  const excerpt = termText(step, terms);
  // On the student's line every mark carries the "Look here" word (UXS-16); elsewhere the mark's own word.
  const word = student ? t("board.annotation.look_here") : annotation.mark === "margin_note" && annotation.note !== null ? annotation.note : t(`board.annotation.${annotation.mark}`);
  const stroke = student ? "stroke-misconception" : "stroke-accent";
  const announce = t("board.annotation.announce", { word, target: excerpt ?? t("board.annotation.whole_line") });
  return (
    <span data-annotation={annotation.mark} data-annotation-target={terms ?? "line"} className="block">
      {/* The drawn mark sits outside the text box (round it, under it or beside it), never filled over the writing. */}
      {annotation.mark !== "margin_note" && (
        <svg aria-hidden="true" data-annotation-mark="" className="pointer-events-none block h-2 w-full overflow-visible" preserveAspectRatio="none" viewBox="0 0 100 8">
          {annotation.mark === "circle" && <ellipse cx="50" cy="4" rx="49" ry="3.5" fill="none" className={stroke} strokeWidth="1.5" />}
          {(annotation.mark === "underline" || annotation.mark === "look_here") && <path d="M1 5 Q 50 7 99 4" fill="none" className={stroke} strokeWidth="1.5" />}
          {annotation.mark === "strike" && !student && <path d="M1 4 L 99 4" fill="none" className={stroke} strokeWidth="1.5" />}
          {annotation.mark === "arrow" && <path d="M1 6 Q 50 0 95 5 M 90 2 L 95 5 L 90 7" fill="none" className={stroke} strokeWidth="1.5" />}
        </svg>
      )}
      <span data-annotation-word="" className={`font-hand text-ui ${student ? "text-misconception" : "text-muted"}`}>
        {word}{excerpt !== null && <span className="ms-1 font-mono">{excerpt}</span>}
      </span>
      <span role="status" className="sr-only">{announce}</span>
    </span>
  );
}
