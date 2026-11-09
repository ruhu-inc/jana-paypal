// The panels of predict, observe, explain (VSL-47), to the S1 design: (1) Predict, the simulation visibly locked
// ("Runs after you predict") over the student's own words, with choices offered only once something is written, and
// "Lock in and run"; (2) Compare, "You predicted" beside "What happened", then the one-line "How did it differ from
// your prediction, and why?", then the explanation drawn in the simulation's own picture: a bar chart of the readouts
// before and after, or the run's curve with the result and the prediction marked. Not a paragraph.
import { useId, useState, type ReactElement, type ReactNode } from "react";
import type { Translate } from "../contract.js";
import { INK, LINE, MUTED, STRONG } from "../kit.js";
import { shownReadout, sig, withUnit, type SimModel, type Values } from "./models.js";
import { compare, trace, type Observation, type Prediction, type Scenario } from "./predict.js";

export type Phase = "free" | "predict" | "observe" | "explained";

const FIELD = "w-full rounded-control border border-line-strong bg-canvas px-3 py-2 text-ui";
const PRIMARY = "min-h-11 rounded-control border border-transparent bg-action px-4 text-ui font-medium text-action-ink hover:bg-action-hover disabled:cursor-not-allowed disabled:bg-panel disabled:text-faint";

function Step({ n, title }: { n: number; title: string }): ReactElement {
  return (
    <div className="flex items-center gap-2">
      <span aria-hidden="true" className="inline-flex size-6 items-center justify-center rounded-pill bg-action text-meta font-semibold text-action-ink">{n}</span>
      <h3 className="text-ui font-semibold">{title}</h3>
    </div>
  );
}

/** The simulation, visibly locked until the prediction is in: drawn but dimmed, with no readings and no controls. */
export function LockedStage({ t, children }: { t: Translate; children: ReactNode }): ReactElement {
  return (
    <div className="relative overflow-hidden rounded-card bg-panel" data-sim-locked="true">
      <div aria-hidden="true" className="opacity-40 grayscale">{children}</div>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="inline-flex items-center gap-2 rounded-pill border border-line bg-canvas px-3 py-2 text-meta text-ink">
          <svg viewBox="0 0 24 24" width={16} height={16} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
            <rect width="18" height="11" x="3" y="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
          {t("sim.predict.locked")}
        </span>
      </div>
    </div>
  );
}

export function choiceLabel(t: Translate, scenario: Scenario, id: string, unit: string): string {
  const choice = scenario.choices.find((c) => c.id === id);
  if (choice?.value !== undefined && choice.labelled !== true) return `${sig(choice.value, 3)} ${unit}`.trim();
  return t(`poe.${scenario.key}.choice.${id}`);
}

