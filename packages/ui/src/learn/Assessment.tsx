import { formatDate } from "@jana/i18n";
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { type Assessment, type AssessmentController, type AssessmentStart, type AssessmentView, type PartAnswer } from "@jana/client-core";
import { ScreenHeader } from "../frame/Frame.js";
import { useT } from "../i18n.js";
import { Button, Callout, ErrorState, FIELD_LOOK, Loader, Notice } from "../primitives/index.js";
import { MasteryMark } from "./Mastery.js";
import { PackText } from "./PackText.js";
import { ItemVisual, PartInput, answerOf, type Draft } from "./items.js";

export interface AssessmentActions {
  onStart: (start: AssessmentStart) => void;
  onOpen: (id: string) => void;
  onAnswer: (parts: PartAnswer[] | null) => void;
  /** Explain it back: the learner's explanation of the check's idea (typed here; a transcript once voice is on). */
  onExplain?: (text: string) => void;
  onStop: () => void;
  onResume: () => void;
  onRefresh: () => void;
  onRetry: () => void;
  onList: () => void;
  onClose: () => void;
}
export interface AssessmentNames { subject: (name: string) => string; topic: (id: string) => string; kc: (id: string) => string }

function AssessmentQuestion({ assessment, view, actions }: { assessment: Assessment; view: AssessmentView; actions: AssessmentActions }) {
  const t = useT(), heading = useRef<HTMLHeadingElement>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const item = assessment.current_item;
  useEffect(() => heading.current?.focus(), []);
  if (item === null) return null;
  const parts = item.parts.map((p) => answerOf(p, drafts[p.part_id] ?? {})).filter((p): p is PartAnswer => p !== null);
  const diagnostic = assessment.kind === "diagnostic";
  return <section className="flex flex-col gap-5 rounded-card border border-line bg-canvas p-6" data-assessment-item={item.item_id}>
    <h2 ref={heading} tabIndex={-1} className="text-heading font-regular text-ink"><PackText text={item.stem} /></h2>
    <ItemVisual item={item} />
    <fieldset disabled={view.acting || view.uncertain} className="flex min-w-0 flex-col gap-5">
      <legend className="sr-only">{t("assessment.answer")}</legend>
      {item.parts.map((part) => <PartInput key={part.part_id} part={part} itemId={item.item_id} draft={drafts[part.part_id] ?? {}}
        onDraft={(draft) => setDrafts((all) => ({ ...all, [part.part_id]: draft }))} prompt={item.parts.length > 1 || part.prompt !== item.stem} />)}
      <div className="flex flex-wrap gap-2">
        <Button tone="primary" disabled={parts.length !== item.parts.length} onClick={() => actions.onAnswer(parts)}>{t("assessment.save_answer")}</Button>
        {diagnostic && <Button onClick={() => actions.onAnswer(null)}>{t("assessment.not_learned")}</Button>}
      </div>
    </fieldset>
  </section>;
}

/** Explain it back: all three answers were right; the learner explains the idea before it counts as Secure. A miss
 * brings one follow-up question; the key points themselves are never shown. */
function ExplainBack({ assessment, view, onExplain }: { assessment: Assessment; view: AssessmentView; onExplain: (text: string) => void }) {
  const t = useT(), heading = useRef<HTMLHeadingElement>(null);
  const [text, setText] = useState("");
  const explain = assessment.explain;
  useEffect(() => heading.current?.focus(), [explain?.attempts]);
  if (explain === undefined) return null;
  const field = `${assessment.assessment_id}-explain`;
  return <section className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-6" data-explain-back data-outcome={explain.outcome}>
    <h2 ref={heading} tabIndex={-1} className="text-heading font-regular text-ink">{t("assessment.explain.title")}</h2>
    <p className="text-body text-ink">{t("assessment.explain.prompt", { topic: explain.statement })}</p>
    {explain.follow_up !== null && <Callout tone="hint" heading={t("assessment.explain.follow_up")}><PackText text={explain.follow_up} /></Callout>}
    {explain.outcome === "unavailable" && <Notice tone="warn" message={t("assessment.explain.unavailable")} />}
    <fieldset disabled={view.acting || view.uncertain} className="flex min-w-0 flex-col gap-3">
      <label htmlFor={field} className="text-label text-ink">{t("assessment.explain.label")}</label>
      <textarea id={field} className={FIELD_LOOK} rows={4} maxLength={1200} value={text} aria-describedby={`${field}-hint`} onChange={(e) => setText(e.target.value)} />
      <p id={`${field}-hint`} className="text-meta text-muted">{t("assessment.explain.hint")}</p>
      <div><Button tone="primary" disabled={text.trim() === ""} onClick={() => { onExplain(text.trim()); setText(""); }}>{t("assessment.explain.check")}</Button></div>
    </fieldset>
  </section>;
}

