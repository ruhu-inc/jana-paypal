import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";
import type { Store } from "@jana/client-core";

const ESTIMATE = 240;
const THRESHOLD = 40;
const hashes = new WeakMap<object, string>();
function hash(value: object): string {
  const known = hashes.get(value);
  if (known !== undefined) return known;
  let result = 2166136261;
  const text = JSON.stringify(value);
  for (let i = 0; i < text.length; i++) result = Math.imul(result ^ text.charCodeAt(i), 16777619);
  const key = (result >>> 0).toString(36); hashes.set(value, key); return key;
}
export function signature(store: Store, key: string): string {
  if (key.startsWith("m:")) { const message = store.messages[key.slice(2)]; return message === undefined ? key : hash(message); }
  const turn = store.turns[key.slice(2)];
  return turn === undefined ? key : [hash(turn), ...turn.part_ids.map((id) => { const part = store.parts[id]; return `${id}:${part === undefined ? "" : hash(part)}`; })].join(":");
}
export function protectedRows(store: Store, list: string[]): Set<string> {
  const result = new Set<string>();
  for (const key of list) {
    const turn = key.startsWith("t:") ? store.turns[key.slice(2)] : undefined;
    if (turn !== undefined && (turn.status === "streaming" || turn.part_ids.some((id) => ["open", "answering"].includes(store.parts[id]?.card?.state ?? "")))) result.add(key);
  }
  return result;
}
export function findRows(store: Store, list: string[], query: string): string[] {
  const needle = query.trim().toLocaleLowerCase();
  if (needle === "") return [];
  return list.filter((key) => {
    const text = key.startsWith("m:") ? JSON.stringify(store.messages[key.slice(2)]?.content ?? [])
      : (store.turns[key.slice(2)]?.part_ids ?? []).map((id) => store.parts[id]?.text ?? "").join("\n");
    return text.toLocaleLowerCase().includes(needle);
  });
}
interface Measure { content: string; width: number; scale: string; height: number }
export type WindowEntry = { key: string; spacer?: never } | { key: string; spacer: number };

