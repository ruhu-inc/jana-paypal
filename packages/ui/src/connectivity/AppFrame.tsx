// The outermost frame of every screen: the connectivity slice, and the connectivity indicator on every screen (UXS-06,
// ARC-10). Where the studio frame's sidebar is on screen its foot carries the indicator (UXS-01); on the screens
// before a profile opens (the picker, first run, the launch) a quiet status line at the foot of the screen carries it.
// "What leaves this device" opens from the indicator as a page over the screen, so nothing under it (a draft, the
// board) is lost.
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useState, type ReactNode } from "react";
import type { ProfileController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { ConnectivityIndicator } from "./ConnectivityIndicator.js";
import { ConnectivityProvider } from "./context.js";
import { LedgerPage } from "./Ledger.js";

interface LedgerApi {
  open: () => void;
  /** A sidebar that shows the indicator itself claims it, so the status column steps aside while it is mounted. */
  claim: () => () => void;
}

const LedgerContext = createContext<LedgerApi>({ open: () => undefined, claim: () => () => undefined });

/** The launch screen claims the frame's foot too, so the status line sits on its midnight surface instead of a strip
 * of paper under it (DSN-25's one brand moment fills the screen). */
const LaunchContext = createContext<() => () => void>(() => () => undefined);

export function useLaunchSurface(): void {
  const claim = useContext(LaunchContext);
  // Before paint, so the foot never shows one frame of paper under the launch screen.
  useLayoutEffect(() => claim(), [claim]);
}

/** Opens "What leaves this device" over the screen; a component that shows the indicator calls it from there. */
export function useLedger(): () => void {
  const api = useContext(LedgerContext);
  useEffect(() => api.claim(), [api]);
  return api.open;
}

export function AppFrame({ profiles, children }: { profiles: ProfileController; children: ReactNode }) {
  const t = useT();
  const [ledger, setLedger] = useState(false);
  const [claims, setClaims] = useState(0);
  useEffect(() => profiles.watchApp(), [profiles]);
  const open = useCallback(() => setLedger(true), []);
  const claim = useCallback(() => {
    setClaims((n) => n + 1);
    return () => setClaims((n) => n - 1);
  }, []);
  const [api] = useState<LedgerApi>(() => ({ open, claim }));
  const [launching, setLaunching] = useState(0);
  const launch = useCallback(() => {
    setLaunching((n) => n + 1);
    return () => setLaunching((n) => n - 1);
  }, []);
  return (
    <ConnectivityProvider connectivity={profiles.connectivity}>
      <LedgerContext.Provider value={api}>
        <LaunchContext.Provider value={launch}>
          <div className={`flex h-full min-h-0 flex-col ${launching > 0 ? "bg-midnight" : "bg-paper"}`} data-theme={launching > 0 ? "dark" : undefined}>
            <div className="relative min-h-0 min-w-0 flex-1">
              {children}
              {ledger && <LedgerPage connectivity={profiles.connectivity} onClose={() => setLedger(false)} />}
            </div>
            {claims === 0 && (
              <aside aria-label={t("app.rail")} className="flex shrink-0 items-center px-4 pb-3">
                <ConnectivityIndicator onOpenLedger={open} />
              </aside>
            )}
          </div>
        </LaunchContext.Provider>
      </LedgerContext.Provider>
    </ConnectivityProvider>
  );
}
