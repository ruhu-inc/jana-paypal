// A gentle daily goal (LRN-45, D120): a small number of study actions a day, two by default, counted from today's plan
// (a practice set, a lesson, a review, a past-paper section: the plan's study blocks, never its look back). It never
// shames: nothing here keeps a streak, counts misses or remembers yesterday, so a missed day is simply a new day.
// The learner can change the goal or hide it (0). The core does not store that choice yet (its Settings key is proposed
// in contract-additions-m45d.json): until it does, the choice holds for the open profile and is dropped with it
// (SEC-19); a core that sends `daily_goal` in the profile's settings is followed, and then the choice is saved there.
import type { PlanView } from "./plans.js";
import type { ProfileController } from "./profiles.js";

export const DEFAULT_DAILY_GOAL = 2;
/** The goals a learner can pick, in study actions a day; 0 hides the chip. */
export const DAILY_GOAL_CHOICES = [1, 2, 3, 4] as const;
/** The plan blocks that are study actions (LRN-45): everything but the look back. */
export const STUDY_ACTION_KINDS = ["warmup", "main", "mixed", "mock", "mastery_check"] as const;

export interface GoalProgress {
  /** Study actions finished today, at most the goal. */
  done: number;
  target: number;
  met: boolean;
}

/** Today's progress on the goal from today's plan. A plan of the day already finished ("Keep learning") means the goal
 * was met earlier today. */
export function goalProgress(plan: PlanView | null, target: number): GoalProgress {
  const goal = Math.max(1, Math.round(target));
  if (plan === null) return { done: 0, target: goal, met: false };
  const finished = plan.blocks.filter((b) => b.status === "done" && (STUDY_ACTION_KINDS as readonly string[]).includes(b.kind)).length;
  const done = Math.min(goal, plan.keep_learning ? goal : finished);
  return { done, target: goal, met: done >= goal };
}

type Listener = () => void;

/** The open profile's daily goal: the core's `daily_goal` setting when it has one, otherwise the choice made since the
 * profile was opened, otherwise two. */
export class DailyGoal {
  private chosen: number | null = null;
  private readonly listeners = new Set<Listener>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.update(null));
  }

  private stored(): number | null {
    const value = (this.profiles.getView().settings as Record<string, unknown> | null)?.daily_goal;
    return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 4 ? value : null;
  }

  /** The goal in study actions a day; 0 when the learner hid the chip. */
  getTarget = (): number => this.chosen ?? this.stored() ?? DEFAULT_DAILY_GOAL;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    const off = this.profiles.subscribe(listener);
    return () => {
      this.listeners.delete(listener);
      off();
    };
  };

  private update(value: number | null): void {
    this.chosen = value;
    for (const listener of [...this.listeners]) listener();
  }

  /** Change the goal (1 to 4) or hide it (0). Saved in the profile's settings where the core keeps the key. */
  async set(target: number): Promise<void> {
    const value = Math.min(4, Math.max(0, Math.round(target)));
    this.update(value);
    if (this.stored() === null) return;
    try {
      await this.profiles.setPreferences({ daily_goal: value } as never);
    } catch {
      // The choice still holds for this visit; nothing is lost if the core could not save it.
    }
  }
}
