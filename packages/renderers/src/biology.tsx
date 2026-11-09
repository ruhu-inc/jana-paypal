// Biology kinds (VSL-05, VSL-46): punnett_square (drawn as HTML), food_web, cycle_diagram, classification_key,
// pedigree_chart and gel_electrophoresis as templates, and microscope_view and diagram_comparison as library kinds.
// Everything drawn comes from the resolution: Punnett cells, classes and ratios from bio_genetics' stand-in checks,
// cycles and keys from the pack's science assets, a pedigree's generations and a gel's band order and logarithmic
// band positions from the engine's layout, and a microscope's scale bar and magnification from the library entry and
// its check. Part ids are the resolution's (organism, stage, node, individual and lane ids), never positions.
import { must } from "./must.js";
import type { ReactElement, ReactNode } from "react";
import { define } from "./define.js";
import { Arrow, Frame, INK, LINE, Label, MUTED, PAPER, Part, STRONG, SURFACE, WordsFallback, areaFill, computed, computedLabel, described, highlighted, lineStyle, params, rowProps, type Scene } from "./kit.js";
import { entryDrawing, type LibraryEntry } from "./library.js";
import type { ResolvedSpec, TableData } from "./contract.js";

const W = 480;
type Pt = [number, number];

/** Words split into lines of at most `max` characters (the last line ends in an ellipsis past `lines`). */
export function wrap(text: string, max: number, lines = 3): string[] {
  const out: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    if (line !== "" && `${line} ${word}`.length > max) {
      out.push(line);
      line = word;
    } else line = line === "" ? word : `${line} ${word}`;
  }
  if (line !== "") out.push(line);
  if (out.length <= lines) return out;
  const kept = out.slice(0, lines);
  kept[lines - 1] = `${must(kept[lines - 1])}…`;
  return kept;
}

/** A label over several lines; one blank in quiz mode, nothing in hidden mode (VSL-11). */
function Lines({ s, part, x, y, lines, size = 12, step = 14 }: { s: Scene; part: string; x: number; y: number; lines: string[]; size?: number; step?: number }): ReactElement {
  const top = y - ((lines.length - 1) * step * s.scale) / 2;
  return <>{lines.map((text, i) => (s.labels === "quiz" && i > 0 ? null : <Label key={i} s={s} part={part} x={x} y={top + i * step * s.scale} text={text} anchor="middle" size={size} />))}</>;
}

/** Plain words in a drawing that are neither a label nor a figure (questions, captions, legends). */
function Words({ x, y, text, s, anchor = "middle", size = 11, muted = false }: { x: number; y: number; text: string; s: Scene; anchor?: "start" | "middle" | "end"; size?: number; muted?: boolean }): ReactElement {
  return <text x={x} y={y} fontSize={size * s.scale} textAnchor={anchor} fill={muted ? MUTED : "var(--color-ink)"} data-presentation="true">{text}</text>;
}

/** The ends of a line from box a to box b, stopped at each box's edge (half sizes hw, hh) with a small gap. */
function between(a: Pt, b: Pt, hw: number, hh: number, offset = 0): [Pt, Pt] {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const edge = Math.min(Math.abs(ux) > 1e-9 ? hw / Math.abs(ux) : Infinity, Math.abs(uy) > 1e-9 ? hh / Math.abs(uy) : Infinity) + 4;
  const ox = -uy * offset;
  const oy = ux * offset;
  return [[a[0] + ux * edge + ox, a[1] + uy * edge + oy], [b[0] - ux * edge + ox, b[1] - uy * edge + oy]];
}

// --- punnett_square -------------------------------------------------------------------------------------------------

interface PunnettParams {
  cross: string;
  parent1: string;
  parent2: string;
  show?: string[];
}

interface PunnettComputed {
  gametes?: { p1: string[]; p2: string[] };
  grid?: string[][];
  genotype_classes?: { genotype: string; probability: string; ratio: number }[];
  phenotype_classes?: { phenotype: string; probability: string; ratio: number }[];
}

