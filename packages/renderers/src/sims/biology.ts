// The S3 Biology simulation models (VSL-08a, VSL-46; D38: TypeScript models cross-checked against the verification
// tools): population dynamics. Like the other models they are pure: the same values and steps give the same states,
// with no clock, random source or network. enzyme_sim and osmosis_sim are not here: each needs a source-backed asset
// from an installed Biology pack, which there is not yet, so they stay unavailable (the engine's UNAVAILABLE_KINDS).
import { num, type SimModel, type Values } from "./models.js";

/** Fixed RK4 steps in one run: the model always integrates on the grid t = j × duration / POPULATION_STEPS, however the
 * frames fall, so a run's populations do not depend on the frame rate. */
export const POPULATION_STEPS = 2000;

// --- population_sim (math_evaluate: logistic N' = rN(1 − N/K); Lotka–Volterra x' = ax − bxy, y' = dxy − cy) ------

export interface PopulationSetup {
  model: "logistic" | "predator_prey";
  initial_prey: number;
  initial_predator: number;
  duration: number;
}

export interface PopulationState {
  /** Model time the run has reached (time scale × screen time); the populations are at the last grid step before it. */
  clock: number;
  step: number;
  prey: number;
  predator: number;
}

/** The controls each model has, in order: the rates (and the logistic carrying capacity). */
export const POPULATION_CONTROLS = {
  logistic: ["growth_rate", "carrying_capacity"],
  predator_prey: ["growth_rate", "predation_rate", "predator_death_rate", "conversion_rate"],
} as const;

function slopes(s: PopulationSetup, p: Values, x: number, y: number): [number, number] {
  const a = num(p, "growth_rate");
  if (s.model === "logistic") return [a * x * (1 - x / num(p, "carrying_capacity")), 0];
  return [a * x - num(p, "predation_rate") * x * y, num(p, "conversion_rate") * x * y - num(p, "predator_death_rate") * y];
}

/** One fixed RK4 step of size h. */
function rk4(s: PopulationSetup, p: Values, x: number, y: number, h: number): [number, number] {
  const [k1x, k1y] = slopes(s, p, x, y);
  const [k2x, k2y] = slopes(s, p, x + (h / 2) * k1x, y + (h / 2) * k1y);
  const [k3x, k3y] = slopes(s, p, x + (h / 2) * k2x, y + (h / 2) * k2y);
  const [k4x, k4y] = slopes(s, p, x + h * k3x, y + h * k3y);
  return [x + (h / 6) * (k1x + 2 * k2x + 2 * k3x + k4x), y + (h / 6) * (k1y + 2 * k2y + 2 * k3y + k4y)];
}

/** The state moved on to grid step `to` (never past the end of the run). */
function advance(s: PopulationSetup, p: Values, state: PopulationState, to: number): PopulationState {
  const h = s.duration / POPULATION_STEPS;
  let { prey, predator } = state;
  let step = state.step;
  for (const last = Math.min(POPULATION_STEPS, to); step < last; step += 1) [prey, predator] = rk4(s, p, prey, predator, h);
  return { clock: state.clock, step, prey, predator };
}

/** Lotka–Volterra's conserved quantity, dx − c ln x + by − a ln y: constant along every exact trajectory. */
export function conserved(p: Values, prey: number, predator: number): number {
  return num(p, "conversion_rate") * prey - num(p, "predator_death_rate") * Math.log(prey) + num(p, "predation_rate") * predator - num(p, "growth_rate") * Math.log(predator);
}

/** The run sampled every `every` grid steps from the start (the drawing's curve and the text mode's rows). */
export function trajectory(s: PopulationSetup, p: Values, until: number, every: number): [number, number, number][] {
  const h = s.duration / POPULATION_STEPS;
  let state: PopulationState = { clock: 0, step: 0, prey: s.initial_prey, predator: s.initial_predator };
  const out: [number, number, number][] = [[0, state.prey, state.predator]];
  while (state.step < Math.min(until, POPULATION_STEPS)) {
    state = advance(s, p, state, Math.min(until, state.step + every));
    out.push([state.step * h, state.prey, state.predator]);
  }
  return out;
}

export function population(s: PopulationSetup, start: Values): SimModel<PopulationState> {
  const h = s.duration / POPULATION_STEPS;
  const lv = s.model === "predator_prey";
  // Each control's slider runs from a tenth of its starting value to twice it, in tenths (the engine refuses a rate
  // too small for these steps).
  // The control labels as literal catalogue keys, so the catalogue check sees each one used.
  const CONTROL_LABELS: Record<string, string> = {
    growth_rate: "sim.population.growth_rate", carrying_capacity: "sim.population.carrying_capacity",
    predation_rate: "sim.population.predation_rate", predator_death_rate: "sim.population.predator_death_rate",
    conversion_rate: "sim.population.conversion_rate",
  };
  const slider = (id: string, labelKey: string) => {
    const v = num(start, id);
    return { id, labelKey, unit: "", kind: "range" as const, min: v / 10, max: 2 * v, step: v / 10, default: v };
  };
  return {
    kind: "population_sim",
    params: POPULATION_CONTROLS[s.model].map((id) => slider(id, CONTROL_LABELS[id] ?? id)),
    readouts: [
      { id: "out.time", labelKey: "sim.population.time", unit: "", sf: 3 },
      { id: "out.prey", labelKey: lv ? "sim.population.prey" : "sim.population.population", unit: "", sf: 3 },
      ...(lv ? [{ id: "out.predator", labelKey: "sim.population.predator", unit: "", sf: 3 }] : []),
    ],
    init: () => ({ clock: 0, step: 0, prey: s.initial_prey, predator: s.initial_predator }),
    next(state, p, dt) {
      const clock = state.clock + dt * num(p, "timescale");
      return { ...advance(s, p, state, Math.floor(clock / h + 1e-9)), clock };
    },
    read: (state) => ({ "out.time": state.step * h, "out.prey": state.prey, ...(lv ? { "out.predator": state.predator } : {}) }),
    done: (state) => state.step >= POPULATION_STEPS,
    table(p) {
      const rows = trajectory(s, p, POPULATION_STEPS, POPULATION_STEPS / 8);
      return {
        columns: ["sim.population.time", lv ? "sim.population.prey" : "sim.population.population", ...(lv ? ["sim.population.predator"] : [])],
        rows: rows.map(([t, x, y]) => (lv ? [t, x, y] : [t, x])),
      };
    },
  };
}
