// The lesson reader (UXS-23, CUR-11, LRN-17 subset, UXS-16; direction A "Studio"): a pack lesson one part at a time in
// script order (before you start, the idea, the worked example on the board, your turn with help, quick checks, common
// mistakes, the check of what was learnt, the summary). The 56 px header carries Back to the map, the lesson's progress
// ("Part 2 of 8"), read aloud and "Ask about this"; the reading column, at most 768 px wide, names the lesson in title
// type with the parts to jump between, then the part itself; Back and Next (Finish on the last part) sit under it. The
// position is saved as the student moves. It plays with no model and no network: everything comes resolved in the
// lesson, checks are marked by the core's deterministic marker, and "Ask about this" is offered only when the tutor can
// answer (no handler on the content-only class, PLT-29). Feedback follows the UXS-16 words: an icon and a word for every
// outcome, never colour alone, and never the word "wrong". Moving to another part puts focus on its heading; "Finish"
// puts focus on the finished message, which is also announced, and going back from it returns focus to the part's
// heading. A step-shape lesson (TABLE CUR-11b, D110) plays in `StepLessonContent` instead: problem first, an action on
// every screen; script-shape lessons play here as before.
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { isStepLesson } from "@jana/client-core";
import type {
  CheckBlockType, CheckState, LessonBlock, LessonController, LessonPlayerView, PartAnswer, PracticeItem, StepRec, WorkedExampleView,
} from "@jana/client-core";
import { ReadAloud } from "../voice/SpeechTools.js";
import { BoardStep } from "../board/Board.js";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, Callout, EmptyState, ErrorState, IconButton, Notice, ProgressBar, Skeleton, Spinner } from "../primitives/index.js";
import { Tex } from "../maths/Tex.js";
import { Rendered } from "../registry/registry.js";
import { Feedback, ItemVisual, PartInput, answerOf, type Draft } from "./items.js";
import { PackText } from "./PackText.js";
import { StepLessonContent } from "./StepLesson.js";

export interface AskContext {
  lessonId: string;
  blockIndex: number;
  blockType: LessonBlock["type"];
}

export interface LessonActions {
  onGo: (index: number) => void;
  onTeachingGo?: (index: number) => void;
  onAnswer: (itemId: string, parts: PartAnswer[], timeMs: number) => void;
  onClose?: () => void;
  onPractise?: () => void;
  /** "Ask about this": absent when no tutor can answer (content-only, PLT-29). */
  onAsk?: (context: AskContext) => void;
  onRetry?: () => void;
}

const CHECK_BLOCKS: readonly string[] = ["prior_knowledge", "guided_step", "quick_check", "exit_check"];

function CheckItem({ item, ordinal, state, onAnswer, readAloud }: { item: PracticeItem; ordinal: number; state: CheckState | undefined; onAnswer: LessonActions["onAnswer"]; readAloud?: ReactNode }) {
  const t = useT();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [started] = useState(() => Date.now());
  const answers = item.parts.map((p) => answerOf(p, drafts[p.part_id] ?? {})).filter((a): a is PartAnswer => a !== null);
  const results = new Map((state?.result?.parts ?? []).map((p) => [p.part_id, p]));
  const solved = state?.status === "marked" && state.result !== null && state.result.marks_total > 0 && state.result.marks_awarded >= state.result.marks_total;
  return (
    <article className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-5" data-item-id={item.item_id} aria-labelledby={`${item.item_id}-q`}>
      <h3 id={`${item.item_id}-q`} className="flex items-center justify-between gap-2 text-label text-faint">
        <span>{t("check.question", { index: ordinal })}</span>
        <span className="flex items-center gap-1">
          {item.marks !== undefined && <span>{t("check.marks", { marks: item.marks })}</span>}
          {readAloud}
        </span>
      </h3>
      <PackText text={item.stem} /><ItemVisual item={item} />
      {item.parts.map((part) => {
        const marked = results.get(part.part_id);
        return (
          <div key={part.part_id} className="flex flex-col gap-3" data-part-id={part.part_id}>
            <PartInput part={part} itemId={item.item_id} draft={drafts[part.part_id] ?? {}} onDraft={(d) => setDrafts((all) => ({ ...all, [part.part_id]: d }))} />
            <div aria-live="polite">{marked !== undefined && <Feedback part={marked} />}</div>
          </div>
        );
      })}
      <div className="flex flex-wrap items-center gap-3">
        {/* Full marks ends the question: no Check button left asking to be pressed again. */}
        {!solved && <Button disabled={answers.length === 0 || state?.status === "checking"} onClick={() => onAnswer(item.item_id, answers, Date.now() - started)}>
          {t("check.submit")}
        </Button>}
        {state?.status === "checking" && (
          <span role="status" className="inline-flex items-center gap-2 text-meta text-muted">
            <Spinner />
            {t("check.checking")}
          </span>
        )}
      </div>
      {state?.status === "failed" && <Notice tone="warn" message={t("check.failed")} />}
    </article>
  );
}

