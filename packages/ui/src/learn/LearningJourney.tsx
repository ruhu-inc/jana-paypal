import type { JourneyAction, LearningJourneyView } from "@jana/client-core";
import { useId } from "react";
import { useT } from "../i18n.js";
import { Button, ErrorState } from "../primitives/index.js";

export function LearningJourney({ view, busy, error, onStep, onSource, onTopic, onRetry, place = "overview", onPlan, onHelp }: {
  view: LearningJourneyView | null; busy: boolean; error: boolean;
  onStep: (action: JourneyAction) => void; onSource: () => void; onTopic: () => void; onRetry: () => void;
  place?: "overview" | "lesson" | "practice" | "tutor";
  onPlan?: () => void; onHelp?: () => void;
}) {
  const t = useT();
  const id = useId();
  if (!view) return error ? <ErrorState message={t("journey.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} /> : null;
  const reason = view.phase === "ready" ? (view.lesson_id ? "ready_lesson" : view.prior_attempts ? "ready_known" : "ready")
    : view.phase === "repair" ? `repair_${view.evidence}`
    : view.phase;
  const step = place === "lesson" ? "learn" : place === "tutor" ? view.practice_set_id ? "practise" : "learn" : view.stage === "check" || ["ready_check", "complete"].includes(view.phase) ? "check" : view.phase === "question" || view.phase === "repair" || !view.lesson_id ? "practise" : "learn";
  return <section aria-labelledby={id} className="mx-4 my-3 flex shrink-0 flex-col gap-3 rounded-card border border-line bg-canvas p-4 sm:mx-6" data-learning-journey={view.phase}>
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h2 id={id} className="text-label text-faint">{t("journey.goal")}</h2>
        <p className="text-ui font-semibold text-ink">{view.goal.objective}</p>
      </div>
      {onPlan && <Button size="sm" tone="ghost" disabled={busy} onClick={onPlan}>{t("journey.back_plan")}</Button>}
    </div>
    <ol aria-label={t("journey.steps")} className="grid grid-cols-3 gap-2">
      {(["learn", "practise", "check"] as const).map((name, i) => <li key={name} aria-current={step === name ? "step" : undefined}
        className={`flex min-w-0 items-center gap-2 border-b-2 pb-2 text-meta ${step === name ? "border-ink font-semibold text-ink" : "border-line text-muted"}`}>
        <span aria-hidden="true" className="flex size-6 shrink-0 items-center justify-center rounded-full bg-panel tabular-nums">{i + 1}</span><span className="min-w-0 break-words">{t(`journey.step.${name}`)}</span>
      </li>)}
    </ol>
    <p className="text-meta text-muted" role="status">{t(place === "tutor" ? "journey.status.tutor" : place === "lesson" ? "journey.status.lesson" : view.phase === "ready" && !view.lesson_id ? "journey.status.ready_without_lesson" : `journey.status.${view.phase}`)}</p>
    {view.source_changed && <p className="text-meta text-muted">{t("journey.pinned")}</p>}
    {error && <ErrorState message={t("journey.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} />}
    <div className="flex flex-wrap gap-2">
      {place === "tutor" ? <>
        {view.practice_set_id && <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("practice")}>{t("journey.back_question")}</Button>}
        {view.lesson_id && <Button size="sm" tone={view.practice_set_id ? "secondary" : "primary"} disabled={busy} onClick={() => onStep("review")}>{t("journey.back_lesson")}</Button>}
      </> : <>
      {view.phase === "ready" && <>
        {view.lesson_id && place !== "lesson" && <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("review")}>{t(view.lesson_opened ? "journey.continue_lesson" : "journey.teach_beginning")}</Button>}
        <Button size="sm" tone={view.lesson_id && place !== "lesson" ? "secondary" : "primary"} disabled={busy} onClick={() => onStep("practice")}>{t(view.lesson_opened ? "journey.try_question" : "journey.start_check")}</Button>
      </>}
      {view.phase === "question" && place !== "practice" && <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("practice")}>{t("journey.resume_question")}</Button>}
      {view.phase === "repair" && (view.lesson_id ? <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("review")}>{t("journey.review_lesson")}</Button>
        : onHelp ? <Button size="sm" tone="primary" disabled={busy} onClick={onHelp}>{t("journey.get_help")}</Button>
        : <Button size="sm" disabled={busy} onClick={() => onStep("practice")}>{t("journey.resume_question")}</Button>)}
      {view.phase === "ready_check" && <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("check")}>{t("journey.new_check")}</Button>}
      {view.phase === "complete" && <Button size="sm" tone="primary" disabled={busy} onClick={() => onStep("check")}>{t("journey.continue_practice")}</Button>}
      {place !== "lesson" && view.lesson_id && ["ready_check", "unavailable"].includes(view.phase) && <Button size="sm" tone="ghost" disabled={busy} onClick={() => onStep("review")}>{t("journey.review_lesson")}</Button>}
      {view.phase === "unavailable" && <Button size="sm" disabled={busy} onClick={() => onStep("check")}>{t("journey.retry_check")}</Button>}
      </>}
      {view.source && <Button size="sm" tone="ghost" disabled={busy} onClick={onSource}>{t("journey.back_source", { revision: view.source.revision })}</Button>}
      <Button size="sm" tone="ghost" disabled={busy} onClick={onTopic}>{t(view.phase === "complete" ? "journey.next_topic" : "journey.choose")}</Button>
    </div>
    <details className="text-meta text-muted"><summary className="cursor-pointer">{t("journey.why")}</summary>
      <div className="mt-2 flex flex-col gap-1"><p>{t(`journey.reason.${reason}`)}</p><p>{t("journey.evidence", { count: view.prior_attempts })}</p>
        {view.lesson_opened && <p>{t("journey.lesson_opened")}</p>}
      </div>
    </details>
  </section>;
}
