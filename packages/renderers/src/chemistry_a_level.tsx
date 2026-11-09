// The organic and A level chemistry kinds (VSL-06, VSL-07, VSL-46): organic_structure, energy_levels, orbital_boxes,
// maxwell_boltzmann, born_haber_cycle, concentration_time and spectrum_chart (reaction_mechanism is a library kind,
// drawn in library.tsx). Every figure comes from the resolution: structures and their drawings from chem_organic,
// photon energies and the Born-Haber closing value from math_evaluate, curves from math_plot_points, configurations
// and spectra from pack data. Positions are presentation geometry of those values; an illustrative equilibrium
// response says so and shows no time scale. Each kind offers its values as a table (LOC-16).
import { createElement, type ReactElement, type ReactNode } from "react";
import type { ResolvedSpec, TableData } from "./contract.js";
import { define } from "./define.js";
import {
  Arrow, Axes, DASHES, Frame, INK, Label, LINE, MUTED, PAPER, Part, SERIES, STRONG, TINT, WordsFallback, areaFill, computed, computedLabel, fmt,
  highlighted, lineStyle, linear, params, shown, ticks, type Scene,
} from "./kit.js";
import { SvgRefused, parseSvg, reactProps, svgProblems, type SvgNode } from "./library_svg.js";
import { must } from "./must.js";

const W = 480;
const H = 320;
const BOX = { left: 56, right: W - 24, top: 28, bottom: H - 44 };

type Pt = [number, number];

