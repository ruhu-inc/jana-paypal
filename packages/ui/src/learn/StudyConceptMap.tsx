// A Study concept map (the third layer of the concept map): a document's concepts and the relations it states, each
// with its citation, or a typed topic's map (the pack's curated skills, or AI-generated with no source). Every box
// says where it came from in words: "From your document", "Curated skill", "AI-generated, no source" or "Added by
// you"; a concept that matches a curated skill says so, and its sheet names the skill. A selected concept's sheet
// shows its cited summary (or plain note), its relations with their citations, the study actions (ask Jana, make
// flashcards, quiz me: the same cited, consented and budgeted jobs as the Generate panel) and the learner's edits
// (rename, remove, merge, add), which are kept. The same map is a table for those who prefer one.
import { createContext, useContext, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { StudyContracts as W } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, FIELD_LOOK, IconButton, SegmentedControl } from "../primitives/index.js";
import { useVisualSettings } from "../visuals/VisualViewer.js";
import { useConceptRenderer, type ConceptDrawEdge, type ConceptDrawNode } from "./ConceptMap.js";

type Concept = W.StudyConcept;
type Origin = Concept["origin"];

export interface StudyConceptActions {
  onAsk?: (name: string) => void;
  onFlashcards?: (name: string) => void;
  onQuiz?: (name: string) => void;
  /** Open the curated skill a concept matches on the subject's map. */
  onOpenSkill?: (kcId: string) => void;
}

const DIRECTED = new Set(["builds_on", "part_of", "causes", "example_of", "includes"]); // `includes`: a pack's outline

/** The map is of a research pack's material (SPEC §18): its sourced concepts came from web pages, so they say so. */
const FromWeb = createContext(false);

function useOriginWord(fromWeb?: boolean): (origin: Origin) => string {
  const t = useT(), inherited = useContext(FromWeb), web = fromWeb ?? inherited;
  return (origin) => t(web && origin === "document" ? "study.concept.origin.web" : `study.concept.origin.${origin}`);
}

