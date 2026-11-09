// A one-line composer that grows with what is typed (the crowding review of 2026-11-01): the text area starts at one
// row and takes the height of its text up to a cap, then scrolls. Nothing is reserved while it is empty.
import { useEffect, type RefObject } from "react";

export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: string, maxPx = 160): void {
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    el.style.height = "";
    const height = el.scrollHeight;
    if (height > 0) { el.style.height = `${Math.min(height, maxPx)}px`; el.style.overflowY = height > maxPx ? "auto" : "hidden"; }
  }, [ref, value, maxPx]);
}
