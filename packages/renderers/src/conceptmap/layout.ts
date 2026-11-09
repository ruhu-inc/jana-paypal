// The concept map's layered layout (UXS-22 map view): a pure, deterministic function of the nodes and edges, so the
// same map always draws the same way and a test can check it. No graph library.
//
// 1. Layers by longest path: a skill sits one column right of the furthest skill it builds on (foundations left);
//    a `late` source (a linked skill of another subject) sits just left of the first skill built on it.
//    A cycle (never in a pack, but defended against) is broken at the edge that closes it, in id order.
// 2. An edge spanning several columns gets a dummy point in each column it crosses, so it is routed between boxes.
// 3. Crossing reduction: barycentre sweeps left-to-right and right-to-left from the syllabus order (the node `order`
//    key, then id), keeping the order with the fewest crossings seen (the earlier one on a tie).
// 4. Coordinates: each column stacks its boxes in that order with a fixed gap, then boxes are pulled towards their
//    neighbours' centres and pushed apart again, so no two boxes ever overlap.
import { must } from "../must.js";

export interface LayoutNode {
  id: string;
  /** The initial order inside a column (the syllabus code); ties fall back to the id. */
  order: string;
  /** Placed as late as it can be: just left of the first skill built on it (a linked skill of another subject, so
   * its links stay short). Only for a node nothing comes before. */
  late?: boolean;
  width?: number;
  height?: number;
}

export interface LayoutEdge {
  from: string;
  to: string;
}

