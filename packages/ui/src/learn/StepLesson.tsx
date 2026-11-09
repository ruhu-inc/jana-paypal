// The step-shape lesson (UXS-23, TABLE CUR-11b, D110; canvas artboard "L1 · Interactive lesson step"): problem first,
// an action on every screen. The header carries leave, the segmented progress (one segment per step and a labelled
// "Check"), read aloud and, where the tutor can answer, "Ask about this". The column, at most 640 px wide, names the
// lesson and what the step asks ("Try it first", "Choose", "Predict", "Calculate"), its lead-in of at most three
// sentences, then the question. One primary button moves the student on: Check, then Continue, or "Try again" after
// a miss. After an answer the student's answer shows with its UXS-16 state beside the step's two-line why and the
// item's checked working; a miss shows where to look and never the answer; after two misses the core shows the step
// ("Shown") and the lesson moves on, never counting against mastery (lesson answers record no attempt). The check's
// three questions follow one at a time, then the finished screen. Moving to a question puts focus on its heading.
import { useEffect, useRef, useState } from "react";
import type { CheckExplanation, CheckPart, LessonBlock, LessonPlayerView, LessonView, PartAnswer, PracticeItem } from "@jana/client-core";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { Button, Chip, IconButton, Notice, Spinner } from "../primitives/index.js";
import { Rendered } from "../registry/registry.js";
import { ReadAloud } from "../voice/SpeechTools.js";
import { ItemVisual, PartInput, answerOf, type Draft, type ItemPart } from "./items.js";
import { PackInline, PackText } from "./PackText.js";
import type { LessonActions } from "./LessonPlayer.js";

type StepBlock = Extract<LessonBlock, { type: "step" }>;
type Phase = "answering" | "checking" | "missed" | "done";

const MISSES = ["incorrect", "partially_correct"];
/** Misses before the step is shown, for a core that sends no explanation (before D110): the same rule as the core's. */
const MISSES_BEFORE_SHOWN = 2;

/** The marker's categories with their own "where to look" line (the check.why copy of UXS-16). */
const WHY: readonly string[] = ["unreadable", "value", "power_of_ten", "unit_error", "units_missing", "sig_figs", "misconception"];

/** One question of the lesson as the player walks it: a step's item, or one of the check's three. */
interface Question {
  block: number;
  item: PracticeItem;
  step: StepBlock | null;
  /** The question's place in the check (from 0), or null on a step. */
  inCheck: number | null;
  checkSize: number;
}

function questionsOf(lesson: LessonView): Question[] {
  const out: Question[] = [];
  lesson.blocks.forEach((block, i) => {
    if (block.type === "step") {
      const item = block.items[0];
      if (item !== undefined) out.push({ block: i, item, step: block, inCheck: null, checkSize: 0 });
    } else if (block.type === "check") {
      block.items.forEach((item, n) => out.push({ block: i, item, step: null, inCheck: n, checkSize: block.items.length }));
    }
  });
  return out;
}

/** The student's answer to one part as words: the option chosen, the value with its unit, or what was typed. */
function answerText(part: ItemPart, answer: PartAnswer | undefined): string {
  if (answer === undefined) return "";
  const input = part.input;
  if (answer.kind === "mcq" && input.kind === "mcq") return input.options.find((o) => o.id === answer.option_id)?.label ?? "";
  if (answer.kind === "multi_select" && input.kind === "multi_select") return input.options.filter((o) => answer.option_ids.includes(o.id)).map((o) => o.label).join(", ");
  if (answer.kind === "numeric") return [answer.value, answer.unit ?? ""].join(" ").trim();
  if (answer.kind === "algebraic") return answer.latex;
  if (answer.kind === "equation_solutions") return answer.values.join(", ");
  if (answer.kind === "chemical_equation") return answer.equation;
  if (answer.kind === "short_text") return answer.text;
  if (answer.kind === "ordering" && input.kind === "ordering") return answer.element_ids.map((id) => input.elements.find((e) => e.id === id)?.label ?? id).join(", ");
  return "";
}