/** A genotype with its sex-chromosome or blood-group alleles as superscripts (X^H, I^A): notation, not figures. */
export function Genotype({ text, cross }: { text: string; cross: string }): ReactElement {
  const carrier = cross === "sex_linked" ? "X" : cross === "multiple_alleles" ? "I" : null;
  if (carrier === null) return <span data-formula="true">{text}</span>;
  const pieces = text.match(new RegExp(`${carrier}[A-Za-z]?|.`, "g")) ?? [];
  return (
    <span data-formula="true">
      {pieces.map((piece, i) => (piece.length === 2 && piece.startsWith(carrier) ? <span key={i}>{carrier}<sup>{piece.slice(1)}</sup></span> : <span key={i}>{piece}</span>))}
    </span>
  );
}

const PUNNETT_SHOW = ["gametes", "genotype_ratio", "phenotype_ratio"];

/** The ratio rows under the square, as the engine lists them (punnett_rows in agent_core/assets/visual_kinds.py). */
function punnettRows(s: Scene, p: PunnettParams): string[] {
  const show = new Set(Array.isArray(p.show) ? p.show : PUNNETT_SHOW);
  for (const a of s.state.added) if (a.element === "ratio_row") show.add(a.of ?? "genotype_ratio");
  return ["genotype_ratio", "phenotype_ratio", "probabilities"].filter((k) => show.has(k));
}

