// The tutor's pen (UXS-56, D103): a small pen-tip marker, the tutor's hand on the board. It rests at the end of the line
// being written and travels on a short arc to what the tutor points at (a board line, a term or a diagram part, by id),
// dwells while the ring shows and returns. It carries no meaning by itself: the ring and "Showing: …" do, so it is
// hidden from assistive technology. Reduced motion makes it jump; low-power mode hides it.

import { useEffect, useState, type RefObject } from "react";
import type { PenView } from "@jana/client-core";

export const PEN_TRAVEL_MS = 240;

function selectorOf(pen: PenView): string | null {
  const t = pen.target;
  if (t === null) return null;
  if (t.kind === "photo_region") return `[data-photo-region="${t.capture_id}:${t.region_id}"]`;
  if (t.kind === "kc") return `[data-kc-node="${t.kc_id}"]`;
  return t.kind === "step" ? `[data-step-id="${t.step_id}"] .board-math` : `[data-part="${t.part_id}"]`;
}

export function Pen({ pen, container, reducedMotion, lowPower = false }: {
  pen: PenView; container: RefObject<HTMLElement | null>; reducedMotion: boolean; lowPower?: boolean;
}) {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const selector = selectorOf(pen);
  // A passive effect: it runs once the board's own ref is attached, which a child's layout effect would precede.
  useEffect(() => {
    const box = container.current;
    const el = selector === null || box === null ? null : box.querySelector<HTMLElement>(selector);
    if (box === null || el === null) { setAt(null); return; }
    const outer = box.getBoundingClientRect(), inner = el.getBoundingClientRect();
    // The end of a line, or the centre of a part.
    setAt(pen.target?.kind === "step" ? { x: inner.right - outer.left, y: inner.bottom - outer.top } : { x: (inner.left + inner.right) / 2 - outer.left, y: (inner.top + inner.bottom) / 2 - outer.top });
  }, [container, selector, pen.target?.kind, pen.since_ms]);
  if (lowPower || at === null) return null;
  return (
    <span aria-hidden="true" data-pen="" data-pen-target={selector ?? undefined} data-pen-motion={reducedMotion ? "jump" : "travel"}
      className="pointer-events-none absolute start-0 top-0 size-2.5 rounded-pill bg-ink"
      style={{ transform: `translate(${at.x}px, ${at.y}px)`, transition: reducedMotion ? "none" : `transform ${PEN_TRAVEL_MS}ms cubic-bezier(0.3, 0.7, 0.4, 1)` }} />
  );
}
