import { errors, type learning_journey as J } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";

export type LearningJourneyView = J.View;
export type JourneyAction = J.Step["action"];

/** Resume the saved activity; never create a second practice set just to navigate. */
export function nextJourneyAction(view: LearningJourneyView): JourneyAction {
  if (view.phase === "ready") return view.lesson_id ? "review" : "practice";
  if (view.phase === "repair") return view.lesson_id ? "review" : "practice";
  if (view.phase === "question") return "practice";
  return "check";
}

export class LearningJourneyController {
  private view: { journey: J.View | null; busy: boolean; error: boolean } = { journey: null, busy: false, error: false };
  private listeners = new Set<() => void>();
  private request = 0;
  private mutation = 0;
  constructor(private profiles: ProfileController) {
    profiles.onClear(() => { this.request++; this.mutation = 0; this.set({ journey: null, busy: false, error: false }); });
  }
  getView = () => this.view;
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  private set(view: typeof this.view) { this.view = view; for (const fn of this.listeners) fn(); }
  private async run(method: "GET" | "POST", path: string, body?: J.Start | J.Step): Promise<J.View | null> {
    if (method === "POST" && this.mutation) return null;
    const generation = this.profiles.generation, request = ++this.request;
    if (method === "POST") this.mutation = request;
    this.set({ ...this.view, busy: true, error: false });
    try {
      const commands = this.profiles.commands.scoped(() => generation === this.profiles.generation);
      const reply = await commands.request<J.Reply>(method, path, body, method === "GET" ? null : clientId());
      if (generation !== this.profiles.generation || request !== this.request) return null;
      if (errors("learning/journey#/$defs/Reply", reply).length) throw new Error("invalid_reply");
      this.set({ journey: reply.journey, busy: false, error: false });
      return reply.journey;
    } catch {
      if (generation === this.profiles.generation && request === this.request) this.set({ ...this.view, busy: false, error: true });
      return null;
    } finally {
      if (this.mutation === request) this.mutation = 0;
    }
  }
  // A navigation refresh must not supersede an in-flight start/step and discard its result.
  load = () => this.mutation ? Promise.resolve(this.view.journey) : this.run("GET", "/v1/learning-journey");
  start = (node_id: string, kc_id: string, lesson_id?: string) => this.run("POST", "/v1/learning-journey", { node_id, kc_id, ...(lesson_id ? { lesson_id } : {}) });
  step = (action: JourneyAction) => this.run("POST", "/v1/learning-journey/step", { action });
}
