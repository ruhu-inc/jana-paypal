// Graph kinds (VSL-03): function_graph, motion_graph, statistics_chart, titration_curve and break_even_chart. Curves
// and lines come from the resolution's tool results (computed); axis ticks and grid are presentation numbers; every
// labelled figure is a tool result or a traced given. Series differ by dash pattern and marker shape as well as colour
// (DSN-04), and every graph offers its plotted values as a table (LOC-16).
import { must } from "./must.js";
import type { ReactElement } from "react";
import type { ResolvedSpec, TableData } from "./contract.js";
import { define } from "./define.js";
import { ExprError, sample } from "./expr.js";
import {
  Axes, DASHES, Frame, INK, Label, Part, SERIES, STROKE, TINT, WordsFallback, areaFill, computed, computedLabel, fmt, highlighted, lineStyle, linear,
  params, shown, type Scene,
} from "./kit.js";
import statisticsSchema from "./schemas/statistics_chart.schema.json" with { type: "json" };
import breakEvenSchema from "./schemas/break_even_chart.schema.json" with { type: "json" };
import motionGraphSchema from "./schemas/motion_graph.schema.json" with { type: "json" };

const W = 480;
const H = 320;
const BOX = { left: 48, right: W - 20, top: 28, bottom: H - 40 };

type Pt = [number, number];

function extent(values: number[], fallback: [number, number]): [number, number] {
  const finite = values.filter(Number.isFinite);
  if (finite.length === 0) return fallback;
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  return [lo, hi];
}

