// One editor for Ask, your-turn and multiline working (INP-03/06/11/12). Clipboard data never inserts HTML.
import { inputError } from "./input-error.js";
import { Extension, Mark, type JSONContent } from "@tiptap/core";
import { EditorContent, useEditor } from "@tiptap/react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent } from "react";
import { convertEntry, InputError, INPUT_LIMITS, normaliseInput, pastedQuestions, DraftController, journalFromText, ledgerTotals, LEDGER_COLUMNS, type EntryMode, type LedgerTemplate } from "@jana/client-core";
import { Tex } from "../maths/Tex.js";
import { renderTex } from "../maths/katex.js";
import { useT } from "../i18n.js";
import { Button, ErrorState } from "../primitives/index.js";
import { Icon } from "../icons.js";
import type { ContentBlock } from "@jana/contracts-ts";
import { useOptionalSession } from "../store.js";
import { extensions, inlineContent, insertText, serialise, serialiseBlocks } from "./editor.js";
import { clipboardText } from "./paste.js";

export interface EntryHandle { text(): string; insert(text: string, uncertain?: boolean): void; blocks(): ContentBlock[]; clear(): void; focus(): void; submit(): void }
interface Props {
  label: string;
  placeholder: string;
  initialText?: string;
  initialDocument?: JSONContent;
  sessionId?: string;
  draftSlot?: string;
  accounting?: boolean;
  mode?: EntryMode;
  multiline?: boolean;
  /** The number of the first line when `multiline` numbers them: the board's next step, after the ones it has. */
  firstLine?: number;
  /** `multiline` without line numbers: a note is prose, not working. */
  numbered?: boolean;
  disabled?: boolean;
  onSubmit: (text: string, pasted: boolean) => boolean;
  onCancel?: () => void;
  onDictate?: (pressed: boolean) => void;
  onChange?: (text: string) => void;
  /** `composer`: the borderless text area inside the composer shell, which draws the edge (UXS-15). */
  look?: "field" | "composer";
  /** Set when the host draws the maths keyboard toggle itself (the composer's keyboard slot); the editor then draws
   * only the keyboard, and reports Alt+M and Escape through `onPaletteChange`. */
  palette?: boolean;
  onPaletteChange?: (open: boolean) => void;
}
export const PALETTES: { key: string; mode: EntryMode; entries: string[] }[] = [
  { key: "basic", mode: "maths", entries: ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "□+□", "□-□", "□*□", "□/□", "□=□", "(□)", "frac(□,□)", "□^□", "sqrt(□)", "cbrt(□)", "root(□,□)", "□+-□", "pi", "□deg"] },
  { key: "algebra", mode: "maths", entries: ["□_□", "abs(□)", "□<=□", "□>=□", "□!=□", "sin(□)", "cos(□)", "tan(□)", "asin(□)", "acos(□)", "atan(□)", "log(□)", "ln(□)", "exp(□)", "□~=□", "alpha", "beta", "lambda", "mu", "rho", "omega", "Delta", "theta", "inf", "□ -> □"] },
  { key: "units", mode: "units", entries: ["□ m", "□ s", "□ kg", "□ N", "□ J", "□ W", "□ Pa", "□ V", "□ A", "□ mol", "□ dm^3", "□ cm^3", "□ degC", "□ K", "□ ohm", "□ kohm", "□ um", "□ mm", "□ km", "□ MPa", "□ m^-1", "□ s^-2"] },
  { key: "accounting", mode: "maths", entries: [] },
  { key: "chem", mode: "chem", entries: ["H2O", "SO4^2-", "Na^+", "Cl^-", "Ca^2+", "□(s)", "□(l)", "□(g)", "□(aq)", "CuSO4.5H2O", "□ -> □", "□ <=> □", "□ ->[heat] □", "Delta"] },
];

