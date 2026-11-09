// INP-08/09: previews preserve case, coefficients and magnitudes. Tools receive the subject's plain grammar.
import { InputError, INPUT_LIMITS, normaliseInput } from "./input.js";
import { canonicalDecimalCommas, convertMath, plainScripts, standardFormAsE, unitLatexOf, type Conversion, type EntryMode } from "./maths-input.js";

const ELEMENTS = new Set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split(" "));
/** A case-only suggestion is offered as a question; never apply it to the learner's source. */
export function elementCaseSuggestion(word: string): string | null {
  const candidate = word.slice(0, 1).toUpperCase() + word.slice(1).toLowerCase();
  return candidate !== word && ELEMENTS.has(candidate) ? candidate : null;
}
const QUANTITY = /^([-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?)\s+(.+)$/;
// Recognise vetted school/SI spellings without turning arbitrary algebra identifiers into upright units.
const SCHOOL_UNITS = /^(?:(?:[yzafpnumkMGTPEZYdcµμ])?(?:m|s|g|N|J|W|Pa|V|A|mol|ohm|Ω|Hz|L|F|H|T|Wb|S)|kg|K|C|deg\s*C|°C|rad|cd|h|min|M|u|kWh|eV|mmHg|atm)(?=$|[\s\d^/*·(])/u;

function bounded(source: string): string {
  const text = normaliseInput(source).trim();
  if (text.length > INPUT_LIMITS.expression) throw new InputError("too_long", INPUT_LIMITS.expression);
  if (text === "" || text.includes("□")) throw new InputError("empty_slot", Math.max(0, text.indexOf("□")));
  return text;
}

function chemistry(source: string): Conversion {
  // "H₂SO₄" and "Fe³⁺" as typed with a symbol bar or pasted; an ion's charge typed without "^" ("Fe3+", "Na+", "Cl-",
  // "SO4 2-" is not read) is a charge when nothing but a space, a bracket or the end follows it.
  const text = plainScripts(bounded(source), "digits").replace(/(?<=[\d)])(?:\.|\*)(?=\d*[A-Z])/g, "·")
    .replace(/(?<=[A-Za-z)\]])(\d*[+-])(?=$|\s|\(|,)/g, (charge: string, _sign: string, offset: number, all: string) => all[offset - 1] === "^" ? charge : `^${charge}`);
  let latex = "";
  let at = 0;
  const stack: string[] = [];
  const pattern = /\s+|->\[[A-Za-z ]{1,40}\]|<=>|->|\^\d*[+-]|\((?:s|l|g|aq)\)|e\^-|[A-Z][a-z]?|\d+|[()+·]|\[|\]/gy;
  while (at < text.length) {
    pattern.lastIndex = at;
    const match = pattern.exec(text);
    if (!match) throw new InputError("unknown_element", at, text.slice(at, at + 2));
    const token = match[0];
    if (/^\s+$/.test(token)) { at = pattern.lastIndex; continue; }
    if (token === "e^-") latex += "\\mathrm{e}^{-}"; // the electron of a half-equation
    else if (/^[A-Z]/.test(token)) {
      if (!ELEMENTS.has(token)) throw new InputError("unknown_element", at, token);
      latex += `\\mathrm{${token}}`;
    } else if (/^\d/.test(token)) {
      latex += /[A-Za-z)\]]/.test(text[at - 1] ?? "") ? `_{${token}}` : token;
    } else if (token.startsWith("^")) latex += `^{${token.slice(1)}}`;
    else if (token.startsWith("->")) latex += token.length > 2 ? ` \\xrightarrow{\\mathrm{${token.slice(3, -1)}}} ` : " \\rightarrow ";
    else if (token === "<=>") latex += " \\rightleftharpoons ";
    else if (/^\((?:s|l|g|aq)\)$/.test(token)) latex += `\\mathrm{${token}}`;
    else if (token === "+") latex += " + ";
    else if (token === "·") latex += "\\cdot ";
    else {
      if (token === "(" || token === "[") stack.push(token);
      if (token === ")" || token === "]") {
        if (stack.pop() !== (token === ")" ? "(" : "[")) throw new InputError("unbalanced_bracket", at);
      }
      latex += token;
    }
    at = pattern.lastIndex;
  }
  if (stack.length > 0) throw new InputError("unbalanced_bracket", text.length);
  return { latex: latex.trim(), serialised: text, error: null };
}

function units(source: string): Conversion {
  const text = standardFormAsE(plainScripts(canonicalDecimalCommas(bounded(source)), "digits")).replace(/μ/g, "µ").replace(/deg\s*C\b/g, "°C").replace(/\b([kMmu]?)ohm\b/g, "$1Ω")
    .replace(/\bu(?=[A-Za-zΩ])/g, "µ");
  const match = QUANTITY.exec(text);
  if (!match) throw new InputError("quantity_expected", 0);
  const value = match[1] ?? "";
  const unit = match[2] ?? "";
  const shown = value.replace(/^(.*)[eE]([-+]?\d+)$/, (_match, mantissa: string, exponent: string) => `${mantissa} \\times 10^{${exponent}}`);
  let latex = `${shown}\\,`;
  let at = 0;
  const pattern = /\s+|\^\(?-?\d+\)?|[A-Za-zµΩ]+|°C|[/*·]/gy;
  while (at < unit.length) {
    pattern.lastIndex = at;
    const part = pattern.exec(unit);
    if (!part) throw new InputError("unknown_word", match[0].length - unit.length + at, unit.slice(at));
    const token = part[0];
    if (token === "C") throw new InputError("ambiguous_unit", text.length - unit.length + at, "C");
    if (token.startsWith("^")) latex += `^{${token.slice(1).replace(/[()]/g, "")}}`;
    else if (token === "/") latex += "/";
    else if (/^[*·\s]/.test(token)) latex += "\\,";
    else latex += `\\mathrm{${token.replace(/µ/g, "\\mu ").replace(/Ω/g, "\\Omega ").replace(/°C/g, "{}^{\\circ}C")}}`;
    at = pattern.lastIndex;
  }
  return { latex, serialised: text, error: null };
}

export function convertEntry(source: string, mode: EntryMode = "maths"): Conversion {
  try {
    if (mode === "chem") return chemistry(source);
    const quantity = QUANTITY.exec(standardFormAsE(canonicalDecimalCommas(normaliseInput(source).trim())));
    if (quantity !== null && (mode === "units" || SCHOOL_UNITS.test(quantity[2] ?? ""))) return units(source);
    if (mode === "units") {
      // In a physics or chemistry entry a unit typed on its own with a power or a "/" ("dm^-3", "m/s^2") is set
      // upright as a unit, not as italic variables; anything else that is not a quantity ("v^2", "3.2x10^-4") is maths.
      const text = normaliseInput(source).trim();
      const unit = /[\^/⁰¹²³⁴⁵⁶⁷⁸⁹⁻]/.test(text) ? unitLatexOf(text) : null;
      if (unit !== null) return { latex: unit, serialised: text, error: null };
    }
    return convertMath(source);
  } catch (error) {
    if (!(error instanceof InputError)) throw error;
    const text = normaliseInput(source).trim();
    return { latex: text, serialised: text, error: { code: error.code, position: error.position, word: error.word } };
  }
}
