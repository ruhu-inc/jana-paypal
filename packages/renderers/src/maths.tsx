// Mathematics template kinds (VSL-03, VSL-04, VSL-29): number_line, geometry_figure, unit_circle, venn_diagram,
// bearing_diagram, probability_tree and transformation_grid. Geometry is laid out here from the parameters and the
// tool results (geom_figure, math_evaluate); every figure a label shows is a tool result or a traced given.
import { must } from "./must.js";
import type { ReactElement } from "react";
import { define } from "./define.js";
import {
  Arrow, Frame, INK, LINE, Label, MUTED, PAPER, Part, RING, SERIES, STROKE, STRONG, TINT, WordsFallback, areaFill, computed, computedLabel, highlighted,
  lineStyle, linear, params, ticks, type Scene,
} from "./kit.js";
import numberLineSchema from "./schemas/number_line.schema.json" with { type: "json" };
import geometrySchema from "./schemas/geometry_figure.schema.json" with { type: "json" };
import unitCircleSchema from "./schemas/unit_circle.schema.json" with { type: "json" };
import vennSchema from "./schemas/venn_diagram.schema.json" with { type: "json" };
import bearingSchema from "./schemas/bearing_diagram.schema.json" with { type: "json" };
import treeSchema from "./schemas/probability_tree.schema.json" with { type: "json" };
import transformationSchema from "./schemas/transformation_grid.schema.json" with { type: "json" };

const W = 480;
const H = 320;
const DEG = Math.PI / 180;

type Pt = [number, number];

/** An angle arc at `at` from direction a1 to a2 (radians, SVG y down), radius r. */
function arc(at: Pt, a1: number, a2: number, r: number): string {
  let sweep = a2 - a1;
  while (sweep < 0) sweep += Math.PI * 2;
  while (sweep > Math.PI * 2) sweep -= Math.PI * 2;
  const p1: Pt = [at[0] + r * Math.cos(a1), at[1] + r * Math.sin(a1)];
  const p2: Pt = [at[0] + r * Math.cos(a1 + sweep), at[1] + r * Math.sin(a1 + sweep)];
  return `M${p1[0].toFixed(2)},${p1[1].toFixed(2)} A${r},${r} 0 ${sweep > Math.PI ? 1 : 0} 1 ${p2[0].toFixed(2)},${p2[1].toFixed(2)}`;
}

function dir(from: Pt, to: Pt): number {
  return Math.atan2(to[1] - from[1], to[0] - from[0]);
}

/** The arc and label spot for the interior angle at `at` between rays to `p` and `q`. */
function angleAt(at: Pt, p: Pt, q: Pt, r = 24): { d: string; label: Pt } {
  let a1 = dir(at, p);
  let a2 = dir(at, q);
  let sweep = a2 - a1;
  while (sweep < 0) sweep += Math.PI * 2;
  if (sweep > Math.PI) {
    [a1, a2] = [a2, a1];
    sweep = Math.PI * 2 - sweep;
  }
  const mid = a1 + sweep / 2;
  return { d: arc(at, a1, a2, r), label: [at[0] + (r + 14) * Math.cos(mid), at[1] + (r + 14) * Math.sin(mid) + 4] };
}

// --- number_line ----------------------------------------------------------------------------------------------------

interface Interval {
  id: string;
  from: string;
  to: string;
  from_closed?: boolean;
  to_closed?: boolean;
}

function drawNumberLine(s: Scene): ReactElement {
  const p = params<{ intervals: Interval[]; range?: [number, number]; variable?: string }>(s.spec);
  const finite = p.intervals.flatMap((i) => [i.from, i.to]).filter((v) => v !== "-inf" && v !== "inf").map(Number);
  const lo = p.range?.[0] ?? Math.floor(Math.min(...finite, 0) - 2);
  const hi = p.range?.[1] ?? Math.ceil(Math.max(...finite, 0) + 2);
  const x = linear(lo, hi, 40, W - 40);
  const base = 200;
  return (
    <Frame s={s} width={W} height={260}>
      <Part s={s} id="line" focusable={false}>
        <line x1={20} x2={W - 20} y1={base} y2={base} stroke={INK} strokeWidth={STROKE} />
        {ticks(lo, hi, 10).map((v) => (
          <g key={v}>
            <line x1={x(v)} x2={x(v)} y1={base - 5} y2={base + 5} stroke={INK} strokeWidth={1} />
            {s.labels !== "hidden" && <text x={x(v)} y={base + 20} textAnchor="middle" fontSize={11 * s.scale} fill={MUTED} data-presentation="true">{v}</text>}
          </g>
        ))}
      </Part>
      {p.intervals.map((iv, n) => {
        const y = base - 30 - n * 34;
        const a = iv.from === "-inf" ? 20 : x(Number(iv.from));
        const b = iv.to === "inf" ? W - 20 : x(Number(iv.to));
        return (
          <g key={iv.id}>
            <Part s={s} id={iv.id}>
              <line x1={a} x2={b} y1={y} y2={y} {...lineStyle(s, iv.id, SERIES[n % SERIES.length], 4)} />
              {iv.from === "-inf" && <path d={`M${a},${y} l10,-6 l0,12 Z`} fill={SERIES[n % SERIES.length]} />}
              {iv.to === "inf" && <path d={`M${b},${y} l-10,-6 l0,12 Z`} fill={SERIES[n % SERIES.length]} />}
            </Part>
            {(["from", "to"] as const).filter((end) => iv[end] !== "-inf" && iv[end] !== "inf").map((end) => {
              const id = `${iv.id}.${end}`;
              const cx = end === "from" ? a : b;
              const closed = end === "from" ? iv.from_closed === true : iv.to_closed === true;
              return (
                <Part key={id} s={s} id={id}>
                  <circle cx={cx} cy={y} r={6} fill={closed ? INK : PAPER} {...lineStyle(s, id, INK, 2)} />
                  <Label s={s} part={id} x={cx} y={y - 12} text={iv[end]} anchor="middle" size={12} />
                </Part>
              );
            })}
          </g>
        );
      })}
    </Frame>
  );
}

