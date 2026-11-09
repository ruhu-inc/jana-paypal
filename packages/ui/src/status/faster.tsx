// "Use the faster tutor" (UXS-03, MDL-05): the app supplies how to remember the choice, and supplies it only where
// a faster tutor can take over; without a provider the progress line never offers it.
import { createContext, useContext, type ReactNode } from "react";

const FasterTutor = createContext<(() => void) | null>(null);

export function FasterTutorProvider({ use, children }: { use: (() => void) | null; children: ReactNode }) {
  return <FasterTutor.Provider value={use}>{children}</FasterTutor.Provider>;
}

export function useFasterTutor(): (() => void) | null {
  return useContext(FasterTutor);
}
