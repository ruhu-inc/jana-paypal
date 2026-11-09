// The subject map and the lesson player on the client (UXS-22, UXS-23; LRN-09, LRN-34, CUR-11, LRN-17 subset).
//
// The core serves one subject's map (`GET /v1/curriculum/{subject_id}/map`): syllabus sections, each with its topics
// (tiles), every KC with its TABLE LRN-09a state and LRN-34 band, and the lessons that teach them. The client picks a
// tile's label by TABLE UXS-22a (`topicLabel`) and counts its KCs by band for the segmented bar. A lesson
// (`GET /v1/lessons/{lesson_id}`) arrives with every block resolved, so it plays with no model and no network; its
// quick checks are marked by the core's deterministic marker and its position is saved as the student moves. A
// step-shape lesson (TABLE CUR-11b, D110) is steps that each ask one item, then a three-question check; the check reply
// carries a step's why and the item's working only with a correct answer or once the step is shown after two misses.
// The map's board syllabus points (CUR-06, D113) let the student show one syllabus's topics with their codes; the
// choice is kept per subject while the app is open (`chooseSyllabus`).
//
// These are the M13.g contract additions (proposed, D72; `contract-additions-m13g.json`): until the command contract
// carries them, the types below mirror the proposal and each reply is checked at the boundary; the check items inside
// a lesson are the contract's own `PracticeItem` and are checked against it. Everything here belongs to the unlocked
// profile and is dropped when it locks (SEC-19).
import { errors, type api_commands, type content_content_pack } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

type Listener = () => void;

export type PracticeItem = api_commands.PracticeItem;
export type PartAnswer = api_commands.PartAnswer;
export type VisualRef = content_content_pack.VisualRef;

/** TABLE LRN-09a states, in the order the topic sheet lists them. */
export const KC_STATES = ["not_started", "learning", "ready_to_check", "mastered", "needs_review", "moved_on"] as const;
export type KcState = (typeof KC_STATES)[number];
/** LRN-34's three bands, in the segmented bar's order. */
export const BANDS = ["secure", "learning", "not_started"] as const;
export type MapBand = (typeof BANDS)[number];

/** One board syllabus point (CUR-06): mapping facts only, never board text. */
export interface SyllabusPoint {
  scheme: string;
  code: string;
  coverage: "full" | "partial";
}

/** A board syllabus the subject's map is mapped to: its scheme id, the board's name and the syllabus's own code. */
export interface MapSyllabus {
  scheme: string;
  board: string;
  syllabus_code: string;
}

export interface MapKc {
  kc_id: string;
  code: string;
  statement: string;
  state: KcState;
  band: MapBand;
  /** Its own mappings, otherwise its node's (D113). */
  syllabus_points?: SyllabusPoint[];
}

export interface MapLesson {
  lesson_id: string;
  title: string;
  estimated_minutes: number;
  /** The tile's KCs this lesson teaches. */
  kc_ids: string[];
  /** Those of the KCs it teaches (D113). */
  syllabus_points?: SyllabusPoint[];
}

export interface MapTopic {
  node_id: string;
  code: string;
  title: string;
  syllabus_points?: SyllabusPoint[];
  kcs: MapKc[];
  lessons: MapLesson[];
}

export interface MapSection {
  node_id: string;
  code: string;
  title: string;
  syllabus_points?: SyllabusPoint[];
  topics: MapTopic[];
}

export type MapStatus = "ready" | "not_installed" | "unverified";

export interface SubjectMap {
  subject_id: string;
  subject: "mathematics" | "physics" | "chemistry" | "biology" | "accounting";
  level: "O" | "A";
  title: string | null;
  status: MapStatus;
  pack: { pack_id: string; version: string } | null;
  /** The board syllabuses its map is mapped to; absent or empty when none (CUR-06). */
  syllabuses?: MapSyllabus[];
  sections: MapSection[];
}

export interface WorkedExampleView {
  worked_example_id: string;
  problem: string;
  steps: { n: number; latex: string; reason: string; check_id: string | null }[];
  final_answer: { latex: string; check_id: string };
  /** Step numbers shown as blanks for this student (LRN-17). */
  faded_steps: number[];
  /** The worked example's figure, resolved and checked when the pack was built; absent or null when it has none. */
  visual?: api_commands.WorkedExampleView["visual"];
}

