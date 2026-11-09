// INP-14: run before storage, scanning or editor insertion. ZWJ/ZWNJ survive only inside words.
export function normaliseInput(text: string): string {
  const clean = [...text.normalize("NFC")].filter((ch) => {
    const code = ch.codePointAt(0) ?? 0;
    return code === 9 || code === 10 || code >= 32 && (code < 127 || code > 159);
  }).join("");
  return clean
    .replace(/[\u202a-\u202e\u2066-\u2069\u200b\u2060\ufeff\u00ad]/g, "")
    .replace(/[\u200c\u200d]/g, (ch, at: number, whole: string) =>
      /[\p{L}\p{N}]/u.test(whole[at - 1] ?? "") && /[\p{L}\p{N}]/u.test(whole[at + 1] ?? "") ? ch : "");
}

export const INPUT_LIMITS = { text: 8000, latex: 4000, expression: 2000, blocks: 8, workingLines: 30 } as const;

export class InputError extends Error {
  constructor(readonly code: string, readonly position: number, readonly word = "") { super(code); }
}

/** Numbered question lists require a choice before starting a turn (INP-13, PED-19). */
export function pastedQuestions(text: string): string[] {
  const lines = normaliseInput(text).split(/\r?\n/);
  const starts = lines.map((line, index) => /^\s*(?:\d{1,2}[.)]|Q\s*\d{1,2}[:.)])\s+\S/i.test(line) ? index : -1)
    .filter((index) => index >= 0);
  if (starts.length < 3 || lines.slice(0, starts[0]).some((line) => line.trim() !== "")) return [];
  return starts.map((at, index) => lines.slice(at, starts[index + 1] ?? lines.length).join("\n")
    .replace(/^\s*(?:\d{1,2}[.)]|Q\s*\d{1,2}[:.)])\s+/i, "").trim());
}

export function workingLines(text: string): string[] {
  if (normaliseInput(text).length > INPUT_LIMITS.text) throw new InputError("too_long", INPUT_LIMITS.text);
  const rawLines = normaliseInput(text).split(/\r?\n/);
  const lines = rawLines.map((line) => line.trim()).filter(Boolean);
  if (lines.length > INPUT_LIMITS.workingLines) throw new InputError("too_many_lines", 0);
  let position = 0;
  for (const raw of rawLines) {
    const line = raw.trim();
    if (line.length > INPUT_LIMITS.expression) throw new InputError("too_long", position + raw.indexOf(line) + INPUT_LIMITS.expression);
    position += raw.length + 1;
  }
  return lines;
}
