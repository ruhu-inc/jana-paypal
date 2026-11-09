// Physics template kinds (VSL-03, VSL-04, VSL-29): free_body, circuit, ray_diagram, wave_diagram and motion_diagram.
// Layout is computed here, deterministically, from the parameters; circuit layout comes from the connections (series
// and parallel groups), never from coordinates a model supplied. Figures are traced givens or tool results.
import { must } from "./must.js";
import type { ReactElement } from "react";
import { define } from "./define.js";
import {
  Arrow, Frame, INK, LINE, Label, MUTED, PAPER, Part, RING, SERIES, STROKE, TINT, WordsFallback, areaFill, computed, computedLabel, highlighted,
  isAddedStep, lineStyle, linear, params, shown, type Scene,
} from "./kit.js";
import waveSchema from "./schemas/wave_diagram.schema.json" with { type: "json" };
import motionDiagramSchema from "./schemas/motion_diagram.schema.json" with { type: "json" };

const W = 480;
const H = 320;
const DEG = Math.PI / 180;
type Pt = [number, number];

// --- free_body ------------------------------------------------------------------------------------------------------

interface Force {
  id: string;
  name: string;
  magnitude: string;
  direction_deg: number;
  applied_at?: string;
}

interface FreeBodyParams {
  object: string;
  surface?: { incline_deg?: number; kind?: string; friction?: boolean };
  forces: Force[];
  scale?: "to_scale" | "schematic";
  show_resultant?: boolean;
}

export function surfaceKind(p: FreeBodyParams): string {
  const surface = p.surface;
  if (surface === undefined || Object.keys(surface).length === 0) return "none";
  if (surface.kind !== undefined) return surface.kind;
  return (surface.incline_deg ?? 0) > 0 ? "incline" : "horizontal";
}

function magnitudeOf(text: string): number {
  const n = Number(/^-?[0-9.]+/.exec(text)?.[0] ?? NaN);
  return Number.isFinite(n) ? Math.abs(n) : 1;
}

function drawFreeBody(s: Scene): ReactElement {
  const p = params<FreeBodyParams>(s.spec);
  const kind = surfaceKind(p);
  const incline = kind === "incline";
  const theta = incline ? (p.surface?.incline_deg ?? 0) * DEG : 0;
  const c: Pt = [240, 170];
  const largest = Math.max(1, ...p.forces.map((f) => magnitudeOf(f.magnitude)));
  const len = (f: Force) => (p.scale === "schematic" ? 90 : 30 + (90 * magnitudeOf(f.magnitude)) / largest);
  const along = (deg: number): Pt => [Math.cos(deg * DEG), -Math.sin(deg * DEG)];
  const block = 34;
  const rot = `rotate(${(-theta / DEG).toFixed(2)} ${c[0]} ${c[1]})`;
  const ids = new Set(p.forces.map((f) => f.id));
  const comps = s.state.added.filter((a) => a.element === "components_of" && a.of !== undefined && ids.has(a.of));
  const resultant = p.show_resultant === true || s.state.added.some((a) => a.element === "resultant") || (s.spec.parts as { id: string }[]).some((x) => x.id === "resultant");
  const sum = p.forces.reduce<Pt>((acc, f) => [acc[0] + magnitudeOf(f.magnitude) * Math.cos(f.direction_deg * DEG), acc[1] + magnitudeOf(f.magnitude) * Math.sin(f.direction_deg * DEG)], [0, 0]);
  return (
    <Frame s={s} width={W} height={H}>
      <Part s={s} id="object">
        <rect x={c[0] - block / 2} y={c[1] - block / 2} width={block} height={block} transform={rot} fill={areaFill(s, "object", PAPER)} {...lineStyle(s, "object")} />
      </Part>
      {kind !== "none" && (
        <Part s={s} id="surface">
          <line x1={c[0] - 200 * Math.cos(theta)} y1={c[1] + block / 2 / Math.cos(theta) + 200 * Math.sin(theta)} x2={c[0] + 200 * Math.cos(theta)} y2={c[1] + block / 2 / Math.cos(theta) - 200 * Math.sin(theta)} {...lineStyle(s, "surface", MUTED, 2)} />
        </Part>
      )}
      {p.forces.filter((f) => shown(s, f.id)).map((f) => {
        const [dx, dy] = along(f.direction_deg);
        const end: Pt = [c[0] + dx * len(f), c[1] + dy * len(f)];
        const style = lineStyle(s, f.id, INK, 2);
        return (
          <Part key={f.id} s={s} id={f.id}>
            <Arrow x1={c[0]} y1={c[1]} x2={end[0]} y2={end[1]} stroke={style.stroke} width={style.strokeWidth} />
            <Label s={s} part={f.id} x={end[0] + dx * 10} y={end[1] + dy * 10 + 4} text={`${f.name} ${f.magnitude}`} anchor={dx >= 0 ? "start" : "end"} size={12} />
          </Part>
        );
      })}
      {comps.flatMap((a) => {
        const f = must(p.forces.find((x) => x.id === a.of));
        const names = incline ? ["parallel", "perpendicular"] : ["x", "y"];
        const axes = [theta, theta + Math.PI / 2];
        return names.map((n, i) => {
          const id = `${f.id}.${n}`;
          const axis = must(axes[i]);
          const proj = magnitudeOf(f.magnitude) * Math.cos(f.direction_deg * DEG - axis);
          const k = (len(f) / magnitudeOf(f.magnitude)) * proj;
          const end: Pt = [c[0] + k * Math.cos(axis), c[1] - k * Math.sin(axis)];
          const style = lineStyle(s, id, SERIES[i + 1], 1.5);
          return (
            <Part key={id} s={s} id={id}>
              <Arrow x1={c[0]} y1={c[1]} x2={end[0]} y2={end[1]} stroke={style.stroke} width={style.strokeWidth} dash="5 3" />
              <Label s={s} part={id} x={end[0] + (end[0] >= c[0] ? 6 : -6)} y={end[1] + (end[1] >= c[1] ? 16 : -6)} text={computedLabel(s.spec, id)} anchor={end[0] >= c[0] ? "start" : "end"} size={11} />
            </Part>
          );
        });
      })}
      {resultant && (
        <Part s={s} id="resultant">
          <Arrow x1={c[0]} y1={c[1]} x2={c[0] + (sum[0] / largest) * 90} y2={c[1] - (sum[1] / largest) * 90} stroke={highlighted(s, "resultant") ? RING : SERIES[3]} width={2.5} dash="2 3" />
        </Part>
      )}
    </Frame>
  );
}

