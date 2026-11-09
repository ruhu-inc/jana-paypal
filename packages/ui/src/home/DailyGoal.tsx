// The daily goal chip (LRN-45, D120): a small goal, two study actions a day by default, filling as today's plan is
// worked. It never shames: no streak, no lost-day message, no count that resets with a warning; a day with nothing done
// yet reads exactly like the start of any day. The chip is a menu: change the goal (1 to 4 a day) or hide it.
import type { GoalProgress } from "@jana/client-core";
import { DAILY_GOAL_CHOICES } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { MenuButton } from "../primitives/index.js";

export function DailyGoalChip({ progress, onChange }: { progress: GoalProgress; onChange: (target: number) => void }) {
  const t = useT();
  const words = progress.met ? t("goal.met") : t("goal.progress", { done: progress.done, target: progress.target });
  return (
    <MenuButton label={t("goal.label")}
      items={[
        ...DAILY_GOAL_CHOICES.map((count) => ({ id: `goal-${count}`, group: "goal", label: t("goal.choice", { count }), checked: count === progress.target, onSelect: () => onChange(count) })),
        { id: "goal-hide", group: "hide", label: t("goal.hide"), onSelect: () => onChange(0) },
      ]}
      trigger={({ ref, ...menu }) => (
        <button ref={ref} type="button" {...menu} aria-label={`${t("goal.label")}: ${words}`} data-goal-met={progress.met ? "" : undefined} data-goal={`${progress.done}/${progress.target}`}
          className={`jana-press relative inline-flex min-h-9 items-center gap-2 rounded-chip px-3 text-meta font-medium after:absolute after:-inset-1 after:content-[''] hover:bg-wash aria-expanded:bg-wash ${progress.met ? "bg-verified-soft text-verified" : "bg-panel text-ink"}`}>
          {progress.met && <Icon name="verified" size={16} />}
          <span aria-hidden="true" className="inline-flex gap-1">
            {Array.from({ length: progress.target }, (_, i) => (
              <span key={i} data-goal-step={i < progress.done ? "done" : "open"} className={`inline-block size-2 rounded-pill ${i < progress.done ? "bg-verified" : "bg-line-strong"}`} />
            ))}
          </span>
          <span>{words}</span>
        </button>
      )} />
  );
}