function polyline(points: Pt[], x: (v: number) => number, y: (v: number) => number): string {
  return points.map(([a, b]) => `${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ");
}

// --- function_graph -------------------------------------------------------------------------------------------------

interface Curve {
  id: string;
  expr: string;
  label?: string;
  style?: "solid" | "dashed" | "dotted";
  domain?: [number, number];
}

interface GraphParams {
  curves: Curve[];
  x_range: [number, number];
  y_range?: [number, number];
  points?: { id: string; x: number; y: number; label?: string }[];
  shade?: { id: string; between: [string, string]; from?: number; to?: number }[];
  axis_labels?: { x?: string; y?: string };
  grid?: boolean;
}

interface Feature {
  id: string;
  x: number;
  y: number;
  label: string;
}

function variableOf(expr: string): string {
  const names = (expr.match(/[a-z]+/gi) ?? []).filter((n) => !["sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "exp", "ln", "log", "pi", "e", "y"].includes(n));
  return names[0] ?? "x";
}

/** The curve's points: the engine's samples (VSL-13), else the client's own safe sampling as the stated fallback. */
function curveSegments(spec: ResolvedSpec): { curve: Curve; segments: Pt[][] }[] {
  const p = params<GraphParams>(spec);
  const done = computed<{ curves?: Record<string, Pt[][]> }>(spec).curves ?? {};
  return p.curves.map((curve) => {
    let segments = done[curve.id];
    if (segments === undefined) {
      const [from, to] = curve.domain ?? p.x_range;
      try {
        segments = sample(curve.expr.replace(/^y\s*=\s*/, ""), from, to, 200, variableOf(curve.expr));
      } catch (error) {
        if (!(error instanceof ExprError)) throw error;
        segments = [];
      }
    }
    return { curve, segments };
  });
}

function drawFunctionGraph(s: Scene): ReactElement {
  const p = params<GraphParams>(s.spec);
  const curves = curveSegments(s.spec);
  if (curves.every((c) => c.segments.length === 0)) return <WordsFallback spec={s.spec} reason="text_only" />;
  const [x0, x1] = p.x_range;
  const [y0, y1] = p.y_range ?? extent(curves.flatMap((c) => c.segments.flat().map((q) => q[1])), [0, 1]);
  const x = linear(x0, x1, BOX.left, BOX.right);
  const y = linear(y0, y1, BOX.bottom, BOX.top);
  const features = computed<{ features?: Record<string, Feature[]> }>(s.spec).features ?? {};
  const axis: { xLabel?: string; yLabel?: string } = {};
  if (p.axis_labels?.x !== undefined) axis.xLabel = p.axis_labels.x;
  if (p.axis_labels?.y !== undefined) axis.yLabel = p.axis_labels.y;
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[x0, x1]} yRange={[y0, y1]} box={BOX} grid={p.grid !== false} {...axis} />
      {(p.shade ?? []).filter((r) => shown(s, r.id)).map((region) => {
        const first = curves.find((c) => c.curve.id === region.between[0]);
        const pts = (first?.segments.flat() ?? []).filter(([a]) => a >= (region.from ?? x0) && a <= (region.to ?? x1));
        if (pts.length < 2) return null;
        const d = `M${x(must(pts[0])[0]).toFixed(2)},${y(0).toFixed(2)} ` + pts.map(([a, b]) => `L${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ") + ` L${x(must(pts[pts.length - 1])[0]).toFixed(2)},${y(0).toFixed(2)} Z`;
        return <Part key={region.id} s={s} id={region.id}><path d={d} fill={areaFill(s, region.id, TINT)} fillOpacity={0.5} stroke="none" /></Part>;
      })}
      {curves.filter(({ curve }) => shown(s, curve.id)).map(({ curve, segments }, i) => {
        const style = lineStyle(s, curve.id, SERIES[i % SERIES.length], 2);
        const dash = DASHES[curve.style === "dashed" ? 1 : curve.style === "dotted" ? 2 : i % DASHES.length];
        const top = segments.flat().reduce<Pt | null>((best, q) => (best === null || q[1] > best[1] ? q : best), null);
        return (
          <Part key={curve.id} s={s} id={curve.id}>
            {segments.map((pts, n) => <polyline key={n} points={polyline(pts, x, y)} fill="none" {...style} {...(dash ? { strokeDasharray: dash } : {})} />)}
            {curve.label !== undefined && top !== null && <Label s={s} part={curve.id} x={x(top[0]) + 6} y={y(top[1]) - 8} text={curve.label} formula />}
          </Part>
        );
      })}
      {curves.flatMap(({ curve }) => (features[curve.id] ?? []).map((f) => {
        const id = `${curve.id}.${f.id}`;
        return (
          <Part key={id} s={s} id={id}>
            <circle cx={x(f.x)} cy={y(f.y)} r={highlighted(s, id) ? 6 : 4} fill={INK} />
            <Label s={s} part={id} x={x(f.x) + 7} y={y(f.y) - 7} text={computedLabel(s.spec, id, f.label)} size={11} />
          </Part>
        );
      }))}
      {(p.points ?? []).map((pt, i) => (
        <Part key={pt.id} s={s} id={pt.id}>
          {i % 2 === 0 ? <circle cx={x(pt.x)} cy={y(pt.y)} r={4} fill={INK} /> : <rect x={x(pt.x) - 4} y={y(pt.y) - 4} width={8} height={8} fill={INK} />}
          {pt.label !== undefined && <Label s={s} part={pt.id} x={x(pt.x) + 6} y={y(pt.y) - 6} text={pt.label} size={12} />}
        </Part>
      ))}
    </Frame>
  );
}

function functionTable(spec: ResolvedSpec): TableData {
  const rows: string[][] = [];
  const features = computed<{ features?: Record<string, Feature[]> }>(spec).features ?? {};
  for (const { curve, segments } of curveSegments(spec)) {
    for (const f of features[curve.id] ?? []) rows.push([curve.label ?? curve.expr, computedLabel(spec, `${curve.id}.${f.id}`, f.label)]);
    const flat = segments.flat();
    const every = Math.max(1, Math.ceil(flat.length / 10));
    flat.filter((_, n) => n % every === 0).forEach(([a, b]) => rows.push([curve.label ?? curve.expr, `(${fmt(a)}, ${fmt(b)})`]));
  }
  return { columns: ["visual.table.curve", "visual.table.point"], rows };
}

export const functionGraph = define({ kind: "function_graph", schema: "visuals/kinds/function_graph", draw: drawFunctionGraph, table: functionTable });

// --- motion_graph ---------------------------------------------------------------------------------------------------

