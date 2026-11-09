import type { Draft, ItemPart } from "./items.js";
import { useT } from "../i18n.js";
import { Button, EmptyState, FIELD_LOOK } from "../primitives/index.js";
import { PackInline } from "./PackText.js";

/** Subject-specific fields share the same typed drafts in lessons, practice, checks and saved mock answers. */
export function SubjectInput({ part, draft, onDraft, prompt }: { part: ItemPart; draft: Draft; onDraft: (draft: Draft) => void; prompt: boolean }) {
  const t = useT(), input = part.input;
  const heading = prompt ? <p className="text-body text-ink"><PackInline text={part.prompt} /></p> : null;
  if (input.kind === "chemical_equation") return <div className="flex flex-col gap-3">
    {heading}<p className="text-body text-ink"><code>{input.unbalanced_equation}</code></p>
    <label className="flex flex-col gap-2 text-meta text-muted">{t("check.chemical_equation")}
      <input className={FIELD_LOOK} autoComplete="off" spellCheck={false} maxLength={4000} value={draft.value ?? ""} onChange={(e) => onDraft({ value: e.target.value })} />
    </label>
    <p className="text-meta text-muted">{input.state_symbols_required ? t("check.chemical_states") : t("check.chemical_help")}</p>
  </div>;
  if (input.kind === "short_text") return <div className="flex flex-col gap-3">
    {heading}<label className="flex flex-col gap-2 text-meta text-muted">{t("check.written_answer")}
      <textarea className={`${FIELD_LOOK} min-h-28 py-2`} maxLength={input.max_characters} value={draft.value ?? ""} onChange={(e) => onDraft({ value: e.target.value })} />
    </label><p className="text-meta text-muted">{t("check.text_limit", { count: input.max_characters })}</p>
  </div>;
  if (input.kind === "diagram_label") return <fieldset className="flex min-w-0 flex-col gap-3">
    <legend className={prompt ? "mb-2 text-body text-ink" : "sr-only"}><PackInline text={part.prompt} /></legend>
    {input.target_ids.map((target) => <label key={target} className="flex flex-col gap-2 text-meta text-muted">
      {t("check.diagram_target", { target: target.replace(/^label_/, "") })}
      {input.label_choices.length > 0 ? <select className={FIELD_LOOK} value={draft.labels?.[target] ?? ""} onChange={(e) => onDraft({ labels: { ...draft.labels, [target]: e.target.value } })}>
        <option value="">{t("check.choose_label")}</option>{input.label_choices.map((choice) => <option key={choice.id} value={choice.label}>{choice.label}</option>)}
      </select> : <input className={FIELD_LOOK} maxLength={200} autoComplete="off" value={draft.labels?.[target] ?? ""} onChange={(e) => onDraft({ labels: { ...draft.labels, [target]: e.target.value } })} />}
    </label>)}
  </fieldset>;
  if (input.kind === "journal_entry") {
    const lines = draft.lines ?? [{ account_id: "", side: "debit" as const, amount: "" }];
    const change = (i: number, update: Partial<typeof lines[number]>) => onDraft({ lines: lines.map((line, n) => n === i ? { ...line, ...update } : line) });
    return <div className="flex flex-col gap-3">{heading}<p className="text-meta text-muted">{t("check.currency", { currency: input.currency })}</p>
      {lines.map((line, i) => <fieldset key={i} className="flex min-w-0 flex-col gap-3 rounded-control border border-line p-4">
        <legend className="px-1 text-meta text-muted">{t("check.entry_number", { number: i + 1 })}</legend>
        <label className="flex flex-col gap-2 text-meta text-muted">{t("check.account")}
          {input.account_options.length > 0 ? <select className={FIELD_LOOK} value={line.account_id} onChange={(e) => change(i, { account_id: e.target.value })}>
            <option value="">{t("check.choose_account")}</option>{input.account_options.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select> : <input className={FIELD_LOOK} value={line.account_id} maxLength={96} onChange={(e) => change(i, { account_id: e.target.value })} />}
        </label>
        <div className="flex flex-wrap gap-3">
          <label className="flex min-w-32 flex-1 flex-col gap-2 text-meta text-muted">{t("check.side")}
            <select className={FIELD_LOOK} value={line.side} onChange={(e) => change(i, { side: e.target.value as "debit" | "credit" })}>
              <option value="debit">{t("check.debit")}</option><option value="credit">{t("check.credit")}</option>
            </select>
          </label>
          <label className="flex min-w-32 flex-1 flex-col gap-2 text-meta text-muted">{t("check.amount")}
            <input className={FIELD_LOOK} inputMode="decimal" maxLength={80} value={line.amount} onChange={(e) => change(i, { amount: e.target.value })} />
          </label>
        </div>
        <Button size="sm" disabled={lines.length === 1} onClick={() => onDraft({ lines: lines.filter((_, n) => n !== i) })}>{t("check.remove_entry", { number: i + 1 })}</Button>
      </fieldset>)}
      <Button disabled={lines.length >= 200} onClick={() => onDraft({ lines: [...lines, { account_id: "", side: "debit", amount: "" }] })}>{t("check.add_entry")}</Button>
    </div>;
  }
  if (input.kind === "accounting_table") {
    const empty = () => input.headers.map(() => "");
    const rows = draft.rows ?? Array.from({ length: Math.max(1, input.row_prompts.length) }, empty);
    return <div className="flex min-w-0 flex-col gap-3">{heading}
      <p className="text-meta text-muted">{t("check.currency", { currency: input.currency })}</p>
      <p className="text-meta text-muted">{t("check.accounting_help")}</p>
      {rows.map((row, i) => <fieldset key={i} className="flex min-w-0 flex-col gap-3 rounded-control border border-line p-4">
        <legend className="px-1 text-meta text-muted">{t("check.row_number", { number: i + 1 })}</legend>
        {input.row_prompts[i] !== undefined && <p className="text-ui text-ink"><PackInline text={input.row_prompts[i] ?? ""} /></p>}
        <div className="grid min-w-0 gap-3 sm:grid-cols-3">{input.headers.map((header, col) => <label key={col} className="flex min-w-0 flex-col gap-2 text-meta text-muted">
          {header === "Account" ? t("check.account") : header === "Amount" ? t("check.amount") : header === "Side" ? t("check.side") : header}<input className={FIELD_LOOK} maxLength={200} value={row[col] ?? ""} onChange={(e) => onDraft({ rows: rows.map((r, n) => n === i ? input.headers.map((_, j) => j === col ? e.target.value : r[j] ?? "") : r) })} />
        </label>)}</div>
        <Button size="sm" disabled={rows.length === 1 || i < input.row_prompts.length} onClick={() => onDraft({ rows: rows.filter((_, n) => n !== i) })}>{t("check.remove_row", { number: i + 1 })}</Button>
      </fieldset>)}
      <Button disabled={rows.length >= 200} onClick={() => onDraft({ rows: [...rows, empty()] })}>{t("check.add_row")}</Button>
    </div>;
  }
  return <EmptyState message={t("lesson.no_items")} />;
}
