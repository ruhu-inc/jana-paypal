// Store access for components (UIA-01): each component selects the records it shows by id; a delta to one part
// re-renders only the component that selected that part.
import { createContext, useCallback, useContext, useRef, useSyncExternalStore, type ReactNode } from "react";
import type { SessionController, Store, StoreHolder } from "@jana/client-core";

interface Ctx {
  holder: StoreHolder;
  session: SessionController | null;
  /** A session shown read-only (session history, UXS-48): its replies offer no action that writes or sends. */
  readOnly: boolean;
}

const StoreContext = createContext<Ctx | null>(null);

export function StoreProvider({ holder, session, readOnly = false, children }: { holder: StoreHolder; session: SessionController | null; readOnly?: boolean; children: ReactNode }) {
  return <StoreContext.Provider value={{ holder, session, readOnly }}>{children}</StoreContext.Provider>;
}

function useCtx(): Ctx {
  const ctx = useContext(StoreContext);
  if (ctx === null) throw new Error("StoreProvider missing");
  return ctx;
}

/** Select a value; the component re-renders only when the selected value changes (by identity, or shallowly). */
export function useSelect<T>(selector: (store: Store) => T, equal: (a: T, b: T) => boolean = Object.is): T {
  const { holder } = useCtx();
  const last = useRef<{ value: T } | null>(null);
  const get = useCallback(() => {
    const next = selector(holder.getState());
    if (last.current !== null && equal(last.current.value, next)) return last.current.value;
    last.current = { value: next };
    return next;
  }, [holder, selector, equal]);
  return useSyncExternalStore(holder.subscribe, get, get);
}

export function shallowArray<T>(a: readonly T[], b: readonly T[]): boolean {
  return a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
}

/** The store holder, for components that apply a local action (the visual viewer's step moves, VSL-20). */
export function useStoreHolder(): StoreHolder {
  return useCtx().holder;
}

export function useSession(): SessionController | null {
  return useCtx().session;
}

export function useReadOnly(): boolean {
  return useContext(StoreContext)?.readOnly ?? false;
}

export function useOptionalSession(): SessionController | null { return useContext(StoreContext)?.session ?? null; }