/** The segmented progress: a segment per step, filled up to the one shown, and the labelled check (L2). */
function StepProgress({ steps, at, inCheck }: { steps: number; at: number; inCheck: { index: number; total: number } | null }) {
  const t = useT();
  const text = inCheck === null ? t("lesson.steps_progress", { index: at + 1, total: steps }) : t("lesson.check_progress", { index: inCheck.index + 1, total: inCheck.total });
  return (
    <span role="progressbar" aria-label={t("lesson.parts")} aria-valuemin={1} aria-valuemax={steps + 1} aria-valuenow={inCheck === null ? at + 1 : steps + 1} aria-valuetext={text}
      data-step-progress="" className="flex min-w-0 max-w-120 flex-1 items-center gap-1.5">
      {Array.from({ length: steps }, (_, i) => (
        <span key={i} data-segment={i < at || inCheck !== null ? "done" : i === at ? "current" : "ahead"} className={`h-1 min-w-3 flex-1 rounded-pill ${i <= at || inCheck !== null ? "bg-ink" : "bg-line"}`} />
      ))}
      <span aria-hidden="true" className={`ms-1.5 shrink-0 text-meta ${inCheck !== null ? "font-semibold text-ink" : "font-medium text-muted"}`}>{t("lesson.check_label")}</span>
    </span>
  );
}

/** One answered part: what the student gave, with its state in an icon and a word, never colour alone (UXS-16). */
function AnsweredPart({ part, answer, marked, shown }: { part: ItemPart; answer: PartAnswer | undefined; marked: CheckPart | undefined; shown: boolean }) {
  const t = useT();
  const verdict = marked?.verdict ?? "not_checked";
  const state = verdict === "correct" ? "checked" : verdict === "not_checked" ? "not_checked" : "look_here";
  const ring = state === "checked" ? "ring-[1.5px] ring-verified" : state === "look_here" ? "ring-[1.5px] ring-misconception" : "ring-1 ring-line-strong";
  return (
    <div className="flex flex-col gap-2" data-part-id={part.part_id} data-answer-state={state}>
      <p className="text-ui text-muted"><PackInline text={part.prompt} /></p>
      <div className={`flex min-h-13 items-center gap-3 rounded-card bg-canvas px-4 py-2 ${ring}`}>
        <span className="sr-only">{t("lesson.your_answer")}</span>
        <span className="min-w-0 flex-1 text-body text-ink"><PackInline text={answerText(part, answer)} /></span>
        {state === "checked" && <Chip tone="verified" icon="verified" size="sm">{t("lesson.state.checked")}</Chip>}
        {state === "look_here" && <Chip tone="look" icon="look-here" size="sm">{t(shown ? "lesson.state.shown" : "lesson.state.look_here")}</Chip>}
        {state === "not_checked" && <Chip size="sm">{t("lesson.state.not_checked")}</Chip>}
      </div>
    </div>
  );
}

/** Where to look after a miss: the marker's category line and the option's own feedback; never the answer. */
function MissNote({ parts }: { parts: CheckPart[] }) {
  const t = useT();
  const lines = parts.filter((p) => MISSES.includes(p.verdict)).flatMap((p) => {
    const known = WHY.includes(p.category ?? "") && !(p.category === "misconception" && p.misconception === null);
    return [p.category === null ? null : t(`check.why.${known ? p.category : "other"}`, { name: p.misconception?.name ?? "" }), p.feedback];
  }).filter((line, i, all): line is string => line !== null && all.indexOf(line) === i);
  return (
    <div data-miss-note="" className="flex flex-col gap-1.5 rounded-card bg-misconception-soft px-4 py-3 text-ui text-ink">
      <p className="font-semibold">{t("lesson.retry_heading")}</p>
      {lines.map((line) => <p key={line}><PackInline text={line} /></p>)}
    </div>
  );
}

