// The student's turn on a manipulable visual (VSL-42, D103): every adjustable value has a keyboard and screen-reader
// equivalent, as simulations do (VSL-32). Arrow keys move it by the kind's step, Page Up and Page Down by ten steps,
// the value can be typed, and it is announced; "Check" sends the values, which the verification tools mark exactly as
// the typed line. The tutor narrates; nothing here is set by the model.

import { useState, type KeyboardEvent } from "react";
import { useT } from "../i18n.js";
import { Button, ErrorState } from "../primitives/index.js";

export type ManipulableKind = "number_line" | "function_graph" | "free_body";

interface Field { key: string; label: string; step: number; unit?: string }

/** The values one part needs: a point on a line, a point (x, y) on a graph, or a force's size and direction. */
export function fieldsOf(kind: ManipulableKind, partId: string): Field[] {
  if (kind === "function_graph") return [{ key: `${partId}:x`, label: "x", step: 0.5 }, { key: `${partId}:y`, label: "y", step: 0.5 }];
  if (kind === "free_body") return [{ key: `${partId}:size`, label: "size", step: 1, unit: "N" }, { key: `${partId}:angle`, label: "direction", step: 15, unit: "°" }];
  return [{ key: `${partId}:x`, label: "x", step: 0.5 }];
}

/** The value sent for a part, in the form the typed line takes. */
export function submitted(kind: ManipulableKind, partId: string, values: Record<string, string>): string {
  const v = (k: string) => values[`${partId}:${k}`] ?? "0";
  if (kind === "function_graph") return `(${v("x")}, ${v("y")})`;
  if (kind === "free_body") return `${v("size")} N at ${v("angle")}`;
  return v("x");
}

function stepped(value: string, delta: number): string {
  const n = Number(value);
  const next = (Number.isFinite(n) ? n : 0) + delta;
  return String(Math.round(next * 1000) / 1000);
}

export function ManipulationPanel({ kind, partIds, prompt, onCheck }: {
  kind: ManipulableKind; partIds: string[]; prompt: string;
  onCheck: (values: { part_id: string; value: string }[]) => unknown | Promise<unknown>;
}) {
  const t = useT();
  const fields = partIds.flatMap((id) => fieldsOf(kind, id).map((f) => ({ ...f, part: id })));
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f.key, "0"])));
  const [announced, setAnnounced] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const set = (field: Field & { part: string }, value: string) => {
    setValues((all) => ({ ...all, [field.key]: value }));
    setAnnounced(t("visual.manipulate.value", { part: field.part, name: field.label, value: `${value}${field.unit ?? ""}` }));
  };
  const onKey = (field: Field & { part: string }) => (e: KeyboardEvent<HTMLInputElement>) => {
    const moves: Record<string, number> = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 10, PageDown: -10 };
    const move = moves[e.key];
    if (move === undefined) return;
    e.preventDefault();
    set(field, stepped(values[field.key] ?? "0", move * field.step));
  };
  return (
    <section data-manipulation="" aria-label={prompt} className="grid gap-2 rounded-card border border-line p-3">
      <p className="text-ui text-ink">{prompt}</p>
      {fields.map((field) => (
        <label key={field.key} className="flex items-center gap-2 text-ui">
          <span>{field.part} {field.label}</span>
          <input
            role="spinbutton" inputMode="decimal" data-field={field.key} value={values[field.key] ?? ""}
            aria-valuenow={Number(values[field.key]) || 0} aria-valuetext={`${values[field.key]}${field.unit ?? ""}`}
            onKeyDown={onKey(field)} onChange={(e) => set(field, e.target.value)}
            className="w-24 rounded-control border border-line px-2 py-1 font-mono"
          />
          {field.unit !== undefined && <span aria-hidden="true">{field.unit}</span>}
        </label>
      ))}
      <span role="status" className="sr-only">{announced}</span>
      {failed && <ErrorState message={t("visual.manipulate.failed")} />}
      <Button disabled={busy} onClick={() => {
        setBusy(true); setFailed(false);
        try {
          const checked = onCheck(partIds.map((id) => ({ part_id: id, value: submitted(kind, id, values) })));
          void Promise.resolve(checked).catch(() => setFailed(true)).finally(() => setBusy(false));
        } catch { setFailed(true); setBusy(false); }
      }}>{t("visual.manipulate.check")}</Button>
    </section>
  );
}
