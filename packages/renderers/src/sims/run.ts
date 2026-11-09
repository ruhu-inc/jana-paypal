// Running a simulation model to its end (VSL-30): the reduced-motion and text-mode state, and the end of a predicted run.
import type { SimModel, Values } from "./models.js";

const MAX_STEPS = 100_000;

/** Run the model to its end (or a bounded number of steps). */
export function runToEnd<S>(model: SimModel<S>, values: Values, dt: number): S {
  return runOn(model, model.init(values), values, dt);
}

/** Run the model on from a state to its end (or a bounded number of steps). */
export function runOn<S>(model: SimModel<S>, from: S, values: Values, dt: number): S {
  if (model.endless === true) return from;
  let state = from;
  for (let i = 0; i < MAX_STEPS && !model.done(state, values); i += 1) state = model.next(state, values, dt);
  return state;
}
