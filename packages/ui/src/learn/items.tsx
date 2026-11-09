// One practice item's answer and its marking (UXS-16, UXS-23, UXS-24; ASM-02): the inputs of each part (choices as
// large rows, tick boxes, a number with its unit, algebra, solution sets, an order to arrange) and the verdict in the UXS-16 words, an icon and
// a word for every outcome on its learning colour, never colour alone and never the word "wrong". Shared by the lesson
// player's checks and Practice.
import { useId, useRef, type RefObject } from "react";
import { canonicalDecimalCommas, convertEntry, convertMath, unitLatexOf, type CheckPart, type PartAnswer, type PracticeItem } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { Button, Callout, FIELD_LOOK, IconButton } from "../primitives/index.js";
import { Rendered } from "../registry/registry.js";
import { PackInline } from "./PackText.js";
import { SubjectInput } from "./SubjectInputs.js";

export type ItemPart = PracticeItem["parts"][number];
export type Draft = { option?: string; options?: string[]; value?: string; unit?: string; order?: string[];
  lines?: { account_id: string; side: "debit" | "credit"; amount: string }[]; rows?: string[][]; labels?: Record<string, string> };

export function ItemVisual({ item }: { item: PracticeItem }) {
  return item.visual == null ? null : <Rendered keys={[`visual:${item.visual.type}`, "visual:*"]} data={item.visual} lifecycle="complete" partId={`assessment-visual-${item.item_id}`} />;
}

/** The marker's error categories with their own "where to look" line; any other reads as a general one. */
const WHY: readonly string[] = ["unreadable", "value", "power_of_ten", "unit_error", "units_missing", "sig_figs", "misconception"];

const CHOICE = "flex min-h-12 cursor-pointer items-center gap-3 rounded-control border border-line-strong bg-canvas px-4 text-body text-ink hover:bg-wash has-[:checked]:border-ink has-[:checked]:bg-wash has-[:checked]:font-semibold";

/** One symbol the bar inserts: what it shows, what it types into the field (plain grammar the checker reads), and its
 * spoken name. `caret` places the cursor back inside the inserted text ("√(" … ")"). */
type NotationKey = { shown: string; typed: string; name: string; caret?: number };
const NUMBER_KEYS: NotationKey[] = [
  { shown: "×10ⁿ", typed: "×10^", name: "times_ten" }, { shown: "xⁿ", typed: "^", name: "power" },
  { shown: "−", typed: "-", name: "minus" }, { shown: "÷", typed: "/", name: "fraction" },
];
const UNIT_KEYS: NotationKey[] = [
  { shown: "x⁻¹", typed: "^-1", name: "inverse" }, { shown: "x²", typed: "^2", name: "squared" }, { shown: "x³", typed: "^3", name: "cubed" },
  { shown: "°C", typed: "°C", name: "degrees_c" }, { shown: "Ω", typed: "Ω", name: "ohm" }, { shown: "µ", typed: "µ", name: "micro" },
];
const ALGEBRA_KEYS: NotationKey[] = [
  { shown: "xⁿ", typed: "^", name: "power" }, { shown: "√", typed: "sqrt()", name: "root", caret: 5 }, { shown: "÷", typed: "/", name: "fraction" },
  { shown: "π", typed: "π", name: "pi" }, { shown: "×", typed: "*", name: "times" }, { shown: "±", typed: "+-", name: "plus_minus" },
];

/** A small symbol bar under an answer (Desmos-, Khan-style): each key types plain notation at the cursor of the field
 * it serves, so nobody needs a maths keyboard or to know "^" and "*". */
function NotationBar({ keys, field, value, onValue }: { keys: NotationKey[]; field: RefObject<HTMLInputElement | null>; value: string; onValue: (next: string) => void }) {
  const t = useT();
  const insert = (key: NotationKey) => {
    const el = field.current;
    const from = el?.selectionStart ?? value.length, to = el?.selectionEnd ?? value.length;
    onValue(value.slice(0, from) + key.typed + value.slice(to));
    const at = from + (key.caret ?? key.typed.length);
    requestAnimationFrame(() => { el?.focus(); el?.setSelectionRange(at, at); });
  };
  return (
    <div role="toolbar" aria-label={t("check.symbols")} className="flex flex-wrap gap-1">
      {keys.map((key) => <Button key={key.name} size="sm" tone="ghost" onMouseDown={(e) => e.preventDefault()} onClick={() => insert(key)}><span aria-hidden="true">{key.shown}</span><span className="sr-only">{t(`check.symbol.${key.name}`)}</span></Button>)}
    </div>
  );
}

