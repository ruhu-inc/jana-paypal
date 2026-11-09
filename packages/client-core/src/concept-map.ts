// The concept map on the client (UXS-22 map view): one subject's curated skill backbone with the learner's states,
// their questions and diagnosed mistakes, the ranked "what next" and the gap path a root-gap card points along
// (`GET /v1/curriculum/{subject_id}/concept-map`). The reply is checked against the command contract's `ConceptMap`
// at the boundary. Everything here belongs to the unlocked profile and is dropped when it locks (SEC-19).
//
// The learner's map (SPEC §21, W10) adds the lens over the same drawing: the layer shown (Mastery, Freshness,
// Mistakes, Study, Notes, Topics your tutor is tracking), the scrubber's date (`?as_of=`, the map replayed on the core
// as it stood then, read-only) and each skill's links (`GET /v1/curriculum/kcs/{kc_id}/links`). The layer and the
// date are kept in memory only, never stored; the overview of every installed subject is `MapOverviewController`.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import type { ProfileController } from "./profiles.js";

type Listener = () => void;

export type ConceptMap = api_commands.ConceptMap;
export type ConceptNode = api_commands.ConceptNode;
export type ConceptEdge = api_commands.ConceptEdge;
export type ConceptNext = api_commands.ConceptNext;
export type ConceptQuestion = api_commands.ConceptQuestion;
export type ConceptMistake = api_commands.ConceptMistake;
export type KcLinks = api_commands.KcLinks;
export type CurriculumOverview = api_commands.CurriculumOverview;
export type OverviewSubject = CurriculumOverview["subjects"][number];
export type OverviewTopic = OverviewSubject["topics"][number];

/** The layers over the drawing (SPEC §21.1), in the switch's order. */
export const MAP_LAYERS = ["mastery", "freshness", "mistakes", "study", "notes", "tracked"] as const;
export type MapLayer = (typeof MAP_LAYERS)[number];
/** Freshness buckets, freshest first (JM-D36: buckets by code, never a probability). */
export const FRESHNESS = ["today", "week", "month", "older", "never"] as const;
export type Freshness = (typeof FRESHNESS)[number];
/** Each bucket drawn as one of five mastery looks: its fill, pattern and icon from tokens.json, the word its own. */
export const FRESHNESS_LOOK: Record<Freshness, "mastered" | "ready_to_check" | "learning" | "moved_on" | "not_started"> = {
  today: "mastered", week: "ready_to_check", month: "learning", older: "moved_on", never: "not_started",
};
/** The scrubber goes back at most 26 weeks (SPEC §21.5). */
export const SCRUB_WEEKS = 26;
/** Misconception statuses shown on the Mistakes layer: every one but resolved (a disputed one is the learner's word). */
export const RINGED: readonly ConceptMistake["status"][] = ["suspected", "active", "resolving", "disputed"];

const DAY_MS = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** The scrubber's dates, oldest first: today and each week back to the week of the first learning event, at most 26
 * weeks (SPEC §21.5). With no learning event yet, today only. */
export function weeklyDates(firstDay: string | null | undefined, today: string): string[] {
  const end = Date.parse(`${today}T00:00:00Z`);
  const first = firstDay ? Date.parse(`${firstDay}T00:00:00Z`) : end;
  const out: string[] = [];
  for (let back = 0; back <= SCRUB_WEEKS; back++) {
    const at = end - back * 7 * DAY_MS;
    if (at < first - 6 * DAY_MS) break;
    out.push(isoDay(at));
  }
  return out.reverse();
}

/** What a skill shows on a layer, as codes the UI turns into words. */
export interface LayerFacts {
  freshness: Freshness;
  refreshDue: boolean;
  rings: ConceptMistake["status"][];
  study: { materials: number; moments: number; cards_due: number };
  notes: number;
  tracked: boolean;
  changed: "up" | "new" | null;
}

