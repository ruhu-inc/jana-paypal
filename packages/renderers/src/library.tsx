// library_diagram (VSL-03, VSL-22, VSL-26): a vetted drawing from the diagram library, with tappable parts and an
// optional process sequence. The drawing is the entry's sanitised SVG carried by the resolution; it is parsed and
// checked again here (VSL-23) and drawn as elements, never injected. Parts follow the entry's tab order (LOC-15),
// labels obey the label mode (VSL-11: hidden removes them, quiz numbers blanks), a sequence step shows its pre-drawn
// arrows, and a highlighted part is drawn with the ring stroke and an underlined label. The Biology library kinds
// (microscope_view, diagram_comparison) draw their entries with the same routine. A reaction mechanism (VSL-06) is a
// library sequence whose curly arrows are pre-drawn in the entry, played up to the step the spec asks for; nothing is
// placed from a request.
import { createElement, type ReactElement, type ReactNode } from "react";
import { define } from "./define.js";
import { INK, MUTED, PAPER, RING, RING_STROKE, STRONG, WordsFallback, computed, described, highlighted, partName, partNameRef, type Scene } from "./kit.js";
import { SvgRefused, parseSvg, reactProps, svgProblems, type SvgNode } from "./library_svg.js";

interface EntryPart {
  id: string;
  name: string;
  initially_hidden?: boolean;
}

export interface LibraryEntry {
  id?: string;
  title?: string;
  parts?: EntryPart[];
  sequences?: { id: string; steps: { highlight: string[]; arrows?: string[]; caption?: string }[] }[];
  regions?: { id: string; viewbox: number[] }[];
}

interface LibraryComputed {
  svg?: string;
  entry?: LibraryEntry;
}

interface DrawContext {
  hl: boolean;
  arrows: Set<string>;
  hidden: Set<string>;
  /** Part-id prefix for a drawing shown beside another (`left.`, `right.`); its element ids are prefixed too. */
  prefix: string;
  /** Parts drawn as declared differences (diagram_comparison): a dashed ring, distinct from a step's highlight. */
  marked: Set<string>;
}

/** An element id or internal reference, kept apart from the other drawing's on the same page. */
function scoped(name: string, value: string, prefix: string): string {
  if (prefix === "") return value;
  const tag = prefix.replace(/\.$/, "-");
  if (name === "id") return `${tag}${value}`;
  if ((name === "href" || name === "xlink:href") && value.startsWith("#")) return `#${tag}${value.slice(1)}`;
  return value.replace(/url\(#([^)]+)\)/g, (_, id: string) => `url(#${tag}${id})`);
}

function drawNode(n: SvgNode, s: Scene, key: string, ctx: DrawContext): ReactNode {
  const attrs = reactProps(ctx.prefix === "" ? n.attrs : Object.fromEntries(Object.entries(n.attrs).map(([k, v]) => [k, scoped(k, v, ctx.prefix)])));
  const part = n.attrs["data-part"] === undefined ? undefined : `${ctx.prefix}${n.attrs["data-part"]}`;
  const arrow = n.attrs["data-arrow"];
  const labelFor = n.attrs["data-label-for"] === undefined ? undefined : `${ctx.prefix}${n.attrs["data-label-for"]}`;
  if (ctx.prefix !== "") {
    if (part !== undefined) attrs["data-part"] = part;
    if (labelFor !== undefined) attrs["data-label-for"] = labelFor;
  }
  if (arrow !== undefined && !ctx.arrows.has(arrow)) return null;
  if (part !== undefined && ctx.hidden.has(part)) return null;
  if (n.tag === "title" || n.tag === "desc") return null;
  if (labelFor !== undefined) {
    if (s.labels === "hidden") return null;
    if (s.labels === "quiz") {
      const index = s.order.get(labelFor) ?? 0;
      const x = Number(n.attrs.x ?? 0);
      const y = Number(n.attrs.y ?? 0);
      const size = Number(n.attrs["font-size"] ?? 13) * s.scale;
      return (
        <g key={key} data-blank-for={labelFor}>
          <rect x={x} y={y - size} width={size * 2} height={size * 1.3} fill={PAPER} stroke={STRONG} strokeWidth={1} strokeDasharray="3 2" />
          <text x={x + size} y={y} fontSize={size} textAnchor="middle" fill={MUTED}>{index}</text>
        </g>
      );
    }
  }
  const hl = ctx.hl || (part !== undefined && highlighted(s, part));
  const marked = part !== undefined && ctx.marked.has(part);
  if (attrs.fontSize !== undefined) attrs.fontSize = String(Number(attrs.fontSize) * s.scale);
  // A content colour keeps its line-strong outline even when highlighted (DSN-18); the label still shows the highlight.
  if (hl && n.tag !== "text" && n.tag !== "tspan" && attrs.stroke !== undefined && attrs.stroke !== "none" && n.attrs["data-content-colour"] === undefined) {
    attrs.stroke = RING;
    attrs.strokeWidth = String(Math.max(RING_STROKE, Number(attrs.strokeWidth ?? 1.5)));
  }
  if (hl && n.tag === "text" && labelFor !== undefined) {
    attrs.textDecoration = "underline";
    attrs.fontWeight = "600";
  }
  const children = n.children.map((c, i) => (typeof c === "string" ? c : drawNode(c, s, `${key}.${i}`, { ...ctx, hl })));
  if (part !== undefined) {
    const extra = {
      ref: partNameRef(s, part), tabIndex: 0, role: "button", "aria-label": partName(s, part), "data-highlighted": highlighted(s, part) ? "true" : undefined,
      "data-pointed": s.focus.has(part) ? "true" : undefined, onClick: () => s.activate(part),
      onKeyDown: (e: { key: string; preventDefault(): void }) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          s.activate(part);
        }
      },
      ...(marked ? { "data-difference": "true" } : {}),
    };
    const drawn = createElement(n.tag, { key, ...attrs, ...extra }, ...children);
    // A declared difference wears a dashed ring over its own outline, so it never depends on colour alone (DSN-18).
    return marked && !hl ? createElement("g", { key: `${key}.diff`, strokeDasharray: "5 3" }, drawn) : drawn;
  }
  return createElement(n.tag, { key, ...attrs }, ...children);
}