function polyline(points: Pt[], x: (v: number) => number, y: (v: number) => number): string {
  return points.map(([a, b]) => `${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ");
}

/** A params id as a part id segment (the engine's `part_id`, VSL-19). */
function pid(raw: string): string {
  return raw.replace(/[^A-Za-z0-9_]/g, "_");
}

/** Sampled rows of a curve for a table: about ten points, as the tool gave them. */
function sampled(segments: Pt[][]): Pt[] {
  const flat = segments.flat();
  const every = Math.max(1, Math.ceil(flat.length / 10));
  return flat.filter((_, n) => n % every === 0);
}

// --- energy_levels --------------------------------------------------------------------------------------------------

interface Level {
  id: string;
  energy_ev: number;
  label: string;
}

interface LevelsParams {
  levels: Level[];
  transitions: { from: string; to: string }[];
}

function drawEnergyLevels(s: Scene): ReactElement {
  const p = params<LevelsParams>(s.spec);
  const found = new Map((computed<{ transitions?: { id: string; direction: string }[] }>(s.spec).transitions ?? []).map((t) => [t.id, t]));
  const energies = p.levels.map((l) => l.energy_ev);
  const y = linear(Math.min(...energies), Math.max(...energies), H - 40, 40);
  const at = new Map(p.levels.map((l) => [l.id, y(l.energy_ev)]));
  const step = Math.min(70, 220 / Math.max(1, p.transitions.length));
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        <Arrow x1={30} y1={H - 20} x2={30} y2={20} stroke={MUTED} width={1} />
        {s.labels !== "hidden" && <text x={36} y={18} fontSize={11 * s.scale} fill={MUTED} data-presentation="true">{s.t("visual.levels.axis")}</text>}
      </g>
      {p.levels.map((l) => {
        const id = `lvl_${pid(l.id)}`;
        return (
          <Part key={id} s={s} id={id}>
            <line x1={200} x2={440} y1={y(l.energy_ev)} y2={y(l.energy_ev)} {...lineStyle(s, id, INK, 2)} />
            <Label s={s} part={id} x={194} y={y(l.energy_ev) + 4} text={computedLabel(s.spec, id, l.label)} anchor="end" size={11} />
          </Part>
        );
      })}
      {p.transitions.map((t, i) => {
        const id = `tr_${pid(t.from)}_${pid(t.to)}`;
        const x = 230 + i * step;
        const emission = found.get(id)?.direction !== "absorption";
        const y1 = must(at.get(t.from));
        const y2 = must(at.get(t.to));
        return (
          <Part key={id} s={s} id={id}>
            <Arrow x1={x} y1={y1} x2={x} y2={y2} stroke={lineStyle(s, id, SERIES[emission ? 0 : 1], 2).stroke} width={2} {...(emission ? {} : { dash: "5 3" })} />
            <Label s={s} part={id} x={x + 5} y={(y1 + y2) / 2} text={computedLabel(s.spec, id)} size={11} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const energyLevels = define({
  kind: "energy_levels", schema: "visuals/kinds/energy_levels", draw: drawEnergyLevels,
  table: (spec) => {
    const p = params<LevelsParams>(spec);
    const names = new Map(p.levels.map((l) => [l.id, l.label]));
    return {
      columns: ["visual.table.level", "visual.table.energy"],
      rows: [
        ...p.levels.map((l) => [l.label, `${fmt(l.energy_ev)} eV`]),
        ...p.transitions.map((t) => [`${names.get(t.from) ?? t.from} → ${names.get(t.to) ?? t.to}`, computedLabel(spec, `tr_${pid(t.from)}_${pid(t.to)}`)]),
      ],
    };
  },
});

// --- orbital_boxes --------------------------------------------------------------------------------------------------

interface Subshell {
  id: string;
  electrons: number;
  boxes: ("up" | "down")[][];
}

/** The filling order the boxes are stacked in (presentation layout: lower energy lower down). */
const FILLING = ["1s", "2s", "2p", "3s", "3p", "4s", "3d", "4p"];

function Spin({ x, y, up }: { x: number; y: number; up: boolean }): ReactElement {
  const [a, b] = up ? [y + 9, y - 9] : [y - 9, y + 9];
  const head = up ? `M${x - 3},${b + 4} L${x},${b} L${x + 3},${b + 4}` : `M${x - 3},${b - 4} L${x},${b} L${x + 3},${b - 4}`;
  return <path d={`M${x},${a} L${x},${b} ${head}`} fill="none" stroke={INK} strokeWidth={1.5} />;
}

function drawOrbitals(s: Scene): ReactElement {
  const c = computed<{ subshells?: Subshell[] }>(s.spec);
  const subshells = c.subshells ?? [];
  if (subshells.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const rank = (id: string) => FILLING.indexOf(id);
  const top = Math.max(...subshells.map((x) => rank(x.id)));
  const rowY = (id: string) => H - 70 - (rank(id) / Math.max(1, top)) * (H - 120);
  const size = 24;
  const configuration = computedLabel(s.spec, "configuration");
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        <Arrow x1={30} y1={H - 50} x2={30} y2={24} stroke={MUTED} width={1} />
        {s.labels !== "hidden" && <text x={36} y={20} fontSize={11 * s.scale} fill={MUTED} data-presentation="true">{s.t("visual.orbitals.energy")}</text>}
      </g>
      {subshells.map((sub) => {
        const id = `sub_${sub.id}`;
        const y = rowY(sub.id);
        const left = 140 + (sub.id.endsWith("d") ? 120 : 0);
        return (
          <Part key={id} s={s} id={id}>
            <Label s={s} part={id} x={left - 8} y={y + 5} text={sub.id} anchor="end" size={13} formula />
            {sub.boxes.map((box, k) => (
              <g key={k}>
                <rect x={left + k * size} y={y - size / 2} width={size} height={size} fill={areaFill(s, id, PAPER)} {...lineStyle(s, id, STRONG, 1.5)} />
                {box.map((spin, n) => <Spin key={n} x={left + k * size + (box.length === 1 ? size / 2 : n === 0 ? 8 : 16)} y={y} up={spin === "up"} />)}
              </g>
            ))}
          </Part>
        );
      })}
      {s.labels !== "hidden" && configuration !== "" && (
        <text x={W / 2} y={H - 18} textAnchor="middle" fontSize={13 * s.scale} fill="var(--color-ink)">
          {configuration.split(" ").flatMap((term, i) => {
            const [, name, count] = /^(\d[spdf])(\d+)$/.exec(term) ?? [term, term, ""];
            // Each electron count is a superscript; the next subshell drops back to the baseline.
            return [
              <tspan key={`n${i}`} dy={i > 0 ? 5 : 0}>{`${i > 0 ? " " : ""}${name}`}</tspan>,
              <tspan key={`c${i}`} dy={-5} fontSize={9 * s.scale}>{count}</tspan>,
            ];
          })}
        </text>
      )}
    </Frame>
  );
}

export const orbitalBoxes = define({
  kind: "orbital_boxes", schema: "visuals/kinds/orbital_boxes", draw: drawOrbitals,
  table: (spec) => ({ columns: ["visual.table.subshell", "visual.table.electrons"], rows: (computed<{ subshells?: Subshell[] }>(spec).subshells ?? []).map((x) => [x.id, String(x.electrons)]) }),
});

// --- maxwell_boltzmann ----------------------------------------------------------------------------------------------

interface MaxwellParams {
  temperatures_k: number[];
  activation_energy_j_mol: number;
  energy_max_j_mol: number;
}

function drawMaxwell(s: Scene): ReactElement {
  const p = params<MaxwellParams>(s.spec);
  const curves = computed<{ curves?: Record<string, Pt[][]> }>(s.spec).curves ?? {};
  const all = Object.values(curves).flatMap((seg) => seg.flat());
  if (all.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const peak = Math.max(...all.map((q) => q[1]));
  const x = linear(0, p.energy_max_j_mol, BOX.left, BOX.right);
  const y = linear(0, peak * 1.15, BOX.bottom, BOX.top);
  const ea = p.activation_energy_j_mol;
  return (
    <Frame s={s} width={W} height={H}>
      <Axes s={s} x={x} y={y} xRange={[0, p.energy_max_j_mol]} yRange={[0, peak * 1.15]} box={BOX} grid={false} xLabel={s.t("visual.mb.energy")} yLabel={s.t("visual.mb.fraction")} />
      {p.temperatures_k.map((_, i) => {
        const id = `curve_${i + 1}`;
        const pts = (curves[id] ?? []).flat();
        const top = pts.reduce<Pt | null>((best, q) => (best === null || q[1] > best[1] ? q : best), null);
        const dash = DASHES[i % DASHES.length];
        return shown(s, id) ? (
          <Part key={id} s={s} id={id}>
            <polyline points={polyline(pts, x, y)} fill="none" {...lineStyle(s, id, SERIES[i % SERIES.length], 2)} {...(dash ? { strokeDasharray: dash } : {})} />
            {top !== null && <Label s={s} part={id} x={x(top[0]) + 6} y={y(top[1]) - 6} text={computedLabel(s.spec, id)} size={11} />}
          </Part>
        ) : null;
      })}
      <Part s={s} id="ea">
        <line x1={x(ea)} x2={x(ea)} y1={BOX.bottom} y2={BOX.top + 10} {...lineStyle(s, "ea", INK, 1.5)} strokeDasharray="5 3" />
        <Label s={s} part="ea" x={x(ea) + 4} y={BOX.top + 14} text={computedLabel(s.spec, "ea")} size={11} />
      </Part>
      {p.temperatures_k.map((_, i) => {
        const id = `tail_${i + 1}`;
        const pts = (curves[`curve_${i + 1}`] ?? []).flat().filter((q) => q[0] >= ea);
        if (pts.length < 2 || !shown(s, id)) return null;
        const d = `M${x(ea).toFixed(2)},${y(0).toFixed(2)} ` + pts.map(([a, b]) => `L${x(a).toFixed(2)},${y(b).toFixed(2)}`).join(" ") + ` L${x(must(pts[pts.length - 1])[0]).toFixed(2)},${y(0).toFixed(2)} Z`;
        return (
          <Part key={id} s={s} id={id}>
            <path d={d} fill={areaFill(s, id, SERIES[i % SERIES.length])} fillOpacity={0.2 + 0.15 * i} stroke="none" />
            <Label s={s} part={id} x={BOX.right - 4} y={BOX.top + 30 + i * 16} text={`${computedLabel(s.spec, `curve_${i + 1}`)}: ${s.t("visual.mb.tail")} ${computedLabel(s.spec, id)}`} anchor="end" size={11} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const maxwellBoltzmann = define({
  kind: "maxwell_boltzmann", schema: "visuals/kinds/maxwell_boltzmann", draw: drawMaxwell,
  table: (spec) => {
    const p = params<MaxwellParams>(spec);
    const curves = computed<{ curves?: Record<string, Pt[][]> }>(spec).curves ?? {};
    const rows: string[][] = [];
    p.temperatures_k.forEach((_, i) => {
      const name = computedLabel(spec, `curve_${i + 1}`);
      rows.push([name, computedLabel(spec, `tail_${i + 1}`)]);
      for (const [a, b] of sampled(curves[`curve_${i + 1}`] ?? [])) rows.push([name, `(${fmt(a)}, ${fmt(b)})`]);
    });
    return { columns: ["visual.table.curve", "visual.table.point"], rows };
  },
});

// --- born_haber_cycle -----------------------------------------------------------------------------------------------

interface CycleStep {
  id: string;
  kind: "formation" | "atomisation" | "ionisation" | "electron_affinity" | "lattice";
  orientation: 1 | -1;
}

function drawBornHaber(s: Scene): ReactElement {
  const p = params<{ compound: string; steps: CycleStep[] }>(s.spec);
  const c = computed<{ contributions?: Record<string, number> }>(s.spec);
  if (c.contributions === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  // The level after each step, walking round the cycle from the elements (0).
  const levels = [0];
  for (const st of p.steps) levels.push(must(levels[levels.length - 1]) + (c.contributions[`step_${pid(st.id)}`] ?? 0));
  const y = linear(Math.min(...levels), Math.max(...levels), H - 40, 40);
  const gap = Math.min(66, (W - 90) / Math.max(1, p.steps.length));
  const xAt = (i: number) => 60 + i * gap;
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        {levels.map((v, i) => <line key={i} x1={xAt(Math.max(0, i - 1)) - 10} x2={xAt(Math.min(p.steps.length - 1, i)) + 10} y1={y(v)} y2={y(v)} stroke={LINE} strokeWidth={1.5} />)}
        {s.labels !== "hidden" && <text x={W - 10} y={H - 10} textAnchor="end" fontSize={11 * s.scale} fill={MUTED} data-presentation="true">{s.t("visual.born_haber.unit")}</text>}
      </g>
      {p.steps.map((st, i) => {
        const id = `step_${pid(st.id)}`;
        const [from, to] = st.orientation === 1 ? [must(levels[i]), must(levels[i + 1])] : [must(levels[i + 1]), must(levels[i])];
        const x = xAt(i);
        return (
          <Part key={id} s={s} id={id}>
            <Arrow x1={x} y1={y(from)} x2={x} y2={y(to)} stroke={lineStyle(s, id, SERIES[st.kind === "lattice" || st.kind === "formation" ? 1 : 0], 2).stroke} width={2} />
            <Label s={s} part={id} x={x + 5} y={(y(from) + y(to)) / 2 - 3} text={s.t(`visual.born_haber.${st.kind}`)} size={11} />
            <Label s={s} part={id} x={x + 5} y={(y(from) + y(to)) / 2 + 11} text={computedLabel(s.spec, id)} size={11} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const bornHaberCycle = define({
  kind: "born_haber_cycle", schema: "visuals/kinds/born_haber_cycle", draw: drawBornHaber,
  table: (spec) => ({
    columns: ["visual.table.step", "visual.table.enthalpy"],
    rows: params<{ steps: CycleStep[] }>(spec).steps.map((st) => [st.kind.replace(/_/g, " "), computedLabel(spec, `step_${pid(st.id)}`)]),
  }),
});

// --- concentration_time ---------------------------------------------------------------------------------------------

interface ConcSpecies {
  id: string;
  label: string;
  initial?: number;
  points?: { time: number; concentration: number }[];
  initial_plateau?: number;
  final_plateau?: number;
}

interface ConcParams {
  mode: "data" | "first_order" | "qualitative_equilibrium";
  species: ConcSpecies[];
  time_unit: string;
  concentration_unit: string;
  time_range: [number, number];
  event?: { at: number; change: string };
}

interface ConcComputed {
  curves?: Record<string, Pt[][]>;
  tangents?: Record<string, Pt[][]>;
  half_lives?: Record<string, number>;
  illustrative?: boolean;
}

function drawConcentration(s: Scene): ReactElement {
  const p = params<ConcParams>(s.spec);
  const c = computed<ConcComputed>(s.spec);
  const curves = c.curves ?? {};
  const all = Object.values(curves).flatMap((seg) => seg.flat());
  if (all.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const [t0, t1] = p.time_range;
  const top = Math.max(...all.map((q) => q[1])) * 1.15 || 1;
  const x = linear(t0, t1, BOX.left, BOX.right);
  const y = linear(0, top, BOX.bottom, BOX.top);
  const qualitative = p.mode === "qualitative_equilibrium";
  return (
    <Frame s={s} width={W} height={H}>
      {qualitative ? (
        <g aria-hidden="true">
          <Arrow x1={BOX.left} y1={BOX.bottom} x2={BOX.right} y2={BOX.bottom} stroke={STRONG} width={1.5} />
          <Arrow x1={BOX.left} y1={BOX.bottom} x2={BOX.left} y2={BOX.top} stroke={STRONG} width={1.5} />
          {s.labels !== "hidden" && <text x={BOX.right} y={BOX.bottom + 16} textAnchor="end" fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{s.t("visual.conc.time_unmeasured")}</text>}
          {s.labels !== "hidden" && <text x={BOX.left} y={BOX.top - 8} fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{s.t("visual.conc.relative")}</text>}
        </g>
      ) : (
        <Axes s={s} x={x} y={y} xRange={[t0, t1]} yRange={[0, top]} box={BOX} grid={false} xLabel={s.t("visual.conc.time", { unit: p.time_unit })} yLabel={s.t("visual.conc.concentration", { unit: p.concentration_unit })} />
      )}
      {p.species.map((sp, i) => {
        const id = `sp_${pid(sp.id)}`;
        const segments = curves[id] ?? [];
        const last = segments.flat().at(-1);
        const dash = DASHES[i % DASHES.length];
        return shown(s, id) ? (
          <Part key={id} s={s} id={id}>
            {segments.map((seg, n) => <polyline key={n} points={polyline(seg, x, y)} fill="none" {...lineStyle(s, id, SERIES[i % SERIES.length], 2)} {...(dash ? { strokeDasharray: dash } : {})} />)}
            {p.mode === "data" && (sp.points ?? []).map((q, n) => <circle key={`m${n}`} cx={x(q.time)} cy={y(q.concentration)} r={3} fill={INK} />)}
            {last !== undefined && <Label s={s} part={id} x={Math.min(x(last[0]) - 4, BOX.right - 4)} y={y(last[1]) - 6} text={sp.label} anchor="end" size={12} formula />}
          </Part>
        ) : null;
      })}
      {p.mode === "first_order" && p.species.flatMap((sp) => {
        const id = `sp_${pid(sp.id)}`;
        const half = c.half_lives?.[id];
        const tangent = c.tangents?.[id];
        const out: ReactNode[] = [];
        if (half !== undefined && sp.initial !== undefined) {
          out.push(
            <Part key={`${id}.half_life`} s={s} id={`${id}.half_life`}>
              <line x1={x(half)} x2={x(half)} y1={BOX.bottom} y2={y(sp.initial / 2)} stroke={MUTED} strokeWidth={1} strokeDasharray="3 3" />
              <circle cx={x(half)} cy={y(sp.initial / 2)} r={highlighted(s, `${id}.half_life`) ? 6 : 4} fill={INK} />
              <Label s={s} part={`${id}.half_life`} x={x(half) + 6} y={y(sp.initial / 2) - 8} text={computedLabel(s.spec, `${id}.half_life`)} size={11} />
            </Part>,
          );
        }
        if (tangent !== undefined) {
          const pts = tangent.flat();
          const end = pts.at(-1);
          out.push(
            <Part key={`${id}.tangent`} s={s} id={`${id}.tangent`}>
              <polyline points={polyline(pts, x, y)} fill="none" {...lineStyle(s, `${id}.tangent`, INK, 1.5)} strokeDasharray="6 3" />
              {end !== undefined && <Label s={s} part={`${id}.tangent`} x={x(end[0]) + 6} y={y(end[1]) - 6} text={`${s.t("visual.conc.initial_rate")} ${computedLabel(s.spec, `${id}.tangent`)}`} size={11} />}
            </Part>,
          );
        }
        return out;
      })}
      {qualitative && p.event !== undefined && (
        <Part s={s} id="event">
          <line x1={x(p.event.at)} x2={x(p.event.at)} y1={BOX.bottom} y2={BOX.top} {...lineStyle(s, "event", MUTED, 1.5)} strokeDasharray="4 3" />
          <Label s={s} part="event" x={x(p.event.at) + 4} y={BOX.top + 12} text={s.t(`visual.conc.change.${p.event.change}`)} size={11} />
        </Part>
      )}
      {qualitative && c.illustrative === true && <text x={W / 2} y={H - 6} textAnchor="middle" fontSize={11 * s.scale} fill={MUTED} data-presentation="true" data-illustrative="true">{s.t("visual.conc.illustrative")}</text>}
    </Frame>
  );
}

function concentrationTable(spec: ResolvedSpec): TableData {
  const p = params<ConcParams>(spec);
  if (p.mode === "qualitative_equilibrium") {
    return { columns: ["visual.table.species", "visual.table.before", "visual.table.after"], rows: p.species.map((sp) => [sp.label, fmt(sp.initial_plateau ?? 0), fmt(sp.final_plateau ?? 0)]) };
  }
  const c = computed<ConcComputed>(spec);
  const rows: string[][] = [];
  for (const sp of p.species) {
    const id = `sp_${pid(sp.id)}`;
    const pts: Pt[] = p.mode === "data" ? (sp.points ?? []).map((q) => [q.time, q.concentration]) : sampled(c.curves?.[id] ?? []);
    for (const [a, b] of pts) rows.push([sp.label, fmt(a), fmt(b)]);
    const half = computedLabel(spec, `${id}.half_life`);
    if (half !== "") rows.push([sp.label, half, fmt((sp.initial ?? 0) / 2)]);
  }
  return { columns: ["visual.table.species", "visual.table.time", "visual.table.concentration"], rows };
}

export const concentrationTime = define({ kind: "concentration_time", schema: "visuals/kinds/concentration_time", draw: drawConcentration, table: concentrationTable });

// --- spectrum_chart -------------------------------------------------------------------------------------------------

interface Peak {
  id: string;
  position: number;
  intensity: number;
  assignment: string;
  multiplicity?: string;
  integration?: number;
}

interface SpectrumComputed {
  spectrum?: { type: "mass" | "infrared" | "proton_nmr"; x_unit: string; y_unit: string };
  peaks?: Peak[];
}

function drawSpectrum(s: Scene): ReactElement {
  const p = params<{ type: string; peaks?: string[] }>(s.spec);
  const c = computed<SpectrumComputed>(s.spec);
  const peaks = c.peaks ?? [];
  if (c.spectrum === undefined || peaks.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const kind = c.spectrum.type;
  const positions = peaks.map((q) => q.position);
  // Infrared and NMR run from high to low along the axis, as they are printed; a mass spectrum runs up from zero.
  const range: [number, number] = kind === "infrared" ? [4000, 400] : kind === "proton_nmr" ? [Math.ceil(Math.max(...positions, 10)), 0] : [0, Math.ceil(Math.max(...positions) * 1.1)];
  const x = linear(range[0], range[1], BOX.left, BOX.right);
  const yMax = kind === "infrared" ? 100 : Math.max(...peaks.map((q) => q.intensity)) * 1.15;
  const y = linear(0, yMax, BOX.bottom, BOX.top);
  const base = kind === "infrared" ? 100 : 0;
  // The labelled peaks in the order the spec names them (their parts' order, VSL-19).
  const wanted = (p.peaks ?? []).map((id) => peaks.find((q) => q.id === id)).filter((q): q is Peak => q !== undefined);
  const xs = ticks(Math.min(...range), Math.max(...range), 6);
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        <line x1={BOX.left} x2={BOX.right} y1={BOX.bottom} y2={BOX.bottom} stroke={STRONG} strokeWidth={1.5} />
        <line x1={BOX.left} x2={BOX.left} y1={BOX.bottom} y2={BOX.top} stroke={STRONG} strokeWidth={1.5} />
        {s.labels !== "hidden" && xs.map((v) => <text key={v} x={x(v)} y={BOX.bottom + 14 * s.scale} textAnchor="middle" fontSize={11 * s.scale} fill={MUTED} data-presentation="true">{v}</text>)}
        {s.labels !== "hidden" && <text x={BOX.right} y={BOX.bottom + 30 * s.scale} textAnchor="end" fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{s.t(`visual.spectrum.x.${kind}`)}</text>}
        {s.labels !== "hidden" && <text x={BOX.left} y={BOX.top - 8} fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{s.t(`visual.spectrum.y.${kind}`)}</text>}
      </g>
      <Part s={s} id="trace">
        {kind === "infrared" && <line x1={BOX.left} x2={BOX.right} y1={y(100)} y2={y(100)} {...lineStyle(s, "trace", SERIES[0], 1.5)} />}
        {peaks.map((q) => <line key={q.id} x1={x(q.position)} x2={x(q.position)} y1={y(base)} y2={y(q.intensity)} {...lineStyle(s, "trace", SERIES[0], 3)} />)}
      </Part>
      {wanted.map((q, i) => {
        const id = `peak_${q.id}`;
        const tip = y(q.intensity);
        return (
          <Part key={id} s={s} id={id}>
            <circle cx={x(q.position)} cy={tip} r={highlighted(s, id) ? 6 : 4} fill={INK} />
            <Label s={s} part={id} x={x(q.position) + (x(q.position) > W / 2 ? -6 : 6)} y={kind === "infrared" ? tip + 16 + i * 14 : tip - 8 - i * 14} text={computedLabel(s.spec, id)} anchor={x(q.position) > W / 2 ? "end" : "start"} size={11} />
          </Part>
        );
      })}
    </Frame>
  );
}

export const spectrumChart = define({
  kind: "spectrum_chart", schema: "visuals/kinds/spectrum_chart", draw: drawSpectrum,
  table: (spec) => ({
    columns: ["visual.table.position", "visual.table.intensity", "visual.table.assignment"],
    rows: (computed<SpectrumComputed>(spec).peaks ?? []).map((q) => [fmt(q.position), fmt(q.intensity), [q.assignment, q.multiplicity, q.integration === undefined ? undefined : `${q.integration}H`].filter(Boolean).join(", ")]),
  }),
});

// --- organic_structure ----------------------------------------------------------------------------------------------

interface Structure {
  formula: string;
  canonical_smiles: string;
  functional_groups: string[];
  highlighted: string[];
  svg: string;
}

/** The tool's sanitised drawing (VSL-23), as elements: its atom and bond ids become the molecule's addressable,
 * non-focusable parts (`mol_1.a3`), so steps and pointing reach them without adding tab stops. */
function drawMoleculeNode(n: SvgNode, prefix: string, key: string, hl: boolean): ReactNode {
  if (n.tag === "title" || n.tag === "desc") return null;
  const attrs = reactProps(n.attrs);
  const part = n.attrs["data-part"];
  if (part !== undefined) attrs["data-part"] = `${prefix}.${part}`;
  if (hl && n.tag !== "text" && attrs.stroke !== undefined && attrs.stroke !== "none") attrs.stroke = "var(--color-diagram-highlight-ring)";
  return createElement(n.tag, { key, ...attrs }, ...n.children.map((c, i) => (typeof c === "string" ? c : drawMoleculeNode(c, prefix, `${key}.${i}`, hl))));
}

function drawOrganic(s: Scene): ReactElement {
  const c = computed<{ structures?: Structure[]; relationship?: string }>(s.spec);
  const structures = c.structures ?? [];
  if (structures.length === 0) return <WordsFallback spec={s.spec} reason="text_only" />;
  const roots: SvgNode[] = [];
  for (const st of structures) {
    try {
      const root = parseSvg(st.svg);
      if (svgProblems(root).length > 0) return <WordsFallback spec={s.spec} reason="refused" />;
      roots.push(root);
    } catch (error) {
      if (error instanceof SvgRefused) return <WordsFallback spec={s.spec} reason="refused" />;
      throw error;
    }
  }
  const panel = W / structures.length;
  return (
    <Frame s={s} width={W} height={H}>
      {structures.map((st, i) => {
        const id = `mol_${i + 1}`;
        const root = must(roots[i]);
        return (
          <g key={id}>
            <Part s={s} id={id}>
              <rect x={i * panel + 8} y={12} width={panel - 16} height={H - 96} fill={areaFill(s, id, PAPER)} {...lineStyle(s, id, LINE, 1)} />
              <svg x={i * panel + 16} y={20} width={panel - 32} height={H - 112} viewBox={root.attrs.viewBox ?? "0 0 300 300"}>
                {root.children.map((child, n) => (typeof child === "string" ? null : drawMoleculeNode(child, id, `n${n}`, highlighted(s, id))))}
              </svg>
              <Label s={s} part={id} x={i * panel + panel / 2} y={H - 64} text={computedLabel(s.spec, id)} anchor="middle" size={13} />
              <Label s={s} part={id} x={i * panel + panel / 2} y={H - 46} text={computedLabel(s.spec, `${id}.formula`, st.formula)} anchor="middle" size={12} formula />
            </Part>
            {st.highlighted.map((group, k) => {
              const gid = `${id}.fg_${pid(group)}`;
              return (
                <Part key={gid} s={s} id={gid}>
                  <rect x={i * panel + 16 + k * 90} y={H - 36} width={84} height={18} rx={9} fill={areaFill(s, gid, TINT)} {...lineStyle(s, gid, STRONG, 1)} />
                  <Label s={s} part={gid} x={i * panel + 58 + k * 90} y={H - 23} text={group} anchor="middle" size={11} />
                </Part>
              );
            })}
          </g>
        );
      })}
      {c.relationship !== undefined && s.labels !== "hidden" && <text x={W / 2} y={H - 4} textAnchor="middle" fontSize={12 * s.scale} fill={MUTED} data-presentation="true">{s.t(`visual.organic.${c.relationship}`)}</text>}
    </Frame>
  );
}

export const organicStructure = define({
  kind: "organic_structure", schema: "visuals/kinds/organic_structure", draw: drawOrganic,
  table: (spec) => ({
    columns: ["visual.table.structure", "visual.table.formula", "visual.table.groups"],
    rows: (computed<{ structures?: Structure[] }>(spec).structures ?? []).map((st, i) => [computedLabel(spec, `mol_${i + 1}`, st.canonical_smiles), st.formula, st.functional_groups.join(", ")]),
  }),
});
