// Chemistry kinds (VSL-03): balanced_equation (a board kind, drawn as HTML), particle_diagram, electron_shells,
// periodic_table and energy_profile. Coefficients and particle counts come from chem_balance; electron arrangements and
// relative masses from the pack's reference data (N3); energies are traced givens. Formula subscripts are part of the
// formula, not figures.
import { must } from "./must.js";
import type { ReactElement } from "react";
import { define } from "./define.js";
import {
  Arrow, Frame, INK, LINE, Label, MUTED, PAPER, Part, RING, SERIES, STRONG, TINT, WordsFallback, areaFill, computed, computedLabel, highlighted,
  isAddedStep, lineStyle, params, rowProps, type Scene,
} from "./kit.js";
import particleSchema from "./schemas/particle_diagram.schema.json" with { type: "json" };
import shellsSchema from "./schemas/electron_shells.schema.json" with { type: "json" };
import periodicSchema from "./schemas/periodic_table.schema.json" with { type: "json" };
import energySchema from "./schemas/energy_profile.schema.json" with { type: "json" };

const W = 480;
const H = 320;
type Pt = [number, number];

/** A formula with its digits as subscripts (and a trailing charge as a superscript): structure, not figures. */
export function Formula({ text }: { text: string }): ReactElement {
  const pieces = text.match(/[A-Z][a-z]?|\d+|[()[\]]|[+-]$|./g) ?? [];
  return (
    <span data-formula="true">
      {pieces.map((piece, i) => (/^\d+$/.test(piece) && i > 0 ? <sub key={i}>{piece}</sub> : /^[+-]$/.test(piece) && i === pieces.length - 1 ? <sup key={i}>{piece}</sup> : <span key={i}>{piece}</span>))}
    </span>
  );
}

/** Atoms in a formula, expanded (H2O -> H, H, O; brackets multiply). */
export function atomsOf(formula: string): string[] {
  const tokens = formula.match(/[A-Z][a-z]?|\d+|\(|\)/g) ?? [];
  const stack: string[][] = [[]];
  for (let i = 0; i < tokens.length; i += 1) {
    const tok = must(tokens[i]);
    const next = tokens[i + 1];
    const times = next !== undefined && /^\d+$/.test(next) ? Number(next) : 1;
    if (tok === "(") stack.push([]);
    else if (tok === ")") {
      const group = stack.pop() ?? [];
      for (let k = 0; k < times; k += 1) must(stack[stack.length - 1]).push(...group);
      if (times > 1 || next === "1") i += 1;
    } else if (/^[A-Z]/.test(tok)) {
      for (let k = 0; k < times; k += 1) must(stack[stack.length - 1]).push(tok);
      if (next !== undefined && /^\d+$/.test(next)) i += 1;
    }
  }
  return stack[0] ?? [];
}

// --- balanced_equation ----------------------------------------------------------------------------------------------

interface Species {
  formula: string;
  state?: string;
}

