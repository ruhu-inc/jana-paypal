// The simulation shell (VSL-30, VSL-32, LOC-16): native, labelled controls with their units (arrow keys move one
// step, Page keys ten), readouts in a polite live region updated at most once per sim_readout_announce_ms, play and
// pause, reset to the starting values, record a reading for the tutor, and the text mode (a table of readings) one
// control away. The model advances only at its fixed time step while the visual is on screen and the window is
// visible; under reduced motion each change jumps to its final state instead of animating.
//
// With a predict scenario (VSL-47) the shell runs predict, observe, explain: the simulation is locked until the
// student's own prediction is in (choices follow the writing, never before), the run's result is set beside the
// prediction, which can no longer change, and after the student's one line the explanation is drawn in the
// simulation's own picture. Readouts stay off until "Show readings" is ticked, so a number never gives the answer away.
//
// The tutor points at a control (`ctrl.<id>`) or a readout (`out.<id>`) as at any other part (VSL-19, UXS-57): the
// named one wears the pointed ring and says so to a screen reader, and under the spotlight the others are dimmed.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactElement } from "react";
import type { VisualProps } from "../contract.js";
import { CLIENT_KEYS } from "../generated/keys.js";
import { keyOnly } from "../kit.js";
import { applies, literal, shownReadout, sig, withUnit, type SimModel, type SimParam, type Values } from "./models.js";
import { ComparePanel, LockedStage, PredictPanel, type Phase } from "./PredictFlow.js";
import { compare, observe, scenarioArgs, type Prediction, type Scenario } from "./predict.js";
import { runOn, runToEnd } from "./run.js";

export { runToEnd };

function stepOf(p: SimParam): number {
  return p.step ?? 1;
}

function clamp(p: SimParam, v: number): number {
  const lo = p.min ?? -Infinity;
  const hi = p.max ?? Infinity;
  const k = Math.round((Math.min(hi, Math.max(lo, v)) - (p.min ?? 0)) / stepOf(p));
  return Number(((p.min ?? 0) + k * stepOf(p)).toFixed(6));
}

export interface SimViewProps<S> {
  model: SimModel<S>;
  props: VisualProps;
  start: Values;
  locked: readonly string[];
  /** Whether the model moves on its own (play and pause), or only follows the controls. */
  animated: boolean;
  draw(state: S, values: Values): ReactElement;
  /** A predict, observe, explain scenario (VSL-47); without one the simulation is free to explore. */
  scenario?: Scenario | null;
}

const BUTTON = "rounded-control border border-line px-3 py-1 text-ui";
const POINTED = "rounded-control outline-dashed outline-2 outline-offset-2 outline-highlight-line";

/** The tutor's pointing at one control or readout: the pointed ring, or the spotlight's dimming of the others. */
function pointing(props: VisualProps, id: string): { pointed: boolean; attrs: Record<string, unknown> } {
  const focus = props.focus ?? [];
  const pointed = focus.includes(id);
  if (pointed) return { pointed, attrs: { "data-pointed": "true" } };
  if (props.spotlight === true && focus.length > 0) return { pointed, attrs: { "data-dimmed": "true", style: { opacity: CLIENT_KEYS.board_spotlight_dim_opacity } } };
  return { pointed, attrs: {} };
}