interface MotionGraphParams {
  graph: "distance_time" | "displacement_time" | "velocity_time" | "speed_time";
  points?: { id: string; t: string; value: string }[];
  curve?: { expr: string; domain: [number, number] };
  shade?: { id: string; from: string; to: string }[];
  gradient?: { id: string; from: string; to: string }[];
  units?: { t?: string; value?: string };
}

const QUANTITY_SYMBOL: Record<string, string> = { distance_time: "s", displacement_time: "s", velocity_time: "v", speed_time: "v" };

function drawMotionGraph(s: Scene): ReactElement {
  const p = params<MotionGraphParams>(s.spec);
  const pts = (p.points ?? []).map((q) => ({ id: q.id, t: Number(q.t), v: Number(q.value), label: `(${q.t}, ${q.value})` }));
  const curve = computed<{ curve?: Pt[][] }>(s.spec).curve ?? [];
  const tAll = [...pts.map((q) => q.t), ...curve.flat().map((q) => q[0]), 0];
  const vAll = [...pts.map((q) => q.v), ...curve.flat().map((q) => q[1]), 0];
  const [t0, t1] = extent(tAll, [0, 1]);
  const [v0, v1] = extent(vAll, [0, 1]);
  const x = linear(t0, t1 * 1.05, BOX.left, BOX.right);
  const y = linear(v0, v1 * 1.1, BOX.bottom, BOX.top);
  const at = new Map(pts.map((q) => [q.id, q]));
  const units = p.units ?? {};
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[t0, t1 * 1.05]} yRange={[v0, v1 * 1.1]} box={BOX} xLabel={`t${units.t ? ` (${units.t})` : ""}`}
        yLabel={`${QUANTITY_SYMBOL[p.graph] ?? ""}${units.value ? ` (${units.value})` : ""}`} />
      {(p.shade ?? []).filter((r) => shown(s, r.id)).map((r) => {
        const a = at.get(r.from);
        const b = at.get(r.to);
        if (a === undefined || b === undefined) return null;
        const inside = pts.filter((q) => q.t >= a.t && q.t <= b.t);
        const d = `M${x(a.t)},${y(0)} ` + inside.map((q) => `L${x(q.t)},${y(q.v)}`).join(" ") + ` L${x(b.t)},${y(0)} Z`;
        return <Part key={r.id} s={s} id={r.id}><path d={d} fill={areaFill(s, r.id, TINT)} fillOpacity={0.6} stroke="none" /></Part>;
      })}
      {(p.gradient ?? []).filter((g) => shown(s, g.id)).map((g) => {
        const a = at.get(g.from);
        const b = at.get(g.to);
        if (a === undefined || b === undefined) return null;
        return (
          <Part key={g.id} s={s} id={g.id}>
            <path d={`M${x(a.t)},${y(a.v)} L${x(b.t)},${y(a.v)} L${x(b.t)},${y(b.v)}`} fill="none" {...lineStyle(s, g.id, INK, 1)} strokeDasharray="4 3" />
          </Part>
        );
      })}
      {pts.slice(1).map((b, i) => {
        const a = must(pts[i]);
        const id = `seg_${a.id}_${b.id}`;
        return shown(s, id) ? <Part key={id} s={s} id={id}><line x1={x(a.t)} y1={y(a.v)} x2={x(b.t)} y2={y(b.v)} {...lineStyle(s, id, SERIES[0], 2)} /></Part> : null;
      })}
      {curve.length > 0 && shown(s, "curve") && (
        <Part s={s} id="curve">{curve.map((seg, n) => <polyline key={n} points={polyline(seg, x, y)} fill="none" {...lineStyle(s, "curve", SERIES[0], 2)} />)}</Part>
      )}
      {pts.map((q) => (
        <Part key={q.id} s={s} id={q.id}>
          <circle cx={x(q.t)} cy={y(q.v)} r={highlighted(s, q.id) ? 6 : 4} fill={INK} />
          <Label s={s} part={q.id} x={x(q.t) + 6} y={y(q.v) - 8} text={q.label} size={11} />
        </Part>
      ))}
    </Frame>
  );
}