/** The worked example on the board: each checked step with its reason; a faded step is a blank until shown. */
export function ExampleBoard({ example }: { example: WorkedExampleView }) {
  const t = useT();
  const [shown, setShown] = useState<number[]>([]);
  return (
    <div className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-5">
      <div className="flex flex-col gap-1">
        <h3 className="text-label text-faint">{t("lesson.example.problem")}</h3>
        <PackText text={example.problem} />
      </div>
      {example.visual != null && (
        <Rendered keys={[`visual:${example.visual.type}`, "visual:*"]} data={example.visual} lifecycle="complete" partId={`example-visual-${example.worked_example_id}`} />
      )}
      <ol className="flex flex-col gap-1" aria-label={t("lesson.block.worked_example")}>
        {example.steps.map((step, i) => {
          if (example.faded_steps.includes(step.n) && !shown.includes(step.n)) {
            return (
              <li key={step.n} data-faded={step.n} className="grid grid-cols-[2rem_1fr] items-center gap-3 rounded-card border border-dashed border-line-strong py-3 ps-3 pe-4">
                <span className="text-center text-meta text-faint tabular-nums">{i + 1}</span>
                <span className="flex flex-wrap items-center justify-between gap-3">
                  <span className="text-ui text-muted">{t("lesson.example.blank", { n: step.n })}</span>
                  <Button size="sm" onClick={() => setShown((s) => [...s, step.n])}>{t("lesson.example.show", { n: step.n })}</Button>
                </span>
              </li>
            );
          }
          const rec: StepRec = { step_id: `${example.worked_example_id}-${step.n}`, index: i, latex: step.latex, reason: step.reason, owner: "tutor", status: "final", check_id: step.check_id, error: null };
          return <BoardStep key={step.n} step={rec} number={i + 1} current={false} />;
        })}
      </ol>
      <p className="flex flex-wrap items-center gap-2 border-t border-line pt-4 text-ui text-ink" data-check-id={example.final_answer.check_id}>
        <span className="font-semibold">{t("lesson.example.answer")}</span>
        <span className="inline-flex items-center gap-1.5 rounded-chip bg-verified-soft px-3 py-1 text-verified">
          <Icon name="verified" size={16} />
          <span className="board-math text-ink"><Tex tex={example.final_answer.latex} /></span>
        </span>
      </p>
    </div>
  );
}

function Visual({ visual, index }: { visual: NonNullable<Extract<LessonBlock, { type: "idea" }>["visual"]>; index: number }) {
  const t = useT();
  if ("visual_type" in visual && typeof visual.visual_type === "string") {
    // The pack build resolved and checked the figure; a record built before that shows its words fallback.
    return (
      <div className="rounded-card border border-line bg-canvas p-4">
        <Rendered keys={[`visual:${visual.resolved?.type ?? visual.visual_type}`, "visual:*"]} data={visual.resolved ?? visual.params} lifecycle="complete" partId={`lesson-visual-${index}`} />
      </div>
    );
  }
  return <Notice message={t("lesson.visual_unavailable")} />;
}

function Block({ block, index, view, actions }: { block: LessonBlock; index: number; view: LessonPlayerView; actions: LessonActions }): ReactNode {
  const t = useT();
  if (CHECK_BLOCKS.includes(block.type)) {
    const items = (block as { type: CheckBlockType; items: PracticeItem[] }).items;
    if (items.length === 0) return <EmptyState message={t("lesson.no_items")} />;
    return (
      <div className="flex flex-col gap-4">
        {items.map((item, i) => (
          <CheckItem key={item.item_id} item={item} ordinal={i + 1} state={view.checks[item.item_id]} onAnswer={actions.onAnswer}
            readAloud={view.lesson !== null ? <ReadAloud source={{ kind: "lesson_question", lesson_id: view.lesson.lesson_id, version: view.lesson.version, block_index: index, item_id: item.item_id }} /> : undefined} />
        ))}
      </div>
    );
  }
  switch (block.type) {
    case "idea":
      return (
        <div className="flex max-w-(--reading-measure) flex-col gap-4">
          <PackText text={block.text} />
          {block.visual !== null && <Visual visual={block.visual} index={index} />}
        </div>
      );
    case "worked_example":
      return <ExampleBoard example={block.worked_example} />;
    case "common_mistakes":
      return (
        <div className="flex flex-col gap-4">
          {block.text !== null && <PackText text={block.text} />}
          <ul className="flex flex-col gap-3">
            {block.mistakes.map((m) => (
              <li key={m.misconception_id} data-misconception-id={m.misconception_id}>
                <Callout tone="look" heading={m.name}>
                  <span className="flex flex-col gap-1">
                    <span className="text-meta text-muted">{t("lesson.mistake.fix")}</span>
                    <PackText text={m.fix} />
                  </span>
                </Callout>
              </li>
            ))}
          </ul>
        </div>
      );
    case "visual":
      return (
        <figure className="flex flex-col gap-2">
          <Visual visual={block.visual} index={index} />
          {block.caption !== null && <figcaption className="text-meta text-muted">{block.caption}</figcaption>}
        </figure>
      );
    case "summary":
      return <div className="max-w-(--reading-measure)"><PackText text={block.text} /></div>;
    default:
      return null;
  }
}