export function SimView<S>({ model, props, start, locked, animated, draw, scenario = null }: SimViewProps<S>): ReactElement {
  const t = literal(props.t ?? keyOnly);
  const dt = 1 / CLIENT_KEYS.sim_steps_per_s;
  const parts = new Set(((props.spec.parts as { id: string }[] | undefined) ?? []).map((p) => p.id));
  const [values, setValues] = useState<Values>(start);
  const [state, setState] = useState<S>(() => (props.reducedMotion ? runToEnd(model, start, dt) : model.init(start)));
  const [playing, setPlaying] = useState(false);
  const [textMode, setTextMode] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [announced, setAnnounced] = useState("");
  const [phase, setPhase] = useState<Phase>(scenario === null ? "free" : "predict");
  const [prediction, setPrediction] = useState<Prediction | null>(null);
  const [why, setWhy] = useState<string | null>(null);
  const [showReadings, setShowReadings] = useState(scenario === null);
  const lastAnnounce = useRef(-Infinity);
  const clock = useRef<{ last: number | null; carry: number }>({ last: null, carry: 0 });

  const predicting = phase === "predict";
  const readings = model.read(state, values);
  const readingText = model.readouts.map((r) => `${t(r.labelKey)} ${shownReadout(readings[r.id] ?? NaN, r)} ${r.unit}`.trim()).join("; ");
  const emit = props.onSimEvent ?? (() => undefined);
  const speed = phase === "free" ? 1 : scenario?.run.speed ?? 1;

  const announce = (now: number, text: string) => {
    if (!showReadings) return;
    if (now - lastAnnounce.current >= CLIENT_KEYS.sim_readout_announce_ms) {
      lastAnnounce.current = now;
      setAnnounced(text);
    }
  };

  // Paused when the window is hidden (VSL-30 lifecycle).
  useEffect(() => {
    const onVisibility = () => setHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  const running = animated && playing && !predicting && !props.reducedMotion && props.visible !== false && !hidden && !model.done(state, values);
  useEffect(() => {
    if (!running) {
      clock.current.last = null;
      return undefined;
    }
    let frame = 0;
    const tick = (now: number) => {
      const c = clock.current;
      const elapsed = c.last === null ? 0 : Math.min(0.25, (now - c.last) / 1000);
      c.last = now;
      // A predicted run may play faster than real time; it still advances only by whole fixed steps.
      c.carry += elapsed * speed;
      const steps = Math.floor(c.carry / dt);
      c.carry -= steps * dt;
      if (steps > 0) {
        setState((s) => {
          let next = s;
          for (let i = 0; i < steps && !model.done(next, values); i += 1) next = model.next(next, values, dt);
          return next;
        });
      }
      announce(now, readingText);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  });

  const change = (p: SimParam, value: number | string | boolean) => {
    if (predicting) return;
    const v = p.kind === "range" ? clamp(p, Number(value)) : value;
    const next = { ...values, [p.id]: v };
    setValues(next);
    const from = model.adjust === undefined ? model.init(next) : model.adjust(state, values, next);
    const fresh = props.reducedMotion || !animated ? runOn(model, from, next, dt) : from;
    setState(fresh);
    emit({ type: "sim.param_changed", visual_id: props.spec.visual_id, param: p.id, value: v });
    const r = model.read(fresh, next);
    announce(performance.now(), model.readouts.map((o) => `${t(o.labelKey)} ${shownReadout(r[o.id] ?? NaN, o)} ${o.unit}`.trim()).join("; "));
  };

  const onRangeKey = (p: SimParam) => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "PageUp" && e.key !== "PageDown") return;
    e.preventDefault();
    change(p, Number(values[p.id]) + (e.key === "PageUp" ? 10 : -10) * stepOf(p));
  };

  // Reset restores the starting values; a locked-in prediction and the comparison stay as they are.
  const reset = () => {
    setValues(start);
    setState(props.reducedMotion || !animated ? runToEnd(model, start, dt) : model.init(start));
    setPlaying(false);
    emit({ type: "sim.reset", visual_id: props.spec.visual_id });
  };

  const record = () => {
    const reading: Record<string, string> = {};
    for (const r of model.readouts) reading[r.id] = shownReadout(readings[r.id] ?? NaN, r);
    emit({ type: "sim.reading_recorded", visual_id: props.spec.visual_id, reading });
  };

  // The comparison is computed once from the model, never from frames; the start and the scenario never change.
  const seen = useMemo(() => (scenario === null ? null : observe(model, scenario, start, dt)), [scenario?.id]);
  const args = scenario === null || seen === null ? {} : scenarioArgs(model, scenario, start, seen);

  // Lock in: the prediction is fixed, then the scenario's run starts from the starting values.
  const commit = (p: Prediction) => {
    if (scenario === null || seen === null || !predicting) return;
    setPrediction(p);
    setPhase("observe");
    const runValues = seen.runValues;
    setValues(runValues);
    setState(props.reducedMotion || !animated ? runToEnd(model, runValues, dt) : model.init(runValues));
    setPlaying(animated);
    const standing = compare(scenario, p);
    emit({ type: "sim.prediction_committed", visual_id: props.spec.visual_id, scenario_id: scenario.id, misconception_id: scenario.misconception_id,
      prediction: p.text, choice: p.choice, correct: standing.correct, common_guess: standing.common_guess });
  };

  const explain = (line: string) => {
    if (scenario === null || prediction === null) return;
    setWhy(line);
    setPhase("explained");
    emit({ type: "sim.explained", visual_id: props.spec.visual_id, scenario_id: scenario.id, misconception_id: scenario.misconception_id, explanation: line });
  };

  // The result appears once the run has happened: at once for a change that settles immediately, else at its end.
  const happened = scenario !== null && !predicting && (scenario.run.until === "now" || props.reducedMotion || !animated || model.done(state, values) || phase === "explained");

  const table = model.table(values);
  const drawing = textMode ? (
    <table className="w-full text-ui" data-sim-table="true">
      <thead><tr>{table.columns.map((c) => <th key={c} scope="col" className="text-start">{t(c)}</th>)}</tr></thead>
      <tbody>{table.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j} className="tabular-nums">{sig(v, 3)}</td>)}</tr>)}</tbody>
    </table>
  ) : draw(state, values);
  return (
    <div className="my-2 rounded-card border border-line p-3" role="group" aria-label={props.spec.alt.summary} data-kind={props.spec.type} data-visual-id={props.spec.visual_id}
      {...(scenario !== null ? { "data-sim-phase": phase, "data-scenario": scenario.id } : {})}>
      {scenario !== null && <p className="mb-3 text-body font-medium" data-sim-question="true">{t(`poe.${scenario.key}.question`, args)}</p>}
      {predicting ? <LockedStage t={t}>{draw(state, values)}</LockedStage> : drawing}
      {predicting && scenario !== null && <PredictPanel t={t} scenario={scenario} unit={unitOf(model, scenario.observe)} idBase={props.spec.visual_id} onCommit={commit} />}
      {scenario !== null && prediction !== null && seen !== null && (
        <ComparePanel t={t} model={model} scenario={scenario} prediction={prediction} start={start} seen={seen} args={args} happened={happened} why={why}
          idBase={props.spec.visual_id} dt={dt} onExplain={explain} />
      )}
      {!predicting && (
        <>
          <div className="mt-3 grid gap-2">
            {model.params.filter((p) => applies(p, values)).map((p) => {
              const id = `${props.spec.visual_id}-${p.id}`;
              const value = values[p.id];
              const disabled = locked.includes(p.id);
              const part = parts.has(`ctrl.${p.id}`) ? { "data-part": `ctrl.${p.id}` } : {};
              const { pointed, attrs } = pointing(props, `ctrl.${p.id}`);
              const ring = pointed ? ` ${POINTED}` : "";
              const said = pointed ? <span className="sr-only">{`, ${t("visual.part.pointed")}`}</span> : null;
              if (p.kind === "toggle") {
                return (
                  <label key={p.id} htmlFor={id} className={`flex min-h-11 items-center gap-2 text-ui${ring}`} {...attrs}>
                    <input id={id} type="checkbox" checked={value === true} disabled={disabled} onChange={(e) => change(p, e.currentTarget.checked)} {...part} />
                    {t(p.labelKey)}{said}
                  </label>
                );
              }
              if (p.kind === "choice") {
                return (
                  <label key={p.id} htmlFor={id} className={`flex min-h-11 items-center gap-2 text-ui${ring}`} {...attrs}>
                    {t(p.labelKey)}{said}
                    <select id={id} className="min-h-11 rounded-control border border-line-strong bg-canvas px-2" value={String(value)} disabled={disabled} onChange={(e) => change(p, e.currentTarget.value)} {...part}>
                      {(p.choices ?? []).map((c) => <option key={c.value} value={c.value}>{t(c.labelKey)}</option>)}
                    </select>
                  </label>
                );
              }
              return (
                <label key={p.id} htmlFor={id} className={`grid grid-cols-[10rem_1fr_6rem] items-center gap-2 text-ui${ring}`} {...attrs}>
                  <span>{`${t(p.labelKey)}${p.unit ? ` (${p.unit})` : ""}`}{said}</span>
                  <input id={id} type="range" className="min-h-11" min={p.min} max={p.max} step={p.step} value={Number(value)} disabled={disabled}
                    aria-valuetext={withUnit(String(value), p.unit)} onChange={(e) => change(p, e.currentTarget.value)} onKeyDown={onRangeKey(p)} {...part} />
                  <output htmlFor={id} className="tabular-nums">{withUnit(String(value), p.unit)}</output>
                </label>
              );
            })}
          </div>
          {scenario !== null && (
            <label htmlFor={`${props.spec.visual_id}-readings`} className="mt-3 flex min-h-11 items-center gap-2 text-ui">
              <input id={`${props.spec.visual_id}-readings`} type="checkbox" checked={showReadings} onChange={(e) => setShowReadings(e.currentTarget.checked)} />
              {t("sim.readings")}
            </label>
          )}
          {showReadings && (
            <dl className="mt-3 grid grid-cols-2 gap-x-4 text-ui">
              {model.readouts.map((r) => {
                const { pointed, attrs } = pointing(props, r.id);
                return (
                  <div key={r.id} className="contents">
                    <dt className="text-muted" {...(pointed ? {} : attrs)}>{t(r.labelKey)} <span className="text-meta">{t("sim.significant_figures", { count: r.sf })}</span></dt>
                    <dd className={`tabular-nums${pointed ? ` ${POINTED}` : ""}`} {...attrs} {...(parts.has(r.id) ? { "data-part": r.id, tabIndex: 0 } : {})}>
                      {withUnit(shownReadout(readings[r.id] ?? NaN, r), r.unit)}
                      {pointed && <span className="sr-only">{`, ${t("visual.part.pointed")}`}</span>}
                    </dd>
                  </div>
                );
              })}
            </dl>
          )}
          <p className="sr-only" aria-live="polite" data-sim-live="true">{announced}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {animated && <button type="button" className={BUTTON} onClick={() => setPlaying((v) => !v)} aria-pressed={playing}>{t(playing ? "sim.pause" : "sim.play")}</button>}
            <button type="button" className={`${BUTTON}${scenario !== null ? " min-h-11 min-w-28 border-line-strong font-medium" : ""}`} onClick={reset}>{t("sim.reset")}</button>
            <button type="button" className={BUTTON} onClick={record}>{t("sim.record")}</button>
            <button type="button" className={BUTTON} onClick={() => setTextMode((v) => !v)} aria-pressed={textMode}>{t(textMode ? "sim.show_drawing" : "sim.show_table")}</button>
          </div>
        </>
      )}
    </div>
  );
}

function unitOf<S>(model: SimModel<S>, readout: string): string {
  return model.readouts.find((r) => r.id === readout)?.unit ?? "";
}
