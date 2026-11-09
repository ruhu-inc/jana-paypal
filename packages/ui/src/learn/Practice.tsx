// Practice (UXS-24, UXS-16, ASM-02; direction A "Studio"): one focused question at a time. The 56 px header names it
// with the set's progress ("Question 1 of 3") and "End practice"; the question card carries the subject, the stem, the
// answer (a number and its unit, or choices) and two controls only: "Check my answer" and "Need help?" (W11, simpler
// by default). "Need help?" discloses every kind of help in one place: the PED-05 line (hints follow a first attempt),
// the help chips (Give me a hint, Walk me through it, Explain simply) and "Don't know"; nothing else about help sits on
// the page. The newest try is marked at once in the UXS-16 words on its learning colour: Checked (verified), Not quite
// with the "look here" note of the first error (misconception, never the word "wrong"), Equivalent but not simplified,
// Nearly there (hint), with the mastery change after the item (LRN-34); tries before it fold into a collapsed "Earlier
// attempts" disclosure so marks never stack down the page. A question the core has moved past offers "Next question"
// whatever its verdict; units are typeset as on the board (the unit field's echo and each try). Help comes before the
// answer (ASM-28, D120): the help chips open the tutor with the item loaded and ask for that help (Alt+H is the hint);
// a first miss shows no answer and offers a second try; the worked answer, with each step's check and the verified
// key, shows after the second miss or on "Don't know". States: loading, canonical, the tries (verified, look here,
// second try), the worked answer, empty ("All caught up"), the set finished, error-retriable, and practice sets not
// served by this computer's core.
import { useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { BANDS, settled, unitLatexOf, type HelpKind, type MapBand, type PartAnswer, type PracticeController, type PracticeTry, type PracticeView } from "@jana/client-core";
import type { api_commands } from "@jana/contracts-ts/client";
import { BoardStep } from "../board/Board.js";
import { HelpChips } from "../chips/NextStepChips.js";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { Button, Callout, Chip, Disclosure, ErrorState, Loader, ProgressBar, SubjectDot } from "../primitives/index.js";
import { ItemVisual, PartInput, answerOf, type Draft } from "./items.js";
import { BandBar } from "./Mastery.js";
import { PackText } from "./PackText.js";

export interface PracticeActions {
  onAnswer: (answers: PartAnswer[], timeMs: number) => void;
  /** Keeps the answer being written (it survives a visit to the tutor). */
  onDraft?: (drafts: Record<string, Draft>) => void;
  onNext: () => void;
  onEnd: () => void;
  /** The help chips (ASM-28): the tutor opens with the item loaded and is asked for that help; absent where no tutor
   * answers (PLT-29). */
  onHelp?: (kind: HelpKind) => void;
  /** "Why is this right?" (UXS-62): offered only once the item is marked correct; the tutor asks the student to explain
   * first and never changes the mark. Absent where no tutor answers. */
  onWhyRight?: () => void;
  /** "Don't know": the worked answer, then the next question (ASM-28). */
  onDontKnow?: () => void;
  onRetry?: () => void;
  /** Where the quick checks are when practice sets are not served (Learn). */
  onLearn?: () => void;
  /** "Start a mixed review" when nothing is due. */
  onMixed?: () => void;
}

/** A unit as the student typed it, typeset as on the board ("m/s^2" as m/s²) when it reads as a unit (UXS-24). */
export function UnitText({ unit }: { unit: string }) {
  const latex = unitLatexOf(unit);
  return latex === null ? <>{unit}</> : <span data-unit-typeset=""><Tex tex={latex} /></span>;
}

/** A try's answer: each part's value and its typeset unit, its choice or its order. */
function AnswerText({ answers, labels }: { answers: PartAnswer[]; labels: Map<string, string> }) {
  const t = useT();
  return (
    <>
      {answers.map((a, i) => {
        const gap = i > 0 ? "; " : "";
        if (a.kind === "numeric") return <span key={a.part_id}>{gap}{a.value}{a.unit !== null && <> <UnitText unit={a.unit} /></>}</span>;
        if (a.kind === "algebraic") return <span key={a.part_id}>{gap}<Tex tex={a.latex} /></span>;
        if (a.kind === "equation_solutions") return <span key={a.part_id}>{gap}{a.values.join(", ")}</span>;
        if (a.kind === "chemical_equation") return <span key={a.part_id}>{gap}{a.equation}</span>;
        if (a.kind === "short_text") return <span key={a.part_id}>{gap}{a.text}</span>;
        if (a.kind === "journal_entry") return <span key={a.part_id}>{gap}{a.lines.map((line) => `${line.account_id}: ${line.side === "debit" ? t("check.debit") : t("check.credit")} ${line.amount}`).join("; ")}</span>;
        if (a.kind === "accounting_table") return <span key={a.part_id}>{gap}{a.rows.map((row) => row.join(" · ")).join("; ")}</span>;
        if (a.kind === "diagram_label") return <span key={a.part_id}>{gap}{a.labels.map((label) => `${label.target_id.replace(/^label_/, "")}: ${label.label}`).join("; ")}</span>;
        const words = a.kind === "mcq" ? labels.get(a.option_id) ?? a.option_id
          : a.kind === "multi_select" ? a.option_ids.map((id) => labels.get(id) ?? id).join(", ")
            : a.kind === "ordering" ? a.element_ids.map((id) => labels.get(id) ?? id).join(", ") : "";
        return <span key={a.part_id}>{gap}{words}</span>;
      })}
    </>
  );
}

/** A band the core named on a mark (LRN-34), one of the bar's three. */
function isBand(band: string): band is MapBand {
  return (BANDS as readonly string[]).includes(band);
}

/** Where the answer goes in the catalogue's "Try {number}: {answer}", so the answer can carry typeset maths. */
const ANSWER_SLOT = "\uE000";

function TryMark({ attempt, number, labels, last, closed, onNext, finishing, worked }: { attempt: PracticeTry; number: number; labels: Map<string, string>; last: boolean; closed: boolean; onNext: () => void; finishing: boolean; worked: boolean }) {
  const t = useT();
  const { marked } = attempt;
  const firstError = marked.first_error as { category?: string; message_key?: string } | null;
  const look = firstError?.message_key !== undefined && t(firstError.message_key) !== firstError.message_key ? t(firstError.message_key) : null;
  const change = marked.mastery_change.band_before !== marked.mastery_change.band_after
    ? t("practice.mastery_change", { before: t(`practice.band.${marked.mastery_change.band_before}`), after: t(`practice.band.${marked.mastery_change.band_after}`) })
    : null;
  // A first miss keeps the question open: no answer shows, a second try is offered (ASM-28).
  const again = last && !closed && !settled(marked) ? <p data-second-try="">{t("practice.second_try")}</p> : null;
  const next = last && !worked && (closed || settled(marked)) ? <Button size="sm" onClick={onNext}>{t(finishing ? "practice.finish" : "practice.next")}<Icon name="go-next" size={16} /></Button> : undefined;
  const [before = "", after = ""] = t("practice.try", { number, answer: ANSWER_SLOT }).split(ANSWER_SLOT);
  const marks = <p className="text-meta text-muted tabular-nums">{t("check.marks_awarded", { awarded: marked.marks_awarded, total: marked.marks_total })}</p>;
  let body: ReactNode;
  if (marked.verdict === "correct") {
    body = <Callout tone="verified" heading={t("practice.checked")} actions={next}>{marks}{change !== null && <p>{change}</p>}</Callout>;
  } else if (marked.verdict === "equivalent_unsimplified") {
    body = <Callout tone="hint" heading={t("practice.equivalent")} actions={next}>{marks}{change !== null && <p>{change}</p>}</Callout>;
  } else if (marked.verdict === "not_checked") {
    body = <Callout tone="hint" heading={t("check.verdict.not_checked")} actions={next}>{change !== null && <p>{change}</p>}</Callout>;
  } else if (marked.verdict === "partially_correct") {
    body = <Callout tone="hint" heading={t("check.verdict.partially_correct")} actions={next}>{look !== null && <p>{look}</p>}{marks}{again}</Callout>;
  } else {
    body = <Callout tone="look" heading={t("check.verdict.incorrect")} actions={next}>{look !== null ? <p>{look}</p> : <p>{t("check.why.other")}</p>}{marks}{change !== null && <p>{change}</p>}{again}</Callout>;
  }
  return (
    <li className="flex flex-col gap-2" data-try={number} data-verdict={marked.verdict}>
      <p className="text-ui text-muted">{before}<AnswerText answers={attempt.answers} labels={labels} />{after}</p>
      {body}
      {isBand(marked.mastery_change.band_after) && <BandBar band={marked.mastery_change.band_after} label={t("practice.mastery_label")} />}
    </li>
  );
}

/** The worked answer, once earned (ASM-28): every step with its check, then the answer from the verified key with the
 * key's check, and the way on. */
function WorkedAnswer({ worked, onNext, finishing }: { worked: api_commands.WorkedExampleView; onNext: () => void; finishing: boolean }) {
  const t = useT();
  return (
    <section aria-labelledby="practice-worked" data-worked={worked.worked_example_id} className="flex flex-col gap-3 border-t border-line pt-5">
      <h2 id="practice-worked" className="text-label text-faint">{t("practice.worked")}</h2>
      <ol className="flex flex-col gap-1">
        {worked.steps.map((step, i) => (
          <BoardStep key={step.n} number={i + 1} current={false}
            step={{ step_id: `${worked.worked_example_id}-${step.n}`, index: i, latex: step.latex, reason: step.reason, owner: "tutor", status: "final", check_id: step.check_id, error: null }} />
        ))}
      </ol>
      <p className="flex flex-wrap items-center gap-2 text-ui text-ink" data-check-id={worked.final_answer.check_id}>
        <span className="font-semibold">{t("lesson.example.answer")}</span>
        <span className="inline-flex items-center gap-1.5 rounded-chip bg-verified-soft px-3 py-1 text-verified">
          <Icon name="verified" size={16} />
          <span className="board-math text-ink"><Tex tex={worked.final_answer.latex} /></span>
        </span>
      </p>
      <p className="text-meta text-muted">{t("practice.worked_check")}</p>
      <div><Button size="sm" onClick={onNext}>{t(finishing ? "practice.finish" : "practice.next")}<Icon name="go-next" size={16} /></Button></div>
    </section>
  );
}

function Question({ view, actions, subjectLabel }: { view: PracticeView; actions: PracticeActions; subjectLabel: string }) {
  const t = useT();
  const item = view.item;
  const [drafts, setDraftState] = useState<Record<string, Draft>>(() => (view.draft as Record<string, Draft> | null) ?? {});
  const [started, setStarted] = useState(() => Date.now());
  // "Need help?" and "Earlier attempts" both start closed on every question (W11).
  const [helpOpen, setHelpOpen] = useState(false);
  const [earlierOpen, setEarlierOpen] = useState(false);
  const helpId = useId();
  const shown = useRef(item?.item_id);
  useEffect(() => {
    if (shown.current === item?.item_id) return;
    shown.current = item?.item_id;
    setDraftState({});
    setStarted(Date.now());
    setHelpOpen(false);
    setEarlierOpen(false);
  }, [item?.item_id]);
  const setDrafts = (change: (all: Record<string, Draft>) => Record<string, Draft>) => {
    const next = change(drafts);
    setDraftState(next);
    actions.onDraft?.(next);
  };
  if (item === null) return null;
  const answers = item.parts.map((p) => answerOf(p, drafts[p.part_id] ?? {})).filter((a): a is PartAnswer => a !== null);
  const labels = new Map<string, string>(item.parts.flatMap((p) => p.input.kind === "mcq" || p.input.kind === "multi_select" ? p.input.options.map((o) => [o.id, o.label] as [string, string])
    : p.input.kind === "ordering" ? p.input.elements.map((e) => [e.id, e.label] as [string, string]) : []));
  const done = view.closed || view.tries.some((x) => settled(x.marked));
  const finishing = view.next === null;
  const busy = view.marking || view.revealing;
  // Every kind of help lives behind "Need help?" (W11): the tutor's chips while the question is open, a try is still
  // unsettled or the worked answer is up (ASM-28), and "Don't know" while the question is open.
  const chipsOffered = actions.onHelp !== undefined && (!done || view.tries.some((attempt) => !settled(attempt.marked)) || view.worked !== null);
  const revealOffered = actions.onDontKnow !== undefined && !done;
  const helpOffered = chipsOffered || revealOffered;
  const tried = view.tries.length > 0;
  const latest = view.tries.at(-1);
  const earlier = view.tries.slice(0, -1);
  const tryMark = (attempt: PracticeTry, number: number, last: boolean) => (
    <TryMark key={attempt.marked.attempt_id} attempt={attempt} number={number} labels={labels} last={last} closed={view.closed} onNext={actions.onNext} finishing={finishing} worked={view.worked !== null} />
  );
  return (
    <section aria-labelledby="practice-question" data-item-id={item.item_id} className="mx-auto flex w-full max-w-200 min-w-0 flex-col gap-5 rounded-card border border-line bg-canvas p-7">
      <div className="flex flex-wrap items-center gap-2 text-meta text-faint">
        {view.subject !== null && <Chip><SubjectDot subject={view.subject} />{subjectLabel}</Chip>}
        {item.marks !== undefined && <span>{t("check.marks", { marks: item.marks })}</span>}
      </div>
      <div id="practice-question" className="text-heading font-regular text-ink"><PackText text={item.stem} /></div>
      <ItemVisual item={item} />
      {item.parts.map((part) => (
        <div key={part.part_id} data-part-id={part.part_id}>
          <PartInput part={part} itemId={item.item_id} draft={drafts[part.part_id] ?? {}} onDraft={(d) => setDrafts((all) => ({ ...all, [part.part_id]: d }))} prompt={item.parts.length > 1 || part.prompt !== item.stem} />
        </div>
      ))}
      {(!done || helpOffered) && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            {!done && <Button tone="primary" disabled={answers.length === 0 || busy} onClick={() => actions.onAnswer(answers, Date.now() - started)}>{t(tried ? "practice.try_again" : "check.submit")}</Button>}
            {helpOffered && (
              <Button aria-expanded={helpOpen} aria-controls={helpId} disabled={busy} onClick={() => setHelpOpen((open) => !open)} data-need-help="">
                <Icon name="stuck" size={16} />{t("practice.need_help")}
              </Button>
            )}
            {view.marking && <Loader label={t("check.checking")} />}
          </div>
          {helpOffered && (
            <div id={helpId} className="jana-disclosure" data-open={helpOpen}>
              <div className="overflow-hidden" inert={!helpOpen} aria-hidden={!helpOpen}>
                <div data-help-panel="" className="flex flex-col gap-3 rounded-card border border-line bg-panel p-4">
                  {/* After a try the panel stops asking for one: hints are open now (PED-05). */}
                  <Callout tone="hint" heading={t(tried ? "practice.stuck_heading_tried" : "practice.stuck_heading")}>
                    <p>{t(tried ? "practice.stuck_body_tried" : "practice.stuck_body")}</p>
                  </Callout>
                  {chipsOffered && actions.onHelp !== undefined && <HelpChips disabled={busy} onHelp={actions.onHelp} hintKeys="Alt+H" />}
                  {revealOffered && <div><Button tone="ghost" disabled={busy} onClick={actions.onDontKnow}>{t("practice.dont_know")}</Button></div>}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
      {view.markFailed && <ErrorState message={t("check.failed")} />}
      {latest !== undefined && (
        <div className="flex flex-col gap-3 border-t border-line pt-5">
          <ol className="flex flex-col gap-4" aria-live="polite" data-latest-try="">
            {tryMark(latest, view.tries.length, true)}
          </ol>
          {earlier.length > 0 && (
            <Disclosure summary={t("practice.earlier_attempts", { count: earlier.length })} open={earlierOpen} onToggle={() => setEarlierOpen((open) => !open)}>
              <ol className="flex flex-col gap-4 pt-3" data-earlier-attempts="">
                {earlier.map((attempt, i) => tryMark(attempt, i + 1, false))}
              </ol>
            </Disclosure>
          )}
        </div>
      )}
      {actions.onWhyRight !== undefined && view.tries.at(-1)?.marked.verdict === "correct" && (
        <div data-why-right=""><Button size="sm" tone="ghost" onClick={actions.onWhyRight}>{t("practice.why_right")}</Button></div>
      )}
      {view.worked !== null && <WorkedAnswer worked={view.worked} onNext={actions.onNext} finishing={finishing} />}
    </section>
  );
}

/** Practice from its view: the gallery and the tests render this directly. */
export type { Draft as PracticeDraft };

export function PracticeContent({ view, actions, subjectLabel = "" }: { view: PracticeView; actions: PracticeActions; subjectLabel?: string }) {
  const t = useT();
  const help = actions.onHelp;
  useEffect(() => {
    if (help === undefined || view.status !== "ready") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyH") {
        event.preventDefault();
        help("hint");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [help, view.status]);
  const progress = view.status === "ready" && view.total > 0
    ? <span className="block w-full max-w-80"><ProgressBar bare label={t("practice.progress_label")} value={view.number} max={view.total} valueText={t("practice.progress", { index: view.number, total: view.total })} /></span>
    : null;
  const body = () => {
    switch (view.status) {
      case "idle":
      case "starting":
        return <div className="flex justify-center py-16"><Loader label={t("practice.starting")} /></div>;
      case "failed":
        return <ErrorState message={t("practice.failed")} {...(actions.onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: actions.onRetry } } : {})} />;
      case "unavailable":
        return (
          <div className="flex flex-col items-start gap-4 rounded-card border border-line bg-canvas p-7" data-practice-unavailable="">
            <p className="text-body text-ink">{t("practice.unavailable")}</p>
            {actions.onLearn !== undefined && <Button tone="primary" onClick={actions.onLearn}><Icon name="learn" size={16} />{t("home.browse_topics")}</Button>}
          </div>
        );
      case "done":
        return (
          <div role="status" className="flex flex-col items-start gap-4 rounded-card border border-line bg-canvas p-7" data-practice-done="">
            <p className="flex items-center gap-2 text-body text-ink">
              <span className="inline-flex text-verified"><Icon name="verified" /></span>
              {view.total === 0 ? t("practice.empty") : t("practice.done")}
            </p>
            <div className="flex flex-wrap gap-2">
              {actions.onMixed !== undefined && <Button tone="primary" onClick={actions.onMixed}>{t("practice.mixed")}</Button>}
              <Button onClick={actions.onEnd}>{t("practice.end")}</Button>
            </div>
          </div>
        );
      default:
        return <Question view={view} actions={actions} subjectLabel={subjectLabel} />;
    }
  };
  return (
    <div className="flex min-h-full flex-col" data-practice-status={view.status}>
      <ScreenHeader end={view.status === "ready" ? <Button tone="ghost" onClick={actions.onEnd}>{t("practice.end")}</Button> : undefined}>
        <h1 className="shrink-0 text-ui font-semibold text-ink">{t("home.practice")}</h1>
        {progress}
      </ScreenHeader>
      <div className="flex-1 px-8 pt-6 pb-12">
        <div className="mx-auto w-full max-w-260">{body()}</div>
      </div>
    </div>
  );
}

/** Practice for the unlocked profile, on its controller: the set started by whoever opened it (Home's ideas, a topic
 * sheet, Ctrl+3). */
export function Practice({ controller, subjectLabel, onEnd, onLearn, onHelp, onWhyRight }: {
  /** "Why is this right?" on a correct item (UXS-62). */
  onWhyRight?: () => void;
  controller: PracticeController;
  subjectLabel: (subject: string) => string;
  onEnd: () => void;
  onLearn?: () => void;
  /** The help chips: absent where no tutor answers (PLT-29). */
  onHelp?: (kind: HelpKind) => void;
}) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const subject = view.subject;
  const actions: PracticeActions = {
    onAnswer: (answers, timeMs) => void controller.answer(answers, timeMs),
    onDraft: (drafts) => controller.keepDraft(drafts),
    onNext: () => controller.advance(),
    onDontKnow: () => void controller.reveal(),
    onEnd: () => {
      controller.end();
      onEnd();
    },
    ...(subject !== null ? { onRetry: () => void controller.start(subject), onMixed: () => void controller.start(subject) } : {}),
    ...(onLearn !== undefined ? { onLearn } : {}),
    ...(onHelp !== undefined ? { onHelp } : {}),
    ...(onWhyRight !== undefined ? { onWhyRight } : {}),
  };
  return <PracticeContent view={view} actions={actions} subjectLabel={subject === null ? "" : subjectLabel(subject)} />;
}