/** The why beside the answer: the step's two lines, the item's checked working and the idea it names (L3). */
function WhyPanel({ explanation }: { explanation: CheckExplanation }) {
  const t = useT();
  return (
    <section aria-label={t("lesson.why")} data-why="" data-shown={explanation.shown ? "true" : undefined} className="flex flex-col gap-3 rounded-card border border-line bg-canvas px-5 py-4">
      {explanation.shown && <p className="flex items-center gap-2 text-meta text-muted"><Icon name="info" size={16} />{t("lesson.shown_note")}</p>}
      {explanation.why !== null && <PackText text={explanation.why} />}
      {explanation.working.length > 0 && (
        <ol className="flex flex-col items-center gap-2 py-1" aria-label={t("lesson.working")}>
          {explanation.working.map((line, i) => (
            <li key={i} data-check-id={line.check_id ?? undefined} className="flex flex-col items-center gap-0.5 text-center">
              <span className="board-math text-ink"><Tex tex={line.latex} /></span>
              <span className="flex items-center gap-1 text-meta text-muted">
                {line.check_id !== null && <span className="inline-flex text-verified"><Icon name="verified" size={16} /><span className="sr-only">{t("lesson.state.checked")}</span></span>}
                <PackInline text={line.reason} />
              </span>
            </li>
          ))}
        </ol>
      )}
      {explanation.names !== null && <div className="text-ui text-muted [&_strong]:text-ink"><PackText text={explanation.names} /></div>}
    </section>
  );
}

