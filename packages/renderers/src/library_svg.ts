// Library SVG on the client (VSL-23, VSL-18): the sanitised markup from an installed pack is parsed here into a plain
// element tree and checked again against the same allow-list the pack build and pack load apply
// (agent_core/store/svg.py), so the drawing becomes React elements and is never injected as HTML. A tree with anything
// outside the list is refused and the visual falls back to words.
import { must } from "./must.js";
export interface SvgNode {
  tag: string;
  attrs: Record<string, string>;
  children: (SvgNode | string)[];
}

export const ALLOWED_ELEMENTS = new Set([
  "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "defs", "use", "marker", "title", "desc",
  "clipPath", "linearGradient", "stop",
]);

const ALLOWED_ATTRIBUTES = new Set([
  "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform", "viewBox", "dx", "dy", "offset",
  "markerWidth", "markerHeight", "refX", "refY", "orient", "markerUnits", "gradientUnits", "clipPathUnits", "preserveAspectRatio", "version", "xmlns",
  "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "fill-opacity", "stroke-opacity", "opacity", "font-size",
  "font-weight", "font-style", "text-anchor", "dominant-baseline", "marker-start", "marker-end", "marker-mid", "clip-path", "stop-color",
  "stop-opacity", "fill-rule", "vector-effect", "id", "data-part", "data-arrow", "data-label-for", "data-content-colour", "data-region", "href",
  "xlink:href", "xmlns:xlink", "role", "aria-hidden",
]);

const INTERNAL_URL = /^url\(#[A-Za-z][A-Za-z0-9_.-]*\)$/;
const ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

export class SvgRefused extends Error {}

function decode(text: string): string {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e: string) => {
    if (e.startsWith("#x") || e.startsWith("#X")) return String.fromCodePoint(parseInt(e.slice(2), 16));
    if (e.startsWith("#")) return String.fromCodePoint(Number(e.slice(1)));
    return ENTITIES[e] ?? m;
  });
}

/** Parse the subset of XML a sanitised drawing uses: elements, attributes in quotes, text and comments. */
export function parseSvg(markup: string): SvgNode {
  if (/<!(DOCTYPE|ENTITY)/i.test(markup)) throw new SvgRefused("doctype or entity");
  const source = markup.replace(/<\?xml[^>]*\?>/g, "").replace(/<!--[\s\S]*?-->/g, "");
  const stack: SvgNode[] = [{ tag: "#root", attrs: {}, children: [] }];
  const tagRe = /<\/?([A-Za-z][\w:.-]*)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>/g;
  let last = 0;
  for (let m = tagRe.exec(source); m !== null; m = tagRe.exec(source)) {
    const text = source.slice(last, m.index);
    if (text.trim() !== "") must(stack[stack.length - 1]).children.push(decode(text));
    if (/[<>]/.test(text)) throw new SvgRefused("malformed markup");
    last = tagRe.lastIndex;
    const [whole, tag, rawAttrs, selfClosing] = m as unknown as [string, string, string, string];
    if (whole.startsWith("</")) {
      const open = stack.pop();
      if (open === undefined || open.tag !== tag) throw new SvgRefused(`unbalanced </${tag}>`);
      continue;
    }
    const attrs: Record<string, string> = {};
    for (const a of rawAttrs.matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) attrs[must(a[1])] = decode(a[2] ?? a[3] ?? "");
    const node: SvgNode = { tag, attrs, children: [] };
    must(stack[stack.length - 1]).children.push(node);
    if (selfClosing !== "/") stack.push(node);
  }
  if (source.slice(last).trim() !== "") throw new SvgRefused("trailing text");
  if (stack.length !== 1) throw new SvgRefused("unclosed element");
  const root = must(stack[0]).children.find((c): c is SvgNode => typeof c !== "string");
  if (root === undefined || root.tag !== "svg") throw new SvgRefused("no svg root");
  return root;
}

/** Everything the allow-list refuses in a parsed drawing (VSL-23); [] when it may be drawn. */
export function svgProblems(node: SvgNode): string[] {
  const out: string[] = [];
  const walk = (n: SvgNode) => {
    if (!ALLOWED_ELEMENTS.has(n.tag)) out.push(`<${n.tag}> is not allowed`);
    for (const [name, value] of Object.entries(n.attrs)) {
      if (/^on/i.test(name)) out.push(`<${n.tag}> has an event handler`);
      else if (!ALLOWED_ATTRIBUTES.has(name)) out.push(`<${n.tag}> has a ${name} attribute`);
      else if ((name === "href" || name === "xlink:href") && (n.tag !== "use" || !value.startsWith("#"))) out.push(`<${n.tag}> refers outside the document`);
      else if (value.includes("url(") && !INTERNAL_URL.test(value.trim())) out.push(`<${n.tag}> uses an external url()`);
      else if (/javascript:|data:/i.test(value)) out.push(`<${n.tag}> carries a script or data URL`);
    }
    for (const c of n.children) if (typeof c !== "string") walk(c);
  };
  walk(node);
  return out;
}

export function partNodes(node: SvgNode): SvgNode[] {
  const out: SvgNode[] = [];
  const walk = (n: SvgNode) => {
    if (n.attrs["data-part"] !== undefined) out.push(n);
    for (const c of n.children) if (typeof c !== "string") walk(c);
  };
  walk(node);
  return out;
}

const REACT_NAMES: Record<string, string> = {
  "stroke-width": "strokeWidth", "stroke-dasharray": "strokeDasharray", "stroke-linecap": "strokeLinecap", "stroke-linejoin": "strokeLinejoin",
  "fill-opacity": "fillOpacity", "stroke-opacity": "strokeOpacity", "font-size": "fontSize", "font-weight": "fontWeight", "font-style": "fontStyle",
  "text-anchor": "textAnchor", "dominant-baseline": "dominantBaseline", "marker-start": "markerStart", "marker-end": "markerEnd", "marker-mid": "markerMid",
  "clip-path": "clipPath", "stop-color": "stopColor", "stop-opacity": "stopOpacity", "fill-rule": "fillRule", "vector-effect": "vectorEffect",
  "xlink:href": "href", "xmlns:xlink": "xmlnsXlink",
};

/** React props for an SVG node's attributes (names in React's spelling). */
export function reactProps(attrs: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "xmlns" || k === "xmlns:xlink" || k === "version") continue;
    out[REACT_NAMES[k] ?? k] = v;
  }
  return out;
}
