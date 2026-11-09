// The diagram drill (JM-D10, SPEC §5.2): a photographed, labelled diagram becomes "tap the part…" questions. Muse
// Spark named each labelled part and SAM 3.1 outlined it; the student confirmed the names. Here every tap is marked by
// code alone: it is right when it falls inside the asked part's outline, never by asking a model. Where parts nest (a
// ventricle inside the heart), a tap belongs to the smallest part that contains it. A part the reader was unsure of
// (`[?]`) or that has no place on the photo is left out; nothing is guessed.

import type { CapturedQuestion } from "./capture.js";

export interface Point { x: number; y: number }
/** A part the drill can ask about: its region, its confirmed name and its outline on the photo (0..1). */
export interface DrillPart { regionId: string; name: string; outline: readonly Point[]; label: { x: number; y: number; w: number; h: number } | null }
export type TapMark = { outcome: "correct"; part: DrillPart } | { outcome: "other_part"; part: DrillPart } | { outcome: "missed" };

/** A tap this close to a part's edge (a share of the photo) still counts as on it: outlines are drawn from SAM's
 * rows, and a finger is wider than a line. */
export const EDGE_SLACK = 0.015;
/** Fewer parts than this make no drill. */
export const MIN_PARTS = 2;

function boxOutline(box: { x: number; y: number; w: number; h: number }): Point[] {
  return [{ x: box.x, y: box.y }, { x: box.x + box.w, y: box.y }, { x: box.x + box.w, y: box.y + box.h }, { x: box.x, y: box.y + box.h }];
}

/** The parts of a confirmed diagram capture the drill can ask about, in reading order. */
export function drillParts(capture: CapturedQuestion): DrillPart[] {
  if (capture.purpose !== "diagram") return [];
  const seen = new Set<string>();
  const parts: DrillPart[] = [];
  for (const region of capture.regions) {
    const name = region.text.trim();
    if (region.kind !== "diagram" || name === "" || name.includes("[?]")) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue; // two parts with one name cannot be told apart by a tap
    const outline = region.outline != null && region.outline.length >= 3 ? region.outline : region.bbox != null ? boxOutline(region.bbox) : null;
    if (outline === null) continue;
    seen.add(key);
    const label = region.label_box ?? null;
    parts.push({ regionId: region.region_id, name, outline, label: label === null ? null : { x: label.x, y: label.y, w: label.w, h: label.h } });
  }
  return parts;
}

/** Whether a point is inside a closed polygon (even-odd rule). */
export function inside(point: Point, polygon: readonly Point[]): boolean {
  let hit = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i], b = polygon[j];
    if (a === undefined || b === undefined) continue;
    if ((a.y > point.y) !== (b.y > point.y) && point.x < ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x) hit = !hit;
  }
  return hit;
}

function edgeDistance(point: Point, polygon: readonly Point[]): number {
  let best = Infinity;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j], b = polygon[i];
    if (a === undefined || b === undefined) continue;
    const dx = b.x - a.x, dy = b.y - a.y, length = dx * dx + dy * dy;
    const t = length === 0 ? 0 : Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length));
    best = Math.min(best, Math.hypot(point.x - (a.x + t * dx), point.y - (a.y + t * dy)));
  }
  return best;
}

/** The polygon's area (shoelace), to prefer the smallest of nested parts. */
export function area(polygon: readonly Point[]): number {
  let sum = 0;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j], b = polygon[i];
    if (a !== undefined && b !== undefined) sum += a.x * b.y - b.x * a.y;
  }
  return Math.abs(sum) / 2;
}

/** The part a tap lands on: the smallest part containing it, else the nearest part within `EDGE_SLACK`; null for
 * none. */
export function partAt(parts: readonly DrillPart[], point: Point): DrillPart | null {
  const containing = parts.filter((part) => inside(point, part.outline));
  if (containing.length > 0) return containing.reduce((a, b) => (area(b.outline) < area(a.outline) ? b : a));
  let nearest: DrillPart | null = null, distance = EDGE_SLACK;
  for (const part of parts) {
    const d = edgeDistance(point, part.outline);
    if (d <= distance) { nearest = part; distance = d; }
  }
  return nearest;
}

/** The mark for a tap when the drill asked for `asked`. */
export function markTap(parts: readonly DrillPart[], asked: DrillPart, point: Point): TapMark {
  const tapped = partAt(parts, point);
  if (tapped === null) return { outcome: "missed" };
  return tapped.regionId === asked.regionId ? { outcome: "correct", part: tapped } : { outcome: "other_part", part: tapped };
}

/** The order the drill asks the parts in: a shuffle fixed by `seed`, so a retry can repeat or change it. */
export function drillOrder(parts: readonly DrillPart[], seed: number): DrillPart[] {
  let state = seed >>> 0 || 1;
  const random = (): number => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const out = [...parts];
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    const a = out[i], b = out[j];
    if (a !== undefined && b !== undefined) { out[i] = b; out[j] = a; }
  }
  return out;
}