export const motionGraph = define({
  kind: "motion_graph", schema: motionGraphSchema, draw: drawMotionGraph,
  table: (spec) => ({ columns: ["visual.table.time", "visual.table.value"], rows: (params<MotionGraphParams>(spec).points ?? []).map((q) => [q.t, q.value]) }),
});

// --- statistics_chart -----------------------------------------------------------------------------------------------

interface StatsParams {
  chart: "bar" | "pie" | "histogram" | "frequency_polygon" | "cumulative_frequency" | "box_plot";
  categories?: { id: string; label: string; value: string }[];
  classes?: { id: string; lower: string; upper: string; frequency: string }[];
  summary?: { min: string; q1: string; median: string; q3: string; max: string };
  axis_labels?: { x?: string; y?: string };
}

function drawStatistics(s: Scene): ReactElement {
  const p = params<StatsParams>(s.spec);
  const axisLabels: { xLabel?: string; yLabel?: string } = {};
  if (p.axis_labels?.x !== undefined) axisLabels.xLabel = p.axis_labels.x;
  if (p.axis_labels?.y !== undefined) axisLabels.yLabel = p.axis_labels.y;
  if (p.chart === "pie") {
    const cats = p.categories ?? [];
    const total = cats.reduce((a, c) => a + Number(c.value), 0) || 1;
    let angle = -Math.PI / 2;
    const cx = 200;
    const cy = 165;
    const r = 120;
    return (
      <Frame s={s} width={W} height={H}>
        {cats.map((c, i) => {
          const sweep = (Number(c.value) / total) * Math.PI * 2;
          const a0 = angle;
          const a1 = angle + sweep;
          angle = a1;
          const large = sweep > Math.PI ? 1 : 0;
          const d = `M${cx},${cy} L${(cx + r * Math.cos(a0)).toFixed(2)},${(cy + r * Math.sin(a0)).toFixed(2)} A${r},${r} 0 ${large} 1 ${(cx + r * Math.cos(a1)).toFixed(2)},${(cy + r * Math.sin(a1)).toFixed(2)} Z`;
          const mid = (a0 + a1) / 2;
          return (
            <Part key={c.id} s={s} id={c.id}>
              <path d={d} fill={highlighted(s, c.id) ? TINT : SERIES[i % SERIES.length]} fillOpacity={0.35} {...lineStyle(s, c.id)} />
              <Label s={s} part={c.id} x={cx + (r + 14) * Math.cos(mid)} y={cy + (r + 14) * Math.sin(mid) + 4} text={`${c.label} ${c.value}`} anchor={Math.cos(mid) >= 0 ? "start" : "end"} size={12} />
            </Part>
          );
        })}
      </Frame>
    );
  }
  if (p.chart === "box_plot") {
    const sum = p.summary ?? { min: "0", q1: "0", median: "0", q3: "0", max: "0" };
    const lo = Number(sum.min);
    const hi = Number(sum.max);
    const x = linear(lo - (hi - lo) * 0.1, hi + (hi - lo) * 0.1, BOX.left, BOX.right);
    const mid = 150;
    const keys = ["min", "q1", "median", "q3", "max"] as const;
    return (
      <Frame s={s} width={W} height={H}>
        <Axes s={s} x={x} y={() => 260} xRange={[lo - (hi - lo) * 0.1, hi + (hi - lo) * 0.1]} yRange={[0, 0]} box={{ ...BOX, top: 240, bottom: 260 }} grid={false} {...axisLabels} />
        <line x1={x(lo)} x2={x(Number(sum.q1))} y1={mid} y2={mid} stroke={INK} strokeWidth={STROKE} />
        <line x1={x(Number(sum.q3))} x2={x(hi)} y1={mid} y2={mid} stroke={INK} strokeWidth={STROKE} />
        <rect x={x(Number(sum.q1))} y={mid - 30} width={x(Number(sum.q3)) - x(Number(sum.q1))} height={60} fill="none" stroke={INK} strokeWidth={STROKE} />
        {keys.map((k, i) => (
          <Part key={k} s={s} id={k}>
            <line x1={x(Number(sum[k]))} x2={x(Number(sum[k]))} y1={mid - (k === "min" || k === "max" ? 12 : 30)} y2={mid + (k === "min" || k === "max" ? 12 : 30)} {...lineStyle(s, k)} />
            <Label s={s} part={k} x={x(Number(sum[k]))} y={i % 2 === 0 ? mid - 40 : mid + 52} text={sum[k]} anchor="middle" size={12} />
          </Part>
        ))}
      </Frame>
    );
  }
  if (p.chart === "bar") {
    const cats = p.categories ?? [];
    const top = Math.max(1, ...cats.map((c) => Number(c.value)));
    const y = linear(0, top * 1.15, BOX.bottom, BOX.top);
    const band = (BOX.right - BOX.left) / Math.max(1, cats.length);
    return (
      <Frame s={s} width={W} height={H}>
        <Axes s={s} x={() => BOX.left} y={y} xRange={[0, 0]} yRange={[0, top * 1.15]} box={BOX} {...axisLabels} />
        {cats.map((c, i) => (
          <Part key={c.id} s={s} id={c.id}>
            <rect x={BOX.left + i * band + band * 0.15} y={y(Number(c.value))} width={band * 0.7} height={y(0) - y(Number(c.value))} fill={areaFill(s, c.id, SERIES[0])} fillOpacity={0.45} {...lineStyle(s, c.id)} />
            <Label s={s} part={c.id} x={BOX.left + i * band + band / 2} y={y(Number(c.value)) - 6} text={c.value} anchor="middle" size={12} />
            <Label s={s} x={BOX.left + i * band + band / 2} y={BOX.bottom + 16} text={c.label} anchor="middle" size={11} />
          </Part>
        ))}
      </Frame>
    );
  }
  const classes = p.classes ?? [];
  const lo = Math.min(...classes.map((c) => Number(c.lower)), 0);
  const hi = Math.max(...classes.map((c) => Number(c.upper)), 1);
  const x = linear(lo, hi, BOX.left, BOX.right);
  const density = classes.map((c) => Number(c.frequency) / ((Number(c.upper) - Number(c.lower)) || 1));
  const heights = p.chart === "histogram" ? density : classes.map((c) => Number(c.frequency));
  const top = Math.max(1, ...heights);
  const y = linear(0, top * 1.15, BOX.bottom, BOX.top);
  const at = (c: (typeof classes)[number]) => (p.chart === "cumulative_frequency" ? Number(c.upper) : (Number(c.lower) + Number(c.upper)) / 2);
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[lo, hi]} yRange={[0, top * 1.15]} box={BOX} {...axisLabels} />
      {p.chart !== "histogram" && classes.length > 1 && (
        <polyline points={classes.map((c, i) => `${x(at(c)).toFixed(2)},${y(must(heights[i])).toFixed(2)}`).join(" ")} fill="none" stroke={SERIES[0]} strokeWidth={2} />
      )}
      {classes.map((c, i) => (
        <Part key={c.id} s={s} id={c.id}>
          {p.chart === "histogram" ? (
            <rect x={x(Number(c.lower))} y={y(must(heights[i]))} width={x(Number(c.upper)) - x(Number(c.lower))} height={y(0) - y(must(heights[i]))} fill={areaFill(s, c.id, SERIES[0])} fillOpacity={0.45} {...lineStyle(s, c.id)} />
          ) : (
            <circle cx={x(at(c))} cy={y(must(heights[i]))} r={highlighted(s, c.id) ? 6 : 4} fill={INK} />
          )}
          <Label s={s} part={c.id} x={p.chart === "histogram" ? (x(Number(c.lower)) + x(Number(c.upper))) / 2 : x(at(c))} y={y(must(heights[i])) - 8} text={c.frequency} anchor="middle" size={11} />
        </Part>
      ))}
    </Frame>
  );
}

