// The way back to the "What your tutor knows" page from the conversation (MEM-28, MEM-11): the app supplies how to
// open it; without a provider (a component shown on its own) the lines that link there render without the link.
import { createContext, useContext, type ReactNode } from "react";

const KnowsLink = createContext<(() => void) | null>(null);

export function KnowsLinkProvider({ open, children }: { open: () => void; children: ReactNode }) {
  return <KnowsLink.Provider value={open}>{children}</KnowsLink.Provider>;
}

export function useOpenKnows(): (() => void) | null {
  return useContext(KnowsLink);
}
