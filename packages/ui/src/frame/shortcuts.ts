// The frame's global keys (TABLE LOC-12a): Ctrl+1 to Ctrl+5 open Home, Learn, Practice, Mock exam and Progress (those
// on this build), Ctrl+, opens Settings, "/" puts the cursor in the Ask box, "?" opens the keyboard help, and F6 /
// Shift+F6 move between the regions (sidebar, conversation, board, dock). Ctrl reads Cmd on macOS. "/" and "?" are
// single-character keys: they do nothing while a text field has focus, and the student can turn them off in Settings →
// Accessibility and language ("Character-key shortcuts", WCAG 2.1.4). That choice is a student preference kept with
// the profile's settings (`Settings.character_shortcuts`, D92), so it applies again on the next launch; a core older
// than API 1.3 does not return the key, and then a choice lasts for the session, like the theme.
import { useEffect, useRef, useSyncExternalStore } from "react";
import type { Settings } from "@jana/contracts-ts";

/** Whether the key landed in something that takes text: a single-character shortcut leaves it alone. */
export function inTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.closest("input, textarea, select, [contenteditable='true'], [role='textbox']") !== null;
}

const REGIONS = "[data-region]";

/** Move focus to the next (or previous) region of the frame, wrapping round. */
export function moveRegion(back: boolean): void {
  // Regions in document order; a nested one (the board sheet inside the conversation) counts on its own.
  const regions = [...document.querySelectorAll<HTMLElement>(REGIONS)];
  if (regions.length === 0) return;
  const active = document.activeElement;
  let at = -1;
  regions.forEach((r, i) => {
    if (r.contains(active)) at = i; // the innermost region holding the focus is the last one that contains it
  });
  const next = regions[(at + (back ? -1 : 1) + regions.length) % regions.length];
  if (next === undefined) return;
  // The dock's region is the composer: focus goes to its text, ready to type.
  const target = next.matches("[data-region='dock']") ? (next.querySelector<HTMLElement>("[contenteditable='true'], textarea") ?? next) : next;
  if (!target.hasAttribute("tabindex") && !target.matches("button, [href], input, textarea, select, [contenteditable='true']")) target.tabIndex = -1;
  target.focus();
}

// --- the character-key shortcuts preference (LOC-12, TABLE UXS-46a) ----------------------------------------------------

/** The stored choice (`Settings.character_shortcuts`; absent from an older core). */
function storedCharacterKeys(settings: Settings | null): boolean | undefined {
  const value = (settings as { character_shortcuts?: unknown } | null)?.character_shortcuts;
  return typeof value === "boolean" ? value : undefined;
}

/** Whether the core keeps the choice with the profile's settings. */
export function characterKeysStored(settings: Settings | null): boolean {
  return storedCharacterKeys(settings) !== undefined;
}

const sessionChoice = new Map<string, boolean>();
const listeners = new Set<() => void>();

/** A choice made while the core does not keep it (an older core): held for the session, per profile. */
export const sessionCharacterKeys = {
  get(profileId: string | null): boolean | undefined {
    return profileId === null ? undefined : sessionChoice.get(profileId);
  },
  set(profileId: string, on: boolean): void {
    sessionChoice.set(profileId, on);
    for (const listener of listeners) listener();
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

/** The open profile's choice: on unless the student turned the character keys off. */
export function useCharacterKeys(profileId: string | null, settings: Settings | null): boolean {
  const held = useSyncExternalStore(sessionCharacterKeys.subscribe, () => sessionCharacterKeys.get(profileId), () => undefined);
  return storedCharacterKeys(settings) ?? held ?? true;
}

// --- the keys ----------------------------------------------------------------------------------------------------------

export interface GlobalKeys {
  places: readonly ((() => void) | null)[];
  onSettings: () => void;
  /** "?": the keyboard help; absent where there is none. */
  onHelp?: () => void;
  /** Whether the single-character keys ("/" and "?") are on (LOC-12); they are unless the student turned them off. */
  characterKeys?: boolean;
}

export function useGlobalShortcuts({ places, onSettings, onHelp, characterKeys = true }: GlobalKeys): void {
  const handlers = useRef({ places, onSettings, onHelp, characterKeys });
  handlers.current = { places, onSettings, onHelp, characterKeys };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const command = event.ctrlKey || event.metaKey;
      if (command && !event.shiftKey && !event.altKey && /^Digit[1-5]$/.test(event.code)) {
        const open = handlers.current.places[Number(event.code.slice(5)) - 1];
        if (open != null) {
          event.preventDefault();
          open();
        }
        return;
      }
      if (command && !event.shiftKey && event.key === ",") {
        event.preventDefault();
        handlers.current.onSettings();
        return;
      }
      if (event.key === "F6") {
        event.preventDefault();
        moveRegion(event.shiftKey);
        return;
      }
      // The single-character keys: never while typing, never with a modifier, and only while they are on.
      if (command || event.altKey || !handlers.current.characterKeys || inTextField(event.target) || event.defaultPrevented) return;
      if (event.key === "/") {
        const ask = document.querySelector<HTMLElement>("[data-composer] [contenteditable='true'], [data-composer] textarea");
        if (ask !== null) {
          event.preventDefault();
          ask.focus();
        }
        return;
      }
      if (event.key === "?" && handlers.current.onHelp !== undefined && document.querySelector("[aria-modal='true']") === null) {
        event.preventDefault();
        handlers.current.onHelp();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
