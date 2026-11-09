// Catalogue checks for CI: locale parity (LOC-02), plural branches valid for each locale's CLDR categories (LOC-03)
// and the interface copy rules of UXS-05.
import { parse, TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";

export type ArgumentTypes = Record<string, string>;

/** Every argument a message uses, with its ICU type (`string`, `number`, `plural`, `select`, `date`, `time`). */
export function argumentsOf(message: string): ArgumentTypes {
  const found: ArgumentTypes = {};
  const walk = (elements: MessageFormatElement[]) => {
    for (const el of elements) {
      switch (el.type) {
        case TYPE.argument:
          found[el.value] ??= "string";
          break;
        case TYPE.number:
          found[el.value] = "number";
          break;
        case TYPE.date:
          found[el.value] = "date";
          break;
        case TYPE.time:
          found[el.value] = "time";
          break;
        case TYPE.plural:
          found[el.value] = el.pluralType === "ordinal" ? "selectordinal" : "plural";
          for (const option of Object.values(el.options)) walk(option.value);
          break;
        case TYPE.select:
          found[el.value] = "select";
          for (const option of Object.values(el.options)) walk(option.value);
          break;
        case TYPE.tag:
          walk(el.children);
          break;
        default:
          break;
      }
    }
  };
  walk(parse(message, { ignoreTag: true }));
  return found;
}

export function parityErrors(base: Record<string, string>, locale: string, other: Record<string, string>): string[] {
  const errors: string[] = [];
  for (const key of Object.keys(base)) {
    const message = other[key];
    if (message === undefined) {
      errors.push(`${locale}: missing ${key}`);
      continue;
    }
    const want = JSON.stringify(Object.entries(argumentsOf(base[key] ?? "")).sort());
    const got = JSON.stringify(Object.entries(argumentsOf(message)).sort());
    if (want !== got) errors.push(`${locale}: ${key} has placeholders ${got}, base has ${want}`);
  }
  for (const key of Object.keys(other)) if (!(key in base)) errors.push(`${locale}: extra key ${key}`);
  return errors;
}

/** Plural branches must be CLDR categories of the locale (or exact `=n`), and `other` is always present. */
export function pluralErrors(locale: string, catalogue: Record<string, string>): string[] {
  const errors: string[] = [];
  const cardinal = new Set<string>(new Intl.PluralRules(locale).resolvedOptions().pluralCategories);
  const ordinal = new Set<string>(new Intl.PluralRules(locale, { type: "ordinal" }).resolvedOptions().pluralCategories);
  const walk = (key: string, elements: MessageFormatElement[]) => {
    for (const el of elements) {
      if (el.type === TYPE.plural) {
        const allowed = el.pluralType === "ordinal" ? ordinal : cardinal;
        const options = Object.keys(el.options);
        if (!options.includes("other")) errors.push(`${locale}: ${key} has no "other" branch`);
        for (const option of options) {
          if (!option.startsWith("=") && !allowed.has(option)) errors.push(`${locale}: ${key} has branch "${option}", not a CLDR category`);
        }
        for (const option of Object.values(el.options)) walk(key, option.value);
      } else if (el.type === TYPE.select) {
        for (const option of Object.values(el.options)) walk(key, option.value);
      }
    }
  };
  for (const [key, message] of Object.entries(catalogue)) walk(key, parse(message, { ignoreTag: true }));
  return errors;
}

/** Conventional uppercase terms and brand names, not shouted copy. Keep these exact so other all-caps words in the
 * same message still fail the sentence-case check. */
const UPPERCASE_TERMS = new Set(["USB", "JPEG", "PNG", "PDF", "URL", "HTTPS", "API"]);

/** UXS-05: sentence case (no all-caps), never "wrong", no exclamation marks in interface copy. */
export function copyLintErrors(catalogue: Record<string, string>): string[] {
  const errors: string[] = [];
  for (const [key, message] of Object.entries(catalogue)) {
    const words = message.replace(/\{[^}]*\}/g, " ").match(/[A-Za-z]{2,}/g) ?? [];
    if (words.some((w) => w.length > 2 && w === w.toUpperCase() && !UPPERCASE_TERMS.has(w))) errors.push(`${key}: all-caps word`);
    if (/\bwrong\b/i.test(message) && !key.startsWith("error.")) errors.push(`${key}: says "wrong" to a student`);
    if (/\bwrong\b/i.test(message) && key.startsWith("error.") && /student|answer|step/i.test(message)) errors.push(`${key}: says "wrong" about the student's work`);
    if (message.includes("!")) errors.push(`${key}: exclamation mark in interface copy`);
  }
  return errors;
}
