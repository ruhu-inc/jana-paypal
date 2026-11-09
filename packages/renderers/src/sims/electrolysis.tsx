// electrolysis_sim (VSL-08a, VSL-46): an electrolysis cell from a preset (the electrolyte, its ions and the product at
// each electrode, from the syllabus rules), with the current as the control and the run's time as it goes. Ions drift
// to the electrode of opposite charge, a gas comes off as bubbles and a metal builds up as a layer; the amounts follow
// Q = It and the pack's Faraday constant. Ion motion is schematic. No product is named until the current has flowed,
// so the locked drawing never answers the question.
import type { ReactElement } from "react";
import type { ResolvedSpec, VisualProps } from "../contract.js";
import { define } from "../define.js";
import { INK, LINE, MUTED, PAPER, SERIES, STRONG, WordsFallback, keyOnly } from "../kit.js";
import { must } from "../must.js";
import { num, type SimSetup, type Values } from "./models.js";
import presets from "./presets.json" with { type: "json" };
import { scenarioOf } from "./predict.js";
import { electrolysis, RUN_SECONDS, type ElectrodeProduct, type ElectrolysisConstants, type ElectrolysisPreset, type ElectrolysisState } from "./science.js";
import { SimView } from "./SimView.js";

const W = 480;
const H = 260;

export function electrolysisPreset(id: string): ElectrolysisPreset | undefined {
  return (presets.presets as { id: string; kind: string }[]).find((p) => p.id === id && p.kind === "electrolysis_sim") as ElectrolysisPreset | undefined;
}

interface ElectrolysisParams {
  preset_id: string;
  current_a: number;
  duration_s: number;
  locked?: string[];
}

/** The model and starting values a resolved electrolysis_sim spec gives (the renderer's, and the tests'). */
export function electrolysisSetup(spec: ResolvedSpec): SimSetup<ElectrolysisState> {
  const p = spec.params as ElectrolysisParams;
  const preset = must(electrolysisPreset(p.preset_id));
  const k = (spec.computed as { constants?: ElectrolysisConstants }).constants ?? { faraday: NaN, vm_cm3: NaN, molar_mass: {} };
  const start: Values = { current: p.current_a, ions: true, duration: p.duration_s, timescale: p.duration_s / RUN_SECONDS };
  return { model: electrolysis(preset, k), start, locked: p.locked ?? [] };
}

function charge(n: number): string {
  const size = Math.abs(n) === 1 ? "" : String(Math.abs(n));
  return `${size}${n > 0 ? "+" : "-"}`;
}

