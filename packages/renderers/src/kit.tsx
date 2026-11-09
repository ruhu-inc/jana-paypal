// The shared drawing kit of the vetted renderers (VSL-10, VSL-11, VSL-18, VSL-19, DSN-18, LOC-15): the SVG frame, a
// focusable part with its accessible name, labels that obey the label mode, and the token styles every kind uses, so
// a highlighted board term and a highlighted diagram part look the same in every kind.
import { must } from "./must.js";
import type { KeyboardEvent, ReactElement, ReactNode } from "react";
import type { LabelMode, PartInfo, ResolvedSpec, Translate, VisualProps } from "./contract.js";
import { CLIENT_KEYS } from "./generated/keys.js";
import { stepState, type StepState } from "./steps.js";

export const INK = "var(--color-diagram-stroke)";
export const MUTED = "var(--color-muted)";
export const LINE = "var(--color-line)";
export const STRONG = "var(--color-line-strong)";
export const PAPER = "var(--color-paper)";
export const SURFACE = "var(--color-panel)";
export const RING = "var(--color-diagram-highlight-ring)";
export const TINT = "var(--color-diagram-highlight)";
export const SERIES = ["var(--color-series-1)", "var(--color-series-2)", "var(--color-series-3)", "var(--color-series-4)"] as const;
export const DASHES = ["", "6 4", "2 3", "8 3 2 3"] as const;
export const STROKE = 1.5;
export const RING_STROKE = 3;
export const LABEL_SIZE = 13;

/** The translator a renderer uses when its host gives none: the key itself (tests and server rendering only). */
export const keyOnly: Translate = (key, args) => (args === undefined ? key : `${key} ${JSON.stringify(args)}`);

export interface Scene {
  spec: ResolvedSpec;
  state: StepState;
  step: number;
  labels: LabelMode;
  focus: Set<string>;
  /** Parts dimmed by the tutor's spotlight (UXS-57): opacity only, the target keeps its ring. */
  dimmed: (id: string) => boolean;
  t: Translate;
  scale: number;
  order: Map<string, number>;
  count: number;
  names: Map<string, string>;
  activate(id: string): void;
}

/** The scene a kind draws in: the cumulative step state, the label mode, tutor focus and part order. */
export function scene(props: VisualProps): Scene {
  const parts = ((props.spec.parts as PartInfo[] | undefined) ?? []).filter((p) => p.focusable);
  return {
    spec: props.spec,
    state: stepState(props.spec, props.step),
    step: props.step,
    labels: props.labels,
    focus: new Set(props.focus),
    dimmed: (id) => props.spotlight === true && props.focus.length > 0 && !props.focus.includes(id),
    t: props.t ?? keyOnly,
    scale: props.textScale ?? 1,
    order: new Map(parts.map((p, i) => [p.id, i + 1])),
    count: parts.length,
    names: new Map(parts.map((p) => [p.id, p.name])),
    activate: (id) => props.onPartActivate(id),
  };
}

export function highlighted(s: Scene, id: string): boolean {
  return s.state.highlight.has(id);
}

/** Whether a step up to the current one added this derived element (VSL-10). */
export function isAddedStep(s: Scene, element: string, of?: string): boolean {
  return s.state.added.some((a) => a.element === element && (of === undefined || a.of === of));
}

export function shown(s: Scene, id: string, initiallyHidden = false): boolean {
  if (s.state.hidden.has(id) || s.state.unbuilt.has(id)) return false;
  return !initiallyHidden || s.state.revealed.has(id);
}

/** Stroke and fill for a part's lines: the highlight ring is thicker as well as coloured (never colour alone). */
export function lineStyle(s: Scene, id: string, base: string = INK, width: number = STROKE): { stroke: string; strokeWidth: number } {
  return highlighted(s, id) ? { stroke: RING, strokeWidth: Math.max(RING_STROKE, width) } : { stroke: base, strokeWidth: width };
}

export function areaFill(s: Scene, id: string, base: string = PAPER): string {
  return highlighted(s, id) ? TINT : base;
}

/** A part's accessible name (LOC-15): its name, its place in the spec's order and whether it is highlighted; in hidden
 * and quiz modes the name never reveals the label. */
export function partName(s: Scene, id: string): string {
  const index = s.order.get(id) ?? 0;
  const where = { index, count: s.count };
  if (s.labels === "quiz") return s.t("visual.part.blank", where);
  if (s.labels === "hidden") return s.t("visual.part.hidden", where);
  const bits = [s.names.get(id) ?? id, s.t("visual.part.position", where)];
  if (highlighted(s, id)) bits.push(s.t("visual.part.highlighted"));
  if (s.focus.has(id)) bits.push(s.t("visual.part.pointed"));
  return bits.join(", ");
}