export const statisticsChart = define({
  kind: "statistics_chart", schema: statisticsSchema, draw: drawStatistics,
  table: (spec) => {
    const p = params<StatsParams>(spec);
    if (p.chart === "box_plot" && p.summary !== undefined) return { columns: ["visual.table.measure", "visual.table.value"], rows: Object.entries(p.summary).map(([k, v]) => [k, v]) };
    if (p.categories !== undefined && p.categories.length > 0) return { columns: ["visual.table.category", "visual.table.value"], rows: p.categories.map((c) => [c.label, c.value]) };
    return { columns: ["visual.table.class", "visual.table.frequency"], rows: (p.classes ?? []).map((c) => [`${c.lower} – ${c.upper}`, c.frequency]) };
  },
});

// --- titration_curve ------------------------------------------------------------------------------------------------

interface TitrationParams {
  indicator?: string;
  marks?: string[];
}

interface TitrationComputed {
  points?: Pt[];
  equivalence?: { volume: { decimal: string }; ph: { decimal: string } };
  half_equivalence?: { volume: { decimal: string }; ph: { decimal: string } };
  start_ph?: { decimal: string };
  axes?: { x_unit?: string };
}

/** Indicator colour-change ranges are drawn as a band with the indicator's name, never as figures (N0 shading). */
const INDICATOR_BAND: Record<string, [number, number]> = {
  phenolphthalein: [8.2, 10], methyl_orange: [3.1, 4.4], screened_methyl_orange: [3.1, 4.4], bromothymol_blue: [6, 7.6], litmus: [5, 8],
};

