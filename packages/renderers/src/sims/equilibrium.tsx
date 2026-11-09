// equilibrium_sim (VSL-08a, VSL-46): a closed, well-mixed reversible reaction by mass action, with the controls the
// request declares (temperature by van't Hoff from the reference Kc; volume for the gases; adding a reactant or a
// product; a catalyst that speeds both directions equally). Bars show each concentration and two arrows the forward
// and backward rates, which meet at equilibrium: the reaction carries on both ways, it does not stop. A change
// disturbs the mixture as it stands.
import { useMemo, type ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { CLIENT_KEYS } from "../generated/keys.js";
import { INK, LINE, MUTED, SERIES, STRONG, keyOnly } from "../kit.js";
import { must } from "../must.js";
import { num, type SimSetup, type Values } from "./models.js";
import { scenarioOf } from "./predict.js";
import { runToEnd } from "./run.js";
import { CATALYST_FACTOR, equilibrium, equilibriumTimescale, kcAt, type EquilibriumSetup, type EquilibriumSpecies, type EquilibriumState } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;

interface EquilibriumParams {
  reaction: { species: EquilibriumSpecies[] };
  initial_concentrations: { species_id: string; value: number }[];
  temperature_k: number;
  reference_temperature_k: number;
  reference_kc: number;
  forward_enthalpy_j_mol: number;
  volume_l: number;
  forward_rate_constant: number;
  controls: string[];
}

function setupOf(p: EquilibriumParams): EquilibriumSetup {
  return {
    species: p.reaction.species,
    initial: Object.fromEntries(p.initial_concentrations.map((c) => [c.species_id, c.value])),
    reference_temperature_k: p.reference_temperature_k,
    reference_kc: p.reference_kc,
    forward_enthalpy_j_mol: p.forward_enthalpy_j_mol,
    volume_l: p.volume_l,
    forward_rate_constant: p.forward_rate_constant,
  };
}

/** The model and starting values a resolved equilibrium_sim spec gives (the renderer's, and the tests'). */
export function equilibriumSetup(spec: ResolvedSpec): SimSetup<EquilibriumState> {
  const p = spec.params as EquilibriumParams;
  const s = setupOf(p);
  const base: Values = { temperature: p.temperature_k, volume: p.volume_l, catalyst: false, add_reactant: 0, add_product: 0 };
  return { model: equilibrium(s, p.controls), start: { ...base, timescale: equilibriumTimescale(s, base) }, locked: [] };
}

/** A formula with its counts as subscripts (the 2 of H2 lowered); a coefficient before a formula stays on the line. */
function Formula({ text }: { text: string }): ReactElement {
  const bits = text.match(/[A-Z][a-z]?|\d+|[^A-Za-z\d]+/g) ?? [text];
  let lowered = false;
  return (
    <>
      {bits.map((b, i) => {
        const sub = /^\d+$/.test(b) && i > 0 && /[A-Za-z]$/.test(bits[i - 1] ?? "");
        const dy = sub && !lowered ? 3 : !sub && lowered ? -3 : 0;
        lowered = sub;
        return <tspan key={i} dy={dy || undefined} fontSize={sub ? "0.75em" : undefined}>{b}</tspan>;
      })}
    </>
  );
}

function EquilibriumSim(props: VisualProps): ReactElement {
  const p = props.spec.params as EquilibriumParams;
  const s = setupOf(p);
  const { model, start, locked } = equilibriumSetup(props.spec);
  const t = props.t ?? keyOnly;
  const active = s.species.filter((x) => x.state === "gas" || x.state === "aqueous");
  // A fixed scale from the starting mixture and its equilibrium, so the bars move against it.
  const settled = useMemo(() => model.read(runToEnd(model, start, 1 / CLIENT_KEYS.sim_steps_per_s), start), [props.spec.visual_id]);
  const top = Math.max(1e-9, ...active.map((x) => s.initial[x.id] ?? 0), ...active.map((x) => settled[`out.c_${x.id}`] ?? 0)) * 1.6;
  const equation = `${s.species.filter((x) => x.side === "reactant").map((x) => `${x.coefficient > 1 ? x.coefficient : ""}${x.formula}`).join(" + ")} ⇌ ${s.species.filter((x) => x.side === "product").map((x) => `${x.coefficient > 1 ? x.coefficient : ""}${x.formula}`).join(" + ")}`;
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated scenario={scenarioOf("equilibrium_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      const factor = values.catalyst === true ? CATALYST_FACTOR : 1;
      const kf = s.forward_rate_constant * factor;
      const kr = kf / kcAt(s, num(values, "temperature"));
      let forward = kf;
      let backward = kr;
      active.forEach((x, i) => {
        const c = Math.max(0, must(state.c[i])) ** x.coefficient;
        if (x.side === "reactant") forward *= c;
        else backward *= c;
      });
      const most = Math.max(forward, backward, 1e-300);
      const barW = Math.min(56, 220 / active.length);
      const base = 210;
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.equilibrium.drawing")}>
          <text x={20} y={26} fontSize={14} fill="var(--color-ink)" data-sim-equation="true"><Formula text={equation} /></text>
          <line x1={20} x2={280} y1={base} y2={base} stroke={STRONG} strokeWidth={1.5} />
          {active.map((x, i) => {
            const c = must(r[`out.c_${x.id}`]);
            const h = Math.min(160, (c / top) * 160);
            const cx = 40 + i * (barW + 18);
            return (
              <g key={x.id} data-sim-bar={x.id}>
                <rect x={cx} y={base - h} width={barW} height={h} fill={x.side === "reactant" ? SERIES[0] : SERIES[1]} stroke={INK} strokeWidth={1} />
                <text x={cx + barW / 2} y={base + 18} textAnchor="middle" fontSize={13} fill="var(--color-ink)"><Formula text={x.formula} /></text>
              </g>
            );
          })}
          <g data-sim-rates="true">
            <text x={300} y={70} fontSize={12} fill={MUTED}>{t("sim.equilibrium.forward")}</text>
            <rect x={300} y={78} width={Math.max(2, (forward / most) * 160)} height={10} fill={SERIES[0]} stroke={INK} strokeWidth={1} />
            <path d={`M${302 + (forward / most) * 160},76 l10,7 l-10,7 z`} fill={INK} />
            <text x={300} y={124} fontSize={12} fill={MUTED}>{t("sim.equilibrium.backward")}</text>
            <rect x={460 - Math.max(2, (backward / most) * 160)} y={132} width={Math.max(2, (backward / most) * 160)} height={10} fill={SERIES[1]} stroke={INK} strokeWidth={1} />
            <path d={`M${458 - (backward / most) * 160},130 l-10,7 l10,7 z`} fill={INK} />
          </g>
          {values.catalyst === true && <rect x={300} y={168} width={160} height={6} fill={LINE} data-sim-catalyst="true" />}
        </svg>
      );
    }} />
  );
}

export const equilibriumSimulation = define({ kind: "equilibrium_sim", schema: "visuals/kinds/equilibrium_sim", labelModes: ["shown"], draw: (_s, props) => <EquilibriumSim {...props} /> });
