import type { StudyContracts as W } from "@jana/client-core";
import { useEffect, useRef } from "react";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";

/** The same saved next step on Home and beside the material; no generation or grading on opening. */
export function StudyJourney({ journey, busy, onContinue, onLesson, onResults, onReviewDue }: {
  journey: W.StudyJourney; busy: boolean; onContinue: () => void; onLesson: () => void; onResults: () => void; onReviewDue?: () => void;
}) {
  const t = useT();
  const refresh = useRef(onReviewDue);
  refresh.current = onReviewDue;
  useEffect(() => {
    const due = journey.next_review_at;
    if (due === null || due <= Date.now() || !refresh.current) return;
    const timer = setTimeout(() => refresh.current?.(), Math.min(due - Date.now() + 50, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [journey.next_review_at]);
  return <section aria-label={t("study.journey.title")} className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5">
    <h2 className="text-label text-faint">{t("study.journey.title")}</h2>
    <p className="text-ui font-semibold text-ink">{journey.title}</p>
    <p className="text-ui text-muted" role="status">{t(`study.journey.phase.${journey.phase}`, { count: journey.questions_to_review })}</p>
    <p className="text-meta text-muted">{t("study.journey.progress", { reviewed: journey.cards_reviewed, cards: journey.cards_total, answered: journey.questions_answered, questions: journey.questions_total })}</p>
    {journey.phase === "complete" && journey.next_review_at !== null && <p className="text-meta text-muted">{t("study.journey.review_due", { date: new Date(journey.next_review_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) })}</p>}
    <div className="flex flex-wrap gap-2">
      <Button size="sm" tone="primary" disabled={busy} onClick={onContinue}>{t(journey.retry_quiz ? "study.journey.retry_quiz" : `study.journey.action.${journey.phase}`)}</Button>
      {journey.next_aid_id !== journey.lesson_aid_id && <Button size="sm" tone="ghost" disabled={busy} onClick={onLesson}>{t("study.journey.open_lesson")}</Button>}
      {journey.quiz_aid_id && journey.questions_answered > 0 && journey.next_aid_id !== journey.quiz_aid_id && <Button size="sm" tone="ghost" disabled={busy} onClick={onResults}>{t("study.journey.open_quiz")}</Button>}
    </div>
    <p className="text-meta text-faint">{t("study.journey.evidence")}</p>
  </section>;
}
