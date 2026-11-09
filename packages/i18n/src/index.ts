// ICU message catalogues and CLDR formatting (LOC-01 to LOC-04). Every user-visible string is a key in a catalogue;
// plurals and numbers come from the engine's CLDR data through Intl, never from hand-written rules.
import { IntlMessageFormat } from "intl-messageformat";
import en from "../locales/en.json" with { type: "json" };
import { pseudoCatalogue } from "./pseudo.js";

export type Catalogue = Record<string, string>;
export type Args = Record<string, string | number | Date>;

/** Locales shipped to students. A locale under development ships behind a flag only (LOC-02). */
export const SHIPPED = ["en"] as const;
/** Development and gallery locales: the accented, padded pseudo-locale of LOC-04. */
export const PSEUDO = ["en-XA"] as const;
export type Locale = (typeof SHIPPED)[number] | (typeof PSEUDO)[number];

export const BASE: Catalogue = en;

const catalogues: Record<Locale, Catalogue> = { en: BASE, "en-XA": pseudoCatalogue(BASE) };

export function catalogue(locale: Locale): Catalogue {
  return catalogues[locale];
}

export type Translate = (key: string, args?: Args) => string;

export function translator(locale: Locale): Translate {
  const messages = catalogues[locale];
  const cache = new Map<string, IntlMessageFormat>();
  const intlLocale = locale === "en-XA" || locale === "en" ? "en-GB" : locale;
  return (key, args) => {
    const message = messages[key];
    if (message === undefined) return key; // a missing key fails CI (LOC-01); at run time the key shows, never a crash
    let format = cache.get(key);
    if (format === undefined) {
      format = new IntlMessageFormat(message, intlLocale, {
        date: {
          short: { day: "2-digit", month: "2-digit", year: "numeric" },
          medium: { day: "numeric", month: "long", year: "numeric" },
          long: { day: "numeric", month: "long", year: "numeric" },
          full: { weekday: "long", day: "numeric", month: "long", year: "numeric" },
        },
        time: {
          short: { hour: "numeric", minute: "2-digit", hour12: true },
          medium: { hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true },
          long: { hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short", hour12: true },
          full: { hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "long", hour12: true },
        },
      });
      cache.set(key, format);
    }
    return String(format.format(args));
  };
}

export { pseudoCatalogue, pseudoMessage } from "./pseudo.js";
export { argumentsOf, copyLintErrors, parityErrors, pluralErrors } from "./check.js";


/** Interface dates and wall times; maths and verified numerical strings never pass through these formatters. */
export function formatDate(value: Date | number | string, locale: Locale = "en", short = false): string {
  return new Intl.DateTimeFormat(locale === "en-XA" || locale === "en" ? "en-GB" : locale,
    { day: short ? "2-digit" : "numeric", month: short ? "2-digit" : "long", year: "numeric" }).format(typeof value === "string" ? new Date(value) : value);
}
export function formatTime(value: Date | number | string, locale: Locale = "en"): string {
  return new Intl.DateTimeFormat(locale === "en-XA" || locale === "en" ? "en-GB" : locale,
    { hour: "numeric", minute: "2-digit", hour12: true }).format(typeof value === "string" ? new Date(value) : value);
}
