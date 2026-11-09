// The concept map drawn as hand-written SVG with the shared kit (UXS-22 map view, DSN-23, VSL-19): one box per skill
// in its layer (`layout`), its state as a word as well as a fill (never colour alone), the learner's badges ("Asked 3",
// "1 mistake") in words, prerequisite links solid (required) or dashed (helpful), and root-gap links dotted with
// "not reviewed" in their names when no reviewer checked them. A lit gap path rings its boxes and links and dims the
// rest; the tutor's pointer rings one box. Keyboard: one box is in the tab order (roving tabindex); arrow keys move
// up and down a column and left or right along links (to the nearest box in the next column when there is none);
// Enter or Space opens it. Motion only when reduced motion is off.
//
// The learner's map (SPEC §21, W10) paints its layers on the same boxes, each with its words in the box's label (and
// the UI's legend and table): a look and tokens.json's mastery pattern per Freshness bucket with the bucket's word in
// the box's head; refresh due as a dashed outline; watched misconceptions as rings; Study as dots with a count; a
// note as a pin; and a skill that moved since last time as an outline that pulses once, only without reduced motion.
import { must } from "../must.js";
import { useEffect, useId, useMemo, useRef, type KeyboardEvent, type ReactElement } from "react";
import type { Translate } from "../contract.js";
import { INK, LINE, MUTED, PAPER, RING, RING_STROKE, SERIES, STROKE, STRONG, arrowHead } from "../kit.js";
import { layout as place, type Layout, type PlacedNode } from "./layout.js";

export interface DrawNode {
  id: string;
  code: string;
  title: string;
  /** The state's look: a TABLE LRN-09a state, or `explaining`. */
  state: string;
  /** The state's word, drawn on the box. */
  stateWord: string;
  linked: boolean;
  /** The other subject's name, on a linked box. */
  subject?: string;
  asked: number;
  mistakes: number;
  /** Ranked in "what next" (its place from 1), or 0. */
  next: number;
  /** A badge line of its own (a Study concept's "Matches a syllabus skill"), in place of the learner's badges. */
  badge?: string;
  /** The layer shown on the map (SPEC §21). */
  layer?: DrawLayer;
  /** The skill moved since the map was last opened: the words ("Up since last time"). */
  changed?: string;
}

/** tokens.json's mastery patterns (DSN-23): drawn over a box's fill in its ink at 40%, 1 px every 6 px. */
export type MasteryPattern = "none" | "dots-sparse" | "dots-dense" | "solid" | "hatch-diagonal" | "cross-hatch";

/** What a layer paints on one box (SPEC §21). Every mark has its words in `words` or `word`, never colour alone. */
export interface DrawLayer {
  /** The look the box is filled with on this layer (a Freshness bucket's); the state's when absent. */
  look?: string;
  pattern?: MasteryPattern;
  /** The layer's word for the box, drawn in its head in place of the state's word. */
  word?: string;
  /** A dashed outline: refresh due. */
  dashed?: boolean;
  /** Rings around the box, one per watched misconception. */
  rings?: number;
  /** Study dots: the count is drawn beside them. */
  dots?: number;
  /** A pin: a tutor's note cites this skill. */
  pin?: boolean;
  /** The layer's words for the box, on its badge line and in its accessible name. */
  words?: string[];
}

export interface DrawEdge {
  from: string;
  to: string;
  kind: "prerequisite" | "root_gap";
  strength?: "required" | "helpful" | undefined;
  reviewed?: boolean | undefined;
  /** The misconception's student name, on a root-gap link. */
  name?: string | undefined;
}

/** Fill and ink per state look, from the mastery tokens (the tiles use the same). */
const LOOK: Record<string, [string, string]> = {
  not_started: ["var(--color-mastery-not-started)", "var(--color-mastery-not-started-ink)"],
  learning: ["var(--color-mastery-learning)", "var(--color-mastery-learning-ink)"],
  ready_to_check: ["var(--color-mastery-nearly)", "var(--color-mastery-nearly-ink)"],
  explaining: ["var(--color-mastery-nearly)", "var(--color-mastery-nearly-ink)"],
  mastered: ["var(--color-mastery-secure)", "var(--color-mastery-secure-ink)"],
  needs_review: ["var(--color-mastery-refresh)", "var(--color-mastery-refresh-ink)"],
  moved_on: ["var(--color-mastery-moved-on)", "var(--color-mastery-moved-on-ink)"],
  // A Study concept's origin: its word is drawn on the box too (never colour alone).
  document: ["var(--color-panel)", "var(--color-ink)"],
  pack: ["var(--color-canvas)", "var(--color-ink)"],
  learner: ["var(--color-wash)", "var(--color-ink)"],
  model: [PAPER, "var(--color-ink)"],
};
const OUTLINE = "var(--color-mastery-tile-outline)";
const PATTERNS: readonly MasteryPattern[] = ["dots-sparse", "dots-dense", "hatch-diagonal", "cross-hatch"];
const GAP = 6; // tokens.json mastery.patternStroke.spacing
const DOTS_MAX = 3;

