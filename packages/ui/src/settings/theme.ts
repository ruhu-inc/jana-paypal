// The theme preference (DSN-05; Settings → Accessibility and language, TABLE UXS-46a): "Match this computer" (the
// default), "Light" or "Dark". The root element always carries the resolved `data-theme` ("light" or "dark"), which
// the generated token stylesheet reads; "Match this computer" follows the operating system's setting live, without a
// reload. It is a student preference kept with the open profile's settings (`Settings.theme`, D91), so it applies again
// on the next launch; a core older than API 1.2 does not return the key, and then a choice lasts for the session.
import { useEffect, useSyncExternalStore } from "react";
import type { Settings } from "@jana/contracts-ts";

export const THEME_PREFERENCES = ["system", "light", "dark"] as const;
export type ThemePreference = (typeof THEME_PREFERENCES)[number];
export type Theme = "light" | "dark";

const DARK_QUERY = "(prefers-color-scheme: dark)";

/** The stored preference, or "Match this computer" when the settings do not carry one (yet). */
export function themeOf(settings: Settings | null): ThemePreference {
  const value = (settings as { theme?: unknown } | null)?.theme;
  return THEME_PREFERENCES.includes(value as ThemePreference) ? (value as ThemePreference) : "system";
}

/** Whether the core keeps the theme with the profile's settings (`Settings.theme`; absent from an older core). */
export function themeStored(settings: Settings | null): boolean {
  return settings !== null && "theme" in settings;
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): Theme {
  return preference === "system" ? (systemDark ? "dark" : "light") : preference;
}

function media(): MediaQueryList | null {
  return typeof window === "undefined" || typeof window.matchMedia !== "function" ? null : window.matchMedia(DARK_QUERY);
}

function subscribeSystem(onChange: () => void): () => void {
  const list = media();
  if (list === null) return () => undefined;
  list.addEventListener("change", onChange);
  return () => list.removeEventListener("change", onChange);
}

/** This computer's light or dark setting, followed live. */
export function useSystemDark(): boolean {
  return useSyncExternalStore(subscribeSystem, () => media()?.matches === true, () => false);
}

/** Sets `data-theme` on the root element from the preference, and keeps it in step with the system setting while the
 * preference is "Match this computer". */
export function useAppliedTheme(preference: ThemePreference, root: HTMLElement = document.documentElement): Theme {
  const theme = resolveTheme(preference, useSystemDark());
  useEffect(() => {
    root.setAttribute("data-theme", theme);
  }, [root, theme]);
  return theme;
}

// A choice made while the core does not keep it (an older core): held for the session, per profile.
const sessionChoice = new Map<string, ThemePreference>();
const listeners = new Set<() => void>();

export const sessionTheme = {
  get(profileId: string | null): ThemePreference | undefined {
    return profileId === null ? undefined : sessionChoice.get(profileId);
  },
  set(profileId: string, preference: ThemePreference): void {
    sessionChoice.set(profileId, preference);
    for (const listener of listeners) listener();
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

/** The open profile's preference: the stored value once the core keeps it, else this session's choice. */
export function useThemePreference(profileId: string | null, settings: Settings | null): ThemePreference {
  const held = useSyncExternalStore(sessionTheme.subscribe, () => sessionTheme.get(profileId), () => undefined);
  if (themeStored(settings)) return themeOf(settings);
  return held ?? "system";
}
