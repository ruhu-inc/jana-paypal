// The S2 simulations (VSL-08, VSL-30 to VSL-32): projectile, circuit and titration. Each runs entirely in the
// client with no network and no model; the Coach starts one with a spec and may read the readings the student
// records, but cannot drive it continuously. Starting readouts come from the resolution's tool call; the live model is
// cross-checked against the same tools in CI (vsl31_sim_matches_tools).
import { must } from "../must.js";
import type { ReactElement } from "react";
import type { VisualProps } from "../contract.js";
import { define } from "../define.js";
import { CLIENT_KEYS } from "../generated/keys.js";
import { INK, LINE, MUTED, PAPER, SERIES, STRONG, keyOnly } from "../kit.js";
import projectileSchema from "../schemas/projectile_sim.schema.json" with { type: "json" };
import circuitSimSchema from "../schemas/circuit_sim.schema.json" with { type: "json" };
import titrationSimSchema from "../schemas/titration_sim.schema.json" with { type: "json" };
import { circuitSim, endPoint, indicatorColour, num, projectile, sig, titrationSim, type Values } from "./models.js";
import { scenarioOf } from "./predict.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;

function scene(props: VisualProps): { start: Values; locked: string[] } {
  const p = props.spec.params as Record<string, unknown>;
  return { start: {}, locked: (p.locked as string[] | undefined) ?? [] };
}

// --- projectile_sim -------------------------------------------------------------------------------------------------

function ProjectileSim(props: VisualProps): ReactElement {
  const p = props.spec.params as { mode: "horizontal" | "vertical" | "angled"; speed: number; angle?: number; height?: number };
  const g = (props.spec.computed as { g?: number }).g ?? NaN;
  const model = projectile(g);
  const start: Values = { mode: p.mode, speed: p.speed, angle: p.angle ?? 45, height: p.height ?? (p.mode === "horizontal" ? 20 : 0) };
  const t = props.t ?? keyOnly;
  return (
    <SimView model={model} props={props} start={start} locked={scene(props).locked} animated scenario={scenarioOf("projectile_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      const table = model.table(values);
      const xmax = Math.max(10, must(r["out.range"]) * 1.1);
      const ymax = Math.max(10, must(r["out.max_height"]) * 1.15);
      const x = (v: number) => 40 + (v / xmax) * (W - 60);
      const y = (v: number) => H - 30 - (v / ymax) * (H - 50);
      const path = table.rows.map(([, px, py]) => `${x(must(px)).toFixed(2)},${y(must(py)).toFixed(2)}`).join(" ");
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.projectile.drawing")}>
          <line x1={20} x2={W - 10} y1={y(0)} y2={y(0)} stroke={STRONG} strokeWidth={1.5} />
          <rect x={x(0) - 10} y={y(num(values, "height"))} width={10} height={y(0) - y(num(values, "height"))} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          <polyline points={path} fill="none" stroke={SERIES[0]} strokeWidth={1.5} strokeDasharray="5 4" />
          <circle cx={x(must(r["out.x"]))} cy={y(must(r["out.y"]))} r={7} fill={SERIES[1]} stroke={INK} strokeWidth={1} />
        </svg>
      );
    }} />
  );
}

export const projectileSim = define({ kind: "projectile_sim", schema: projectileSchema, labelModes: ["shown"], draw: (_s, props) => <ProjectileSim {...props} /> });

// --- circuit_sim ----------------------------------------------------------------------------------------------------

