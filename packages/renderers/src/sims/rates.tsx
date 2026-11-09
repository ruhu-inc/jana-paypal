// rates_sim (VSL-08a, VSL-46): a schematic collision box for a simplified first-order reaction (k = A e^(−Ea/RT),
// product = c0 (1 − e^(−kt))), with temperature, concentration and a catalyst (a lower activation energy) as controls.
// Circles are the reactant and squares the product; the particles are schematic and never counted molecules, and a
// run ends once the product is within one part in a million of all it can become.
import type { ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { INK, LINE, MUTED, PAPER, SERIES, keyOnly } from "../kit.js";
import { must } from "../must.js";
import { num, type SimSetup, type Values } from "./models.js";
import { scenarioOf } from "./predict.js";
import { RATES_END_KT, RUN_SECONDS, rateConstant, rates, scatter, type RatesSetup, type RatesState } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;

interface RatesParams extends RatesSetup {
  temperature_k: number;
  concentration_mol_l: number;
  locked?: string[];
}

/** The model and starting values a resolved rates_sim spec gives (the renderer's, and the tests'). */
export function ratesSetup(spec: ResolvedSpec): SimSetup<RatesState> {
  const p = spec.params as RatesParams;
  // The reaction-time scale: the run from the starting values takes RUN_SECONDS at normal speed, so a faster run is
  // seen to finish sooner.
  const end = RATES_END_KT / rateConstant(p, p.temperature_k, false);
  const start: Values = { temperature: p.temperature_k, concentration: p.concentration_mol_l, catalyst: false, timescale: end / RUN_SECONDS };
  return { model: rates(p), start, locked: p.locked ?? [] };
}

// A triangle wave: a coordinate that bounces between 0 and 1 as `u` grows.
function bounce(u: number): number {
  const v = ((u % 2) + 2) % 2;
  return v > 1 ? 2 - v : v;
}

function RatesSim(props: VisualProps): ReactElement {
  const { model, start, locked } = ratesSetup(props.spec);
  const t = props.t ?? keyOnly;
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated scenario={scenarioOf("rates_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      const c0 = num(values, "concentration");
      const reacted = c0 > 0 ? must(r["out.product"]) / c0 : 0;
      const count = Math.max(2, Math.round(c0 * 16));
      // Screen time drives the motion; particles move faster when hotter (as the square root of the kelvin temperature).
      const seconds = state.time / Math.max(1e-12, num(values, "timescale"));
      const speed = 0.25 * Math.sqrt(num(values, "temperature") / 293);
      const box = { x: 30, y: 30, w: 300, h: 200 };
      const particles = Array.from({ length: count }, (_, i) => {
        const x = box.x + 10 + bounce(scatter(i, 1) * 2 + seconds * speed * (0.6 + scatter(i, 3))) * (box.w - 20);
        const y = box.y + 10 + bounce(scatter(i, 2) * 2 + seconds * speed * (0.6 + scatter(i, 4))) * (box.h - 20);
        // Particle i has turned to product once that share of the reactant has reacted (deterministic order).
        const done = (i + 0.5) / count <= reacted;
        return done
          ? <rect key={i} x={x - 6} y={y - 6} width={12} height={12} fill={SERIES[1]} stroke={INK} strokeWidth={1} data-sim-particle="product" />
          : <circle key={i} cx={x} cy={y} r={6} fill={SERIES[0]} stroke={INK} strokeWidth={1} data-sim-particle="reactant" />;
      });
      return (
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={t("sim.rates.drawing")}>
          <rect x={box.x} y={box.y} width={box.w} height={box.h} fill={PAPER} stroke={INK} strokeWidth={1.5} />
          {particles}
          {values.catalyst === true && <rect x={box.x} y={box.y + box.h - 8} width={box.w} height={8} fill={LINE} data-sim-catalyst="true" />}
          <g data-sim-progress="true">
            <rect x={360} y={box.y} width={24} height={box.h} fill={PAPER} stroke={INK} strokeWidth={1.5} />
            <rect x={361} y={box.y + box.h * (1 - reacted)} width={22} height={box.h * reacted} fill={SERIES[1]} />
            <text x={372} y={box.y + box.h + 18} textAnchor="middle" fontSize={12} fill={MUTED}>{t("sim.rates.product")}</text>
          </g>
          <g fontSize={12} fill={MUTED}>
            <circle cx={410} cy={60} r={6} fill={SERIES[0]} stroke={INK} strokeWidth={1} />
            <text x={422} y={64}>{t("sim.rates.reactant")}</text>
            <rect x={404} y={84} width={12} height={12} fill={SERIES[1]} stroke={INK} strokeWidth={1} />
            <text x={422} y={94}>{t("sim.rates.product")}</text>
          </g>
        </svg>
      );
    }} />
  );
}

export const ratesSimulation = define({ kind: "rates_sim", schema: "visuals/kinds/rates_sim", labelModes: ["shown"], draw: (_s, props) => <RatesSim {...props} /> });