/** A node's layer facts (a reply from a core before W10 has none: everything reads as "not yet"). */
export function layerFacts(map: ConceptMap, node: ConceptNode): LayerFacts {
  return {
    freshness: node.freshness ?? "never",
    refreshDue: node.refresh_due ?? node.state === "needs_review",
    rings: map.mistakes.filter((m) => m.kc_id === node.kc_id && RINGED.includes(m.status)).map((m) => m.status),
    study: node.study ?? { materials: 0, moments: 0, cards_due: 0 },
    notes: node.notes ?? 0,
    tracked: node.tracked ?? false,
    changed: node.changed_since ?? null,
  };
}

/** The layer, the scrubber's date and the links read so far: in memory only, dropped when the profile locks. */
export interface MapLens {
  layer: MapLayer;
  /** The date the map shown stands at, or null for today's live map. */
  asOf: string | null;
  /** A dated map is being replayed (the shown map stays until it arrives). */
  scrubbing: boolean;
  /** A dated read failed: the map shown is unchanged. */
  scrubFailed: boolean;
  links: Record<string, KcLinks | "loading" | "failed">;
}

export const EMPTY_LENS: MapLens = { layer: "mastery", asOf: null, scrubbing: false, scrubFailed: false, links: {} };

export function readKcLinks(value: unknown): KcLinks {
  const problems = errors("api/commands#/$defs/KcLinks", value);
  if (problems.length > 0) throw new Error(`kc links reply does not match the contract: ${problems.slice(0, 3).join("; ")}`);
  return value as KcLinks;
}

export function readOverview(value: unknown): CurriculumOverview {
  const problems = errors("api/commands#/$defs/CurriculumOverview", value);
  if (problems.length > 0) throw new Error(`overview reply does not match the contract: ${problems.slice(0, 3).join("; ")}`);
  return value as CurriculumOverview;
}

/** A gap path lit on the map: the skills from the root gap to the KC the mistake was on, and the misconception. */
export interface GapHighlight {
  path: string[];
  misconceptionId: string | null;
}

export interface ConceptMapView {
  status: "idle" | "loading" | "ready" | "failed";
  subjectId: string | null;
  map: ConceptMap | null;
  gap: GapHighlight | null;
}

export const EMPTY_CONCEPT_MAP: ConceptMapView = { status: "idle", subjectId: null, map: null, gap: null };

export function readConceptMap(value: unknown): ConceptMap {
  const problems = errors("api/commands#/$defs/ConceptMap", value);
  if (problems.length > 0) throw new Error(`concept map reply does not match the contract: ${problems.slice(0, 3).join("; ")}`);
  return value as ConceptMap;
}

/** What a node shows from the learner's own layer: how often they asked about it and the mistakes diagnosed on it
 * (any not yet resolved first). */
export interface NodeBadges {
  asked: number;
  mistakes: ConceptMistake[];
}

export function nodeBadges(map: ConceptMap): Map<string, NodeBadges> {
  const out = new Map<string, NodeBadges>();
  const of = (kc: string) => {
    let found = out.get(kc);
    if (found === undefined) out.set(kc, (found = { asked: 0, mistakes: [] }));
    return found;
  };
  for (const q of map.questions) if (q.kc_id !== null) of(q.kc_id).asked++;
  for (const m of map.mistakes) of(m.kc_id).mistakes.push(m);
  return out;
}

/** The questions that match no KC: the map shows them apart, as open questions to take to the tutor. */
export function openQuestions(map: ConceptMap): ConceptQuestion[] {
  return map.questions.filter((q) => q.kc_id === null && q.kept);
}

/** The shortest chain of map edges from `from` to `to` (both included), or [] when there is none; ties go to the
 * earlier edge in the map's order, as on the core. */