/** The lesson in the step shape, from the player's view. */
export function StepLessonContent({ view, lesson, actions, supported = false }: { view: LessonPlayerView; lesson: LessonView; actions: LessonActions; supported?: boolean }) {
  const t = useT();
  const questions = questionsOf(lesson);
  const steps = questions.filter((q) => q.step !== null).length;
  // The question shown: the first of the saved block, or the one the student moved to inside the check.
  const [at, setAt] = useState(() => Math.max(0, questions.findIndex((q) => q.block === view.index)));
  const [finished, setFinished] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, Record<string, Draft>>>({});
  // The try after which the student chose "Try again", by item: the question is open again until the next answer.
  const [retry, setRetry] = useState<Record<string, number>>({});
  const started = useRef<Record<string, number>>({});
  const heading = useRef<HTMLHeadingElement>(null);
  const done = useRef<HTMLParagraphElement>(null);
  const moved = useRef(false);
  const question = questions[Math.min(at, questions.length - 1)];

  useEffect(() => {
    if (!moved.current) return;
    (finished ? done.current : heading.current)?.focus();
  }, [at, finished]);
  // Once an answer is marked its field goes, so focus moves to what comes next (Try again or Continue), never lost to
  // the page; "Try again" puts it back in the answer field.
  const footer = useRef<HTMLElement>(null), main = useRef<HTMLElement>(null);
  const status = question === undefined ? undefined : view.checks[question.item.item_id]?.status;
  const lastStatus = useRef(status);
  useEffect(() => {
    if (lastStatus.current === "checking" && status === "marked") footer.current?.querySelector<HTMLButtonElement>("[data-step-next]")?.focus();
    lastStatus.current = status;
  }, [status]);
  const answerAgain = () => requestAnimationFrame(() => main.current?.querySelector<HTMLElement>("input, textarea, [contenteditable='true']")?.focus());

  if (question === undefined) return null;
  const { item } = question;
  const state = view.checks[item.item_id];
  const tries = state?.tries ?? 0;
  const result = state?.result ?? null;
  const explanation = result?.explanation ?? null;
  const missed = result !== null && MISSES.includes(result.verdict);
  const shown = missed && (explanation?.shown === true || (result.explanation === undefined && tries >= MISSES_BEFORE_SHOWN));
  const phase: Phase = state?.status === "checking" ? "checking"
    : state === undefined || state.status === "failed" || result === null || retry[item.item_id] === tries ? "answering"
    : missed && !shown ? "missed" : "done";
  const itemDrafts = drafts[item.item_id] ?? {};
  const answers = item.parts.map((p) => answerOf(p, itemDrafts[p.part_id] ?? {})).filter((a): a is PartAnswer => a !== null);
  const given = new Map((state?.answers ?? answers).map((a) => [a.part_id, a]));
  const marked = new Map((result?.parts ?? []).map((p) => [p.part_id, p]));
  started.current[item.item_id] ??= Date.now();

  const go = (next: number) => {
    moved.current = true;
    const target = questions[next];
    if (target === undefined) return;
    setAt(next);
    if (target.block !== view.index) actions.onGo(target.block);
  };
  const onContinue = () => {
    moved.current = true;
    if (at >= questions.length - 1) setFinished(true);
    else go(at + 1);
  };
  const onBack = () => {
    moved.current = true;
    if (finished) setFinished(false);
    else if (at > 0) go(at - 1);
  };
  const check = () => {
    const begun = started.current[item.item_id] ?? Date.now();
    started.current[item.item_id] = Date.now();
    actions.onAnswer(item.item_id, answers, Date.now() - begun);
  };

  const next = questions[at + 1];
  const action = question.step?.action;
  const eyebrow = question.step !== null && action !== undefined ? t(supported && action === "try_first" ? "journey.try_supported" : `lesson.action.${action}`) : t("lesson.check_question", { index: (question.inCheck ?? 0) + 1, total: question.checkSize });
  const read = question.step !== null
    ? { kind: "lesson_block" as const, lesson_id: lesson.lesson_id, version: lesson.version, block_index: question.block }
    : { kind: "lesson_question" as const, lesson_id: lesson.lesson_id, version: lesson.version, block_index: question.block, item_id: item.item_id };

  return (
    <div className="flex min-h-full flex-col" data-lesson-status={view.status} data-lesson-id={lesson.lesson_id} data-lesson-shape="steps" data-block-index={question.block}>
      <ScreenHeader end={<>
        {!finished && <ReadAloud source={read} />}
        {actions.onAsk !== undefined && !finished && (
          <IconButton icon="ask" label={t("lesson.ask")} onClick={() => actions.onAsk?.({ lessonId: lesson.lesson_id, blockIndex: question.block, blockType: lesson.blocks[question.block]?.type ?? "step" })} />
        )}
      </>}>
        {actions.onClose !== undefined && <span className="-ms-3 shrink-0"><IconButton icon="dismiss" label={t("lesson.leave")} onClick={actions.onClose} /></span>}
        <StepProgress steps={steps} at={question.step !== null ? questions.slice(0, at + 1).filter((q) => q.step !== null).length - 1 : steps} inCheck={question.inCheck === null ? null : { index: question.inCheck, total: question.checkSize }} />
      </ScreenHeader>
      {/* Enter in an answer field checks it, as the footer's Check does (keyboard use, UXS-21). */}
      <main ref={main} className="mx-auto flex w-full max-w-160 flex-1 flex-col gap-6 px-6 pt-8 pb-6" onKeyDown={(event) => {
        if (event.key !== "Enter" || event.defaultPrevented || event.shiftKey || !(event.target instanceof HTMLInputElement)) return;
        if (finished || phase !== "answering" || answers.length === 0) return;
        event.preventDefault();
        check();
      }}>
        {view.positionFailed && <Notice tone="warn" message={t("lesson.position_failed")} />}
        {finished ? (
          <section role="status" className="flex flex-col items-start gap-4 rounded-card border border-line bg-canvas p-6" data-lesson-finished>
            <p ref={done} tabIndex={-1} className="flex items-center gap-2 text-body text-ink">
              <span className="inline-flex text-verified"><Icon name="verified" /></span>
              {t("lesson.finished")}
            </p>
            {actions.onPractise !== undefined && <Button tone="primary" onClick={actions.onPractise}>{t("lesson.practise")}</Button>}
            {actions.onClose !== undefined && <Button onClick={actions.onClose}>{t("lesson.back_to_map")}</Button>}
          </section>
        ) : (
          <section aria-labelledby="lesson-question" className="flex flex-col gap-5" data-block-type={question.step !== null ? "step" : "check"} data-item-id={item.item_id} data-phase={phase}>
            <p className="text-meta font-medium text-muted">{lesson.title} · {eyebrow}</p>
            {question.step?.text != null && <div className="text-muted"><PackText text={supported && action === "try_first" ? t("journey.apply_example") : question.step.text} /></div>}
            {question.step?.visual != null && "visual_type" in question.step.visual && typeof question.step.visual.visual_type === "string" && (
              <div className="rounded-card border border-line bg-canvas p-4">
                <Rendered keys={[`visual:${question.step.visual.resolved?.type ?? question.step.visual.visual_type}`, "visual:*"]} data={question.step.visual.resolved ?? question.step.visual.params} lifecycle="complete" partId={`lesson-visual-${question.block}`} />
              </div>
            )}
            <h1 id="lesson-question" ref={heading} tabIndex={-1} className="text-title text-ink"><PackInline text={item.stem} /></h1>
            <ItemVisual item={item} />
            {phase === "answering" || phase === "checking" ? (
              item.parts.map((part) => (
                <div key={part.part_id} data-part-id={part.part_id}>
                  <PartInput part={part} itemId={item.item_id} draft={itemDrafts[part.part_id] ?? {}}
                    onDraft={(d) => setDrafts((all) => ({ ...all, [item.item_id]: { ...(all[item.item_id] ?? {}), [part.part_id]: d } }))} />
                </div>
              ))
            ) : (
              <div className="flex flex-col gap-4" role="status">
                {item.parts.map((part) => <AnsweredPart key={part.part_id} part={part} answer={given.get(part.part_id)} marked={marked.get(part.part_id)} shown={shown} />)}
                {phase === "missed" && result !== null && <MissNote parts={result.parts} />}
                {phase === "done" && explanation !== null && <WhyPanel explanation={explanation} />}
                {phase === "done" && shown && explanation === null && <p className="text-ui text-muted">{t("lesson.shown_note")}</p>}
              </div>
            )}
            {state?.status === "failed" && <Notice tone="warn" message={t("check.failed")} />}
            {phase === "done" && next !== undefined && (
              <div className="flex flex-col gap-1.5" data-next-up="">
                <p className="text-meta font-medium text-muted">{next.step !== null ? t(`lesson.next_action.${next.step.action}`) : t("lesson.next_check", { total: next.checkSize })}</p>
                {next.step !== null && <p className="text-ui text-muted"><PackInline text={next.item.stem} /></p>}
              </div>
            )}
          </section>
        )}
      </main>
      <footer ref={footer} className="mx-auto flex w-full max-w-160 items-center gap-3 border-t border-line px-6 pt-4 pb-7">
        <Button tone="ghost" disabled={at === 0 && !finished} onClick={onBack}>
          <Icon name="go-back" size={16} />
          {t("lesson.back")}
        </Button>
        <span className="flex-1" />
        {phase === "checking" && !finished && (
          <span role="status" className="inline-flex items-center gap-2 text-meta text-muted"><Spinner />{t("check.checking")}</span>
        )}
        {!finished && (phase === "answering" || phase === "checking") && (
          <Button tone="primary" disabled={answers.length === 0 || phase === "checking"} onClick={check}>{t("lesson.check")}</Button>
        )}
        {!finished && phase === "missed" && (
          <Button tone="primary" data-step-next="" onClick={() => { setRetry((r) => ({ ...r, [item.item_id]: tries })); moved.current = true; answerAgain(); }}>{t("lesson.try_again")}</Button>
        )}
        {!finished && phase === "done" && (
          <Button tone="primary" data-step-next="" onClick={onContinue}>
            {t(at >= questions.length - 1 ? "lesson.finish" : "lesson.continue")}
            {at < questions.length - 1 && <Icon name="go-next" size={16} />}
          </Button>
        )}
      </footer>
    </div>
  );
}
