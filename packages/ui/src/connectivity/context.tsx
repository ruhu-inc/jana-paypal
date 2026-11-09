// The app's connectivity slice for components (UIA-23): the indicator, the popover and a turn's egress line select from
// it. Without a provider (the gallery, a bare transcript) nothing is selected and nothing is shown.
import { createContext, useCallback, useContext, useRef, useSyncExternalStore, type ReactNode } from "react";
import type { ConnectivityController, ConnectivitySlice } from "@jana/client-core";

const ConnectivityContext = createContext<ConnectivityController | null>(null);

export function ConnectivityProvider({ connectivity, children }: { connectivity: ConnectivityController; children: ReactNode }) {
  return <ConnectivityContext.Provider value={connectivity}>{children}</ConnectivityContext.Provider>;
}

export function useConnectivityController(): ConnectivityController | null {
  return useContext(ConnectivityContext);
}

export function useRequiredConnectivity(): ConnectivityController {
  const controller = useContext(ConnectivityContext);
  if (controller === null) throw new Error("ConnectivityProvider missing");
  return controller;
}

const none = () => () => undefined;

/** Select from the slice; re-renders only when the selected value changes. Null without a provider. */
export function useConnectivitySelect<T>(selector: (slice: ConnectivitySlice) => T): T | null {
  const controller = useContext(ConnectivityContext);
  const last = useRef<{ value: T } | null>(null);
  const get = useCallback((): T | null => {
    if (controller === null) return null;
    const next = selector(controller.getView());
    if (last.current !== null && Object.is(last.current.value, next)) return last.current.value;
    last.current = { value: next };
    return next;
  }, [controller, selector]);
  return useSyncExternalStore(controller === null ? none : controller.subscribe, get, get);
}