export type CheckBlockType = "prior_knowledge" | "guided_step" | "quick_check" | "exit_check";
/** What a step asks the student to do (TABLE CUR-11b). */
export type StepAction = "try_first" | "choose" | "predict" | "calculate";

export type LessonBlock =
  | { type: CheckBlockType; items: PracticeItem[] }
  | { type: "idea"; text: string; visual: VisualRef | null }
  | { type: "worked_example"; worked_example: WorkedExampleView }
  | { type: "common_mistakes"; text: string | null; mistakes: { misconception_id: string; name: string; fix: string }[] }
  | { type: "visual"; visual: VisualRef; caption: string | null }
  | { type: "summary"; text: string }
  | { type: "step"; action: StepAction; text: string | null; visual: VisualRef | null; items: PracticeItem[] }
  | { type: "check"; items: PracticeItem[] };

/** Whether a lesson is in the step shape of TABLE CUR-11b (D110). */
export const isStepLesson = (lesson: { blocks: readonly LessonBlock[] }): boolean => lesson.blocks.some((b) => b.type === "step");

export interface LessonView {
  lesson_id: string;
  node_id: string;
  title: string;
  estimated_minutes: number;
  kc_ids: string[];
  version: string;
  pack: { pack_id: string; version: string } | null;
  blocks: LessonBlock[];
  position: { block_index: number; updated_ts: number } | null;
}

export type CheckVerdict = "correct" | "incorrect" | "partially_correct" | "not_checked";

export interface CheckPart {
  part_id: string;
  verdict: CheckVerdict;
  marks_awarded: number;
  marks_total: number;
  /** The marker's error category: the client picks the UXS-16 words from it. */
  category: string | null;
  misconception: { misconception_id: string; name: string } | null;
  /** The chosen option's authored feedback, shown as text. */
  feedback: string | null;
  check_ids: string[];
}

/** A step-shape lesson's why and checked working (D110): with a correct answer, or the step shown after two misses. */
export interface CheckExplanation {
  shown: boolean;
  why: string | null;
  names: string | null;
  working: { latex: string; reason: string; check_id: string | null }[];
}

export interface CheckResult {
  client_attempt_id: string;
  item_id: string;
  verdict: CheckVerdict;
  marks_awarded: number;
  marks_total: number;
  parts: CheckPart[];
  /** Absent from a core before D110 and on a script-shape lesson's checks; null after a first miss. */
  explanation?: CheckExplanation | null;
}

// --- TABLE UXS-22a: a topic's label from its KCs' states (first matching row wins) -----------------------------------

/** The state whose LRN-09a label, icon, pattern and `mastery-*` token a tile shows. */
export function topicLabel(states: readonly KcState[]): KcState {
  if (states.some((s) => s === "needs_review")) return "needs_review";
  if (states.some((s) => s === "moved_on")) return "moved_on";
  if (states.every((s) => s === "not_started")) return "not_started";
  if (states.every((s) => s === "mastered")) return "mastered";
  if (states.every((s) => s === "ready_to_check" || s === "mastered")) return "ready_to_check";
  return "learning";
}

/** How many of a topic's KCs are in each band, for the segmented bar ("4 Secure · 2 Learning · 1 Not started"). */
export function bandCounts(kcs: readonly Pick<MapKc, "band">[]): Record<MapBand, number> {
  const counts: Record<MapBand, number> = { secure: 0, learning: 0, not_started: 0 };
  for (const kc of kcs) counts[kc.band] += 1;
  return counts;
}

/** A topic's codes in one syllabus ("1.2"); none when the topic is outside it, so the filter hides it (D113). */
export function syllabusCodes(topic: Pick<MapTopic, "syllabus_points">, scheme: string): SyllabusPoint[] {
  return (topic.syllabus_points ?? []).filter((p) => p.scheme === scheme);
}

