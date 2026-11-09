// Rendering markdown tokens as React elements: no raw HTML ever (HTML in model text renders as literal text, UIA-13),
// links are shown but never navigate the webview (SEC-11), maths goes through the memoised KaTeX (UIA-15).
import { Fragment, memo, type ReactNode } from "react";
import type { Token, Tokens } from "marked";
import { Tex } from "../maths/Tex.js";
import { heal, lex } from "./lexer.js";

function inline(tokens: Token[] | undefined, key = "i"): ReactNode {
  if (tokens === undefined) return null;
  return tokens.map((token, i) => <Fragment key={`${key}${i}`}>{inlineToken(token, `${key}${i}.`)}</Fragment>);
}

function inlineToken(token: Token, key: string): ReactNode {
  switch (token.type) {
    case "text": {
      const t = token as Tokens.Text;
      return t.tokens ? inline(t.tokens, key) : t.text;
    }
    case "escape":
      return (token as Tokens.Escape).text;
    case "strong":
      return <strong className="font-semibold">{inline((token as Tokens.Strong).tokens, key)}</strong>;
    case "em":
      return <em>{inline((token as Tokens.Em).tokens, key)}</em>;
    case "del":
      return <del>{inline((token as Tokens.Del).tokens, key)}</del>;
    case "codespan":
      return <code className="rounded-control bg-panel px-1 font-mono text-meta">{(token as Tokens.Codespan).text}</code>;
    case "br":
      return <br />;
    case "link": {
      const t = token as Tokens.Link;
      return <span className="text-accent underline">{inline(t.tokens, key)}</span>;
    }
    case "image":
      return (token as Tokens.Image).text;
    case "inlineMath":
      return <Tex tex={(token as unknown as { text: string }).text} />;
    case "displayMath":
      return <Tex tex={(token as unknown as { text: string }).text} display />;
    case "html":
      return (token as Tokens.HTML).raw; // literal text, never markup
    default:
      return token.raw;
  }
}

function block(token: Token, key: string): ReactNode {
  switch (token.type) {
    case "paragraph":
      return <p className="my-2">{inline((token as Tokens.Paragraph).tokens, key)}</p>;
    case "heading":
      return <p className="my-2 text-heading font-semibold">{inline((token as Tokens.Heading).tokens, key)}</p>;
    case "list": {
      const t = token as Tokens.List;
      const items = t.items.map((item, i) => <li key={i}>{item.tokens.map((child, j) => <Fragment key={j}>{child.type === "text" ? inline((child as Tokens.Text).tokens ?? [child], `${key}${i}.${j}`) : block(child, `${key}${i}.${j}`)}</Fragment>)}</li>);
      return t.ordered ? <ol className="my-2 list-decimal ps-6">{items}</ol> : <ul className="my-2 list-disc ps-6">{items}</ul>;
    }
    case "code":
      return <pre className="my-2 overflow-x-auto rounded-card bg-panel p-3 font-mono text-meta"><code>{(token as Tokens.Code).text}</code></pre>;
    case "blockquote":
      return <blockquote className="my-2 border-s-2 border-line-strong ps-3 text-muted">{(token as Tokens.Blockquote).tokens.map((c, i) => <Fragment key={i}>{block(c, `${key}${i}.`)}</Fragment>)}</blockquote>;
    case "displayMath":
      return <Tex tex={(token as unknown as { text: string }).text} display />;
    case "table": {
      const t = token as Tokens.Table;
      return (
        <table className="my-2 text-ui">
          <thead><tr>{t.header.map((cell, i) => <th key={i} className="border-b border-line-strong px-2 text-start font-semibold">{inline(cell.tokens, `${key}h${i}`)}</th>)}</tr></thead>
          <tbody>{t.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c} className="border-b border-line px-2">{inline(cell.tokens, `${key}${r}.${c}`)}</td>)}</tr>)}</tbody>
        </table>
      );
    }
    case "hr":
      return <hr className="my-3 border-line" />;
    case "space":
      return null;
    case "html":
      return <p className="my-2">{(token as Tokens.HTML).raw}</p>;
    default:
      return <p className="my-2">{token.raw}</p>;
  }
}

/** One block of markdown. Settled blocks are memoised by their text, so their DOM nodes are never replaced. */
export const MarkdownBlock = memo(function MarkdownBlock({ text }: { text: string }) {
  return <>{lex(text).map((token, i) => <Fragment key={i}>{block(token, `${i}.`)}</Fragment>)}</>;
});

/** A text part: settled blocks render once; only the healed tail re-renders on each flush. */
export function StreamingMarkdown({ settled, tail }: { settled: string[]; tail: string }) {
  return (
    <div className="max-w-[var(--reading-measure)]">
      {settled.map((text, i) => <MarkdownBlock key={i} text={text} />)}
      {tail !== "" && <MarkdownBlock key={`tail-${settled.length}`} text={heal(tail)} />}
    </div>
  );
}