export const freeBody = define({ kind: "free_body", schema: "visuals/kinds/free_body", draw: drawFreeBody });

// --- circuit --------------------------------------------------------------------------------------------------------

interface Component {
  id: string;
  type: string;
  value?: string;
  state?: "open" | "closed";
  label?: string;
}

interface CircuitParams {
  components: Component[];
  connections: { from: string; to: string }[];
  show?: string[];
}

type Tree = { kind: "leaf"; id: string } | { kind: "series" | "parallel"; items: Tree[] };
const SOURCES = new Set(["cell", "battery", "ac_supply"]);

/** Series-parallel decomposition of the circuit between the source's terminals: parallel edges merge, a node met by
 * exactly two edges joins them in series; whatever does not reduce is drawn as one series chain. */
export function circuitTree(p: CircuitParams): { source: string | null; tree: Tree } {
  const parent = new Map<string, string>();
  const find = (x: string): string => {
    let r = x;
    while (parent.get(r) !== undefined && parent.get(r) !== r) r = must(parent.get(r));
    return r;
  };
  const union = (a: string, b: string) => parent.set(find(a), find(b));
  const ends = (c: Component): [string, string] => (SOURCES.has(c.type) ? [`${c.id}.pos`, `${c.id}.neg`] : [`${c.id}.a`, `${c.id}.b`]);
  for (const c of p.components) for (const t of ends(c)) parent.set(t, t);
  for (const { from, to } of p.connections) {
    if (!parent.has(from)) parent.set(from, from);
    if (!parent.has(to)) parent.set(to, to);
    union(from, to);
  }
  const source = p.components.find((c) => SOURCES.has(c.type)) ?? null;
  type Edge = { a: string; b: string; tree: Tree };
  let edges: Edge[] = p.components.filter((c) => c !== source).map((c) => {
    const [x, y] = ends(c);
    return { a: find(x), b: find(y), tree: { kind: "leaf", id: c.id } };
  });
  const flat = { kind: "series" as const, items: edges.map((e) => e.tree) };
  if (source === null) return { source: null, tree: flat };
  const [sp, sn] = ends(source).map(find) as [string, string];
  for (let guard = 0; guard < 200 && edges.length > 1; guard += 1) {
    let changed = false;
    // parallel: two edges joining the same pair of nodes
    outer: for (let i = 0; i < edges.length; i += 1) {
      for (let j = i + 1; j < edges.length; j += 1) {
        const e = must(edges[i]);
        const f = must(edges[j]);
        if ((e.a === f.a && e.b === f.b) || (e.a === f.b && e.b === f.a)) {
          const items = [...(e.tree.kind === "parallel" ? e.tree.items : [e.tree]), ...(f.tree.kind === "parallel" ? f.tree.items : [f.tree])];
          edges = [...edges.filter((_, k) => k !== i && k !== j), { a: e.a, b: e.b, tree: { kind: "parallel", items } }];
          changed = true;
          break outer;
        }
      }
    }
    if (changed) continue;
    // series: an inner node with exactly two edges
    const degree = new Map<string, number[]>();
    edges.forEach((e, i) => {
      for (const n of [e.a, e.b]) degree.set(n, [...(degree.get(n) ?? []), i]);
    });
    for (const [node, list] of degree) {
      if (node === sp || node === sn || list.length !== 2) continue;
      const [i, j] = list as [number, number];
      const e = must(edges[i]);
      const f = must(edges[j]);
      const ea = e.a === node ? e.b : e.a;
      const fb = f.a === node ? f.b : f.a;
      const first = e.a === node ? f : e;
      const second = first === e ? f : e;
      const startA = first === e ? ea : fb;
      const endB = first === e ? fb : ea;
      const items = [...(first.tree.kind === "series" ? first.tree.items : [first.tree]), ...(second.tree.kind === "series" ? second.tree.items : [second.tree])];
      edges = [...edges.filter((_, k) => k !== i && k !== j), { a: startA, b: endB, tree: { kind: "series", items } }];
      changed = true;
      break;
    }
    if (!changed) break;
  }
  if (edges.length === 1) return { source: source.id, tree: must(edges[0]).tree };
  return { source: source.id, tree: flat };
}