/** Variable-height rows keep the focused/open/reading rows mounted. Measurements are local to this session. */
export function useTranscriptWindow({ root, list, store, target, onPositioned, focusTarget }: {
  root: RefObject<HTMLDivElement | null>; list: string[]; store: Store; target: string | null; onPositioned: () => void; focusTarget: boolean;
}) {
  const cache = useRef(new Map<string, Measure>());
  const [viewport, setViewport] = useState({ top: Number.POSITIVE_INFINITY, height: 800, width: 0, scale: "1" });
  const [, setRevision] = useState(0);
  const protectedLocal = useRef(new Set<string>());
  const anchor = useRef<string | null>(null);
  const signatures = new Map(list.map((key) => [key, signature(store, key)]));
  const latest = useRef({ signatures, viewport }); latest.current = { signatures, viewport };
  const update = useCallback(() => {
    const el = root.current; if (el === null) return;
    const top = el.getBoundingClientRect().top;
    const rows = [...el.querySelectorAll<HTMLElement>("[data-row-key]")];
    anchor.current = rows.find((node) => node.getBoundingClientRect().bottom > top)?.dataset.rowKey ?? null;
    const pinned = new Set<string>();
    for (const node of rows) if (node.contains(document.activeElement) || node.querySelector('details[open], [aria-expanded="true"]') !== null) pinned.add(node.dataset.rowKey ?? "");
    protectedLocal.current = pinned;
    const next = { top: el.scrollTop, height: el.clientHeight || 800, width: el.clientWidth, scale: getComputedStyle(el).getPropertyValue("--text-scale") };
    setViewport((old) => Object.keys(next).every((k) => old[k as keyof typeof old] === next[k as keyof typeof next]) ? old : next);
  }, [root]);
  const measure = useCallback(() => {
    const el = root.current; if (el === null) return;
    let changed = false;
    const { viewport: view, signatures: current } = latest.current;
    for (const node of el.querySelectorAll<HTMLElement>("[data-row-key]")) {
      const key = node.dataset.rowKey ?? "", content = current.get(key) ?? "";
      const height = node.getBoundingClientRect().height;
      if (height <= 0) continue;
      const old = cache.current.get(key);
      if (old?.height !== height || old.content !== content || old.width !== view.width || old.scale !== view.scale) {
        cache.current.set(key, { content, width: view.width, scale: view.scale, height }); changed = true;
      }
    }
    if (changed) { setRevision((n) => n + 1); }
  }, [root]);
  const observer = useRef<ResizeObserver | null>(null);
  const observed = useRef(new Set<HTMLElement>());
  useLayoutEffect(() => {
    const el = root.current; if (el === null) return;
    const resize = new ResizeObserver(() => { measure(); update(); }); observer.current = resize;
    resize.observe(el);
    const change = () => { update(); setRevision((n) => n + 1); };
    el.addEventListener("toggle", change, true); el.addEventListener("focusin", change); el.addEventListener("focusout", change);
    return () => { resize.disconnect(); observer.current = null; observed.current.clear(); el.removeEventListener("toggle", change, true); el.removeEventListener("focusin", change); el.removeEventListener("focusout", change); };
  }, [root, measure, update]);
  useLayoutEffect(() => {
    const current = new Set(root.current?.querySelectorAll<HTMLElement>("[data-row-key]") ?? []);
    for (const node of observed.current) if (!current.has(node)) observer.current?.unobserve(node);
    for (const node of current) if (!observed.current.has(node)) observer.current?.observe(node);
    observed.current = current;
    measure();
  });
  const longSession = list.filter((key) => key.startsWith("t:")).length > THRESHOLD;
  const pinned = protectedRows(store, list);
  for (const key of protectedLocal.current) pinned.add(key);
  if (anchor.current !== null) pinned.add(anchor.current);
  if (target !== null) pinned.add(target);
  // Adjacent message/turn pairs remain together at keyboard and reading boundaries.
  for (const key of [...pinned]) { const at = list.indexOf(key); for (const n of [at - 1, at + 1]) if (list[n] !== undefined) pinned.add(list[n] ?? ""); }
  const heights = list.map((key) => {
    const entry = cache.current.get(key);
    return entry !== undefined && entry.content === signatures.get(key) && entry.width === viewport.width && entry.scale === viewport.scale ? entry.height : ESTIMATE;
  });
  const total = heights.reduce((sum, height) => sum + height, 0);
  const from = Number.isFinite(viewport.top) ? viewport.top - viewport.height : total - viewport.height * 2;
  const to = Number.isFinite(viewport.top) ? viewport.top + viewport.height * 2 : total;
  const entries: WindowEntry[] = []; let offset = 0; let firstVisible = list.length - 1; let lastVisible = 0;
  for (const [index, key] of list.entries()) {
    const height = heights[index] ?? ESTIMATE;
    if (offset + height >= from && offset <= to) { firstVisible = Math.min(firstVisible, index); lastVisible = index; }
    const visible = !longSession || pinned.has(key) || (offset + height >= from && offset <= to);
    if (visible) entries.push({ key });
    else { const last = entries.at(-1); if (last?.spacer !== undefined) last.spacer += height; else entries.push({ key: `space:${key}`, spacer: height }); }
    offset += height;
  }
  const targetDone = useRef<string | null>(null);
  useLayoutEffect(() => {
    if (target === null) { targetDone.current = null; return; }
    if (targetDone.current === target) return;
    const node = [...root.current?.querySelectorAll<HTMLElement>("[data-row-key]") ?? []].find((node) => node.dataset.rowKey === target);
    if (node === undefined) return;
    const position = () => {
      node.scrollIntoView?.({ block: "center" });
      if (focusTarget) { node.tabIndex = -1; node.focus({ preventScroll: true }); }
      // Capture the requested row immediately: waiting for the browser scroll event lets a spacer measurement
      // restore the previous anchor before that event arrives.
      onPositioned(); update();
    };
    position(); targetDone.current = target;
    const frame = requestAnimationFrame(position);
    return () => cancelAnimationFrame(frame);
  }, [root, target, update, onPositioned, focusTarget]);
  return { entries, onScroll: update, previous: firstVisible > 0 ? list[Math.max(0, firstVisible - 4)] ?? null : null, next: lastVisible < list.length - 1 ? list[Math.min(list.length - 1, lastVisible + 4)] ?? null : null };
}