export function pathOn(map: ConceptMap, from: string, to: string): string[] {
  const after = new Map<string, string[]>();
  for (const e of map.edges) after.set(e.from, [...(after.get(e.from) ?? []), e.to]);
  const came = new Map<string, string | null>([[from, null]]);
  const queue = [from];
  for (let at = queue.shift(); at !== undefined; at = queue.shift()) {
    if (at === to) {
      const path = [at];
      for (let back = came.get(at); back !== null && back !== undefined; back = came.get(back)) path.unshift(back);
      return path;
    }
    for (const next of after.get(at) ?? []) {
      if (!came.has(next)) {
        came.set(next, at);
        queue.push(next);
      }
    }
  }
  return [];
}

/** One subject's concept map for the unlocked profile. */
export class ConceptMapController {
  private view: ConceptMapView = EMPTY_CONCEPT_MAP;
  private lens: MapLens = EMPTY_LENS;
  /** Today's map, kept while a dated one is shown so "Today" needs no read. */
  private live: ConceptMap | null = null;
  /** A gap asked for before its subject's map was read (a root-gap card's "See it on the map"). */
  private wanted: { subjectId: string; gap: { misconceptionId?: string | null; rootKcId: string; kcId: string } } | null = null;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => {
      this.wanted = null;
      this.live = null;
      this.lens = EMPTY_LENS;
      this.set(EMPTY_CONCEPT_MAP);
    });
  }

  getView = (): ConceptMapView => this.view;

  getLens = (): MapLens => this.lens;

  private setLens(next: MapLens): void {
    this.lens = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Show a layer over the same drawing (in memory only). */
  setLayer(layer: MapLayer): void {
    if (layer !== this.lens.layer) this.setLens({ ...this.lens, layer });
  }

  /** The scrubber: the map as it stood at the end of `asOf` (replayed on the core, read-only), or today's with null.
   * A reply after a lock, a switch or a later scrub is dropped; a failed read keeps the map shown. */
  async scrub(asOf: string | null): Promise<void> {
    const { subjectId } = this.view;
    if (subjectId === null) return;
    if (asOf === null) {
      this.setLens({ ...this.lens, asOf: null, scrubbing: false, scrubFailed: false });
      if (this.live !== null) this.set({ ...this.view, status: "ready", map: this.live, gap: null });
      else await this.load(subjectId);
      return;
    }
    const generation = this.profiles.generation;
    this.setLens({ ...this.lens, asOf, scrubbing: true, scrubFailed: false });
    try {
      const path = `/v1/curriculum/${encodeURIComponent(subjectId)}/concept-map?as_of=${encodeURIComponent(asOf)}`;
      const map = readConceptMap(await this.profiles.commands.get<unknown>(path));
      if (generation !== this.profiles.generation || this.view.subjectId !== subjectId || this.lens.asOf !== asOf) return;
      this.set({ ...this.view, status: "ready", map, gap: null });
      this.setLens({ ...this.lens, scrubbing: false });
    } catch {
      if (generation === this.profiles.generation && this.lens.asOf === asOf) this.setLens({ ...this.lens, scrubbing: false, scrubFailed: true });
    }
  }

  /** A skill's Study materials, lecture moments, notes and tracked flag, read once per map (device-only, JM-D39). */
  async links(kcId: string): Promise<void> {
    const held = this.lens.links[kcId];
    if (held !== undefined && held !== "failed") return;
    const generation = this.profiles.generation;
    this.setLens({ ...this.lens, links: { ...this.lens.links, [kcId]: "loading" } });
    try {
      const found = readKcLinks(await this.profiles.commands.get<unknown>(`/v1/curriculum/kcs/${encodeURIComponent(kcId)}/links`));
      if (generation === this.profiles.generation) this.setLens({ ...this.lens, links: { ...this.lens.links, [kcId]: found } });
    } catch {
      if (generation === this.profiles.generation) this.setLens({ ...this.lens, links: { ...this.lens.links, [kcId]: "failed" } });
    }
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: ConceptMapView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Read a subject's concept map; a reply after a lock or a switch is dropped. A re-read keeps the map shown. */
  async load(subjectId: string): Promise<void> {
    const generation = this.profiles.generation;
    const same = this.view.subjectId === subjectId && this.view.map !== null;
    if (!same) {
      this.live = null;
      if (this.lens.asOf !== null || Object.keys(this.lens.links).length > 0) this.lens = { ...this.lens, asOf: null, scrubbing: false, scrubFailed: false, links: {} };
    }
    this.set(same ? { ...this.view } : { status: "loading", subjectId, map: null, gap: null });
    try {
      const map = readConceptMap(await this.profiles.commands.get<unknown>(`/v1/curriculum/${encodeURIComponent(subjectId)}/concept-map`));
      if (generation !== this.profiles.generation || this.view.subjectId !== subjectId) return;
      this.live = map;
      // A dated map stays on screen while the learner scrubs; today's is kept for "Today".
      if (this.lens.asOf !== null) return;
      this.set({ status: "ready", subjectId, map, gap: this.view.gap });
      if (this.wanted?.subjectId === subjectId) {
        const { gap } = this.wanted;
        this.wanted = null;
        this.showGap(gap);
      }
    } catch {
      if (generation === this.profiles.generation && this.view.subjectId === subjectId) this.set({ ...this.view, status: same ? "ready" : "failed" });
    }
  }

  /** Light the gap path of a mistake: from its root-gap skill to the KC it was on (a root-gap card fired, or the
   * learner opened the mistake). A misconception the map does not know lights the direct link when both ends are on
   * the map, else nothing. */
  showGap(gap: { misconceptionId?: string | null; rootKcId: string; kcId: string }): void {
    const map = this.view.map;
    const known = map?.mistakes.find((m) => m.misconception_id === gap.misconceptionId && m.gap_path.length > 0);
    const path = known?.gap_path ?? (map === null ? [] : pathOn(map, gap.rootKcId, gap.kcId));
    this.set({ ...this.view, gap: path.length > 0 ? { path, misconceptionId: gap.misconceptionId ?? null } : null });
  }

  /** Light a gap on a subject's map once it is read (now, when it is the map shown). */
  showGapOn(subjectId: string, gap: { misconceptionId?: string | null; rootKcId: string; kcId: string }): void {
    if (this.view.subjectId === subjectId && this.view.map !== null) this.showGap(gap);
    else this.wanted = { subjectId, gap };
  }

  clearGap(): void {
    if (this.view.gap !== null) this.set({ ...this.view, gap: null });
  }

  /** A KC's mastery moved: re-read when it is on the map shown (and forget its links, which may have moved). */
  onProgress(kcId: string): void {
    const { map, subjectId } = this.view;
    if (this.lens.links[kcId] !== undefined) {
      const { [kcId]: _dropped, ...links } = this.lens.links;
      this.lens = { ...this.lens, links };
    }
    if (map !== null && subjectId !== null && map.nodes.some((n) => n.kc_id === kcId)) void this.load(subjectId);
  }
}

export interface OverviewView {
  status: "idle" | "loading" | "ready" | "failed";
  overview: CurriculumOverview | null;
}

export const EMPTY_OVERVIEW: OverviewView = { status: "idle", overview: null };

/** Progress's hero (SPEC §21.1, JM-D35): every installed subject as topic tiles, read when Progress opens and again
 * on a mastery change. A re-read keeps the tiles shown; a reply after a lock is dropped. */
export class MapOverviewController {
  private view: OverviewView = EMPTY_OVERVIEW;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_OVERVIEW));
  }

  getView = (): OverviewView => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: OverviewView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  async load(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.overview === null) this.set({ status: "loading", overview: null });
    try {
      const overview = readOverview(await this.profiles.commands.get<unknown>("/v1/curriculum/overview"));
      if (generation === this.profiles.generation) this.set({ status: "ready", overview });
    } catch {
      if (generation === this.profiles.generation) this.set({ ...this.view, status: this.view.overview === null ? "failed" : "ready" });
    }
  }
}
