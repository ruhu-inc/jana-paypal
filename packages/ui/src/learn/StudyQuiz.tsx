// A Study quiz as a practice screen (STU-21; the 2026-11-01 crowding review): one question at a time with one primary
// action, "Need help?" for the hints and the question's own coaching, a Questions menu for the rest, Finish quiz at
// the last question, and the results as their own view. ASM-28 holds throughout: a first wrong answer never shows the
// answer; its explanation comes after the second attempt or when the learner asks for it. The draft (answers and the
// question the learner was on) is saved as before, so a reopened quiz resumes where it was left.
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { quizHelpItem, type StudyContracts as W, type StudyWorkspaceView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Disclosure, FIELD_LOOK, IconButton, Loader, MenuButton } from "../primitives/index.js";
import { ComposerShell } from "../primitives/ComposerShell.js";
import { HelpChips } from "../chips/NextStepChips.js";
import { StudyInline as PackInline } from "./StudyText.js";
import { useAutoGrow } from "../primitives/autogrow.js";

type Verdict = W.QuizResult["results"][number];
/** The two kinds of help a Study quiz offers (never "explain simply", which would name the concept being tested). */
export const STUDY_HELP = ["hint", "walk_through"] as const;
export type StudyHelpKind = (typeof STUDY_HELP)[number];
/** A multi-select answer is its chosen option ids, comma-separated (STU-21). */
const picked = (answer: string | undefined) => (answer ?? "").split(",").filter(Boolean);
const missed = (r: Verdict | undefined) => r !== undefined && (r.verdict === "incorrect" || r.verdict === "partial");