// --- the boundary checks (the proposal's shapes; PracticeItem against the contract) -----------------------------------

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isString = (v: unknown): v is string => typeof v === "string";
const isCount = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const oneOf = (v: unknown, values: readonly unknown[]) => values.includes(v);
const listOf = (v: unknown, item: (x: unknown) => boolean) => Array.isArray(v) && v.every(item);
const isPack = (v: unknown) => v === null || (isObject(v) && isString(v.pack_id) && isString(v.version));
const isPoint = (p: unknown) => isObject(p) && isString(p.scheme) && isString(p.code) && oneOf(p.coverage, ["full", "partial"]);
const pointsOk = (v: unknown) => v === undefined || listOf(v, isPoint);
const isSyllabus = (s: unknown) => isObject(s) && isString(s.scheme) && isString(s.board) && isString(s.syllabus_code);
const isKc = (k: unknown) => isObject(k) && pointsOk(k.syllabus_points) && isString(k.kc_id) && isString(k.code) && isString(k.statement) && oneOf(k.state, KC_STATES) && oneOf(k.band, BANDS);
const isMapLesson = (l: unknown) => isObject(l) && pointsOk(l.syllabus_points) && isString(l.lesson_id) && isString(l.title) && isCount(l.estimated_minutes) && listOf(l.kc_ids, isString);
const isTopic = (t: unknown) => isObject(t) && pointsOk(t.syllabus_points) && isString(t.node_id) && isString(t.code) && isString(t.title) && listOf(t.kcs, isKc) && (t.kcs as unknown[]).length > 0 && listOf(t.lessons, isMapLesson);
const isSection = (s: unknown) => isObject(s) && pointsOk(s.syllabus_points) && isString(s.node_id) && isString(s.code) && isString(s.title) && listOf(s.topics, isTopic) && (s.topics as unknown[]).length > 0;

function invalid(problems: string[]): never {
  throw new CommandError(200, "invalid_reply", false, { fields: problems });
}

/** The problems of a subject-map reply, by field (empty when it is well formed). */
export function subjectMapProblems(value: unknown): string[] {
  if (!isObject(value)) return ["reply"];
  const problems: string[] = [];
  const check = (ok: boolean, field: string) => ok || problems.push(field);
  check(isString(value.subject_id) && /^cur\.(mth|phy|chm|bio|acc)\.(o|a)$/.test(value.subject_id), "subject_id");
  check(oneOf(value.subject, ["mathematics", "physics", "chemistry", "biology", "accounting"]), "subject");
  check(oneOf(value.level, ["O", "A"]), "level");
  check(value.title === null || isString(value.title), "title");
  check(oneOf(value.status, ["ready", "not_installed", "unverified"]), "status");
  check(isPack(value.pack), "pack");
  check(value.syllabuses === undefined || listOf(value.syllabuses, isSyllabus), "syllabuses");
  check(listOf(value.sections, isSection), "sections");
  return problems;
}

export function readSubjectMap(value: unknown): SubjectMap {
  const problems = subjectMapProblems(value);
  if (problems.length > 0) invalid(problems);
  return value as SubjectMap;
}

const CHECK_BLOCKS: readonly string[] = ["prior_knowledge", "guided_step", "quick_check", "exit_check", "check"];
const STEP_ACTIONS: readonly string[] = ["try_first", "choose", "predict", "calculate"];
const isVisual = (v: unknown) => isObject(v) && (isString(v.diagram_id) || (isString(v.visual_type) && isObject(v.params)));
const isItem = (i: unknown) => errors("api/commands#/$defs/PracticeItem", i).length === 0;
const isExample = (w: unknown) =>
  isObject(w) && isString(w.worked_example_id) && isString(w.problem) && listOf(w.steps, (s) => isObject(s) && isCount(s.n) && isString(s.latex) && isString(s.reason) && (s.check_id === null || isString(s.check_id))) &&
  isObject(w.final_answer) && isString(w.final_answer.latex) && isString(w.final_answer.check_id) && listOf(w.faded_steps, isCount) &&
  (w.visual === undefined || w.visual === null || errors("visuals/visual-spec#/$defs/resolved", w.visual).length === 0);

