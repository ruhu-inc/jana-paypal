// function_transform_sim (VSL-08a, VSL-46): sliders for a, b, c and d in y = a f(b(x + c)) + d, with the parent graph
// y = f(x) dashed behind the transformed one and the tracked point (where the parent's point at x0 goes) marked. The
// curve is sampled by the renderer's own deterministic parser (expr.ts, no eval); the resolution checks the starting
// curve with math_plot_points and the tracked point with math_evaluate.
import type { ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { evaluate, parse } from "../expr.js";
import { INK, LINE, MUTED, STRONG, WordsFallback, keyOnly } from "../kit.js";
import type { SimSetup, Values } from "./models.js";
import { scenarioOf } from "./predict.js";
import { functionTransform, type Slider } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 300;

interface FunctionParams {
  expression: string;
  x_range: [number, number];
  parameters: Record<"a" | "b" | "c" | "d", Slider>;
  locked?: string[];
}

/** The model and starting values a resolved function_transform_sim spec gives (the renderer's, and the tests'). */
export function functionSetup(spec: ResolvedSpec): SimSetup<null> {
  const p = spec.params as FunctionParams;
  const x0 = (spec.computed as { x0?: number }).x0 ?? 0;
  const model = functionTransform(p.expression, p.x_range, p.parameters, x0);
  const start: Values = Object.fromEntries((["a", "b", "c", "d"] as const).map((k) => [k, p.parameters[k].value]));
  return { model, start, locked: p.locked ?? [] };
}

function FunctionSim(props: VisualProps): ReactElement {
  const p = props.spec.params as FunctionParams;
  // An expression the deterministic parser cannot read is described in words (the engine refuses it before here).
  let node: ReturnType<typeof parse>;
  try {
    node = parse(p.expression);
  } catch {
    return <WordsFallback spec={props.spec} reason="unparsable_expression" />;
  }
  const { model, start, locked } = functionSetup(props.spec);
  const t = props.t ?? keyOnly;
  const f = (x: number) => evaluate(node, x);
  const [lo, hi] = p.x_range;
  // A fixed window: the parent graph over the middle of the x range (a steep curve's ends would flatten the rest),
  // with room for the furthest the d slider can move it.
  const parent = Array.from({ length: 121 }, (_, i) => lo + ((hi - lo) * i) / 120).map((x) => [x, f(x)] as const);
  const ys = parent.filter(([x]) => Math.abs(x - (lo + hi) / 2) <= 0.3 * (hi - lo)).map(([, y]) => y).filter(Number.isFinite);
  const room = Math.max(Math.abs(p.parameters.d.min), Math.abs(p.parameters.d.max), 1);
  const ylo = Math.min(0, ...ys) - room;
  const yhi = Math.max(0, ...ys) + room;
  const L = 16, R = 16, T = 12, B = 16;
  const X = (v: number) => L + ((v - lo) / (hi - lo)) * (W - L - R);
  const Y = (v: number) => H - B - ((v - ylo) / (yhi - ylo)) * (H - T - B);
  const path = (pts: readonly (readonly [number, number])[]) => {
    // Breaks where the curve is undefined or leaves the window far enough to be a jump, not a line.
    const parts: string[] = [];
    let open = false;
    for (const [x, y] of pts) {
      if (!Number.isFinite(y) || y < ylo - (yhi - ylo) || y > yhi + (yhi - ylo)) {
        open = false;
        continue;
      }
      parts.push(`${open ? "L" : "M"}${X(x).toFixed(1)},${Y(y).toFixed(1)}`);
      open = true;
    }
    return parts.join(" ");
  };
  const step = Math.max(1, Math.ceil((hi - lo) / 12));
  const ticks = Array.from({ length: Math.floor((hi - lo) / step) + 1 }, (_, i) => Math.ceil(lo / step) * step + i * step).filter((x) => x <= hi);
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated={false} scenario={scenarioOf("function_transform_sim", props.spec.params)} draw={(_state, values) => {
      const rows = model.table(values).rows.map((r) => [r[0] ?? NaN, r[1] ?? NaN] as const);
      const r = model.read(null, values);
      const px = r["out.point_x"] ?? NaN;
      const py = r["out.point_y"] ?? NaN;
      const clip = `${props.spec.visual_id}-plot`;
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.function.drawing")}>
          <clipPath id={clip}><rect x={L} y={T} width={W - L - R} height={H - T - B} /></clipPath>
          {ticks.map((x) => <line key={x} x1={X(x)} x2={X(x)} y1={T} y2={H - B} stroke={LINE} strokeWidth={0.75} />)}
          <line x1={L} x2={W - R} y1={Y(0)} y2={Y(0)} stroke={STRONG} strokeWidth={1.25} />
          <line x1={X(0)} x2={X(0)} y1={T} y2={H - B} stroke={STRONG} strokeWidth={1.25} />
          {ticks.filter((x) => x !== 0).map((x) => <text key={x} x={X(x)} y={Y(0) + 14} textAnchor="middle" fontSize={11} fill={MUTED}>{x}</text>)}
          <path d={path(parent)} fill="none" stroke={MUTED} strokeWidth={1.5} strokeDasharray="6 5" clipPath={`url(#${clip})`} data-sim-parent="true" />
          <path d={path(rows)} fill="none" stroke={INK} strokeWidth={2.5} clipPath={`url(#${clip})`} data-sim-curve="true" />
          {Number.isFinite(px) && Number.isFinite(py) && py >= ylo && py <= yhi && (
            <circle cx={X(px)} cy={Y(py)} r={5} fill={INK} stroke="var(--color-canvas)" strokeWidth={1.5} data-sim-point="true" />
          )}
          <rect x={L + 4} y={T + 2} width={118} height={18} fill="var(--color-canvas)" />
          <text x={L + 8} y={T + 15} fontSize={12} fill={MUTED}>{t("sim.function.legend")}</text>
        </svg>
      );
    }} />
  );
}

export const functionTransformSimulation = define({ kind: "function_transform_sim", schema: "visuals/kinds/function_transform_sim", labelModes: ["shown"], draw: (_s, props) => <FunctionSim {...props} /> });

