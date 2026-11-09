// population_sim (VSL-08a, VSL-46): logistic growth towards a carrying capacity, or predator and prey (Lotka–Volterra),
// with the rates as sliders. The populations are drawn against time as the run goes on, prey solid and predators
// dashed, with the carrying capacity as a dashed line; a simplified model that shows the pattern, never a prediction.
import type { ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { DASHES, INK, MUTED, PAPER, SERIES, STRONG, keyOnly } from "../kit.js";
import { num, type SimSetup, type Values } from "./models.js";
import { POPULATION_CONTROLS, POPULATION_STEPS, population, trajectory, type PopulationSetup, type PopulationState } from "./biology.js";
import { RUN_SECONDS } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;
const BOX = { left: 40, right: 460, top: 20, bottom: 200 };

interface PopulationParams {
  model: "logistic" | "predator_prey";
  initial_prey: number;
  initial_predator?: number;
  growth_rate: number;
  carrying_capacity?: number;
  predation_rate?: number;
  predator_death_rate?: number;
  conversion_rate?: number;
  duration: number;
}

function setupOf(p: PopulationParams): PopulationSetup {
  return { model: p.model, initial_prey: p.initial_prey, initial_predator: p.initial_predator ?? 0, duration: p.duration };
}

/** The model and starting values a resolved population_sim spec gives (the renderer's, and the tests'). */
export function populationSetup(spec: ResolvedSpec): SimSetup<PopulationState> {
  const p = spec.params as PopulationParams;
  const s = setupOf(p);
  const start: Values = { timescale: p.duration / RUN_SECONDS };
  for (const id of POPULATION_CONTROLS[p.model]) start[id] = Number((p as unknown as Record<string, number>)[id]);
  return { model: population(s, start), start, locked: [] };
}

function PopulationSim(props: VisualProps): ReactElement {
  const p = props.spec.params as PopulationParams;
  const s = setupOf(p);
  const { model, start, locked } = populationSetup(props.spec);
  const t = props.t ?? keyOnly;
  const lv = p.model === "predator_prey";
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated draw={(state, values) => {
      // The run so far, sampled every ten grid steps, with the current state at its end.
      const run = trajectory(s, values, state.step, 10);
      const whole = trajectory(s, values, POPULATION_STEPS, 20);
      const capacity = lv ? 0 : num(values, "carrying_capacity");
      const most = Math.max(capacity, ...whole.flatMap(([, x, y]) => (lv ? [x, y] : [x])).filter(Number.isFinite), 1) * 1.1;
      const x = (time: number) => BOX.left + (time / s.duration) * (BOX.right - BOX.left);
      const y = (n: number) => BOX.bottom - (Math.min(Math.max(n, 0), most) / most) * (BOX.bottom - BOX.top);
      const line = (k: 1 | 2) => run.map((r) => `${x(r[0]).toFixed(2)},${y(r[k]).toFixed(2)}`).join(" ");
      const last = run[run.length - 1];
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t(lv ? "sim.population.drawing_predator_prey" : "sim.population.drawing_logistic")}>
          <rect x={BOX.left} y={BOX.top} width={BOX.right - BOX.left} height={BOX.bottom - BOX.top} fill={PAPER} stroke={STRONG} strokeWidth={1} />
          {!lv && (
            <g data-sim-capacity="true">
              <line x1={BOX.left} x2={BOX.right} y1={y(capacity)} y2={y(capacity)} stroke={MUTED} strokeWidth={1} strokeDasharray={DASHES[1]} />
              <text x={BOX.right - 4} y={y(capacity) - 6} textAnchor="end" fontSize={12} fill={MUTED}>{t("sim.population.capacity")}</text>
            </g>
          )}
          <polyline points={line(1)} fill="none" stroke={SERIES[0]} strokeWidth={2} data-sim-series="prey" />
          {lv && <polyline points={line(2)} fill="none" stroke={SERIES[1]} strokeWidth={2} strokeDasharray={DASHES[1]} data-sim-series="predator" />}
          {last !== undefined && <circle cx={x(last[0])} cy={y(last[1])} r={5} fill={SERIES[0]} stroke={INK} strokeWidth={1} />}
          {last !== undefined && lv && <rect x={x(last[0]) - 5} y={y(last[2]) - 5} width={10} height={10} fill={SERIES[1]} stroke={INK} strokeWidth={1} />}
          <line x1={BOX.left} x2={BOX.right} y1={BOX.bottom} y2={BOX.bottom} stroke={INK} strokeWidth={1.5} />
          <text x={BOX.right} y={BOX.bottom + 18} textAnchor="end" fontSize={12} fill={MUTED}>{t("sim.population.time")}</text>
          <g fontSize={12} fill={MUTED}>
            <line x1={BOX.left} x2={BOX.left + 24} y1={BOX.bottom + 34} y2={BOX.bottom + 34} stroke={SERIES[0]} strokeWidth={2} />
            <text x={BOX.left + 30} y={BOX.bottom + 38}>{t(lv ? "sim.population.prey" : "sim.population.population")}</text>
            {lv && (
              <>
                <line x1={BOX.left + 130} x2={BOX.left + 154} y1={BOX.bottom + 34} y2={BOX.bottom + 34} stroke={SERIES[1]} strokeWidth={2} strokeDasharray={DASHES[1]} />
                <text x={BOX.left + 160} y={BOX.bottom + 38}>{t("sim.population.predator")}</text>
              </>
            )}
            <text x={BOX.right} y={BOX.bottom + 54} textAnchor="end" fill={MUTED}>{t("sim.population.simplified")}</text>
          </g>
        </svg>
      );
    }} />
  );
}

export const populationSimulation = define({ kind: "population_sim", schema: "visuals/kinds/population_sim", labelModes: ["shown"], draw: (_s, props) => <PopulationSim {...props} /> });