function blockProblem(b: unknown): boolean {
  if (!isObject(b) || !isString(b.type)) return false;
  if (CHECK_BLOCKS.includes(b.type)) return listOf(b.items, isItem);
  switch (b.type) {
    case "idea":
      return isString(b.text) && (b.visual === null || isVisual(b.visual));
    case "worked_example":
      return isExample(b.worked_example);
    case "common_mistakes":
      return (b.text === null || isString(b.text)) && listOf(b.mistakes, (m) => isObject(m) && isString(m.misconception_id) && isString(m.name) && isString(m.fix));
    case "visual":
      return isVisual(b.visual) && (b.caption === null || isString(b.caption));
    case "summary":
      return isString(b.text);
    case "step":
      return oneOf(b.action, STEP_ACTIONS) && (b.text === null || isString(b.text)) && (b.visual === null || isVisual(b.visual)) && listOf(b.items, isItem) && (b.items as unknown[]).length === 1;
    default:
      return false;
  }
}

/** The problems of a lesson reply, by field (`blocks.<n>` for a bad block). */
export function lessonProblems(value: unknown): string[] {
  if (!isObject(value)) return ["reply"];
  const problems: string[] = [];
  const check = (ok: boolean, field: string) => ok || problems.push(field);
  check(isString(value.lesson_id) && /^les\.(mth|phy|chm|bio|acc)\.(o|a)\.[a-z0-9_]+\.[a-z0-9_]+$/.test(value.lesson_id), "lesson_id");
  check(isString(value.node_id), "node_id");
  check(isString(value.title), "title");
  check(isCount(value.estimated_minutes), "estimated_minutes");
  check(listOf(value.kc_ids, isString), "kc_ids");
  check(isString(value.version) && /^[0-9]+\.[0-9]+\.[0-9]+$/.test(value.version), "version");
  check(isPack(value.pack), "pack");
  if (!Array.isArray(value.blocks) || value.blocks.length === 0) problems.push("blocks");
  else value.blocks.forEach((b, i) => check(blockProblem(b), `blocks.${i}`));
  const at = value.position;
  check(at === null || (isObject(at) && isCount(at.block_index) && isCount(at.updated_ts)), "position");
  return problems;
}

export function readLesson(value: unknown): LessonView {
  const problems = lessonProblems(value);
  if (problems.length > 0) invalid(problems);
  return value as LessonView;
}

const VERDICTS: readonly string[] = ["correct", "incorrect", "partially_correct", "not_checked"];
const isMark = (v: unknown) => typeof v === "number" && v >= 0;
const isPart = (p: unknown) =>
  isObject(p) && isString(p.part_id) && oneOf(p.verdict, VERDICTS) && isMark(p.marks_awarded) && isMark(p.marks_total) && (p.category === null || isString(p.category)) &&
  (p.misconception === null || (isObject(p.misconception) && isString(p.misconception.misconception_id) && isString(p.misconception.name))) &&
  (p.feedback === null || isString(p.feedback)) && listOf(p.check_ids, isString);
const isExplanation = (e: unknown) =>
  e === undefined || e === null ||
  (isObject(e) && typeof e.shown === "boolean" && (e.why === null || isString(e.why)) && (e.names === null || isString(e.names)) &&
    listOf(e.working, (w) => isObject(w) && isString(w.latex) && isString(w.reason) && (w.check_id === null || isString(w.check_id))));

export function checkResultProblems(value: unknown): string[] {
  if (!isObject(value)) return ["reply"];
  const problems: string[] = [];
  const check = (ok: boolean, field: string) => ok || problems.push(field);
  check(isString(value.client_attempt_id), "client_attempt_id");
  check(isString(value.item_id), "item_id");
  check(oneOf(value.verdict, VERDICTS), "verdict");
  check(isMark(value.marks_awarded) && isMark(value.marks_total), "marks");
  check(Array.isArray(value.parts) && value.parts.length > 0 && value.parts.every(isPart), "parts");
  check(isExplanation(value.explanation), "explanation");
  return problems;
}

export function readCheckResult(value: unknown): CheckResult {
  const problems = checkResultProblems(value);
  if (problems.length > 0) invalid(problems);
  return value as CheckResult;
}

/** The curriculum map's subject node id of a profile subject and level (`physics`, O -> `cur.phy.o`, CUR-02). */
export function subjectId(subject: string, level: string): string | null {
  const code = ({ mathematics: "mth", physics: "phy", chemistry: "chm", biology: "bio", accounting: "acc" } as Record<string, string>)[subject];
  return code === undefined || !/^[OA]$/i.test(level) ? null : `cur.${code}.${level.toLowerCase()}`;
}

