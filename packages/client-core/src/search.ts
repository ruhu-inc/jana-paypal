// Worked-solution search (UXS-51, ASM-22, PED-09): on the content-only class, or while the tutor cannot run, the Ask
// box searches the installed worked solutions instead (`GET /v1/search/items?q=`). The core searches only ordinary
// items, leaves out the mock-reserved pool and every item of a protected assessment before it matches anything, and
// records each preview it returns as seen; an opened match's full solution comes on request with no attempt
// (`GET /v1/items/{item_id}/solution`, adopted in D101), behind the same filter. A match is
// listed with its topic, a one-line preview and the words that matched.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import { CLIENT_KEYS } from "./generated/keys.js";
import type { ProfileController } from "./profiles.js";
import { CommandError } from "./stream/commands.js";

export type SearchItem = api_commands.PracticeItem;
export type WorkedSolution = api_commands.WorkedExampleView;
type Listener = () => void;

export interface SearchMatch {
  item: SearchItem;
  /** The stem's first line, cut at a word to the preview length. */
  preview: string;
  /** The query's words found in the stem, as typed in the stem (for marking them in the preview). */
  words: string[];
}

export interface SearchState {
  status: "idle" | "searching" | "ready" | "failed" | "unavailable";
  query: string;
  matches: SearchMatch[];
  /** The match opened on the board, and its solution once asked for. */
  opened: { item: SearchItem; solution: WorkedSolution | null; solutionStatus: "idle" | "loading" | "ready" | "failed" } | null;
}

export const EMPTY_SEARCH: SearchState = { status: "idle", query: "", matches: [], opened: null };

/** The words of a query, lower-cased, each once, in order. */
export function queryWords(query: string): string[] {
  return [...new Set(query.toLocaleLowerCase().split(/\s+/).map((w) => w.trim()).filter((w) => w.length > 0))];
}

/** A stem's first line, cut at the last whole word within the preview length, never inside a `$…$` maths span. */
export function previewOf(stem: string, max: number = CLIENT_KEYS.ui_search_preview_chars): string {
  const line = (stem.split("\n").find((l) => l.trim() !== "") ?? "").trim();
  if (line.length <= max) return line;
  let cut = line.slice(0, max);
  const space = cut.lastIndexOf(" ");
  if (space > max / 2) cut = cut.slice(0, space);
  if ((cut.match(/\$/g) ?? []).length % 2 === 1) cut = cut.slice(0, cut.lastIndexOf("$"));
  return `${cut.trimEnd()}…`;
}

/** A match as the list shows it: the preview and the query words the stem holds. */
export function matchOf(item: SearchItem, query: string): SearchMatch {
  const stem = item.stem.toLocaleLowerCase();
  return { item, preview: previewOf(item.stem), words: queryWords(query).filter((w) => stem.includes(w)) };
}

/** The preview cut into plain and matched runs, for marking the matched words (case kept as in the preview). */
export function markedRuns(preview: string, words: readonly string[]): { text: string; matched: boolean }[] {
  const lower = preview.toLocaleLowerCase();
  const marks = new Array<boolean>(preview.length).fill(false);
  for (const word of words) {
    for (let at = lower.indexOf(word); at >= 0; at = lower.indexOf(word, at + word.length)) marks.fill(true, at, at + word.length);
  }
  const runs: { text: string; matched: boolean }[] = [];
  for (let i = 0; i < preview.length; i += 1) {
    const char = preview.charAt(i);
    const matched = marks[i] === true;
    const last = runs[runs.length - 1];
    if (last !== undefined && last.matched === matched) last.text += char;
    else runs.push({ text: char, matched });
  }
  return runs;
}

function missing(error: unknown): boolean {
  return error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
}

export class SearchController {
  private state: SearchState = EMPTY_SEARCH;
  private readonly listeners = new Set<Listener>();
  private asked = 0;

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_SEARCH));
  }

  getView = (): SearchState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: SearchState): void {
    if (next === this.state) return;
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  /** Search the installed worked solutions; only the newest query's reply is shown. */
  async search(query: string): Promise<void> {
    const words = queryWords(query);
    this.asked += 1;
    const asked = this.asked;
    if (words.length === 0) {
      this.set({ ...EMPTY_SEARCH });
      return;
    }
    const generation = this.profiles.generation;
    this.set({ ...this.state, status: "searching", query, opened: null });
    try {
      const raw = await this.profiles.commands.get<unknown>(`/v1/search/items?q=${encodeURIComponent(words.join(" "))}`);
      const problems = errors("api/commands#/$defs/PracticeItemList", raw);
      if (problems.length > 0) throw new Error(problems.slice(0, 3).join("; "));
      if (asked !== this.asked || generation !== this.profiles.generation) return;
      const items = (raw as SearchItem[]).slice(0, CLIENT_KEYS.ui_search_results_max);
      this.set({ status: "ready", query, matches: items.map((item) => matchOf(item, query)), opened: null });
    } catch (error) {
      if (asked !== this.asked || generation !== this.profiles.generation) return;
      this.set({ ...this.state, status: missing(error) ? "unavailable" : "failed", matches: [] });
    }
  }

  /** Open a match on the board, as a question; its solution waits until the student asks for it. */
  open(itemId: string): void {
    const match = this.state.matches.find((m) => m.item.item_id === itemId);
    if (match !== undefined) this.set({ ...this.state, opened: { item: match.item, solution: null, solutionStatus: "idle" } });
  }

  close(): void {
    this.set({ ...this.state, opened: null });
  }

  /** "Show solution": the full worked solution, with no attempt needed (PED-09, D87). */
  async showSolution(): Promise<void> {
    const opened = this.state.opened;
    if (opened === null || opened.solutionStatus === "loading") return;
    const generation = this.profiles.generation;
    this.set({ ...this.state, opened: { ...opened, solutionStatus: "loading" } });
    try {
      const raw = await this.profiles.commands.get<unknown>(`/v1/items/${encodeURIComponent(opened.item.item_id)}/solution`);
      const problems = errors("api/commands#/$defs/WorkedExampleView", raw);
      if (problems.length > 0) throw new Error(problems.slice(0, 3).join("; "));
      if (generation !== this.profiles.generation || this.state.opened?.item.item_id !== opened.item.item_id) return;
      this.set({ ...this.state, opened: { ...opened, solution: raw as WorkedSolution, solutionStatus: "ready" } });
    } catch {
      if (generation === this.profiles.generation && this.state.opened?.item.item_id === opened.item.item_id) {
        this.set({ ...this.state, opened: { ...opened, solutionStatus: "failed" } });
      }
    }
  }
}