export const numberLine = define({ kind: "number_line", schema: numberLineSchema, draw: drawNumberLine });

// --- geometry_figure ------------------------------------------------------------------------------------------------

interface GeometryParams {
  figure: "triangle" | "circle" | "circle_theorem" | "parallel_lines";
  theorem?: string;
  givens?: Record<string, string | number>;
  vertices?: [string, string, string];
  right_angle?: string;
  label?: string[];
  pairs?: string[];
}

function fit(points: Pt[], box: { left: number; right: number; top: number; bottom: number }): (p: Pt) => Pt {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const k = Math.min((box.right - box.left) / (x1 - x0 || 1), (box.bottom - box.top) / (y1 - y0 || 1));
  const ox = box.left + ((box.right - box.left) - (x1 - x0) * k) / 2;
  const oy = box.bottom - ((box.bottom - box.top) - (y1 - y0) * k) / 2;
  return ([a, b]) => [ox + (a - x0) * k, oy - (b - y0) * k];
}

function labelled(p: GeometryParams, id: string): boolean {
  return p.label === undefined || p.label.includes(id);
}

function drawTriangle(s: Scene, p: GeometryParams): ReactElement {
  const fig = computed<{ figure?: Record<"A" | "B" | "C", Pt> }>(s.spec).figure;
  if (fig === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const names = p.vertices ?? ["A", "B", "C"];
  const map = fit([fig.A, fig.B, fig.C], { left: 80, right: W - 80, top: 50, bottom: H - 50 });
  const P: Record<string, Pt> = { A: map(fig.A), B: map(fig.B), C: map(fig.C) };
  const centre: Pt = [(must(P.A)[0] + must(P.B)[0] + must(P.C)[0]) / 3, (must(P.A)[1] + must(P.B)[1] + must(P.C)[1]) / 3];
  const sides: [string, string, string][] = [["a", "B", "C"], ["b", "C", "A"], ["c", "A", "B"]];
  return (
    <Frame s={s} width={W} height={H}>
      {sides.map(([side, u, v]) => {
        const id = `side_${side}`;
        const m: Pt = [(must(P[u])[0] + must(P[v])[0]) / 2, (must(P[u])[1] + must(P[v])[1]) / 2];
        const out: Pt = [m[0] + (m[0] - centre[0]) * 0.18, m[1] + (m[1] - centre[1]) * 0.18 + 4];
        return (
          <Part key={id} s={s} id={id}>
            <line x1={must(P[u])[0]} y1={must(P[u])[1]} x2={must(P[v])[0]} y2={must(P[v])[1]} {...lineStyle(s, id)} />
            {labelled(p, id) && <Label s={s} part={id} x={out[0]} y={out[1]} text={computedLabel(s.spec, id, side)} anchor="middle" />}
          </Part>
        );
      })}
      {(["A", "B", "C"] as const).map((corner, i) => {
        const id = `angle_${corner}`;
        const others = (["A", "B", "C"] as const).filter((c) => c !== corner);
        const at = must(P[corner]);
        const right = p.right_angle === corner;
        const { d, label } = angleAt(at, must(P[must(others[0])]), must(P[must(others[1])]), 22);
        const u = dir(at, must(P[must(others[0])]));
        const v = dir(at, must(P[must(others[1])]));
        const sq = `M${at[0] + 14 * Math.cos(u)},${at[1] + 14 * Math.sin(u)} L${at[0] + 14 * Math.cos(u) + 14 * Math.cos(v)},${at[1] + 14 * Math.sin(u) + 14 * Math.sin(v)} L${at[0] + 14 * Math.cos(v)},${at[1] + 14 * Math.sin(v)}`;
        return (
          <Part key={id} s={s} id={id}>
            <path d={right ? sq : d} fill="none" {...lineStyle(s, id, INK, 1)} />
            {labelled(p, id) && !right && <Label s={s} part={id} x={label[0]} y={label[1]} text={`${computedLabel(s.spec, id, names[i] ?? corner)}°`} anchor="middle" size={11} />}
          </Part>
        );
      })}
      {names.map((n, i) => {
        const at = must(P[must((["A", "B", "C"] as const)[i])]);
        const out: Pt = [at[0] + (at[0] - centre[0]) * 0.12, at[1] + (at[1] - centre[1]) * 0.12 + 5];
        return (
          <Part key={n} s={s} id={n}>
            <circle cx={at[0]} cy={at[1]} r={3} fill={INK} />
            <Label s={s} part={n} x={out[0]} y={out[1]} text={n} anchor="middle" />
          </Part>
        );
      })}
    </Frame>
  );
}

const CX = 240;
const CY = 160;
const R = 110;

function onCircle(deg: number): Pt {
  return [CX + R * Math.cos(-deg * DEG), CY + R * Math.sin(-deg * DEG)];
}

/** Circle theorem layouts: points placed so the drawn angles are the tool's values (geom_figure, VER-15). */
function theoremLayout(theorem: string, given: number, derived: number): { points: Record<string, Pt>; lines: [string, string][]; given: [string, string, string]; derived: [string, string, string]; tangent?: [Pt, Pt] } {
  const O: Pt = [CX, CY];
  switch (theorem) {
    case "cyclic_quadrilateral":
      // Q and S stand 2g apart on the arc through R, so the angle at P is g and the angle at R is 180 - g.
      return { points: { O, P: onCircle(200), Q: onCircle(20 + given), R: onCircle(20), S: onCircle(20 - given) }, lines: [["P", "Q"], ["Q", "R"], ["R", "S"], ["S", "P"]], given: ["Q", "P", "S"], derived: ["S", "R", "Q"] };
    case "same_segment":
      return { points: { O, A: onCircle(-90 - given), B: onCircle(-90 + given), P: onCircle(110), Q: onCircle(70) }, lines: [["A", "P"], ["B", "P"], ["A", "Q"], ["B", "Q"]], given: ["A", "P", "B"], derived: ["A", "Q", "B"] };
    case "semicircle":
      return { points: { O, A: onCircle(180), B: onCircle(0), P: onCircle(given < 90 ? 2 * given : 120) }, lines: [["A", "B"], ["A", "P"], ["P", "B"]], given: ["P", "A", "B"], derived: ["A", "P", "B"] };
    case "tangent_radius": {
      const P = onCircle(-90);
      return { points: { O, P, T: [P[0] + 140, P[1]] }, lines: [["O", "P"]], given: ["O", "P", "T"], derived: ["O", "P", "T"], tangent: [[P[0] - 160, P[1]], [P[0] + 160, P[1]]] };
    }
    case "alternate_segment": {
      const T = onCircle(-90);
      const A = onCircle(-90 + 2 * given);
      return { points: { O, T, A, B: onCircle(150) }, lines: [["T", "A"], ["T", "B"], ["A", "B"]], given: ["A", "T", "X"], derived: ["T", "B", "A"], tangent: [[T[0] - 160, T[1]], [T[0] + 160, T[1]]] };
    }
    case "isosceles_radii":
      return { points: { O, A: onCircle(-90 - given / 2), B: onCircle(-90 + given / 2) }, lines: [["O", "A"], ["O", "B"], ["A", "B"]], given: ["A", "O", "B"], derived: ["O", "A", "B"] };
    case "angle_at_circumference":
    case "angle_at_centre":
    default: {
      const atCirc = theorem === "angle_at_circumference" ? derived : given;
      return {
        points: { O, A: onCircle(-90 - atCirc), B: onCircle(-90 + atCirc), P: onCircle(90) },
        lines: [["O", "A"], ["O", "B"], ["P", "A"], ["P", "B"]],
        given: theorem === "angle_at_circumference" ? ["A", "O", "B"] : ["A", "P", "B"],
        derived: theorem === "angle_at_circumference" ? ["A", "P", "B"] : ["A", "O", "B"],
      };
    }
  }
}

function drawTheorem(s: Scene, p: GeometryParams): ReactElement {
  const c = computed<{ given?: number; derived?: number }>(s.spec);
  if (c.given === undefined || c.derived === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const theorem = p.theorem ?? "angle_at_centre";
  const layout = theoremLayout(theorem, c.given, c.derived);
  const pts = layout.points;
  const pick = (k: string): Pt => (k === "X" && layout.tangent !== undefined ? layout.tangent[1] : (pts[k] ?? [CX, CY]));
  const angle = (id: "angle_given" | "angle_derived", tri: [string, string, string]) => {
    const { d, label } = angleAt(pick(tri[1]), pick(tri[0]), pick(tri[2]), 22);
    return (
      <Part key={id} s={s} id={id}>
        <path d={d} fill="none" {...lineStyle(s, id, INK, 1)} />
        {labelled(p, id) && <Label s={s} part={id} x={label[0]} y={label[1]} text={`${computedLabel(s.spec, id)}°`} anchor="middle" size={11} />}
      </Part>
    );
  };
  return (
    <Frame s={s} width={W} height={H}>
      <Part s={s} id="circle" focusable={false}><circle cx={CX} cy={CY} r={R} fill="none" stroke={INK} strokeWidth={STROKE} /></Part>
      {layout.tangent !== undefined && (
        <Part s={s} id="tangent"><line x1={layout.tangent[0][0]} y1={layout.tangent[0][1]} x2={layout.tangent[1][0]} y2={layout.tangent[1][1]} {...lineStyle(s, "tangent")} /></Part>
      )}
      {layout.lines.map(([a, b]) => <line key={`${a}${b}`} x1={pick(a)[0]} y1={pick(a)[1]} x2={pick(b)[0]} y2={pick(b)[1]} stroke={INK} strokeWidth={STROKE} />)}
      {angle("angle_given", layout.given)}
      {angle("angle_derived", layout.derived)}
      {Object.entries(pts).map(([name, at]) => (
        <Part key={name} s={s} id={name}>
          <circle cx={at[0]} cy={at[1]} r={3} fill={INK} />
          <Label s={s} part={name} x={at[0] + (at[0] >= CX ? 8 : -8)} y={at[1] + (at[1] >= CY ? 16 : -8)} text={name} anchor={at[0] >= CX ? "start" : "end"} />
        </Part>
      ))}
    </Frame>
  );
}

function drawCircle(s: Scene, p: GeometryParams): ReactElement {
  const fig = computed<{ figure?: Record<"O" | "P" | "Q", Pt> }>(s.spec).figure;
  const theta = fig?.Q !== undefined ? Math.atan2(fig.Q[1], fig.Q[0]) : Math.PI / 3;
  const P = onCircle(0);
  const Q = onCircle(theta / DEG);
  return (
    <Frame s={s} width={W} height={H}>
      <Part s={s} id="circle"><circle cx={CX} cy={CY} r={R} fill="none" {...lineStyle(s, "circle")} /></Part>
      <Part s={s} id="sector"><path d={`M${CX},${CY} L${P[0]},${P[1]} A${R},${R} 0 ${theta > Math.PI ? 1 : 0} 0 ${Q[0]},${Q[1]} Z`} fill={areaFill(s, "sector", TINT)} fillOpacity={0.5} stroke="none" />
        {labelled(p, "sector") && <Label s={s} part="sector" x={CX + 30} y={CY - 10} text={`${computedLabel(s.spec, "sector")}°`} size={11} />}</Part>
      <Part s={s} id="radius"><line x1={CX} y1={CY} x2={P[0]} y2={P[1]} {...lineStyle(s, "radius")} />
        {labelled(p, "radius") && <Label s={s} part="radius" x={(CX + P[0]) / 2} y={CY + 16} text={computedLabel(s.spec, "radius", "r")} anchor="middle" />}</Part>
      <Part s={s} id="chord"><line x1={P[0]} y1={P[1]} x2={Q[0]} y2={Q[1]} {...lineStyle(s, "chord", INK, STROKE)} strokeDasharray="5 3" />
        {labelled(p, "chord") && <Label s={s} part="chord" x={(P[0] + Q[0]) / 2 + 8} y={(P[1] + Q[1]) / 2} text={computedLabel(s.spec, "chord")} />}</Part>
      <Part s={s} id="arc"><path d={`M${P[0]},${P[1]} A${R},${R} 0 ${theta > Math.PI ? 1 : 0} 0 ${Q[0]},${Q[1]}`} fill="none" {...lineStyle(s, "arc", INK, 3)} />
        {labelled(p, "arc") && <Label s={s} part="arc" x={onCircle(theta / DEG / 2)[0] + 10} y={onCircle(theta / DEG / 2)[1]} text={computedLabel(s.spec, "arc")} />}</Part>
      <Part s={s} id="O"><circle cx={CX} cy={CY} r={3} fill={INK} /><Label s={s} part="O" x={CX - 8} y={CY + 16} text="O" anchor="end" /></Part>
    </Frame>
  );
}

/** Two parallel lines and a transversal: angle letters a to h, equal pairs marked alike, never derived figures. */
function drawParallel(s: Scene, p: GeometryParams): ReactElement {
  const given = Number(p.givens?.angle ?? 60);
  const theta = (Number.isFinite(given) && given > 0 && given < 180 ? given : 60) * DEG;
  const y1 = 110;
  const y2 = 220;
  const mx = 240;
  const dx = (y2 - y1) / Math.tan(theta);
  const I1: Pt = [mx + dx / 2, y1];
  const I2: Pt = [mx - dx / 2, y2];
  const ext = 90;
  const ux = Math.cos(theta);
  const uy = Math.sin(theta);
  const t0: Pt = [I1[0] + ext * ux, I1[1] - ext * uy];
  const t1: Pt = [I2[0] - ext * ux, I2[1] + ext * uy];
  // Angles at each crossing (SVG angles, y down): a/e upper right, b/f upper left, c/g lower left, d/h lower right.
  const spans: [number, number][] = [[-theta, 0], [-Math.PI, -theta], [Math.PI - theta, Math.PI], [0, Math.PI - theta]];
  const pairs = new Set(p.pairs ?? ["corresponding", "alternate"]);
  const marked = new Map<string, number>();
  if (pairs.has("corresponding")) for (const k of ["a", "e"]) marked.set(k, 1);
  if (pairs.has("alternate")) for (const k of ["c", "e"]) marked.set(k, marked.get(k) ?? 2);
  if (pairs.has("vertically_opposite")) for (const k of ["a", "c"]) marked.set(k, marked.get(k) ?? 1);
  if (pairs.has("co_interior")) for (const k of ["d", "e"]) if (!marked.has(k)) marked.set(k, 3);
  const letter = (k: string, at: Pt, span: [number, number], n: number) => {
    const mid = (span[0] + span[1]) / 2;
    const ring = marked.get(k);
    return (
      <Part key={k} s={s} id={k}>
        <path d={arc(at, span[0], span[1], 20 + n)} fill="none" {...lineStyle(s, k, ring === undefined ? MUTED : SERIES[(ring - 1) % SERIES.length], ring === undefined ? 1 : 1.5)} {...(ring === 3 ? { strokeDasharray: "3 2" } : {})} />
        <Label s={s} part={k} x={at[0] + 36 * Math.cos(mid)} y={at[1] + 36 * Math.sin(mid) + 4} text={k === "a" && p.givens?.angle !== undefined ? `a = ${String(p.givens.angle)}°` : k} anchor="middle" size={12} />
      </Part>
    );
  };
  return (
    <Frame s={s} width={W} height={H}>
      <Part s={s} id="line_1"><line x1={40} x2={W - 40} y1={y1} y2={y1} {...lineStyle(s, "line_1")} /><path d={`M${W - 70},${y1 - 5} l8,5 l-8,5`} fill="none" stroke={INK} strokeWidth={STROKE} /></Part>
      <Part s={s} id="line_2"><line x1={40} x2={W - 40} y1={y2} y2={y2} {...lineStyle(s, "line_2")} /><path d={`M${W - 70},${y2 - 5} l8,5 l-8,5`} fill="none" stroke={INK} strokeWidth={STROKE} /></Part>
      <Part s={s} id="transversal"><line x1={t0[0]} y1={t0[1]} x2={t1[0]} y2={t1[1]} {...lineStyle(s, "transversal")} /></Part>
      {["a", "b", "c", "d"].map((k, i) => letter(k, I1, must(spans[i]), 0))}
      {["e", "f", "g", "h"].map((k, i) => letter(k, I2, must(spans[i]), 0))}
    </Frame>
  );
}

function drawGeometry(s: Scene): ReactElement {
  const p = params<GeometryParams>(s.spec);
  if (p.figure === "triangle") return drawTriangle(s, p);
  if (p.figure === "circle") return drawCircle(s, p);
  if (p.figure === "parallel_lines") return drawParallel(s, p);
  return drawTheorem(s, p);
}

export const geometryFigure = define({ kind: "geometry_figure", schema: geometrySchema, draw: drawGeometry });

// --- unit_circle ----------------------------------------------------------------------------------------------------

function drawUnitCircle(s: Scene): ReactElement {
  const p = params<{ angle_deg: number; show?: string[] }>(s.spec);
  const show = p.show ?? ["sin", "cos"];
  const th = p.angle_deg * DEG;
  const O: Pt = [CX, CY];
  const P: Pt = [CX + R * Math.cos(th), CY - R * Math.sin(th)];
  const foot: Pt = [P[0], CY];
  const tanOk = Math.abs(Math.cos(th)) > 1e-9 && show.includes("tan");
  const T: Pt = [CX + R, CY - R * Math.tan(th)];
  const quadrant = [[CX + 60, CY - 60], [CX - 60, CY - 60], [CX - 60, CY + 70], [CX + 60, CY + 70]] as Pt[];
  return (
    <Frame s={s} width={W} height={H}>
      <line x1={CX - R - 20} x2={CX + R + 20} y1={CY} y2={CY} stroke={LINE} strokeWidth={1} />
      <line x1={CX} x2={CX} y1={CY - R - 20} y2={CY + R + 20} stroke={LINE} strokeWidth={1} />
      <Part s={s} id="circle" focusable={false}><circle cx={CX} cy={CY} r={R} fill="none" stroke={INK} strokeWidth={STROKE} /></Part>
      {show.includes("quadrant_signs") && quadrant.map((at, i) => (
        <Part key={i} s={s} id={`q${i + 1}`}><Label s={s} part={`q${i + 1}`} x={at[0]} y={at[1]} text={s.t(`visual.unit_circle.q${i + 1}`)} anchor="middle" size={11} /></Part>
      ))}
      {show.includes("cos") && (
        <Part s={s} id="leg_cos"><line x1={O[0]} y1={O[1]} x2={foot[0]} y2={foot[1]} {...lineStyle(s, "leg_cos", SERIES[1], 2.5)} />
          <Label s={s} part="leg_cos" x={(O[0] + foot[0]) / 2} y={CY + 18} text={`cos θ = ${computedLabel(s.spec, "leg_cos")}`} anchor="middle" size={11} /></Part>
      )}
      {show.includes("sin") && (
        <Part s={s} id="leg_sin"><line x1={foot[0]} y1={foot[1]} x2={P[0]} y2={P[1]} {...lineStyle(s, "leg_sin", SERIES[0], 2.5)} />
          <Label s={s} part="leg_sin" x={foot[0] + (P[0] >= CX ? 8 : -8)} y={(foot[1] + P[1]) / 2} text={`sin θ = ${computedLabel(s.spec, "leg_sin")}`} anchor={P[0] >= CX ? "start" : "end"} size={11} /></Part>
      )}
      {tanOk && Math.abs(T[1] - CY) < 250 && (
        <Part s={s} id="leg_tan"><line x1={CX + R} y1={CY} x2={T[0]} y2={T[1]} {...lineStyle(s, "leg_tan", SERIES[2], 2.5)} strokeDasharray="5 3" />
          <Label s={s} part="leg_tan" x={T[0] + 6} y={T[1]} text={`tan θ = ${computedLabel(s.spec, "leg_tan")}`} size={11} /></Part>
      )}
      <line x1={O[0]} y1={O[1]} x2={P[0]} y2={P[1]} stroke={INK} strokeWidth={STROKE} />
      <Part s={s} id="angle"><path d={arc(O, -th, 0, 26)} fill="none" {...lineStyle(s, "angle", INK, 1)} />
        <Label s={s} part="angle" x={CX + 34 * Math.cos(-th / 2)} y={CY + 34 * Math.sin(-th / 2) + 4} text={`θ = ${p.angle_deg}°`} size={11} /></Part>
      <Part s={s} id="P"><circle cx={P[0]} cy={P[1]} r={4} fill={INK} /><Label s={s} part="P" x={P[0] + 8} y={P[1] - 8} text="P" /></Part>
    </Frame>
  );
}

export const unitCircle = define({ kind: "unit_circle", schema: unitCircleSchema, draw: drawUnitCircle });

// --- venn_diagram ---------------------------------------------------------------------------------------------------

interface VennParams {
  sets: { id: string; label: string }[];
  regions: { id: string; value?: string; elements?: string[] }[];
  universal_label?: string;
}

const VENN2: Record<string, Pt> = { A: [170, 160], AB: [240, 160], B: [310, 160], none: [430, 290] };
const VENN3: Record<string, Pt> = { A: [180, 125], B: [300, 125], C: [240, 235], AB: [240, 110], AC: [205, 185], BC: [275, 185], ABC: [240, 160], none: [440, 300] };
const CIRCLES2: Record<string, Pt> = { A: [200, 160], B: [280, 160] };
const CIRCLES3: Record<string, Pt> = { A: [205, 140], B: [275, 140], C: [240, 200] };

function drawVenn(s: Scene): ReactElement {
  const p = params<VennParams>(s.spec);
  const three = p.sets.length === 3;
  const circles = three ? CIRCLES3 : CIRCLES2;
  const spots = three ? VENN3 : VENN2;
  return (
    <Frame s={s} width={W} height={H}>
      <rect x={20} y={20} width={W - 40} height={H - 40} fill="none" stroke={INK} strokeWidth={STROKE} />
      {p.universal_label !== undefined && <Label s={s} x={30} y={40} text={p.universal_label} />}
      {p.sets.map((set, i) => {
        const at = circles[set.id] ?? [240, 160];
        const id = `set_${set.id}`;
        return (
          <Part key={id} s={s} id={id}>
            <circle cx={at[0]} cy={at[1]} r={three ? 85 : 100} fill={highlighted(s, id) ? TINT : "none"} fillOpacity={0.4} {...lineStyle(s, id, SERIES[i % SERIES.length], 2)} />
            <Label s={s} part={id} x={at[0] + (set.id === "B" ? 70 : set.id === "C" ? 0 : -70)} y={set.id === "C" ? at[1] + 105 : at[1] - (three ? 90 : 105)} text={set.label} anchor="middle" />
          </Part>
        );
      })}
      {p.regions.map((region) => {
        const id = `region_${region.id}`;
        const at = spots[region.id] ?? [240, 160];
        const text = region.value ?? (region.elements ?? []).join(", ");
        return (
          <Part key={id} s={s} id={id}>
            <circle cx={at[0]} cy={at[1] - 4} r={14} fill={highlighted(s, id) ? TINT : "transparent"} stroke={highlighted(s, id) ? RING : "none"} strokeWidth={2} />
            <Label s={s} part={id} x={at[0]} y={at[1]} text={text} anchor="middle" size={12} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const vennDiagram = define({
  kind: "venn_diagram", schema: vennSchema, draw: drawVenn,
  table: (spec) => ({ columns: ["visual.table.region", "visual.table.value"], rows: params<VennParams>(spec).regions.map((r) => [r.id, r.value ?? (r.elements ?? []).join(", ")]) }),
});

// --- bearing_diagram ------------------------------------------------------------------------------------------------

function drawBearing(s: Scene): ReactElement {
  const p = params<{ from: string; to: string; distance: string; show?: string[] }>(s.spec);
  const fig = computed<{ figure?: { P: Pt; Q: Pt } }>(s.spec).figure;
  if (fig === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const show = new Set([...(p.show ?? []), ...s.state.added.map((a) => a.element)]);
  const map = fit([fig.P, fig.Q, [fig.P[0], fig.P[1] + 1], [fig.Q[0], fig.Q[1] + 1]], { left: 100, right: W - 100, top: 70, bottom: H - 50 });
  const A = map(fig.P);
  const B = map(fig.Q);
  const north = (at: Pt, id: string) => (
    <Part s={s} id={id} focusable={false}><Arrow x1={at[0]} y1={at[1]} x2={at[0]} y2={at[1] - 60} width={1.5} /><text x={at[0]} y={at[1] - 66} textAnchor="middle" fontSize={12 * s.scale} fill={INK}>N</text></Part>
  );
  const turn = (Math.atan2(B[0] - A[0], -(B[1] - A[1])) + Math.PI * 2) % (Math.PI * 2);
  const clockwise = `M${A[0]},${A[1] - 30} A30,30 0 ${turn > Math.PI ? 1 : 0} 1 ${(A[0] + 30 * Math.sin(turn)).toFixed(2)},${(A[1] - 30 * Math.cos(turn)).toFixed(2)}`;
  const back = (turn + Math.PI) % (Math.PI * 2);
  const backArc = `M${B[0]},${B[1] - 30} A30,30 0 ${back > Math.PI ? 1 : 0} 1 ${(B[0] + 30 * Math.sin(back)).toFixed(2)},${(B[1] - 30 * Math.cos(back)).toFixed(2)}`;
  return (
    <Frame s={s} width={W} height={H}>
      {north(A, `north_${p.from}`)}
      {show.has("back_bearing") && north(B, `north_${p.to}`)}
      <Part s={s} id={p.from}><circle cx={A[0]} cy={A[1]} r={4} fill={INK} /><Label s={s} part={p.from} x={A[0] - 10} y={A[1] + 16} text={p.from} anchor="end" /></Part>
      <Part s={s} id={p.to}><circle cx={B[0]} cy={B[1]} r={4} fill={INK} /><Label s={s} part={p.to} x={B[0] + 10} y={B[1] + 16} text={p.to} /></Part>
      <Part s={s} id="path"><line x1={A[0]} y1={A[1]} x2={B[0]} y2={B[1]} {...lineStyle(s, "path")} />
        <Label s={s} part="path" x={(A[0] + B[0]) / 2 + 10} y={(A[1] + B[1]) / 2 + 16} text={p.distance} /></Part>
      <Part s={s} id="bearing"><path d={clockwise} fill="none" {...lineStyle(s, "bearing", INK, 1)} />
        <Label s={s} part="bearing" x={A[0] + 36} y={A[1] - 34} text={`${computedLabel(s.spec, "bearing")}°`} size={12} /></Part>
      {show.has("back_bearing") && (
        <Part s={s} id="back_bearing"><path d={backArc} fill="none" {...lineStyle(s, "back_bearing", INK, 1)} />
          <Label s={s} part="back_bearing" x={B[0] - 36} y={B[1] - 34} text={`${computedLabel(s.spec, "back_bearing")}°`} anchor="end" size={12} /></Part>
      )}
      {show.has("components") && (
        <>
          <Part s={s} id="east"><line x1={A[0]} y1={B[1]} x2={B[0]} y2={B[1]} {...lineStyle(s, "east", MUTED, 1)} strokeDasharray="4 3" />
            <Label s={s} part="east" x={(A[0] + B[0]) / 2} y={B[1] - 6} text={computedLabel(s.spec, "east")} anchor="middle" size={11} /></Part>
          <Part s={s} id="north"><line x1={A[0]} y1={A[1]} x2={A[0]} y2={B[1]} {...lineStyle(s, "north", MUTED, 1)} strokeDasharray="4 3" />
            <Label s={s} part="north" x={A[0] - 6} y={(A[1] + B[1]) / 2} text={computedLabel(s.spec, "north")} anchor="end" size={11} /></Part>
        </>
      )}
    </Frame>
  );
}

export const bearingDiagram = define({ kind: "bearing_diagram", schema: bearingSchema, draw: drawBearing });

// --- probability_tree -----------------------------------------------------------------------------------------------

interface Branch {
  id: string;
  label: string;
  p: string;
  then?: Branch[];
}

interface TreeNode {
  path: string[];
  branch: Branch;
  at: Pt;
  from: Pt;
}

/** Leaves take one row each in order; a node sits level with the middle of its children; columns by depth. */
function layoutTree(branches: Branch[], outcomes: boolean): { nodes: TreeNode[]; leaves: TreeNode[] } {
  const depth = (bs: Branch[]): number => 1 + Math.max(0, ...bs.map((b) => (b.then ? depth(b.then) : 0)));
  const leavesOf = (bs: Branch[]): number => bs.reduce((n, b) => n + (b.then ? leavesOf(b.then) : 1), 0);
  const colW = (W - (outcomes ? 170 : 70)) / depth(branches);
  const rowH = (H - 30) / Math.max(1, leavesOf(branches));
  const nodes: TreeNode[] = [];
  let row = 0;
  const place = (bs: Branch[], from: Pt, level: number, prefix: string[]): number[] => {
    const ys: number[] = [];
    for (const b of bs) {
      const path = [...prefix, b.id];
      const index = nodes.length;
      nodes.push({ path, branch: b, at: [0, 0], from });
      let y: number;
      if (b.then) {
        const child = place(b.then, [0, 0], level + 1, path);
        y = (Math.min(...child) + Math.max(...child)) / 2;
      } else {
        y = 15 + (row + 0.5) * rowH;
        row += 1;
      }
      const at: Pt = [30 + (level + 1) * colW, y];
      nodes[index] = { path, branch: b, at, from };
      for (const n of nodes) if (n.path.length === path.length + 1 && n.path.slice(0, -1).join("_") === path.join("_")) n.from = [at[0] + 44, at[1]];
      ys.push(y);
    }
    return ys;
  };
  const top = place(branches, [0, 0], 0, []);
  const root: Pt = [30, (Math.min(...top) + Math.max(...top)) / 2];
  for (const n of nodes) if (n.path.length === 1) n.from = root;
  return { nodes, leaves: nodes.filter((n) => n.branch.then === undefined) };
}

function drawTree(s: Scene): ReactElement {
  const p = params<{ branches: Branch[]; show_outcomes?: boolean }>(s.spec);
  const outcomes = p.show_outcomes !== false;
  const { nodes, leaves } = layoutTree(p.branches, outcomes);
  return (
    <Frame s={s} width={W} height={H}>
      {nodes.map(({ path, branch, at, from }) => {
        const id = `b_${path.join("_")}`;
        return (
          <Part key={id} s={s} id={id}>
            <line x1={from[0]} y1={from[1]} x2={at[0]} y2={at[1]} {...lineStyle(s, id)} />
            <Label s={s} part={id} x={(from[0] + at[0]) / 2} y={(from[1] + at[1]) / 2 - 6} text={branch.p} anchor="middle" size={11} />
            <Label s={s} x={at[0] + 4} y={at[1] + 4} text={branch.label} size={12} />
          </Part>
        );
      })}
      {outcomes && leaves.map(({ path, at }) => {
        const id = `o_${path.join("_")}`;
        return (
          <Part key={id} s={s} id={id}>
            <Label s={s} part={id} x={W - 150} y={at[1] + 4} text={`${path.join(", ")}: ${computedLabel(s.spec, id)}`} size={11} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const probabilityTree = define({ kind: "probability_tree", schema: treeSchema, draw: drawTree });

// --- transformation_grid --------------------------------------------------------------------------------------------

interface TransformationParams {
  object: { label: string; x: number; y: number }[];
  transformation: { kind: string; vector?: [number, number]; line?: string; centre?: [number, number] };
  grid?: [number, number];
}

function drawTransformation(s: Scene): ReactElement {
  const p = params<TransformationParams>(s.spec);
  const image = computed<{ image?: Pt[] }>(s.spec).image;
  if (image === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const [g0, g1] = p.grid ?? [-6, 6];
  const size = Math.min(W, H) - 40;
  const x = linear(g0, g1, (W - size) / 2, (W + size) / 2);
  const y = linear(g0, g1, (H + size) / 2, (H - size) / 2);
  const grid: ReactElement[] = [];
  for (let v = g0; v <= g1; v += 1) {
    grid.push(<line key={`x${v}`} x1={x(v)} x2={x(v)} y1={y(g0)} y2={y(g1)} stroke={v === 0 ? STRONG : LINE} strokeWidth={v === 0 ? STROKE : 1} />);
    grid.push(<line key={`y${v}`} x1={x(g0)} x2={x(g1)} y1={y(v)} y2={y(v)} stroke={v === 0 ? STRONG : LINE} strokeWidth={v === 0 ? STROKE : 1} />);
  }
  const obj = p.object.map((q) => [q.x, q.y] as Pt);
  const poly = (pts: Pt[]) => pts.map(([a, b]) => `${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ");
  const kind = p.transformation.kind;
  const extraId = kind === "reflection" ? "mirror" : kind === "translation" ? "vector" : "centre";
  const line = p.transformation.line ?? "y = 0";
  const k = Number(/-?\d+/.exec(line.split("=")[1] ?? "0")?.[0] ?? 0);
  const mirror = (): ReactElement => {
    if (line.startsWith("x")) return <line x1={x(k)} x2={x(k)} y1={y(g0)} y2={y(g1)} {...lineStyle(s, "mirror", SERIES[3], 2)} strokeDasharray="6 4" />;
    if (line === "y = x") return <line x1={x(g0)} x2={x(g1)} y1={y(g0)} y2={y(g1)} {...lineStyle(s, "mirror", SERIES[3], 2)} strokeDasharray="6 4" />;
    if (line === "y = -x") return <line x1={x(g0)} x2={x(g1)} y1={y(-g0)} y2={y(-g1)} {...lineStyle(s, "mirror", SERIES[3], 2)} strokeDasharray="6 4" />;
    return <line x1={x(g0)} x2={x(g1)} y1={y(k)} y2={y(k)} {...lineStyle(s, "mirror", SERIES[3], 2)} strokeDasharray="6 4" />;
  };
  const c = p.transformation.centre ?? [0, 0];
  const v = p.transformation.vector ?? [0, 0];
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">{grid}</g>
      <Part s={s} id="object"><polygon points={poly(obj)} fill={areaFill(s, "object", PAPER)} fillOpacity={0.6} {...lineStyle(s, "object", SERIES[0], 2)} /></Part>
      <Part s={s} id="image"><polygon points={poly(image)} fill={areaFill(s, "image", PAPER)} fillOpacity={0.6} {...lineStyle(s, "image", SERIES[1], 2)} strokeDasharray="5 3" /></Part>
      <Part s={s} id={extraId}>
        {kind === "reflection" ? mirror() : kind === "translation" ? <Arrow x1={x(must(obj[0])[0])} y1={y(must(obj[0])[1])} x2={x(must(obj[0])[0] + v[0])} y2={y(must(obj[0])[1] + v[1])} stroke={highlighted(s, "vector") ? RING : INK} /> : <circle cx={x(c[0])} cy={y(c[1])} r={5} fill={highlighted(s, "centre") ? RING : INK} />}
      </Part>
      {p.object.map((q, i) => (
        <Part key={`o${q.label}`} s={s} id={`object.${q.label}`}>
          <circle cx={x(q.x)} cy={y(q.y)} r={3} fill={SERIES[0]} />
          <Label s={s} part={`object.${q.label}`} x={x(q.x) - 6} y={y(q.y) - 6} text={q.label} anchor="end" size={12} />
          {void i}
        </Part>
      ))}
      {p.object.map((q, i) => (
        <Part key={`i${q.label}`} s={s} id={`image.${q.label}`}>
          <circle cx={x(must(image[i])[0])} cy={y(must(image[i])[1])} r={3} fill={SERIES[1]} />
          <Label s={s} part={`image.${q.label}`} x={x(must(image[i])[0]) + 6} y={y(must(image[i])[1]) - 6} text={`${q.label}'`} size={12} />
        </Part>
      ))}
    </Frame>
  );
}

export const transformationGrid = define({ kind: "transformation_grid", schema: transformationSchema, draw: drawTransformation });

export { arc, angleAt, fit, STRONG as STRONG_LINE };
