// The composer's maths model (INP-03): what the student types as maths (`source`) and the LaTeX it becomes. Only
// \(…\) starts maths; a single `$` never does (UIA-14), so currency in Accounting stays text.

import { convertMath, normaliseInput } from "@jana/client-core";

/** Format without changing the source on an input error; the worker decides meaning on submission. */
export function normaliseLatex(source: string): string {
  return convertMath(source).latex;
}

export type Segment = { kind: "text"; text: string } | { kind: "math"; source: string; latex: string };

/** Split text into runs and \(…\) maths, as typed or pasted. */
export function segments(text: string): Segment[] {
  text = normaliseInput(text);
  const out: Segment[] = [];
  const pattern = /\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]|\$\$([\s\S]+?)\$\$/g;
  let at = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > at) out.push({ kind: "text", text: text.slice(at, match.index) });
    const source = match[1] ?? match[2] ?? match[3] ?? "";
    out.push({ kind: "math", source, latex: normaliseLatex(source) });
    at = match.index + match[0].length;
  }
  if (at < text.length) out.push({ kind: "text", text: text.slice(at) });
  // PDF/plain-text clipboard tokens use Unicode mathematical forms without explicit delimiters.
  return out.flatMap((segment): Segment[] => {
    if (segment.kind === "math") return [segment];
    const result: Segment[] = [];
    let cursor = 0;
    const tokens = [...segment.text.matchAll(/\S+/g)];
    const words = new Set("pi inf alpha beta gamma delta theta lambda mu rho sigma phi omega Delta Omega sqrt cbrt root frac abs sin cos tan asin acos atan log ln exp".split(" "));
    const part = (token: string, at: number) => {
      const lead = token.match(/^["'“‘]+/)?.[0].length ?? 0;
      const tail = token.slice(lead).match(/[.,;:!?"'”’]+$/)?.[0].length ?? 0;
      const core = token.slice(lead, token.length - tail);
      if (!core || !/^[\p{L}\p{N}⁰¹²³⁴⁵⁶⁷⁸⁹√×÷−≤≥≠½¼¾+*/^_=<>~|(),.{}[\]\\-]+$/u.test(core)
        || [...core.matchAll(/[A-Za-z]+/g)].some(([word]) => word.length > 1 && !words.has(word))) return null;
      return { start: at + lead, end: at + token.length - tail, lead, tail };
    };
    for (let index = 0; index < tokens.length;) {
      const token = tokens[index]; if (!token) break;
      const first = part(token[0], token.index);
      if (!first) { index++; continue; }
      let last = first, next = index + 1;
      while (!last.tail && next < tokens.length) {
        const item = tokens[next]; if (!item) break;
        const following = part(item[0], item.index);
        if (!following || following.lead || segment.text.slice(last.end, following.start).includes("\n")) break;
        last = following; next++;
      }
      const source = segment.text.slice(first.start, last.end);
      const converted = /[²³⁰¹⁴⁵⁶⁷⁸⁹√×÷−≤≥≠½¼¾παβγδθλμρσφωΔΩ]/u.test(source) ? convertMath(source) : null;
      if (converted?.error === null) {
        if (first.start > cursor) result.push({ kind: "text", text: segment.text.slice(cursor, first.start) });
        result.push({ kind: "math", source, latex: converted.latex }); cursor = last.end;
      }
      index = next; // a rejected run stays text; bounded input is scanned once, never guessed or retried quadratically
    }
    if (cursor < segment.text.length) result.push({ kind: "text", text: segment.text.slice(cursor) });
    return result;
  });
}