export interface PlacedNode {
  id: string;
  layer: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RoutedEdge {
  from: string;
  to: string;
  /** From the source box's right edge through each dummy point to the target box's left edge. */
  points: { x: number; y: number }[];
}

export interface Layout {
  nodes: PlacedNode[];
  edges: RoutedEdge[];
  width: number;
  height: number;
  layers: number;
  crossings: number;
}

export const NODE_WIDTH = 188;
export const NODE_HEIGHT = 72;
const LAYER_GAP = 72;
const NODE_GAP = 14;
const DUMMY_GAP = 6;
const MARGIN = 16;
const SWEEPS = 12;
const PULLS = 6;

interface Slot {
  id: string;
  dummy: boolean;
  height: number;
  width: number;
}

function compare(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/** Longest-path layers, and the edges kept: an edge closing a cycle (found in id order) is left out. */
function layering(ids: string[], edges: LayoutEdge[], late: Set<string>): { layer: Map<string, number>; kept: LayoutEdge[] } {
  const out = new Map<string, LayoutEdge[]>();
  for (const e of edges) out.set(e.from, [...(out.get(e.from) ?? []), e]);
  const state = new Map<string, 1 | 2>();
  const kept: LayoutEdge[] = [];
  const visit = (start: string) => {
    // Iterative DFS so a long chain cannot overflow the stack.
    const stack: { id: string; next: number }[] = [{ id: start, next: 0 }];
    state.set(start, 1);
    while (stack.length > 0) {
      const top = must(stack[stack.length - 1]);
      const list = out.get(top.id) ?? [];
      if (top.next >= list.length) {
        state.set(top.id, 2);
        stack.pop();
        continue;
      }
      const e = must(list[top.next++]);
      const seen = state.get(e.to);
      if (seen === 1) continue; // a back edge: it would close a cycle
      kept.push(e);
      if (seen === undefined) {
        state.set(e.to, 1);
        stack.push({ id: e.to, next: 0 });
      }
    }
  };
  for (const id of ids) if (!state.has(id)) visit(id);
  // Longest path over the kept edges, in topological order (Kahn, ready ids in id order).
  const indegree = new Map(ids.map((id) => [id, 0]));
  for (const e of kept) indegree.set(e.to, must(indegree.get(e.to)) + 1);
  const layer = new Map(ids.map((id) => [id, 0]));
  const ready = ids.filter((id) => indegree.get(id) === 0);
  const after = new Map<string, string[]>();
  for (const e of kept) after.set(e.from, [...(after.get(e.from) ?? []), e.to]);
  while (ready.length > 0) {
    const id = must(ready.shift());
    for (const to of after.get(id) ?? []) {
      layer.set(to, Math.max(must(layer.get(to)), must(layer.get(id)) + 1));
      indegree.set(to, must(indegree.get(to)) - 1);
      if (indegree.get(to) === 0) ready.push(to);
    }
  }
  for (const id of ids) {
    if (!late.has(id) || kept.some((e) => e.to === id)) continue;
    const next = (after.get(id) ?? []).map((to) => must(layer.get(to)));
    if (next.length > 0) layer.set(id, Math.max(0, Math.min(...next) - 1));
  }
  return { layer, kept };
}

function crossingsBetween(upper: string[], lower: string[], links: Map<string, string[]>): number {
  const pos = new Map(lower.map((id, i) => [id, i]));
  const pairs: [number, number][] = [];
  upper.forEach((id, i) => {
    for (const to of links.get(id) ?? []) {
      const j = pos.get(to);
      if (j !== undefined) pairs.push([i, j]);
    }
  });
  let count = 0;
  for (let a = 0; a < pairs.length; a++) {
    for (let b = a + 1; b < pairs.length; b++) {
      const [i1, j1] = must(pairs[a]), [i2, j2] = must(pairs[b]);
      if ((i1 - i2) * (j1 - j2) < 0) count++;
    }
  }
  return count;
}

/** The layout; `sweeps` (12) sets the crossing-reduction passes, 0 keeping the syllabus order. */
export function layout(nodes: readonly LayoutNode[], edges: readonly LayoutEdge[], { sweeps = SWEEPS }: { sweeps?: number } = {}): Layout {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const ids = [...byId.keys()].sort(compare);
  const sortedEdges = edges
    .filter((e) => byId.has(e.from) && byId.has(e.to) && e.from !== e.to)
    .map((e) => ({ from: e.from, to: e.to }))
    .sort((a, b) => compare(a.from, b.from) || compare(a.to, b.to))
    .filter((e, i, all) => i === 0 || e.from !== must(all[i - 1]).from || e.to !== must(all[i - 1]).to);
  const { layer, kept } = layering(ids, sortedEdges, new Set(nodes.filter((n) => n.late === true).map((n) => n.id)));
  const layers = ids.length === 0 ? 0 : Math.max(...ids.map((id) => must(layer.get(id)))) + 1;

  // Slots per column, with a dummy for each column a long edge crosses.
  const slots = new Map<string, Slot>();
  for (const id of ids) {
    const n = must(byId.get(id));
    slots.set(id, { id, dummy: false, height: n.height ?? NODE_HEIGHT, width: n.width ?? NODE_WIDTH });
  }
  const columns: string[][] = Array.from({ length: layers }, () => []);
  for (const id of ids) must(columns[must(layer.get(id))]).push(id);
  const down = new Map<string, string[]>(), up = new Map<string, string[]>();
  const link = (a: string, b: string) => {
    down.set(a, [...(down.get(a) ?? []), b]);
    up.set(b, [...(up.get(b) ?? []), a]);
  };
  const chains = new Map<string, string[]>();
  for (const e of kept) {
    const from = must(layer.get(e.from)), to = must(layer.get(e.to));
    const chain = [e.from];
    for (let l = from + 1; l < to; l++) {
      const d = `~${e.from}>${e.to}@${l}`;
      slots.set(d, { id: d, dummy: true, height: 0, width: 0 });
      must(columns[l]).push(d);
      chain.push(d);
    }
    chain.push(e.to);
    for (let i = 1; i < chain.length; i++) link(must(chain[i - 1]), must(chain[i]));
    chains.set(`${e.from}>${e.to}`, chain);
  }
  // The starting order: syllabus order for boxes; a dummy goes by its source's order.
  const key = (id: string) => {
    const real = must(slots.get(id)).dummy ? id.slice(1, id.indexOf(">")) : id;
    return must(byId.get(real)).order;
  };
  for (const col of columns) col.sort((a, b) => compare(key(a), key(b)) || compare(a, b));

  const total = (cols: string[][]) => cols.slice(1).reduce((sum, col, i) => sum + crossingsBetween(must(cols[i]), col, down), 0);
  let best = columns.map((c) => [...c]);
  let bestCount = total(best);
  const current = columns.map((c) => [...c]);
  const sweep = (from: number, step: 1 | -1, neighbours: Map<string, string[]>) => {
    for (let l = from; l >= 0 && l < layers; l += step) {
      const ref = current[l - step];
      if (ref === undefined) continue;
      const pos = new Map(ref.map((id, i) => [id, i]));
      const col = must(current[l]);
      const bary = new Map(col.map((id, i) => {
        const ns = (neighbours.get(id) ?? []).map((n) => pos.get(n)).filter((p): p is number => p !== undefined);
        return [id, ns.length === 0 ? i : ns.reduce((a, b) => a + b, 0) / ns.length];
      }));
      const index = new Map(col.map((id, i) => [id, i]));
      col.sort((a, b) => must(bary.get(a)) - must(bary.get(b)) || must(index.get(a)) - must(index.get(b)));
    }
  };
  // Transpose: swap neighbours in a column while that lowers their crossings with both adjacent columns.
  const pairCrossings = (u: string, v: string, l: number) => {
    let count = 0;
    for (const [ref, links] of [[current[l - 1], up], [current[l + 1], down]] as const) {
      if (ref === undefined) continue;
      const pos = new Map(ref.map((id, i) => [id, i]));
      const pu = (links.get(u) ?? []).map((n) => must(pos.get(n))), pv = (links.get(v) ?? []).map((n) => must(pos.get(n)));
      for (const a of pu) for (const b of pv) if (a > b) count++;
    }
    return count;
  };
  const transpose = () => {
    for (let l = 0; l < layers; l++) {
      const col = must(current[l]);
      for (let improved = true, rounds = 0; improved && rounds < 8; rounds++) {
        improved = false;
        for (let i = 0; i + 1 < col.length; i++) {
          const u = must(col[i]), v = must(col[i + 1]);
          if (pairCrossings(v, u, l) < pairCrossings(u, v, l)) {
            col[i] = v;
            col[i + 1] = u;
            improved = true;
          }
        }
      }
    }
  };
  for (let s = 0; s < sweeps; s++) {
    if (s % 2 === 0) sweep(1, 1, up);
    else sweep(layers - 2, -1, down);
    transpose();
    const count = total(current);
    if (count < bestCount) {
      bestCount = count;
      best = current.map((c) => [...c]);
    }
  }

  // Coordinates: stack each column, then pull towards neighbours and push apart (order kept, gaps kept).
  const y = new Map<string, number>();
  const gapAfter = (id: string) => (must(slots.get(id)).dummy ? DUMMY_GAP : NODE_GAP);
  for (const col of best) {
    let at = 0;
    for (const id of col) {
      y.set(id, at);
      at += must(slots.get(id)).height + gapAfter(id);
    }
  }
  const centre = (id: string) => must(y.get(id)) + must(slots.get(id)).height / 2;
  // Each slot as near its wanted place as the gaps allow: the mean of pushing every slot down from the first and
  // pushing every slot up from the last. Both keep every gap, so their mean does too (the gaps are linear).
  const settle = (col: string[], wanted: Map<string, number>) => {
    const space = (id: string) => must(slots.get(id)).height + gapAfter(id);
    const pushed = new Map<string, number>(), lifted = new Map<string, number>();
    let floor = -Infinity;
    for (const id of col) {
      pushed.set(id, Math.max(must(wanted.get(id)), floor));
      floor = must(pushed.get(id)) + space(id);
    }
    let ceiling = Infinity;
    for (let i = col.length - 1; i >= 0; i--) {
      const id = must(col[i]);
      lifted.set(id, Math.min(must(wanted.get(id)), ceiling - space(id)));
      ceiling = must(lifted.get(id));
    }
    for (const id of col) y.set(id, Math.floor((must(pushed.get(id)) + must(lifted.get(id))) / 2)); // floor keeps whole gaps
  };
  for (let p = 0; p < PULLS; p++) {
    const order = p % 2 === 0 ? best.map((_, i) => i) : best.map((_, i) => layers - 1 - i);
    for (const l of order) {
      const col = must(best[l]);
      const wanted = new Map(col.map((id) => {
        const ns = [...(up.get(id) ?? []), ...(down.get(id) ?? [])];
        const target = ns.length === 0 ? centre(id) : ns.reduce((a, n) => a + centre(n), 0) / ns.length;
        return [id, Math.round(target - must(slots.get(id)).height / 2)];
      }));
      settle(col, wanted);
    }
  }
  const top = Math.min(0, ...[...y.values()]);
  const placedY = (id: string) => must(y.get(id)) - top + MARGIN;
  const xOf = (l: number) => MARGIN + l * (NODE_WIDTH + LAYER_GAP);
  const placed: PlacedNode[] = ids.map((id) => {
    const s = must(slots.get(id));
    const l = must(layer.get(id));
    return { id, layer: l, x: xOf(l) + (NODE_WIDTH - s.width) / 2, y: placedY(id), width: s.width, height: s.height };
  });
  const placedById = new Map(placed.map((n) => [n.id, n]));
  const routed: RoutedEdge[] = sortedEdges.map((e) => {
    const chain = chains.get(`${e.from}>${e.to}`);
    const source = must(placedById.get(e.from)), target = must(placedById.get(e.to));
    if (chain === undefined) {
      // A cycle-closing edge: drawn straight between the boxes' facing sides.
      return { from: e.from, to: e.to, points: [{ x: source.x, y: source.y + source.height / 2 }, { x: target.x + target.width, y: target.y + target.height / 2 }] };
    }
    const middle = chain.slice(1, -1).map((d) => ({ x: xOf(must(layer.get(e.from)) + chain.indexOf(d)) + NODE_WIDTH / 2, y: placedY(d) }));
    return {
      from: e.from,
      to: e.to,
      points: [{ x: source.x + source.width, y: source.y + source.height / 2 }, ...middle, { x: target.x, y: target.y + target.height / 2 }],
    };
  });
  const width = layers === 0 ? 0 : xOf(layers - 1) + NODE_WIDTH + MARGIN;
  const height = layers === 0 ? 0 : Math.max(0, ...placed.map((n) => n.y + n.height), ...[...slots.keys()].filter((id) => must(slots.get(id)).dummy).map((d) => placedY(d))) + MARGIN;
  return { nodes: placed, edges: routed, width, height, layers, crossings: bestCount };
}

/** Whether two placed boxes overlap (touching edges do not). */
export function overlaps(a: PlacedNode, b: PlacedNode): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}