function CircuitSim(props: VisualProps): ReactElement {
  const p = props.spec.params as { emf: number; resistors: { id: string; resistance: number }[]; arrangement: "series" | "parallel"; switch_closed?: boolean };
  const ids = p.resistors.map((r) => r.id);
  const model = circuitSim(ids);
  const start: Values = { emf: p.emf, arrangement: p.arrangement, switch: p.switch_closed !== false, ...Object.fromEntries(p.resistors.map((r) => [r.id, r.resistance])) };
  const t = props.t ?? keyOnly;
  return (
    <SimView model={model} props={props} start={start} locked={scene(props).locked.map((k) => (k === "switch" ? "switch" : k))} animated scenario={scenarioOf("circuit_sim", props.spec.params)} draw={(state, values) => {
      const parallel = values.arrangement === "parallel";
      const closed = values.switch !== false;
      const n = ids.length;
      const loop = `M60,60 L420,60 L420,200 L60,200 Z`;
      const dots = Array.from({ length: 12 }, (_, i) => {
        const u = (i / 12 + (closed ? state.phase : 0)) % 1;
        const perimeter = 2 * (360 + 140);
        let d = u * perimeter;
        const pts: [number, number][] = [[60, 60], [420, 60], [420, 200], [60, 200], [60, 60]];
        for (let k = 0; k < 4; k += 1) {
          const [a, b] = [must(pts[k]), must(pts[k + 1])];
          const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
          if (d <= len) return [a[0] + ((b[0] - a[0]) * d) / len, a[1] + ((b[1] - a[1]) * d) / len] as [number, number];
          d -= len;
        }
        return [60, 60] as [number, number];
      });
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.circuit.drawing")}>
          <path d={loop} fill="none" stroke={INK} strokeWidth={1.5} />
          <rect x={180} y={190} width={40} height={20} fill={PAPER} stroke="none" />
          <line x1={194} x2={194} y1={185} y2={215} stroke={INK} strokeWidth={1.5} />
          <line x1={206} x2={206} y1={192} y2={208} stroke={INK} strokeWidth={3} />
          <rect x={280} y={190} width={50} height={20} fill={PAPER} stroke="none" />
          <circle cx={285} cy={200} r={3} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          <circle cx={325} cy={200} r={3} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          <line x1={285} y1={200} x2={closed ? 325 : 318} y2={closed ? 200 : 186} stroke={INK} strokeWidth={1.5} />
          <circle cx={100} cy={130} r={14} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          <text x={100} y={135} textAnchor="middle" fontSize={13} fill="var(--color-ink)">A</text>
          {ids.map((id, i) => {
            // In parallel each resistor is its own branch between the junctions at x = 150 and x = 330, one below
            // the other, so the paths read as separate; in series they sit one after another on the top wire.
            const cx = parallel ? 240 : 140 + ((i + 1) * 220) / (n + 1);
            const cy = parallel ? 60 + i * 45 : 60;
            return (
              <g key={id}>
                {parallel && i > 0 && <path d={`M150,60 L150,${cy} L330,${cy} L330,60`} fill="none" stroke={INK} strokeWidth={1.5} />}
                <rect x={cx - 18} y={cy - 8} width={36} height={16} fill={PAPER} stroke={INK} strokeWidth={1.5} />
                <text x={cx} y={cy - 12} textAnchor="middle" fontSize={11} fill={MUTED}>{`${id} ${sig(num(values, id), 3)} Ω`}</text>
              </g>
            );
          })}
          {parallel && n > 1 && [150, 330].map((x) => <circle key={x} cx={x} cy={60} r={3} fill={INK} />)}
          {dots.map(([dx, dy], i) => <circle key={i} cx={dx} cy={dy} r={2.5} fill={closed ? SERIES[0] : LINE} />)}
        </svg>
      );
    }} />
  );
}

export const circuitSimulation = define({ kind: "circuit_sim", schema: circuitSimSchema, labelModes: ["shown"], draw: (_s, props) => <CircuitSim {...props} /> });

// --- titration_sim --------------------------------------------------------------------------------------------------

function TitrationSim(props: VisualProps): ReactElement {
  const p = props.spec.params as { acid: { formula: string; concentration: number }; base: { formula: string; concentration: number }; acid_volume: number; indicator: string };
  const model = titrationSim(CLIENT_KEYS.sim_titration_rate_cm3_s);
  const start: Values = { volume: 0, tap: false, indicator: p.indicator, acid_concentration: p.acid.concentration, base_concentration: p.base.concentration, acid_volume: p.acid_volume };
  const t = props.t ?? keyOnly;
  return (
    <SimView model={{ ...model, params: model.params.map((q) => (q.id === "volume" ? { ...q, max: Number((2 * endPoint(start)).toFixed(1)) } : q)) }} props={props} start={start} locked={scene(props).locked} animated scenario={scenarioOf("titration_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      const colour = indicatorColour(String(values.indicator), must(r["out.ph"]));
      const full = 2 * endPoint(values);
      const level = 40 + (state.volume / full) * 120;
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.titration.drawing")}>
          <rect x={220} y={20} width={16} height={150} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          <rect x={221} y={level} width={14} height={170 - level} fill="var(--color-series-1)" fillOpacity={0.3} stroke="none" />
          <path d="M228,170 L228,190" stroke={INK} strokeWidth={1.5} />
          <polygon points="218,200 238,200 238,214 270,250 186,250 218,214" fill={`var(--content-${colour})`} stroke={STRONG} strokeWidth={1.5} strokeDasharray={colour === "colourless" ? "4 3" : undefined} data-content-colour={colour} />
          <text x={290} y={236} fontSize={13} fill="var(--color-ink)" data-colour-name={colour}>{t(`colour.${colour}`)}</text>
        </svg>
      );
    }} />
  );
}

export const titrationSimulation = define({ kind: "titration_sim", schema: titrationSimSchema, labelModes: ["shown"], draw: (_s, props) => <TitrationSim {...props} /> });
