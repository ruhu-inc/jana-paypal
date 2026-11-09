// KaTeX, memoised and restricted (UIA-15): an LRU keyed by (display mode, TeX, macro set version) so settled equations
// never re-render; `trust` allows only \htmlClass with allow-listed classes; a parse error never throws.
import katex from "katex";
import { CLIENT_KEYS } from "@jana/client-core";

export const MACRO_VERSION = 2;
const ALLOWED_CLASS = /^(changed|term-[a-z0-9-]+)$/;

export class Lru<V> {
  private readonly map = new Map<string, V>();
  hits = 0;
  misses = 0;

  constructor(private readonly capacity: number) {}

  get(key: string, make: () => V): V {
    const found = this.map.get(key);
    if (found !== undefined) {
      this.hits += 1;
      this.map.delete(key);
      this.map.set(key, found);
      return found;
    }
    this.misses += 1;
    const value = make();
    this.map.set(key, value);
    if (this.map.size > this.capacity) this.map.delete(this.map.keys().next().value as string);
    return value;
  }

  get size(): number {
    return this.map.size;
  }

  /** Forget every entry: rendered maths holds a profile's text, so a lock or a switch empties it (SEC-19). */
  clear(): void {
    this.map.clear();
  }
}

export const katexCache = new Lru<{ html: string } | { error: string }>(CLIENT_KEYS.ui_katex_cache_entries);

export function trust(context: { command: string; class?: string }): boolean {
  return context.command === "\\htmlClass" && typeof context.class === "string" && context.class.split(/\s+/).every((c) => ALLOWED_CLASS.test(c));
}

/** Render TeX to HTML (MathML included for screen readers), or report that it could not be shown. */
export function renderTex(tex: string, display: boolean): { html: string } | { error: string } {
  return katexCache.get(`${display ? "D" : "I"}|${MACRO_VERSION}|${tex}`, () => {
    try {
      const html = katex.renderToString(tex, {
        displayMode: display,
        throwOnError: true,
        output: "htmlAndMathml",
        trust,
        strict: "ignore",
        maxExpand: CLIENT_KEYS.ui_katex_max_expand,
        maxSize: CLIENT_KEYS.ui_katex_max_size_em,
        macros: { "\\changed": "\\html@mathml{\\htmlClass{changed}{#1}}{\\text{changed: }#1}" },
      });
      return { html };
    } catch (error) {
      return { error: error instanceof Error ? error.message : "katex" };
    }
  });
}
