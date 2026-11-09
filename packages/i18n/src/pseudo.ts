// The pseudo-locale `en-XA` (LOC-04): every literal accented, padded by 35% and bracketed, with ICU syntax kept, so the
// gallery shows clipping and untranslated strings at a glance.
import { parse, TYPE, type MessageFormatElement } from "@formatjs/icu-messageformat-parser";
import { printAST } from "@formatjs/icu-messageformat-parser/printer.js";

const ACCENTS: Record<string, string> = {
  a: "á", b: "ƀ", c: "ç", d: "ð", e: "é", f: "ƒ", g: "ĝ", h: "ĥ", i: "í", j: "ĵ", k: "ķ", l: "ļ", m: "ɱ", n: "ñ", o: "ó",
  p: "þ", q: "ǫ", r: "ŕ", s: "š", t: "ţ", u: "ú", v: "ṽ", w: "ŵ", x: "ẋ", y: "ý", z: "ž",
  A: "Á", B: "Ɓ", C: "Ç", D: "Ð", E: "É", F: "Ƒ", G: "Ĝ", H: "Ĥ", I: "Í", J: "Ĵ", K: "Ķ", L: "Ļ", M: "Ṁ", N: "Ñ", O: "Ó",
  P: "Þ", Q: "Ǫ", R: "Ŕ", S: "Š", T: "Ţ", U: "Ú", V: "Ṽ", W: "Ŵ", X: "Ẋ", Y: "Ý", Z: "Ž",
};

function accent(text: string): string {
  return [...text].map((c) => ACCENTS[c] ?? c).join("");
}

function transform(elements: MessageFormatElement[]): MessageFormatElement[] {
  return elements.map((el) => {
    if (el.type === TYPE.literal) return { ...el, value: accent(el.value) };
    if (el.type === TYPE.plural || el.type === TYPE.select) {
      const options = Object.fromEntries(Object.entries(el.options).map(([k, v]) => [k, { ...v, value: transform(v.value) }]));
      return { ...el, options } as MessageFormatElement;
    }
    if (el.type === TYPE.tag) return { ...el, children: transform(el.children) };
    return el;
  });
}

export function pseudoMessage(message: string): string {
  if (message === "") return "";
  const body = printAST(transform(parse(message, { ignoreTag: true })));
  return `[${body}${"~".repeat(Math.ceil(message.length * 0.35))}]`;
}

export function pseudoCatalogue(base: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(base).map(([k, v]) => [k, pseudoMessage(v)]));
}
