// The composer editor (INP-03): one tiptap document of paragraphs, text runs, hard breaks and inline `math` nodes
// (`source` as typed, `latex`, `mode`). A math node renders with the memoised KaTeX; clicking it (or Enter on it)
// opens its source in a small inline editor with live preview: Enter or Tab commits, Esc restores the previous value.
import { InputRule, mergeAttributes, Node, type Editor, type JSONContent } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import Document from "@tiptap/extension-document";
import HardBreak from "@tiptap/extension-hard-break";
import Paragraph from "@tiptap/extension-paragraph";
import Text from "@tiptap/extension-text";
import { Placeholder, UndoRedo } from "@tiptap/extensions";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { convertEntry, isMathShortcut, normaliseInput, ledgerTable, LEDGER_COLUMNS, type EntryMode, type LedgerTemplate } from "@jana/client-core";
import type { ContentBlock } from "@jana/contracts-ts";
import { inputError } from "./input-error.js";
import { LedgerNode } from "./LedgerNode.js";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { segments } from "./maths.js";

function MathView({ node, updateAttributes, editor, getPos }: NodeViewProps) {
  const t = useT();
  const source = String(node.attrs.source ?? "");
  const description = useId();
  const equation = `${description}-equation`;
  const [editing, setEditing] = useState(source.includes("□"));
  const [draft, setDraft] = useState(source);
  const input = useRef<HTMLInputElement>(null);
  const closing = useRef(false);
  const conversion = convertEntry(draft, node.attrs.mode as EntryMode);
  const [spokenTex, setSpokenTex] = useState("");
  const [scroll, setScroll] = useState(0);
  const hintId = `${description}-error`;
  const [hint, setHint] = useState<typeof conversion.error>(null);
  useEffect(() => {
    setHint(null);
    setSpokenTex("");
    const spoken = setTimeout(() => setSpokenTex(conversion.error === null ? conversion.latex : ""), 500);
    const timer = setTimeout(() => setHint(conversion.error), 600);
    return () => { clearTimeout(timer); clearTimeout(spoken); };
  }, [draft]);
  useEffect(() => {
    if (editing) {
      closing.current = false;
      input.current?.focus();
      const slot = draft.indexOf("□");
      if (slot >= 0) input.current?.setSelectionRange(slot, slot + 1);
    }
  }, [editing]);
  const close = () => {
    closing.current = true;
    setEditing(false);
    const pos = typeof getPos === "function" ? getPos() : undefined;
    if (typeof pos === "number") editor.commands.focus(pos + node.nodeSize);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    e.stopPropagation();
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      if (e.key === "Tab") {
        const at = draft.indexOf("□", input.current?.selectionEnd ?? 0);
        if (at >= 0) { input.current?.setSelectionRange(at, at + 1); return; }
      }
      if (conversion.error !== null) { setHint(conversion.error); return; }
      updateAttributes({ source: normaliseInput(draft), latex: conversion.latex });
      close();
    } else if (e.key === "Escape") {
      e.preventDefault();
      setDraft(source);
      close();
    }
  };
  return (
    <NodeViewWrapper as="span" className="inline-flex align-baseline" data-math-node="" data-latex={String(node.attrs.latex ?? "")}>
      {editing ? (
        <span className="inline-flex items-center gap-2 rounded-control border border-focus bg-canvas px-1">
          <span className="relative inline-block w-40 overflow-hidden font-mono text-meta">
            <input ref={input} aria-label={t("composer.maths_source")} aria-invalid={hint !== null} aria-describedby={hint !== null ? hintId : undefined} value={draft} onChange={(e) => setDraft(e.target.value)} onScroll={(e) => setScroll(e.currentTarget.scrollLeft)} onKeyDown={onKey} onBlur={() => {
              if (closing.current || conversion.error !== null) return;
              updateAttributes({ source: normaliseInput(draft), latex: conversion.latex }); setEditing(false);
            }} className="w-40 bg-transparent font-mono text-meta" />
            {hint !== null && <span aria-hidden="true" className="pointer-events-none absolute inset-0 whitespace-pre text-transparent" style={{ transform: `translateX(-${scroll}px)` }}><span>{draft.slice(0, hint.position)}</span><span data-input-error-position={hint.position} className="underline decoration-danger decoration-dotted underline-offset-4">{draft[hint.position] ?? " "}</span></span>}
          </span>
          <span aria-live="off">{conversion.error === null ? <Tex tex={conversion.latex} /> : null}</span>
          <span className="sr-only" aria-live="polite" aria-atomic="true" data-input-announcement="">{spokenTex ? <Tex tex={spokenTex} /> : null}</span>
          {hint !== null && <span id={hintId} role="status" className="text-meta text-muted">{inputError(t, hint)}</span>}
        </span>
      ) : (
        <button type="button" aria-labelledby={equation} aria-describedby={description} ref={(button) => {
          // The button needs a text name as well as its navigable MathML. Reference the visible KaTeX branch so
          // the name always matches the drawing, rather than overriding it with raw TeX source.
          const visible = button?.querySelector(".katex-html, code");
          if (visible) visible.id = equation;
        }} onClick={() => { setDraft(source); setEditing(true); }} className="inline-flex min-h-6 min-w-6 items-center justify-center rounded-control px-0.5 hover:bg-wash">
          <Tex tex={String(node.attrs.latex ?? "")} />
        </button>
      )}
      <span id={description} className="sr-only">{t("composer.maths_edit", { latex: String(node.attrs.latex ?? "") })}</span>
    </NodeViewWrapper>
  );
}

