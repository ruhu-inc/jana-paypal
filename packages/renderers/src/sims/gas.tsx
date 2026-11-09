// gas_laws_sim (VSL-08a, VSL-46): particles in a sealed flask or a syringe, with the one quantity the preset lets the
// student change (temperature in a flask; temperature or volume in a syringe) and the other held. The pressure gauge
// and thermometer move with the model; the particles are schematic, never counted molecules.
import type { ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { INK, LINE, MUTED, PAPER, SERIES, STRONG, keyOnly } from "../kit.js";
import { must } from "../must.js";
import { num, type SimSetup, type Values } from "./models.js";
import presets from "./presets.json" with { type: "json" };
import { scenarioOf } from "./predict.js";
import { gasLaws, type GasState } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;

export interface GasPreset {
  id: string;
  kind: string;
  container: "flask" | "syringe";
  controls: string[];
}

export function gasPreset(id: string): GasPreset | undefined {
  return (presets.presets as { id: string; kind: string }[]).find((p) => p.id === id && p.kind === "gas_laws_sim") as GasPreset | undefined;
}

/** The model and starting values a resolved gas_laws_sim spec gives (the renderer's, and the tests'). */
export function gasSetup(spec: ResolvedSpec): SimSetup<GasState> {
  const p = spec.params as { amount_mol: number; temperature_k: number; volume_m3: number; control: "temperature" | "volume"; preset_id: string };
  const model = gasLaws(p.amount_mol, (spec.computed as { kelvin_offset?: number }).kelvin_offset ?? NaN);
  const start: Values = { temperature: p.temperature_k, volume: Number((p.volume_m3 * 1e6).toFixed(6)), control: p.control };
  // The quantity the declared control does not change is held: isochoric in a flask, isothermal when volume moves.
  return { model, start, locked: [p.control === "volume" ? "temperature" : "volume"] };
}

function GasSim(props: VisualProps): ReactElement {
  const p = props.spec.params as { preset_id: string };
  const syringe = gasPreset(p.preset_id)?.container === "syringe";
  const { model, start, locked } = gasSetup(props.spec);
  const t = props.t ?? keyOnly;
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated scenario={scenarioOf("gas_laws_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      // The box: a fixed flask, or a syringe barrel whose plunger moves with the volume.
      const left = 40;
      const top = 60;
      const height = 150;
      const width = syringe ? 60 + (num(values, "volume") / 500) * 260 : 260;
      const pressure = must(r["out.pressure"]);
      const angle = Math.min(1, pressure / 400) * Math.PI;
      const tFrac = Math.min(1, (num(values, "temperature") - 100) / 500);
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t(syringe ? "sim.gas.drawing_syringe" : "sim.gas.drawing_flask")}>
          <rect x={left} y={top} width={width} height={height} fill={PAPER} stroke={INK} strokeWidth={syringe ? 1.5 : 3} rx={syringe ? 2 : 10} />
          {syringe && (
            <g data-sim-plunger="true">
              <rect x={left + width} y={top + 4} width={8} height={height - 8} fill={STRONG} />
              <rect x={left + width + 8} y={top + height / 2 - 4} width={W - 60 - (left + width + 8)} height={8} fill={LINE} stroke={STRONG} strokeWidth={1} />
            </g>
          )}
          {state.particles.x.map((x, i) => (
            <circle key={i} cx={left + 8 + x * (width - 16)} cy={top + 8 + must(state.particles.y[i]) * (height - 16)} r={4} fill={SERIES[0]} stroke={INK} strokeWidth={0.75} />
          ))}
          <g data-sim-gauge="true">
            <circle cx={left + 40} cy={36} r={24} fill={PAPER} stroke={INK} strokeWidth={1.5} />
            <line x1={left + 40} y1={36} x2={left + 40 - 18 * Math.cos(angle)} y2={36 - 18 * Math.sin(angle)} stroke={INK} strokeWidth={2} strokeLinecap="round" />
            <line x1={left + 40} y1={60} x2={left + 40} y2={top} stroke={INK} strokeWidth={1.5} />
            <text x={left + 72} y={30} fontSize={12} fill={MUTED}>{t("sim.gas.pressure")}</text>
          </g>
          <g data-sim-thermometer="true">
            <rect x={W - 40} y={50} width={12} height={160} rx={6} fill={PAPER} stroke={INK} strokeWidth={1.5} />
            <rect x={W - 37} y={207 - tFrac * 150} width={6} height={tFrac * 150} rx={3} fill={SERIES[1]} />
            <text x={W - 22} y={236} textAnchor="end" fontSize={12} fill={MUTED}>{t("sim.gas.temperature")}</text>
          </g>
        </svg>
      );
    }} />
  );
}

export const gasLawsSimulation = define({ kind: "gas_laws_sim", schema: "visuals/kinds/gas_laws_sim", labelModes: ["shown"], draw: (_s, props) => <GasSim {...props} /> });
