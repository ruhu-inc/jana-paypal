// The online sketch (VSL-43, VSL-44, D103, D106): the vetted renderer of an engine-resolved sketch. It draws only the
// resolved elements of the closed vocabulary (line, arrow, arc, circle, polygon, point, label, axis) in the sketch's
// own frame, never markup or path data from a model, in the rough-sketch style of TABLE DSN-10a: the sketch stroke
// colour, dashed pencil-like strokes and labels in the hand font. Its accessible name is the engine's alt text, which
// names what is drawn (VSL-14). The "not checked" label or the check chip, and "Remove sketch", are the host's.
import type { ReactElement } from "react";
import type { Translate } from "./contract.js";
import { MUTED } from "./kit.js";

/** The sketch tokens (DSN-10a): stroke width and dash, mirrored from the design tokens' `sketch` group. */
export const SKETCH_STROKE = 1.75;
export const SKETCH_DASH = "5 3";
const SKETCH_INK = "var(--color-sketch-stroke, var(--color-muted))";
const W = 480;
const H = 320;
const PAD = 16;

export interface SketchFrame { visual_id: string | null; x_min: number; x_max: number; y_min: number; y_max: number }
export type SketchElement =
  | { kind: "line"; from: [number, number]; to: [number, number]; dashed?: boolean }
  | { kind: "arrow"; from: [number, number]; to: [number, number] }
  | { kind: "arc"; center: [number, number]; radius: number; start_deg: number; end_deg: number }
  | { kind: "circle"; center: [number, number]; radius: number }
  | { kind: "polygon"; points: [number, number][] }
  | { kind: "point"; at: [number, number]; name?: string }
  | { kind: "label"; at: [number, number]; text: string }
  | { kind: "axis"; axis: "x" | "y"; from: number; to: number; ticks?: number[]; title?: string };

export interface ResolvedSketch {
  sketch_id: string;
  sketch_version: 1;
  frame: SketchFrame;
  elements: SketchElement[];
  dropped: { element: number; reason: string }[];
  checks: { element: number; claim: string; result: "pass" | "fail" | "not_checkable"; check_id: string | null }[];
  status: "checked" | "not_checked";
  caption?: string;
  alt: string;
}

/** The frame's coordinates on the drawing, y upwards as a graph reads. */
function scaler(frame: SketchFrame): { x: (v: number) => number; y: (v: number) => number; r: (v: number) => number } {
  const sx = (W - 2 * PAD) / (frame.x_max - frame.x_min);
  const sy = (H - 2 * PAD) / (frame.y_max - frame.y_min);
  return { x: (v) => PAD + (v - frame.x_min) * sx, y: (v) => H - PAD - (v - frame.y_min) * sy, r: (v) => v * Math.min(sx, sy) };
}

function arcPath(cx: number, cy: number, r: number, start: number, end: number): string {
  const a0 = (-start * Math.PI) / 180, a1 = (-end * Math.PI) / 180;
  const large = Math.abs(end - start) > 180 ? 1 : 0;
  const sweep = end > start ? 0 : 1;
  return `M ${cx + r * Math.cos(a0)} ${cy + r * Math.sin(a0)} A ${r} ${r} 0 ${large} ${sweep} ${cx + r * Math.cos(a1)} ${cy + r * Math.sin(a1)}`;
}

export function SketchView({ sketch }: { sketch: ResolvedSketch; t?: Translate }): ReactElement {
  const s = scaler(sketch.frame);
  const stroke = { stroke: SKETCH_INK, strokeWidth: SKETCH_STROKE, strokeDasharray: SKETCH_DASH, strokeLinecap: "round" as const, fill: "none" };
  const marker = `skt-head-${sketch.sketch_id}`;
  const drawn = sketch.elements.map((e, i) => {
    const key = `e${i}`;
    switch (e.kind) {
      case "line":
        return <line key={key} data-sketch-element={e.kind} x1={s.x(e.from[0])} y1={s.y(e.from[1])} x2={s.x(e.to[0])} y2={s.y(e.to[1])} {...stroke} />;
      case "arrow":
        return <line key={key} data-sketch-element={e.kind} x1={s.x(e.from[0])} y1={s.y(e.from[1])} x2={s.x(e.to[0])} y2={s.y(e.to[1])} {...stroke} markerEnd={`url(#${marker})`} />;
      case "arc":
        return <path key={key} data-sketch-element={e.kind} d={arcPath(s.x(e.center[0]), s.y(e.center[1]), s.r(e.radius), e.start_deg, e.end_deg)} {...stroke} />;
      case "circle":
        return <circle key={key} data-sketch-element={e.kind} cx={s.x(e.center[0])} cy={s.y(e.center[1])} r={s.r(e.radius)} {...stroke} />;
      case "polygon":
        return <polygon key={key} data-sketch-element={e.kind} points={e.points.map(([x, y]) => `${s.x(x)},${s.y(y)}`).join(" ")} {...stroke} />;
      case "point":
        return <circle key={key} data-sketch-element={e.kind} cx={s.x(e.at[0])} cy={s.y(e.at[1])} r={3} fill={SKETCH_INK} />;
      case "label":
        return <text key={key} data-sketch-element={e.kind} x={s.x(e.at[0])} y={s.y(e.at[1])} fontSize={14} fill={SKETCH_INK} className="font-hand">{e.text}</text>;
      case "axis": {
        const horizontal = e.axis === "x";
        const at = horizontal ? s.y(Math.min(Math.max(0, sketch.frame.y_min), sketch.frame.y_max)) : s.x(Math.min(Math.max(0, sketch.frame.x_min), sketch.frame.x_max));
        const [a, b] = horizontal ? [s.x(e.from), s.x(e.to)] : [s.y(e.from), s.y(e.to)];
        return (
          <g key={key} data-sketch-element={e.kind}>
            {horizontal ? <line x1={a} y1={at} x2={b} y2={at} {...stroke} strokeDasharray="none" /> : <line x1={at} y1={a} x2={at} y2={b} {...stroke} strokeDasharray="none" />}
            {(e.ticks ?? []).map((v) => (horizontal
              ? <line key={v} x1={s.x(v)} y1={at - 3} x2={s.x(v)} y2={at + 3} stroke={SKETCH_INK} />
              : <line key={v} x1={at - 3} y1={s.y(v)} x2={at + 3} y2={s.y(v)} stroke={SKETCH_INK} />))}
            {e.title !== undefined && <text x={horizontal ? b : at + 6} y={horizontal ? at - 6 : b + 12} fontSize={13} fill={SKETCH_INK} className="font-hand" textAnchor={horizontal ? "end" : "start"}>{e.title}</text>}
          </g>
        );
      }
    }
  });
  return (
    <figure data-sketch={sketch.sketch_id} data-sketch-status={sketch.status} className="grid gap-1">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={sketch.alt}>
        <defs>
          <marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill={SKETCH_INK} />
          </marker>
        </defs>
        {drawn}
      </svg>
      {sketch.caption !== undefined && <figcaption className="font-hand text-ui" style={{ color: MUTED }}>{sketch.caption}</figcaption>}
    </figure>
  );
}