function drawTitration(s: Scene): ReactElement {
  const p = params<TitrationParams>(s.spec);
  const c = computed<TitrationComputed>(s.spec);
  const pts = c.points ?? [];
  if (pts.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const vmax = Math.max(...pts.map((q) => q[0]));
  const x = linear(0, vmax, BOX.left, BOX.right);
  const y = linear(0, 14, BOX.bottom, BOX.top);
  const eq = c.equivalence;
  const marks = p.marks ?? [];
  const band = INDICATOR_BAND[p.indicator ?? ""];
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[0, vmax]} yRange={[0, 14]} box={BOX} xLabel={`V (${c.axes?.x_unit ?? ""})`} yLabel="pH" />
      {marks.includes("indicator_range") && band !== undefined && shown(s, "indicator_range") && (
        <Part s={s} id="indicator_range">
          <rect x={BOX.left} y={y(band[1])} width={BOX.right - BOX.left} height={y(band[0]) - y(band[1])} fill={areaFill(s, "indicator_range", TINT)} fillOpacity={0.5} />
        </Part>
      )}
      <Part s={s} id="curve"><polyline points={polyline(pts, x, y)} fill="none" {...lineStyle(s, "curve", SERIES[0], 2)} /></Part>
      {eq !== undefined && (
        <Part s={s} id="equivalence">
          <circle cx={x(Number(eq.volume.decimal))} cy={y(Number(eq.ph.decimal))} r={highlighted(s, "equivalence") ? 6 : 4} fill={INK} />
          <line x1={x(Number(eq.volume.decimal))} x2={x(Number(eq.volume.decimal))} y1={y(Number(eq.ph.decimal))} y2={BOX.bottom} stroke={INK} strokeWidth={1} strokeDasharray="4 3" />
          <Label s={s} part="equivalence" x={x(Number(eq.volume.decimal)) + 8} y={y(Number(eq.ph.decimal)) + 4} text={`pH ${computedLabel(s.spec, "equivalence")}, ${computedLabel(s.spec, "equivalence_volume")}`} size={11} />
        </Part>
      )}
      {marks.includes("half_equivalence") && c.half_equivalence !== undefined && (
        <Part s={s} id="half_equivalence">
          <circle cx={x(Number(c.half_equivalence.volume.decimal))} cy={y(Number(c.half_equivalence.ph.decimal))} r={4} fill={INK} />
          <Label s={s} part="half_equivalence" x={x(Number(c.half_equivalence.volume.decimal)) + 8} y={y(Number(c.half_equivalence.ph.decimal)) - 6} text={`pH ${computedLabel(s.spec, "half_equivalence")}`} size={11} />
        </Part>
      )}
      {marks.includes("start_ph") && c.start_ph !== undefined && (
        <Part s={s} id="start_ph">
          <circle cx={x(0)} cy={y(Number(c.start_ph.decimal))} r={4} fill={INK} />
          <Label s={s} part="start_ph" x={x(0) + 8} y={y(Number(c.start_ph.decimal)) - 6} text={`pH ${computedLabel(s.spec, "start_ph")}`} size={11} />
        </Part>
      )}
    </Frame>
  );
}