/** One SVG pattern per mastery pattern token (solid and none need none): the stroke takes the box's ink. */
function PatternDefs({ id }: { id: string }): ReactElement {
  return (
    <defs>
      {PATTERNS.map((name) => (
        <pattern key={name} id={`${id}-${name}`} width={name === "dots-dense" ? GAP / 2 : GAP} height={name === "dots-dense" ? GAP / 2 : GAP} patternUnits="userSpaceOnUse">
          {name.startsWith("dots") ? <circle cx={1.5} cy={1.5} r={1} fill="currentColor" /> : null}
          {name === "hatch-diagonal" || name === "cross-hatch" ? <path d={`M0,${GAP} L${GAP},0`} stroke="currentColor" strokeWidth={1} /> : null}
          {name === "cross-hatch" ? <path d={`M0,0 L${GAP},${GAP}`} stroke="currentColor" strokeWidth={1} /> : null}
        </pattern>
      ))}
    </defs>
  );
}
const CHAR = 6.3; // average glyph width at 12 px, for wrapping
const DIM = 0.3;

/** A title in at most `lines` lines of `width` px, the last one ending in an ellipsis when it does not fit. */
export function wrap(text: string, width: number, lines = 2): string[] {
  const max = Math.max(4, Math.floor(width / CHAR));
  const out: string[] = [];
  let line = "";
  const words = text.split(/\s+/).filter(Boolean);
  for (let i = 0; i < words.length; i++) {
    const word = must(words[i]);
    const next = line === "" ? word : `${line} ${word}`;
    if (next.length <= max) {
      line = next;
      continue;
    }
    if (out.length === lines - 1) {
      out.push(`${(line === "" ? word : line).slice(0, max - 1).trimEnd()}…`);
      return out;
    }
    if (line !== "") out.push(line);
    line = word.length > max ? `${word.slice(0, max - 1)}…` : word;
  }
  if (line !== "") out.push(line);
  return out;
}

function route(points: { x: number; y: number }[]): string {
  const [first, ...rest] = points;
  if (first === undefined) return "";
  let d = `M${first.x},${first.y}`;
  let prev = first;
  for (const p of rest) {
    const mid = (prev.x + p.x) / 2;
    d += ` C${mid},${prev.y} ${mid},${p.y} ${p.x},${p.y}`;
    prev = p;
  }
  return d;
}

/** The box an arrow key moves to from `at`, or null. Up and down stay in the column; left and right follow links
 * (the nearest linked box by height), else the nearest box in the next column. */
export function moveFrom(layout: Layout, edges: readonly DrawEdge[], at: string, key: string): string | null {
  const nodes = new Map(layout.nodes.map((n) => [n.id, n]));
  const here = nodes.get(at);
  if (here === undefined) return null;
  const nearest = (pool: PlacedNode[]) => pool.sort((a, b) => Math.abs(a.y - here.y) - Math.abs(b.y - here.y) || (a.id < b.id ? -1 : 1))[0]?.id ?? null;
  if (key === "ArrowUp" || key === "ArrowDown") {
    const column = layout.nodes.filter((n) => n.layer === here.layer).sort((a, b) => a.y - b.y);
    const i = column.findIndex((n) => n.id === at) + (key === "ArrowUp" ? -1 : 1);
    return column[i]?.id ?? null;
  }
  if (key === "ArrowLeft" || key === "ArrowRight") {
    const left = key === "ArrowLeft";
    const linked = edges.filter((e) => (left ? e.to === at : e.from === at)).map((e) => nodes.get(left ? e.from : e.to)).filter((n): n is PlacedNode => n !== undefined);
    if (linked.length > 0) return nearest(linked);
    const step = left ? -1 : 1;
    for (let l = here.layer + step; l >= 0 && l < layout.layers; l += step) {
      const column = layout.nodes.filter((n) => n.layer === l);
      if (column.length > 0) return nearest(column);
    }
  }
  return null;
}

/** The layout of a drawing: linked boxes beside the skill they explain and first in their column, then syllabus
 * order. */
export function layoutOf(nodes: readonly DrawNode[], edges: readonly DrawEdge[]): Layout {
  return place(nodes.map((n) => ({ id: n.id, order: `${n.linked ? "0" : "1"}${n.code}`, late: n.linked })), edges);
}