function onKey(s: Scene, id: string) {
  return (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      s.activate(id);
    }
  };
}

/** Include visible labels/formulas for speech input (WCAG 2.5.3). Hidden/quiz modes remove answer labels before
 * this runs; their semantic name stays neutral, with only still-visible presentation text included. */
export function partNameRef(s: Scene, id: string) {
  return (node: Element | null) => {
    if (node === null) return;
    const visible = node.textContent?.replace(/\s+/g, " ").trim();
    const name = partName(s, id);
    node.setAttribute("aria-label", visible ? `${name}, ${visible}` : name);
    // SVG has no reliable pseudo-element targets. A shared overlay gives small parts priority over long lines.
    if (typeof SVGGraphicsElement === "undefined" || !(node instanceof SVGGraphicsElement) || typeof node.getBBox !== "function") return;
    const group = node, svg = group.ownerSVGElement;
    if (!svg || !svg.namespaceURI) return;
    const namespace = svg.namespaceURI;
    const minimum = Number.parseFloat(svg.ownerDocument.defaultView?.getComputedStyle(svg).getPropertyValue("--target-pointerMin") ?? "") || 44;
    let hit: SVGRectElement | null = null;
    const activate = () => s.activate(id);
    const expand = () => {
      hit?.remove();
      const box = group.getBBox(), matrix = group.getScreenCTM();
      if (matrix === null) return;
      const sx = Math.hypot(matrix.a, matrix.b), sy = Math.hypot(matrix.c, matrix.d);
      if (sx === 0 || sy === 0) return;
      const width = Math.max(box.width, minimum / sx), height = Math.max(box.height, minimum / sy);
      let overlay = svg.querySelector<SVGGElement>(":scope > [data-part-hit-overlay]");
      if (!overlay) { overlay = svg.ownerDocument.createElementNS(namespace, "g") as SVGGElement; overlay.setAttribute("data-part-hit-overlay", ""); svg.append(overlay); }
      hit = svg.ownerDocument.createElementNS(namespace, "rect") as SVGRectElement;
      // The group's local box is transformed into the root SVG coordinate system.
      const rootMatrix = svg.getScreenCTM(); if (!rootMatrix) return;
      const toRoot = rootMatrix.inverse().multiply(matrix);
      const x = box.x + box.width / 2, y = box.y + box.height / 2;
      const centre = { x: toRoot.a * x + toRoot.c * y + toRoot.e, y: toRoot.b * x + toRoot.d * y + toRoot.f };
      const rootSx = Math.hypot(rootMatrix.a, rootMatrix.b), rootSy = Math.hypot(rootMatrix.c, rootMatrix.d);
      const targetWidth = Math.max(width * sx / rootSx, minimum / rootSx), targetHeight = Math.max(height * sy / rootSy, minimum / rootSy);
      hit.setAttribute("data-part-overlay-for", id); hit.setAttribute("aria-hidden", "true");
      hit.setAttribute("data-area", String(box.width * sx * box.height * sy));
      hit.setAttribute("x", String(centre.x - targetWidth / 2)); hit.setAttribute("y", String(centre.y - targetHeight / 2));
      hit.setAttribute("width", String(targetWidth)); hit.setAttribute("height", String(targetHeight));
      hit.setAttribute("fill", "transparent"); hit.setAttribute("pointer-events", "all");
      hit.addEventListener("click", activate); overlay.append(hit);
      for (const rect of [...overlay.children].sort((a, b) => Number(b.getAttribute("data-area")) - Number(a.getAttribute("data-area")))) overlay.append(rect);
    };
    expand();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(expand); observer?.observe(svg);
    return () => { observer?.disconnect(); hit?.removeEventListener("click", activate); hit?.remove(); };

  };
}

/** A focusable part: data-part for addressing (VSL-19), tab-reachable, named, activated by click, Enter or Space. */
export function Part({ s, id, children, focusable = true }: { s: Scene; id: string; children: ReactNode; focusable?: boolean }): ReactElement {
  const pointed = s.focus.has(id);
  if (s.state.unbuilt.has(id)) return <g data-part={id} data-unbuilt="true" />; // a build layer still to come (VSL-41)
  const dim = s.dimmed(id) ? { opacity: CLIENT_KEYS.board_spotlight_dim_opacity, "data-dimmed": "true" } : {};
  if (!focusable) {
    return <g data-part={id} {...dim}>{children}</g>;
  }
  return (
    <g ref={partNameRef(s, id)} data-part={id} {...dim} tabIndex={0} role="button" aria-label={partName(s, id)} data-highlighted={highlighted(s, id) ? "true" : undefined}
      data-pointed={pointed ? "true" : undefined} onClick={() => s.activate(id)} onKeyDown={onKey(s, id)}>
      {children}
    </g>
  );
}

