// What an own-course learner should review today, on Home: their flashcard sets with cards due, from their own
// materials. The counts are the sets' own; nothing here is estimated. It gives a learner with no syllabus and no plan a
// reason to come back, using the spaced review Study already keeps.
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";

export interface DueSet {
  aid_id: string;
  title?: string;
  due: number;
}

const SHOWN = 3;

export function DueToday({ sets, hasCards, onReview }: { sets: readonly DueSet[]; hasCards: boolean; onReview: (aidId: string) => void }) {
  const t = useT();
  if (sets.length === 0) {
    return hasCards ? null : <p data-due-today="" className="text-meta text-muted">{t("home.due.empty")}</p>;
  }
  const ordered = [...sets].sort((a, b) => b.due - a.due).slice(0, SHOWN);
  return (
    <div data-due-today="" className="flex flex-col gap-2">
      <h3 className="text-ui font-medium text-ink">{t("home.due.title")}</h3>
      <ul className="flex flex-col gap-2">
        {ordered.map((set) => (
          <li key={set.aid_id} className="flex flex-wrap items-center justify-between gap-3">
            <span className="min-w-0 flex-1 text-body text-ink">
              {set.title !== undefined ? t("home.due.set", { title: set.title, count: set.due }) : t("home.due.set_private", { count: set.due })}
            </span>
            <Button size="sm" onClick={() => onReview(set.aid_id)}>{t("home.due.review")}</Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
