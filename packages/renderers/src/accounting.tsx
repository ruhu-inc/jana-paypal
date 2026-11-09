// Board and accounting kinds (VSL-03): matrix_table, journal_entry, t_account, trial_balance, financial_statements and
// bank_reconciliation, drawn as HTML tables (rows and cells are the parts). Amounts are traced givens; totals,
// balances and statement lines come from acc_double_entry and acc_statements, formatted once by the engine.
import type { ReactElement, ReactNode } from "react";
import { accountingAmount } from "./notation.js";
import { define } from "./define.js";
import { WordsFallback, computed, computedLabel, described, params, partName, rowProps, type Scene } from "./kit.js";
import matrixSchema from "./schemas/matrix_table.schema.json" with { type: "json" };
import journalSchema from "./schemas/journal_entry.schema.json" with { type: "json" };
import statementsSchema from "./schemas/financial_statements.schema.json" with { type: "json" };
import reconciliationSchema from "./schemas/bank_reconciliation.schema.json" with { type: "json" };

function Board({ s, children }: { s: Scene; children: ReactNode }): ReactElement {
  return (
    <div className="my-2 overflow-x-auto rounded-card border border-line p-3 text-ui" role="group" aria-label={described(s)} data-kind={s.spec.type} data-visual-id={s.spec.visual_id}>
      {children}
    </div>
  );
}

/** A cell's text: hidden in hidden mode, a numbered blank in quiz mode (VSL-11). */
function cellText(s: Scene, id: string, text: ReactNode): ReactNode {
  if (s.labels === "hidden") return null;
  if (s.labels === "quiz") return <span className="inline-block min-w-8 border-b border-dashed border-line-strong text-center text-muted">{s.order.get(id) ?? ""}</span>;
  return text;
}

function money(currency: string | undefined, amount: string): string {
  return accountingAmount(amount, currency);
}

// --- matrix_table ---------------------------------------------------------------------------------------------------