/** An HTML row or cell part, for the board and table kinds. */
export function rowProps(s: Scene, id: string, role?: string): Record<string, unknown> {
  return {
    ref: partNameRef(s, id), "data-part": id, tabIndex: 0, role, hidden: s.state.unbuilt.has(id) ? true : undefined, "aria-label": partName(s, id), "data-highlighted": highlighted(s, id) ? "true" : undefined,
    "data-pointed": s.focus.has(id) ? "true" : undefined, onClick: () => s.activate(id), onKeyDown: onKey(s, id),
    className: highlighted(s, id) ? "bg-highlight-soft underline" : s.focus.has(id) ? "outline-dashed outline-2 outline-highlight-line" : undefined,
  };
}

/** A label: drawn in shown mode, absent in hidden mode, a numbered blank in quiz mode (VSL-11). */
export function Label({ s, part, x, y, text, anchor = "start", size = LABEL_SIZE, presentation = false, formula = false }: {
  s: Scene; part?: string; x: number; y: number; text: string; anchor?: "start" | "middle" | "end"; size?: number; presentation?: boolean; formula?: boolean;
}): ReactElement | null {
  if (s.labels === "hidden" && !presentation) return null;
  const fontSize = size * s.scale;
  if (s.labels === "quiz" && !presentation && part !== undefined) {
    const index = s.order.get(part) ?? 0;
    const width = fontSize * 2;
    const left = anchor === "middle" ? x - width / 2 : anchor === "end" ? x - width : x;
    return (
      <g data-blank-for={part}>
        <rect x={left} y={y - fontSize} width={width} height={fontSize * 1.3} fill={PAPER} stroke={STRONG} strokeWidth={1} strokeDasharray="3 2" />
        <text x={left + width / 2} y={y} fontSize={fontSize} textAnchor="middle" fill={MUTED}>{index}</text>
      </g>
    );
  }
  const hl = part !== undefined && highlighted(s, part);
  return (
    <text x={x} y={y} fontSize={fontSize} textAnchor={anchor} fill="var(--color-ink)" data-label-for={part} data-presentation={presentation ? "true" : undefined} data-formula={formula ? "true" : undefined}
      textDecoration={hl ? "underline" : undefined} fontWeight={hl ? 600 : undefined}>
      {text}
    </text>
  );
}

/** The visual's own description: the generated alt text, except in hidden and quiz modes, where the alt text would
 * give the labels away (VSL-11) and only the part count is said. */
export function described(s: Scene): string {
  if (s.labels === "shown") return s.spec.alt.summary;
  return s.t(s.labels === "quiz" ? "visual.alt.quiz" : "visual.alt.labels_hidden", { count: s.count });
}

/** The SVG frame: a described group whose parts are its focusable children. */
export function Frame({ s, width, height, children }: { s: Scene; width: number; height: number; children: ReactNode }): ReactElement {
  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="group" aria-label={described(s)} className="w-full max-w-xl" data-kind={s.spec.type} data-visual-id={s.spec.visual_id}>
      {children}
    </svg>
  );
}

/** The words fallback every renderer shares (VSL-16): the alt summary, never empty axes. */
export function WordsFallback({ spec }: { spec: unknown; reason: string }): ReactElement {
  const summary = (spec as { alt?: { summary?: string } } | null)?.alt?.summary ?? "";
  return <p className="text-ui text-muted">{summary}</p>;
}

/** The resolved spec's part list, as `parts()` reports it (VSL-19). */
export function specParts(resolved: ResolvedSpec): PartInfo[] {
  return ((resolved.parts as PartInfo[] | undefined) ?? []).map((p) => ({ id: p.id, name: p.name, role: p.role ?? "part", focusable: p.focusable }));
}

/** A label the engine formatted (computed.labels), or the fallback. */
export function computedLabel(spec: ResolvedSpec, id: string, fallback = ""): string {
  const labels = ((spec.computed as { labels?: Record<string, string> }).labels ?? {}) as Record<string, string>;
  return labels[id] ?? fallback;
}

export function params<T>(spec: ResolvedSpec): T {
  return spec.params as unknown as T;
}

export function computed<T>(spec: ResolvedSpec): T {
  return spec.computed as unknown as T;
}

