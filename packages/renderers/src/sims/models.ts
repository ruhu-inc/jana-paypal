// Simulation models (VSL-30, VSL-31; D38 open: TypeScript models cross-checked against the verification tools). Each
// model declares its parameters (id, label key, unit, range, step, default) and readouts (unit and display
// precision), and advances by a pure step function `next(state, params, dt)` at a fixed time step. Nothing here reads
// a clock, a random source or the network: the same inputs give the same states. Readouts are cross-checked in CI
// against phys_projectile, phys_circuit and chem_acid_base at the sample points of fixtures/sim_fidelity.json.

import { must } from "../must.js";
export type Values = Record<string, number | string | boolean>;

export interface SimParam {
  id: string;
  labelKey: string;
  unit: string;
  kind: "range" | "choice" | "toggle";
  min?: number;
  max?: number;
  step?: number;
  choices?: readonly { value: string; labelKey: string }[];
  default: number | string | boolean;
  /** Shown only while each named control has the given value (a launch angle only for an angled launch). */
  when?: Readonly<Record<string, string>>;
}

/** Whether a control applies at the current values (its `when`, if any, holds). */
export function applies(p: SimParam, values: Values): boolean {
  return Object.entries(p.when ?? {}).every(([id, v]) => values[id] === v);
}

export interface SimReadout {
  id: string;
  labelKey: string;
  unit: string;
  /** Significant figures shown. */
  sf: number;
  /** Shown without trailing zeros (a coordinate reads -3, not -3.00). */
  trim?: boolean;
}

export interface SimModel<S> {
  kind: string;
  params: readonly SimParam[];
  readouts: readonly SimReadout[];
  init(p: Values): S;
  next(state: S, p: Values, dt: number): S;
  read(state: S, p: Values): Record<string, number>;
  done(state: S, p: Values): boolean;
  /** The text mode (VSL-30): a table of readings for the current parameters, without animation. */
  table(p: Values): { columns: string[]; rows: number[][] };
  /** A change of control that carries on from the current state (an equilibrium disturbed, a gas heated) instead of
   * starting again; without it a change starts the model again from the new values. */
  adjust?(state: S, from: Values, to: Values): S;
  /** A model that never ends (particles that keep moving): its end, under reduced motion, is its start. */
  endless?: boolean;
}

/** Label keys may be literal text (`text:[HI]`, a species from the request); every other key is the catalogue's. */
export function literal(t: (key: string, args?: Record<string, string | number>) => string): (key: string, args?: Record<string, string | number>) => string {
  return (key, args) => (key.startsWith("text:") ? key.slice(5) : t(key, args));
}

/** What a simulation renderer builds from its resolved spec: the model, its starting values and the locked controls. */
export interface SimSetup<S> {
  model: SimModel<S>;
  start: Values;
  locked: string[];
}

export function num(p: Values, id: string): number {
  const v = p[id];
  return typeof v === "number" ? v : Number(v);
}

/** A number at `sf` significant figures, as displayed. */
export function sig(v: number, sf: number): string {
  if (!Number.isFinite(v)) return "—";
  if (!Number.isInteger(sf) || sf < 1 || sf > 15) throw new RangeError("Invalid significant figures");
  const sign = v < 0 ? "-" : "";
  if (v === 0) return sf === 1 ? "0" : `0.${"0".repeat(sf - 1)}`;
  const [mantissa = "", power = "0"] = Math.abs(v).toString().split("e");
  const whole = mantissa.split(".")[0] ?? "";
  const raw = mantissa.replace(".", ""), first = raw.search(/[1-9]/);
  let exponent = whole.length - first - 1 + Number(power);
  const digits = raw.slice(first).padEnd(sf + 1, "0");
  let kept = digits.slice(0, sf);
  // Decimal half-up, matching the verification tools, without a binary floating-point tie changing the result.
  if (Number(digits[sf]) >= 5) kept = String(BigInt(kept) + 1n);
  if (kept.length > sf) { exponent++; kept = kept.slice(0, sf); }
  if (exponent < -6 || exponent >= sf) return `${sign}${kept[0]}${sf > 1 ? `.${kept.slice(1)}` : ""}e${exponent >= 0 ? "+" : ""}${exponent}`;
  if (exponent < 0) return `${sign}0.${"0".repeat(-exponent - 1)}${kept}`;
  const point = exponent + 1;
  return sign + (point < sf ? `${kept.slice(0, point)}.${kept.slice(point)}` : kept);
}

/** A value with its unit; degrees sit against the number (30°), every other unit after a space (20 m/s). */
export function withUnit(value: string | number, unit: string): string {
  return unit === "°" ? `${value}°` : `${value} ${unit}`.trim();
}

/** A readout's value as displayed: `sf` significant figures, a large value as a whole number (1380, not 1.38e+3),
 * and trailing zeros dropped where the readout says so. */