function drawEquation(s: Scene): ReactElement {
  const p = params<{ reactants: Species[]; products: Species[]; arrow?: string; show_states?: boolean; show_atom_table?: boolean }>(s.spec);
  const rows = computed<{ atom_counts?: { element: string; reactants: number; products: number }[] }>(s.spec).atom_counts ?? [];
  const term = (sp: Species, id: string) => {
    const coefficient = (computedLabel(s.spec, id, sp.formula).match(/^\d+/) ?? [""])[0];
    return (
      <span key={id} {...rowProps(s, id, "button")}>
        {s.labels === "hidden" ? null : <>{coefficient}<Formula text={sp.formula} />{p.show_states !== false && sp.state !== undefined ? `(${sp.state})` : ""}</>}
      </span>
    );
  };
  const side = (list: Species[], prefix: string) => list.flatMap((sp, i) => [...(i > 0 ? [<span key={`plus${prefix}${i}`} aria-hidden="true"> + </span>] : []), term(sp, `${prefix}${i + 1}`)]);
  return (
    <div className="my-2 rounded-card border border-line p-3" role="group" aria-label={s.labels === "shown" ? s.spec.alt.summary : s.t("visual.alt.labels_hidden", { count: s.count })} data-kind="balanced_equation">
      <p className="text-body">
        {side(p.reactants, "r")}
        <span aria-hidden="true">{p.arrow === "equilibrium" ? " ⇌ " : " → "}</span>
        {side(p.products, "p")}
      </p>
      {p.show_atom_table !== false && rows.length > 0 && (
        <table className="mt-2 text-ui">
          <thead><tr><th scope="col">{s.t("visual.equation.element")}</th><th scope="col">{s.t("visual.equation.left")}</th><th scope="col">{s.t("visual.equation.right")}</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.element} {...rowProps(s, `atoms.${row.element}`)}>
                <td>{row.element}</td><td>{s.labels === "hidden" ? "" : row.reactants}</td><td>{s.labels === "hidden" ? "" : row.products}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export const balancedEquation = define({ kind: "balanced_equation", schema: "visuals/kinds/balanced_equation", draw: drawEquation });

// --- particle_diagram -----------------------------------------------------------------------------------------------

/** A deterministic scatter (the same spec draws the same picture: VSL-18). */
function scatter(seed: string, n: number, box: [number, number, number, number], gap: number): Pt[] {
  let h = 2166136261;
  for (const ch of seed) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
  const rnd = () => ((h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0) % 10000) / 10000;
  const out: Pt[] = [];
  for (let tries = 0; out.length < n && tries < n * 200; tries += 1) {
    const q: Pt = [box[0] + rnd() * (box[2] - box[0]), box[1] + rnd() * (box[3] - box[1])];
    if (out.every((o) => Math.hypot(o[0] - q[0], o[1] - q[1]) >= gap)) out.push(q);
  }
  return out;
}

const ELEMENT_SHADE: Record<string, string> = {};
function shadeOf(element: string): string {
  if (ELEMENT_SHADE[element] === undefined) ELEMENT_SHADE[element] = must(SERIES[Object.keys(ELEMENT_SHADE).length % SERIES.length]);
  return must(ELEMENT_SHADE[element]);
}

function Molecule({ formula, at, s }: { formula: string; at: Pt; s: Scene }): ReactElement {
  const atoms = atomsOf(formula);
  return (
    <g>
      {atoms.map((el, i) => {
        const a = (i / Math.max(1, atoms.length)) * Math.PI * 2;
        const r = atoms.length === 1 ? 0 : 7;
        return (
          <g key={i}>
            <circle cx={at[0] + r * Math.cos(a)} cy={at[1] + r * Math.sin(a)} r={7} fill={shadeOf(el)} fillOpacity={0.55} stroke={INK} strokeWidth={1} />
            {s.labels !== "hidden" && atoms.length <= 3 && <text x={at[0] + r * Math.cos(a)} y={at[1] + r * Math.sin(a) + 3} textAnchor="middle" fontSize={8 * s.scale} fill={INK} data-formula="true">{el}</text>}
          </g>
        );
      })}
    </g>
  );
}

function drawParticles(s: Scene): ReactElement {
  const p = params<{ mode: "state" | "reaction"; state?: "solid" | "liquid" | "gas" }>(s.spec);
  if (p.mode === "state") {
    const state = p.state ?? "solid";
    const box: [number, number, number, number] = [130, 70, 350, 270];
    const pts: Pt[] = state === "solid"
      ? Array.from({ length: 36 }, (_, i) => [148 + (i % 6) * 32, 106 + Math.floor(i / 6) * 30] as Pt)
      : state === "liquid" ? scatter(`${s.spec.visual_id}l`, 30, [box[0] + 14, box[1] + 70, box[2] - 14, box[3] - 14], 24) : scatter(`${s.spec.visual_id}g`, 10, [box[0] + 14, box[1] + 14, box[2] - 14, box[3] - 14], 40);
    return (
      <Frame s={s} width={W} height={H}>
        <Part s={s} id="container" focusable={false}><path d={`M${box[0]},${box[1]} L${box[0]},${box[3]} L${box[2]},${box[3]} L${box[2]},${box[1]}`} fill="none" stroke={INK} strokeWidth={2} /></Part>
        <Part s={s} id="particles">{pts.map((q, i) => <circle key={i} cx={q[0]} cy={q[1]} r={12} fill={areaFill(s, "particles", SERIES[0])} fillOpacity={0.5} stroke={INK} strokeWidth={1} />)}</Part>
      </Frame>
    );
  }
  const species = computed<{ species?: { formula: string; side: string; coefficient: number }[] }>(s.spec).species;
  if (species === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const boxes: Record<string, [number, number, number, number]> = { reactants: [20, 40, 220, 260], products: [260, 40, 460, 260] };
  const groups = species.map((sp, i) => {
    const b = must(boxes[sp.side]);
    const sameSide = species.filter((o) => o.side === sp.side);
    const k = sameSide.indexOf(sp);
    const band: [number, number, number, number] = [b[0] + 10, b[1] + 10 + (k * (b[3] - b[1] - 20)) / sameSide.length, b[2] - 10, b[1] + 10 + ((k + 1) * (b[3] - b[1] - 20)) / sameSide.length];
    return { sp, i, band, at: scatter(`${s.spec.visual_id}${i}`, sp.coefficient, band, 30) };
  });
  return (
    <Frame s={s} width={W} height={H}>
      {(["before", "after"] as const).map((id, n) => {
        const b = must(boxes[n === 0 ? "reactants" : "products"]);
        return (
          <Part key={id} s={s} id={id}>
            <rect x={b[0]} y={b[1]} width={b[2] - b[0]} height={b[3] - b[1]} fill={areaFill(s, id, PAPER)} {...lineStyle(s, id)} />
            {groups.filter((g) => g.sp.side === (n === 0 ? "reactants" : "products")).flatMap((g) => g.at.map((q, j) => <Molecule key={`${g.i}-${j}`} formula={g.sp.formula} at={q} s={s} />))}
          </Part>
        );
      })}
      <Arrow x1={226} y1={150} x2={254} y2={150} />
      {groups.map((g) => (
        <Part key={`sp${g.i + 1}`} s={s} id={`sp${g.i + 1}`}>
          <Label s={s} part={`sp${g.i + 1}`} x={(g.band[0] + g.band[2]) / 2} y={g.sp.side === "reactants" ? 285 + (g.i % 2) * 16 : 285 + (g.i % 2) * 16} text={computedLabel(s.spec, `sp${g.i + 1}`, g.sp.formula)} anchor="middle" size={12} />
        </Part>
      ))}
    </Frame>
  );
}

export const particleDiagram = define({ kind: "particle_diagram", schema: particleSchema, draw: drawParticles });

// --- electron_shells ------------------------------------------------------------------------------------------------

function Atom({ s, at, symbol, shells, prefix, mark }: { s: Scene; at: Pt; symbol: string; shells: number[]; prefix: string; mark: "dot" | "cross" }): ReactElement {
  const nucleus = prefix === "" ? "nucleus" : "partner_nucleus";
  return (
    <g>
      <Part s={s} id={nucleus}>
        <circle cx={at[0]} cy={at[1]} r={16} fill={areaFill(s, nucleus, PAPER)} {...lineStyle(s, nucleus)} />
        <text x={at[0]} y={at[1] + 5} textAnchor="middle" fontSize={14 * s.scale} fill="var(--color-ink)" data-formula="true">{symbol}</text>
      </Part>
      {shells.map((count, n) => {
        const id = `${prefix}shell_${n + 1}`;
        const r = 30 + n * 22;
        return (
          <Part key={id} s={s} id={id}>
            <circle cx={at[0]} cy={at[1]} r={r} fill="none" {...lineStyle(s, id, LINE, highlighted(s, id) ? 3 : 1.5)} />
            {Array.from({ length: count }, (_, k) => {
              const a = (k / count) * Math.PI * 2 - Math.PI / 2;
              const ex = at[0] + r * Math.cos(a);
              const ey = at[1] + r * Math.sin(a);
              return mark === "dot" ? <circle key={k} cx={ex} cy={ey} r={4} fill={INK} /> : <path key={k} d={`M${ex - 4},${ey - 4} L${ex + 4},${ey + 4} M${ex - 4},${ey + 4} L${ex + 4},${ey - 4}`} stroke={INK} strokeWidth={1.5} />;
            })}
          </Part>
        );
      })}
    </g>
  );
}

function drawShells(s: Scene): ReactElement {
  const p = params<{ element: string; charge?: number; partner?: string; bonding?: string }>(s.spec);
  const c = computed<{ shells?: number[]; partner?: { element: string; shells: number[] } }>(s.spec);
  if (c.shells === undefined) return <WordsFallback spec={s.spec} reason="text_only" />;
  const two = c.partner !== undefined;
  const at: Pt = two ? [150, 150] : [240, 150];
  const charge = p.charge ?? 0;
  const chargeText = charge === 0 ? "" : `${Math.abs(charge) === 1 ? "" : Math.abs(charge)}${charge > 0 ? "+" : "−"}`;
  const outer = 30 + (c.shells.length - 1) * 22 + 14;
  return (
    <Frame s={s} width={W} height={H}>
      <Atom s={s} at={at} symbol={p.element} shells={c.shells} prefix="" mark="dot" />
      {charge !== 0 && (
        <g aria-hidden="true">
          <path d={`M${at[0] - outer + 8},${at[1] - outer} L${at[0] - outer},${at[1] - outer} L${at[0] - outer},${at[1] + outer} L${at[0] - outer + 8},${at[1] + outer} M${at[0] + outer - 8},${at[1] - outer} L${at[0] + outer},${at[1] - outer} L${at[0] + outer},${at[1] + outer} L${at[0] + outer - 8},${at[1] + outer}`} fill="none" stroke={INK} strokeWidth={1.5} />
          <text x={at[0] + outer + 4} y={at[1] - outer + 10} fontSize={14 * s.scale} fill="var(--color-ink)" data-formula="true">{chargeText}</text>
        </g>
      )}
      {two && <Atom s={s} at={[p.bonding === "covalent" ? 150 + 30 + (c.shells.length - 1) * 22 + 30 + ((must(c.partner).shells.length - 1) * 22) - 10 : 340, 150]} symbol={must(c.partner).element} shells={must(c.partner).shells} prefix="partner_" mark="cross" />}
      {s.labels !== "hidden" && <text x={at[0]} y={H - 20} textAnchor="middle" fontSize={13 * s.scale} fill="var(--color-ink)">{c.shells.map((n, i) => computedLabel(s.spec, `shell_${i + 1}`, String(n))).join(",")}</text>}
    </Frame>
  );
}

export const electronShells = define({ kind: "electron_shells", schema: shellsSchema, draw: drawShells });

// --- periodic_table -------------------------------------------------------------------------------------------------

/** Element symbols in proton-number order (layout data: positions, never figures shown as claims). */
export const SYMBOLS = (
  "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe " +
  "Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split(" ");

/** Where element z sits: [period, column 1-18] for the main table, [8 or 9, 3-17] for the f-block rows. */
export function cell(z: number): [number, number] {
  const starts = [1, 3, 11, 19, 37, 55, 87];
  const period = starts.filter((st) => z >= st).length;
  const offset = z - must(starts[period - 1]);
  if (period === 1) return [1, z === 1 ? 1 : 18];
  if (period <= 3) return [period, offset < 2 ? offset + 1 : offset + 11];
  if (period <= 5) return [period, offset + 1];
  if (offset < 2) return [period, offset + 1];
  if (offset >= 2 && offset <= 16) return [period + 2, offset + 1];
  return [period, offset - 13];
}

const MAIN_COLUMNS = [1, 2, 13, 14, 15, 16, 17, 18];

function blockOf(z: number): string {
  const [period, col] = cell(z);
  if (period >= 8) return "f";
  if (col <= 2 || z === 2) return "s";
  if (col >= 13) return "p";
  return "d";
}

function drawPeriodic(s: Scene): ReactElement {
  const p = params<{ highlight?: { kind: string; values: string[] }; trend?: string }>(s.spec);
  const cw = 24;
  const ch = 26;
  const left = 22;
  const top = 34;
  const h = p.highlight;
  const lit = (z: number): boolean => {
    if (h === undefined) return false;
    const [period, col] = cell(z);
    const sym = must(SYMBOLS[z - 1]);
    if (h.kind === "group") return h.values.some((g) => MAIN_COLUMNS[["1", "2", "3", "4", "5", "6", "7", "0"].indexOf(g)] === col && period <= 7);
    if (h.kind === "period") return h.values.includes(String(period > 7 ? period - 2 : period));
    if (h.kind === "block") return h.values.includes(blockOf(z));
    return h.values.includes(sym);
  };
  const xy = (z: number): Pt => {
    const [period, col] = cell(z);
    return [left + (col - 1) * cw, top + (period - 1) * ch + (period >= 8 ? 10 : 0)];
  };
  const listed = h?.kind === "elements" ? h.values : [];
  return (
    <Frame s={s} width={W} height={H}>
      {["1", "2", "3", "4", "5", "6", "7", "0"].map((g, i) => (
        <Part key={g} s={s} id={`group_${g}`}><text x={left + (must(MAIN_COLUMNS[i]) - 1) * cw + cw / 2} y={top - 8} textAnchor="middle" fontSize={10 * s.scale} fill={highlighted(s, `group_${g}`) ? RING : MUTED} data-presentation="true">{g}</text></Part>
      ))}
      {[1, 2, 3, 4, 5, 6, 7].map((n) => (
        <Part key={n} s={s} id={`period_${n}`}><text x={left - 6} y={top + (n - 1) * ch + 17} textAnchor="end" fontSize={10 * s.scale} fill={highlighted(s, `period_${n}`) ? RING : MUTED} data-presentation="true">{n}</text></Part>
      ))}
      <g aria-hidden="true">
        {SYMBOLS.map((sym, i) => {
          if (listed.includes(sym)) return null;
          const [x, y] = xy(i + 1);
          return (
            <g key={sym}>
              <rect x={x} y={y} width={cw - 2} height={ch - 2} fill={lit(i + 1) ? TINT : PAPER} stroke={lit(i + 1) ? STRONG : LINE} strokeWidth={1} />
              <text x={x + (cw - 2) / 2} y={y + 16} textAnchor="middle" fontSize={9 * s.scale} fill="var(--color-ink)" data-formula="true">{sym}</text>
            </g>
          );
        })}
      </g>
      {listed.map((sym) => {
        const z = SYMBOLS.indexOf(sym) + 1;
        if (z <= 0) return null;
        const [x, y] = xy(z);
        const id = `el_${sym}`;
        const mass = computedLabel(s.spec, id);
        return (
          <Part key={id} s={s} id={id}>
            <rect x={x} y={y} width={cw - 2} height={ch - 2} fill={TINT} {...lineStyle(s, id, STRONG, 2)} />
            <text x={x + (cw - 2) / 2} y={y + 16} textAnchor="middle" fontSize={9 * s.scale} fill="var(--color-ink)" data-formula="true">{sym}</text>
            {mass !== "" && <Label s={s} part={id} x={x + (cw - 2) / 2} y={y - 3} text={mass} anchor="middle" size={8} />}
          </Part>
        );
      })}
      {p.trend !== undefined && (
        <Part s={s} id="trend">
          <Arrow x1={left} y1={H - 24} x2={left + 17 * cw} y2={H - 24} stroke={lineStyle(s, "trend", SERIES[0], 2).stroke} width={2} />
          <Label s={s} part="trend" x={left + 9 * cw} y={H - 30} text={s.t(`visual.trend.${p.trend}`)} anchor="middle" size={11} />
        </Part>
      )}
    </Frame>
  );
}

export const periodicTable = define({ kind: "periodic_table", schema: periodicSchema, draw: drawPeriodic });

// --- energy_profile -------------------------------------------------------------------------------------------------

function drawEnergy(s: Scene): ReactElement {
  const p = params<{ reaction: "exothermic" | "endothermic"; reactants?: string; products?: string; activation_energy?: string; enthalpy_change?: string; catalysed?: boolean }>(s.spec);
  const exo = p.reaction === "exothermic";
  const rY = exo ? 200 : 240;
  const pY = exo ? 250 : 170;
  const peak = 80;
  const curve = `M60,${rY} L140,${rY} C200,${rY} 200,${peak} 240,${peak} C280,${peak} 280,${pY} 340,${pY} L420,${pY}`;
  const lowPeak = peak + 60;
  const catalysed = `M140,${rY} C200,${rY} 200,${lowPeak} 240,${lowPeak} C280,${lowPeak} 280,${pY} 340,${pY}`;
  const showCat = p.catalysed === true || isAddedStep(s, "catalysed_path");
  return (
    <Frame s={s} width={W} height={H}>
      <g aria-hidden="true">
        <Arrow x1={40} y1={290} x2={40} y2={30} stroke={MUTED} width={1} />
        <Arrow x1={40} y1={290} x2={450} y2={290} stroke={MUTED} width={1} />
        {s.labels !== "hidden" && <text x={44} y={26} fontSize={11 * s.scale} fill={MUTED}>{s.t("visual.energy.axis")}</text>}
        {s.labels !== "hidden" && <text x={450} y={306} textAnchor="end" fontSize={11 * s.scale} fill={MUTED}>{s.t("visual.energy.progress")}</text>}
      </g>
      <Part s={s} id="reactants"><line x1={60} x2={140} y1={rY} y2={rY} {...lineStyle(s, "reactants", INK, 3)} /><Label s={s} part="reactants" x={100} y={rY + 18} text={p.reactants ?? s.t("visual.energy.reactants")} anchor="middle" size={12} formula={p.reactants !== undefined} /></Part>
      <Part s={s} id="products"><line x1={340} x2={420} y1={pY} y2={pY} {...lineStyle(s, "products", INK, 3)} /><Label s={s} part="products" x={380} y={pY + 18} text={p.products ?? s.t("visual.energy.products")} anchor="middle" size={12} formula={p.products !== undefined} /></Part>
      <Part s={s} id="path"><path d={curve} fill="none" {...lineStyle(s, "path", SERIES[0], 2)} /></Part>
      {p.activation_energy !== undefined && (
        <Part s={s} id="activation_energy">
          <Arrow x1={170} y1={rY} x2={170} y2={peak} stroke={lineStyle(s, "activation_energy").stroke} />
          <Label s={s} part="activation_energy" x={164} y={(rY + peak) / 2} text={`Ea = ${p.activation_energy}`} anchor="end" size={12} />
        </Part>
      )}
      {p.enthalpy_change !== undefined && (
        <Part s={s} id="enthalpy_change">
          <Arrow x1={440} y1={rY} x2={440} y2={pY} stroke={lineStyle(s, "enthalpy_change").stroke} />
          <Label s={s} part="enthalpy_change" x={436} y={(rY + pY) / 2 - 8} text={`ΔH = ${p.enthalpy_change}`} anchor="end" size={12} />
        </Part>
      )}
      {showCat && <Part s={s} id="catalysed_path"><path d={catalysed} fill="none" {...lineStyle(s, "catalysed_path", SERIES[1], 2)} strokeDasharray="6 4" /></Part>}
    </Frame>
  );
}

export const energyProfile = define({ kind: "energy_profile", schema: energySchema, draw: drawEnergy });