function ElectrolysisSim(props: VisualProps): ReactElement {
  const p = props.spec.params as ElectrolysisParams;
  const preset = electrolysisPreset(p.preset_id);
  // A preset this build does not have is described in words, never guessed at.
  if (preset === undefined) return <WordsFallback spec={props.spec} reason="unknown_preset" />;
  const { model, start, locked } = electrolysisSetup(props.spec);
  const t = props.t ?? keyOnly;
  const left = 110;
  const right = 370;
  const top = 100;
  const bottom = 236;
  const cathodeX = 175;
  const anodeX = 305;
  return (
    <SimView model={model} props={props} start={start} locked={locked} animated scenario={scenarioOf("electrolysis_sim", props.spec.params)} draw={(state, values) => {
      const r = model.read(state, values);
      const progress = Math.min(1, state.time / Math.max(1e-9, num(values, "duration")));
      const flowing = state.time > 0;
      // A deposit or a dissolving anode changes the electrode's width; a gas comes off as bubbles.
      const layer = (product: ElectrodeProduct, mass: number) => (product.measure === "gas" ? 0 : Math.min(8, Math.abs(mass) * 6) * Math.sign(mass));
      const cathodeLayer = layer(preset.cathode, must(r["out.cathode_mass"]));
      const anodeLayer = layer(preset.anode, must(r["out.anode_mass"]));
      const bubbles = (x: number, gas: number) => Array.from({ length: Math.min(10, Math.ceil(gas / 6)) }, (_, i) => {
        const rise = ((i * 0.37 + progress * 3) % 1) * (bottom - top - 20);
        return <circle key={i} cx={x + (i % 2 === 0 ? -14 : 14) + ((i * 5) % 7) - 3} cy={bottom - 16 - rise} r={3 + (i % 3)} fill={PAPER} stroke={INK} strokeWidth={1} />;
      });
      // Ions sit on a loose grid clear of the electrodes and drift toward the electrode of opposite charge.
      const slots: [number, number][] = [];
      for (let row = 0; row < 3; row += 1) {
        for (let col = 0; col < 8; col += 1) {
          const x = left + 24 + col * ((right - left - 48) / 7);
          if (Math.abs(x - cathodeX) > 26 && Math.abs(x - anodeX) > 26) slots.push([x, top + 22 + row * 40 + (col % 2) * 12]);
        }
      }
      const placed = preset.ions.flatMap((ion, j) => [0, 1].map((i) => ({ ion, i, j })));
      const ions = values.ions === true ? placed.map(({ ion, i, j }, n) => {
        const [hx, y] = must(slots[(n * 5) % slots.length]);
        const toward = ion.charge > 0 ? cathodeX : anodeX;
        const drift = flowing ? ((progress * 4 + i * 0.3 + j * 0.17) % 1) * 0.5 : 0;
        const x = hx + (toward - hx) * drift;
        return (
          <g key={`${ion.formula}-${i}`} data-sim-ion={ion.formula}>
            <circle cx={x} cy={y} r={13} fill={PAPER} stroke={ion.charge > 0 ? SERIES[0] : SERIES[1]} strokeWidth={1.5} strokeDasharray={ion.charge > 0 ? undefined : "3 2"} />
            <text x={x} y={y + 4} textAnchor="middle" fontSize={10} fill="var(--color-ink)">{ion.formula}<tspan dy={-5} fontSize={8}>{charge(ion.charge)}</tspan></text>
          </g>
        );
      }) : [];
      const electrode = (x: number, extra: number, label: string, product: ElectrodeProduct, gas: number, part: string) => (
        <g data-sim-electrode={part}>
          <rect x={x - 6} y={60} width={12} height={bottom - 70} fill={preset.electrodes === "copper" ? SERIES[2] : STRONG} stroke={INK} strokeWidth={1.5} />
          {extra > 0 && (
            // A metal builds up as a layer below the surface of the electrolyte.
            <rect x={x - 6 - extra} y={top + 4} width={12 + 2 * extra} height={bottom - top - 14} fill={SERIES[2]} fillOpacity={0.8} stroke={INK} strokeWidth={1} data-sim-layer="deposit" />
          )}
          {extra < 0 && (
            // A copper anode wears away: thinner below the surface.
            <g data-sim-layer="dissolved">
              <rect x={x - 7} y={top + 4} width={14} height={bottom - top - 14} fill={PAPER} stroke="none" />
              <rect x={x - Math.max(1.5, 6 + extra / 2)} y={top + 4} width={Math.max(3, 12 + extra)} height={bottom - top - 14} fill={SERIES[2]} stroke={INK} strokeWidth={1} />
            </g>
          )}
          <text x={x + (x < W / 2 ? -12 : 12)} y={54} textAnchor={x < W / 2 ? "end" : "start"} fontSize={12} fill="var(--color-ink)">{t(label)}</text>
          {bubbles(x, gas)}
          {flowing && <text x={x} y={bottom + 18} textAnchor="middle" fontSize={12} fill={MUTED} data-sim-product={product.id}>{t(`sim.electrolysis.product.${product.id}`)}</text>}
        </g>
      );
      return (
        <svg viewBox={`0 0 ${W} ${H + 32}`} className="w-full" role="img" aria-label={t("sim.electrolysis.drawing", { cell: t(`sim.electrolysis.cell.${preset.key}`) })}>
          <path d={`M${cathodeX},60 L${cathodeX},24 L${W / 2 - 18},24 M${W / 2 + 18},24 L${anodeX},24 L${anodeX},60`} fill="none" stroke={INK} strokeWidth={1.5} />
          <line x1={W / 2 - 8} x2={W / 2 - 8} y1={12} y2={36} stroke={INK} strokeWidth={1.5} />
          <line x1={W / 2 + 8} x2={W / 2 + 8} y1={17} y2={31} stroke={INK} strokeWidth={3} />
          <rect x={left} y={top} width={right - left} height={bottom - top} fill={SERIES[0]} fillOpacity={0.08} stroke="none" />
          <path d={`M${left},${top - 30} L${left},${bottom} L${right},${bottom} L${right},${top - 30}`} fill="none" stroke={INK} strokeWidth={1.5} />
          <line x1={left} x2={right} y1={top} y2={top} stroke={LINE} strokeWidth={1} />
          {ions}
          {electrode(cathodeX, cathodeLayer, "sim.electrolysis.cathode", preset.cathode, must(r["out.cathode_gas"]), "cathode")}
          {electrode(anodeX, anodeLayer, "sim.electrolysis.anode", preset.anode, must(r["out.anode_gas"]), "anode")}
          <text x={W / 2} y={H + 26} textAnchor="middle" fontSize={12} fill={MUTED}>{t(`sim.electrolysis.cell.${preset.key}`)}</text>
        </svg>
      );
    }} />
  );
}

export const electrolysisSimulation = define({ kind: "electrolysis_sim", schema: "visuals/kinds/electrolysis_sim", labelModes: ["shown"], draw: (_s, props) => <ElectrolysisSim {...props} /> });
