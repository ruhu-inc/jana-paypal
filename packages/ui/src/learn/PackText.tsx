// Text from a content pack (CUR-11, CUR-13): Markdown with `$…$` maths (the content contract's RichText). A single
// `$` in model text is never maths (UIA-15), but in pack text a pair is, under the usual rule: the opening `$` is
// followed by no space and the closing one is preceded by no space and followed by no digit, so currency ("$210 and
// $35", "$5-$10") stays text. An opening `$` before a digit opens maths only when the span shows it is maths (a
// command, an operator, a bracket, a coefficient such as `2x`, or nothing but the number), so "$5 - (-4) \times 2$"
// renders and "$1 = 16.4 pesos. Change $250" does not. `items.py` in the core keeps the same rule for its LaTeX.
// Maths renders through the memoised KaTeX; nothing is ever interpreted as HTML.
import { Fragment } from "react";
import { MarkdownBlock } from "../markdown/Markdown.js";
import { Tex } from "../maths/Tex.js";

const PACK_MATHS =
  /(?<![\\$])\$(?:(?![\s\d$])|(?=\d)(?=[^$\n]*?[\\^_=<>+\-*/(){}]|[^$\n]*?\d[A-Za-z]|\d+(?:\.\d+)?\$))((?:\\\$|[^$\n])+?)(?<![\s\\])\$(?!\d)/g;

/** Pack text with its `$…$` maths in the delimiters the Markdown reader knows (`\(…\)`). */
export function packMarkdown(text: string): string {
  return text.replace(PACK_MATHS, (_, maths: string) => `\\(${maths}\\)`);
}

/** A block of pack text (an idea, a stem, a summary, a fix). */
export function PackText({ text }: { text: string }) {
  return (
    <div className="max-w-[var(--reading-measure)] text-body text-ink [&_p]:my-1">
      <MarkdownBlock text={packMarkdown(text)} />
    </div>
  );
}

/** Pack text inside a line (an option, a step's reason): plain text with its maths, no block elements. */
export function PackInline({ text }: { text: string }) {
  const parts: { maths: boolean; text: string }[] = [];
  let at = 0;
  for (const match of text.matchAll(PACK_MATHS)) {
    if (match.index > at) parts.push({ maths: false, text: text.slice(at, match.index) });
    parts.push({ maths: true, text: match[1] ?? "" });
    at = match.index + match[0].length;
  }
  if (at < text.length) parts.push({ maths: false, text: text.slice(at) });
  return <>{parts.map((p, i) => <Fragment key={i}>{p.maths ? <Tex tex={p.text} /> : p.text}</Fragment>)}</>;
}