export function PredictPanel({ t, scenario, unit, idBase, onCommit }: {
  t: Translate; scenario: Scenario; unit: string; idBase: string; onCommit(p: Prediction): void;
}): ReactElement {
  const [text, setText] = useState("");
  const [choice, setChoice] = useState<string | null>(null);
  const id = `${idBase}-prediction`;
  const written = text.trim().length > 0;
  return (
    <section className="mt-3 grid gap-3" aria-labelledby={`${id}-step`} data-sim-predict="true">
      <div id={`${id}-step`}><Step n={1} title={t("sim.predict.step")} /></div>
      <label htmlFor={id} className="text-ui font-medium">{t("sim.predict.label")}</label>
      <textarea id={id} rows={2} className={`${FIELD} resize-none`} value={text} maxLength={500} onChange={(e) => setText(e.currentTarget.value)} />
      {written && (
        <fieldset className="grid gap-2" data-sim-choices="true">
          <legend className="mb-1 text-ui text-muted">{t("sim.predict.choices")}</legend>
          {scenario.choices.map((c) => (
            <label key={c.id} htmlFor={`${id}-${c.id}`} className="flex min-h-11 items-center gap-3 rounded-control border border-line px-3 text-ui">
              <input id={`${id}-${c.id}`} type="radio" name={`${id}-choice`} value={c.id} checked={choice === c.id} onChange={() => setChoice(c.id)} />
              {choiceLabel(t, scenario, c.id, unit)}
            </label>
          ))}
        </fieldset>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={PRIMARY} disabled={!written} onClick={() => onCommit({ text: text.trim(), choice })}>{t("sim.predict.commit")}</button>
        <span className="text-meta text-muted">{t("sim.predict.commit_note")}</span>
      </div>
    </section>
  );
}

export function ComparePanel<S>({ t, model, scenario, prediction, start, seen, args, happened, why, idBase, dt, onExplain }: {
  t: Translate; model: SimModel<S>; scenario: Scenario; prediction: Prediction; start: Values; seen: Observation; args: Record<string, string | number>;
  happened: boolean; why: string | null; idBase: string; dt: number; onExplain(line: string): void;
}): ReactElement {
  const [line, setLine] = useState("");
  const id = `${idBase}-why`;
  const unit = model.readouts.find((r) => r.id === scenario.observe)?.unit ?? "";
  const standing = compare(scenario, prediction);
  return (
    <section className="mt-3 grid gap-3" aria-labelledby={`${id}-step`} data-sim-compare="true">
      <div id={`${id}-step`}><Step n={2} title={t("sim.predict.compare_step")} /></div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid content-start gap-1 rounded-card border border-line bg-panel p-3" data-sim-predicted="true">
          <span className="text-meta font-medium text-muted">{t("sim.predict.you_predicted")}</span>
          <p className="text-ui">{prediction.text}</p>
          {prediction.choice !== null && <p className="text-ui font-medium" data-sim-choice={prediction.choice}>{choiceLabel(t, scenario, prediction.choice, unit)}</p>}
        </div>
        <div className="grid content-start gap-1 rounded-card bg-hint-soft p-3" data-sim-happened={happened ? "true" : "running"} aria-live="polite">
          <span className="text-meta font-semibold text-hint">{t("sim.predict.what_happened")}</span>
          <p className="text-ui">{happened ? t(`poe.${scenario.key}.outcome`, args) : t("sim.predict.running")}</p>
        </div>
      </div>
      {happened && standing.common_guess && <p className="text-meta text-muted" data-sim-common-guess="true">{t("sim.predict.common_guess")}</p>}
      {happened && why === null && (
        <form className="grid gap-2" onSubmit={(e) => { e.preventDefault(); if (line.trim()) onExplain(line.trim()); }}>
          <label htmlFor={id} className="text-ui font-medium">{t("sim.predict.why")}</label>
          <textarea id={id} rows={2} className={`${FIELD} resize-none`} placeholder={t("sim.predict.why_placeholder")} value={line} maxLength={300} onChange={(e) => setLine(e.currentTarget.value)} />
          <div><button type="submit" className={PRIMARY} disabled={line.trim().length === 0}>{t("sim.predict.explain")}</button></div>
        </form>
      )}
      {why !== null && (
        <div className="grid gap-2" data-sim-explanation="true">
          <p className="text-ui"><span className="text-muted">{t("sim.predict.your_why")}: </span>{why}</p>
          {scenario.explain.chart === "bars" && <Bars t={t} model={model} readouts={scenario.explain.readouts} seen={seen} />}
          {scenario.explain.chart === "curve" && <Curve t={t} model={model} scenario={scenario} seen={seen} prediction={prediction} dt={dt} />}
          {scenario.explain.chart === "runs" && <Runs t={t} model={model} x={scenario.explain.x} y={scenario.explain.y} mark={scenario.explain.mark} start={start} seen={seen} dt={dt} />}
          {scenario.explain.chart === "overlay" && <Overlay t={t} model={model} mark={scenario.explain.mark} start={start} seen={seen} />}
          <p className="text-ui">{t(`poe.${scenario.key}.explain`, args)}</p>
        </div>
      )}
    </section>
  );
}

const CW = 480;

/** Each readout before and after the run, as a pair of bars on its own scale. */
function Bars<S>({ t, model, readouts, seen }: { t: Translate; model: SimModel<S>; readouts: string[]; seen: Observation }): ReactElement {
  const rows = readouts.map((id) => model.readouts.find((r) => r.id === id)).filter((r) => r !== undefined);
  const rowH = 64;
  return (
    <div className="max-w-xl rounded-card border border-line bg-canvas p-2" data-sim-chart="bars">
      {rows.map((r) => {
        const before = seen.before[r.id] ?? 0;
        const after = seen.after[r.id] ?? 0;
        const top = Math.max(Math.abs(before), Math.abs(after)) || 1;
        const w = (v: number) => Math.max(2, (Math.abs(v) / top) * (CW - 200));
        const label = t("sim.predict.chart", { name: t(r.labelKey), before: sig(before, r.sf), after: sig(after, r.sf), unit: r.unit });
        return (
          <svg key={r.id} viewBox={`0 0 ${CW} ${rowH}`} className="w-full" role="img" aria-label={label}>
            <text x={0} y={14} fontSize={13} fill="var(--color-ink)" fontWeight={600}>{t(r.labelKey)}</text>
            <text x={0} y={34} fontSize={12} fill={MUTED}>{t("sim.predict.before")}</text>
            <rect x={70} y={24} width={w(before)} height={14} fill={LINE} stroke={STRONG} strokeWidth={1} />
            <text x={76 + w(before)} y={35} fontSize={12} fill="var(--color-ink)">{`${sig(before, r.sf)} ${r.unit}`.trim()}</text>
            <text x={0} y={56} fontSize={12} fill={MUTED}>{t("sim.predict.after")}</text>
            <rect x={70} y={46} width={w(after)} height={14} fill={INK} />
            <text x={76 + w(after)} y={57} fontSize={12} fill="var(--color-ink)" fontWeight={600}>{`${sig(after, r.sf)} ${r.unit}`.trim()}</text>
          </svg>
        );
      })}
    </div>
  );
}

/** The run's own curve, with the result marked and a numeric prediction drawn as a dashed line beside it. */
function Curve<S>({ t, model, scenario, seen, prediction, dt }: {
  t: Translate; model: SimModel<S>; scenario: Scenario; seen: Observation; prediction: Prediction; dt: number;
}): ReactElement {
  const clipId = useId();
  if (scenario.explain.chart !== "curve") return <></>;
  const { x: xId, y: yId, mark } = scenario.explain;
  const rx = model.readouts.find((r) => r.id === xId);
  const ry = model.readouts.find((r) => r.id === yId);
  const rm = model.readouts.find((r) => r.id === mark);
  const points = trace(model, seen.runValues, xId, yId, dt);
  const markAt = seen.after[mark] ?? NaN;
  const guess = scenario.choices.find((c) => c.id === prediction.choice)?.value;
  const xs = [...points.map((p) => p[0]), markAt, guess ?? 0].filter(Number.isFinite);
  const ys = points.map((p) => p[1]).filter(Number.isFinite);
  const xmax = Math.max(...xs) * 1.05 || 1;
  const ymin = Math.min(0, ...ys);
  const ymax = Math.max(...ys) || 1;
  const H = 220;
  const L = 44, R = 12, T = 12, B = 34;
  const X = (v: number) => L + (v / xmax) * (CW - L - R);
  const Y = (v: number) => H - B - ((v - ymin) / (ymax - ymin)) * (H - T - B);
  const path = points.map(([px, py]) => `${X(px).toFixed(1)},${Y(py).toFixed(1)}`).join(" ");
  const unit = rm?.unit ?? "";
  const label = t("sim.predict.curve", { y: t(ry?.labelKey ?? yId), x: t(rx?.labelKey ?? xId), value: sig(markAt, rm?.sf ?? 3), unit });
  return (
    <div className="max-w-xl rounded-card border border-line bg-canvas p-2" data-sim-chart="curve">
      <svg viewBox={`0 0 ${CW} ${H}`} className="w-full" role="img" aria-label={label}>
        <clipPath id={clipId}><rect x={L} y={T} width={CW - L - R} height={H - T - B} /></clipPath>
        <line x1={L} x2={CW - R} y1={H - B} y2={H - B} stroke={STRONG} strokeWidth={1} />
        <line x1={L} x2={L} y1={T} y2={H - B} stroke={STRONG} strokeWidth={1} />
        <text x={(L + CW - R) / 2} y={H - 6} textAnchor="middle" fontSize={12} fill={MUTED}>{`${t(rx?.labelKey ?? xId)}${rx?.unit ? ` (${rx.unit})` : ""}`}</text>
        <text x={12} y={(T + H - B) / 2} textAnchor="middle" fontSize={12} fill={MUTED} transform={`rotate(-90 12 ${(T + H - B) / 2})`}>{t(ry?.labelKey ?? yId)}</text>
        <polyline points={path} fill="none" stroke={INK} strokeWidth={2.5} clipPath={`url(#${clipId})`} />
        {guess !== undefined && (
          <g data-sim-guess-mark="true">
            <line x1={X(guess)} x2={X(guess)} y1={T} y2={H - B} stroke={MUTED} strokeWidth={1.5} strokeDasharray="6 5" />
            <text x={X(guess) - 4} y={T + 12} textAnchor="end" fontSize={12} fill={MUTED}>{t("sim.predict.your_prediction")}</text>
          </g>
        )}
        {Number.isFinite(markAt) && (
          <g data-sim-result-mark="true">
            <line x1={X(markAt)} x2={X(markAt)} y1={T} y2={H - B} stroke={INK} strokeWidth={1} />
            <circle cx={X(markAt)} cy={H - B} r={4} fill={INK} />
            <text x={X(markAt) + 6} y={H - B - 8} fontSize={12} fill="var(--color-ink)" fontWeight={600}>{`${sig(markAt, rm?.sf ?? 3)} ${unit}`.trim()}</text>
          </g>
        )}
      </svg>
    </div>
  );
}

interface Frame {
  xs: number[];
  ys: number[];
}

/** Axes fitted to both sets of points, with zero kept in view when the data are all one sign. */
function frame({ xs, ys }: Frame, H: number, L: number, R: number, T: number, B: number) {
  const fx = xs.filter(Number.isFinite);
  const fy = ys.filter(Number.isFinite);
  const xmin = Math.min(0, ...fx);
  const xmax = Math.max(0, ...fx);
  const ymin = Math.min(0, ...fy);
  const ymax = Math.max(0, ...fy);
  const xs0 = xmax - xmin || 1;
  const ys0 = (ymax - ymin) * 1.08 || 1;
  return {
    X: (v: number) => L + ((v - xmin) / xs0) * (CW - L - R),
    Y: (v: number) => H - B - ((v - ymin) / ys0) * (H - T - B),
  };
}

function polyline(points: [number, number][], X: (v: number) => number, Y: (v: number) => number): string {
  return points.filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b)).map(([a, b]) => `${X(a).toFixed(1)},${Y(b).toFixed(1)}`).join(" ");
}