// --- the subject map's controller ------------------------------------------------------------------------------------

export interface MapView {
  status: "idle" | "loading" | "ready" | "failed";
  subjectId: string | null;
  map: SubjectMap | null;
  /** The syllabus whose topics are shown (a scheme id), or null for every topic (UXS-22, D113). */
  syllabus?: string | null;
}

export const EMPTY_MAP: MapView = { status: "idle", subjectId: null, map: null };

class Holder<T> {
  protected view: T;
  private readonly listeners = new Set<Listener>();

  constructor(initial: T) {
    this.view = initial;
  }

  getView = (): T => this.view;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  protected set(next: T): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }
}

/** One subject's map for the unlocked profile. A mastery change (`progress.update` on any open session) re-reads the
 * map when the KC is on it, so the tile shows the new state well within UXS-22's second. */
export class SubjectMapController extends Holder<MapView> {
  /** The syllabus chosen per subject, kept while the app is open. */
  private readonly syllabuses = new Map<string, string>();

  constructor(private readonly profiles: ProfileController) {
    super(EMPTY_MAP);
    profiles.onClear(() => {
      this.syllabuses.clear();
      this.set(EMPTY_MAP);
    });
  }

  /** Show one syllabus's topics (a scheme id of the map), or every topic (null). */
  chooseSyllabus(scheme: string | null): void {
    const { subjectId, map } = this.view;
    if (subjectId === null) return;
    const known = scheme !== null && (map?.syllabuses ?? []).some((s) => s.scheme === scheme);
    if (known) this.syllabuses.set(subjectId, scheme);
    else this.syllabuses.delete(subjectId);
    this.set({ ...this.view, syllabus: known ? scheme : null });
  }

  private chosen(subjectId: string, map: SubjectMap): string | null {
    const scheme = this.syllabuses.get(subjectId);
    return scheme !== undefined && (map.syllabuses ?? []).some((s) => s.scheme === scheme) ? scheme : null;
  }

  /** Read a subject's map; a reply that lands after a lock or switch, or for another subject, is dropped. While a
   * re-read runs the map already shown stays (no skeleton flash). */
  async load(subjectId: string): Promise<void> {
    const generation = this.profiles.generation;
    const same = this.view.subjectId === subjectId && this.view.map !== null;
    this.set(same ? { ...this.view } : { status: "loading", subjectId, map: null });
    try {
      const map = readSubjectMap(await this.profiles.commands.get<unknown>(`/v1/curriculum/${encodeURIComponent(subjectId)}/map`));
      if (generation !== this.profiles.generation || this.view.subjectId !== subjectId) return;
      this.set({ status: "ready", subjectId, map, syllabus: this.chosen(subjectId, map) });
    } catch {
      if (generation === this.profiles.generation && this.view.subjectId === subjectId) this.set({ ...this.view, status: same ? "ready" : "failed" });
    }
  }

  /** A KC's mastery moved (LRN-34 `progress.update`): re-read when it is on the map shown. */
  onProgress(kcId: string): void {
    const { map, subjectId } = this.view;
    if (map === null || subjectId === null) return;
    if (map.sections.some((s) => s.topics.some((t) => t.kcs.some((k) => k.kc_id === kcId)))) void this.load(subjectId);
  }
}

// --- the lesson player's controller ----------------------------------------------------------------------------------

export interface CheckState {
  status: "checking" | "marked" | "failed";
  result: CheckResult | null;
  /** Answers sent for this item in this visit (the `attempt` of the last one); a failed send is not a try. */
  tries: number;
  /** The parts of the answer last sent, shown beside its marking. */
  answers: PartAnswer[];
}

export interface LessonPlayerView {
  status: "idle" | "loading" | "ready" | "failed";
  lessonId: string | null;
  lesson: LessonView | null;
  /** The block shown. */
  index: number;
  /** Each check item's last answer, by item id. */
  checks: Record<string, CheckState>;
  /** Whether saving the position failed: the lesson goes on; the next move tries again. */
  positionFailed: boolean;
  /** Position in the explanation/example shown before a step lesson, retained across tutor help. */
  teachingIndex?: number;
}