export function shownReadout(v: number, r: SimReadout): string {
  const text = sig(v, r.sf);
  if (/e\+/.test(text) && Math.abs(v) < 1e9) return String(Number(text));
  if (r.trim !== true || !text.includes(".") || text.includes("e")) return text;
  const out = text.replace(/0+$/, "").replace(/\.$/, "");
  return out === "-0" ? "0" : out;
}

// --- projectile_sim (phys_projectile, VER-17: horizontal, vertical and angled launches at O level) -----------------

export interface ProjectileState {
  t: number;
  landed: boolean;
}

function launch(p: Values, g: number): { ux: number; uy: number; h: number; tFlight: number } {
  const speed = num(p, "speed");
  const h = num(p, "height");
  // An angled launch is at `angle` degrees above the horizontal: ux = u cos θ, uy = u sin θ, as phys_projectile.
  const theta = p.mode === "angled" ? (num(p, "angle") * Math.PI) / 180 : p.mode === "vertical" ? Math.PI / 2 : 0;
  const ux = p.mode === "vertical" ? 0 : speed * Math.cos(theta);
  const uy = p.mode === "horizontal" ? 0 : speed * Math.sin(theta);
  const tFlight = (uy + Math.sqrt(uy * uy + 2 * g * h)) / g;
  return { ux, uy, h, tFlight };
}

export function projectile(g: number): SimModel<ProjectileState> {
  return {
    kind: "projectile_sim",
    params: [
      { id: "mode", labelKey: "sim.projectile.mode", unit: "", kind: "choice", choices: [{ value: "horizontal", labelKey: "sim.projectile.mode.horizontal" }, { value: "vertical", labelKey: "sim.projectile.mode.vertical" }, { value: "angled", labelKey: "sim.projectile.mode.angled" }], default: "horizontal" },
      { id: "speed", labelKey: "sim.projectile.speed", unit: "m/s", kind: "range", min: 1, max: 40, step: 1, default: 12 },
      { id: "angle", labelKey: "sim.projectile.angle", unit: "°", kind: "range", min: 5, max: 85, step: 5, default: 45, when: { mode: "angled" } },
      { id: "height", labelKey: "sim.projectile.height", unit: "m", kind: "range", min: 0, max: 100, step: 1, default: 20 },
    ],
    readouts: [
      { id: "out.time", labelKey: "sim.projectile.time", unit: "s", sf: 3 },
      { id: "out.range", labelKey: "sim.projectile.range", unit: "m", sf: 3 },
      { id: "out.max_height", labelKey: "sim.projectile.max_height", unit: "m", sf: 3 },
      { id: "out.x", labelKey: "sim.projectile.x", unit: "m", sf: 3 },
      { id: "out.y", labelKey: "sim.projectile.y", unit: "m", sf: 3 },
    ],
    init: () => ({ t: 0, landed: false }),
    next(state, p, dt) {
      const { tFlight } = launch(p, g);
      const t = Math.min(state.t + dt, tFlight);
      return { t, landed: t >= tFlight };
    },
    read(state, p) {
      const { ux, uy, h, tFlight } = launch(p, g);
      const top = uy > 0 ? h + (uy * uy) / (2 * g) : h;
      return {
        "out.time": tFlight, "out.range": ux * tFlight, "out.max_height": top,
        "out.x": ux * state.t, "out.y": Math.max(0, h + uy * state.t - (g * state.t * state.t) / 2),
      };
    },
    done: (state) => state.landed,
    table(p) {
      const { ux, uy, h, tFlight } = launch(p, g);
      const rows: number[][] = [];
      for (let i = 0; i <= 10; i += 1) {
        const t = (tFlight * i) / 10;
        rows.push([t, ux * t, Math.max(0, h + uy * t - (g * t * t) / 2)]);
      }
      return { columns: ["sim.projectile.t", "sim.projectile.x", "sim.projectile.y"], rows };
    },
  };
}

// --- circuit_sim (phys_circuit, VER-16) ----------------------------------------------------------------------------

export interface CircuitState {
  phase: number;
}

export function circuitReadings(p: Values, resistorIds: readonly string[]): Record<string, number> {
  const emf = num(p, "emf");
  const rs = resistorIds.map((id) => num(p, id));
  const series = p.arrangement !== "parallel";
  const closed = p.switch !== false;
  const req = series ? rs.reduce((a, b) => a + b, 0) : 1 / rs.reduce((a, b) => a + 1 / b, 0);
  const current = closed ? emf / req : 0;
  const out: Record<string, number> = { "out.current": current, "out.resistance": req };
  resistorIds.forEach((id, i) => {
    out[`out.pd_${id}`] = closed ? (series ? current * must(rs[i]) : emf) : 0;
  });
  return out;
}

