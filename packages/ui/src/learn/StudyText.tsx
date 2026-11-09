import { Fragment } from "react";
import { PackText } from "./PackText.js";
import { Tex } from "../maths/Tex.js";

// Same explicit Adobe Symbol mappings as study_worker/pdf_text.py. Only a display transform for saved evidence:
// original strings and Unicode citation offsets must never be rewritten to make them look cleaner.
const LEGACY = "\uf8eb\uf8ec\uf8ed\uf8ee\uf8ef\uf8f0\uf8f1\uf8f2\uf8f3\uf8f4\uf8f6\uf8f7\uf8f8\uf8f9\uf8fa\uf8fb\uf8fc\uf8fd\uf8fe";
const STANDARD = "⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪⎞⎟⎠⎤⎥⎦⎫⎬⎭";
export const normalizePdfSymbols = (text: string) => text.replace(/[\uf8eb-\uf8f4\uf8f6-\uf8fe]/g, (ch) => STANDARD[LEGACY.indexOf(ch)] ?? ch);
export const hasLegacyPdfMath = (text: string) => /[\uf8eb-\uf8f4\uf8f6-\uf8fe]/.test(text);

/** A PDF text layer cannot encode two-dimensional fractions/matrices. Keep only the leading prose in a
 * compact preview and send the reader to the original page; do not guess which digits were exponents. */
export function pdfFormulaPreview(text: string): string | null {
  const structured = (line: string) => hasLegacyPdfMath(line) || /[∑∏∫√]/.test(line)
    || /[=<>≤≥]/.test(line) && (/[α-ωΑ-Ω]/.test(line) || /[a-zA-Z][\d_^]|\([^)]*\)/.test(line));
  const lines = text.split("\n"), first = lines.findIndex(structured);
  if (first < 0) return null;
  const prose = lines.slice(0, first).join("\n").trim();
  return /\p{L}{3,}/u.test(prose) ? prose : "";
}

/** Source text is literal: never interpret PDF prose as Markdown or silently repair an equation. */
export function StudySourceText({ text }: { text: string }) {
  return <span className="whitespace-pre-wrap break-words">{normalizePdfSymbols(text).split(/([⎛-⎭])/g).map((part, i) =>
    <Fragment key={i}>{/^[⎛-⎭]$/.test(part) ? <span style={{ fontFamily: "KaTeX_Size4, serif" }}>{part}</span> : part}</Fragment>)}</span>;
}

// Study generation accepts explicit numeric maths too, while $20 and $35 remain currency. Protect display maths
// before examining single dollars. Existing curriculum pack parsing keeps its own stricter content contract.
export function studyMarkdown(text: string): string {
  return text.replace(/\$\$[\s\S]+?\$\$|(?<![\\$])\$(?![\s$])([^$\n]+?)(?<![\s\\])\$(?![\d$])/g,
    (match, maths: string | undefined) => maths === undefined ? match : `\\(${maths}\\)`)
    // Saved model drafts can omit spaces at prose/math boundaries. Keep words readable without
    // changing the expression, punctuation or the frozen source passage.
    .replace(/([\p{L}\p{N}])(?=\\\()/gu, "$1 ")
    .replace(/(\\\))(?=[\p{L}\p{N}])/gu, "$1 ");
}
export function StudyText({ text }: { text: string }) {
  return <PackText text={studyMarkdown(text)} />;
}

/** Quiz options share Study's inline delimiters; block elements never appear inside an option label. */
export function StudyInline({ text }: { text: string }) {
  const prepared = studyMarkdown(text), parts = [], pattern = /\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]|\$\$([\s\S]+?)\$\$/g;
  let at = 0;
  for (const match of prepared.matchAll(pattern)) {
    parts.push(<Fragment key={`t${at}`}>{prepared.slice(at, match.index)}</Fragment>);
    parts.push(<Tex key={`m${match.index}`} tex={match[1] ?? match[2] ?? match[3] ?? ""} />);
    at = match.index + match[0].length;
  }
  return <>{parts}{prepared.slice(at)}</>;
}
