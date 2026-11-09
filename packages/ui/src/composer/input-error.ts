import { elementCaseSuggestion } from "@jana/client-core";
import type { Translate } from "@jana/i18n";
/** Input errors share one message across the inline preview and local/server submission feedback. */
export function inputError(t: Translate, error: { code: string; word?: string; position?: number }): string {
  const joined = error.code === "unknown_word" ? /^(sin|cos|tan)([a-z])$/.exec(error.word ?? "") : null;
  const element = error.code === "unknown_element" ? elementCaseSuggestion(error.word ?? "") : null;
  const message = t(element ? "input.unknown_element_suggestion" : joined ? "input.unknown_word_suggestion" : `input.${error.code}`, {
    word: error.word ?? "", position: (error.position ?? 0) + 1,
    suggestion: element ?? (joined ? `${joined[1]} ${joined[2]}` : ""),
  });
  return error.position !== undefined && ["two_relations", "too_long", "ambiguous_unit"].includes(error.code)
    ? `${message} ${t("input.error_position", { position: error.position + 1 })}` : message;
}