const CELL = 64;

function size(t: Tree): [number, number] {
  if (t.kind === "leaf") return [CELL, 50];
  const sizes = t.items.map(size);
  if (t.kind === "series") return [sizes.reduce((a, b) => a + b[0], 0), Math.max(...sizes.map((x) => x[1]))];
  return [Math.max(...sizes.map((x) => x[0])) + 30, sizes.reduce((a, b) => a + b[1], 0)];
}

/** Place each leaf: its centre, and the wires that join the tree's pieces. */
function place(t: Tree, x: number, y: number, at: Map<string, Pt>, wires: string[]): void {
  const [w] = size(t);
  if (t.kind === "leaf") {
    at.set(t.id, [x + CELL / 2, y]);
    return;
  }
  if (t.kind === "series") {
    let cx = x;
    for (const item of t.items) {
      place(item, cx, y, at, wires);
      cx += size(item)[0];
    }
    return;
  }
  const total = size(t)[1];
  let cy = y - total / 2;
  const rows: number[] = [];
  for (const item of t.items) {
    const [iw, ih] = size(item);
    const ry = cy + ih / 2;
    rows.push(ry);
    wires.push(`M${x},${ry} L${x + 15},${ry} M${x + 15 + iw},${ry} L${x + w},${ry}`);
    if (iw < w - 30) wires.push(`M${x + 15 + iw},${ry} L${x + w - 15},${ry}`);
    place(item, x + 15, ry, at, wires);
    cy += ih;
  }
  wires.push(`M${x},${rows[0]} L${x},${rows[rows.length - 1]} M${x + w},${rows[0]} L${x + w},${rows[rows.length - 1]}`);
  void (y as number);
}