const KEY_NAMES: Record<string, string> = {
  "□+□": "add", "□-□": "subtract", "□*□": "multiply", "□/□": "divide", "□=□": "equals", "(□)": "brackets",
  "frac(□,□)": "fraction", "□^□": "power", "sqrt(□)": "square_root", "cbrt(□)": "cube_root", "root(□,□)": "root",
  "□+-□": "plus_minus", "□deg": "degrees", "□_□": "subscript", "abs(□)": "absolute", "□<=□": "less_equal",
  "□>=□": "greater_equal", "□!=□": "not_equal", "□~=□": "approximately", "sin(□)": "sine", "cos(□)": "cosine", "tan(□)": "tangent",
  "asin(□)": "inverse_sine", "acos(□)": "inverse_cosine", "atan(□)": "inverse_tangent", "log(□)": "log", "ln(□)": "natural_log", "exp(□)": "exponential",
  "□(s)": "solid", "□(l)": "liquid", "□(g)": "gas", "□(aq)": "aqueous", "□ -> □": "reaction", "□ <=> □": "reversible", "□ ->[heat] □": "heated_reaction",
};
// What a key shows: its entry typeset (α, ≈, dm³, SO₄²⁻), with an empty box where the student types, never the linear
// source. Units show the unit alone; the key still inserts its number slot. Each face is converted once.
const BOX = "\\square";
// Divide and fraction both convert to a stacked fraction; the divide key shows ÷ so the two keys look different.
const FACE_OVERRIDES: Record<string, string> = { "□deg": `${BOX}^{\\circ}`, "□/□": `${BOX} \\div ${BOX}` };
const FACES = new Map<string, string>();
export function keyFace(source: string, mode: EntryMode): string {
  const id = `${mode}:${source}`;
  const known = FACES.get(id);
  if (known !== undefined) return known;
  let face = FACE_OVERRIDES[source];
  if (face === undefined) {
    if (mode === "units") {
      const done = convertEntry(source.replace("□", "1"), mode);
      face = done.error ? undefined : done.latex.replace(/^1\\,/, "");
    } else {
      const sentinel = mode === "chem" ? "He" : "Q";
      const done = convertEntry(source.replaceAll("□", sentinel), mode);
      face = done.error ? undefined : done.latex.replaceAll(mode === "chem" ? "\\mathrm{He}" : "Q", BOX);
    }
  }
  face ??= `\\text{${source.replaceAll("□", "").replace(/[\\{}$&#%_^~]/g, "")}}`;
  FACES.set(id, face);
  return face;
}
// A key without its own name (a unit, a species, a letter) is named by exactly what it shows, so the spoken name
// contains the visible label (WCAG 2.5.3): the typeset face's text, without KaTeX's hidden MathML copy.
const NAMES = new Map<string, string>();
export function faceName(face: string): string {
  const known = NAMES.get(face);
  if (known !== undefined) return known;
  const rendered = renderTex(face, false);
  let name = face;
  if ("html" in rendered && typeof document !== "undefined") {
    const holder = document.createElement("template");
    holder.innerHTML = rendered.html;
    holder.content.querySelectorAll(".katex-mathml").forEach((node) => node.remove());
    name = holder.content.textContent ?? face;
  }
  NAMES.set(face, name);
  return name;
}
function moveKey(event: KeyboardEvent<HTMLElement>, selector: string, select = false) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const keys = [...event.currentTarget.querySelectorAll<HTMLButtonElement>(selector)], at = keys.indexOf(document.activeElement as HTMLButtonElement);
  if (!keys.length || at < 0) return;
  event.preventDefault();
  const next = keys[event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : (at + (event.key === "ArrowRight" ? 1 : -1) + keys.length) % keys.length];
  next?.focus(); if (select) next?.click();
}

const LAST_TAB = new Map<string, string>();