export function StudyQuiz({ quiz, result, resultAnswers, feedback, busy, accessBlocked, stale, quizHelp, helpJob, helpPending, safeHelp, helpAids, claim, jobs, onDraft, onCheck, onSubmit, onClearResult, onRetake, ownCourse = false, checkedAnswers, onHelp, onFollowUp }: {
  quiz: W.QuizView; result: W.QuizResult | null; resultAnswers: Record<string, string>; feedback: Record<string, Verdict>;
  busy: boolean; accessBlocked: boolean; stale: boolean;
  quizHelp: StudyWorkspaceView["quizHelp"]; helpJob: W.Job | undefined; helpPending: boolean; safeHelp: boolean;
  /** Chat aids that were coaching for this quiz's questions: shown beside their question, never in the chat's history. */
  helpAids: readonly W.Aid[];
  claim: (c: W.Claim, key?: string) => ReactNode; jobs: (list: W.Job[], compact?: boolean) => ReactNode;
  onDraft: (answers: Record<string, string>, position: number) => void;
  /** Check one answer; resolves true when the check went through (the feedback is in `feedback`). */
  onCheck: (itemId: string, value: string) => Promise<boolean>;
  onSubmit: (answers: Record<string, string>) => void; onClearResult: () => void; onRetake?: () => void; ownCourse?: boolean; checkedAnswers?: Record<string, string>;
  onHelp: (kind: StudyHelpKind, itemId: string) => void;
  /** A follow-up to the coaching shown for a question; resolves true when it was sent. */
  onFollowUp: (itemId: string, kind: StudyHelpKind, text: string) => Promise<boolean>;
}) {
  const t = useT();
  const total = quiz.items.length;
  const [answers, setAnswers] = useState<Record<string, string>>(() => result ? resultAnswers : Object.fromEntries((quiz.draft?.answers ?? []).map((a) => [a.item_id, a.answer])));
  const [position, setPosition] = useState(Math.min(quiz.draft?.position ?? 0, total - 1));
  const [allQuestions, setAllQuestions] = useState(false);
  const [tries, setTries] = useState<Record<string, number>>({});
  const [checked, setChecked] = useState<Record<string, string>>(() => checkedAnswers ?? {});
  const [round, setRound] = useState(0), [shownAnswers, setShownAnswers] = useState<string[]>([]);
  const [helpOpen, setHelpOpen] = useState(false), [followUp, setFollowUp] = useState("");
  const helpId = useId(), helpPanel = useRef<HTMLElement>(null), followUpField = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(followUpField, followUp);
  useEffect(() => { if (result) setRound((r) => r + 1); }, [result?.attempt_id]);
  // Coaching that arrives for the open question opens the help panel and takes focus there.
  useEffect(() => {
    if (!quizHelp) return;
    setHelpOpen(true);
    requestAnimationFrame(() => { helpPanel.current?.focus({ preventScroll: true }); helpPanel.current?.scrollIntoView?.({ block: "nearest" }); });
  }, [quizHelp?.jobId, quizHelp?.aid?.aid_id]);
  const answer = (id: string, value: string, at: number, save = true) => {
    const next = { ...answers, [id]: value };
    setAnswers(next);
    if (save) onDraft(next, at);
  };
  const move = (at: number) => { setPosition(at); setHelpOpen(false); setFollowUp(""); onDraft(answers, at); };
  const check = (id: string) => { const value = answers[id] ?? ""; void onCheck(id, value).then((ok) => {
    if (ok) { setTries((now) => ({ ...now, [id]: (now[id] ?? 0) + 1 })); setChecked((now) => ({ ...now, [id]: value })); }
    onDraft(answers, position);
  }); };
  const finish = () => onSubmit(answers);
  // Enter in an answer checks that answer, as the primary button does; only Finish quiz submits the whole quiz.
  const enterChecks = (e: KeyboardEvent<HTMLFormElement>) => {
    if (e.key !== "Enter" || !(e.target instanceof HTMLInputElement)) return;
    e.preventDefault();
    const at = e.target.closest<HTMLElement>("[data-item]")?.dataset.item;
    const id = at === undefined ? undefined : quiz.items[Number(at)]?.item_id;
    if (id !== undefined && !busy && quiz.status === "ready" && answers[id]?.trim() && checked[id] !== answers[id]) check(id);
  };
  const locked = busy || !!result || quiz.status !== "ready";

  const inputs = (item: W.QuizItem, i: number) => item.type === "choice" ? item.options?.map((option) => <label key={option.option_id} className="flex min-h-11 items-center gap-3 rounded-control border border-line px-4 py-2 text-ui text-ink has-checked:border-ink"><input disabled={locked} type="radio" name={item.item_id} value={option.option_id} checked={answers[item.item_id] === option.option_id} onChange={() => answer(item.item_id, option.option_id, i)} /><span><PackInline text={option.text} /></span></label>)
    : item.type === "multi_select" ? item.options?.map((option) => { const chosen = picked(answers[item.item_id]); return <label key={option.option_id} className="flex min-h-11 items-center gap-3 rounded-control border border-line px-4 py-2 text-ui text-ink has-checked:border-ink"><input disabled={locked} type="checkbox" checked={chosen.includes(option.option_id)} onChange={() => answer(item.item_id, (chosen.includes(option.option_id) ? chosen.filter((x) => x !== option.option_id) : [...chosen, option.option_id]).join(","), i)} /><span><PackInline text={option.text} /></span></label>; })
    : item.type === "true_false" ? (["true", "false"] as const).map((value) => <label key={value} className="flex min-h-11 items-center gap-3 rounded-control border border-line px-4 py-2 text-ui text-ink has-checked:border-ink"><input disabled={locked} type="radio" name={item.item_id} value={value} checked={answers[item.item_id] === value} onChange={() => answer(item.item_id, value, i)} />{t(`study.${value}`)}</label>)
    : <label className="flex flex-col gap-2 text-ui">{t(item.type === "fill_blank" ? "study.fill_blank" : "study.answer")}<input disabled={locked} className={FIELD_LOOK} value={answers[item.item_id] ?? ""} onChange={(e) => answer(item.item_id, e.target.value, i, false)} onBlur={() => onDraft(answers, i)} /></label>;
  const question = (item: W.QuizItem, i: number, heading: boolean) => <fieldset data-item={i} className="flex flex-col gap-3">
    <legend className={heading ? "text-ui font-semibold text-ink" : "text-meta font-medium text-muted"}>{t("study.question_of", { number: i + 1, total })}{item.type === "multi_select" ? ` · ${t("study.choose_all")}` : ""}</legend>
    {claim(item.stem)}
    {inputs(item, i)}
  </fieldset>;
  // Feedback for a checked answer (ASM-28): the explanation only after the second try or on request.
  const feedbackFor = (item: W.QuizItem) => {
    const fb = feedback[item.item_id];
    if (!fb || checked[item.item_id] === undefined || checked[item.item_id] !== answers[item.item_id]) return null;
    const open = !missed(fb) || (tries[item.item_id] ?? 0) >= 2 || shownAnswers.includes(item.item_id);
    return <section aria-label={t("study.question_feedback")} className="flex flex-col gap-2 rounded-card bg-panel px-4 py-3" aria-live="polite">
      <p className="text-ui font-semibold">{t(`study.verdict.${fb.verdict}`)}</p>
      {"explanation" in fb && (open ? <><p className="text-meta text-muted">{fb.label}</p>{claim(fb.explanation)}</> : <><p className="text-ui">{t("study.not_yet")}</p><span><Button size="sm" tone="ghost" onClick={() => setShownAnswers((now) => [...now, item.item_id])}>{t("study.show_answer")}</Button></span></>)}
    </section>;
  };
  const earlierHelp = (item: W.QuizItem) => helpAids.filter((aid) => aid.status === "ready" && quizHelpItem(aid.prompt, quiz)?.item_id === item.item_id && aid.aid_id !== quizHelp?.aid?.aid_id);
  // Need help?: the two hint chips, the coaching already given for this question, and a follow-up to it.
  const helpPanelFor = (item: W.QuizItem) => {
    const here = quizHelp?.quizId === quiz.quiz_id && quizHelp.itemId === item.item_id ? quizHelp : null;
    const earlier = earlierHelp(item);
    return <div id={helpId} hidden={!helpOpen} className="flex flex-col gap-3 rounded-card border border-line p-4">
      {!here && <HelpChips kinds={STUDY_HELP} disabled={busy || accessBlocked || helpPending || quiz.status !== "ready"} onHelp={(kind) => onHelp(kind as StudyHelpKind, item.item_id)} />}
      {here && <section ref={helpPanel} tabIndex={-1} aria-label={t("study.quiz_help")} className="flex flex-col gap-3 outline-none" aria-live="polite">
        <h3 className="text-ui font-semibold">{t(`help.chip.${here.kind}`)}</h3>
        {here.aid ? here.aid.claims.map((c, i) => claim(c, String(i))) : safeHelp && helpJob ? <>
          <p className="text-meta text-muted">{t("study.quiz_help_general")}</p>
          <p className="text-ui">{t(item.type === "choice" || item.type === "multi_select" ? (here.kind === "hint" ? "study.quiz_help_safe_hint" : "study.quiz_help_safe_walk") : (here.kind === "hint" ? "study.quiz_help_safe_hint_written" : "study.quiz_help_safe_walk_written"))}</p>
          {item.stem.citations.length > 0 && claim({ text: t("study.quiz_help_read_source"), citations: item.stem.citations }, "safe-help-source")}
          <p className="text-ui">{t("study.quiz_help_safe_followup")}</p>
          <details><summary className="cursor-pointer text-meta text-muted">{t("study.quiz_help_tailored")}</summary>{jobs([helpJob], true)}</details>
        </> : helpJob && ["failed", "paused", "waiting_online", "cancelled"].includes(helpJob.state) ? jobs([helpJob], true) : <Loader label={t("study.quiz_help_loading")} />}
        {(here.aid || safeHelp) && <>
          <HelpChips kinds={STUDY_HELP.filter((kind) => kind !== here.kind)} disabled={busy || accessBlocked || helpPending || quiz.status !== "ready"} onHelp={(kind) => onHelp(kind as StudyHelpKind, item.item_id)} />
          <form onSubmit={(e) => { e.preventDefault(); e.stopPropagation(); if (!followUp.trim()) return; void onFollowUp(item.item_id, here.kind, followUp).then((ok) => { if (ok) setFollowUp(""); }); }}>
            <ComposerShell send={<IconButton type="submit" tone="action" icon="send" tip="above" label={t("study.help_follow_up_send")} disabled={busy || helpPending || accessBlocked || !followUp.trim()} />}>
              <label htmlFor={`${helpId}-follow-up`} className="sr-only">{t("study.help_follow_up")}</label>
              <textarea ref={followUpField} id={`${helpId}-follow-up`} rows={1} maxLength={1000} placeholder={t("study.help_follow_up")} value={followUp} onChange={(e) => setFollowUp(e.target.value)} className="w-full resize-none bg-transparent px-1 text-body text-ink outline-none placeholder:text-placeholder" />
            </ComposerShell>
          </form>
        </>}
      </section>}
      {earlier.length > 0 && <QuietDisclosure summary={t("study.earlier_help", { count: earlier.length })}><div className="flex flex-col gap-3 pt-2" data-earlier-help>{earlier.map((aid) => <div key={aid.aid_id} className="flex flex-col gap-2">{aid.claims.map((c, i) => claim(c, String(i)))}</div>)}</div></QuietDisclosure>}
    </div>;
  };

  // --- the results, their own view ---------------------------------------------------------------------------------
  if (result) {
    const verdicts = new Map(result.results.map((r) => [r.item_id, r]));
    const correct = result.results.filter((r) => r.verdict === "correct").length;
    const labels = [...new Set(result.results.flatMap((r) => "label" in r ? [r.label] : []))];
    const order = [...quiz.items.entries()].sort(([, a], [, b]) => Number(missed(verdicts.get(b.item_id))) - Number(missed(verdicts.get(a.item_id))));
    const retry = round < 2 && result.results.some((r) => missed(r) && !shownAnswers.includes(r.item_id));
    return <section className="flex flex-col gap-5" aria-label={t("study.results")} data-quiz-results>
      <header className="flex flex-col gap-1">
        <p role="status" className="text-heading text-ink" data-quiz-score>{t("study.results_score", { correct, total })}</p>
        <p className="text-meta text-muted">{t(ownCourse ? "study.no_mastery_material" : "study.no_mastery")}</p>
        {labels.length === 1 && <p className="text-meta text-muted" data-quiz-grading>{labels[0]}</p>}
        <details className="text-meta text-muted"><summary className="cursor-pointer">{t("study.grading_details")}</summary><p className="mt-2">{t("study.grading_help")}</p></details>
      </header>
      <span className="flex flex-wrap gap-2">
        {retry && <Button tone="primary" disabled={busy} onClick={onClearResult}>{t("study.try_again")}</Button>}
        {onRetake && <Button tone={retry ? "secondary" : "primary"} disabled={busy} onClick={() => { setAnswers({}); setPosition(0); setChecked({}); setTries({}); setRound(0); setShownAnswers([]); setHelpOpen(false); onRetake(); }}>{t("study.retake")}</Button>}
      </span>
      <ol className="flex flex-col gap-4">{order.map(([i, item]) => {
        const r = verdicts.get(item.item_id);
        const wrong = missed(r);
        const open = !wrong || round >= 2 || shownAnswers.includes(item.item_id);
        return <li key={item.item_id} className="flex flex-col gap-3 rounded-card border border-line p-4" data-result-item={r?.verdict ?? "skipped"}>
          <p className="text-ui font-semibold">{t(`study.verdict.${r?.verdict ?? "skipped"}`)}{labels.length > 1 && r && "label" in r ? <span className="text-meta font-normal text-muted"> · {r.label}</span> : null}</p>
          {question(item, i, false)}
          {r && "explanation" in r && (wrong
            ? open ? claim(r.explanation) : <><p className="text-ui">{t("study.not_yet")}</p><HelpChips kinds={STUDY_HELP} disabled={busy || accessBlocked || helpPending} onHelp={(kind) => onHelp(kind as StudyHelpKind, item.item_id)} /><span><Button size="sm" tone="ghost" onClick={() => setShownAnswers((now) => [...now, item.item_id])}>{t("study.show_answer")}</Button></span></>
            : <QuietDisclosure summary={t("study.explanation")}>{claim(r.explanation)}</QuietDisclosure>)}
        </li>;
      })}</ol>
    </section>;
  }

  // --- answering ---------------------------------------------------------------------------------------------------
  const current = quiz.items[position] ?? quiz.items[0];
  if (!current) return null;
  const last = position >= total - 1;
  const state = (item: W.QuizItem) => checked[item.item_id] !== undefined && checked[item.item_id] === answers[item.item_id] ? "checked" : (answers[item.item_id] ?? "").trim() ? "answered" : "open";
  const navigator = <MenuButton label={t("study.quiz_navigation")} align="end" trigger={(props) => <Button size="sm" tone="ghost" {...props}><Icon name="table" size={16} />{t("study.questions_menu")}</Button>}
    items={[
      ...quiz.items.map((item, i) => ({ id: item.item_id, label: t("study.question_of", { number: i + 1, total }), hint: t(`study.question_state.${state(item)}`), checked: !allQuestions && i === position, onSelect: () => { setAllQuestions(false); move(i); } })),
      { id: "all", group: "view", label: t("study.show_all_questions"), checked: allQuestions, onSelect: () => setAllQuestions((v) => !v) },
      ...(last && !allQuestions ? [] : [{ id: "finish", group: "finish", label: t("study.finish_now"), onSelect: finish }]),
    ]} />;
  const checkedNow = checked[current.item_id] !== undefined && checked[current.item_id] === answers[current.item_id] && !!feedback[current.item_id];
  return <form className="flex flex-col gap-5" data-study-quiz data-stale={stale ? "" : undefined} onKeyDown={enterChecks} onSubmit={(e) => { e.preventDefault(); finish(); }}>
    {allQuestions ? <>
      {quiz.items.map((item, i) => <div key={item.item_id} className="flex flex-col gap-3">{question(item, i, true)}
        <span><Button size="sm" disabled={busy || !answers[item.item_id]?.trim() || quiz.status !== "ready"} onClick={() => check(item.item_id)}>{t("study.check_answer")}</Button></span>
        {feedbackFor(item)}
      </div>)}
      <div className="flex flex-wrap items-center gap-2">{navigator}<span className="flex-1" /><Button type="submit" tone="primary" disabled={busy || quiz.status !== "ready"}>{t("study.submit")}</Button></div>
    </> : <>
      <div className="flex items-start justify-between gap-2">{question(current, position, true)}</div>
      {feedbackFor(current)}
      {/* Each button keeps its own element: one whose type changed to submit during its own click would finish the quiz. */}
      <div className="flex flex-wrap items-center gap-2">
        {checkedNow
          ? last ? <Button key="finish" type="submit" tone="primary" disabled={busy || quiz.status !== "ready"}>{t("study.submit")}</Button> : <Button key="next" tone="primary" disabled={busy} onClick={() => move(position + 1)}>{t("study.next_question")}</Button>
          : <Button key="check" tone="primary" disabled={busy || !answers[current.item_id]?.trim() || quiz.status !== "ready"} onClick={() => check(current.item_id)}>{t("study.check_answer")}</Button>}
        <Button size="sm" tone="ghost" aria-expanded={helpOpen} aria-controls={helpId} disabled={quiz.status !== "ready"} onClick={() => setHelpOpen((o) => !o)}><Icon name="hint" size={16} />{t("study.need_help")}</Button>
      </div>
      {helpPanelFor(current)}
      <nav aria-label={t("study.quiz_navigation")} className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <Button size="sm" tone="ghost" disabled={busy || position === 0} onClick={() => move(position - 1)}>{t("study.previous_question")}</Button>
        {!checkedNow && (last
          ? <Button key="finish" size="sm" tone="ghost" type="submit" disabled={busy || quiz.status !== "ready"}>{t("study.submit")}</Button>
          : <Button key="next" size="sm" tone="ghost" disabled={busy} onClick={() => move(position + 1)}>{t("study.next_question")}</Button>)}
        <span className="flex-1" />
        {navigator}
      </nav>
    </>}
  </form>;
}

/** A closed-by-default disclosure for a correct answer's explanation. */
function QuietDisclosure({ summary, children }: { summary: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <Disclosure summary={summary} open={open} onToggle={() => setOpen((o) => !o)}>{children}</Disclosure>;
}