function Loading() {
  const t = useT();
  return (
    <div role="status" aria-busy="true" className="mx-auto flex w-full max-w-192 flex-col gap-4 px-6 pt-8" data-lesson-loading>
      <span className="sr-only">{t("lesson.loading")}</span>
      <span className="block h-8 w-80"><Skeleton lines={1} /></span>
      <div className="flex min-h-64 flex-col gap-3 rounded-card border border-line bg-canvas p-5"><Skeleton lines={5} /></div>
    </div>
  );
}

/** The player from its view: the gallery and the tests render this directly. */
export function LessonPlayerContent({ view, actions }: { view: LessonPlayerView; actions: LessonActions }) {
  const t = useT();
  const heading = useRef<HTMLHeadingElement>(null);
  const shownIndex = useRef<number | null>(null);
  const [finished, setFinished] = useState(false);
  const [localTeachingPart, setTeachingPart] = useState(0);
  const teachingPart = view.teachingIndex ?? localTeachingPart;
  const goTeaching = (index: number) => { if (actions.onTeachingGo) actions.onTeachingGo(index); else setTeachingPart(index); };
  const done = useRef<HTMLParagraphElement>(null);
  const wasFinished = useRef(false);
  const lesson = view.lesson;
  useEffect(() => {
    if (finished) done.current?.focus();
    else if (wasFinished.current) heading.current?.focus();
    wasFinished.current = finished;
  }, [finished]);
  useEffect(() => {
    if (lesson === null) return;
    if (shownIndex.current !== null && shownIndex.current !== view.index) heading.current?.focus();
    shownIndex.current = view.index;
  }, [lesson, view.index]);
  const back = actions.onClose !== undefined ? <span className="-ms-3 shrink-0"><IconButton icon="go-back" label={t("lesson.back_to_map")} onClick={actions.onClose} /></span> : null;
  if (view.status === "failed") {
    return (
      <div className="flex min-h-full flex-col" data-lesson-status="failed">
        <ScreenHeader>{back}</ScreenHeader>
        <div className="mx-auto w-full max-w-192 px-6 pt-8">
          <ErrorState message={t("lesson.failed")} {...(actions.onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: actions.onRetry } } : {})} />
        </div>
      </div>
    );
  }
  const block = lesson === null ? undefined : (lesson.blocks[view.index] ?? lesson.blocks[0]);
  if (lesson === null || block === undefined) {
    return <div className="flex min-h-full flex-col" data-lesson-status={view.status}><ScreenHeader>{back}</ScreenHeader><Loading /></div>;
  }
  if (isStepLesson(lesson)) {
    const teaching = lesson.blocks.filter((part) => part.type === "idea" || part.type === "worked_example");
    const part = teaching[teachingPart];
    if (part) return <div className="flex min-h-full flex-col" data-lesson-status="ready" data-lesson-id={lesson.lesson_id} data-teaching-part={teachingPart}>
      <ScreenHeader end={actions.onAsk && <Button size="sm" tone="ghost" onClick={() => actions.onAsk?.({ lessonId: lesson.lesson_id, blockIndex: lesson.blocks.indexOf(part), blockType: part.type })}>{t("lesson.ask")}</Button>}>{back}</ScreenHeader>
      <main className="mx-auto flex w-full max-w-192 flex-1 flex-col gap-6 px-6 py-8">
        <p className="text-meta text-muted">{lesson.title}</p>
        <h1 className="text-title text-ink">{t(part.type === "idea" ? "journey.understand" : "journey.see_example")}</h1>
        {part.type === "idea" ? <PackText text={part.text} /> : <ExampleBoard example={part.worked_example} />}
        <p className="text-meta text-muted">{t("journey.teaching_note")}</p>
        <div className="flex flex-wrap gap-3">
          {teachingPart > 0 && <Button onClick={() => goTeaching(teachingPart - 1)}>{t("lesson.back")}</Button>}
          <Button tone="primary" onClick={() => goTeaching(teachingPart + 1)}>{t(teachingPart === teaching.length - 1 ? "journey.try_supported" : "lesson.continue")}</Button>
        </div>
      </main>
    </div>;
    return <StepLessonContent key={`${lesson.lesson_id}@${lesson.version}`} view={view} lesson={lesson} actions={actions} supported={teaching.length > 0} />;
  }
  const last = view.index >= lesson.blocks.length - 1;
  const total = lesson.blocks.length;
  return (
    <div className="flex min-h-full flex-col" data-lesson-status={view.status} data-lesson-id={lesson.lesson_id} data-block-index={view.index}>
      <ScreenHeader end={<>
        {!finished && <ReadAloud source={{ kind: "lesson_block", lesson_id: lesson.lesson_id, version: lesson.version, block_index: view.index }} />}
        {actions.onAsk !== undefined && !finished && (
          <Button onClick={() => actions.onAsk?.({ lessonId: lesson.lesson_id, blockIndex: view.index, blockType: block.type })}>
            <Icon name="ask" size={16} />
            {t("lesson.ask")}
          </Button>
        )}
      </>}>
        {back}
        <span className="block w-full max-w-80">
          <ProgressBar bare label={t("lesson.parts")} value={finished ? total : view.index + 1} max={total} valueText={t("lesson.progress", { index: view.index + 1, total })} />
        </span>
      </ScreenHeader>
      <article className="mx-auto flex w-full max-w-192 flex-1 flex-col gap-8 px-6 pt-8 pb-4">
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <p className="text-meta text-faint">{t("lesson.minutes", { minutes: lesson.estimated_minutes })}</p>
            <h1 className="text-title text-ink">{lesson.title}</h1>
          </div>
          <nav aria-label={t("lesson.parts")}>
            <ol className="flex flex-wrap gap-1.5">
              {lesson.blocks.map((b, i) => (
                <li key={i}>
                  <button
                    type="button"
                    aria-current={i === view.index ? "step" : undefined}
                    onClick={() => {
                      setFinished(false);
                      actions.onGo(i);
                    }}
                    className={`jana-press relative min-h-8 rounded-chip px-3 text-meta after:absolute after:-inset-1.5 after:content-[''] ${i === view.index ? "bg-wash font-semibold text-ink" : i < view.index ? "text-muted hover:bg-wash hover:text-ink" : "text-faint hover:bg-wash hover:text-ink"}`}
                  >
                    {t(`lesson.block.${b.type}`)}
                  </button>
                </li>
              ))}
            </ol>
          </nav>
        </div>
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
          <section aria-labelledby="lesson-block-heading" className="flex flex-col gap-4" data-block-type={block.type}>
            <h2 id="lesson-block-heading" ref={heading} tabIndex={-1} className="text-label text-faint">{t(`lesson.block.${block.type}`)}</h2>
            <Block key={`${lesson.lesson_id}-${view.index}`} block={block} index={view.index} view={view} actions={actions} />
          </section>
        )}
      </article>
      <div className="mx-auto flex w-full max-w-192 justify-between gap-3 px-6 pt-2 pb-8">
        <Button tone="ghost" disabled={view.index === 0 && !finished} onClick={() => (finished ? setFinished(false) : actions.onGo(view.index - 1))}>
          <Icon name="go-back" size={16} />
          {t("lesson.back")}
        </Button>
        {!finished && (
          <Button tone="primary" onClick={() => (last ? setFinished(true) : actions.onGo(view.index + 1))}>
            {t(last ? "lesson.finish" : "lesson.next")}
            {!last && <Icon name="go-next" size={16} />}
          </Button>
        )}
      </div>
    </div>
  );
}

/** The player for the unlocked profile: opens the lesson at its saved position. */
export function LessonPlayer({ controller, lessonId, onClose, onAsk, onPractise, teaching = false }: { controller: LessonController; lessonId: string; onClose?: () => void; onAsk?: (context: AskContext) => void; onPractise?: () => void; teaching?: boolean }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => {
    void controller.load(lessonId, teaching);
  }, [controller, lessonId, teaching]);
  const actions: LessonActions = {
    onGo: (index) => void controller.go(index),
    onTeachingGo: (index) => controller.goTeaching(index),
    onAnswer: (itemId, parts, timeMs) => void controller.answer(itemId, parts, timeMs),
    onRetry: () => void controller.load(lessonId, teaching),
    ...(onClose !== undefined ? { onClose } : {}),
    ...(onPractise !== undefined ? { onPractise } : {}),
    ...(onAsk !== undefined ? { onAsk } : {}),
  };
  return <LessonPlayerContent key={`${lessonId}:${teaching}`} view={view} actions={actions} />;
}
