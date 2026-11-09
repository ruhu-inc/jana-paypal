// The active locale's translator for components (LOC-01): every user-visible string goes through `t(key, args)`.
import { createContext, useContext, useMemo, type ReactNode } from "react";
import { translator, type Locale, type Translate } from "@jana/i18n";

const LocaleContext = createContext<Translate>(translator("en"));

export function LocaleProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  const t = useMemo(() => translator(locale), [locale]);
  return <LocaleContext.Provider value={t}>{children}</LocaleContext.Provider>;
}

export function useT(): Translate {
  return useContext(LocaleContext);
}

/** A given translator (tests count calls with it to see which rows render). */
export function TranslateProvider({ t, children }: { t: Translate; children: ReactNode }) {
  return <LocaleContext.Provider value={t}>{children}</LocaleContext.Provider>;
}