export interface EntryDrawing {
  viewBox: string;
  width: number;
  height: number;
  nodes: ReactNode[];
}

/** A library entry's drawing as elements, at a sequence step and in a region if the spec asks; null when the
 * drawing is refused (VSL-23) and the visual shows its words instead. */
export function entryDrawing(s: Scene, svg: string, entry: LibraryEntry | undefined, o: { sequence?: string; region?: string; prefix?: string; marked?: Set<string> } = {}): EntryDrawing | null {
  let root: SvgNode;
  try {
    root = parseSvg(svg);
  } catch (error) {
    if (error instanceof SvgRefused) return null;
    throw error;
  }
  if (svgProblems(root).length > 0) return null;
  const prefix = o.prefix ?? "";
  const sequence = entry?.sequences?.find((q) => q.id === o.sequence);
  const step = Math.min((sequence?.steps.length ?? 0) - 1, s.step);
  const arrows = new Set(sequence !== undefined && step >= 0 ? (sequence.steps[step]?.arrows ?? []) : []);
  const hidden = new Set(s.state.hidden);
  for (const part of entry?.parts ?? []) if (part.initially_hidden === true && !s.state.revealed.has(`${prefix}${part.id}`)) hidden.add(`${prefix}${part.id}`);
  const region = entry?.regions?.find((r) => r.id === o.region);
  const viewBox = region !== undefined ? region.viewbox.join(" ") : (root.attrs.viewBox ?? "0 0 480 320");
  const [, , width = 480, height = 320] = viewBox.split(/[\s,]+/).map(Number);
  const ctx: DrawContext = { hl: false, arrows, hidden, prefix, marked: o.marked ?? new Set() };
  const nodes = root.children.map((child, i) => (typeof child === "string" ? null : drawNode(child, s, `${prefix}n${i}`, ctx)));
  return { viewBox, width: Number.isFinite(width) && width > 0 ? width : 480, height: Number.isFinite(height) && height > 0 ? height : 320, nodes };
}

function drawLibrary(s: Scene): ReactElement {
  const c = computed<LibraryComputed>(s.spec);
  if (c.svg === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const p = s.spec.params as { sequence?: string; sequence_id?: string; region?: string };
  const sequence = p.sequence ?? p.sequence_id;
  const drawing = entryDrawing(s, c.svg, c.entry, { ...(sequence !== undefined ? { sequence } : {}), ...(p.region !== undefined ? { region: p.region } : {}) });
  if (drawing === null) return <WordsFallback spec={s.spec} reason="refused" />;
  return (
    <svg viewBox={drawing.viewBox} role="group" aria-label={described(s)} className="w-full max-w-xl" data-kind={s.spec.type} data-visual-id={s.spec.visual_id} data-ref={s.spec.ref} stroke={undefined} color={INK}>
      {drawing.nodes}
    </svg>
  );
}

export const libraryDiagram = define({ kind: "library_diagram", schema: "visuals/kinds/library_diagram", labelModes: ["shown", "hidden", "quiz"], library: true, draw: drawLibrary });

export const reactionMechanism = define({
  kind: "reaction_mechanism", schema: "visuals/kinds/reaction_mechanism", labelModes: ["shown", "hidden", "quiz"], library: true, draw: drawLibrary,
  table: (spec) => {
    const p = spec.params as { sequence_id: string; step: number };
    const sequence = computed<LibraryComputed>(spec).entry?.sequences?.find((q) => q.id === p.sequence_id);
    return { columns: ["visual.table.step", "visual.table.caption"], rows: (sequence?.steps ?? []).slice(0, p.step + 1).map((st, i) => [String(i + 1), st.caption ?? ""]) };
  },
});
