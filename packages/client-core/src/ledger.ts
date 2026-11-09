// INP-10: exact decimal totals are display information; the worker decides accounting correctness.
import type { ContentBlock } from "@jana/contracts-ts";
import { InputError, normaliseInput } from "./input.js";

export type LedgerTable = Extract<ContentBlock, { type: "ledger_table" }>;
export type LedgerTemplate = LedgerTable["template"];
export const LEDGER_COLUMNS: Record<LedgerTemplate, string[]> = {
  journal: ["Date", "Particulars", "Folio", "Dr", "Cr"],
  t_account: ["Dr Date", "Dr Details", "Dr Amount", "Cr Date", "Cr Details", "Cr Amount"],
  trial_balance: ["Account", "Dr", "Cr"],
};
const CURRENCY: Record<string, string> = { "$": "USD", "£": "GBP", "€": "EUR", "₦": "NGN", "¥": "JPY" };
interface Amount { units: bigint; scale: number; currency: string }

function amount(raw: string): Amount {
  const text = normaliseInput(raw).trim();
  const match = /^(\()?\s*(-)?\s*([$£€₦¥]|[A-Z]{3})?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,8}))?\s*(\))?$/.exec(text);
  if (!match || Boolean(match[1]) !== Boolean(match[6]) || text.length > 30) throw new InputError("ledger_amount", 0);
  const fraction = match[5] ?? "";
  const digits = (match[4] ?? "0").replace(/,/g, "") + fraction;
  return { units: BigInt(digits) * (match[1] !== undefined || match[2] !== undefined ? -1n : 1n), scale: fraction.length,
    currency: CURRENCY[match[3] ?? ""] ?? match[3] ?? "" };
}

function decimal(units: bigint, scale: number): string {
  const sign = units < 0n ? "-" : "";
  const digits = (units < 0n ? -units : units).toString().padStart(scale + 1, "0");
  const tail = scale === 0 ? "" : digits.slice(-scale).replace(/0+$/, "");
  return sign + (scale === 0 ? digits : digits.slice(0, -scale)) + (tail === "" ? "" : `.${tail}`);
}

export function ledgerTable(template: LedgerTemplate, rows: string[][]): LedgerTable {
  const columns = LEDGER_COLUMNS[template];
  if (rows.length < 1 || rows.length > 60 || rows.some((row) => row.length !== columns.length)) throw new InputError("ledger_rows", 0);
  const clean = rows.map((row) => row.map((cell) => {
    const text = normaliseInput(cell);
    if (text.length > 80) throw new InputError("too_long", 80);
    return text;
  }));
  const fallback = [columns.join(" | "), ...clean.map((row) => row.join(" | "))].join("\n");
  if (fallback.length > 4000) throw new InputError("too_long", 4000);
  return { type: "ledger_table", template, columns: [...columns], rows: clean, text_fallback: fallback };
}

export function ledgerTotals(table: LedgerTable): { dr: string; cr: string; currency: string } {
  const [drIndex, crIndex] = table.template === "journal" ? [3, 4] : table.template === "trial_balance" ? [1, 2] : [2, 5];
  const sides: Amount[][] = [[], []];
  for (const row of table.rows) {
    for (const [side, index] of [drIndex, crIndex].entries()) {
      const text = row[index ?? 0]?.trim() ?? "";
      if (text !== "") sides[side]?.push(amount(text));
    }
  }
  const all = sides.flat();
  const currency = all.find((a) => a.currency !== "")?.currency ?? "USD";
  if (all.some((a) => a.currency !== "" && a.currency !== currency)) throw new InputError("ledger_currency", 0);
  const scale = Math.max(0, ...all.map((a) => a.scale));
  const sum = (list: Amount[]) => decimal(list.reduce((n, a) => n + a.units * 10n ** BigInt(scale - a.scale), 0n), scale);
  return { dr: sum(sides[0] ?? []), cr: sum(sides[1] ?? []), currency };
}

/** Recognition is offered as a choice; this never converts prose or currency automatically. */
export function journalFromText(text: string): LedgerTable | null {
  const entries = normaliseInput(text).split(/[;\n]+/).map((line) => line.trim()).filter(Boolean);
  if (entries.length < 2 || entries.length > 60) return null;
  const rows: string[][] = [];
  for (const line of entries) {
    const match = /^(Dr|Cr)\s+(.+?)\s+((?:\(?-?\s*(?:[$£€₦¥]|[A-Z]{3}\s*)?\s*)?\d[\d,.]*\)?)$/i.exec(line);
    if (!match) return null;
    const side = (match[1] ?? "").toLowerCase();
    const value = match[3] ?? "";
    try { amount(value); } catch { return null; }
    rows.push(["", match[2] ?? "", "", side === "dr" ? value : "", side === "cr" ? value : ""]);
  }
  try { return ledgerTable("journal", rows); } catch { return null; }
}