export const EntryEditor = forwardRef<EntryHandle, Props>(function EntryEditor({ label, placeholder, initialText = "", initialDocument, sessionId, draftSlot, accounting = false, mode = "maths", multiline = false, firstLine = 1, numbered = multiline, disabled = false, onSubmit, onCancel, onDictate, onChange, look = "field", palette: hostPalette, onPaletteChange }, ref) {
  const t = useT();
  const session = useOptionalSession();
  const draftStore = useRef<DraftController | null>(null);
  const [draftError, setDraftError] = useState(false);
  const [journal, setJournal] = useState<ReturnType<typeof journalFromText>>(null);
  const [ownPalette, setOwnPalette] = useState(false);
  const hosted = hostPalette !== undefined;
  const palette = hosted ? hostPalette : ownPalette;
  const paletteState = useRef({ open: palette, hosted, change: onPaletteChange });
  paletteState.current = { open: palette, hosted, change: onPaletteChange };
  const setPalette = (next: boolean | ((open: boolean) => boolean)) => {
    const { open, hosted: isHosted, change } = paletteState.current;
    const value = typeof next === "function" ? next(open) : next;
    if (isHosted) change?.(value);
    else setOwnPalette(value);
  };
  const [tab, setTab] = useState(LAST_TAB.get(accounting ? "accounting" : mode) ?? (accounting ? "accounting" : mode === "maths" ? "basic" : mode));
  const [keyIndex, setKeyIndex] = useState(0);
  const [error, setError] = useState<{ code: string; word?: string; position?: number } | null>(null);
  const [questions, setQuestions] = useState<string[]>([]);
  const [lines, setLines] = useState(1);
  const pasted = useRef(false);
  const actions = useRef<{ submit: () => boolean; cancel: () => boolean }>({ submit: () => false, cancel: () => false });
  const editorRef = useRef<ReturnType<typeof useEditor>>(null);
  const root = useRef<HTMLDivElement>(null);
  const receive = (data: Pick<DataTransfer, "getData">): boolean => {
    try {
      const text = clipboardText(data);
      const choices = multiline ? [] : pastedQuestions(text);
      if (choices.length > 0) setQuestions(choices);
      else if (editorRef.current !== null) { insertText(editorRef.current, text, mode); pasted.current = true; }
      setError(null);
    } catch (err) { setError({ code: err instanceof InputError ? err.code : "unknown_word" }); }
    return true;
  };
  const editor = useEditor({
    content: initialDocument ?? { type: "doc", content: [{ type: "paragraph", content: inlineContent(initialText, mode) }] },
    extensions: [...extensions(placeholder, mode), Mark.create({ name: "dictationUncertain", parseHTML: () => [{ tag: "span[data-dictation-uncertain]" }],
      renderHTML: () => ["span", { "data-dictation-uncertain": "", class: "underline decoration-dotted", title: t("voice.uncertain_dictation") }, 0] }), Extension.create({
      name: "entryKeys",
      addKeyboardShortcuts: () => ({
        Enter: ({ editor: e }) => multiline ? e.commands.setHardBreak() : actions.current.submit(),
        "Shift-Enter": ({ editor: e }) => e.commands.setHardBreak(),
        "Mod-Enter": () => actions.current.submit(),
        Escape: () => actions.current.cancel(),
      }),
    })],
    editable: !disabled,
    onBlur: () => { void draftStore.current?.flush(); },
    onUpdate: ({ editor: e }) => {
      const text = serialise(e.getJSON());
      setLines(text.split("\n").length);
      onChange?.(text);
      if (accounting) setJournal(journalFromText(text));
      draftStore.current?.change(e.getJSON());
    },
    editorProps: {
      handleKeyDown: (_view, event) => { if (event.ctrlKey && event.code === "KeyM" && onDictate) { event.preventDefault(); if (!event.repeat) onDictate(true); return true; } return false; },
      handleDOMEvents: {
        // Handle raw clipboard/drop data before ProseMirror constructs an HTML slice (and its resource nodes).
        paste: (_view, event) => { event.preventDefault(); return event.clipboardData === null ? true : receive(event.clipboardData); },
        drop: (_view, event) => {
          if (event.dataTransfer === null || event.dataTransfer.files.length > 0) return false;
          event.preventDefault(); return receive(event.dataTransfer);
        },
        keyup: (_view, event) => { if (event.code === "KeyM" && onDictate) { onDictate(false); return true; } return false; } },
      attributes: { "aria-label": label, role: "textbox", "aria-multiline": String(multiline), class: look === "composer"
        ? "max-h-60 min-h-9 flex-1 overflow-y-auto px-2 py-1.5 text-body text-ink"
        : "min-h-12 flex-1 rounded-control border border-line-strong bg-canvas px-3 py-2 text-body" },
    },
  });
  editorRef.current = editor;
  useEffect(() => { editor?.setEditable(!disabled); }, [editor, disabled]);
  useEffect(() => {
    if (editor === null || session === null || sessionId === undefined || draftSlot === undefined) return;
    const controller = new DraftController(session.commands, `/v1/sessions/${sessionId}/drafts/${draftSlot}`,
      (doc) => { editor.commands.setContent(doc); onChange?.(serialise(editor.getJSON())); }, () => setDraftError(true));
    draftStore.current = controller;
    void controller.open();
    const save = () => { void controller.flush(); };
    const hidden = () => { if (document.visibilityState === "hidden") save(); };
    window.addEventListener("pagehide", save);
    document.addEventListener("visibilitychange", hidden);
    return () => { window.removeEventListener("pagehide", save); document.removeEventListener("visibilitychange", hidden); draftStore.current = null; controller.close(); };
  }, [editor, session, sessionId, draftSlot]);
  const submit = (): boolean => {
    if (editor === null || disabled || questions.length > 0) return true;
    // An uncommitted inline edit belongs to the draft, not the old node attributes. Never submit the old value.
    const editing = root.current?.querySelector<HTMLInputElement>("[data-math-node] input");
    if (editing) { editing.focus(); return true; }
    const text = normaliseInput(serialise(editor.getJSON())).trim();
    if (text === "") return true;
    if (text.length > INPUT_LIMITS.text) { setError({ code: "too_long" }); return true; }
    let issue: typeof error = null;
    editor.state.doc.descendants((node) => {
      if (node.type.name === "math") issue ??= convertEntry(String(node.attrs.source), node.attrs.mode as EntryMode).error;
    });
    try {
      const blocks = serialiseBlocks(editor.getJSON());
      if (blocks.length > INPUT_LIMITS.blocks) throw new InputError("too_long", 0);
      for (const block of blocks) if (block.type === "ledger_table") ledgerTotals(block);
    } catch (err) { issue = { code: err instanceof InputError ? err.code : "ledger_rows" }; }
    if (issue !== null) { setError(issue); return true; }
    void draftStore.current?.flush();
    if (onSubmit(text, pasted.current)) {
      editor.commands.clearContent();
      pasted.current = false;
      setError(null);
    }
    return true;
  };
  actions.current = { submit, cancel: () => {
    if (questions.length > 0) setQuestions([]);
    else if (palette) setPalette(false);
    else onCancel?.();
    return true;
  } };
  useImperativeHandle(ref, () => ({ text: () => editor === null ? "" : serialise(editor.getJSON()),
    insert: (text, uncertain = false) => {
      if (editor === null) return;
      const from = editor.state.selection.from;
      insertText(editor, text, mode);
      const to = editor.state.selection.to;
      if (uncertain && to > from) editor.chain().setTextSelection({ from, to }).setMark("dictationUncertain")
        .setTextSelection(to).unsetMark("dictationUncertain").run();
    },
    blocks: () => editor === null ? [] : serialiseBlocks(editor.getJSON()),
    clear: () => { editor?.commands.clearContent(); if (editor !== null) void draftStore.current?.clear(editor.getJSON()); }, focus: () => { editor?.commands.focus(); }, submit: () => { actions.current.submit(); } }), [editor]);
  const selected: (typeof PALETTES)[number] = PALETTES.find((p) => p.key === tab) ?? { key: "basic", mode: "maths", entries: [] };
  return <div ref={root} className="min-w-0 flex-1" onKeyDownCapture={(event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault(); event.stopPropagation(); actions.current.submit();
    } else if (event.altKey && event.key.toLowerCase() === "m" && !disabled) {
      event.preventDefault(); event.stopPropagation(); setPalette(!palette);
      if (palette) editor?.commands.focus();
      else requestAnimationFrame(() => root.current?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')?.focus());
    } else if (event.key === "Escape" && (questions.length > 0 || palette) && !(event.target instanceof HTMLInputElement && event.target.closest("[data-math-node]"))) {
      event.preventDefault(); event.stopPropagation(); actions.current.cancel(); editor?.commands.focus();
    }
  }}>
    <div className="flex gap-2">
      {multiline && numbered && <div data-line-numbers="" aria-hidden="true" className="py-2 font-mono text-meta text-faint">{Array.from({ length: Math.min(lines, 30) }, (_, i) => <div key={i}>{firstLine + i}</div>)}</div>}
      <EditorContent editor={editor} className="flex-1" />
    </div>
    {/* A quiet tool under the field, not a second row of full buttons beside the host's own actions. */}
    {!hosted && <div className="flex"><Button size="sm" tone="ghost" disabled={disabled} aria-expanded={palette} onClick={() => setPalette((open) => !open)}><Icon name="maths" size={16} />{t("input.keyboard")}</Button></div>}
    {palette && <div data-entry-palette="" className="mt-1 flex flex-col gap-2 rounded-card bg-panel p-1">
      <div className="flex gap-2" role="tablist" aria-label={t("input.keyboard")} onKeyDown={(event) => moveKey(event, 'button[role="tab"]', true)}>
        {PALETTES.map((p) => <Button key={p.key} type="button" size="sm" tone="ghost" role="tab" tabIndex={tab === p.key ? 0 : -1} aria-selected={tab === p.key} onClick={() => { setTab(p.key); setKeyIndex(0); LAST_TAB.set(accounting ? "accounting" : mode, p.key); }}>{t(`input.palette.${p.key}`)}</Button>)}
      </div>
      {selected.entries.length > 0 && <div role="tabpanel" aria-label={t(`input.palette.${tab}`)}><div role={selected.entries.length ? "toolbar" : undefined} aria-label={selected.entries.length ? t(`input.palette.${tab}`) : undefined} onKeyDown={(event) => moveKey(event, "button:not([disabled])")} className="flex flex-wrap gap-1.5 [&>button]:min-w-11 [&>button]:justify-center">
        {selected.entries.map((source, index) => <Button key={source} data-key={source} type="button" size="sm" disabled={disabled} tabIndex={keyIndex === index ? 0 : -1} onFocus={() => setKeyIndex(index)} aria-label={KEY_NAMES[source] ? t("input.key.named", { name: t(`input.key.${source === "□ -> □" && selected.mode === "maths" ? "tends" : KEY_NAMES[source]}`), source }) : t("input.insert", { source: faceName(keyFace(source, selected.mode === "chem" && source === "Delta" ? "maths" : selected.mode)) })} onClick={() => {
          const entryMode = selected.mode === "chem" && source === "Delta" ? "maths" : selected.mode;
          editor?.chain().insertContent({ type: "math", attrs: { source, latex: convertEntry(source, entryMode).latex, mode: entryMode } }).run();
        }}><span aria-hidden="true" className="text-body leading-none"><Tex tex={keyFace(source, selected.mode === "chem" && source === "Delta" ? "maths" : selected.mode)} /></span></Button>)}
      </div></div>}
    {tab === "accounting" && <div role="toolbar" aria-label={t("input.palette.accounting")} className="flex flex-wrap gap-1.5" onKeyDown={(event) => moveKey(event, "button:not([disabled])")}>
      {["$", "Dr", "Cr", "c/d", "b/d", "(0)"].map((source, index) => <Button key={source} tabIndex={keyIndex === index ? 0 : -1} onFocus={() => setKeyIndex(index)} disabled={disabled} onClick={() => { if (editor !== null) insertText(editor, source, "maths"); }}>{source}</Button>)}
      {(["journal", "t_account", "trial_balance"] as LedgerTemplate[]).map((template, index) => <Button key={template} tabIndex={keyIndex === index + 6 ? 0 : -1} onFocus={() => setKeyIndex(index + 6)} disabled={disabled} onClick={() => editor?.commands.insertContent({ type: "ledger_table", attrs: { template, rows: [LEDGER_COLUMNS[template].map(() => "")] } })}>{t(`ledger.template.${template}`)}</Button>)}
    </div>}
    </div>}
    {journal !== null && <Button disabled={disabled} onClick={() => {
      if (editor !== null && journal !== null) { editor.commands.setContent({ type: "doc", content: [{ type: "ledger_table", attrs: { template: "journal", rows: journal.rows } }] }); setJournal(null); }
    }}>{t("ledger.convert")}</Button>}
    {multiline && editor !== null && serialise(editor.getJSON()).split("\n").some((line) => (line.match(/(?<![<>!])=(?!=)/g) ?? []).length > 1) && <Button onClick={() => {
      const text = serialise(editor.getJSON()).split("\n").flatMap((line) => {
        const plain = line.replace(/\\\((.*?)\\\)/g, "$1");
        const parts = plain.split("=");
        return parts.length <= 2 ? [line] : parts.slice(0, -1).map((left, i) => `${left.trim()} = ${parts[i + 1]?.trim() ?? ""}`);
      }).join("\n");
      editor.commands.clearContent(); insertText(editor, text, mode);
    }}>{t("input.split_relations")}</Button>}
    {draftError && <div><ErrorState message={t("input.draft_failed")} /><Button onClick={() => { setDraftError(false); void draftStore.current?.open(); }}>{t("input.save_current_draft")}</Button></div>}
    {questions.length > 0 && <div role="group" aria-label={t("input.choose_question")} className="mt-2 bg-panel p-2">
      <p className="text-body">{t("input.choose_question")}</p>
      {questions.map((question, index) => <Button key={index} onClick={() => {
        if (editor !== null) { editor.commands.clearContent(); insertText(editor, question, mode); pasted.current = true; }
        setQuestions([]);
      }}>{t("input.question_number", { index: index + 1 })}: {question.slice(0, 120)}</Button>)}
      <Button onClick={() => { setQuestions([]); editor?.commands.focus(); }}>{t("action.cancel")}</Button>
    </div>}
    {error !== null && <ErrorState message={inputError(t, error)} />}
  </div>;
});
