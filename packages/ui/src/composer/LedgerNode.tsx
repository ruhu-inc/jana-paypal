import { Node, mergeAttributes } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from "@tiptap/react";
import { useEffect, useRef, useState } from "react";
import { ledgerTable, ledgerTotals, LEDGER_COLUMNS, type LedgerTemplate } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, ErrorState } from "../primitives/index.js";

function LedgerView({ node, updateAttributes, editor, deleteNode, getPos }: NodeViewProps) {
  const t = useT();
  const template = node.attrs.template as LedgerTemplate;
  const columns = LEDGER_COLUMNS[template] ?? LEDGER_COLUMNS.journal;
  const rows = node.attrs.rows as string[][];
  const root = useRef<HTMLDivElement>(null);
  const [nextFocus, setNextFocus] = useState<number | null>(null);
  let totals: ReturnType<typeof ledgerTotals> | null = null;
  let error = false;
  try { totals = ledgerTotals(ledgerTable(template, rows)); } catch { error = true; }
  useEffect(() => {
    if (nextFocus !== null) { root.current?.querySelector<HTMLInputElement>(`[data-cell="${nextFocus}"]`)?.focus(); setNextFocus(null); }
  }, [rows, nextFocus]);
  const currentRows = (): string[][] => {
    const pos = getPos();
    return typeof pos === "number" ? (editor.state.doc.nodeAt(pos)?.attrs.rows as string[][] | undefined) ?? rows : rows;
  };
  const addRow = () => {
    const rows = currentRows();
    if (rows.length >= 60) return;
    updateAttributes({ rows: [...rows, columns.map(() => "")] });
    setNextFocus(rows.length * columns.length);
  };
  return <NodeViewWrapper as="div" ref={root} contentEditable={false} className="my-2 overflow-x-auto rounded-card border border-line p-2" data-ledger="">
    <table aria-label={t(`ledger.template.${template}`)} className="w-full text-ui">
      <caption className="text-start font-semibold">{t(`ledger.template.${template}`)}</caption>
      <thead><tr>{columns.map((column) => <th key={column} scope="col" className="p-1 text-start text-label">{t(`ledger.column.${column.replace(/ /g, "_")}`)}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column, col) => {
        const money = ["Dr", "Cr", "Dr Amount", "Cr Amount"].includes(column);
        return <td key={column} className="p-1"><input maxLength={money ? 30 : 80} readOnly={!editor.isEditable}
          data-cell={index * columns.length + col} aria-label={t("ledger.cell", { row: index + 1, column: t(`ledger.column.${column.replace(/ /g, "_")}`) })}
          value={row[col] ?? ""} className={`w-full rounded-control border border-line bg-canvas p-1 ${money ? "text-end tabular-nums" : "text-start"}`}
          onChange={(e) => updateAttributes({ rows: currentRows().map((current, n) => n === index ? current.map((value, c) => c === col ? e.target.value : value) : current) })}
          onKeyDown={(e) => {
            e.stopPropagation();
            if (e.key === "Tab" && !e.shiftKey && index === rows.length - 1 && col === columns.length - 1 && rows.length < 60) { e.preventDefault(); addRow(); }
          }} /></td>;
      })}</tr>)}</tbody>
    </table>
    {totals !== null && <p className="text-meta tabular-nums">{t("ledger.totals", { dr: totals.dr, cr: totals.cr, currency: totals.currency })}</p>}
    {error && <ErrorState message={t("input.ledger_amount")} />}
    <div className="mt-1 flex flex-wrap gap-2"><Button disabled={!editor.isEditable} onClick={() => { const current = currentRows(); if (current.length > 1) updateAttributes({ rows: current.slice(0, -1) }); else deleteNode(); }}>{t("ledger.remove_row")}</Button>
    <Button disabled={!editor.isEditable} onClick={deleteNode}>{t("ledger.remove_table")}</Button>
    <Button disabled={!editor.isEditable || rows.length >= 60} onClick={addRow}>{t("ledger.add_row")}</Button></div>
  </NodeViewWrapper>;
}

export const LedgerNode = Node.create({
  name: "ledger_table", group: "block", atom: true,
  addAttributes() { return { template: { default: "journal" }, rows: { default: [["", "", "", "", ""]] } }; },
  parseHTML() { return [{ tag: "div[data-ledger]" }]; },
  renderHTML({ HTMLAttributes }) { return ["div", mergeAttributes(HTMLAttributes, { "data-ledger": "" })]; },
  addNodeView() { return ReactNodeViewRenderer(LedgerView, { stopEvent: ({ event }) => event.target instanceof HTMLInputElement || event.target instanceof HTMLButtonElement }); },
});