function Symbol({ c, s }: { c: Component; s: Scene }): ReactElement {
  const st = lineStyle(s, c.id);
  const box = { fill: areaFill(s, c.id, PAPER), ...st };
  switch (c.type) {
    case "cell":
      return <g><line x1={-20} y1={0} x2={-4} y2={0} {...st} /><line x1={4} y1={0} x2={20} y2={0} {...st} /><line x1={-4} y1={-14} x2={-4} y2={14} {...st} /><line x1={4} y1={-7} x2={4} y2={7} {...st} strokeWidth={3} /></g>;
    case "battery":
      return <g><line x1={-24} y1={0} x2={-12} y2={0} {...st} /><line x1={12} y1={0} x2={24} y2={0} {...st} /><line x1={-12} y1={-14} x2={-12} y2={14} {...st} /><line x1={-6} y1={-7} x2={-6} y2={7} {...st} strokeWidth={3} /><line x1={6} y1={-14} x2={6} y2={14} {...st} /><line x1={12} y1={-7} x2={12} y2={7} {...st} strokeWidth={3} /><line x1={-6} y1={0} x2={6} y2={0} {...st} strokeDasharray="2 2" /></g>;
    case "lamp":
      return <g><line x1={-24} y1={0} x2={-12} y2={0} {...st} /><line x1={12} y1={0} x2={24} y2={0} {...st} /><circle r={12} {...box} /><path d="M-8.5,-8.5 L8.5,8.5 M-8.5,8.5 L8.5,-8.5" {...st} /></g>;
    case "switch":
      return <g><line x1={-24} y1={0} x2={-12} y2={0} {...st} /><line x1={12} y1={0} x2={24} y2={0} {...st} /><circle cx={-12} r={2.5} fill={PAPER} {...st} /><circle cx={12} r={2.5} fill={PAPER} {...st} /><line x1={-12} y1={0} x2={c.state === "open" ? 8 : 12} y2={c.state === "open" ? -12 : 0} {...st} /></g>;
    case "ammeter":
    case "voltmeter":
    case "motor":
      return <g><line x1={-24} y1={0} x2={-12} y2={0} {...st} /><line x1={12} y1={0} x2={24} y2={0} {...st} /><circle r={12} {...box} /><text y={5} textAnchor="middle" fontSize={13 * s.scale} fill="var(--color-ink)">{c.type === "ammeter" ? "A" : c.type === "voltmeter" ? "V" : "M"}</text></g>;
    case "diode":
    case "led":
      return <g><line x1={-24} y1={0} x2={-8} y2={0} {...st} /><line x1={8} y1={0} x2={24} y2={0} {...st} /><path d="M-8,-9 L8,0 L-8,9 Z" {...box} /><line x1={8} y1={-9} x2={8} y2={9} {...st} />{c.type === "led" && <path d="M0,-12 l6,-8 M5,-12 l6,-8" {...st} strokeWidth={1} />}</g>;
    case "capacitor":
      return <g><line x1={-24} y1={0} x2={-4} y2={0} {...st} /><line x1={4} y1={0} x2={24} y2={0} {...st} /><line x1={-4} y1={-12} x2={-4} y2={12} {...st} /><line x1={4} y1={-12} x2={4} y2={12} {...st} /></g>;
    case "earth":
      return <g><line x1={-24} y1={0} x2={0} y2={0} {...st} /><line x1={0} y1={0} x2={0} y2={10} {...st} /><path d="M-10,10 L10,10 M-6,14 L6,14 M-2,18 L2,18" {...st} /></g>;
    case "ac_supply":
      return <g><line x1={-24} y1={0} x2={-12} y2={0} {...st} /><line x1={12} y1={0} x2={24} y2={0} {...st} /><circle r={12} {...box} /><path d="M-7,0 q3.5,-7 7,0 t7,0" fill="none" {...st} /></g>;
    case "bell":
      return <g><line x1={-24} y1={0} x2={-10} y2={0} {...st} /><line x1={10} y1={0} x2={24} y2={0} {...st} /><path d="M-10,0 A10,10 0 0 1 10,0 Z" {...box} /></g>;
    default:
      // resistor, variable resistor, potentiometer, thermistor, LDR and fuse share the box with their own marks
      return (
        <g>
          <line x1={-24} y1={0} x2={-14} y2={0} {...st} /><line x1={14} y1={0} x2={24} y2={0} {...st} />
          <rect x={-14} y={-7} width={28} height={14} {...box} />
          {(c.type === "variable_resistor" || c.type === "potentiometer") && <path d="M-14,12 L14,-12" {...st} strokeWidth={1} />}
          {c.type === "thermistor" && <path d="M-14,12 L-6,12 L10,-12" fill="none" {...st} strokeWidth={1} />}
          {c.type === "fuse" && <line x1={-14} y1={0} x2={14} y2={0} {...st} strokeWidth={1} />}
          {c.type === "ldr" && <><circle r={16} fill="none" {...st} strokeWidth={1} /><path d="M-16,-22 l6,6 M-8,-24 l6,6" {...st} strokeWidth={1} /></>}
        </g>
      );
  }
}