/** The answer as it will be read, typeset, once it has notation in it ("3.2x10^-4", "m/s^2"): a plain number needs none. */
function ReadsAs({ tex, unitEcho = false }: { tex: string | null; unitEcho?: boolean }) {
  const t = useT();
  if (tex === null) return null;
  return <p aria-hidden="true" data-reads-as="" data-unit-echo={unitEcho ? "" : undefined} className="flex flex-wrap items-baseline gap-2 text-ui text-muted">{t("check.reads_as")}<span className="text-body text-ink"><Tex tex={tex} /></span></p>;
}

const NOTATION = /[eE^xX×*·/⁰¹²³⁴⁵⁶⁷⁸⁹⁻√π]/;

/** A number, with a unit field and the unit symbols only when the answer has a unit (`unit_expected`; an older core
 * that does not say keeps them). */
function NumericInput({ part, draft, onDraft, prompt, unitPrompt, unitExpected }: { part: ItemPart; draft: Draft; onDraft: (d: Draft) => void; prompt: boolean; unitPrompt: string | null; unitExpected: boolean }) {
  const t = useT();
  const valueField = useRef<HTMLInputElement>(null), unitField = useRef<HTMLInputElement>(null);
  const value = draft.value ?? "", unit = unitExpected ? draft.unit ?? "" : "";
  // The unit field keeps what was typed; a unit with a power or a product, or a value in standard form, reads back
  // typeset as on the board ("m/s^2" as m/s², "3.2x10^-4" as 3.2 × 10⁻⁴).
  const unitTex = /[\^*·⁰¹²³⁴⁵⁶⁷⁸⁹⁻]|ohm/.test(unit) ? unitLatexOf(unit) : null;
  const valueTex = NOTATION.test(value) ? convertMath(value) : null;
  const both = value.trim() !== "" && unit.trim() !== "" ? convertEntry(`${value.trim()} ${unit.trim()}`, "units") : null;
  const tex = valueTex === null && unitTex === null ? null
    : both !== null && both.error === null ? both.latex
    : valueTex !== null && valueTex.error === null ? valueTex.latex : unitTex;
  return (
    <div className="flex flex-col gap-3">
      {prompt && <p className="text-body text-ink"><PackInline text={part.prompt} /></p>}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-48 flex-1 flex-col gap-1.5 text-meta font-medium text-muted">
          {t("check.answer")}
          <input ref={valueField} className={FIELD_LOOK} inputMode="decimal" autoComplete="off" value={value} onChange={(e) => onDraft({ ...draft, value: e.target.value })} />
        </label>
        {unitExpected && <label className="flex w-36 flex-col gap-1.5 text-meta font-medium text-muted">
          {t("check.unit")}
          <input ref={unitField} className={FIELD_LOOK} autoComplete="off" value={unit} onChange={(e) => onDraft({ ...draft, unit: e.target.value })} />
        </label>}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <NotationBar keys={NUMBER_KEYS} field={valueField} value={value} onValue={(next) => onDraft({ ...draft, value: next })} />
        {unitExpected && <NotationBar keys={UNIT_KEYS} field={unitField} value={unit} onValue={(next) => onDraft({ ...draft, unit: next })} />}
      </div>
      <ReadsAs tex={tex} unitEcho={unitTex !== null} />
      {unitPrompt !== null && <p className="text-meta text-muted">{t("check.unit_prompt", { unit: unitPrompt })}</p>}
    </div>
  );
}