export const EMPTY_LESSON: LessonPlayerView = { status: "idle", lessonId: null, lesson: null, index: 0, checks: {}, positionFailed: false };

export class LessonController extends Holder<LessonPlayerView> {
  private loadRequest = 0;
  private teachingPositions = new Map<string, number>();
  constructor(private readonly profiles: ProfileController) {
    super(EMPTY_LESSON);
    profiles.onClear(() => { this.teachingPositions.clear(); this.set(EMPTY_LESSON); });
  }

  /** Open a lesson at its saved position (or its first block). */
  async load(lessonId: string, teaching = false): Promise<void> {
    const generation = this.profiles.generation;
    const request = ++this.loadRequest;
    this.set({ ...EMPTY_LESSON, status: "loading", lessonId });
    try {
      const lesson = readLesson(await this.profiles.commands.get<unknown>(`/v1/lessons/${encodeURIComponent(lessonId)}${teaching ? "?teaching=true" : ""}`));
      if (generation !== this.profiles.generation || this.view.lessonId !== lessonId || request !== this.loadRequest) return;
      this.set({ ...EMPTY_LESSON, status: "ready", lessonId, lesson, index: lesson.position?.block_index ?? 0,
        teachingIndex: this.teachingPositions.get(`${lessonId}@${lesson.version}`) ?? 0 });
    } catch {
      if (generation === this.profiles.generation && this.view.lessonId === lessonId && request === this.loadRequest) this.set({ ...this.view, status: "failed" });
    }
  }

  goTeaching(index: number): void {
    const lesson = this.view.lesson;
    if (!lesson || index < 0 || index > lesson.blocks.filter((block) => block.type === "idea" || block.type === "worked_example").length) return;
    this.teachingPositions.set(`${lesson.lesson_id}@${lesson.version}`, index);
    this.set({ ...this.view, teachingIndex: index });
  }

  /** Move to a block (Next, Back) and save the position; a failed save never stops the lesson. */
  async go(index: number): Promise<void> {
    const { lesson } = this.view;
    if (lesson === null || index < 0 || index >= lesson.blocks.length || index === this.view.index) return;
    const generation = this.profiles.generation;
    this.set({ ...this.view, index });
    try {
      await this.profiles.commands.put<unknown>(`/v1/lessons/${encodeURIComponent(lesson.lesson_id)}/position`, { block_index: index, lesson_version: lesson.version }, clientId());
      if (generation === this.profiles.generation && this.view.lesson === lesson && this.view.positionFailed) this.set({ ...this.view, positionFailed: false });
    } catch {
      if (generation === this.profiles.generation && this.view.lesson === lesson) this.set({ ...this.view, positionFailed: true });
    }
  }

  /** Answer a check item; the core marks it (ASM-02). Returns the result, or null when it could not be marked. On a
   * step-shape lesson's step or check the try is sent as `attempt`, so the second miss comes back as the step shown. */
  async answer(itemId: string, parts: PartAnswer[], timeMs: number): Promise<CheckResult | null> {
    const { lesson } = this.view;
    if (lesson === null) return null;
    const generation = this.profiles.generation;
    const mark = (state: CheckState) => {
      if (generation === this.profiles.generation && this.view.lesson === lesson) this.set({ ...this.view, checks: { ...this.view.checks, [itemId]: state } });
    };
    const before = this.view.checks[itemId];
    const tries = (before?.tries ?? 0) + 1;
    mark({ status: "checking", result: before?.result ?? null, tries: before?.tries ?? 0, answers: parts });
    const stepped = lesson.blocks.some((b) => (b.type === "step" || b.type === "check") && b.items.some((i) => i.item_id === itemId));
    const body = { client_attempt_id: clientId(), item_id: itemId, answer: { parts }, time_ms: Math.max(0, Math.round(timeMs)), ...(stepped ? { attempt: tries } : {}) };
    try {
      const result = readCheckResult(await this.profiles.commands.post<unknown>(`/v1/lessons/${encodeURIComponent(lesson.lesson_id)}/checks`, body, clientId()));
      mark({ status: "marked", result, tries, answers: parts });
      return result;
    } catch {
      mark({ status: "failed", result: null, tries: before?.tries ?? 0, answers: parts });
      return null;
    }
  }
}
