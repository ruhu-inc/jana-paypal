// The S3 simulation models of the installed O level subjects (VSL-08a, VSL-46; D38: TypeScript models cross-checked
// against the verification tools): gas laws, function transformations, electrolysis, rates and equilibrium. Like the
// S2 models they are pure: the same values and steps give the same states, with no clock, random source or network.
// Each declares bounded parameters and readouts with units and precision; pack figures (the Faraday constant, the
// molar gas volume, relative atomic masses) arrive from the resolution, never from here. The readouts are checked in
// CI against math_evaluate, math_solve and math_plot_points at the sample points of fixtures/sim_fidelity.json.
import { evaluate, parse } from "../expr.js";
import { must } from "../must.js";
import { num, type SimModel, type Values } from "./models.js";

/** The molar gas constant the simplified models use, J mol⁻¹ K⁻¹ (the resolution passes the same value to the tools). */
export const GAS_CONSTANT = 8.314;
/** How much a catalyst speeds both directions of a reaction in these schematic models (VSL-46: equally, both ways). */
export const CATALYST_FACTOR = 4;

/** Screen seconds a full run takes at normal speed: the reaction-time scale is fitted to it from the starting values. */
export const RUN_SECONDS = 8;

/** Deterministic "scatter" for schematic particles, in [0, 1): a fixed hash of the particle and a salt, not a random
 * source. Integer arithmetic only, so every platform and engine draws the same particles (VSL-35 golden renders): the
 * `sin(x) * 43758.5453` hash it replaces moved them when `Math.sin` differed in its last bit. */