function AlgebraInput({ part, draft, onDraft, prompt, help, typeset }: { part: ItemPart; draft: Draft; onDraft: (d: Draft) => void; prompt: boolean; help: string; typeset: boolean }) {
  const t = useT();
  const id = useId();
  const field = useRef<HTMLInputElement>(null);
  const value = draft.value ?? "";
  // Read with the same grammar as the board ("sqrt(2)" as √2, "x^2" as x²), not as raw LaTeX.
  const read = typeset && value.trim() !== "" ? convertMath(value) : null;
  return <div className="flex flex-col gap-2">
    {prompt && <p className="text-body text-ink"><PackInline text={part.prompt} /></p>}
    <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
      {t("check.answer")}
      <input ref={field} className={FIELD_LOOK} autoComplete="off" spellCheck={false} maxLength={4000} value={value}
        aria-describedby={`${id}-help`} onChange={(event) => onDraft({ value: event.target.value })} />
    </label>
    <NotationBar keys={ALGEBRA_KEYS} field={field} value={value} onValue={(next) => onDraft({ value: next })} />
    <p id={`${id}-help`} className="text-meta text-muted">{help}</p>
    <ReadsAs tex={read === null ? null : read.error === null ? read.latex : value} />
  </div>;
}

/** One part's input: choices, a number with its unit, algebra, solution values, or an order to arrange. */
export function PartInput({ part, draft, onDraft, itemId, prompt = true }: { part: ItemPart; draft: Draft; onDraft: (d: Draft) => void; itemId: string; prompt?: boolean }) {
  const t = useT();
  const id = useId();
  const input = part.input;
  if (input.kind === "mcq" || input.kind === "multi_select") {
    const multi = input.kind === "multi_select";
    return (
      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className={prompt ? "mb-2 text-body text-ink" : "sr-only"}><PackInline text={part.prompt} /></legend>
        {input.options.map((o) => {
          const checked = multi ? (draft.options ?? []).includes(o.id) : draft.option === o.id;
          return (
            <label key={o.id} className={CHOICE}>
              <input
                type={multi ? "checkbox" : "radio"}
                name={`${itemId}-${part.part_id}-${id}`}
                value={o.id}
                checked={checked}
                onChange={() => onDraft(multi ? { options: checked ? (draft.options ?? []).filter((x) => x !== o.id) : [...(draft.options ?? []), o.id] } : { option: o.id })}
                className="size-4 shrink-0"
              />
              <span><PackInline text={o.label} /></span>
            </label>
          );
        })}
      </fieldset>
    );
  }
  if (input.kind === "numeric") return <NumericInput part={part} draft={draft} onDraft={onDraft} prompt={prompt} unitPrompt={input.unit_prompt} unitExpected={input.unit_expected !== false} />;
  if (input.kind === "algebraic" || input.kind === "equation_solutions") {
    return <AlgebraInput part={part} draft={draft} onDraft={onDraft} prompt={prompt} help={input.kind === "algebraic" ? t("check.expression_help") : t("check.solutions_help", { variable: input.variable })} typeset={input.kind === "algebraic"} />;
  }
  if (input.kind === "ordering") {
    const order = draft.order ?? input.elements.map((e) => e.id);
    const label = (elementId: string) => input.elements.find((e) => e.id === elementId)?.label ?? elementId;
    const move = (from: number, to: number) => {
      const next = [...order];
      const [moved] = next.splice(from, 1);
      if (moved !== undefined) next.splice(to, 0, moved);
      onDraft({ order: next });
    };
    return (
      <div className="flex flex-col gap-2">
        {prompt && <p className="text-body text-ink"><PackInline text={part.prompt} /></p>}
        <ol className="flex flex-col gap-2">
          {order.map((elementId, i) => (
            <li key={elementId} className="flex min-h-12 items-center gap-2 rounded-control border border-line-strong bg-canvas ps-4 pe-1 text-body text-ink">
              <span className="w-5 text-meta text-faint tabular-nums">{i + 1}</span>
              <span className="flex-1"><PackInline text={label(elementId)} /></span>
              <IconButton icon="move-up" label={t("check.order_up", { label: label(elementId) })} disabled={i === 0} onClick={() => move(i, i - 1)} />
              <IconButton icon="move-down" label={t("check.order_down", { label: label(elementId) })} disabled={i === order.length - 1} onClick={() => move(i, i + 1)} />
            </li>
          ))}
        </ol>
      </div>
    );
  }
  return <SubjectInput part={part} draft={draft} onDraft={onDraft} prompt={prompt} />;
}