function ConceptSheet({ concept, map, claim, actions, busy, onEdit, onClose, onPick }: {
  concept: Concept; map: W.StudyConceptMap; claim: (c: W.Claim) => ReactNode; actions: StudyConceptActions; busy: boolean;
  onEdit?: (ops: W.ConceptEditOp[]) => void; onClose: () => void; onPick: (id: string) => void;
}) {
  const t = useT();
  const origin = useOriginWord();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const [name, setName] = useState(concept.name);
  const [mergeWith, setMergeWith] = useState("");
  useEffect(() => { heading.current?.focus(); setName(concept.name); setMergeWith(""); }, [concept.concept_id, concept.name]);
  const byId = new Map(map.concepts.map((c) => [c.concept_id, c]));
  const links = map.relations.filter((r) => r.from === concept.concept_id || r.to === concept.concept_id);
  const others = map.concepts.filter((c) => c.concept_id !== concept.concept_id);
  const onKey = (e: KeyboardEvent<HTMLElement>) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
  return (
    <section role="dialog" aria-modal="false" aria-labelledby={id} onKeyDown={onKey} data-study-concept={concept.concept_id} data-origin={concept.origin}
      className="jana-reveal flex flex-col gap-4 rounded-card border border-line bg-canvas p-5">
      <div className="flex items-start justify-between gap-2">
        <h3 id={id} ref={heading} tabIndex={-1} className="text-heading text-ink">{concept.name}</h3>
        <span className="-me-2 -mt-1"><IconButton icon="dismiss" size="sm" label={t("sheet.close")} onClick={onClose} /></span>
      </div>
      <p className="text-meta font-semibold text-muted" data-origin-label>{origin(concept.origin)}</p>
      {concept.summary !== null ? claim(concept.summary) : concept.note !== null && <p className="text-ui text-ink" data-note>{concept.note}</p>}
      {concept.anchor !== null && concept.origin !== "pack" && (
        <div className="flex flex-col items-start gap-2 rounded-control bg-panel p-3" data-anchor={concept.anchor.kc_id}>
          <span className="text-ui text-ink">{t("study.concept.matches", { skill: concept.anchor.statement })}</span>
          {actions.onOpenSkill !== undefined && <Button size="sm" tone="ghost" onClick={() => { if (concept.anchor !== null) actions.onOpenSkill?.(concept.anchor.kc_id); }}>{t("study.concept.open_skill")}</Button>}
        </div>
      )}
      {(actions.onAsk || actions.onFlashcards || actions.onQuiz) && (
        <span className="flex flex-wrap gap-2">
          {actions.onAsk && <Button size="sm" tone="primary" disabled={busy} onClick={() => actions.onAsk?.(concept.name)}>{t("concept.ask_jana")}</Button>}
          {actions.onFlashcards && <Button size="sm" disabled={busy} onClick={() => actions.onFlashcards?.(concept.name)}>{t("study.concept.flashcards")}</Button>}
          {actions.onQuiz && <Button size="sm" disabled={busy} onClick={() => actions.onQuiz?.(concept.name)}>{t("study.concept.quiz")}</Button>}
        </span>
      )}
      {links.length > 0 && (
        <div className="flex flex-col gap-2">
          <h4 className="text-label text-faint">{t("study.concept.links")}</h4>
          <ul className="flex flex-col divide-y divide-line">
            {links.map((r, i) => {
              const other = byId.get(r.from === concept.concept_id ? r.to : r.from);
              const sentence = t(`study.concept.relation.${r.type}`, { from: byId.get(r.from)?.name ?? r.from, to: byId.get(r.to)?.name ?? r.to });
              return (
                <li key={i} className="flex flex-col items-start gap-2 py-2" data-relation={`${r.from}>${r.to}`} data-type={r.type}>
                  <button type="button" className="text-start text-ui text-ink underline-offset-2 hover:underline" onClick={() => other && onPick(other.concept_id)}>{sentence}</button>
                  {r.claim !== null ? claim(r.claim) : <span className="text-meta text-faint">{origin(r.origin)}</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {onEdit !== undefined && (
        <div className="flex flex-col gap-3 border-t border-line pt-4" data-concept-edits>
          <h4 className="text-label text-faint">{t("study.concept.edit")}</h4>
          <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); if (name.trim() && name.trim() !== concept.name) onEdit([{ op: "rename", concept_id: concept.concept_id, name: name.trim() }]); }}>
            <label className="flex min-w-0 flex-1 basis-40 flex-col gap-1 text-meta text-muted">{t("study.concept.name")}<input className={FIELD_LOOK} value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></label>
            <Button size="sm" type="submit" disabled={busy || !name.trim() || name.trim() === concept.name}>{t("study.concept.rename")}</Button>
          </form>
          {others.length > 0 && (
            <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); if (mergeWith) onEdit([{ op: "merge", concept_ids: [concept.concept_id, mergeWith], name: name.trim() || concept.name }]); }}>
              <label className="flex min-w-0 flex-1 basis-40 flex-col gap-1 text-meta text-muted">{t("study.concept.merge_with")}
                <select className={FIELD_LOOK} value={mergeWith} onChange={(e) => setMergeWith(e.target.value)}>
                  <option value="">{t("study.concept.choose")}</option>
                  {others.map((c) => <option key={c.concept_id} value={c.concept_id}>{c.name}</option>)}
                </select>
              </label>
              <Button size="sm" type="submit" disabled={busy || !mergeWith}>{t("study.concept.merge")}</Button>
            </form>
          )}
          <span><Button size="sm" tone="ghost" disabled={busy} onClick={() => { onEdit([{ op: "remove", concept_id: concept.concept_id }]); onClose(); }}>{t("study.concept.remove")}</Button></span>
        </div>
      )}
    </section>
  );
}

function ConceptTable({ map, onPick }: { map: W.StudyConceptMap; onPick: (id: string) => void }) {
  const t = useT();
  const origin = useOriginWord();
  const byId = new Map(map.concepts.map((c) => [c.concept_id, c]));
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-start text-ui" data-study-concept-table>
        <caption className="sr-only">{t("study.concept.table_caption")}</caption>
        <thead><tr className="border-b border-line text-label text-faint">
          <th scope="col" className="p-2 text-start">{t("study.concept.table.concept")}</th>
          <th scope="col" className="p-2 text-start">{t("study.concept.table.from")}</th>
          <th scope="col" className="p-2 text-start">{t("study.concept.table.links")}</th>
        </tr></thead>
        <tbody>
          {map.concepts.map((c) => (
            <tr key={c.concept_id} className="border-b border-line align-top" data-row-concept={c.concept_id}>
              <th scope="row" className="p-2 text-start font-normal"><button type="button" className="text-start text-ink underline-offset-2 hover:underline" onClick={() => onPick(c.concept_id)}>{c.name}</button>
                {c.anchor !== null && c.origin !== "pack" && <span className="block text-meta text-faint">{t("study.concept.matches_short")}</span>}</th>
              <td className="p-2">{origin(c.origin)}</td>
              <td className="p-2 text-muted">{map.relations.filter((r) => r.from === c.concept_id).map((r) => t(`study.concept.relation.${r.type}`, { from: c.name, to: byId.get(r.to)?.name ?? r.to })).join("; ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A Study concept map with its sheet, edits and table. `claim` draws a cited claim the workspace's way. */
export function StudyConceptMap({ map, claim, busy = false, actions = {}, onEdit, heading, fromWeb = false }: {
  map: W.StudyConceptMap; claim: (c: W.Claim) => ReactNode; busy?: boolean; actions?: StudyConceptActions;
  onEdit?: (ops: W.ConceptEditOp[]) => void; heading?: ReactNode;
  /** The material is a research pack's web pages (SPEC §18), not the learner's own document. */
  fromWeb?: boolean;
}) {
  const t = useT();
  const origin = useOriginWord(fromWeb);
  const Drawing = useConceptRenderer();
  const { reducedMotion } = useVisualSettings();
  const [mode, setMode] = useState<"map" | "table">("map");
  const [open, setOpen] = useState<string | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [linkNew, setLinkNew] = useState(true);
  const drawing = useMemo(() => {
    const nodes: ConceptDrawNode[] = map.concepts.map((c) => ({
      id: c.concept_id, code: "", title: c.name, state: c.origin, stateWord: origin(c.origin), linked: c.origin === "model",
      asked: 0, mistakes: 0, next: 0, ...(c.anchor !== null && c.origin !== "pack" ? { badge: t("study.concept.matches_short") } : {}),
    }));
    const edges: ConceptDrawEdge[] = map.relations.map((r) => ({ from: r.from, to: r.to, kind: "prerequisite", strength: DIRECTED.has(r.type) ? "required" : "helpful" }));
    return { nodes, edges };
  }, [map, origin, t]);
  const concept = open === null ? null : map.concepts.find((c) => c.concept_id === open) ?? null;
  const unsourced = map.concepts.some((c) => c.origin === "model");
  return (<FromWeb.Provider value={fromWeb}>
    <section className="flex flex-col gap-4" data-study-concept-map data-concepts={map.concepts.length} aria-label={t("study.tool.concept_map")}>
      {heading}
      {unsourced && <p role="note" className="rounded-control border border-dashed border-line-strong p-3 text-ui text-ink" data-no-source>{t("study.concept.no_source_note")}</p>}
      {map.edited && <p className="text-meta text-muted">{t("study.concept.edited")}</p>}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-meta text-muted">{t(map.concepts.some((c) => c.origin === "outline") ? "study.concept.legend_outline" : "study.concept.legend")}</p>
        {Drawing !== null && <SegmentedControl label={t("concept.view")} value={mode} onChange={setMode} options={[{ value: "map", label: t("concept.view.map") }, { value: "table", label: t("concept.view.table") }]} />}
      </div>
      {mode === "map" && Drawing !== null ? (
        <div className="max-h-[60vh] overflow-auto rounded-card border border-line bg-canvas" data-study-concept-scroller>
          <Drawing nodes={drawing.nodes} edges={drawing.edges} label={t("study.concept.map_label", { count: map.concepts.length })} t={t} active={active}
            reducedMotion={reducedMotion} zoom={0.9} onActivate={setOpen} onFocusNode={setActive} />
        </div>
      ) : <ConceptTable map={map} onPick={setOpen} />}
      {concept !== null && <ConceptSheet concept={concept} map={map} claim={claim} actions={actions} busy={busy} {...(onEdit !== undefined ? { onEdit } : {})} onClose={() => setOpen(null)} onPick={setOpen} />}
      {onEdit !== undefined && (
        <form className="flex flex-wrap items-end gap-2" data-concept-add onSubmit={(e) => {
          e.preventDefault();
          const name = adding.trim();
          if (!name) return;
          onEdit([{ op: "add", name, ...(linkNew && concept !== null ? { link_to: concept.concept_id } : {}) }]);
          setAdding("");
        }}>
          <label className="flex min-w-0 flex-1 basis-48 flex-col gap-1 text-meta text-muted">{t("study.concept.add")}<input className={FIELD_LOOK} value={adding} maxLength={80} onChange={(e) => setAdding(e.target.value)} /></label>
          {concept !== null && <label className="flex min-h-11 items-center gap-2 text-meta text-muted"><input type="checkbox" checked={linkNew} onChange={(e) => setLinkNew(e.target.checked)} />{t("study.concept.link_to", { name: concept.name })}</label>}
          <Button size="sm" type="submit" disabled={busy || !adding.trim()}>{t("study.concept.add_button")}</Button>
        </form>
      )}
    </section>
  </FromWeb.Provider>);
}