/** The run from the starting values (dashed) and the predicted run (solid), over time or as a path, each marked where
 * it ended, and labelled there with the `mark` readout when one is named (a flight's range). */
function Runs<S>({ t, model, x, y, mark, start, seen, dt }: { t: Translate; model: SimModel<S>; x: string; y: string; mark?: string | undefined; start: Values; seen: Observation; dt: number }): ReactElement {
  const clipId = useId();
  const rx = model.readouts.find((r) => r.id === x);
  const ry = model.readouts.find((r) => r.id === y);
  const rm = mark === undefined ? undefined : model.readouts.find((r) => r.id === mark);
  const marked = (o: Record<string, number>) => (rm === undefined ? "" : ` ${withUnit(shownReadout(o[rm.id] ?? NaN, rm), rm.unit)}`);
  const before = trace(model, start, x, y, dt);
  const after = trace(model, seen.runValues, x, y, dt);
  const H = 220, L = 44, R = 12, T = 12, B = 34;
  const { X, Y } = frame({ xs: [...before, ...after].map((p) => p[0]), ys: [...before, ...after].map((p) => p[1]) }, H, L, R, T, B);
  const sf = ry?.sf ?? 3;
  const ends = [
    { id: "before", at: seen.before, dash: "6 5", colour: MUTED },
    { id: "after", at: seen.after, dash: undefined, colour: INK },
  ];
  const label = rm !== undefined
    ? t("sim.predict.chart", { name: t(rm.labelKey), before: sig(seen.before[rm.id] ?? NaN, rm.sf), after: sig(seen.after[rm.id] ?? NaN, rm.sf), unit: rm.unit })
    : t("sim.predict.runs", {
      y: t(ry?.labelKey ?? y), x: t(rx?.labelKey ?? x), unit: ry?.unit ?? "",
      before: sig(seen.before[y] ?? NaN, sf), after: sig(seen.after[y] ?? NaN, sf),
    });
  return (
    <div className="max-w-xl rounded-card border border-line bg-canvas p-2" data-sim-chart="runs">
      <svg viewBox={`0 0 ${CW} ${H}`} className="w-full" role="img" aria-label={label}>
        <clipPath id={clipId}><rect x={L} y={T} width={CW - L - R} height={H - T - B} /></clipPath>
        <line x1={L} x2={CW - R} y1={H - B} y2={H - B} stroke={STRONG} strokeWidth={1} />
        <line x1={L} x2={L} y1={T} y2={H - B} stroke={STRONG} strokeWidth={1} />
        <text x={(L + CW - R) / 2} y={H - 6} textAnchor="middle" fontSize={12} fill={MUTED}>{`${t(rx?.labelKey ?? x)}${rx?.unit ? ` (${rx.unit})` : ""}`}</text>
        <text x={12} y={(T + H - B) / 2} textAnchor="middle" fontSize={12} fill={MUTED} transform={`rotate(-90 12 ${(T + H - B) / 2})`}>{t(ry?.labelKey ?? y)}</text>
        <polyline points={polyline(before, X, Y)} fill="none" stroke={MUTED} strokeWidth={2} strokeDasharray="6 5" clipPath={`url(#${clipId})`} data-sim-run="before" />
        <polyline points={polyline(after, X, Y)} fill="none" stroke={INK} strokeWidth={2.5} clipPath={`url(#${clipId})`} data-sim-run="after" />
        {ends.map((e, i) => {
          const ex = e.at[x] ?? NaN;
          const ey = e.at[y] ?? NaN;
          if (!Number.isFinite(ex) || !Number.isFinite(ey)) return null;
          return (
            <g key={e.id} data-sim-end={e.id}>
              <line x1={X(ex)} x2={X(ex)} y1={Y(ey)} y2={H - B} stroke={e.colour} strokeWidth={1} strokeDasharray={e.dash} />
              <circle cx={X(ex)} cy={Y(ey)} r={4} fill={e.colour} />
              {/* A marked end is labelled in the top corner, clear of paths that come down to where it landed. */}
              <text x={rm !== undefined ? CW - R : X(ex)} y={rm !== undefined ? T + 12 + i * 16 : H - B - 6 - i * 14} textAnchor={rm !== undefined || X(ex) > CW - 90 ? "end" : "start"}
                dx={rm !== undefined ? 0 : X(ex) > CW - 90 ? -6 : 6} fontSize={12} fill={e.colour} fontWeight={i === 1 ? 600 : 400}>
                {`${t(i === 0 ? "sim.predict.before" : "sim.predict.after")}${marked(e.at)}`}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** The table's (x, y) rows at the starting values (dashed) and after the run (solid), with the tracked point marked. */
function Overlay<S>({ t, model, mark, start, seen }: { t: Translate; model: SimModel<S>; mark: [string, string]; start: Values; seen: Observation }): ReactElement {
  const clipId = useId();
  const before = model.table(start).rows.map((r) => [r[0] ?? NaN, r[1] ?? NaN] as [number, number]);
  const after = model.table(seen.runValues).rows.map((r) => [r[0] ?? NaN, r[1] ?? NaN] as [number, number]);
  const [mx, my] = mark;
  const H = 240, L = 16, R = 16, T = 12, B = 16;
  const pts = [...before, ...after];
  // The y window: the graphs over the middle of the x range and both tracked points, so steep ends do not flatten it.
  const xs = pts.map((p) => p[0]).filter(Number.isFinite);
  const mid = (Math.min(...xs) + Math.max(...xs)) / 2;
  const half = 0.3 * (Math.max(...xs) - Math.min(...xs));
  const ys = [...pts.filter((p) => Math.abs(p[0] - mid) <= half).map((p) => p[1]), seen.before[my] ?? NaN, seen.after[my] ?? NaN];
  const { X, Y } = frame({ xs, ys }, H, L, R, T + 24, B);
  const rx = model.readouts.find((r) => r.id === mx);
  const sf = rx?.sf ?? 3;
  const at = (o: Record<string, number>) => [o[mx] ?? NaN, o[my] ?? NaN] as const;
  const ry = model.readouts.find((r) => r.id === my);
  const point = (o: Record<string, number>) => `(${rx === undefined ? sig(at(o)[0], sf) : shownReadout(at(o)[0], rx)}, ${ry === undefined ? sig(at(o)[1], sf) : shownReadout(at(o)[1], ry)})`;
  const label = t("sim.predict.overlay", { name: t(rx?.labelKey ?? mx), before: point(seen.before), after: point(seen.after) });
  return (
    <div className="max-w-xl rounded-card border border-line bg-canvas p-2" data-sim-chart="overlay">
      <svg viewBox={`0 0 ${CW} ${H}`} className="w-full" role="img" aria-label={label}>
        <clipPath id={clipId}><rect x={L} y={T + 24} width={CW - L - R} height={H - T - 24 - B} /></clipPath>
        <line x1={L} x2={CW - R} y1={Y(0)} y2={Y(0)} stroke={STRONG} strokeWidth={1} />
        <line x1={X(0)} x2={X(0)} y1={T} y2={H - B} stroke={STRONG} strokeWidth={1} />
        <polyline points={polyline(before, X, Y)} fill="none" stroke={MUTED} strokeWidth={2} strokeDasharray="6 5" clipPath={`url(#${clipId})`} data-sim-run="before" />
        <polyline points={polyline(after, X, Y)} fill="none" stroke={INK} strokeWidth={2.5} clipPath={`url(#${clipId})`} data-sim-run="after" />
        {([["before", seen.before, MUTED], ["after", seen.after, INK]] as const).map(([id, o, colour], i) => {
          const [px, py] = at(o);
          if (!Number.isFinite(px) || !Number.isFinite(py)) return null;
          return (
            <g key={id} data-sim-end={id}>
              <circle cx={X(px)} cy={Y(py)} r={4.5} fill={i === 1 ? INK : "var(--color-canvas)"} stroke={colour} strokeWidth={1.5} />
              <line x1={X(px)} x2={X(px)} y1={Y(py) - 6} y2={T + 8 + i * 16} stroke={colour} strokeWidth={0.75} strokeDasharray="2 3" />
              <text x={X(px)} y={T + 4 + i * 16} textAnchor="middle" fontSize={12} fill={colour} fontWeight={i === 1 ? 600 : 400}>{`${t(i === 0 ? "sim.predict.before" : "sim.predict.after")} ${point(o)}`}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