function drawCircuit(s: Scene): ReactElement {
  const p = params<CircuitParams>(s.spec);
  const { source, tree } = circuitTree(p);
  const [tw, th] = size(tree);
  const scale = Math.min(1, (W - 80) / tw, (H - 120) / th);
  const at = new Map<string, Pt>();
  const wires: string[] = [];
  const left = 40;
  const topY = 40 + (th * scale) / 2;
  place(tree, 0, 0, at, wires);
  const bottom = H - 40;
  const tx = (q: Pt): Pt => [left + q[0] * scale, topY + q[1] * scale];
  const srcAt: Pt = [left + (tw * scale) / 2, bottom];
  const showReadings = (p.show ?? ["meter_readings"]).some((k) => k === "meter_readings" || k === "currents" || k === "voltages");
  return (
    <Frame s={s} width={W} height={H}>
      <g transform={`translate(${left} ${topY}) scale(${scale})`} aria-hidden="true">
        {wires.map((d, i) => <path key={i} d={d} fill="none" stroke={INK} strokeWidth={STROKE / scale} />)}
      </g>
      <path d={`M${left},${topY} L${left},${bottom} L${srcAt[0] - 24},${bottom} M${srcAt[0] + 24},${bottom} L${left + tw * scale},${bottom} L${left + tw * scale},${topY}`} fill="none" stroke={INK} strokeWidth={STROKE} aria-hidden="true" />
      {source === null && <line x1={srcAt[0] - 24} x2={srcAt[0] + 24} y1={bottom} y2={bottom} stroke={INK} strokeWidth={STROKE} />}
      {p.components.map((c) => {
        const pos = c.id === source ? srcAt : tx(at.get(c.id) ?? [tw / 2, 0]);
        const reading = computedLabel(s.spec, `${c.id}.reading`);
        const text = [c.label ?? c.id, c.value].filter((v) => v !== undefined && v !== "").join(" ");
        return (
          <g key={c.id}>
            <Part s={s} id={c.id}>
              <g transform={`translate(${pos[0].toFixed(2)} ${pos[1].toFixed(2)})`}><Symbol c={c} s={s} /></g>
              <Label s={s} part={c.id} x={pos[0]} y={pos[1] - 22} text={text} anchor="middle" size={11} />
            </Part>
            {reading !== "" && showReadings && (
              <Part s={s} id={`${c.id}.reading`}>
                <rect x={pos[0] - 26} y={pos[1] + 18} width={52} height={18} rx={3} fill={areaFill(s, `${c.id}.reading`, PAPER)} stroke={LINE} strokeWidth={1} />
                <Label s={s} part={`${c.id}.reading`} x={pos[0]} y={pos[1] + 31} text={reading} anchor="middle" size={11} />
              </Part>
            )}
          </g>
        );
      })}
      {computedLabel(s.spec, "equivalent_resistance") !== "" && s.labels !== "hidden" && (
        <text x={W - 20} y={24} textAnchor="end" fontSize={12 * s.scale} fill="var(--color-ink)">{`${s.t("visual.circuit.total_resistance")} ${computedLabel(s.spec, "equivalent_resistance")}`}</text>
      )}
    </Frame>
  );
}

export const circuit = define({ kind: "circuit", schema: "visuals/kinds/circuit", draw: drawCircuit });

// --- ray_diagram ----------------------------------------------------------------------------------------------------

interface RayComputed {
  rays?: string[];
  paths?: Record<string, Pt[]>;
  object_top?: Pt;
  image_top?: Pt | null;
  focal_points?: Pt[];
  nature?: { type?: string };
}

