// INP-05: a bounded, deterministic formatting grammar. Meaning is checked by math_parse on submission.
import { InputError, INPUT_LIMITS, normaliseInput } from "./input.js";

export type EntryMode = "maths" | "chem" | "units";
export type Conversion = { latex: string; serialised: string; error: null } |
  { latex: string; serialised: string; error: { code: string; position: number; word: string } };
interface Token { value: string; at: number }
interface Term { latex: string; grouped?: boolean; numeric?: boolean }
const GREEK = new Set("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi rho sigma tau upsilon phi chi psi omega Delta Gamma Theta Lambda Sigma Phi Psi Omega".split(" "));
const FUNCTIONS = new Set("sin cos tan sec csc cot asin acos atan arcsin arccos arctan sinh cosh tanh ln log exp".split(" "));
const REL: Record<string, string> = { "=": "=", "<": "<", ">": ">", "<=": "\\le", ">=": "\\ge", "!=": "\\ne", "+-": "\\pm", "~=": "\\approx", "->": "\\to" };
const COMMAND: Record<string, string> = { times: "*", cdot: "*", div: "÷", le: "<=", leq: "<=", ge: ">=", geq: ">=", ne: "!=", neq: "!=", pm: "+-", infty: "inf", approx: "~=", to: "->" };
const SUPERSCRIPT: Record<string, string> = Object.fromEntries([..."⁰¹²³⁴⁵⁶⁷⁸⁹⁻"].map((v, i) => [v, "0123456789-"[i] ?? ""]));

