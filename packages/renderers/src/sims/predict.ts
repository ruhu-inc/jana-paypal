// Predict, observe, explain (VSL-47): the authored scenarios and the pure steps the simulation shell runs them with.
// A scenario names a case where the usual guess is wrong and the pack misconception behind that guess; the student
// writes a prediction before anything runs, the run's result is set beside it, and the explanation is drawn in the
// simulation's own picture. Every number here comes from the model; the words come from the locale catalogue.
import data from "./scenarios.json" with { type: "json" };
import { runToEnd } from "./run.js";
import { shownReadout, withUnit, type SimModel, type Values } from "./models.js";

export interface ScenarioChoice {
  id: string;
  /** A numeric answer, shown in the observed readout's unit; otherwise the label is `poe.<key>.choice.<id>`. */
  value?: number;
  /** A numeric answer still labelled in words (`poe.<key>.choice.<id>`), as a coordinate is. */
  labelled?: boolean;
  /** A which-one answer: it holds when this readout is above zero after the run (a product forms, say). */
  readout?: string;
  correct?: boolean;
  /** The usual guess: the one the scenario's misconception gives. */
  common_guess?: boolean;
}

export interface Scenario {
  id: string;
  key: string;
  kind: string;
  subject: string;
  misconception_id: string;
  kc_ids: string[];
  start: Record<string, unknown>;
  /** `before: "end"` compares the run with the same run from the starting values, both read at their ends. */
  run: { set: Values; until: "now" | "done"; speed?: number; before?: "start" | "end" };
  /** The readout whose result is set beside the prediction. */
  observe: string;
  choices: ScenarioChoice[];
  explain:
    | { chart: "bars"; readouts: string[] }
    | { chart: "curve"; x: string; y: string; mark: string }
    // Both runs, before dashed and after solid, each with where it ended; `mark` names the readout each end is
    // labelled with (a flight's range), when the end's own y says nothing (both land at height 0).
    | { chart: "runs"; x: string; y: string; mark?: string }
    // The text-mode table's (x, y) rows before and after, with a tracked point (two readouts) marked on each.
    | { chart: "overlay"; mark: [string, string] };
}

export const SCENARIOS: readonly Scenario[] = data.scenarios as Scenario[];

/** The scenario a resolved spec's `params.predict` names, if it is one of this kind's. */
export function scenarioOf(kind: string, params: unknown): Scenario | null {
  const id = (params as { predict?: unknown } | null)?.predict;
  if (typeof id !== "string") return null;
  return SCENARIOS.find((s) => s.id === id && s.kind === kind) ?? null;
}

export interface Prediction {
  text: string;
  choice: string | null;
}

export interface Observation {
  before: Record<string, number>;
  after: Record<string, number>;
  /** The values the run leaves the controls at. */
  runValues: Values;
}

/** Readings at the starting values and at the end of the run: deterministic, so the comparison never depends on frames. */
export function observe<S>(model: SimModel<S>, scenario: Scenario, start: Values, dt: number): Observation {
  const runValues = { ...start, ...scenario.run.set };
  const before = model.read(scenario.run.before === "end" ? runToEnd(model, start, dt) : model.init(start), start);
  // A change that settles at once is read at once; a run with an end is read at its end.
  const after = model.read(scenario.run.until === "now" ? model.init(runValues) : runToEnd(model, runValues, dt), runValues);
  return { before, after, runValues };
}

/** The message arguments of a scenario's words: the starting values, then each readout before and after, with units. */
export function scenarioArgs<S>(model: SimModel<S>, scenario: Scenario, start: Values, seen: Observation): Record<string, string | number> {
  const args: Record<string, string | number> = {};
  // Numbers carry their units from the model, so the catalogue's words hold no unit symbols.
  for (const [k, v] of Object.entries(start)) {
    if (typeof v !== "number") continue;
    const unit = model.params.find((p) => p.id === k)?.unit ?? "";
    args[k] = withUnit(v, unit);
  }
  for (const r of model.readouts) {
    const short = r.id.replace(/^out\./, "");
    const before = `${shownReadout(seen.before[r.id] ?? NaN, r)} ${r.unit}`.trim();
    const after = `${shownReadout(seen.after[r.id] ?? NaN, r)} ${r.unit}`.trim();
    args[`${short}_before`] = before;
    args[`${short}_after`] = after;
    if (r.id === scenario.observe) Object.assign(args, { before, after });
  }
  return args;
}

/** How the prediction stands against the scenario: whether the chosen answer was right and whether it was the usual guess. */
export function compare(scenario: Scenario, prediction: Prediction): { correct: boolean | null; common_guess: boolean } {
  const choice = scenario.choices.find((c) => c.id === prediction.choice);
  return { correct: choice === undefined ? null : choice.correct === true, common_guess: choice?.common_guess === true };
}

/** The run sampled as (x, y) readout pairs, for a curve explanation: at most `points` samples, start and end included. */
export function trace<S>(model: SimModel<S>, values: Values, x: string, y: string, dt: number, points = 120): [number, number][] {
  const states: S[] = [];
  let state = model.init(values);
  states.push(state);
  for (let i = 0; i < 100_000 && !model.done(state, values); i += 1) {
    state = model.next(state, values, dt);
    states.push(state);
  }
  const every = Math.max(1, Math.ceil(states.length / points));
  const picked = states.filter((_, i) => i % every === 0 || i === states.length - 1);
  return picked.map((s) => {
    const r = model.read(s, values);
    return [r[x] ?? NaN, r[y] ?? NaN];
  });
}