function Outcome({ assessment, names }: { assessment: Assessment; names: AssessmentNames }) {
  const t = useT();
  if (assessment.kind === "mastery_check") return <Callout tone={assessment.state === "completed" ? "verified" : "hint"}
    heading={t(assessment.state === "completed" ? "assessment.passed" : assessment.state === "stopped" ? "assessment.stopped" : "assessment.not_yet", {
      topic: names.kc(assessment.kc_id ?? ""), day: assessment.retry_at === null ? t("assessment.later") : formatDate(assessment.retry_at),
    })}>{assessment.explain?.outcome === "not_yet" ? <p>{t("assessment.explain.not_yet")}</p> : undefined}</Callout>;
  const strong = assessment.outcome_topics.filter((x) => x.status === "mastered" || x.status === "ready_to_check");
  const build = assessment.outcome_topics.filter((x) => !strong.includes(x));
  return <div className="grid gap-5 sm:grid-cols-2">
    {[["assessment.strong", strong], ["assessment.build", build]].map(([label, topics]) => <section key={label as string} className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5">
      <h2 className="text-heading text-ink">{t(label as string)}</h2>
      <ul className="flex flex-col gap-3">{(topics as Assessment["outcome_topics"]).map((row) => <li key={row.topic_id} className="flex flex-col gap-1 text-body text-ink">
        {names.topic(row.topic_id)}<MasteryMark state={row.status} />
      </li>)}</ul>
    </section>)}
  </div>;
}

