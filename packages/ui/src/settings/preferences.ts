// The open profile's display preferences, applied to the whole app (DSN-05, DSN-07, LOC-17, LOC-18, LOC-20, DSN-09):
// the theme sets `data-theme` (always present: "Match this computer" until a profile chooses otherwise, following the
// operating system live); "Board and chat text size" sets `--text-scale`, which scales body text; "Easy-read text"
// sets `data-easy-read` and "Reduce motion" (or low-power mode) sets `data-reduced-motion` on the root element, which
// the stylesheet and the motion primitives read. Only the theme applies while no profile is open, and a switch clears
// the rest.
import { useEffect } from "react";
import type { ProfileController } from "@jana/client-core";
import { TOKENS } from "../generated/tokens.js";
import { useProfiles } from "../profiles/ProfilePicker.js";
import { useAppliedTheme, useThemePreference } from "./theme.js";

/** The text scale a stored value asks for, held to the token contract's range and step (DSN-07). */
export function textScaleOf(value: number | undefined): number {
  const { min, max, step, default: fallback } = TOKENS.textScale;
  if (value === undefined || !Number.isFinite(value)) return fallback;
  const clamped = Math.min(max, Math.max(min, value));
  return Number((Math.round(clamped / step) * step).toFixed(2));
}

export function useAppliedPreferences(profiles: ProfileController, root: HTMLElement = document.documentElement): void {
  const view = useProfiles(profiles);
  const missing = view.active !== null && view.settings === null;
  useEffect(() => {
    if (missing) profiles.loadSettings().catch(() => undefined);
  }, [profiles, missing]);
  useAppliedTheme(useThemePreference(view.active, view.settings), root);
  const easyRead = view.settings?.easy_read === true;
  const reducedMotion = view.settings?.reduced_motion === true || view.settings?.low_power === true;
  const scale = textScaleOf(view.settings?.text_scale);
  useEffect(() => {
    if (easyRead) root.setAttribute("data-easy-read", "true");
    else root.removeAttribute("data-easy-read");
    if (reducedMotion) root.setAttribute("data-reduced-motion", "true");
    else root.removeAttribute("data-reduced-motion");
  }, [root, easyRead, reducedMotion]);
  useEffect(() => {
    if (scale === TOKENS.textScale.default) root.style.removeProperty("--text-scale");
    else root.style.setProperty("--text-scale", String(scale));
  }, [root, scale]);
}