export const MathNode = Node.create({
  name: "math",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  addOptions() { return { mode: "maths" as EntryMode }; },
  addAttributes() {
    return { source: { default: "" }, latex: { default: "" }, mode: { default: "maths" } };
  },
  parseHTML() {
    return [{ tag: "span[data-math-node]" }];
  },
  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, { "data-math-node": "" })];
  },
  addNodeView() {
    return ReactNodeViewRenderer(MathView, { stopEvent: ({ event }) => event.target instanceof HTMLInputElement });
  },
  addProseMirrorPlugins() {
    // Whatever path the text took (keys, IME composition, paste, DOM mutation), every completed \(…\) in the text
    // becomes a math node.
    const type = this.type;
    return [
      new Plugin({
        key: new PluginKey("mathFromText"),
        appendTransaction: (transactions, _old, state) => {
          if (!transactions.some((tr) => tr.docChanged)) return null;
          const found: { from: number; to: number; source: string }[] = [];
          state.doc.descendants((node, pos) => {
            if (!node.isText) return true;
            for (const match of (node.text ?? "").matchAll(/\\\(([^)]+?)\\\)/g)) {
              found.push({ from: pos + match.index, to: pos + match.index + match[0].length, source: match[1] ?? "" });
            }
            return false;
          });
          if (found.length === 0) return null;
          const tr = state.tr;
          for (const span of found.reverse()) {
            tr.replaceWith(span.from, span.to, type.create({ source: span.source, latex: convertEntry(span.source, this.options.mode).latex, mode: this.options.mode }));
          }
          return tr;
        },
      }),
    ];
  },
  addInputRules() {
    return [
      new InputRule({
        find: /\\\(([^)]+?)\\\)$/,
        handler: ({ state, range, match }) => {
          const source = match[1] ?? "";
          state.tr.replaceWith(range.from, range.to, this.type.create({ source, latex: convertEntry(source, this.options.mode).latex, mode: this.options.mode }));
        },
      }),
      new InputRule({
        find: /(?:^|\s)([^\s]+) $/,
        handler: ({ state, range, match }) => {
          const source = match[1] ?? "";
          const result = convertEntry(source, this.options.mode);
          const unambiguous = this.options.mode === "chem" ? /\d|->|<=>|\^/.test(source) : isMathShortcut(source);
          if (!unambiguous || result.error !== null) return null;
          const from = range.to - source.length - 1;
          state.tr.replaceWith(from, range.to, [this.type.create({ source, latex: result.latex, mode: this.options.mode }), state.schema.text(" ")]);
        },
      }),
    ];
  },
  addKeyboardShortcuts() {
    return { Backspace: () => this.editor.commands.undoInputRule() };
  },
});

export function extensions(placeholder: string, mode: EntryMode = "maths") {
  return [Document, Paragraph, Text, HardBreak, UndoRedo, Placeholder.configure({ placeholder }), MathNode.configure({ mode }), LedgerNode];
}

/** Insert text the way typing or pasting does: \(…\) spans become math nodes, everything else stays text. */
export function inlineContent(text: string, mode: EntryMode = "maths"): JSONContent[] {
  const content: JSONContent[] = [];
  for (const s of segments(text)) {
    if (s.kind === "math") content.push({ type: "math", attrs: { source: s.source, latex: convertEntry(s.source, mode).latex, mode } });
    else s.text.split("\n").forEach((line, index) => {
      if (index > 0) content.push({ type: "hardBreak" });
      if (line !== "") content.push({ type: "text", text: line });
    });
  }
  return content;
}

export function insertText(editor: Editor, text: string, mode: EntryMode = "maths"): void {
  const content = inlineContent(text, mode);
  if (content.length === 0) {
    editor.commands.focus();
    return;
  }
  editor.chain().focus().insertContent(content).run();
}

/** The document as StartTurn text: runs as written, maths as \(latex\), breaks as new lines. */
export function serialise(doc: JSONContent): string {
  const out: string[] = [];
  const walk = (node: JSONContent) => {
    if (node.type === "text") out.push(node.text ?? "");
    else if (node.type === "math") {
      const result = convertEntry(String(node.attrs?.source ?? ""), node.attrs?.mode as EntryMode);
      out.push(result.serialised !== result.latex || node.attrs?.mode !== "maths" ? result.serialised : `\\(${String(node.attrs?.latex ?? "")}\\)`);
    }
    else if (node.type === "hardBreak") out.push("\n");
    else if (node.type === "ledger_table") {
      const table = node.attrs?.rows as string[][];
      out.push([LEDGER_COLUMNS[node.attrs?.template as LedgerTemplate].join(" | "), ...(table ?? []).map((row) => row.join(" | "))].join("\n") + "\n");
    }
    for (const child of node.content ?? []) walk(child);
    if (node.type === "paragraph") out.push("\n");
  };
  walk(doc);
  return out.join("").replace(/\n+$/, "");
}

export function serialiseBlocks(doc: JSONContent): ContentBlock[] {
  const blocks: ContentBlock[] = [];
  let pending: JSONContent[] = [];
  const flush = () => {
    const text = serialise({ type: "doc", content: pending }).trim();
    if (text !== "") blocks.push({ type: "text", text });
    pending = [];
  };
  for (const node of doc.content ?? []) {
    if (node.type !== "ledger_table") pending.push(node);
    else { flush(); blocks.push(ledgerTable(node.attrs?.template as LedgerTemplate, node.attrs?.rows as string[][])); }
  }
  flush();
  return blocks;
}

export function mathNodes(doc: JSONContent): { source: string; latex: string }[] {
  const found: { source: string; latex: string }[] = [];
  const walk = (node: JSONContent) => {
    if (node.type === "math") found.push({ source: String(node.attrs?.source ?? ""), latex: String(node.attrs?.latex ?? "") });
    for (const child of node.content ?? []) walk(child);
  };
  walk(doc);
  return found;
}
