// The app frame's breakpoints (DSN-17, UXS-01, UXS-35), from the token contract's `layout.breakpoints`: compact below
// 640 px (a bottom tab bar replaces the sidebar), medium to 959 px (the sidebar is an overlay drawer and the board a
// split board), expanded to 1279 px (the sidebar starts as the rail) and wide from 1280 px (sidebar, conversation and
// board side by side). Read from the window's width as it changes; a host with no media queries is wide.
import { useEffect, useState } from "react";
import { TOKENS } from "../generated/tokens.js";

export type LayoutClass = "compact" | "medium" | "expanded" | "wide";

const ORDER: readonly Exclude<LayoutClass, "compact">[] = ["wide", "expanded", "medium"];

function query(layout: Exclude<LayoutClass, "compact">): string {
  return `(min-width: ${TOKENS.layout.breakpoints[layout]}px)`;
}

/** The layout class for a window `width` px wide. */
export function layoutOf(width: number): LayoutClass {
  return ORDER.find((layout) => width >= TOKENS.layout.breakpoints[layout]) ?? "compact";
}

function current(): LayoutClass {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "wide";
  return ORDER.find((layout) => window.matchMedia(query(layout)).matches) ?? "compact";
}

/** The window's layout class, following resizes. */
export function useLayoutClass(): LayoutClass {
  const [layout, setLayout] = useState<LayoutClass>(current);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const lists = ORDER.map((layout) => window.matchMedia(query(layout)));
    const update = () => setLayout(current());
    for (const list of lists) list.addEventListener("change", update);
    update();
    return () => {
      for (const list of lists) list.removeEventListener("change", update);
    };
  }, []);
  return layout;
}

/** Keep conversation and working beside one another from medium width; phones use the sheet. */
export function boardBeside(layout: LayoutClass): boolean {
  return layout !== "compact";
}