export function ConceptMapSvg({ nodes, edges, layout: given, label, t, active, gap = [], pointed = null, reducedMotion, zoom = 1, onActivate, onFocusNode }: {
  nodes: readonly DrawNode[];
  edges: readonly DrawEdge[];
  /** Placed already (the same function of nodes and edges); laid out here when absent. */
  layout?: Layout;
  label: string;
  t: Translate;
  /** The box in the tab order (the one last focused or opened). */
  active: string | null;
  /** A lit gap path, from the root gap to the KC of the mistake. */
  gap?: readonly string[];
  pointed?: string | null;
  reducedMotion: boolean;
  /** Drawn size against the layout's (1: one layout unit per CSS pixel). */
  zoom?: number;
  onActivate: (id: string) => void;
  onFocusNode: (id: string) => void;
}): ReactElement {
  const computed = useMemo(() => given ?? layoutOf(nodes, edges), [given, nodes, edges]);
  const layout = computed;
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const lit = useMemo(() => new Set(gap), [gap]);
  const litEdges = useMemo(() => new Set(gap.slice(1).map((to, i) => `${gap[i]}>${to}`)), [gap]);
  const kinds = useMemo(() => new Map(edges.map((e) => [`${e.from}>${e.to}`, e])), [edges]);
  const lighting = gap.length > 0;
  const focusable = active !== null && byId.has(active) ? active : layout.nodes[0]?.id ?? null;
  const svg = useRef<SVGSVGElement>(null);
  const moved = useRef(false);
  useEffect(() => {
    if (!moved.current || focusable === null) return;
    moved.current = false;
    svg.current?.querySelector<SVGGElement>(`[data-kc-node="${CSS.escape(focusable)}"]`)?.focus();
  }, [focusable]);
  const onKey = (id: string) => (e: KeyboardEvent<SVGGElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onActivate(id);
      return;
    }
    const to = moveFrom(layout, edges, id, e.key);
    if (to !== null) {
      e.preventDefault();
      moved.current = true;
      onFocusNode(to);
    }
  };
  const fade = reducedMotion ? undefined : { transition: "opacity 160ms ease-out" };
  const patternId = `cm-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <svg ref={svg} viewBox={`0 0 ${layout.width} ${layout.height}`} width={Math.round(layout.width * zoom)} height={Math.round(layout.height * zoom)} role="group" aria-label={label}
      data-concept-map="" data-gap={lighting ? gap.join(" ") : undefined} style={{ maxWidth: "none" }}>
      {nodes.some((n) => n.layer?.pattern !== undefined) && <PatternDefs id={patternId} />}
      <g aria-hidden="true" data-edges="">
        {layout.edges.map((routed) => {
          const key = `${routed.from}>${routed.to}`;
          const e = kinds.get(key);
          const on = litEdges.has(key);
          const root = e?.kind === "root_gap";
          const stroke = on ? RING : root ? SERIES[2] : e?.strength === "helpful" ? LINE : STRONG;
          const dash = root ? "2 3" : e?.strength === "helpful" ? "6 4" : undefined;
          const last = must(routed.points[routed.points.length - 1]);
          const before = routed.points[routed.points.length - 2] ?? must(routed.points[0]);
          return (
            <g key={key} data-edge={key} data-kind={e?.kind} data-lit={on ? "true" : undefined} opacity={lighting && !on ? DIM : 1} style={fade}>
              <path d={route(routed.points)} fill="none" stroke={stroke} strokeWidth={on ? RING_STROKE : STROKE} {...(dash !== undefined ? { strokeDasharray: dash } : {})} />
              <path d={arrowHead(Math.min(before.x, last.x - 8), last.y, last.x, last.y, 7)} fill={stroke} />
            </g>
          );
        })}
      </g>
      {layout.nodes.map((placed) => {
        const n = byId.get(placed.id);
        if (n === undefined) return null;
        const paint = n.layer ?? {};
        const [fill, ink] = LOOK[paint.look ?? n.state] ?? must(LOOK.not_started);
        const on = lit.has(n.id);
        const ring = on || pointed === n.id;
        const inner = placed.width - 16;
        const title = wrap(n.title, inner);
        const word = paint.word ?? n.stateWord;
        const head = n.linked && n.subject !== undefined ? `${n.subject} · ${word}` : n.code ? `${n.code} · ${word}` : word;
        const layered = [...(paint.words ?? []), ...(n.changed !== undefined ? [n.changed] : [])];
        const badges = layered.length > 0 && n.layer !== undefined ? layered.join(" · ") : n.badge ?? [n.next > 0 ? t("concept.next_badge", { place: n.next }) : "", n.asked > 0 ? t("concept.badge.asked", { count: n.asked }) : "",
          n.mistakes > 0 ? t("concept.badge.mistakes", { count: n.mistakes }) : "", ...layered].filter(Boolean).join(" · ");
        const into = edges.filter((e) => e.to === n.id && e.kind === "root_gap");
        const names = [
          n.title, n.stateWord, ...(paint.word !== undefined && paint.word !== n.stateWord ? [paint.word] : []), ...layered,
          ...(n.linked && n.subject !== undefined ? [t("concept.linked_from", { subject: n.subject })] : []),
          ...(n.next > 0 ? [t("concept.next_place", { place: n.next })] : []),
          ...(n.asked > 0 ? [t("concept.badge.asked", { count: n.asked })] : []),
          ...(n.mistakes > 0 ? [t("concept.badge.mistakes", { count: n.mistakes })] : []),
          ...(n.badge !== undefined ? [n.badge] : []),
          ...into.map((e) => t(e.reviewed === true ? "concept.root_gap_named" : "concept.root_gap_unreviewed_named", { name: e.name ?? "" })),
          ...(on ? [t("concept.on_gap_path")] : []),
          ...(pointed === n.id ? [t("visual.part.pointed")] : []),
        ];
        return (
          <g key={n.id} data-kc-node={n.id} data-part={`kc:${n.id}`} data-state={n.state} data-linked={n.linked ? "true" : undefined} data-lit={on ? "true" : undefined}
            data-pointed={pointed === n.id ? "true" : undefined} role="button" tabIndex={n.id === focusable ? 0 : -1} aria-label={names.join(", ")}
            transform={`translate(${placed.x},${placed.y})`} opacity={lighting && !on ? DIM : 1} style={{ ...fade, cursor: "pointer", outline: "none" }}
            onClick={() => { onFocusNode(n.id); onActivate(n.id); }} onKeyDown={onKey(n.id)} onFocus={() => n.id !== focusable && onFocusNode(n.id)}>
            <title>{n.title}</title>
            {Array.from({ length: Math.min(paint.rings ?? 0, 3) }, (_, i) => (
              <rect key={`ring-${i}`} data-ring={i + 1} x={-5 - 4 * i} y={-5 - 4 * i} width={placed.width + 10 + 8 * i} height={placed.height + 10 + 8 * i} rx={14 + 4 * i}
                fill="none" stroke={SERIES[2]} strokeWidth={2} />
            ))}
            {n.changed !== undefined && (
              <rect data-changed="" x={-3} y={-3} width={placed.width + 6} height={placed.height + 6} rx={12} fill="none" stroke={RING} strokeWidth={RING_STROKE}>
                {/* Pulses once (SPEC §21.1), never with reduced motion; the outline stays. */}
                {!reducedMotion && <animate attributeName="stroke-opacity" values="1;0.15;1" dur="1.4s" repeatCount="1" />}
              </rect>
            )}
            <rect width={placed.width} height={placed.height} rx={10} fill={n.linked ? PAPER : fill} stroke={ring ? RING : paint.dashed ? STRONG : OUTLINE}
              strokeWidth={ring ? RING_STROKE : paint.dashed ? 2 : 1} {...(n.linked ? { strokeDasharray: "5 3" } : paint.dashed ? { strokeDasharray: "6 3", "data-dashed": "" } : {})} />
            {paint.pattern !== undefined && PATTERNS.includes(paint.pattern) && !n.linked && (
              <rect data-pattern={paint.pattern} width={placed.width} height={placed.height} rx={10} fill={`url(#${patternId}-${paint.pattern})`} style={{ color: ink }} opacity={0.4} pointerEvents="none" />
            )}
            {(paint.dots ?? 0) > 0 && (
              <g data-dots={paint.dots} transform={`translate(${placed.width - 10},10)`}>
                {Array.from({ length: Math.min(paint.dots ?? 0, DOTS_MAX) }, (_, i) => <circle key={i} cx={-i * 7} cy={0} r={2.5} fill={ink} />)}
                <text x={-Math.min(paint.dots ?? 0, DOTS_MAX) * 7 - 2} y={4} fontSize={10} fontWeight={600} fill={ink} textAnchor="end">{paint.dots}</text>
              </g>
            )}
            {paint.pin === true && (
              <path data-pin="" d={`M${placed.width - 10},4 a4,4 0 1 1 0.01,0 Z M${placed.width - 10},12 L${placed.width - 10},18`} fill={ink} stroke={ink} strokeWidth={1.5} />
            )}
            <rect className="concept-focus" x={-4} y={-4} width={placed.width + 8} height={placed.height + 8} rx={13} fill="none" stroke={INK} strokeWidth={2} opacity={0} />
            <text x={8} y={16} fontSize={11} fontWeight={600} fill={n.linked ? MUTED : ink}>{head}</text>
            {title.map((line, i) => <text key={i} x={8} y={33 + i * 15} fontSize={12} fill={n.linked ? INK : ink}>{line}</text>)}
            {badges !== "" && <text x={8} y={placed.height - 8} fontSize={11} fontWeight={600} fill={n.linked ? INK : ink} data-badges="" {...(n.next > 0 ? { "data-next": n.next } : {})}>{badges}</text>}
          </g>
        );
      })}
    </svg>
  );
}