export const titrationCurve = define({
  kind: "titration_curve", schema: "visuals/kinds/titration_curve", draw: drawTitration,
  table: (spec) => ({ columns: ["visual.table.volume", "visual.table.ph"], rows: (computed<TitrationComputed>(spec).points ?? []).map(([v, ph]) => [fmt(v), fmt(ph)]) }),
});

// --- break_even_chart -----------------------------------------------------------------------------------------------

interface BreakEvenComputed {
  lines?: Record<"fixed_cost" | "total_cost" | "revenue", Pt[]>;
  break_even?: Pt;
  max_units?: number;
}

const LINES = ["fixed_cost", "total_cost", "revenue"] as const;

function drawBreakEven(s: Scene): ReactElement {
  const c = computed<BreakEvenComputed>(s.spec);
  const p = params<{ output_units?: number }>(s.spec);
  if (c.lines === undefined || c.break_even === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const xmax = c.max_units ?? Math.max(...c.lines.revenue.map((q) => q[0]));
  const ymax = Math.max(...LINES.flatMap((k) => must(c.lines)[k].map((q) => q[1])));
  const x = linear(0, xmax, BOX.left, BOX.right);
  const y = linear(0, ymax * 1.08, BOX.bottom, BOX.top);
  const [bx, by] = c.break_even;
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[0, xmax]} yRange={[0, ymax * 1.08]} box={BOX} xLabel={s.t("visual.break_even.units")} yLabel={s.t("visual.break_even.money")} />
      {p.output_units !== undefined && shown(s, "margin_of_safety") && (
        <Part s={s} id="margin_of_safety">
          <rect x={x(bx)} y={BOX.bottom - 14} width={x(p.output_units) - x(bx)} height={10} fill={areaFill(s, "margin_of_safety", TINT)} {...lineStyle(s, "margin_of_safety", INK, 1)} />
          <Label s={s} part="margin_of_safety" x={(x(bx) + x(p.output_units)) / 2} y={BOX.bottom - 20} text={`${s.t("visual.break_even.margin")} ${computedLabel(s.spec, "margin_of_safety")}`} anchor="middle" size={11} />
        </Part>
      )}
      {LINES.map((k, i) => {
        const pts = must(c.lines)[k];
        const end = must(pts[pts.length - 1]);
        return (
          <Part key={k} s={s} id={k}>
            <polyline points={polyline(pts, x, y)} fill="none" {...lineStyle(s, k, SERIES[i], 2)} {...(DASHES[i] ? { strokeDasharray: DASHES[i] } : {})} />
            <Label s={s} part={k} x={x(end[0]) - 4} y={y(end[1]) - 6 - i * 2} text={s.t(`visual.break_even.${k}`)} anchor="end" size={11} />
          </Part>
        );
      })}
      <Part s={s} id="break_even">
        <circle cx={x(bx)} cy={y(by)} r={highlighted(s, "break_even") ? 6 : 4} fill={INK} />
        <Label s={s} part="break_even" x={x(bx) + 8} y={y(by) + 16} text={`${s.t("visual.break_even.point")} (${computedLabel(s.spec, "break_even")})`} size={11} />
      </Part>
    </Frame>
  );
}

export const breakEvenChart = define({
  kind: "break_even_chart", schema: breakEvenSchema, draw: drawBreakEven,
  table: (spec) => {
    const lines = computed<BreakEvenComputed>(spec).lines;
    if (lines === undefined) return { columns: [], rows: [] };
    return {
      columns: ["visual.table.units", "visual.break_even.fixed_cost", "visual.break_even.total_cost", "visual.break_even.revenue"],
      rows: lines.revenue.map(([u], i) => [fmt(u), fmt(lines.fixed_cost[i]?.[1] ?? 0), fmt(lines.total_cost[i]?.[1] ?? 0), fmt(lines.revenue[i]?.[1] ?? 0)]),
    };
  },
});