function drawMatrix(s: Scene): ReactElement {
  const p = params<{ table: "matrix" | "frequency" | "truth"; name?: string; headers?: string[]; row_headers?: string[]; rows: string[][] }>(s.spec);
  const matrix = p.table === "matrix";
  return (
    <Board s={s}>
      <div className="flex items-center gap-2">
        {matrix && p.name !== undefined && <span className="text-body">{`${p.name} =`}</span>}
        <table className={matrix ? "border-x-2 border-ink" : "border-collapse"}>
          {(p.headers ?? []).length > 0 && (
            <thead><tr>{(p.row_headers ?? []).length > 0 && <th />}{(p.headers ?? []).map((h, i) => <th key={i} scope="col" className="px-2">{s.labels === "hidden" ? "" : h}</th>)}</tr></thead>
          )}
          <tbody>
            {p.rows.map((row, r) => (
              <tr key={r}>
                {(p.row_headers ?? []).length > 0 && <th scope="row" className="px-2">{s.labels === "hidden" ? "" : (p.row_headers?.[r] ?? "")}</th>}
                {row.map((value, c) => {
                  const id = `r${r + 1}c${c + 1}`;
                  return <td key={c} {...rowProps(s, id)} data-cell={id}><span className="px-2 tabular-nums">{cellText(s, id, value)}</span></td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Board>
  );
}

export const matrixTable = define({
  kind: "matrix_table", schema: matrixSchema, draw: drawMatrix,
  table: (spec) => {
    const p = params<{ headers?: string[]; rows: string[][] }>(spec);
    return { columns: p.headers ?? p.rows[0]?.map((_, i) => String(i + 1)) ?? [], rows: p.rows };
  },
});

// --- journal_entry --------------------------------------------------------------------------------------------------

interface JournalParams {
  date?: string;
  lines: { id: string; account: string; side: "debit" | "credit"; amount: string }[];
  narration?: string;
  currency?: string;
}

function drawJournal(s: Scene): ReactElement {
  const p = params<JournalParams>(s.spec);
  return (
    <Board s={s}>
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-line-strong"><th scope="col" className="text-start">{s.t("visual.ledger.date")}</th><th scope="col" className="text-start">{s.t("visual.ledger.account")}</th><th scope="col" className="text-end">{s.t("visual.ledger.debit")}</th><th scope="col" className="text-end">{s.t("visual.ledger.credit")}</th></tr>
        </thead>
        <tbody>
          {p.lines.map((line, i) => (
            <tr key={line.id} {...rowProps(s, line.id)}>
              <td data-presentation="true">{i === 0 ? (p.date ?? "") : ""}</td>
              <td className={line.side === "credit" ? "ps-6" : ""}>{cellText(s, line.id, line.account)}</td>
              <td className="text-end tabular-nums">{line.side === "debit" ? cellText(s, line.id, money(p.currency, line.amount)) : ""}</td>
              <td className="text-end tabular-nums">{line.side === "credit" ? cellText(s, line.id, money(p.currency, line.amount)) : ""}</td>
            </tr>
          ))}
          <tr className="border-t border-line-strong">
            <td /><td />
            <td {...rowProps(s, "total_debit")}><span className="block text-end tabular-nums">{cellText(s, "total_debit", money(p.currency, computedLabel(s.spec, "total_debit")))}</span></td>
            <td {...rowProps(s, "total_credit")}><span className="block text-end tabular-nums">{cellText(s, "total_credit", money(p.currency, computedLabel(s.spec, "total_credit")))}</span></td>
          </tr>
          {p.narration !== undefined && (
            <tr {...rowProps(s, "narration")}><td colSpan={4} className="text-muted italic">{cellText(s, "narration", p.narration)}</td></tr>
          )}
        </tbody>
      </table>
    </Board>
  );
}

export const journalEntry = define({ kind: "journal_entry", schema: journalSchema, draw: drawJournal });

// --- t_account ------------------------------------------------------------------------------------------------------

interface TAccountParams {
  accounts: { id: string; name: string; opening_balance?: string; opening_side?: "debit" | "credit" }[];
  entries: { id: string; date?: string; debit: string; credit: string; amount: string; narration?: string }[];
  currency?: string;
  show_balances?: boolean;
}

function drawTAccounts(s: Scene): ReactElement {
  const p = params<TAccountParams>(s.spec);
  const balances = computed<{ balances?: Record<string, { side: string; amount: string }> }>(s.spec).balances ?? {};
  const names = new Map(p.accounts.map((a) => [a.id, a.name]));
  return (
    <Board s={s}>
      <div className="grid gap-4 sm:grid-cols-2">
        {p.accounts.map((acc) => {
          const rows = p.entries.flatMap((e) => [
            ...(e.debit === acc.id ? [{ id: `${acc.id}.${e.id}`, side: "debit", text: names.get(e.credit) ?? e.credit, amount: e.amount }] : []),
            ...(e.credit === acc.id ? [{ id: `${acc.id}.${e.id}`, side: "credit", text: names.get(e.debit) ?? e.debit, amount: e.amount }] : []),
          ]);
          const balance = p.show_balances !== false ? balances[acc.id] : undefined;
          return (
            <table key={acc.id} className="w-full border-collapse">
              <thead>
                <tr><th colSpan={2} scope="colgroup" {...rowProps(s, acc.id)} className="border-b-2 border-ink text-center font-semibold">{s.labels === "hidden" ? <span className="sr-only">{partName(s, acc.id)}</span> : cellText(s, acc.id, acc.name)}</th></tr>
                <tr><th scope="col" className="w-1/2 border-e-2 border-ink text-start">{s.t("visual.ledger.debit")}</th><th scope="col" className="text-start ps-2">{s.t("visual.ledger.credit")}</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} {...rowProps(s, r.id)}>
                    <td className="border-e-2 border-ink">{r.side === "debit" ? cellText(s, r.id, `${r.text} ${money(p.currency, r.amount)}`) : ""}</td>
                    <td className="ps-2">{r.side === "credit" ? cellText(s, r.id, `${r.text} ${money(p.currency, r.amount)}`) : ""}</td>
                  </tr>
                ))}
                {balance !== undefined && (
                  <tr {...rowProps(s, `${acc.id}.balance_cd`)}>
                    <td className="border-e-2 border-ink">{balance.side === "debit" ? cellText(s, `${acc.id}.balance_cd`, `${s.t("visual.ledger.balance_cd")} ${money(p.currency, computedLabel(s.spec, `${acc.id}.balance_cd`))}`) : ""}</td>
                    <td className="ps-2">{balance.side === "credit" ? cellText(s, `${acc.id}.balance_cd`, `${s.t("visual.ledger.balance_cd")} ${money(p.currency, computedLabel(s.spec, `${acc.id}.balance_cd`))}`) : ""}</td>
                  </tr>
                )}
              </tbody>
            </table>
          );
        })}
      </div>
    </Board>
  );
}

export const tAccount = define({ kind: "t_account", schema: "visuals/kinds/t_account", draw: drawTAccounts });

// --- trial_balance --------------------------------------------------------------------------------------------------

function drawTrialBalance(s: Scene): ReactElement {
  const p = params<{ as_at: string; currency?: string }>(s.spec);
  const c = computed<{ rows?: { account: string; side: string; amount: string }[]; agrees?: boolean }>(s.spec);
  if (c.rows === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  return (
    <Board s={s}>
      <table className="w-full border-collapse">
        <caption className="text-start font-semibold">{`${s.t("visual.ledger.trial_balance")} ${p.as_at}`}</caption>
        <thead><tr className="border-b border-line-strong"><th scope="col" className="text-start">{s.t("visual.ledger.account")}</th><th scope="col" className="text-end">{s.t("visual.ledger.debit")}</th><th scope="col" className="text-end">{s.t("visual.ledger.credit")}</th></tr></thead>
        <tbody>
          {c.rows.map((row, i) => {
            const id = `row_${i + 1}`;
            return (
              <tr key={id} {...rowProps(s, id)}>
                <td>{cellText(s, id, row.account)}</td>
                <td className="text-end tabular-nums">{row.side === "debit" ? cellText(s, id, money(p.currency, computedLabel(s.spec, id))) : ""}</td>
                <td className="text-end tabular-nums">{row.side === "credit" ? cellText(s, id, money(p.currency, computedLabel(s.spec, id))) : ""}</td>
              </tr>
            );
          })}
          <tr className="border-t-2 border-line-strong font-semibold">
            <td>{s.t("visual.ledger.totals")}</td>
            <td {...rowProps(s, "total_debit")}><span className="block text-end tabular-nums">{cellText(s, "total_debit", money(p.currency, computedLabel(s.spec, "total_debit")))}</span></td>
            <td {...rowProps(s, "total_credit")}><span className="block text-end tabular-nums">{cellText(s, "total_credit", money(p.currency, computedLabel(s.spec, "total_credit")))}</span></td>
          </tr>
          {c.agrees === false && (
            <tr {...rowProps(s, "mismatch")}><td colSpan={3}>{cellText(s, "mismatch", `${s.t("visual.ledger.difference")} ${money(p.currency, computedLabel(s.spec, "mismatch"))}`)}</td></tr>
          )}
        </tbody>
      </table>
    </Board>
  );
}

export const trialBalance = define({ kind: "trial_balance", schema: "visuals/kinds/trial_balance", draw: drawTrialBalance });

// --- financial_statements and bank_reconciliation -------------------------------------------------------------------

interface StatementLine {
  id: string;
  label: string;
  op: string;
}

function statementAmount(amount: string, currency: string | undefined, less: boolean): string {
  const text = accountingAmount(amount, currency, true);
  return less && !text.startsWith("(") ? `(${text})` : text;
}

function drawStatement(s: Scene): ReactElement {
  const p = params<{ currency?: string; business?: string; period_end?: string }>(s.spec);
  const c = computed<{ title?: string; lines?: StatementLine[] }>(s.spec);
  if (c.lines === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  return (
    <Board s={s}>
      <table className="w-full border-collapse">
        {(c.title !== undefined || p.business !== undefined) && <caption className="text-start font-semibold">{[p.business, c.title, p.period_end].filter(Boolean).join(" · ")}</caption>}
        <tbody>
          {c.lines.map((line) => {
            const total = line.op === "total" || line.op === "subtotal";
            return (
              <tr key={line.id} {...rowProps(s, line.id)} data-op={line.op}>
                <td className={total ? "font-semibold" : line.op === "less" ? "ps-4" : ""}>{cellText(s, line.id, line.label)}</td>
                <td className={`text-end tabular-nums ${total ? "border-t border-line-strong font-semibold" : ""}`}>{cellText(s, line.id, statementAmount(computedLabel(s.spec, line.id), p.currency, line.op === "less"))}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Board>
  );
}

const statementTable = (spec: Parameters<typeof computedLabel>[0]) => ({
  columns: ["visual.table.line", "visual.table.amount"],
  rows: (computed<{ lines?: StatementLine[] }>(spec).lines ?? []).map((l) => [l.label, computedLabel(spec, l.id)]),
});

export const financialStatements = define({ kind: "financial_statements", schema: statementsSchema, draw: drawStatement, table: statementTable });
export const bankReconciliation = define({ kind: "bank_reconciliation", schema: reconciliationSchema, draw: drawStatement, table: statementTable });