export function circuitSim(resistorIds: readonly string[]): SimModel<CircuitState> {
  return {
    kind: "circuit_sim",
    params: [
      { id: "emf", labelKey: "sim.circuit.emf", unit: "V", kind: "range", min: 1.5, max: 24, step: 0.5, default: 6 },
      { id: "arrangement", labelKey: "sim.circuit.arrangement", unit: "", kind: "choice", choices: [{ value: "series", labelKey: "sim.circuit.arrangement.series" }, { value: "parallel", labelKey: "sim.circuit.arrangement.parallel" }], default: "series" },
      { id: "switch", labelKey: "sim.circuit.switch", unit: "", kind: "toggle", default: true },
      ...resistorIds.map((id) => ({ id, labelKey: "sim.circuit.resistance", unit: "Ω", kind: "range" as const, min: 1, max: 100, step: 1, default: 10 })),
    ],
    readouts: [
      { id: "out.current", labelKey: "sim.circuit.current", unit: "A", sf: 3 },
      { id: "out.resistance", labelKey: "sim.circuit.total_resistance", unit: "Ω", sf: 3 },
      ...resistorIds.map((id) => ({ id: `out.pd_${id}`, labelKey: "sim.circuit.pd", unit: "V", sf: 3 })),
    ],
    init: () => ({ phase: 0 }),
    next: (state, p, dt) => ({ phase: p.switch === false ? state.phase : (state.phase + dt * Math.min(1, must(circuitReadings(p, resistorIds)["out.current"]))) % 1 }),
    read: (_state, p) => circuitReadings(p, resistorIds),
    done: () => false,
    table(p) {
      const r = circuitReadings(p, resistorIds);
      return { columns: ["sim.circuit.component", "sim.circuit.current", "sim.circuit.pd"], rows: resistorIds.map((id, i) => [i + 1, p.arrangement === "parallel" ? must(r[`out.pd_${id}`]) / num(p, id) : must(r["out.current"]), must(r[`out.pd_${id}`])]) };
    },
  };
}

// --- titration_sim (chem_acid_base, VER-21: strong acid and strong base) -------------------------------------------

export interface TitrationState {
  volume: number;
}

/** pH after `vb` cm³ of base is added to `va` cm³ of acid (strong and strong, Kw = 1.0e-14 at 25 °C). */
export function titrationPh(ca: number, va: number, cb: number, vb: number): number {
  const acid = (ca * va) / 1000;
  const base = (cb * vb) / 1000;
  const litres = (va + vb) / 1000;
  if (Math.abs(acid - base) <= 1e-15) return 7;
  if (acid > base) return -Math.log10((acid - base) / litres);
  return 14 + Math.log10((base - acid) / litres);
}

export function endPoint(p: Values): number {
  return (num(p, "acid_concentration") * num(p, "acid_volume")) / num(p, "base_concentration");
}

export function titrationSim(rate: number): SimModel<TitrationState> {
  return {
    kind: "titration_sim",
    params: [
      { id: "volume", labelKey: "sim.titration.volume_added", unit: "cm³", kind: "range", min: 0, max: 50, step: 0.1, default: 0 },
      { id: "tap", labelKey: "sim.titration.tap", unit: "", kind: "toggle", default: false },
      { id: "indicator", labelKey: "sim.titration.indicator", unit: "", kind: "choice", choices: [{ value: "phenolphthalein", labelKey: "sim.titration.indicator.phenolphthalein" }, { value: "methyl_orange", labelKey: "sim.titration.indicator.methyl_orange" }], default: "phenolphthalein" },
    ],
    readouts: [
      { id: "out.volume", labelKey: "sim.titration.volume_added", unit: "cm³", sf: 3 },
      { id: "out.ph", labelKey: "sim.titration.ph", unit: "", sf: 3 },
      { id: "out.end_point", labelKey: "sim.titration.end_point", unit: "cm³", sf: 3 },
    ],
    init: (p) => ({ volume: num(p, "volume") || 0 }),
    next: (state, p, dt) => ({ volume: p.tap === true ? Math.min(2 * endPoint(p), state.volume + rate * dt) : state.volume }),
    read: (state, p) => ({
      "out.volume": state.volume,
      "out.ph": titrationPh(num(p, "acid_concentration"), num(p, "acid_volume"), num(p, "base_concentration"), state.volume),
      "out.end_point": endPoint(p),
    }),
    done: (state, p) => state.volume >= 2 * endPoint(p),
    table(p) {
      const end = endPoint(p);
      const rows: number[][] = [];
      for (let i = 0; i <= 8; i += 1) {
        const v = (end * i) / 4;
        rows.push([v, titrationPh(num(p, "acid_concentration"), num(p, "acid_volume"), num(p, "base_concentration"), v)]);
      }
      return { columns: ["sim.titration.volume_added", "sim.titration.ph"], rows };
    },
  };
}

/** The indicator's colour at a pH, as a content colour swatch (DSN-18): named in text, outlined, never themed. */
export function indicatorColour(indicator: string, ph: number): "colourless" | "pink" | "red" | "orange" | "yellow" {
  if (indicator === "methyl_orange") return ph < 3.1 ? "red" : ph <= 4.4 ? "orange" : "yellow";
  return ph < 8.2 ? "colourless" : "pink";
}