const SUBSCRIPT: Record<string, string> = Object.fromEntries([..."₀₁₂₃₄₅₆₇₈₉"].map((v, i) => [v, String(i)]));
const SUPERSCRIPT_RUN = /[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+/g;
const superscriptDigits = (run: string) => [...run].map((ch) => ch === "⁺" ? "+" : SUPERSCRIPT[ch] ?? "").join("");

/** Standard form as students type it ("3.2x10^-4", "3.2 X 10^(-4)", "3.2*10**-4", "3.2×10⁻⁴") as e-notation, the
 * one spelling the formatter, the unit reader and the marker share; only a "10" raised to a whole power after a
 * number is read this way, so "2x" stays algebra. */
export function standardFormAsE(source: string): string {
  return source.replace(SUPERSCRIPT_RUN, (run) => `^${superscriptDigits(run)}`).replace(/\*\*/g, "^")
    .replace(/(\d(?:\.\d+)?)\s*(?:[xX×*·]|\\times)\s*10\s*\^\s*[({]?\s*([-+−]?\d+)(?:\s*[)}])?/g, (_m, mantissa: string, power: string) => `${mantissa}e${power.replace("−", "-")}`);
}

/** Unicode the student pasted or typed with a symbol bar, as the plain grammar: superscript digits and signs as
 * "^…", subscript digits as plain digits (chemistry) or "_…" (maths). */
export function plainScripts(source: string, subscripts: "digits" | "underscore"): string {
  return source.replace(SUPERSCRIPT_RUN, (run) => `^${superscriptDigits(run)}`)
    .replace(/[₀₁₂₃₄₅₆₇₈₉]+/g, (run) => (subscripts === "underscore" ? "_" : "") + [...run].map((ch) => SUBSCRIPT[ch] ?? "").join(""));
}

function linearUnicode(source: string): string {
  const greek: Record<string, string> = { "π": "pi", "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "θ": "theta", "λ": "lambda", "μ": "mu", "ρ": "rho", "σ": "sigma", "φ": "phi", "ω": "omega", "Δ": "Delta", "Ω": "Omega" };
  return source.replace(/[παβγδθλμρσφωΔΩ]/g, (ch) => ` ${greek[ch]} `)
    .replace(/[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+/g, (s) => `^(${[...s].map((ch) => SUPERSCRIPT[ch]).join("")})`)
    .replace(/−/g, "-").replace(/[×·]/g, "*")
    .replace(/≤/g, "<=").replace(/≥/g, ">=").replace(/≠/g, "!=")
    .replace(/½/g, "(1/2)").replace(/¼/g, "(1/4)").replace(/¾/g, "(3/4)")
    .replace(/°(?!C)/g, " deg ").replace(/[₀₁₂₃₄₅₆₇₈₉]+/g, (run) => `_(${[...run].map((ch) => SUBSCRIPT[ch] ?? "").join("")})`);
}

function tokens(source: string): Token[] {
  const out: Token[] = [];
  const pattern = /\s+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+|\\(?:[A-Za-z]+|[{}|,;!])|[A-Za-z]+|<=>|\*\*|<=|>=|!=|\+-|~=|->|./gu;
  for (const match of source.matchAll(pattern)) {
    let value = match[0];
    if (/^\s+$/.test(value)) continue;
    if (value.startsWith("\\")) {
      const name = value.slice(1);
      if (["left", "right", ",", ";", "!"].includes(name)) continue;
      value = COMMAND[name] ?? name;
      if (!(GREEK.has(value) || FUNCTIONS.has(value) || value === "pi" ||
          ["frac", "dfrac", "tfrac", "sqrt", "{", "}", "|", ...Object.values(COMMAND)].includes(value))) {
        throw new InputError("unknown_word", match.index, name);
      }
    }
    out.push({ value: value === "**" ? "^" : value, at: match.index });
  }
  out.push({ value: "", at: source.length });
  return out;
}

class Formatter {
  private index = 0;
  private depth = 0;
  private powerDepth = 0;
  constructor(private readonly list: Token[]) {}
  private peek(): Token { return this.list[this.index] ?? { value: "", at: this.list.at(-1)?.at ?? 0 }; }
  private take(): Token { const token = this.peek(); this.index += 1; return token; }
  private expect(value: string): void {
    if (this.peek().value !== value) throw new InputError("unbalanced_bracket", this.peek().at);
    this.take();
  }
  format(): string {
    const result = this.relation();
    if (this.peek().value !== "") throw new InputError("unknown_word", this.peek().at, this.peek().value);
    return result.latex;
  }
  private relation(): Term {
    let result = this.sum();
    let previous: string | null = null;
    while (["=", "<", ">", "<=", ">=", "!=", "~=", "->"].includes(this.peek().value)) {
      const op = this.take();
      if (previous !== null && !(previous.includes("<") && op.value.includes("<") || previous.includes(">") && op.value.includes(">"))) {
        throw new InputError("two_relations", op.at);
      }
      previous = op.value;
      result = { latex: `${result.latex} ${REL[op.value]} ${this.sum().latex}` };
    }
    return result;
  }
  private sum(): Term {
    let result = this.product();
    while (["+", "-", "+-"].includes(this.peek().value)) {
      const op = this.take().value;
      const rhs = this.product();
      result = { latex: `${result.latex} ${REL[op] ?? op} ${rhs.latex}`, numeric: Boolean(result.numeric && rhs.numeric) };
    }
    return result;
  }
  private product(): Term {
    let result = this.power();
    while (true) {
      const op = this.peek().value;
      if (op === "*" || op === "/" || op === "." || op === "÷") {
        this.take();
        const rhs = this.power();
        result = { latex: op === "/" ? `\\frac{${result.latex}}{${rhs.latex}}` : `${result.latex} ${op === "÷" ? "\\div" : result.numeric && rhs.numeric ? "\\times" : "\\cdot"} ${rhs.latex}`, numeric: Boolean(result.numeric && rhs.numeric) };
      } else if (/^(?:[A-Za-z\d]|\(|\{|√|∞)/u.test(op)) {
        result = { latex: `${result.latex} ${this.power().latex}` };
      } else break;
    }
    return result;
  }
  private power(): Term {
    if (++this.powerDepth > 64) throw new InputError("too_deep", this.peek().at);
    try { return this.innerPower(); } finally { this.powerDepth -= 1; }
  }
  private innerPower(): Term {
    const prefix = this.peek().value;
    if (prefix === "+" || prefix === "-") {
      this.take();
      const rhs = this.power();
      return { latex: `${prefix}${rhs.latex}`, numeric: Boolean(rhs.numeric) };
    }
    let result = this.atom();
    while (this.peek().value === "_" || this.peek().value === "^") {
      const script = this.take().value;
      // A power is right-associative; subscripts consume one atom.
      const rhs = script === "^" ? this.power() : this.atom();
      result = { latex: `${result.latex}${script}{${rhs.grouped ? rhs.latex.slice(1, -1) : rhs.latex}}`, numeric: Boolean(result.numeric && rhs.numeric) };
    }
    if (this.peek().value === "deg") { this.take(); result = { latex: `${result.latex}^{\\circ}` }; }
    return result;
  }
  private atom(): Term {
    if (++this.depth > 64) throw new InputError("too_deep", this.peek().at);
    try { return this.innerAtom(); } finally { this.depth -= 1; }
  }
  private innerAtom(): Term {
    const token = this.take();
    const value = token.value;
    if (value === "" || value === "□" || [")", "}", "]", ","].includes(value)) throw new InputError("empty_slot", token.at);
    if (value === "(" || value === "{") {
      const result = this.relation();
      this.expect(value === "(" ? ")" : "}");
      return { latex: `(${result.latex})`, grouped: true, numeric: Boolean(result.numeric) };
    }
    if (/^(?:\d|\.\d)/.test(value)) {
      // Standard form reads back the way it is written in exams: 3.2 × 10⁻⁴, never "3.2e-4", and one group never
      // breaks across lines between the number and its power of ten.
      const standard = /^(.*?)[eE]([-+]?\d+)$/.exec(value);
      return { latex: standard === null ? value : `{${standard[1]} \\times 10^{${(standard[2] ?? "").replace(/^\+/, "")}}}`, numeric: true };
    }
    if (value === "pi") return { latex: "\\pi" };
    if (value === "inf" || value === "∞") return { latex: "\\infty" };
    if (GREEK.has(value)) return { latex: `\\${value}` };
    if (["sqrt", "cbrt", "root", "frac", "dfrac", "tfrac", "abs"].includes(value)) {
      let degree: string | null = value === "cbrt" ? "3" : null;
      if (value === "sqrt" && this.peek().value === "[") {
        this.take(); degree = this.sum().latex; this.expect("]");
      }
      const braces = this.peek().value === "{";
      const bracket = this.take();
      if (bracket.value !== "(" && bracket.value !== "{") throw new InputError("unbalanced_bracket", bracket.at);
      let argument = this.relation().latex;
      if (value === "root" || value.includes("frac")) {
        if (braces && value.includes("frac")) { this.expect("}"); this.expect("{"); }
        else this.expect(",");
        const second = this.relation().latex;
        if (value === "root") { degree = argument; argument = second; }
        else argument = `\\frac{${argument}}{${second}}`;
      }
      this.expect(braces ? "}" : ")");
      if (value.includes("frac")) return { latex: argument };
      return { latex: value === "abs" ? `|${argument}|` : `\\sqrt${degree === null ? "" : `[${degree}]`}{${argument}}` };
    }
    if (value === "√") {
      const arg = this.power();
      return { latex: `\\sqrt{${arg.grouped ? arg.latex.slice(1, -1) : arg.latex}}` };
    }
    if (value === "|") {
      const arg = this.relation(); this.expect("|"); return { latex: `|${arg.latex}|` };
    }
    if (FUNCTIONS.has(value)) {
      let script = "";
      if (this.peek().value === "^" || this.peek().value === "_") {
        const op = this.take().value;
        const arg = this.atom(); script = `${op}{${arg.grouped ? arg.latex.slice(1, -1) : arg.latex}}`;
      }
      let arg = this.power();
      while (/^(?:[A-Za-z\d]|\(|\{|√|∞)/u.test(this.peek().value) && !FUNCTIONS.has(this.peek().value)) {
        arg = { latex: `${arg.latex} ${this.power().latex}` };
      }
      const name = { asin: "arcsin", acos: "arccos", atan: "arctan" }[value] ?? value;
      return { latex: `\\${name}${script} ${arg.latex}` };
    }
    if (/^[A-Za-z]{1,3}$/.test(value)) return { latex: value };
    throw new InputError("unknown_word", token.at, value);
  }
}

/** Canonical decimal points without changing frac/root argument commas. */
export function canonicalDecimalCommas(source: string): string {
  const frames: { close: string; separator: boolean }[] = []; let result = "";
  for (let at = 0; at < source.length; at++) {
    const char = source[at] ?? "";
    if (char === "(" || char === "{") frames.push({ close: char === "(" ? ")" : "}", separator: char === "(" && /(?:frac|dfrac|tfrac|root)\s*$/.test(source.slice(0, at)) });
    if (char === ")" || char === "}") { if (frames.at(-1)?.close === char) frames.pop(); }
    result += char === "," && (at === 0 || /[\d=(+\-*/\s]/.test(source[at - 1] ?? "")) && /\d/.test(source[at + 1] ?? "") && !frames.at(-1)?.separator ? "." : char;
  }
  return result;
}

/** Outside a node, only a complete unambiguous mathematical token is converted. */
export function isMathShortcut(source: string): boolean {
  return !/\s/.test(source) && /[\^_]|(?:sqrt|cbrt|root|frac|abs)\(|<=|>=|!=|~=|\+-|->|=|\d[A-Za-z]/.test(source);
}

export function convertMath(source: string): Conversion {
  const text = normaliseInput(source).trim();
  try {
    if (text.length > INPUT_LIMITS.expression) throw new InputError("too_long", INPUT_LIMITS.expression);
    // Keep mixed numbers as a sum, rather than turning their whitespace into an implicit product.
    const linear = linearUnicode(standardFormAsE(canonicalDecimalCommas(text))).replace(/\b(\d+)\s+(\d+)\s*\/\s*(\d+)\b/g, "($1+$2/$3)");
    const latex = new Formatter(tokens(linear)).format();
    return { latex, serialised: latex, error: null };
  } catch (error) {
    if (!(error instanceof InputError)) throw error;
    return { latex: text, serialised: text, error: { code: error.code, position: error.position, word: error.word } };
  }
}

// A student's line on the board (UXS-15, UXS-16) is what they typed, as plain maths input ("v^2 = u^2 + 2as",
// "a = 9.8 m/s^2", "u = 0, s = 20 m"): it is shown typeset, the way the tutor's lines are. A trailing unit after a
// number is upright, a thin space after the number; assignments separated by ", " sit side by side. A line that is
// words (or already carries \(…\) maths) is not maths input and stays as typed.
const UNIT_ATOMS = ["kg", "g", "mg", "km", "cm", "mm", "nm", "m", "s", "ms", "min", "h", "N", "kN", "J", "kJ", "W", "kW", "Pa", "kPa",
  "V", "A", "mA", "Ω", "ohm", "Hz", "C", "K", "mol", "L", "ml", "mL", "dm", "°C", "%"];
const UNIT_ATOM = `(?:${[...UNIT_ATOMS].sort((a, b) => b.length - a.length).join("|")})(?:\\^-?\\d+)?`;
const UNIT_TAIL = new RegExp(`^(.*\\d)(\\s*)(${UNIT_ATOM}(?:\\s*[/·*]\\s*${UNIT_ATOM}|\\s+${UNIT_ATOM})*)$`, "u");

function unitLatex(unit: string): string {
  const pieces = unit.split(/\s*([/·*])\s*|\s+/u).filter((piece): piece is string => piece !== undefined && piece !== "");
  let latex = "";
  for (const piece of pieces) {
    if (piece === "/") {
      latex += "/";
      continue;
    }
    if (piece === "·" || piece === "*") {
      latex += "\\,";
      continue;
    }
    const [atom = "", power] = piece.split("^");
    const base = atom === "Ω" || atom === "ohm" ? "\\Omega" : atom === "°C" ? "{}^{\\circ}\\mathrm{C}" : atom === "%" ? "\\%" : `\\mathrm{${atom}}`;
    // Two units side by side ("N m") keep a thin space between them.
    if (latex !== "" && !latex.endsWith("/") && !latex.endsWith("\\,")) latex += "\\,";
    latex += power === undefined ? base : `${base}^{${power}}`;
  }
  return latex;
}

function assignmentLatex(source: string): string | null {
  const text = source.trim();
  if (text === "" || !/[\d=+\-*/^_<>]/.test(text)) return null;
  const tail = UNIT_TAIL.exec(text);
  // A unit stuck to its number ("20m") reads as a product unless it has a "/" or a power ("9.8m/s^2").
  const unit = tail !== null && (tail[2] !== "" || /[/^]/.test(tail[3] ?? "")) ? tail : null;
  const expression = convertMath(unit === null ? text : (unit[1] ?? ""));
  if (expression.error !== null) return null;
  return unit === null ? expression.latex : `${expression.latex}\\ ${unitLatex(unit[3] ?? "")}`;
}

/** The LaTeX of a student's board line typed as maths input, or null when it is words and stays as typed. */
export function boardLineLatex(line: string): string | null {
  const text = plainScripts(normaliseInput(line), "underscore").trim();
  if (text === "" || /\\\(|\\\[|\$/.test(text)) return null;
  const parts = text.split(/\s*[,;]\s+/u).map(assignmentLatex);
  if (parts.some((p) => p === null)) return null;
  return parts.join(",\\quad ");
}

const UNIT_ONLY = new RegExp(`^${UNIT_ATOM}(?:\\s*[/·*]\\s*${UNIT_ATOM}|\\s+${UNIT_ATOM})*$`, "u");

/** A unit typed on its own ("m/s^2", "kg m^-3", "ohm"), typeset as on the board (UXS-24's unit field and tries), or
 * null when it is not a unit this reads and stays as typed. */
export function unitLatexOf(unit: string): string | null {
  const text = plainScripts(normaliseInput(unit), "digits").replace(/\bdeg\s*C\b/g, "°C").trim();
  return text !== "" && UNIT_ONLY.test(text) ? unitLatex(text) : null;
}
