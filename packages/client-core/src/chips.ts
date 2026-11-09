// Help before the answer and the next step after it (D120): the help chips every practice, quiz and past-paper
// question offers (ASM-28), the follow-up chips after a tutor, study or Ask anything answer (UXS-68) and a chat's
// automatic title. Pure logic: the chips' words come from the catalogue and a chip never carries the answer.

/** Give me a hint, Walk me through it, Explain simply (ASM-28), in the order they are shown. */
export const HELP_KINDS = ["hint", "walk_through", "explain_simply"] as const;
export type HelpKind = (typeof HELP_KINDS)[number];

/** How the tutor is asked for each help chip. A hint is the hint ladder's next rung (intent `stuck`, LRN-20); the other
 * two are the student's words on the loaded question (intent `answer`, so the guided tutor keeps the problem and never
 * starts a new one). */
export function helpTurn(kind: HelpKind): { intent: "stuck" | "answer"; textKey: string } {
  return { intent: kind === "hint" ? "stuck" : "answer", textKey: `help.ask.${kind}` };
}

/** Practise this, Make flashcards from this, Explain a step (UXS-68). */
export const FOLLOW_UP_KINDS = ["practise", "flashcards", "explain_step"] as const;
export type FollowUpKind = (typeof FOLLOW_UP_KINDS)[number];

export interface FollowUpChip {
  kind: FollowUpKind;
  label: string;
}

/** At most two follow-up chips after an answer (UXS-68). */
export const MAX_FOLLOW_UPS = 2;

const NUMBER = /[-−]?\d+(?:[.,]\d+)?/g;

function squash(text: string): string {
  return text.toLowerCase().replace(/[\s$\\{}]/g, "").replace(/−/g, "-");
}

/** Whether `text` contains one of `answers`: a number as a number (12 is in "v = 12 m/s" but not in "120"), anything
 * else case-insensitively with spaces and maths delimiters ignored. An empty answer never matches. */
export function mentionsAnswer(text: string, answers: readonly string[]): boolean {
  const numbers = new Set((text.match(NUMBER) ?? []).map((n) => Number(n.replace("−", "-").replace(",", "."))));
  const flat = squash(text);
  return answers.some((answer) => {
    const wanted = answer.trim();
    if (wanted === "") return false;
    if (/^[-−]?\d+(?:[.,]\d+)?$/.test(wanted)) return numbers.has(Number(wanted.replace("−", "-").replace(",", ".")));
    const key = squash(wanted);
    return key !== "" && flat.includes(key);
  });
}

/** The chips to show after an answer: the offered kinds in order, each once, at most two, and never one whose label
 * mentions the current question's answer (its final value or the correct option's text). */
export function followUps(offered: readonly FollowUpKind[], label: (kind: FollowUpKind) => string, answers: readonly string[] = []): FollowUpChip[] {
  const chips: FollowUpChip[] = [];
  for (const kind of offered) {
    if (chips.length >= MAX_FOLLOW_UPS || chips.some((c) => c.kind === kind) || !FOLLOW_UP_KINDS.includes(kind)) continue;
    const words = label(kind);
    if (!mentionsAnswer(words, answers)) chips.push({ kind, label: words });
  }
  return chips;
}

/** The most words a chat's automatic title has (UXS-68). */
export const TITLE_WORDS = 6;

/** A chat's automatic title from its first exchange: its first line, maths delimiters and Markdown marks dropped, at
 * most six words, each with a letter and no digit (a title never carries a value from the question or its answer). The
 * core titles tutor sessions itself by the same rule (`engine/titles.py`); this is for a chat the client titles. */
export function chatTitle(text: string): string {
  const line = text.split("\n").map((l) => l.trim()).find((l) => l !== "") ?? "";
  const plain = line.replace(/\\[()[\]]/g, "").replace(/\$/g, "").replace(/[*_`#>]/g, "");
  const words = plain.split(/\s+/).filter((w) => /\p{L}/u.test(w) && !/\d/.test(w));
  return words.slice(0, TITLE_WORDS).join(" ").replace(/[,;:]$/, "");
}