function drawRay(s: Scene): ReactElement {
  const p = params<{ optic: string; object_distance: string; focal_length?: string; show?: string[] }>(s.spec);
  const c = computed<RayComputed>(s.spec);
  if (c.object_top === undefined || c.paths === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const pts: Pt[] = [c.object_top, ...(c.image_top ? [c.image_top] : []), ...(c.focal_points ?? []), ...Object.values(c.paths).flat()];
  const xs = pts.map((q) => q[0]);
  const ymax = Math.max(1, ...pts.map((q) => Math.abs(q[1])));
  const x0 = Math.min(...xs, 0) * 1.15;
  const x1 = Math.max(...xs, 0) * 1.15 + 1;
  const x = linear(x0, x1, 30, W - 30);
  const y = linear(-ymax * 1.3, ymax * 1.3, H - 30, 30);
  const lens = p.optic.endsWith("lens");
  const virtual = c.nature?.type === "virtual";
  const show = new Set(p.show ?? ["image", "focal_points"]);
  const opticX = x(0);
  const optic = lens ? (
    <g>
      <line x1={opticX} x2={opticX} y1={y(ymax * 1.2)} y2={y(-ymax * 1.2)} {...lineStyle(s, "optic", INK, 2)} />
      <path d={p.optic === "converging_lens" ? `M${opticX - 6},${y(ymax * 1.2) + 8} L${opticX},${y(ymax * 1.2)} L${opticX + 6},${y(ymax * 1.2) + 8} M${opticX - 6},${y(-ymax * 1.2) - 8} L${opticX},${y(-ymax * 1.2)} L${opticX + 6},${y(-ymax * 1.2) - 8}` : `M${opticX - 6},${y(ymax * 1.2) - 8} L${opticX},${y(ymax * 1.2)} L${opticX + 6},${y(ymax * 1.2) - 8} M${opticX - 6},${y(-ymax * 1.2) + 8} L${opticX},${y(-ymax * 1.2)} L${opticX + 6},${y(-ymax * 1.2) + 8}`} fill="none" {...lineStyle(s, "optic", INK, 2)} />
    </g>
  ) : (
    <path d={`M${opticX},${y(ymax * 1.2)} Q${opticX + (p.optic === "concave_mirror" ? 14 : p.optic === "convex_mirror" ? -14 : 0)},${y(0)} ${opticX},${y(-ymax * 1.2)}`} fill="none" {...lineStyle(s, "optic", INK, 3)} />
  );
  return (
    <Frame s={s} width={W} height={H}>
      <Part s={s} id="optic">{optic}<Label s={s} part="optic" x={opticX + 8} y={y(ymax * 1.2) + 4} text={s.t(`visual.optic.${p.optic}`)} size={11} /></Part>
      <Part s={s} id="principal_axis" focusable={false}><line x1={20} x2={W - 20} y1={y(0)} y2={y(0)} stroke={MUTED} strokeWidth={1} /></Part>
      <Part s={s} id="object">
        <Arrow x1={x(c.object_top[0])} y1={y(0)} x2={x(c.object_top[0])} y2={y(c.object_top[1])} stroke={highlighted(s, "object") ? RING : SERIES[0]} width={2.5} />
        <Label s={s} part="object" x={x(c.object_top[0])} y={y(0) + 18} text={show.has("measurements") ? `u = ${p.object_distance}` : s.t("visual.ray.object")} anchor="middle" size={11} />
      </Part>
      {c.image_top && show.has("image") && (
        <Part s={s} id="image">
          <Arrow x1={x(c.image_top[0])} y1={y(0)} x2={x(c.image_top[0])} y2={y(c.image_top[1])} stroke={highlighted(s, "image") ? RING : SERIES[1]} width={2.5} {...(virtual ? { dash: "5 3" } : {})} />
          <Label s={s} part="image" x={x(c.image_top[0])} y={y(0) + (c.image_top[1] < 0 ? -8 : 18)} text={show.has("measurements") ? `v = ${computedLabel(s.spec, "image_distance")}` : s.t("visual.ray.image")} anchor="middle" size={11} />
        </Part>
      )}
      {(c.focal_points ?? []).slice(0, 2).map((f, i) => (
        <Part key={`F${i + 1}`} s={s} id={`F${i + 1}`}>
          <circle cx={x(f[0])} cy={y(0)} r={3} fill={INK} />
          <Label s={s} part={`F${i + 1}`} x={x(f[0])} y={y(0) + 16} text="F" anchor="middle" size={11} />
        </Part>
      ))}
      {(c.rays ?? []).map((r, i) => {
        const path = must(c.paths)[r] ?? [];
        const id = `ray.${r}`;
        const style = lineStyle(s, id, SERIES[(i + 2) % SERIES.length], 1.5);
        return (
          <Part key={id} s={s} id={id}>
            {path.slice(1).map((q, n) => {
              const a = must(path[n]);
              const back = n > 0 && q[0] < a[0] && a[0] >= 0 - 1e-9;
              return <line key={n} x1={x(a[0])} y1={y(a[1])} x2={x(q[0])} y2={y(q[1])} {...style} {...(back ? { strokeDasharray: "4 3" } : {})} />;
            })}
          </Part>
        );
      })}
    </Frame>
  );
}

export const rayDiagram = define({ kind: "ray_diagram", schema: "visuals/kinds/ray_diagram", draw: drawRay });

// --- wave_diagram ---------------------------------------------------------------------------------------------------

function drawWave(s: Scene): ReactElement {
  const p = params<{ wave: "transverse" | "longitudinal"; wavelength: string; amplitude?: string; cycles?: number; markers?: string[] }>(s.spec);
  const cycles = p.cycles ?? 2;
  const markers = p.markers ?? ["wavelength"];
  const x0 = 40;
  const lambda = (W - 80) / cycles;
  const mid = 160;
  const amp = 70;
  const wave: ReactElement = p.wave === "transverse" ? (
    <path d={Array.from({ length: cycles * 40 + 1 }, (_, i) => `${i === 0 ? "M" : "L"}${(x0 + (i / 40) * lambda).toFixed(2)},${(mid - amp * Math.sin((i / 40) * Math.PI * 2)).toFixed(2)}`).join(" ")} fill="none" {...lineStyle(s, "wave", SERIES[0], 2)} />
  ) : (
    <g>{Array.from({ length: cycles * 16 }, (_, i) => {
      const u = i / 16;
      const xx = x0 + (u + 0.12 * Math.sin(u * Math.PI * 2) / Math.PI) * lambda;
      return <line key={i} x1={xx} x2={xx} y1={mid - 50} y2={mid + 50} {...lineStyle(s, "wave", INK, 1.5)} />;
    })}</g>
  );
  const crest = x0 + lambda / 4;
  const items: Record<string, ReactElement> = {
    wavelength: (
      <g>
        <Arrow x1={crest + lambda / 2} y1={mid - amp - 16} x2={crest + lambda} y2={mid - amp - 16} stroke={lineStyle(s, "wavelength").stroke} />
        <Arrow x1={crest + lambda / 2} y1={mid - amp - 16} x2={crest} y2={mid - amp - 16} stroke={lineStyle(s, "wavelength").stroke} />
        <Label s={s} part="wavelength" x={crest + lambda / 2} y={mid - amp - 24} text={`λ = ${p.wavelength}`} anchor="middle" size={12} />
      </g>
    ),
    amplitude: (
      <g>
        <Arrow x1={crest} y1={mid} x2={crest} y2={mid - amp} stroke={lineStyle(s, "amplitude").stroke} />
        <Label s={s} part="amplitude" x={crest + 6} y={mid - amp / 2} text={p.amplitude !== undefined ? `A = ${p.amplitude}` : "A"} size={12} />
      </g>
    ),
    crest: <g><circle cx={crest} cy={mid - amp} r={4} fill={highlighted(s, "crest") ? RING : INK} /><Label s={s} part="crest" x={crest} y={mid - amp - 8} text={s.t("visual.wave.crest")} anchor="middle" size={11} /></g>,
    trough: <g><circle cx={crest + lambda / 2} cy={mid + amp} r={4} fill={highlighted(s, "trough") ? RING : INK} /><Label s={s} part="trough" x={crest + lambda / 2} y={mid + amp + 18} text={s.t("visual.wave.trough")} anchor="middle" size={11} /></g>,
    compression: <g><rect x={x0 - 4} y={mid - 56} width={lambda * 0.24} height={112} fill="none" {...lineStyle(s, "compression", SERIES[1], 1.5)} strokeDasharray="4 3" /><Label s={s} part="compression" x={x0} y={mid + 74} text={s.t("visual.wave.compression")} size={11} /></g>,
    rarefaction: <g><rect x={x0 + lambda * 0.38} y={mid - 56} width={lambda * 0.24} height={112} fill="none" {...lineStyle(s, "rarefaction", SERIES[2], 1.5)} strokeDasharray="4 3" /><Label s={s} part="rarefaction" x={x0 + lambda * 0.4} y={mid + 92} text={s.t("visual.wave.rarefaction")} size={11} /></g>,
    direction: <g><Arrow x1={W - 150} y1={H - 24} x2={W - 60} y2={H - 24} stroke={lineStyle(s, "direction").stroke} /><Label s={s} part="direction" x={W - 155} y={H - 20} text={s.t("visual.wave.direction")} anchor="end" size={11} /></g>,
  };
  return (
    <Frame s={s} width={W} height={H}>
      {p.wave === "transverse" && <line x1={x0} x2={W - 40} y1={mid} y2={mid} stroke={LINE} strokeWidth={1} aria-hidden="true" />}
      <Part s={s} id="wave">{wave}</Part>
      {markers.map((m) => <Part key={m} s={s} id={m}>{items[m] ?? <g />}</Part>)}
    </Frame>
  );
}

export const waveDiagram = define({ kind: "wave_diagram", schema: waveSchema, draw: drawWave });

// --- motion_diagram -------------------------------------------------------------------------------------------------

function drawMotionDiagram(s: Scene): ReactElement {
  const p = params<{ axis: "horizontal" | "vertical"; marks: number; show?: string[]; initial_velocity: string; acceleration: string }>(s.spec);
  const c = computed<{ positions?: number[]; velocities?: number[] }>(s.spec);
  if (c.positions === undefined || c.velocities === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const show = new Set([...(p.show ?? []), ...(isAddedStep(s, "velocity_arrows") ? ["velocity"] : [])]);
  const lo = Math.min(0, ...c.positions);
  const hi = Math.max(1e-9, ...c.positions);
  const vmax = Math.max(1e-9, ...c.velocities.map(Math.abs));
  const horizontal = p.axis === "horizontal";
  const along = linear(lo, hi, horizontal ? 50 : 40, horizontal ? W - 60 : H - 40);
  const place = (v: number, k: number): Pt => (horizontal ? [along(v), 150 + (k % 2) * 0] : [180, along(v)]);
  return (
    <Frame s={s} width={W} height={H}>
      <line x1={horizontal ? 30 : 180} x2={horizontal ? W - 30 : 180} y1={horizontal ? 180 : 20} y2={horizontal ? 180 : H - 20} stroke={LINE} strokeWidth={1} aria-hidden="true" />
      {c.positions.map((v, i) => {
        const at = place(v, i);
        return (
          <Part key={`d${i}`} s={s} id={`dot_${i}`}>
            <circle cx={at[0]} cy={at[1]} r={highlighted(s, `dot_${i}`) ? 9 : 7} fill={areaFill(s, `dot_${i}`, SERIES[0])} stroke={INK} strokeWidth={1} />
            <Label s={s} part={`dot_${i}`} x={horizontal ? at[0] : at[0] - 14} y={horizontal ? at[1] + 26 : at[1] + 4} text={`t${i}`} anchor={horizontal ? "middle" : "end"} size={11} />
          </Part>
        );
      })}
      {show.has("velocity") && c.velocities.map((v, i) => {
        const at = place(must(must(c.positions)[i]), i);
        const len = (40 * v) / vmax;
        const style = lineStyle(s, `v_${i}`, SERIES[1], 2);
        return (
          <Part key={`v${i}`} s={s} id={`v_${i}`}>
            {Math.abs(len) > 1 ? <Arrow x1={at[0] + (horizontal ? 0 : 20)} y1={at[1] - (horizontal ? 20 : 0)} x2={at[0] + (horizontal ? len : 20)} y2={at[1] - (horizontal ? 20 : -len)} stroke={style.stroke} width={style.strokeWidth} /> : <circle cx={at[0]} cy={at[1] - 20} r={2} fill={style.stroke} />}
          </Part>
        );
      })}
      {show.has("acceleration") && (
        <Part s={s} id="acceleration">
          <Arrow x1={horizontal ? 60 : 300} y1={horizontal ? 60 : 60} x2={horizontal ? 140 : 300} y2={horizontal ? 60 : 140} stroke={lineStyle(s, "acceleration", SERIES[3], 2.5).stroke} width={2.5} />
          <Label s={s} part="acceleration" x={horizontal ? 60 : 310} y={horizontal ? 50 : 100} text={`a = ${p.acceleration}`} size={12} />
        </Part>
      )}
    </Frame>
  );
}

export const motionDiagram = define({ kind: "motion_diagram", schema: motionDiagramSchema, draw: drawMotionDiagram });

export { TINT as HIGHLIGHT_TINT };