/** The student's answer to one part from its draft, or null while there is nothing to check. */
export function answerOf(part: ItemPart, draft: Draft): PartAnswer | null {
  const input = part.input;
  if (input.kind === "mcq") return draft.option === undefined ? null : { part_id: part.part_id, kind: "mcq", option_id: draft.option };
  if (input.kind === "multi_select") return (draft.options ?? []).length === 0 ? null : { part_id: part.part_id, kind: "multi_select", option_ids: draft.options ?? [] };
  if (input.kind === "numeric") {
    const raw = (draft.value ?? "").trim();
    const value = /^[+-]?(?:\d+,\d+|,\d+)(?:[eE][+-]?\d+)?$/.test(raw) ? canonicalDecimalCommas(raw) : raw;
    const unit = input.unit_expected === false ? "" : (draft.unit ?? "").trim();
    return value === "" ? null : { part_id: part.part_id, kind: "numeric", value, unit: unit === "" ? null : unit };
  }
  if (input.kind === "ordering") return { part_id: part.part_id, kind: "ordering", element_ids: draft.order ?? input.elements.map((e) => e.id) };
  if (input.kind === "algebraic") {
    const latex = (draft.value ?? "").trim();
    return latex === "" ? null : { part_id: part.part_id, kind: "algebraic", latex };
  }
  if (input.kind === "equation_solutions") {
    const values = (draft.value ?? "").split(",").map((value) => value.trim());
    return values.length > 40 || values.some((value) => value === "") ? null : { part_id: part.part_id, kind: "equation_solutions", values };
  }
  if (input.kind === "chemical_equation") return (draft.value ?? "").trim() === "" ? null : { part_id: part.part_id, kind: "chemical_equation", equation: draft.value ?? "" };
  if (input.kind === "short_text") return (draft.value ?? "").trim() === "" ? null : { part_id: part.part_id, kind: "short_text", text: draft.value ?? "" };
  if (input.kind === "journal_entry") {
    const lines = draft.lines ?? [];
    return lines.every((line) => line.account_id === "" && line.amount === "" && line.side === "debit") ? null : { part_id: part.part_id, kind: "journal_entry", lines };
  }
  if (input.kind === "accounting_table") return (draft.rows ?? []).every((row) => row.every((cell) => cell === "")) ? null : { part_id: part.part_id, kind: "accounting_table", rows: draft.rows ?? [] };
  if (input.kind === "diagram_label") {
    const labels = input.target_ids.map((target_id) => ({ target_id, label: draft.labels?.[target_id] ?? "" }));
    return labels.every((label) => label.label === "") ? null : { part_id: part.part_id, kind: "diagram_label", labels };
  }
  return null;
}

const VERDICT_TONE = { correct: "verified", incorrect: "look", partially_correct: "hint" } as const;

/** One part's verdict in the UXS-16 words: icon and word, then where to look, on its learning colour. */
export function Feedback({ part }: { part: CheckPart }) {
  const t = useT();
  const known = WHY.includes(part.category ?? "") && !(part.category === "misconception" && part.misconception === null);
  const why = part.verdict === "correct" || part.verdict === "not_checked" || part.category === null ? null
    : t(`check.why.${known ? part.category : "other"}`, { name: part.misconception?.name ?? "" });
  const marks = part.verdict !== "not_checked" ? <p className="text-meta text-muted tabular-nums">{t("check.marks_awarded", { awarded: part.marks_awarded, total: part.marks_total })}</p> : null;
  if (part.verdict === "not_checked") {
    return <p data-verdict={part.verdict} className="text-ui text-muted">{t("check.verdict.not_checked")}</p>;
  }
  return (
    <Callout tone={VERDICT_TONE[part.verdict]} data-verdict={part.verdict} heading={why === null ? t(`check.verdict.${part.verdict}`) : <>{t(`check.verdict.${part.verdict}`)} <span className="font-regular">{why}</span></>}>
      {part.feedback !== null && part.verdict !== "correct" && <p><PackInline text={part.feedback} /></p>}
      {marks}
    </Callout>
  );
}
