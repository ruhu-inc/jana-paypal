// Markdown for streamed tutor text (UIA-13, UIA-14). Maths delimiters are tokenizer extensions: inline \(…\), display
// \[…\] and $$…$$. A single `$` is never maths, so currency stays text. HTML in model text is never interpreted.
import { Marked, type Token, type TokenizerAndRendererExtension } from "marked";

const inlineMath: TokenizerAndRendererExtension = {
  name: "inlineMath",
  level: "inline",
  start: (src) => src.indexOf("\\("),
  tokenizer(src) {
    const match = /^\\\(([\s\S]+?)\\\)/.exec(src);
    if (match) return { type: "inlineMath", raw: match[0], text: match[1]?.trim() ?? "" };
    return undefined;
  },
};

const displayMath: TokenizerAndRendererExtension = {
  name: "displayMath",
  level: "block",
  start: (src) => {
    const a = src.indexOf("\\[");
    const b = src.indexOf("$$");
    return a < 0 ? b : b < 0 ? a : Math.min(a, b);
  },
  tokenizer(src) {
    const match = /^(?:\\\[([\s\S]+?)\\\]|\$\$([\s\S]+?)\$\$)[ \t]*(?:\n|$)/.exec(src);
    if (match) return { type: "displayMath", raw: match[0], text: (match[1] ?? match[2] ?? "").trim() };
    return undefined;
  },
};

const inlineDisplayMath: TokenizerAndRendererExtension = {
  name: "inlineDisplayMath",
  level: "inline",
  start: (src) => {
    const a = src.indexOf("\\[");
    const b = src.indexOf("$$");
    return a < 0 ? b : b < 0 ? a : Math.min(a, b);
  },
  tokenizer(src) {
    const match = /^(?:\\\[([\s\S]+?)\\\]|\$\$([\s\S]+?)\$\$)/.exec(src);
    if (match) return { type: "displayMath", raw: match[0], text: (match[1] ?? match[2] ?? "").trim() };
    return undefined;
  },
};

const marked = new Marked({ gfm: true, breaks: false, extensions: [displayMath, inlineMath, inlineDisplayMath] });

export function lex(markdown: string): Token[] {
  return marked.lexer(markdown);
}

/** Close what a still-streaming block left open, for display only (UIA-13): emphasis, code, fences and maths. */
export function heal(tail: string): string {
  let out = tail;
  const fences = (out.match(/^(```|~~~)/gm) ?? []).length;
  if (fences % 2 === 1) out += "\n```";
  if ((out.match(/\\\(/g) ?? []).length > (out.match(/\\\)/g) ?? []).length) out = out.replace(/\\\((?![\s\S]*\\\))([\s\S]*)$/, "`$1`");
  if ((out.match(/\\\[/g) ?? []).length > (out.match(/\\\]/g) ?? []).length) out = out.replace(/\\\[(?![\s\S]*\\\])([\s\S]*)$/, "`$1`");
  if ((out.match(/\$\$/g) ?? []).length % 2 === 1) out = out.replace(/\$\$([^$]*)$/, "`$1`");
  const ticks = (out.match(/(?<!`)`(?!`)/g) ?? []).length;
  if (ticks % 2 === 1) out += "`";
  if ((out.match(/\*\*/g) ?? []).length % 2 === 1) out += "**";
  else if ((out.replace(/\*\*/g, "").match(/\*/g) ?? []).length % 2 === 1) out += "*";
  return out;
}
