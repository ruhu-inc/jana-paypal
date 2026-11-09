// The A level science and statistics graphs (VSL-07, VSL-46): exponential_curve, distribution_curve and data_plot.
// As for the S2 graphs, curves, bars and lines come from the resolution's tool results (computed), axis ticks are
// presentation numbers, and every labelled figure is a computed label; a symbol or an event such as P(X ≤ 4) is drawn
// as a formula. Each offers its plotted values as a table (LOC-16).
import type { ReactElement } from "react";
import type { ResolvedSpec, TableData } from "./contract.js";
import { define } from "./define.js";
import { must } from "./must.js";
import {
  Axes, DASHES, Frame, INK, Label, MUTED, Part, SERIES, TINT, WordsFallback, areaFill, computed, computedLabel, fmt, highlighted, lineStyle, linear,
  params, type Scene,
} from "./kit.js";

const W = 480;
const H = 320;
const BOX = { left: 52, right: W - 20, top: 36, bottom: H - 40 };

type Pt = [number, number];

function polyline(points: Pt[], x: (v: number) => number, y: (v: number) => number): string {
  return points.map(([a, b]) => `${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ");
}

function finite(points: unknown): Pt[] {
  return Array.isArray(points) ? (points as Pt[]).filter((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1])) : [];
}

// --- exponential_curve ----------------------------------------------------------------------------------------------

interface ExponentialParams {
  initial: number;
  mode: "decay" | "charge";
  time_range: [number, number];
  time_unit: string;
  quantity_unit: string;
  mark: "half_life" | "time_constant" | "none";
}

interface ExponentialComputed {
  curve?: Pt[];
  mark?: { t: number; value: number };
}

function drawExponential(s: Scene): ReactElement {
  const p = params<ExponentialParams>(s.spec);
  const c = computed<ExponentialComputed>(s.spec);
  const curve = finite(c.curve);
  if (curve.length < 2) return <WordsFallback spec={s.spec} reason="text_only" />;
  const [t0, t1] = p.time_range;
  const top = Math.max(...curve.map((q) => q[1]), p.initial) * 1.1 || 1;
  const x = linear(t0, t1, BOX.left, BOX.right);
  const y = linear(0, top, BOX.bottom, BOX.top);
  const mark = p.mark === "none" ? undefined : c.mark;
  // The mark's words go where the curve is not: above and right of a falling curve, below and right of a rising one.
  const side = p.mode === "decay" ? -8 : 32;
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[t0, t1]} yRange={[0, top]} box={BOX} xLabel={`t (${p.time_unit})`} yLabel={p.quantity_unit} />
      {p.mode === "charge" && <line x1={BOX.left} x2={BOX.right} y1={y(p.initial)} y2={y(p.initial)} stroke={MUTED} strokeWidth={1} strokeDasharray={DASHES[1]} aria-hidden="true" />}
      <Part s={s} id="curve"><polyline points={polyline(curve, x, y)} fill="none" {...lineStyle(s, "curve", SERIES[0], 2)} /></Part>
      {mark !== undefined && p.mark !== "none" && (
        <Part s={s} id={p.mark}>
          <path d={`M${x(t0).toFixed(2)},${y(mark.value).toFixed(2)} L${x(mark.t).toFixed(2)},${y(mark.value).toFixed(2)} L${x(mark.t).toFixed(2)},${y(0).toFixed(2)}`} fill="none" {...lineStyle(s, p.mark, INK, 1)} strokeDasharray={DASHES[2]} />
          <circle cx={x(mark.t)} cy={y(mark.value)} r={highlighted(s, p.mark) ? 6 : 4} fill={INK} />
          <Label s={s} part={p.mark} x={x(mark.t) + 8} y={y(mark.value) + side - 16} text={s.t(`visual.exponential.${p.mark}`)} formula />
          <Label s={s} part={p.mark} x={x(mark.t) + 8} y={y(mark.value) + side} text={`${computedLabel(s.spec, p.mark)} ${p.time_unit}`} size={12} />
          <Label s={s} part={p.mark} x={x(t0) + 4} y={y(mark.value) - 6} text={`${computedLabel(s.spec, `${p.mark}.value`)} ${p.quantity_unit}`} size={11} />
        </Part>
      )}
    </Frame>
  );
}

function exponentialTable(spec: ResolvedSpec): TableData {
  return { columns: ["visual.table.time", "visual.table.value"], rows: finite(computed<ExponentialComputed>(spec).curve).map(([t, v]) => [fmt(t), fmt(v)]) };
}

export const exponentialCurve = define({ kind: "exponential_curve", schema: "visuals/kinds/exponential_curve", draw: drawExponential, table: exponentialTable });

// --- distribution_curve ---------------------------------------------------------------------------------------------

interface DistributionParams {
  distribution: "normal" | "binomial" | "poisson";
  parameters: { mean?: number; sd?: number; n?: number; p?: number; lambda?: number };
}

interface DistributionComputed {
  points?: Pt[];
  window?: [number, number];
  bars?: Pt[];
  shade?: number[];
  event?: string;
}

/** The distribution's mean: a presentation position for the mean line, from the request's own parameters. */
function meanOf(p: DistributionParams): number {
  const q = p.parameters;
  if (p.distribution === "normal") return q.mean ?? NaN;
  if (p.distribution === "binomial") return (q.n ?? NaN) * (q.p ?? NaN);
  return q.lambda ?? NaN;
}

/** The density at `at`, interpolated between the two samples either side (where the shading starts and stops). */
function densityAt(points: Pt[], at: number): number {
  for (let i = 1; i < points.length; i += 1) {
    const [a, fa] = must(points[i - 1]);
    const [b, fb] = must(points[i]);
    if (at >= a && at <= b) return b === a ? fa : fa + ((at - a) / (b - a)) * (fb - fa);
  }
  return 0;
}

function ProbabilityLabel({ s, x, anchor }: { s: Scene; x: number; anchor: "start" | "end" }): ReactElement {
  const c = computed<DistributionComputed>(s.spec);
  return (
    <>
      <Label s={s} part="shade" x={x} y={BOX.top + 4} text={c.event ?? ""} anchor={anchor} formula />
      <Label s={s} part="shade" x={x} y={BOX.top + 22} text={`= ${computedLabel(s.spec, "shade")}`} anchor={anchor} />
    </>
  );
}

function drawDistribution(s: Scene): ReactElement {
  const p = params<DistributionParams>(s.spec);
  const c = computed<DistributionComputed>(s.spec);
  const mean = meanOf(p);
  if (p.distribution === "normal") {
    const points = finite(c.points);
    if (points.length < 2 || c.window === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
    const [w0, w1] = c.window;
    const top = Math.max(...points.map((q) => q[1])) * 1.25 || 1;
    const x = linear(w0, w1, BOX.left, BOX.right);
    const y = linear(0, top, BOX.bottom, BOX.top);
    const [a, b] = [c.shade?.[0] ?? w0, c.shade?.[1] ?? w0];
    const inside: Pt[] = [[a, densityAt(points, a)], ...points.filter(([v]) => v > a && v < b), [b, densityAt(points, b)]];
    const area = `M${x(a).toFixed(2)},${y(0).toFixed(2)} ${inside.map(([u, v]) => `L${x(u).toFixed(2)},${y(v).toFixed(2)}`).join(" ")} L${x(b).toFixed(2)},${y(0).toFixed(2)} Z`;
    return (
      <Frame s={s} width={W} height={H}>
        <Axes s={s} x={x} y={y} xRange={[w0, w1]} yRange={[0, top]} box={BOX} xLabel="x" />
        <Part s={s} id="curve"><polyline points={polyline(points, x, y)} fill="none" {...lineStyle(s, "curve", SERIES[0], 2)} /></Part>
        <Part s={s} id="shade">
          {b > a && <path d={area} fill={areaFill(s, "shade", SERIES[0])} fillOpacity={0.35} stroke="none" />}
          <ProbabilityLabel s={s} x={BOX.left + 8} anchor="start" />
        </Part>
        <MeanLine s={s} at={x(mean)} top={BOX.top + 30} />
      </Frame>
    );
  }
  const bars = finite(c.bars);
  if (bars.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const k0 = must(bars[0])[0];
  const k1 = must(bars[bars.length - 1])[0];
  const top = Math.max(...bars.map((q) => q[1])) * 1.25 || 1;
  const x = linear(k0 - 0.5, k1 + 0.5, BOX.left, BOX.right);
  const y = linear(0, top, BOX.bottom, BOX.top);
  const width = Math.max(2, (x(1) - x(0)) * 0.8);
  const [first, last] = c.shade ?? [];
  const shaded = bars.filter(([k]) => first !== undefined && last !== undefined && k >= first && k <= last);
  const bar = ([k, h]: Pt, fill: string, opacity: number, id: string) => (
    <rect key={k} x={x(k) - width / 2} y={y(h)} width={width} height={y(0) - y(h)} fill={fill} fillOpacity={opacity} {...lineStyle(s, id, INK, 1)} />
  );
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[k0 - 0.5, k1 + 0.5]} yRange={[0, top]} box={BOX} xLabel="x" />
      <Part s={s} id="curve">{bars.map((q) => bar(q, areaFill(s, "curve"), 1, "curve"))}</Part>
      <Part s={s} id="shade">
        {shaded.map((q) => bar(q, highlighted(s, "shade") ? TINT : SERIES[0], 0.55, "shade"))}
        <ProbabilityLabel s={s} x={mean > (k0 + k1) / 2 ? BOX.left + 8 : BOX.right - 8} anchor={mean > (k0 + k1) / 2 ? "start" : "end"} />
      </Part>
      <MeanLine s={s} at={x(mean)} top={BOX.top + 30} />
    </Frame>
  );
}

function MeanLine({ s, at, top }: { s: Scene; at: number; top: number }): ReactElement {
  return (
    <Part s={s} id="mean">
      <line x1={at} x2={at} y1={top} y2={BOX.bottom} {...lineStyle(s, "mean", INK, 1)} strokeDasharray={DASHES[1]} />
      <Label s={s} part="mean" x={at + 4} y={top + 10} text={s.t("visual.distribution.mean")} size={11} />
    </Part>
  );
}

function distributionTable(spec: ResolvedSpec): TableData {
  const p = params<DistributionParams>(spec);
  const c = computed<DistributionComputed>(spec);
  if (p.distribution === "normal") return { columns: ["visual.table.value", "visual.table.density"], rows: finite(c.points).map(([v, d]) => [fmt(v), fmt(d)]) };
  return { columns: ["visual.table.outcome", "visual.table.probability"], rows: finite(c.bars).map(([k, h]) => [fmt(k), fmt(h)]) };
}

export const distributionCurve = define({ kind: "distribution_curve", schema: "visuals/kinds/distribution_curve", draw: drawDistribution, table: distributionTable });

// --- data_plot ------------------------------------------------------------------------------------------------------

interface DataParams {
  points: { id: string; x: number; y: number }[];
  x_label: string;
  y_label: string;
  x_unit: string;
  y_unit: string;
  fit: "none" | "linear";
  show_gradient?: boolean;
}

interface DataComputed {
  line?: Pt[];
  intercept?: number;
}

/** A data point's part id (VSL-19 allows no hyphen or dot inside an id): the engine's rule, `data_point_part`. */
export function dataPointPart(id: string): string {
  return id.replace(/[^A-Za-z0-9_]/g, "_");
}

/** A range that starts at zero when the data start near it (a practical plot shows its origin), padded a little. */
function span(values: number[]): [number, number] {
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo >= 0 && lo <= hi / 2) lo = 0;
  if (hi <= 0 && hi >= lo / 2) hi = 0;
  const pad = (hi - lo) * 0.08 || 1;
  return [lo === 0 ? 0 : lo - pad, hi === 0 ? 0 : hi + pad];
}

function drawDataPlot(s: Scene): ReactElement {
  const p = params<DataParams>(s.spec);
  const c = computed<DataComputed>(s.spec);
  const line = p.fit === "linear" ? finite(c.line) : [];
  const pts = (p.points ?? []).filter((q) => Number.isFinite(q.x) && Number.isFinite(q.y));
  if (pts.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const [x0, x1] = span(pts.map((q) => q.x));
  const fitted = line.length === 2;
  const intercept = fitted && c.intercept !== undefined && Number.isFinite(c.intercept) && x0 <= 0 && x1 >= 0 ? c.intercept : undefined;
  const [y0, y1] = span([...pts.map((q) => q.y), ...line.map((q) => q[1]), ...(intercept !== undefined ? [intercept] : [])]);
  const x = linear(x0, x1, BOX.left, BOX.right);
  const y = linear(y0, y1, BOX.bottom, BOX.top);
  const [a, b] = fitted ? [must(line[0]), must(line[1])] : [[0, 0] as Pt, [0, 0] as Pt];
  const unit = `${p.y_unit}/${p.x_unit}`;
  // The gradient's words go in the corner a rising line leaves empty (top left), the intercept's in the opposite one.
  const rising = b[1] >= a[1];
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[x0, x1]} yRange={[y0, y1]} box={BOX} xLabel={`${p.x_label} (${p.x_unit})`} yLabel={`${p.y_label} (${p.y_unit})`} />
      {pts.map((q) => {
        const id = dataPointPart(q.id);
        return (
          <Part key={id} s={s} id={id}>
            <path d={`M${x(q.x) - 5},${y(q.y) - 5} L${x(q.x) + 5},${y(q.y) + 5} M${x(q.x) - 5},${y(q.y) + 5} L${x(q.x) + 5},${y(q.y) - 5}`} fill="none" {...lineStyle(s, id, INK, highlighted(s, id) ? 3 : 1.5)} />
          </Part>
        );
      })}
      {fitted && (
        <Part s={s} id="fit">
          <line x1={x(a[0])} y1={y(a[1])} x2={x(b[0])} y2={y(b[1])} {...lineStyle(s, "fit", SERIES[0], 2)} />
          {intercept !== undefined && a[0] > 0 && <line x1={x(0)} y1={y(intercept)} x2={x(a[0])} y2={y(a[1])} {...lineStyle(s, "fit", SERIES[0], 1.5)} strokeDasharray={DASHES[1]} />}
          <Label s={s} part="fit" x={rising ? BOX.left + 8 : BOX.right - 8} y={BOX.top + 4} text={s.t("visual.data_plot.gradient")} anchor={rising ? "start" : "end"} formula />
          <Label s={s} part="fit" x={rising ? BOX.left + 8 : BOX.right - 8} y={BOX.top + 22} text={`= ${computedLabel(s.spec, "fit.gradient")} ${unit}`} anchor={rising ? "start" : "end"} />
        </Part>
      )}
      {fitted && p.show_gradient === true && (
        <Part s={s} id="gradient">
          <path d={`M${x(a[0]).toFixed(2)},${y(a[1]).toFixed(2)} L${x(b[0]).toFixed(2)},${y(a[1]).toFixed(2)} L${x(b[0]).toFixed(2)},${y(b[1]).toFixed(2)}`} fill="none" {...lineStyle(s, "gradient", INK, 1)} strokeDasharray={DASHES[2]} />
          <Label s={s} part="gradient" x={(x(a[0]) + x(b[0])) / 2} y={y(a[1]) + 16 <= BOX.bottom - 4 ? y(a[1]) + 16 : y(a[1]) - 6} text={`${computedLabel(s.spec, "gradient.dx")} ${p.x_unit}`} anchor="middle" size={11} />
          <Label s={s} part="gradient" x={x(b[0]) + (x(b[0]) > W - 70 ? -6 : 6)} y={(y(a[1]) + y(b[1])) / 2} text={`${computedLabel(s.spec, "gradient.dy")} ${p.y_unit}`} anchor={x(b[0]) > W - 70 ? "end" : "start"} size={11} />
        </Part>
      )}
      {fitted && (
        <Part s={s} id="intercept">
          {intercept !== undefined && <circle cx={x(0)} cy={y(intercept)} r={highlighted(s, "intercept") ? 6 : 4} fill={INK} />}
          <Label s={s} part="intercept" x={rising ? BOX.right - 4 : BOX.left + 8} y={BOX.bottom - 26} text={s.t("visual.data_plot.intercept")} anchor={rising ? "end" : "start"} formula />
          <Label s={s} part="intercept" x={rising ? BOX.right - 4 : BOX.left + 8} y={BOX.bottom - 8} text={`= ${computedLabel(s.spec, "fit.intercept")} ${p.y_unit}`} anchor={rising ? "end" : "start"} />
        </Part>
      )}
    </Frame>
  );
}

function dataTable(spec: ResolvedSpec): TableData {
  const p = params<DataParams>(spec);
  return { columns: ["visual.table.point", "visual.table.x", "visual.table.y"], rows: (p.points ?? []).map((q) => [q.id, fmt(q.x), fmt(q.y)]) };
}

export const dataPlot = define({ kind: "data_plot", schema: "visuals/kinds/data_plot", draw: drawDataPlot, table: dataTable });