export function scatter(i: number, salt: number): number {
  let h = Math.imul(i + 1, 0x9e3779b1) ^ Math.imul(salt + 1, 0x85ebca6b);
  h = Math.imul(h ^ (h >>> 16), 0x7feb352d);
  h = Math.imul(h ^ (h >>> 15), 0x846ca68b);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

export interface Particles {
  x: number[];
  y: number[];
  vx: number[];
  vy: number[];
}

export function particles(n: number): Particles {
  const p: Particles = { x: [], y: [], vx: [], vy: [] };
  for (let i = 0; i < n; i += 1) {
    const a = scatter(i, 3) * 2 * Math.PI;
    p.x.push(0.05 + 0.9 * scatter(i, 1));
    p.y.push(0.05 + 0.9 * scatter(i, 2));
    p.vx.push(Math.cos(a));
    p.vy.push(Math.sin(a));
  }
  return p;
}

/** Particles moved inside the unit box at `speed` box widths per second, bouncing off the walls. */
export function moved(p: Particles, speed: number, dt: number): Particles {
  const out: Particles = { x: [], y: [], vx: [], vy: [] };
  const bounce = (pos: number, vel: number): [number, number] => {
    let x = pos + vel * speed * dt;
    let v = vel;
    if (x < 0) { x = -x; v = -v; }
    if (x > 1) { x = 2 - x; v = -v; }
    return [Math.min(1, Math.max(0, x)), v];
  };
  for (let i = 0; i < p.x.length; i += 1) {
    const [x, vx] = bounce(must(p.x[i]), must(p.vx[i]));
    const [y, vy] = bounce(must(p.y[i]), must(p.vy[i]));
    out.x.push(x); out.y.push(y); out.vx.push(vx); out.vy.push(vy);
  }
  return out;
}

// --- gas_laws_sim (math_evaluate: p = nRT/V, ideal gas, isochoric or isothermal) ----------------------------------

export interface GasState {
  particles: Particles;
}

/** Pressure in kPa of `n` mol at `t` K in `v` cm³. */
export function gasPressure(n: number, t: number, v: number): number {
  return (n * GAS_CONSTANT * t) / (v * 1e-6) / 1000;
}

/** `kelvinOffset` is the pack's (phy.kelvin_offset): the Celsius reading is shown beside the kelvin one. */
export function gasLaws(amount: number, kelvinOffset: number): SimModel<GasState> {
  const read = (p: Values) => ({
    "out.pressure": gasPressure(amount, num(p, "temperature"), num(p, "volume")),
    "out.temperature": num(p, "temperature"),
    "out.celsius": num(p, "temperature") - kelvinOffset,
    "out.volume": num(p, "volume"),
  });
  return {
    kind: "gas_laws_sim",
    params: [
      { id: "temperature", labelKey: "sim.gas.temperature", unit: "K", kind: "range", min: 100, max: 600, step: 1, default: 293 },
      { id: "volume", labelKey: "sim.gas.volume", unit: "cm³", kind: "range", min: 20, max: 500, step: 1, default: 100 },
    ],
    readouts: [
      { id: "out.pressure", labelKey: "sim.gas.pressure", unit: "kPa", sf: 3 },
      { id: "out.temperature", labelKey: "sim.gas.temperature", unit: "K", sf: 3 },
      { id: "out.celsius", labelKey: "sim.gas.temperature", unit: "°C", sf: 3 },
      { id: "out.volume", labelKey: "sim.gas.volume", unit: "cm³", sf: 3 },
    ],
    init: () => ({ particles: particles(24) }),
    // Particle speed goes as the square root of the kelvin temperature; the box is redrawn to the volume.
    next: (state, p, dt) => ({ particles: moved(state.particles, 0.35 * Math.sqrt(num(p, "temperature") / 300), dt) }),
    read: (_state, p) => read(p),
    done: () => false,
    endless: true,
    adjust: (state) => state,
    table(p) {
      const isochoric = p.control !== "volume";
      const rows: number[][] = [];
      for (let i = 0; i <= 8; i += 1) {
        if (isochoric) {
          const t = 200 + 50 * i;
          rows.push([t, gasPressure(amount, t, num(p, "volume"))]);
        } else {
          const v = 50 + 50 * i;
          rows.push([v, gasPressure(amount, num(p, "temperature"), v)]);
        }
      }
      return { columns: [isochoric ? "sim.gas.temperature" : "sim.gas.volume", "sim.gas.pressure"], rows };
    },
  };
}

// --- function_transform_sim (math_plot_points: y = a f(b(x + c)) + d) ----------------------------------------------

export interface Slider {
  min: number;
  max: number;
  step: number;
  value: number;
}

export function transformed(f: (x: number) => number, p: Values, x: number): number {
  return num(p, "a") * f(num(p, "b") * (x + num(p, "c"))) + num(p, "d");
}

/** The tracked point: where (x0, f(x0)) of the parent graph goes, ((x0 / b) − c, a f(x0) + d). */
export function trackedPoint(f: (x: number) => number, x0: number, p: Values): [number, number] {
  const b = num(p, "b");
  return [b === 0 ? NaN : x0 / b - num(p, "c"), num(p, "a") * f(x0) + num(p, "d")];
}

const SLIDER_KEYS = { a: "sim.function.a", b: "sim.function.b", c: "sim.function.c", d: "sim.function.d" } as const;

export function functionTransform(expression: string, xRange: [number, number], sliders: Record<"a" | "b" | "c" | "d", Slider>, x0 = 0): SimModel<null> {
  const node = parse(expression);
  const f = (x: number) => evaluate(node, x);
  const [lo, hi] = xRange;
  return {
    kind: "function_transform_sim",
    params: (["a", "b", "c", "d"] as const).map((id) => ({ id, labelKey: SLIDER_KEYS[id], unit: "", kind: "range" as const, min: sliders[id].min, max: sliders[id].max, step: sliders[id].step, default: sliders[id].value })),
    readouts: [
      { id: "out.point_x", labelKey: "sim.function.point_x", unit: "", sf: 3, trim: true },
      { id: "out.point_y", labelKey: "sim.function.point_y", unit: "", sf: 3, trim: true },
    ],
    init: () => null,
    next: (state) => state,
    read: (_state, p) => {
      const [px, py] = trackedPoint(f, x0, p);
      return { "out.point_x": px, "out.point_y": py };
    },
    // Nothing moves on its own: every change is its final state at once.
    done: () => true,
    table(p) {
      const rows: number[][] = [];
      for (let i = 0; i <= 120; i += 1) {
        const x = lo + ((hi - lo) * i) / 120;
        rows.push([x, transformed(f, p, x)]);
      }
      return { columns: ["sim.function.x", "sim.function.y"], rows };
    },
  };
}

// --- electrolysis_sim (chem_balance for each half-equation; amounts from Q = It and the pack's Faraday constant) ----

export interface ElectrodeProduct {
  /** The product's name key (`sim.electrolysis.product.<id>`) and formula. */
  id: string;
  formula: string;
  /** A gas is measured by volume at r.t.p.; a deposit or a liquid by mass; `dissolves` is an anode that wears away. */
  measure: "gas" | "mass" | "dissolves";
  /** Electrons per formula unit of product, from the preset's half-equation. */
  electrons: number;
  half_equation: string;
}

export interface ElectrolysisPreset {
  id: string;
  /** The cell's catalogue key (`sim.electrolysis.cell.<key>`). */
  key: string;
  electrolyte: string;
  phase: "molten" | "aqueous";
  electrodes: "inert" | "copper";
  ions: { formula: string; charge: number }[];
  cathode: ElectrodeProduct;
  anode: ElectrodeProduct;
}

export interface ElectrolysisConstants {
  faraday: number;
  /** Molar gas volume at r.t.p., cm³ per mole. */
  vm_cm3: number;
  /** Molar masses (g/mol) of the products measured by mass, by formula. */
  molar_mass: Record<string, number>;
}

export interface ElectrolysisState {
  time: number;
}

/** Amount (mol) of an electrode's product after `charge` coulombs. */
function amountOf(product: ElectrodeProduct, charge: number, faraday: number): number {
  return charge / (faraday * product.electrons);
}

export function electrolysisReadings(preset: ElectrolysisPreset, k: ElectrolysisConstants, current: number, time: number): Record<string, number> {
  const charge = current * time;
  const side = (product: ElectrodeProduct) => {
    const n = amountOf(product, charge, k.faraday);
    const mass = product.measure === "gas" ? 0 : n * (k.molar_mass[product.formula] ?? NaN);
    return { gas: product.measure === "gas" ? n * k.vm_cm3 : 0, mass: product.measure === "dissolves" ? -mass : mass };
  };
  const cathode = side(preset.cathode);
  const anode = side(preset.anode);
  return {
    "out.time": time,
    "out.charge": charge,
    "out.cathode_mass": cathode.mass,
    "out.cathode_gas": cathode.gas,
    "out.anode_mass": anode.mass,
    "out.anode_gas": anode.gas,
  };
}

export function electrolysis(preset: ElectrolysisPreset, k: ElectrolysisConstants): SimModel<ElectrolysisState> {
  return {
    kind: "electrolysis_sim",
    params: [
      { id: "current", labelKey: "sim.electrolysis.current", unit: "A", kind: "range", min: 0.1, max: 5, step: 0.1, default: 1 },
      { id: "ions", labelKey: "sim.electrolysis.ions", unit: "", kind: "toggle", default: true },
    ],
    readouts: [
      { id: "out.time", labelKey: "sim.electrolysis.time", unit: "s", sf: 3 },
      { id: "out.charge", labelKey: "sim.electrolysis.charge", unit: "C", sf: 3 },
      { id: "out.cathode_mass", labelKey: "sim.electrolysis.cathode_mass", unit: "g", sf: 3 },
      { id: "out.cathode_gas", labelKey: "sim.electrolysis.cathode_gas", unit: "cm³", sf: 3 },
      { id: "out.anode_mass", labelKey: "sim.electrolysis.anode_mass", unit: "g", sf: 3 },
      { id: "out.anode_gas", labelKey: "sim.electrolysis.anode_gas", unit: "cm³", sf: 3 },
    ],
    init: () => ({ time: 0 }),
    next: (state, p, dt) => ({ time: Math.min(num(p, "duration"), state.time + dt * num(p, "timescale")) }),
    read: (state, p) => electrolysisReadings(preset, k, num(p, "current"), state.time),
    done: (state, p) => state.time >= num(p, "duration"),
    table(p) {
      const rows: number[][] = [];
      for (let i = 0; i <= 6; i += 1) {
        const t = (num(p, "duration") * i) / 6;
        const r = electrolysisReadings(preset, k, num(p, "current"), t);
        rows.push([t, must(r["out.cathode_mass"]), must(r["out.cathode_gas"]), must(r["out.anode_mass"]), must(r["out.anode_gas"])]);
      }
      return { columns: ["sim.electrolysis.time", "sim.electrolysis.cathode_mass", "sim.electrolysis.cathode_gas", "sim.electrolysis.anode_mass", "sim.electrolysis.anode_gas"], rows };
    },
  };
}

// --- rates_sim (math_evaluate: k = A exp(−Ea/RT), simplified first order, product = c0 (1 − e^(−kt))) -------------

export interface RatesSetup {
  pre_exponential_s: number;
  activation_energy_j_mol: number;
  catalysed_activation_energy_j_mol: number;
}

export interface RatesState {
  time: number;
}

/** kt at which a run ends: the product is then within one part in a million of all it can become. */
export const RATES_END_KT = Math.log(1e6);

export function rateConstant(s: RatesSetup, temperature: number, catalyst: boolean): number {
  const ea = catalyst ? s.catalysed_activation_energy_j_mol : s.activation_energy_j_mol;
  return s.pre_exponential_s * Math.exp(-ea / (GAS_CONSTANT * temperature));
}

export function ratesReadings(s: RatesSetup, p: Values, time: number): Record<string, number> {
  const k = rateConstant(s, num(p, "temperature"), p.catalyst === true);
  const c0 = num(p, "concentration");
  const left = c0 * Math.exp(-k * time);
  return { "out.time": time, "out.rate_constant": k, "out.rate": k * left, "out.product": c0 - left };
}

export function rates(s: RatesSetup): SimModel<RatesState> {
  const end = (p: Values) => RATES_END_KT / rateConstant(s, num(p, "temperature"), p.catalyst === true);
  return {
    kind: "rates_sim",
    params: [
      { id: "temperature", labelKey: "sim.rates.temperature", unit: "K", kind: "range", min: 273, max: 373, step: 1, default: 293 },
      { id: "concentration", labelKey: "sim.rates.concentration", unit: "mol/dm³", kind: "range", min: 0.1, max: 2, step: 0.1, default: 1 },
      { id: "catalyst", labelKey: "sim.rates.catalyst", unit: "", kind: "toggle", default: false },
    ],
    readouts: [
      { id: "out.time", labelKey: "sim.rates.time", unit: "s", sf: 3 },
      { id: "out.rate_constant", labelKey: "sim.rates.rate_constant", unit: "/s", sf: 3 },
      { id: "out.rate", labelKey: "sim.rates.rate", unit: "mol/dm³/s", sf: 3 },
      { id: "out.product", labelKey: "sim.rates.product", unit: "mol/dm³", sf: 3 },
    ],
    init: () => ({ time: 0 }),
    next: (state, p, dt) => ({ time: Math.min(end(p), state.time + dt * num(p, "timescale")) }),
    read: (state, p) => ratesReadings(s, p, state.time),
    done: (state, p) => state.time >= end(p),
    table(p) {
      const rows: number[][] = [];
      for (let i = 0; i <= 8; i += 1) {
        const t = (end(p) * i) / 8;
        rows.push([t, must(ratesReadings(s, p, t)["out.product"])]);
      }
      return { columns: ["sim.rates.time", "sim.rates.product"], rows };
    },
  };
}

// --- equilibrium_sim (math_solve for the equilibrium extent; mass action, van't Hoff for Kc(T)) -------------------

export interface EquilibriumSpecies {
  id: string;
  formula: string;
  state: "gas" | "aqueous" | "solid" | "liquid";
  coefficient: number;
  side: "reactant" | "product";
}

export interface EquilibriumSetup {
  species: EquilibriumSpecies[];
  initial: Record<string, number>;
  reference_temperature_k: number;
  reference_kc: number;
  forward_enthalpy_j_mol: number;
  volume_l: number;
  forward_rate_constant: number;
}

export interface EquilibriumState {
  time: number;
  /** Concentrations (mol/dm³) of the species that take part (gases and aqueous), in the reaction's order. */
  c: number[];
}

/** The van't Hoff equilibrium constant at `t` from the reference value, with a constant enthalpy. */
export function kcAt(s: EquilibriumSetup, t: number): number {
  return s.reference_kc * Math.exp((-s.forward_enthalpy_j_mol / GAS_CONSTANT) * (1 / t - 1 / s.reference_temperature_k));
}

// Pure solids and liquids have activity 1 and take no part in the rate or the quotient.
function active(s: EquilibriumSetup): EquilibriumSpecies[] {
  return s.species.filter((x) => x.state === "gas" || x.state === "aqueous");
}

function signed(x: EquilibriumSpecies): number {
  return x.side === "reactant" ? -x.coefficient : x.coefficient;
}

function product(species: EquilibriumSpecies[], c: number[], side: "reactant" | "product"): number {
  let out = 1;
  species.forEach((x, i) => {
    if (x.side === side) out *= Math.max(0, must(c[i])) ** x.coefficient;
  });
  return out;
}

export function quotient(s: EquilibriumSetup, c: number[]): number {
  const sp = active(s);
  const below = product(sp, c, "reactant");
  return below === 0 ? Infinity : product(sp, c, "product") / below;
}

/** Net forward rate (mol dm⁻³ s⁻¹ of extent) at concentrations `c`, and its two directions. */
function netRate(s: EquilibriumSetup, p: Values, c: number[]): { net: number; forward: number; backward: number } {
  const factor = p.catalyst === true ? CATALYST_FACTOR : 1;
  const kf = s.forward_rate_constant * factor;
  const kr = kf / kcAt(s, num(p, "temperature"));
  const sp = active(s);
  const forward = kf * product(sp, c, "reactant");
  const backward = kr * product(sp, c, "product");
  return { net: forward - backward, forward, backward };
}

/** The extents the concentrations allow: no species ever goes below zero. */
function bounds(s: EquilibriumSetup, c: number[]): [number, number] {
  let lo = -Infinity;
  let hi = Infinity;
  active(s).forEach((x, i) => {
    const limit = must(c[i]) / x.coefficient;
    if (x.side === "reactant") hi = Math.min(hi, limit);
    else lo = Math.max(lo, -limit);
  });
  return [lo, hi];
}

function shifted(s: EquilibriumSetup, c: number[], xi: number): number[] {
  return active(s).map((x, i) => Math.max(0, must(c[i]) + signed(x) * xi));
}

/** The equilibrium extent from `c`, by bisection on the net rate (which falls as the extent grows). */
export function equilibriumExtent(s: EquilibriumSetup, p: Values, c: number[]): number {
  let [lo, hi] = bounds(s, c);
  for (let i = 0; i < 200; i += 1) {
    const mid = (lo + hi) / 2;
    if (netRate(s, p, shifted(s, c, mid)).net > 0) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

/** The starting concentrations for the values: the initial mixture, with gases scaled to the volume and additions. */
export function equilibriumStart(s: EquilibriumSetup, p: Values): number[] {
  const scale = s.volume_l / (num(p, "volume") || s.volume_l);
  const sp = active(s);
  const firstReactant = sp.findIndex((x) => x.side === "reactant");
  const firstProduct = sp.findIndex((x) => x.side === "product");
  return sp.map((x, i) => {
    let v = s.initial[x.id] ?? 0;
    if (x.state === "gas") v *= scale;
    if (i === firstReactant) v += num(p, "add_reactant") || 0;
    if (i === firstProduct) v += num(p, "add_product") || 0;
    return v;
  });
}

/** Screen-to-reaction time scale: a run from `values` reaches equilibrium in about RUN_SECONDS at normal speed. */
export function equilibriumTimescale(s: EquilibriumSetup, p: Values): number {
  const c = equilibriumStart(s, p);
  const at = shifted(s, c, equilibriumExtent(s, p, c));
  const h = 1e-6 * Math.max(1e-6, ...at);
  const slope = Math.abs((netRate(s, p, shifted(s, at, h)).net - netRate(s, p, shifted(s, at, -h)).net) / (2 * h));
  return slope > 0 ? Math.log(1e7) / slope / RUN_SECONDS : 1;
}

const EQUILIBRIUM_TOLERANCE = 1e-7;
const MAX_SUBSTEPS = 64;

export function equilibrium(s: EquilibriumSetup, controls: readonly string[]): SimModel<EquilibriumState> {
  const sp = active(s);
  const reactant = sp.find((x) => x.side === "reactant");
  const productSp = sp.find((x) => x.side === "product");
  const t0 = s.reference_temperature_k;
  const params = [
    { id: "temperature", labelKey: "sim.equilibrium.temperature", unit: "K", kind: "range" as const, min: Math.max(1, Math.round(t0 - 200)), max: Math.round(t0 + 200), step: 1, default: t0 },
    { id: "volume", labelKey: "sim.equilibrium.volume", unit: "dm³", kind: "range" as const, min: 0.5, max: 5, step: 0.1, default: s.volume_l },
    { id: "add_reactant", labelKey: "sim.equilibrium.add_reactant", unit: "mol/dm³", kind: "range" as const, min: 0, max: 1, step: 0.1, default: 0 },
    { id: "add_product", labelKey: "sim.equilibrium.add_product", unit: "mol/dm³", kind: "range" as const, min: 0, max: 1, step: 0.1, default: 0 },
    { id: "catalyst", labelKey: "sim.equilibrium.catalyst", unit: "", kind: "toggle" as const, default: false },
  ].filter((q) => controls.includes(q.id) && (q.id !== "add_reactant" || reactant !== undefined) && (q.id !== "add_product" || productSp !== undefined));
  const read = (state: EquilibriumState, p: Values): Record<string, number> => {
    const out: Record<string, number> = { "out.time": state.time, "out.kc": kcAt(s, num(p, "temperature")), "out.q": quotient(s, state.c) };
    sp.forEach((x, i) => {
      out[`out.c_${x.id}`] = must(state.c[i]);
    });
    return out;
  };
  const converged = (state: EquilibriumState, p: Values) => {
    const r = netRate(s, p, state.c);
    return Math.abs(r.net) <= EQUILIBRIUM_TOLERANCE * Math.max(r.forward, r.backward, 1e-300);
  };
  // One fixed step of reaction time h: RK4 on the extent, with bounded substeps when the rate changes fast, and every
  // concentration derived from the one extent, so matter is conserved exactly and nothing goes below zero.
  const step = (c: number[], p: Values, h: number): number[] => {
    const rate = (xi: number) => netRate(s, p, shifted(s, c, xi)).net;
    const [lo, hi] = bounds(s, c);
    const e = 1e-9 * Math.max(1e-9, ...c);
    const slope = Math.abs((rate(e) - rate(-e)) / (2 * e));
    const n = Math.min(MAX_SUBSTEPS, Math.max(1, Math.ceil(h * slope * 2)));
    const dh = h / n;
    let xi = 0;
    for (let i = 0; i < n; i += 1) {
      const k1 = rate(xi);
      const k2 = rate(xi + (dh * k1) / 2);
      const k3 = rate(xi + (dh * k2) / 2);
      const k4 = rate(xi + dh * k3);
      xi = Math.min(hi, Math.max(lo, xi + (dh * (k1 + 2 * k2 + 2 * k3 + k4)) / 6));
    }
    return shifted(s, c, xi);
  };
  return {
    kind: "equilibrium_sim",
    params,
    readouts: [
      { id: "out.time", labelKey: "sim.equilibrium.time", unit: "s", sf: 3 },
      ...sp.map((x) => ({ id: `out.c_${x.id}`, labelKey: `text:[${x.formula}]`, unit: "mol/dm³", sf: 3 })),
      { id: "out.kc", labelKey: "sim.equilibrium.kc", unit: "", sf: 3 },
      { id: "out.q", labelKey: "sim.equilibrium.q", unit: "", sf: 3 },
    ],
    init: (p) => ({ time: 0, c: equilibriumStart(s, p) }),
    next: (state, p, dt) => (converged(state, p) ? state : { time: state.time + dt * num(p, "timescale"), c: step(state.c, p, dt * num(p, "timescale")) }),
    read,
    done: converged,
    // A change disturbs the mixture as it stands: gases follow the volume, an addition adds; nothing starts again.
    adjust(state, from, to) {
      const scale = (num(from, "volume") || s.volume_l) / (num(to, "volume") || s.volume_l);
      const c = sp.map((x, i) => {
        let v = must(state.c[i]);
        if (x.state === "gas") v *= scale;
        if (x === reactant) v += (num(to, "add_reactant") || 0) - (num(from, "add_reactant") || 0);
        if (x === productSp) v += (num(to, "add_product") || 0) - (num(from, "add_product") || 0);
        return Math.max(0, v);
      });
      return { time: state.time, c };
    },
    table(p) {
      // The run from these values, sampled at nine times to equilibrium.
      const dt = 1 / 60;
      const states: EquilibriumState[] = [];
      let state: EquilibriumState = { time: 0, c: equilibriumStart(s, p) };
      for (let i = 0; i < 100_000 && !converged(state, p); i += 1) {
        states.push(state);
        state = { time: state.time + dt * num(p, "timescale"), c: step(state.c, p, dt * num(p, "timescale")) };
      }
      states.push(state);
      const rows = Array.from({ length: 9 }, (_, i) => must(states[Math.round(((states.length - 1) * i) / 8)])).map((x) => [x.time, ...x.c]);
      return { columns: ["sim.equilibrium.time", ...sp.map((x) => `text:[${x.formula}]`)], rows };
    },
  };
}