export function AssessmentContent({ view, subjects, names, actions, plan }: {
  view: AssessmentView; subjects: AssessmentStart["subject"][]; names: AssessmentNames; actions: AssessmentActions; plan?: ReactNode;
}) {
  const t = useT();
  const [subject, setSubject] = useState<AssessmentStart["subject"]>(subjects[0] ?? "mathematics");
  const current = view.current, diagnostic = current?.kind !== "mastery_check";
  const title = t(diagnostic ? "assessment.diagnostic" : "assessment.mastery");
  const item = current?.current_item;
  const nextSubject = subjects.find((s) => s !== current?.subject && !view.assessments.some((a) => a.subject === s && a.kind === "diagnostic"));
  const error = view.error === "tutor_locked" ? t("assessment.exam_locked") : view.error === "retry_not_due" ? t("assessment.retry_not_due") : t("assessment.failed");
  return <div className="flex min-h-full flex-col" data-assessment-kind={current?.kind ?? "intro"}>
    <ScreenHeader end={<Button tone="ghost" disabled={view.acting || view.uncertain} onClick={actions.onClose}>{t("action.close")}</Button>}>
      <h1 className="text-ui font-semibold text-ink">{title}</h1>
    </ScreenHeader>
    <main className="mx-auto flex w-full max-w-220 flex-col gap-5 p-6">
      {view.status === "loading" && <Loader label={t("assessment.loading")} />}
      {view.status === "failed" && <ErrorState message={error} action={{ label: t("action.try_again"), onClick: actions.onRefresh }} />}
      {view.status === "unavailable" && <Notice message={t("assessment.unavailable")} />}
      {view.error !== null && <ErrorState message={error} {...(view.uncertain ? { action: { label: t("action.try_again"), onClick: actions.onRetry } } : {})} />}
      {view.acting && <Loader label={t("assessment.saving")} />}
      {current === null && view.status === "ready" && <>
        <p className="text-body text-ink">{t("assessment.intro")}</p>
        <label className="flex flex-col gap-2 text-meta text-muted">{t("assessment.subject")}
          <select className={FIELD_LOOK} value={subject} onChange={(e) => setSubject(e.target.value as AssessmentStart["subject"])}>
            {subjects.map((s) => <option key={s} value={s}>{names.subject(s)}</option>)}
          </select>
        </label>
        <div className="flex flex-wrap gap-2"><Button tone="primary" disabled={subjects.length === 0 || view.acting || view.uncertain} onClick={() => actions.onStart({ kind: "diagnostic", subject })}>{t("card.action.start")}</Button>
          <Button onClick={actions.onClose}>{t("assessment.not_now")}</Button></div>
        <ul className="flex flex-col gap-3" aria-label={t("assessment.saved_checks")}>
          {view.assessments.map((a) => <li key={a.assessment_id}><Button disabled={view.acting || view.uncertain} onClick={() => actions.onOpen(a.assessment_id)}>
            {t(a.kind === "diagnostic" ? "assessment.resume_diagnostic" : "assessment.resume_mastery", { subject: names.subject(a.subject) })}
          </Button></li>)}
        </ul>
      </>}
      {current !== null && <>
        <p className="text-meta text-muted">{names.subject(current.subject)}</p>
        {current.state === "in_progress" && <>
          <p className="text-body text-ink">{t(diagnostic ? "assessment.question_diagnostic" : "assessment.question_mastery", { number: current.answered_count + 1 })}</p>
          {!diagnostic && <Notice message={t("assessment.mastery_intro")} />}
          {view.resumed && <p role="status" className="text-ui text-muted">{t("assessment.resumed", { number: current.answered_count + 1 })}</p>}
          {item !== null && item !== undefined && <AssessmentQuestion key={`${current.assessment_id}:${item.item_id}`} assessment={current} view={view} actions={actions} />}
        </>}
        {view.saved && diagnostic && <p role="status" className="text-ui text-muted">{t("assessment.saved")}</p>}
        {!diagnostic && view.saved && current.last_mark != null && <Callout tone={current.last_mark.verdict === "correct" ? "verified" : "look"} heading={t(current.last_mark.verdict === "correct" ? "practice.checked" : `check.verdict.${current.last_mark.verdict}`)}>
          <p>{t("check.marks_awarded", { awarded: current.last_mark.marks_awarded, total: current.last_mark.marks_total })}</p>
        </Callout>}
        {current.state === "explaining" && actions.onExplain !== undefined && <ExplainBack key={current.assessment_id} assessment={current} view={view} onExplain={actions.onExplain} />}
        {current.state === "waiting" && <>
          <Notice message={current.available_at === null ? t("assessment.waiting_bank") : t("assessment.waiting", { day: formatDate(current.available_at) })} />
          <Button disabled={view.acting || view.uncertain} onClick={actions.onRefresh}>{t("assessment.check_available")}</Button>
        </>}
        {["completed", "failed", "stopped"].includes(current.state) && <>
          <Outcome assessment={current} names={names} />
          {current.state === "stopped" && <Button tone="primary" disabled={view.acting || view.uncertain} onClick={actions.onResume}>{t("assessment.continue")}</Button>}
          {plan}
          {diagnostic && current.state === "completed" && nextSubject !== undefined && <Button disabled={view.acting || view.uncertain} onClick={() => actions.onStart({ kind: "diagnostic", subject: nextSubject })}>
            {t("assessment.next_subject", { subject: names.subject(nextSubject) })}
          </Button>}
        </>}
        <div className="flex flex-wrap gap-2">
          {(current.state === "in_progress" || current.state === "waiting") && <Button disabled={view.acting || view.uncertain} onClick={actions.onStop}>{t(diagnostic ? "assessment.finish_later" : "assessment.stop")}</Button>}
          <Button disabled={view.acting || view.uncertain} onClick={actions.onList}>{t("assessment.all_checks")}</Button>
        </div>
      </>}
    </main>
  </div>;
}

export function AssessmentPlace({ controller, ...props }: { controller: AssessmentController; subjects: AssessmentStart["subject"][]; names: AssessmentNames; onClose: () => void; plan?: ReactNode }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => { if (controller.getView().status === "idle") void controller.load(); }, [controller]);
  useEffect(() => {
    const current = view.current;
    if (current?.state !== "waiting" || current.available_at === null) return;
    const timer = setTimeout(() => void controller.open(current.assessment_id, false), Math.min(60_000, Math.max(1000, Date.parse(current.available_at) - Date.now())));
    return () => clearTimeout(timer);
  }, [controller, view.current]);
  return <AssessmentContent {...props} view={view} actions={{ onStart: (body) => void controller.start(body), onOpen: (id) => void controller.open(id),
    onAnswer: (parts) => void controller.answer(parts), onExplain: (text) => void controller.explain(text), onStop: () => void controller.action("stop"), onResume: () => void controller.action("resume"),
    onRefresh: () => { if (view.current === null) void controller.load(); else void controller.open(view.current.assessment_id, false); },
    onRetry: () => void controller.retry(), onList: () => controller.back(), onClose: props.onClose }} />;
}