function drawPunnett(s: Scene): ReactElement {
  const p = params<PunnettParams>(s.spec);
  const c = computed<PunnettComputed>(s.spec);
  if (c.grid === undefined || c.gametes === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const shown = s.labels !== "hidden";
  const g = (text: string) => <Genotype text={text} cross={p.cross} />;
  const label = (id: string, content: ReactNode) => (shown ? <span data-label-for={id}>{content}</span> : null);
  const gametes = (list: string[]) => list.flatMap((x, i) => [...(i > 0 ? [", "] : []), <span key={i}>{g(x)}</span>]);
  const genotypes = c.genotype_classes ?? [];
  const phenotypes = c.phenotype_classes ?? [];
  const rows: Record<string, ReactNode> = {
    genotype_ratio: genotypes.flatMap((k, i) => [...(i > 0 ? [" : "] : []), <span key={i}>{k.ratio} {g(k.genotype)}</span>]),
    phenotype_ratio: phenotypes.map((k) => `${k.ratio} ${k.phenotype}`).join(" : "),
    probabilities: phenotypes.map((k) => `${k.phenotype} ${k.probability}`).join(", "),
  };
  return (
    <div className="my-2 rounded-card border border-line p-3" role="group" aria-label={described(s)} data-kind="punnett_square" data-visual-id={s.spec.visual_id}>
      <p className="text-body">
        <span {...rowProps(s, "parent1", "button")}>{label("parent1", g(p.parent1))}</span>
        <span aria-hidden="true"> × </span>
        <span {...rowProps(s, "parent2", "button")}>{label("parent2", g(p.parent2))}</span>
      </p>
      {shown && (p.show ?? PUNNETT_SHOW).includes("gametes") && (
        <p className="text-ui text-muted">
          {s.t("visual.punnett.gametes")} {gametes(c.gametes.p1)}
          <span aria-hidden="true"> × </span>
          {gametes(c.gametes.p2)}
        </p>
      )}
      <table className="mt-2 text-body">
        <thead>
          <tr>
            <th scope="col"><span className="sr-only">{s.t("visual.punnett.gametes")}</span></th>
            {c.gametes.p2.map((x, i) => <th key={i} scope="col"><span {...rowProps(s, `gamete2_${i + 1}`, "button")}>{g(x)}</span></th>)}
          </tr>
        </thead>
        <tbody>
          {c.grid.map((row, r) => (
            <tr key={r}>
              <th scope="row"><span {...rowProps(s, `gamete1_${r + 1}`, "button")}>{g(c.gametes?.p1[r] ?? "")}</span></th>
              {row.map((cell, k) => <td key={k} className="border border-line px-3 py-1 text-center"><span {...rowProps(s, `cell_${r + 1}_${k + 1}`, "button")}>{label(`cell_${r + 1}_${k + 1}`, g(cell))}</span></td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {punnettRows(s, p).length > 0 && (
        <table className="mt-2 text-ui">
          <tbody>
            {punnettRows(s, p).map((key) => (
              <tr key={key}>
                <th scope="row" className="pe-3 text-start">{s.t(`visual.punnett.${key}`)}</th>
                <td><span {...rowProps(s, key, "button")}>{label(key, rows[key])}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function punnettTable(spec: ResolvedSpec): TableData {
  const c = computed<PunnettComputed>(spec);
  return {
    columns: ["visual.table.genotype", "visual.table.probability", "visual.table.ratio"],
    rows: (c.genotype_classes ?? []).map((k) => [k.genotype, k.probability, String(k.ratio)]),
  };
}

export const punnettSquare = define({ kind: "punnett_square", schema: "visuals/kinds/punnett_square", draw: drawPunnett, table: punnettTable });

// --- food_web -------------------------------------------------------------------------------------------------------

interface FoodWebComputed {
  organisms?: { id: string; part: string; label: string; level: number }[];
  links?: { from: string; to: string; part: string }[];
  levels?: number[];
}

const BOX_H = 28;

function drawFoodWeb(s: Scene): ReactElement {
  const c = computed<FoodWebComputed>(s.spec);
  if (c.organisms === undefined || c.links === undefined || c.levels === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const levels = [...c.levels].sort((a, b) => b - a);
  const rowY = (level: number) => 56 + levels.indexOf(level) * 78;
  const H = 56 + levels.length * 78;
  const at = new Map<string, { x: number; y: number; w: number }>();
  for (const level of levels) {
    const row = c.organisms.filter((o) => o.level === level);
    const slot = 440 / Math.max(1, row.length);
    row.forEach((o, i) => at.set(o.id, { x: 20 + slot * (i + 0.5), y: rowY(level), w: Math.min(slot - 12, 110) }));
  }
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        {levels.map((level) => <Words key={level} s={s} x={8} y={rowY(level) - 22} text={level <= 4 ? s.t(`visual.food_web.level_${level}`) : s.t("visual.food_web.level_n", { n: level })} anchor="start" size={10} muted />)}
      </g>
      {c.organisms.map((o) => {
        const box = at.get(o.id);
        if (box === undefined) return null;
        return (
          <Part key={o.part} s={s} id={o.part}>
            <rect x={box.x - box.w / 2} y={box.y - BOX_H / 2} width={box.w} height={BOX_H} rx={4} fill={areaFill(s, o.part, o.level === 1 ? SURFACE : PAPER)} {...lineStyle(s, o.part)} />
            <Label s={s} part={o.part} x={box.x} y={box.y + 4} text={o.label} anchor="middle" size={12} />
          </Part>
        );
      })}
      {c.links.map((link) => {
        const a = at.get(link.from);
        const b = at.get(link.to);
        if (a === undefined || b === undefined) return null;
        const style = lineStyle(s, link.part, INK, 1.5);
        return (
          <Part key={link.part} s={s} id={link.part}>
            <Arrow x1={a.x} y1={a.y - BOX_H / 2 - 2} x2={b.x} y2={b.y + BOX_H / 2 + 2} stroke={style.stroke} width={style.strokeWidth} />
          </Part>
        );
      })}
    </Frame>
  );
}

function foodWebTable(spec: ResolvedSpec): TableData {
  const c = computed<FoodWebComputed>(spec);
  const names = new Map((c.organisms ?? []).map((o) => [o.id, o.label]));
  return { columns: ["visual.table.food", "visual.table.consumer"], rows: (c.links ?? []).map((l) => [names.get(l.from) ?? l.from, names.get(l.to) ?? l.to]) };
}

export const foodWeb = define({ kind: "food_web", schema: "visuals/kinds/food_web", draw: drawFoodWeb, table: foodWebTable });

// --- cycle_diagram --------------------------------------------------------------------------------------------------

interface Stage {
  id: string;
  label: string;
}

interface CycleComputed {
  definition?: { stages?: Stage[]; links?: { from: string; to: string }[] };
}

/** The stages in drawing order, turned to begin at the start stage (cycle_stages in visual_kinds.py). */
function cycleStages(stages: Stage[], start: string | undefined): Stage[] {
  const at = Math.max(0, stages.findIndex((x) => x.id === start));
  return [...stages.slice(at), ...stages.slice(0, at)];
}

function drawCycle(s: Scene): ReactElement {
  const p = params<{ start_stage?: string }>(s.spec);
  const c = computed<CycleComputed>(s.spec);
  const stages = cycleStages(c.definition?.stages ?? [], p.start_stage);
  if (stages.length < 2) return <WordsFallback spec={s.spec} reason="text_only" />;
  const H = 360;
  const hw = 62;
  const hh = 22;
  const at = new Map<string, Pt>(stages.map((x, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / stages.length;
    return [x.id, [240 + 172 * Math.cos(a), 180 + 128 * Math.sin(a)]];
  }));
  const links = c.definition?.links ?? [];
  const both = (l: { from: string; to: string }) => links.some((o) => o.from === l.to && o.to === l.from);
  return (
    <Frame s={s} width={W} height={H}>
      {stages.map((x) => {
        const [cx, cy] = must(at.get(x.id));
        const id = `stage.${x.id}`;
        return (
          <Part key={id} s={s} id={id}>
            <rect x={cx - hw} y={cy - hh} width={hw * 2} height={hh * 2} rx={8} fill={areaFill(s, id, PAPER)} {...lineStyle(s, id)} />
            <Lines s={s} part={id} x={cx} y={cy + 4} lines={wrap(x.label, 16, 2)} />
          </Part>
        );
      })}
      {links.map((l) => {
        const a = at.get(l.from);
        const b = at.get(l.to);
        if (a === undefined || b === undefined) return null;
        const id = `link.${l.from}.${l.to}`;
        const [from, to] = between(a, b, hw, hh, both(l) ? 5 : 0);
        const style = lineStyle(s, id, STRONG, 1.5);
        return (
          <Part key={id} s={s} id={id}>
            <Arrow x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]} stroke={style.stroke} width={style.strokeWidth} />
          </Part>
        );
      })}
    </Frame>
  );
}

function cycleTable(spec: ResolvedSpec): TableData {
  const c = computed<CycleComputed>(spec);
  const names = new Map((c.definition?.stages ?? []).map((x) => [x.id, x.label]));
  return { columns: ["visual.table.stage", "visual.table.next"], rows: (c.definition?.links ?? []).map((l) => [names.get(l.from) ?? l.from, names.get(l.to) ?? l.to]) };
}

export const cycleDiagram = define({ kind: "cycle_diagram", schema: "visuals/kinds/cycle_diagram", draw: drawCycle, table: cycleTable });

// --- classification_key ---------------------------------------------------------------------------------------------

interface KeyNode {
  id: string;
  question: string;
  yes: string;
  no: string;
}

interface KeyComputed {
  definition?: { root?: string; nodes?: KeyNode[]; terminals?: { id: string; label: string }[] };
}

interface Placed {
  id: string;
  part: string;
  text: string;
  group: boolean;
  depth: number;
  x: number;
  children: { id: string; answer: "yes" | "no" }[];
}

/** The key from its start as a tree, depth first with the yes branch first (key_order in visual_kinds.py); leaves
 * spread across the width and each question centred over its branches. */
function keyTree(c: KeyComputed, start: string | undefined): { placed: Placed[]; leaves: number; depth: number } {
  const nodes = new Map((c.definition?.nodes ?? []).map((n) => [n.id, n]));
  const groups = new Map((c.definition?.terminals ?? []).map((t) => [t.id, t.label]));
  const placed: Placed[] = [];
  let leaves = 0;
  let deepest = 0;
  const visit = (id: string, depth: number): number => {
    deepest = Math.max(deepest, depth);
    const node = nodes.get(id);
    if (node === undefined || depth > nodes.size) {
      placed.push({ id, part: `group.${id}`, text: groups.get(id) ?? id, group: true, depth, x: leaves, children: [] });
      leaves += 1;
      return leaves - 1;
    }
    const record: Placed = { id, part: `node.${id}`, text: node.question, group: false, depth, x: 0, children: [{ id: node.yes, answer: "yes" }, { id: node.no, answer: "no" }] };
    placed.push(record);
    const xs = [visit(node.yes, depth + 1), visit(node.no, depth + 1)];
    record.x = (must(xs[0]) + must(xs[1])) / 2;
    return record.x;
  };
  const root = start ?? c.definition?.root;
  if (root !== undefined && nodes.has(root)) visit(root, 0);
  return { placed, leaves, depth: deepest };
}

function drawKey(s: Scene): ReactElement {
  const p = params<{ start_node?: string }>(s.spec);
  const tree = keyTree(computed<KeyComputed>(s.spec), p.start_node);
  if (tree.placed.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const width = Math.max(W, tree.leaves * 112);
  const slot = (width - 16) / Math.max(1, tree.leaves);
  const xOf = (x: number) => 8 + slot * (x + 0.5);
  const yOf = (depth: number) => 40 + depth * 78;
  const H = yOf(tree.depth) + 44;
  const byId = new Map(tree.placed.map((n) => [n.id, n]));
  const half = (n: Placed) => (n.group ? 16 : 24);
  return (
    <Frame s={s} width={width} height={H}>
      <g aria-hidden="true">
        {tree.placed.flatMap((n) => n.children.map((child) => {
          const to = byId.get(child.id);
          if (to === undefined) return null;
          const [x1, y1, x2, y2] = [xOf(n.x), yOf(n.depth) + half(n), xOf(to.x), yOf(to.depth) - half(to)];
          return (
            <g key={`${n.id}.${child.answer}`}>
              <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={STRONG} strokeWidth={1.5} />
              <Words s={s} x={(x1 + x2) / 2 + (child.answer === "yes" ? -6 : 6)} y={(y1 + y2) / 2} text={s.t(`visual.key.${child.answer}`)} anchor={child.answer === "yes" ? "end" : "start"} size={10} muted />
            </g>
          );
        }))}
      </g>
      {tree.placed.map((n) => {
        const w = Math.min(slot - 8, 140);
        const cx = xOf(n.x);
        const cy = yOf(n.depth);
        const lines = wrap(n.text, Math.max(8, Math.floor(w / 6.5)), n.group ? 1 : 3);
        return (
          <Part key={n.part} s={s} id={n.part}>
            <rect x={cx - w / 2} y={cy - half(n)} width={w} height={half(n) * 2} rx={n.group ? 14 : 4} fill={areaFill(s, n.part, n.group ? SURFACE : PAPER)} {...lineStyle(s, n.part)} />
            {n.group ? <Lines s={s} part={n.part} x={cx} y={cy + 4} lines={lines} /> : lines.map((line, i) => <Words key={i} s={s} x={cx} y={cy + 4 + (i - (lines.length - 1) / 2) * 13 * s.scale} text={line} />)}
          </Part>
        );
      })}
    </Frame>
  );
}

function keyTable(spec: ResolvedSpec): TableData {
  const c = computed<KeyComputed>(spec);
  const names = new Map<string, string>([...(c.definition?.nodes ?? []).map((n) => [n.id, n.question] as [string, string]), ...(c.definition?.terminals ?? []).map((t) => [t.id, t.label] as [string, string])]);
  return { columns: ["visual.table.question", "visual.table.yes", "visual.table.no"], rows: (c.definition?.nodes ?? []).map((n) => [n.question, names.get(n.yes) ?? n.yes, names.get(n.no) ?? n.no]) };
}

export const classificationKey = define({ kind: "classification_key", schema: "visuals/kinds/classification_key", draw: drawKey, table: keyTable });

// --- pedigree_chart -------------------------------------------------------------------------------------------------

interface Person {
  id: string;
  sex: "female" | "male" | "unspecified";
  phenotype: "affected" | "unaffected" | "unknown";
  carrier: boolean | null;
}

interface PedigreeComputed {
  rows?: { generation: number; roman: string; members: string[] }[];
  people?: Person[];
  couples?: { parent1: string; parent2: string; children: string[] }[];
}

const R = 14;

function Symbol({ s, person, at, id }: { s: Scene; person: Person; at: Pt; id: string }): ReactElement {
  const [x, y] = at;
  const fill = person.phenotype === "affected" ? INK : areaFill(s, id, PAPER);
  const style = lineStyle(s, id, INK, 1.5);
  const shape = person.sex === "male"
    ? <rect x={x - R} y={y - R} width={R * 2} height={R * 2} fill={fill} {...style} />
    : person.sex === "female" ? <circle cx={x} cy={y} r={R} fill={fill} {...style} />
      : <polygon points={`${x},${y - R - 3} ${x + R + 3},${y} ${x},${y + R + 3} ${x - R - 3},${y}`} fill={fill} {...style} />;
  return (
    <>
      {shape}
      {person.carrier === true && person.phenotype !== "affected" && <circle cx={x} cy={y} r={4} fill={INK} />}
      {person.phenotype === "unknown" && <Words s={s} x={x} y={y + 4} text="?" size={13} />}
    </>
  );
}

function drawPedigree(s: Scene): ReactElement {
  const c = computed<PedigreeComputed>(s.spec);
  if (c.rows === undefined || c.people === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const at = new Map<string, Pt>();
  c.rows.forEach((row, r) => {
    const gap = Math.min(84, 380 / Math.max(1, row.members.length - 1));
    row.members.forEach((id, i) => at.set(id, [260 + (i - (row.members.length - 1) / 2) * gap, 52 + r * 100]));
  });
  const legendY = 52 + c.rows.length * 100 - 20;
  const H = legendY + 34;
  const legend: { key: string; person: Person }[] = [
    { key: "affected", person: { id: "", sex: "male", phenotype: "affected", carrier: null } },
    { key: "unaffected", person: { id: "", sex: "male", phenotype: "unaffected", carrier: null } },
    { key: "carrier", person: { id: "", sex: "male", phenotype: "unaffected", carrier: true } },
    { key: "unknown", person: { id: "", sex: "male", phenotype: "unknown", carrier: null } },
  ];
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        {c.rows.map((row, r) => <Words key={row.roman} s={s} x={14} y={56 + r * 100} text={row.roman} anchor="start" size={13} muted />)}
        {(c.couples ?? []).map((couple) => {
          const a = at.get(couple.parent1);
          const b = at.get(couple.parent2);
          if (a === undefined || b === undefined) return null;
          const kids = couple.children.map((k) => at.get(k)).filter((k): k is Pt => k !== undefined);
          const mid: Pt = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
          const [left, right] = a[0] <= b[0] ? [a, b] : [b, a];
          if (kids.length === 0) return <line key={`${couple.parent1}.${couple.parent2}`} x1={left[0] + R} y1={left[1]} x2={right[0] - R} y2={right[1]} stroke={INK} strokeWidth={1.5} />;
          const bar = Math.min(...kids.map((k) => k[1])) - R - 22;
          const xs = [...kids.map((k) => k[0]), mid[0]];
          return (
            <g key={`${couple.parent1}.${couple.parent2}`}>
              <line x1={left[0] + R} y1={left[1]} x2={right[0] - R} y2={right[1]} stroke={INK} strokeWidth={1.5} />
              <line x1={mid[0]} y1={mid[1]} x2={mid[0]} y2={bar} stroke={INK} strokeWidth={1.5} />
              <line x1={Math.min(...xs)} y1={bar} x2={Math.max(...xs)} y2={bar} stroke={INK} strokeWidth={1.5} />
              {kids.map((k, i) => <line key={i} x1={k[0]} y1={bar} x2={k[0]} y2={k[1] - R - 3} stroke={INK} strokeWidth={1.5} />)}
            </g>
          );
        })}
      </g>
      {c.people.map((person) => {
        const where = at.get(person.id);
        if (where === undefined) return null;
        const id = `ind.${person.id}`;
        return (
          <Part key={id} s={s} id={id}>
            <Symbol s={s} person={person} at={where} id={id} />
            <Label s={s} part={id} x={where[0]} y={where[1] + R + 16} text={computedLabel(s.spec, id, person.id)} anchor="middle" size={11} />
          </Part>
        );
      })}
      <g aria-hidden="true">
        {legend.map((item, i) => (
          <g key={item.key}>
            <Symbol s={s} person={item.person} at={[40 + i * 112, legendY + 8]} id="" />
            <Words s={s} x={40 + i * 112 + R + 6} y={legendY + 12} text={s.t(`visual.pedigree.${item.key}`)} anchor="start" size={10} />
          </g>
        ))}
      </g>
    </Frame>
  );
}

function pedigreeTable(spec: ResolvedSpec): TableData {
  const c = computed<PedigreeComputed>(spec);
  const names = new Map(spec.parts.map((p) => [p.id, p.name]));
  return { columns: ["visual.table.individual", "visual.table.description"], rows: (c.people ?? []).map((p) => [computedLabel(spec, `ind.${p.id}`, p.id), names.get(`ind.${p.id}`) ?? ""]) };
}

export const pedigreeChart = define({ kind: "pedigree_chart", schema: "visuals/kinds/pedigree_chart", draw: drawPedigree, table: pedigreeTable });

// --- gel_electrophoresis --------------------------------------------------------------------------------------------

interface Band {
  bp: number;
  part: string;
  position: number;
}

interface GelComputed {
  ladder?: Band[];
  lanes?: { id: string; part: string; label: string; bands: Band[] }[];
}

function drawGel(s: Scene): ReactElement {
  const c = computed<GelComputed>(s.spec);
  if (c.ladder === undefined || c.lanes === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const H = 380;
  const [x0, x1, top, bottom] = [118, 456, 72, H - 64];
  const lanes = [{ id: "ladder", part: "ladder", label: s.t("visual.gel.ladder"), bands: c.ladder }, ...c.lanes];
  const laneW = (x1 - x0) / lanes.length;
  const yOf = (position: number) => top + 18 + position * (bottom - top - 34);
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        <rect x={x0 - 6} y={top - 14} width={x1 - x0 + 12} height={bottom - top + 20} rx={4} fill={SURFACE} stroke={LINE} strokeWidth={1.5} />
        <Words s={s} x={x1 + 9} y={top} text="−" anchor="start" size={16} />
        <Words s={s} x={x1 + 9} y={bottom} text="+" anchor="start" size={16} />
        <Words s={s} x={W / 2} y={H - 22} text={s.t("visual.gel.direction")} size={11} muted />
      </g>
      {lanes.map((lane, i) => {
        const cx = x0 + laneW * (i + 0.5);
        const bandW = Math.min(laneW * 0.62, 56);
        return (
          <g key={lane.part}>
            <Part s={s} id={lane.part}>
              <rect x={cx - bandW / 2} y={top - 8} width={bandW} height={8} fill={PAPER} {...lineStyle(s, lane.part, STRONG, 1.5)} />
              <Label s={s} part={lane.part} x={cx} y={top - 22} text={lane.label} anchor="middle" size={11} />
            </Part>
            {lane.bands.map((band) => {
              const y = yOf(band.position);
              const lit = highlighted(s, band.part);
              return (
                <Part key={band.part} s={s} id={band.part}>
                  <rect x={cx - bandW / 2} y={y - 3} width={bandW} height={6} rx={2} fill={lane.part === "ladder" ? MUTED : INK} {...(lit ? lineStyle(s, band.part) : {})} />
                  {lane.part === "ladder" && <Label s={s} part={band.part} x={x0 - 14} y={y + 4} text={s.t("visual.gel.bp", { size: band.bp })} anchor="end" size={11} />}
                </Part>
              );
            })}
          </g>
        );
      })}
    </Frame>
  );
}

function gelTable(spec: ResolvedSpec): TableData {
  const c = computed<GelComputed>(spec);
  // The ladder's name is the engine's (its part name), so the table carries no literal words.
  const ladder = spec.parts.find((p) => p.id === "ladder")?.name ?? "";
  const rows: [string, Band[]][] = [[ladder, c.ladder ?? []], ...(c.lanes ?? []).map((l): [string, Band[]] => [l.label, l.bands])];
  return { columns: ["visual.table.lane", "visual.table.bands"], rows: rows.map(([label, bands]) => [label, bands.map((b) => String(b.bp)).join(", ")]) };
}

export const gelElectrophoresis = define({ kind: "gel_electrophoresis", schema: "visuals/kinds/gel_electrophoresis", draw: drawGel, table: gelTable });

// --- microscope_view ------------------------------------------------------------------------------------------------

interface LibraryPayload {
  svg?: string;
  entry?: LibraryEntry;
}

interface MicroscopeComputed extends LibraryPayload {
  scale_bar?: { length: string; drawing_px: number };
}

function drawMicroscope(s: Scene): ReactElement {
  const p = params<{ image_length_mm: number; actual_length_um: number; show_scale?: boolean }>(s.spec);
  const c = computed<MicroscopeComputed>(s.spec);
  if (c.svg === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const drawing = entryDrawing(s, c.svg, c.entry);
  if (drawing === null) return <WordsFallback spec={s.spec} reason="refused" />;
  const factor = W / drawing.width;
  const dh = drawing.height * factor;
  const H = dh + 64;
  const bar = c.scale_bar !== undefined && p.show_scale !== false ? c.scale_bar : null;
  const barEnd = 24 + (bar?.drawing_px ?? 0) * factor;
  return (
    <Frame s={s} width={W} height={H}>
      <svg x={0} y={0} width={W} height={dh} viewBox={drawing.viewBox} color={INK}>{drawing.nodes}</svg>
      {bar !== null && (
        <Part s={s} id="scale_bar">
          <path d={`M24,${dh + 10} L24,${dh + 22} M24,${dh + 16} L${barEnd},${dh + 16} M${barEnd},${dh + 10} L${barEnd},${dh + 22}`} fill="none" {...lineStyle(s, "scale_bar", INK, 3)} />
          <Label s={s} part="scale_bar" x={barEnd + 8} y={dh + 20} text={computedLabel(s.spec, "scale_bar", bar.length)} size={12} />
        </Part>
      )}
      <Part s={s} id="magnification">
        <Words s={s} x={W - 120} y={dh + 20} text={s.t("visual.microscope.magnification")} anchor="end" size={12} />
        <Label s={s} part="magnification" x={W - 112} y={dh + 20} text={computedLabel(s.spec, "magnification")} size={13} />
      </Part>
      {s.labels !== "hidden" && <text x={W / 2} y={dh + 50} fontSize={11 * s.scale} textAnchor="middle" fill={MUTED}>{s.t("visual.microscope.lengths", { image: p.image_length_mm, actual: p.actual_length_um })}</text>}
    </Frame>
  );
}

export const microscopeView = define({ kind: "microscope_view", schema: "visuals/kinds/microscope_view", labelModes: ["shown", "hidden", "quiz"], library: true, draw: drawMicroscope });

// --- diagram_comparison ---------------------------------------------------------------------------------------------

interface ComparisonComputed extends LibraryPayload {
  right?: LibraryPayload;
  differences?: { left: string | null; right: string | null }[];
}

function drawComparison(s: Scene): ReactElement {
  const c = computed<ComparisonComputed>(s.spec);
  if (c.svg === undefined || c.right?.svg === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const marked = new Set((c.differences ?? []).flatMap((d) => [d.left, d.right]).filter((x): x is string => typeof x === "string"));
  const left = entryDrawing(s, c.svg, c.entry, { prefix: "left.", marked });
  const right = entryDrawing(s, c.right.svg, c.right.entry, { prefix: "right.", marked });
  if (left === null || right === null) return <WordsFallback spec={s.spec} reason="refused" />;
  const panel = (W - 8) / 2;
  const heights = [left.height * (panel / left.width), right.height * (panel / right.width)];
  const dh = Math.max(...heights);
  const H = dh + 58;
  return (
    <Frame s={s} width={W} height={H}>
      <Words s={s} x={panel / 2} y={16} text={c.entry?.title ?? ""} size={13} />
      <Words s={s} x={panel + 8 + panel / 2} y={16} text={c.right.entry?.title ?? ""} size={13} />
      <svg x={0} y={24} width={panel} height={heights[0]} viewBox={left.viewBox} color={INK}>{left.nodes}</svg>
      <svg x={panel + 8} y={24} width={panel} height={heights[1]} viewBox={right.viewBox} color={INK}>{right.nodes}</svg>
      <g aria-hidden="true">
        <line x1={16} y1={dh + 44} x2={44} y2={dh + 44} stroke={STRONG} strokeWidth={3} strokeDasharray="5 3" />
        <Words s={s} x={52} y={dh + 48} text={s.t("visual.comparison.difference")} anchor="start" size={11} />
      </g>
    </Frame>
  );
}

export const diagramComparison = define({ kind: "diagram_comparison", schema: "visuals/kinds/diagram_comparison", labelModes: ["shown", "hidden", "quiz"], library: true, draw: drawComparison });