/** Linear map from a data range onto a pixel range, guarding an empty range. */
export function linear(d0: number, d1: number, p0: number, p1: number): (v: number) => number {
  const span = d1 - d0 || 1;
  return (v) => p0 + ((v - d0) / span) * (p1 - p0);
}

/** "Nice" tick values across a range (presentation numbers, VSL-12). */
export function ticks(min: number, max: number, target = 6): number[] {
  const span = max - min;
  if (!(span > 0) || !Number.isFinite(span)) return [min];
  const raw = span / target;
  const step = [1, 2, 5, 10].map((m) => m * 10 ** Math.floor(Math.log10(raw))).find((x) => x >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9 && out.length < 50; v += step) out.push(Number(v.toPrecision(6)));
  return out;
}

/** Axes with gridlines and tick labels marked as presentation numbers. */
export function Axes({ s, x, y, xRange, yRange, box, xLabel, yLabel, grid = true }: {
  s: Scene; x: (v: number) => number; y: (v: number) => number; xRange: [number, number]; yRange: [number, number];
  box: { left: number; right: number; top: number; bottom: number }; xLabel?: string; yLabel?: string; grid?: boolean;
}): ReactElement {
  const xs = ticks(xRange[0], xRange[1]);
  const ys = ticks(yRange[0], yRange[1]);
  const x0 = x(Math.max(xRange[0], Math.min(xRange[1], 0)));
  const y0 = y(Math.max(yRange[0], Math.min(yRange[1], 0)));
  const fs = 11 * s.scale;
  return (
    <g aria-hidden="true">
      {grid && xs.map((v) => <line key={`gx${v}`} x1={x(v)} x2={x(v)} y1={box.top} y2={box.bottom} stroke={LINE} strokeWidth={1} />)}
      {grid && ys.map((v) => <line key={`gy${v}`} x1={box.left} x2={box.right} y1={y(v)} y2={y(v)} stroke={LINE} strokeWidth={1} />)}
      <line x1={box.left} x2={box.right} y1={y0} y2={y0} stroke={STRONG} strokeWidth={STROKE} />
      <line x1={x0} x2={x0} y1={box.top} y2={box.bottom} stroke={STRONG} strokeWidth={STROKE} />
      {s.labels !== "hidden" && xs.map((v) => <text key={`tx${v}`} x={x(v)} y={box.bottom + 14 * s.scale} textAnchor="middle" fontSize={fs} fill={MUTED} data-presentation="true">{v}</text>)}
      {s.labels !== "hidden" && ys.map((v) => <text key={`ty${v}`} x={box.left - 4} y={y(v) + 4} textAnchor="end" fontSize={fs} fill={MUTED} data-presentation="true">{v}</text>)}
      {s.labels !== "hidden" && xLabel !== undefined && <text x={box.right} y={box.bottom + 30 * s.scale} textAnchor="end" fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{xLabel}</text>}
      {s.labels !== "hidden" && yLabel !== undefined && <text x={box.left} y={box.top - 8} fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{yLabel}</text>}
    </g>
  );
}

export function arrowHead(x1: number, y1: number, x2: number, y2: number, size = 8): string {
  const a = Math.atan2(y2 - y1, x2 - x1);
  const l = [x2 - size * Math.cos(a - 0.4), y2 - size * Math.sin(a - 0.4)];
  const r = [x2 - size * Math.cos(a + 0.4), y2 - size * Math.sin(a + 0.4)];
  return `M${x2.toFixed(2)},${y2.toFixed(2)} L${must(l[0]).toFixed(2)},${must(l[1]).toFixed(2)} L${must(r[0]).toFixed(2)},${must(r[1]).toFixed(2)} Z`;
}

/** An arrow from (x1, y1) to (x2, y2): a line and a solid head. */
export function Arrow({ x1, y1, x2, y2, stroke = INK, width = STROKE, dash }: { x1: number; y1: number; x2: number; y2: number; stroke?: string; width?: number; dash?: string }): ReactElement {
  return (
    <g>
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={stroke} strokeWidth={width} {...(dash !== undefined ? { strokeDasharray: dash } : {})} />
      <path d={arrowHead(x1, y1, x2, y2)} fill={stroke} />
    </g>
  );
}

export function fmt(v: number): string {
  return Number(v.toPrecision(6)).toString();
}

export function sceneOf(spec: ResolvedSpec, t?: Translate): Scene {
  return scene({ spec, step: -1, labels: "shown", focus: [], size: { width: 480, height: 320 }, reducedMotion: true, onPartActivate: () => undefined, ...(t !== undefined ? { t } : {}) });
}
