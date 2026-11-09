// Mock exams (UXS-25, UXS-26, ASM-15 to ASM-20, AGT-24, LOC-24; direction A "Studio"). The Mock exam place (the
// sidebar's place, Ctrl+4) lists the student's own mocks, then the papers on this computer a subject at a time: the
// subject choice (preselected and not shown when only one subject has papers), then that subject's papers as compact
// rows by paper kind, each with its time, question count and calculator note (and the time with the student's extra
// time) and "Start", or "Continue" on the paper of the open sitting. A chosen paper waits `ready` with the ASM-16
// notice, its marks and "Start". The sitting: the 56 px header names the paper and carries the clock (the
// core's active time, counting down while it runs, shown with "Extra time: 25%" when the profile has it), "Pause" while
// pauses remain and "Submit paper"; beside the question the navigator marks each question answered, flagged for review
// or not seen yet, each with its icon and word; the question card has the answer, "Flag for review" and "Clear answer"
// (independent of each other, API-27), the calculator note and Previous / Next; every change saves within a second
// (ASM-16). Alt+← / Alt+→, Alt+F and Ctrl+Enter move, flag and open the submit confirmation (TABLE LOC-12a). Each
// `sitting.state` has its words (TABLE UXS-25a): ready, in progress, paused ("Paused. 7 minutes of pause time left."),
// interrupted ("Welcome back. 41 minutes left."), submitted ("Marking your paper…"), marked and reviewed (the report),
// abandoned (closed after the window, answers kept). Time up submits by itself and says so. Focus away from the app and
// large pastes are counted for the report, nothing else (ASM-18). The report (UXS-26, ASM-20): the score, the likely
// band where the core gives one in the exam board's own letters (D27), how the paper was taken (extra time, pauses, the app closing), the integrity counts,
// marks by topic, method and accuracy marks lost, time per section, every question with its marks, "Show me why" on
// every answer short of full marks (the tutor with the item and the answer loaded, AGT-24), the mark-scheme text of a
// criterion not marked automatically (ASM-17) and "Practise these topics". While a sitting is open the tutor, voice,
// capture, lessons and practice show `ExamLockNotice`: why they wait and how long is left.
import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type ReactNode, type DragEvent, type ClipboardEvent as ReactClipboardEvent } from "react";
import {
  CLIENT_KEYS, LOCKED_STATES, examLock, mockBandShown, pauseBudget, remainingAt, type ExamDateController, type ExamLock, type MockController, type MockExam, type MockPaper,
  type HelpKind, type MockResult, type MockSitting, type MockView, type PartAnswer, type PastPaperController, type PastPaperSummary, type PracticeItem,
} from "@jana/client-core";
import { HelpChips } from "../chips/NextStepChips.js";
import type { api_commands } from "@jana/contracts-ts";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon, type IconName } from "../icons.js";
import { useT } from "../i18n.js";
import { IconButton, Button, Callout, Chip, ConfirmDialog, EmptyState, ErrorState, FIELD_LOOK, Loader, ProgressBar, SegmentedControl, Skeleton, SubjectDot } from "../primitives/index.js";
import { ItemVisual, PartInput, answerOf, type Draft } from "../learn/items.js";
import { PackInline, PackText } from "../learn/PackText.js";
import { ExamDatesPanel, MarkingLabel, PastPaperShelf, type PastPaperSubjectOption } from "./PastPapers.js";

type Answer = api_commands.Answer;

const CAPTURE_LOCK_EVENTS = {
  onDragOverCapture: (event: DragEvent<HTMLElement>) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.stopPropagation(); } },
  onDropCapture: (event: DragEvent<HTMLElement>) => { if (event.dataTransfer.files.length > 0) { event.preventDefault(); event.stopPropagation(); } },
  onPasteCapture: (event: ReactClipboardEvent<HTMLElement>) => { if (event.clipboardData.files.length > 0) { event.preventDefault(); event.stopPropagation(); } },
};
function PhotoLocked() {
  const t = useT(), id = useId();
  return <div className="my-3 flex items-center gap-2"><IconButton icon="photo" disabled label={t("photo.title")} aria-describedby={id} /><p id={id} className="text-meta text-muted">{t("photo.refusal.capture_locked")}</p></div>;
}


/** A time as the clock shows it: "41:05", or "1:02:09" from an hour. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const two = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
}

/** Whole minutes, rounded up, for the sentences that name minutes ("41 minutes left"). */
export function minutesOf(ms: number): number {
  return Math.max(0, Math.ceil(ms / 60_000));
}

/** The time now, read again every second while `ticking` (the clock on screen). */
export function useNow(ticking: boolean, clock: () => number = Date.now): number {
  const [now, setNow] = useState(clock);
  useEffect(() => {
    setNow(clock());
    if (!ticking) return;
    const timer = setInterval(() => setNow(clock()), 1000);
    return () => clearInterval(timer);
  }, [ticking, clock]);
  return now;
}

/** The question ids of a sitting, in paper order. */
function questionIds(exam: MockExam): string[] {
  return exam.question_ids ?? exam.questions.map((_, i) => String(i + 1));
}

function questionOf(exam: MockExam, questionId: string): PracticeItem | null {
  const at = questionIds(exam).indexOf(questionId);
  return at < 0 ? null : exam.questions[at] ?? null;
}

// --- the lock (AGT-24) ---------------------------------------------------------------------------------------------------

export type LockedPlace = "tutor" | "lessons" | "practice" | "notes" | "assessment";

/** What a locked place shows while a sitting is open: why it waits (the `tpl.mock_running` words), how long is left
 * and the way back to the mock. */
export function ExamLockNotice({ lock, now, place, onOpen }: { lock: ExamLock; now: number; place: LockedPlace; onOpen: () => void }) {
  const t = useT();
  const time = formatClock(remainingAt(lock, now));
  return (
    <section {...CAPTURE_LOCK_EVENTS} data-exam-lock={place} aria-labelledby="exam-lock-title" className="mx-auto flex w-full max-w-160 flex-col items-start gap-4 rounded-card border border-line bg-canvas p-7">
      <p className="flex items-center gap-2 text-meta font-medium text-faint"><Icon name="mock" size={16} />{t("mock.title")}</p>
      <h2 id="exam-lock-title" className="text-heading font-regular text-ink">{t("mock.lock.body")}</h2>
      <p role="timer" aria-live="off" className="flex items-center gap-2 text-ui text-ink tabular-nums">
        <Icon name="timer" size={16} />{t(`mock.lock.${lock.state === "in_progress" ? "running" : lock.state}`, { time })}
      </p>
      <p className="text-ui text-muted">{t(`mock.lock.place.${place}`)}</p>
      <PhotoLocked />
      <Button tone="primary" onClick={onOpen}><Icon name="mock" size={16} />{t("mock.lock.back")}</Button>
    </section>
  );
}

/** Home's card for a sitting closed after the abandonment window: its answers are kept (TABLE UXS-25a). */
export function AbandonedMockCard({ onNew }: { onNew: () => void }) {
  const t = useT();
  return (
    <section data-mock-abandoned="" className="flex flex-col items-start gap-3 rounded-card border border-line bg-canvas px-5 pt-5 pb-4">
      <p className="flex items-center gap-2 text-meta font-medium text-faint"><Icon name="mock" size={16} />{t("mock.title")}</p>
      <p className="text-ui text-ink">{t("mock.abandoned", { days: CLIENT_KEYS.mock_abandon_days })}</p>
      <Button onClick={onNew}>{t("mock.new_mock")}</Button>
    </section>
  );
}

// --- the place: papers and the student's mocks ------------------------------------------------------------------------

export interface MockSubject {
  subject: string;
  label: string;
}

/** One paper of the catalogue as a compact row: its title, then its time, question count and calculator note (and the
 * time with the student's extra time) as quiet meta text, then one action: "Start", or "Continue" when the open sitting
 * is on this paper. Marks and the full notice wait on the `ready` screen, one click on. */
function PaperRow({ paper, extra, open, busy, onChoose, onOpen }: {
  paper: MockPaper; extra: number; open: MockSitting | null; busy: boolean; onChoose: () => void; onOpen: (examId: string) => void;
}) {
  const t = useT();
  const meta = [
    t("mock.row_meta", { minutes: paper.duration_minutes, questions: paper.questions }),
    t(paper.calculator ? "mock.calculator.on" : "mock.calculator.off"),
    ...(extra > 0 ? [t("mock.extra_meta", { minutes: Math.round(paper.duration_minutes * (100 + extra) / 100) })] : []),
  ].join(" · ");
  return (
    <li data-paper-id={paper.paper_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-ui font-medium text-ink"><PackInline text={paper.title} /></span>
        <span className="text-meta text-muted tabular-nums">{meta}</span>
      </span>
      {open !== null
        ? <Button size="sm" tone="primary" disabled={busy} onClick={() => onOpen(open.exam_id)}>{t("mock.list.continue")}</Button>
        : <Button size="sm" disabled={busy} onClick={onChoose}>{t("mock.start")}</Button>}
    </li>
  );
}

/** The papers on this computer, a subject first: the subject choice (not shown when only one subject has papers), then
 * that subject's papers as compact rows grouped by paper kind. The student's mocks sit above. */
function Papers({ view, subjects, extra, onChoose, onOpen, onRetry, now, titleOf }: {
  view: MockView; subjects: MockSubject[]; extra: number; onChoose: (paperId: string) => void; onOpen: (examId: string) => void; onRetry?: (() => void) | undefined; now: number;
  titleOf?: (paperId: string) => string | null;
}) {
  const t = useT();
  const [picked, setPicked] = useState<string | null>(null);
  if (view.status === "idle" || view.status === "loading") return <div className="flex flex-col gap-4"><Skeleton lines={3} /></div>;
  if (view.status === "unavailable") return <div data-mock-unavailable="" className="rounded-card border border-line bg-canvas p-7"><p className="text-body text-ink">{t("mock.unavailable")}</p></div>;
  if (view.status === "failed") return <ErrorState message={t("mock.failed")} {...(onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: onRetry } } : {})} />;
  const titled = (paperId: string) => view.papers.find((p) => p.paper_id === paperId)?.title ?? titleOf?.(paperId) ?? t("mock.title");
  const groups = subjects.map((s) => ({ ...s, papers: view.papers.filter((p) => p.subject === s.subject) })).filter((g) => g.papers.length > 0);
  const shown = groups.find((g) => g.subject === picked) ?? groups[0];
  const kinds = shown === undefined ? [] : [...new Set(shown.papers.map((p) => p.paper_kind))].map((kind) => ({ kind, papers: shown.papers.filter((p) => p.paper_kind === kind) }));
  const lock = examLock(view);
  const openSitting = view.sittings.find((s) => LOCKED_STATES.includes(s.state)) ?? null;
  return (
    <div className="flex flex-col gap-10">
      {view.sittings.length > 0 && (
        <section aria-labelledby="mock-yours" className="flex flex-col gap-3">
          <h2 id="mock-yours" className="text-heading text-ink">{t("mock.yours")}</h2>
          <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5">
            {view.sittings.map((s) => {
              const open = LOCKED_STATES.includes(s.state);
              const status = open ? t("mock.list.open", { time: formatClock(remainingAt({ state: s.state, remainingMs: s.remaining_ms, readAt: view.listedAt }, now)) })
                : s.state === "marked" || s.state === "reviewed" ? t("mock.list.marked", { score: s.score ?? 0, max: s.max_score ?? 0 })
                  : t(`mock.list.${s.state}`);
              const action = open ? "mock.list.continue" : s.state === "marked" || s.state === "reviewed" ? "mock.list.report" : s.state === "ready" ? "mock.list.start" : null;
              return (
                <li key={s.exam_id} data-sitting-id={s.exam_id} data-sitting-state={s.state} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className="text-ui font-medium text-ink"><PackInline text={titled(s.paper_id)} /></span>
                    <span className="text-meta text-muted tabular-nums">{status}</span>
                  </span>
                  {action !== null && <Button size="sm" tone={open ? "primary" : "secondary"} onClick={() => onOpen(s.exam_id)}>{t(action)}</Button>}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <section aria-labelledby="mock-papers" className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <h2 id="mock-papers" className="text-heading text-ink">{t("mock.papers")}</h2>
          <p className="text-ui text-muted">{t("mock.intro")}</p>
        </div>
        {lock !== null && <Callout tone="hint" icon="timer" heading={t("mock.one_at_a_time")} />}
        {shown === undefined ? <EmptyState message={t("mock.empty")} /> : (
          <div className="flex flex-col gap-4" data-mock-subject={shown.subject}>
            {groups.length > 1 && (
              <div><SegmentedControl label={t("mock.subject")} options={groups.map((g) => ({ value: g.subject, label: g.label }))} value={shown.subject} onChange={setPicked} /></div>
            )}
            {kinds.map((k) => (
              <div key={k.kind} className="flex flex-col gap-2" data-paper-kind={k.kind}>
                <h3 className="flex items-center gap-2 text-ui font-semibold text-ink"><SubjectDot subject={shown.subject} />{t(`mock.kind.${k.kind}`)}</h3>
                <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5">
                  {k.papers.map((p) => (
                    <PaperRow key={p.paper_id} paper={p} extra={extra} open={openSitting?.paper_id === p.paper_id ? openSitting : null}
                      busy={view.opening || (lock !== null && openSitting?.paper_id !== p.paper_id)} onChoose={() => onChoose(p.paper_id)} onOpen={onOpen} />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

// --- the sitting ---------------------------------------------------------------------------------------------------------

/** What the navigator says of a question: answered, flagged for review, not seen yet, or seen and not answered. */
type QuestionState = { answered: boolean; flagged: boolean; seen: boolean };

function stateOf(exam: MockExam, seen: readonly string[], questionId: string): QuestionState {
  const saved = exam.answers[questionId];
  return { answered: saved?.answer != null, flagged: saved?.flagged === true, seen: seen.includes(questionId) || saved !== undefined };
}

function Navigator({ exam, seen, current, onShow }: { exam: MockExam; seen: readonly string[]; current: string | null; onShow: (questionId: string) => void }) {
  const t = useT();
  const ids = questionIds(exam);
  const words = (s: QuestionState) => [s.answered ? t("mock.nav.answered") : s.seen ? t("mock.nav.not_answered") : t("mock.nav.unseen"), ...(s.flagged ? [t("mock.nav.flagged")] : [])].join(", ");
  const legend: [IconName, string][] = [["answered", t("mock.nav.answered")], ["flag-review", t("mock.nav.flagged")], ["unseen", t("mock.nav.unseen")]];
  return (
    <nav aria-label={t("mock.navigator")} data-mock-navigator="" className="flex w-full max-w-80 min-w-0 flex-1 basis-60 flex-col gap-3 self-start rounded-card border border-line bg-canvas p-5">
      <h2 className="text-label text-faint">{t("mock.navigator")}</h2>
      <ol className="grid grid-cols-[repeat(auto-fill,minmax(3rem,1fr))] gap-1.5">
        {ids.map((id, i) => {
          const s = stateOf(exam, [...seen], id);
          const icon: IconName | null = s.flagged ? "flag-review" : s.answered ? "answered" : s.seen ? null : "unseen";
          return (
            <li key={id}>
              <button type="button" data-question={id} data-answered={s.answered ? "true" : undefined} data-flagged={s.flagged ? "true" : undefined} data-seen={s.seen ? "true" : undefined}
                aria-current={current === id ? "step" : undefined} aria-label={t("mock.nav.item", { number: i + 1, state: words(s) })} onClick={() => onShow(id)}
                className={`jana-press flex min-h-11 w-full flex-col items-center justify-center gap-0.5 rounded-control border text-meta font-medium tabular-nums hover:bg-wash aria-[current=step]:border-ink aria-[current=step]:bg-wash ${s.answered ? "border-line-strong bg-panel text-ink" : "border-line text-muted"}`}>
                <span aria-hidden="true">{i + 1}</span>
                {icon !== null && <span aria-hidden="true" className={s.flagged ? "text-hint" : s.answered ? "text-ink" : "text-faint"}><Icon name={icon} size={16} /></span>}
              </button>
            </li>
          );
        })}
      </ol>
      <ul className="flex flex-col gap-1 border-t border-line pt-3 text-meta text-muted">
        {legend.map(([icon, word]) => <li key={icon} className="flex items-center gap-2"><Icon name={icon} size={16} />{word}</li>)}
      </ul>
    </nav>
  );
}

/** A saved part answer back as the inputs' draft. */
export function draftOf(answer: PartAnswer): Draft {
  if (answer.kind === "mcq") return { option: answer.option_id };
  if (answer.kind === "multi_select") return { options: answer.option_ids };
  if (answer.kind === "numeric") return { value: answer.value, unit: answer.unit ?? "" };
  if (answer.kind === "ordering") return { order: answer.element_ids };
  if (answer.kind === "algebraic") return { value: answer.latex };
  if (answer.kind === "equation_solutions") return { value: answer.values.join(", ") };
  if (answer.kind === "chemical_equation") return { value: answer.equation };
  if (answer.kind === "short_text") return { value: answer.text };
  if (answer.kind === "journal_entry") return { lines: answer.lines.map((line) => ({ ...line })) };
  if (answer.kind === "accounting_table") return { rows: answer.rows.map((row) => [...row]) };
  if (answer.kind === "diagram_label") return { labels: Object.fromEntries(answer.labels.map((label) => [label.target_id, label.label])) };
  return {};
}

/** The answer to send from the drafts and the working (null when nothing is answered: unanswered, API-27). */
export function mockAnswerOf(item: PracticeItem, drafts: Record<string, Draft>, working: Record<string, string>): Answer | null {
  const parts = item.parts.map((p) => answerOf(p, drafts[p.part_id] ?? {})).filter((a): a is PartAnswer => a !== null);
  if (parts.length === 0) return null;
  const lines = item.parts.flatMap((p) => (working[p.part_id] ?? "").split("\n").map((line) => line.trim()).filter((line) => line !== "").map((latex) => ({ part_id: p.part_id, latex })));
  return lines.length === 0 ? { parts } : { parts, working: lines.slice(0, 100) };
}

/** An imported past paper's diagram (ASM-27): the page of the paper it is on, read while the paper is sat. */
function PaperFigure({ examId, questionId, figure, load }: { examId: string; questionId: string; figure: NonNullable<PracticeItem["figures"]>[number]; load?: MockActions["onFigure"] }) {
  const t = useT();
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (load === undefined || figure.page === null) return;
    let live = true;
    let made: string | null = null;
    void load(examId, questionId).then((blob) => {
      if (!live || blob === null || typeof URL.createObjectURL !== "function") return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    });
    return () => {
      live = false;
      if (made !== null) URL.revokeObjectURL(made);
    };
  }, [load, examId, questionId, figure.page]);
  return (
    <figure className="flex flex-col gap-2" data-paper-figure={figure.page ?? ""}>
      {url !== null && <img src={url} alt={t("past_paper.figure", { caption: figure.caption })} className="max-h-120 w-full rounded-control border border-line object-contain" />}
      <figcaption className="text-meta text-muted">{figure.page !== null ? t("past_paper.figure_page", { page: figure.page }) : t("past_paper.figure_missing", { caption: figure.caption })}</figcaption>
    </figure>
  );
}

function QuestionCard({ exam, questionId, number, total, onAnswer, onFlag, onClear, onMove, onFigure }: {
  exam: MockExam; questionId: string; number: number; total: number; onAnswer: (answer: Answer | null) => void; onFlag: () => void; onClear: () => void;
  onMove: (step: -1 | 1) => void; onFigure?: MockActions["onFigure"];
}) {
  const t = useT();
  const item = questionOf(exam, questionId);
  const saved = exam.answers[questionId];
  const fromSaved = () => Object.fromEntries((saved?.answer?.parts ?? []).map((p) => [p.part_id, draftOf(p)]));
  const workingFromSaved = () => {
    const out: Record<string, string> = {};
    for (const line of saved?.answer?.working ?? []) out[line.part_id] = out[line.part_id] === undefined ? line.latex : `${out[line.part_id]}\n${line.latex}`;
    return out;
  };
  const [drafts, setDrafts] = useState<Record<string, Draft>>(fromSaved);
  const [working, setWorking] = useState<Record<string, string>>(workingFromSaved);
  // A cleared answer empties the inputs (the saved answer went to null from here or from the core).
  const cleared = saved?.answer == null;
  useEffect(() => {
    if (cleared) {
      setDrafts({});
      setWorking({});
    }
  }, [cleared]);
  if (item === null) return null;
  const change = (nextDrafts: Record<string, Draft>, nextWorking: Record<string, string>) => {
    setDrafts(nextDrafts);
    setWorking(nextWorking);
    onAnswer(mockAnswerOf(item, nextDrafts, nextWorking));
  };
  const flagged = saved?.flagged === true;
  return (
    <section aria-labelledby="mock-question" data-mock-question={questionId} data-item-id={item.item_id} className="flex min-w-0 flex-1 basis-120 flex-col gap-5 rounded-card border border-line bg-canvas p-7">
      <div className="flex flex-wrap items-center gap-2 text-meta text-faint">
        <span className="font-medium text-muted">{t("mock.question_of", { number, total })}</span>
        {item.marks !== undefined && <span>{t("check.marks", { marks: item.marks })}</span>}
        {flagged && <Chip tone="hint" icon="flag-review" size="sm">{t("mock.nav.flagged")}</Chip>}
        {item.number_label !== undefined && <span data-number-label={item.number_label}>{t("past_paper.number", { label: item.number_label })}</span>}
      </div>
      <div id="mock-question" className="text-heading font-regular text-ink"><PackText text={item.stem} /></div>
      <ItemVisual item={item} />
      {(item.figures ?? []).map((f, i) => <PaperFigure key={i} examId={exam.exam_id} questionId={questionId} figure={f} {...(onFigure !== undefined ? { load: onFigure } : {})} />)}
      {item.parts.map((part) => (
        <div key={part.part_id} data-part-id={part.part_id} className="flex flex-col gap-3">
          <PartInput part={part} itemId={item.item_id} draft={drafts[part.part_id] ?? {}} prompt={item.parts.length > 1 || part.prompt !== item.stem}
            onDraft={(d) => change({ ...drafts, [part.part_id]: d }, working)} />
          {["numeric", "algebraic", "equation_solutions"].includes(part.input.kind) && (
            <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
              {t("mock.working")}
              <textarea className={`${FIELD_LOOK} min-h-20 py-2 font-mono`} value={working[part.part_id] ?? ""} spellCheck={false} aria-describedby={`mock-working-help-${part.part_id}`}
                onChange={(e) => change(drafts, { ...working, [part.part_id]: e.target.value })} />
              <span id={`mock-working-help-${part.part_id}`} className="font-regular text-faint">{t("mock.working_help")}</span>
            </label>
          )}
        </div>
      ))}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-5">
        <div className="flex flex-wrap gap-2">
          <Button aria-pressed={flagged} aria-keyshortcuts="Alt+F" onClick={onFlag}><Icon name="flag-review" size={16} />{t(flagged ? "mock.unflag" : "mock.flag")}</Button>
          <Button tone="ghost" disabled={cleared} onClick={onClear}>{t("mock.clear")}</Button>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button tone="ghost" disabled={number <= 1} aria-keyshortcuts="Alt+ArrowLeft" onClick={() => onMove(-1)}><Icon name="go-back" size={16} />{t("mock.previous")}</Button>
          <Button disabled={number >= total} aria-keyshortcuts="Alt+ArrowRight" onClick={() => onMove(1)}>{t("mock.next")}<Icon name="go-next" size={16} /></Button>
        </div>
      </div>
    </section>
  );
}

export interface MockActions {
  onChoose: (paperId: string) => void;
  onOpen: (examId: string) => void;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onShow: (questionId: string) => void;
  onAnswer: (questionId: string, answer: Answer | null) => void;
  onFlag: (questionId: string) => void;
  onClear: (questionId: string) => void;
  onSubmit: () => void;
  onRetryMark: () => void;
  onRetrySave?: () => void;
  onRetry?: () => void;
  onLeave: () => void;
  /** "Show me why" on a question of the report (AGT-24, UXS-26): absent where no tutor answers (PLT-29). */
  onShowWhy?: (reference: string) => void;
  /** "Practise these topics": the follow-up set the report made (ASM-20). */
  onPractise?: (setRef: string) => void;
  /** "Practise this" on one topic of the report, most marks lost first (M1, D109). */
  onPractiseTopic?: (topicId: string) => void;
  /** A help chip on a question of a marked past paper (ASM-28): the tutor opens on it with that request. Never during
   * a sitting: a timed paper shows no help until it is submitted. */
  onHelp?: (reference: string, kind: HelpKind) => void;
  /** The page holding an imported past-paper question's diagram (ASM-27). */
  onFigure?: (examId: string, questionId: string) => Promise<Blob | null>;
}

/** A card in the middle of the sitting for the states without a paper on screen. */
function StateCard({ children, state }: { children: ReactNode; state: string }) {
  return <section data-sitting-card={state} className="mx-auto flex w-full max-w-160 flex-col items-start gap-4 rounded-card border border-line bg-canvas p-7">{children}</section>;
}

function Sitting({ view, exam, now, actions, paper, kindLabel }: { view: MockView; exam: MockExam; now: number; actions: MockActions; paper: MockPaper | null; kindLabel?: string | undefined }) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const ids = questionIds(exam);
  const current = view.current ?? ids[0] ?? null;
  const at = current === null ? -1 : ids.indexOf(current);
  const lock = { state: exam.state, remainingMs: exam.remaining_ms, readAt: view.readAt };
  const left = remainingAt(lock, now);
  const budget = pauseBudget(exam, view.readAt, now);
  const running = exam.state === "in_progress";
  const move = (step: -1 | 1) => {
    const next = ids[at + step];
    if (next !== undefined) actions.onShow(next);
  };
  // The sitting's keys (TABLE LOC-12a): Alt+← / Alt+→ move, Alt+F flags, Ctrl+Enter opens the submit confirmation.
  const keys = useRef({ move, current, running, confirm: () => setConfirming(true), flag: actions.onFlag });
  keys.current = { move, current, running, confirm: () => setConfirming(true), flag: actions.onFlag };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const k = keys.current;
      if (!k.running || document.querySelector("[aria-modal='true']") !== null) return;
      if (event.altKey && !event.ctrlKey && !event.metaKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault();
        k.move(event.key === "ArrowLeft" ? -1 : 1);
      } else if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyF" && k.current !== null) {
        event.preventDefault();
        k.flag(k.current);
      } else if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        k.confirm();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const extra = exam.extra_time_percent ?? 0;
  const clock = (
    <span role="timer" aria-live="off" aria-label={t("mock.time_label")} data-mock-clock={exam.state} className="inline-flex min-h-9 items-center gap-2 rounded-chip bg-panel px-3 text-ui font-semibold text-ink tabular-nums">
      <Icon name="timer" size={16} />{t("mock.time_left", { time: formatClock(left) })}
    </span>
  );
  const header = (
    <ScreenHeader end={running ? (
      <>
        {budget.pauses > 0 && <Button tone="ghost" disabled={view.acting} onClick={actions.onPause}><Icon name="pause" size={16} />{t("mock.pause")}<span className="text-meta font-regular text-muted">{t("mock.pauses_left", { count: budget.pauses })}</span></Button>}
        <Button tone="primary" disabled={view.acting} aria-keyshortcuts="Control+Enter" onClick={() => setConfirming(true)}>{t("mock.submit_paper")}</Button>
      </>
    ) : (
      <Button tone="ghost" onClick={actions.onLeave}><Icon name="go-back" size={16} />{t("mock.back")}</Button>
    )}>
      <h1 className="min-w-0 truncate text-ui font-semibold text-ink"><PackInline text={paper?.title ?? t("mock.title")} /></h1>
      {LOCKED_STATES.includes(exam.state) && clock}
      {extra > 0 && LOCKED_STATES.includes(exam.state) && <Chip size="sm">{t("mock.extra_time", { percent: extra })}</Chip>}
    </ScreenHeader>
  );
  const refused = view.refused === null ? null : <ErrorState message={t(view.refused === "validation_failed" && budget.pauses === 0 ? "mock.refused.pause" : "mock.refused.other")} />;
  const body = (): ReactNode => {
    if (view.markFailed) return <StateCard state="mark-failed"><ErrorState message={t("mock.mark_failed")} action={{ label: t("action.try_again"), onClick: actions.onRetryMark }} /></StateCard>;
    if (view.marking || exam.state === "submitted") {
      return (
        <StateCard state="submitted">
          {view.timeUp && <p className="text-body text-ink" role="status">{t("mock.time_up")}</p>}
          <Loader label={t("mock.marking")} />
        </StateCard>
      );
    }
    switch (exam.state) {
      case "ready":
        return (
          <StateCard state="ready">
            <p className="text-meta font-medium text-faint">{kindLabel ?? (paper !== null ? t(`mock.kind.${paper.paper_kind}`) : t("mock.title"))}</p>
            <p className="text-heading font-regular text-ink">{t("mock.ready_notice")}</p>
            {paper !== null && <p className="text-ui text-muted tabular-nums">{t("mock.paper_meta", { minutes: minutesOf(exam.duration_ms), marks: paper.total_marks, questions: paper.questions })}</p>}
            {extra > 0 && <p className="text-ui text-muted">{t("mock.extra_time", { percent: extra })}</p>}
            <p className="flex items-center gap-1.5 text-ui text-muted"><Icon name="calculator" size={16} />{t(exam.calculator === true ? "mock.calculator.on" : "mock.calculator.off")}</p>
            <div className="flex flex-wrap gap-2">
              <Button tone="primary" disabled={view.acting} onClick={actions.onStart}>{t("mock.start")}</Button>
              <Button tone="ghost" onClick={actions.onLeave}>{t("mock.back")}</Button>
            </div>
            {refused}
          </StateCard>
        );
      case "paused":
        return (
          <StateCard state="paused">
            <p className="flex items-center gap-2 text-heading font-regular text-ink" role="status"><Icon name="pause" />{t("mock.paused", { minutes: minutesOf(budget.ms) })}</p>
            <p className="text-ui text-muted">{t("mock.paused_note")}</p>
            <Button tone="primary" disabled={view.acting} onClick={actions.onResume}><Icon name="continue" size={16} />{t("mock.continue")}</Button>
            {refused}
          </StateCard>
        );
      case "interrupted":
        return (
          <StateCard state="interrupted">
            <p className="text-heading font-regular text-ink" role="status">{t("mock.welcome_back", { minutes: minutesOf(exam.remaining_ms) })}</p>
            <p className="text-ui text-muted">{t("mock.interrupted_note")}</p>
            <Button tone="primary" disabled={view.acting} onClick={actions.onResume}><Icon name="continue" size={16} />{t("mock.continue_mock")}</Button>
            {refused}
          </StateCard>
        );
      case "abandoned":
        return (
          <StateCard state="abandoned">
            <p className="text-body text-ink">{t("mock.abandoned", { days: CLIENT_KEYS.mock_abandon_days })}</p>
            <Button onClick={actions.onLeave}>{t("mock.new_mock")}</Button>
          </StateCard>
        );
      case "in_progress":
        return (
          <div className="flex flex-wrap items-start justify-center gap-6">
            <div className="flex min-w-0 flex-1 basis-120 flex-col gap-3">
              <p className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-meta text-muted">
                <span className="flex items-center gap-1.5"><Icon name="calculator" size={16} />{t(exam.calculator === true ? "mock.calculator.on" : "mock.calculator.off")}</span>
                <span data-mock-saved={view.unsaved.length > 0 ? "saving" : "saved"}>{view.unsaved.length > 0 ? t("mock.saving") : t("mock.saved")}</span>
              </p>
              {view.saveFailed && <ErrorState message={t("mock.save_failed")} {...(actions.onRetrySave === undefined ? {} : { action: { label: t("action.try_again"), onClick: actions.onRetrySave } })} />}
              {current !== null && (
                <QuestionCard key={current} exam={exam} questionId={current} number={at + 1} total={ids.length} onMove={move}
                  onAnswer={(answer) => actions.onAnswer(current, answer)} onFlag={() => actions.onFlag(current)} onClear={() => actions.onClear(current)}
                  {...(actions.onFigure !== undefined ? { onFigure: actions.onFigure } : {})} />
              )}
              {refused}
            </div>
            <Navigator exam={exam} seen={view.seen} current={current} onShow={actions.onShow} />
          </div>
        );
      default:
        return null;
    }
  };
  return (
    <div {...(LOCKED_STATES.includes(exam.state) ? CAPTURE_LOCK_EVENTS : {})} className="flex min-h-full flex-col" data-sitting-state={exam.state}>
      {header}
      <div className="flex-1 px-8 pt-6 pb-12"><div className="mx-auto w-full max-w-260">{LOCKED_STATES.includes(exam.state) && <PhotoLocked />}{body()}</div></div>
      <ConfirmDialog open={confirming && running} message={t("mock.submit_confirm")}
        details={exam.question_ids === undefined ? [] : [t("mock.submit_count", { answered: ids.filter((id) => exam.answers[id]?.answer != null).length, total: ids.length })]}
        confirm={t("mock.submit")} cancel={t("mock.keep_working")} onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          actions.onSubmit();
        }} />
    </div>
  );
}

// --- the report (UXS-26) -------------------------------------------------------------------------------------------------

/** Titles for the report's topics and ideas, from the subject's map. */
export interface ReportNames {
  topic: (topicId: string) => string;
  kc: (kcId: string) => string;
}

function AnswerWords({ item, answer }: { item: PracticeItem; answer: Answer | null | undefined }) {
  const t = useT();
  if (answer == null) return <>{t("mock.report.q.unanswered")}</>;
  const label = new Map<string, string>(item.parts.flatMap((p) => p.input.kind === "mcq" || p.input.kind === "multi_select" ? p.input.options.map((o) => [o.id, o.label] as [string, string])
    : p.input.kind === "ordering" ? p.input.elements.map((e) => [e.id, e.label] as [string, string]) : []));
  return (
    <>{answer.parts.map((a, i) => {
      const gap = i > 0 ? "; " : "";
      const words = a.kind === "mcq" ? label.get(a.option_id) ?? a.option_id : a.kind === "multi_select" ? a.option_ids.map((id) => label.get(id) ?? id).join(", ")
        : a.kind === "numeric" ? `${a.value}${a.unit !== null ? ` ${a.unit}` : ""}` : a.kind === "ordering" ? a.element_ids.map((id) => label.get(id) ?? id).join(", ") : "";
      return <span key={a.part_id}>{gap}<PackInline text={words} /></span>;
    })}</>
  );
}

export function MockReport({ exam, result, paper, names, actions, kindLabel, questionExtra }: {
  exam: MockExam; result: MockResult; paper: MockPaper | null; names: ReportNames; actions: Pick<MockActions, "onShowWhy" | "onPractise" | "onPractiseTopic" | "onLeave" | "onHelp">;
  kindLabel?: string | undefined;
  /** Extra controls under a question of the report: the hook for the help chips of ASM-28 (M45.d), after submission only. */
  questionExtra?: ((questionId: string, item: PracticeItem | null) => ReactNode) | undefined;
}) {
  const t = useT();
  // An imported past paper says how each mark was decided (ASM-27): by part, checked, against the key, or judged.
  const methods = new Map((result.past_paper?.marking ?? []).map((m) => [`${m.question_id}:${m.part_id}`, m.method]));
  const ids = questionIds(exam);
  const criteria = new Map((result.criteria ?? []).map((c) => [c.question_id, c.criteria]));
  const review = new Map(result.review_items.map((r) => [r.question_id, r]));
  const share = result.max_score > 0 ? result.score / result.max_score : 0;
  const notes: string[] = [];
  if (result.taken_with_extra_time === true) notes.push(t("mock.report.extra", { percent: result.extra_time_percent ?? 0 }));
  if ((result.pauses ?? []).length > 0) notes.push(t("mock.report.pauses"));
  if ((result.interruptions ?? []).length > 0) notes.push(t("mock.report.interruptions", { count: (result.interruptions ?? []).length }));
  const integrity = result.integrity_counts;
  // Marks lost on a question left unanswered are not method, accuracy or independent marks lost on an answer: they
  // are counted on their own, and the M/A/B split covers only the answers given.
  const unansweredIds = new Set(result.unanswered ?? []);
  const answeredLost = { M: 0, A: 0, B: 0 };
  let unansweredLost = 0;
  for (const row of result.criteria ?? []) {
    for (const c of row.criteria) {
      const missed = Math.max(0, c.available - c.awarded);
      if (unansweredIds.has(row.question_id)) unansweredLost += missed;
      else if (c.mark_type in answeredLost) answeredLost[c.mark_type as keyof typeof answeredLost] += missed;
    }
  }
  const lost = result.criteria === undefined ? result.marks_lost : answeredLost;
  // The band only in the exam board's own letters (D27, D100): a paper without them shows nothing about a band; a null
  // band says the evidence is not enough yet.
  const band = mockBandShown(result);
  const flagged = new Set(result.flagged ?? []);
  const unanswered = new Set(result.unanswered ?? []);
  const omitted = new Set(result.optional_omitted ?? []);
  const sections = Object.entries(result.time_by_section ?? {});
  const practiseRef = result.practice_set_ref ?? null;
  const flaggedIntegrity = integrity !== undefined && (integrity.focus_loss > 0 || integrity.large_paste > 0);
  return (
    <div className="flex flex-col gap-8" data-mock-report={exam.exam_id}>
      <section aria-labelledby="mock-score" className="flex flex-wrap items-start gap-6 rounded-card border border-line bg-canvas p-7">
        <div className="flex min-w-60 flex-1 flex-col gap-2">
          <p className="text-meta font-medium text-faint">{kindLabel ?? (paper !== null ? t(`mock.kind.${paper.paper_kind}`) : t("mock.title"))}</p>
          <h2 id="mock-score" className="text-display text-ink tabular-nums">{t("mock.report.score", { score: result.score, max: result.max_score })}</h2>
          <span className="block max-w-100"><ProgressBar bare label={t("mock.report.score_label")} value={result.score} max={Math.max(1, result.max_score)} valueText={t("mock.report.percent", { share })} /></span>
          {band.kind !== "none" && (
            <p className="text-ui text-ink" data-grade-band={band.kind === "band" ? band.band : "none"}>
              {band.kind === "band" ? t("mock.report.band", { band: band.band, low: band.low, high: band.high }) : t("mock.report.no_band")}
            </p>
          )}
        </div>
        {result.past_paper !== undefined && <p className="w-full text-ui text-muted" data-past-paper-note="">{t("past_paper.report_note")}</p>}
        {/* The integrity counts show only when there is something to say: two zeros read like being watched. */}
        {(notes.length > 0 || flaggedIntegrity) && (
          <ul className="flex min-w-60 flex-1 flex-col gap-1.5 text-ui text-muted">
            {notes.map((n) => <li key={n}>{n}</li>)}
            {flaggedIntegrity && <li data-integrity="">{t("mock.report.integrity", { away: integrity.focus_loss, pastes: integrity.large_paste })}</li>}
          </ul>
        )}
      </section>
      <div className="flex flex-wrap items-start gap-6">
        <section aria-labelledby="mock-topics" className="flex min-w-72 flex-1 flex-col gap-3 rounded-card border border-line bg-canvas p-5">
          <h2 id="mock-topics" className="text-heading text-ink">{t("mock.report.by_topic")}</h2>
          {/* Most marks lost first; each topic leads straight into practice on it (M1, D109). */}
          <ul className="flex flex-col divide-y divide-line">
            {[...result.by_topic].sort((a, b) => (b.max_score - b.score) - (a.max_score - a.score)).map((topic) => {
              const lostHere = Math.max(0, topic.max_score - topic.score);
              return (
                <li key={topic.topic_id} data-topic-id={topic.topic_id} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                  <span className="min-w-48 flex-1"><ProgressBar label={names.topic(topic.topic_id)} value={topic.score} max={Math.max(1, topic.max_score)} valueText={t("mock.report.marks", { awarded: topic.score, available: topic.max_score })} /></span>
                  {lostHere > 0 && actions.onPractiseTopic !== undefined && <Button size="sm" onClick={() => actions.onPractiseTopic?.(topic.topic_id)}>{t("mock.report.practise_topic")}</Button>}
                </li>
              );
            })}
          </ul>
        </section>
        <section aria-labelledby="mock-detail" className="flex min-w-72 flex-1 flex-col gap-3 rounded-card border border-line bg-canvas p-5">
          <h2 id="mock-detail" className="text-heading text-ink">{t("mock.report.where")}</h2>
          {lost !== undefined && lost.M + lost.A + lost.B > 0 && <p className="text-ui text-ink">{t("mock.report.lost", { m: lost.M, a: lost.A, b: lost.B })}</p>}
          {unansweredLost > 0 && <p className="text-ui text-ink">{t("mock.report.lost_unanswered", { marks: unansweredLost })}</p>}
          {sections.length > 0 && (
            <ul className="flex flex-col gap-1 text-ui text-muted">
              {sections.map(([section, ms]) => <li key={section}>{t("mock.report.section_time", { section, minutes: minutesOf(ms ?? 0) })}</li>)}
            </ul>
          )}
          <p className="text-ui text-muted">{t("mock.report.counts", { flagged: flagged.size, unanswered: unanswered.size })}</p>
          {actions.onPractise !== undefined && practiseRef !== null && (
            <div className="flex flex-col items-start gap-2 border-t border-line pt-3" data-practise-ref={practiseRef}>
              <p className="text-ui text-ink">{t("mock.report.practise_body")}</p>
              {(result.practice_kc_ids ?? []).length > 0 && (
                <ul className="flex list-disc flex-col gap-1 ps-5 text-meta text-muted">{(result.practice_kc_ids ?? []).map((kc) => <li key={kc}><PackInline text={names.kc(kc)} /></li>)}</ul>
              )}
              <Button tone="primary" onClick={() => actions.onPractise?.(practiseRef)}><Icon name="practice" size={16} />{t("mock.report.practise")}</Button>
            </div>
          )}
        </section>
      </div>
      <section aria-labelledby="mock-questions" className="flex flex-col gap-3">
        <h2 id="mock-questions" className="text-heading text-ink">{t("mock.report.questions")}</h2>
        <ol className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5">
          {ids.filter((id) => !omitted.has(id)).map((id) => {
            const item = questionOf(exam, id);
            const marks = criteria.get(id) ?? [];
            const awarded = marks.reduce((n, c) => n + c.awarded, 0);
            const available = marks.reduce((n, c) => n + c.available, 0);
            const row = review.get(id);
            const unresolved = marks.filter((c) => !c.resolved);
            const verdict = unanswered.has(id) ? "unanswered" : row?.correct === true ? "full" : awarded > 0 ? "part" : "none";
            const number = ids.indexOf(id) + 1;
            const partMethods = item?.parts.map((p) => methods.get(`${id}:${p.part_id}`)).filter((m) => m !== undefined) ?? [];
            return (
              <li key={id} data-review-question={id} data-review-verdict={verdict} className="flex flex-col gap-2 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <p className="flex flex-wrap items-center gap-2 text-meta text-faint">
                      <span className="font-medium text-muted">{item?.number_label !== undefined ? t("past_paper.question", { label: item.number_label }) : t("mock.report.question", { number })}</span>
                      <Chip size="sm" tone={verdict === "full" ? "verified" : verdict === "part" ? "hint" : "look"} icon={verdict === "full" ? "verified" : verdict === "part" ? "hint" : "look-here"}>{t(`mock.report.q.${verdict}`)}</Chip>
                      {marks.length > 0 && <span className="tabular-nums">{t("mock.report.marks", { awarded, available })}</span>}
                      {flagged.has(id) && <Chip size="sm" icon="flag-review">{t("mock.nav.flagged")}</Chip>}
                      {[...new Set(partMethods)].map((m) => <MarkingLabel key={m} method={m} />)}
                    </p>
                    {item !== null && <div className="text-ui text-ink"><PackText text={item.stem} /></div>}
                    {item?.parts.filter((p) => p.prompt !== item.stem).map((p) => <p key={p.part_id} className="text-ui text-muted"><PackInline text={p.prompt} /></p>)}
                    {item !== null && <p className="text-meta text-muted">{t("mock.report.your_answer")} <AnswerWords item={item} answer={exam.answers[id]?.answer} /></p>}
                  </div>
                  {row !== undefined && !row.correct && actions.onShowWhy !== undefined && (
                    <Button size="sm" onClick={() => actions.onShowWhy?.(row.show_me_why_ref)} data-show-me-why={row.show_me_why_ref}><Icon name="tutor" size={16} />{t("mock.report.show_me_why")}</Button>
                  )}
                </div>
                {result.past_paper !== undefined && row !== undefined && actions.onHelp !== undefined && (
                  <HelpChips onHelp={(kind) => actions.onHelp?.(row.show_me_why_ref, kind)} />
                )}
                {questionExtra?.(id, item)}
                {unresolved.map((c) => (
                  <Callout key={c.criterion_id} tone="hint" icon="info" heading={t(c.notice_key ?? "assessment.not_marked_automatically")} data-unresolved={c.criterion_id}>
                    <p>{t("mock.report.scheme")} <PackInline text={c.scheme} /></p>
                  </Callout>
                ))}
              </li>
            );
          })}
        </ol>
      </section>
      <div><Button onClick={actions.onLeave}><Icon name="go-back" size={16} />{t("mock.back")}</Button></div>
    </div>
  );
}

// --- the place as a whole ----------------------------------------------------------------------------------------------

/** The Mock exam place from its view: the gallery and the tests render this directly. */
/** An imported past paper as the sitting and the report name it (ASM-27). */
export function pastPaperShown(past: PastPaperSummary): MockPaper {
  return { paper_id: past.paper_id, subject: past.subject, level: "O", title: past.title, paper_kind: "paper_2_structured", duration_minutes: past.duration_minutes,
    total_marks: past.total_marks, questions: past.questions, calculator: past.calculator };
}

export function MockContent({ view, now, actions, subjects, extraPercent = 0, names, pastPapers = [], below, questionExtra }: {
  view: MockView; now: number; actions: MockActions; subjects: MockSubject[]; extraPercent?: number; names: ReportNames;
  /** The student's imported past papers, so their sittings are named (ASM-27). */
  pastPapers?: PastPaperSummary[];
  /** Under the papers: the student's past papers and exam dates (ASM-27, LRN-44). */
  below?: ReactNode;
  questionExtra?: ((questionId: string, item: PracticeItem | null) => ReactNode) | undefined;
}) {
  const t = useT();
  const exam = view.exam;
  const past = exam === null ? undefined : pastPapers.find((p) => p.paper_id === exam.paper_id);
  const kindLabel = past !== undefined || exam?.paper_id.startsWith("ppr_") === true ? t("past_paper.kind") : undefined;
  const paper = exam === null ? null : view.papers.find((p) => p.paper_id === exam.paper_id) ?? (past !== undefined ? pastPaperShown(past) : null);
  if (exam !== null && view.result !== null && !view.marking && (exam.state === "marked" || exam.state === "reviewed")) {
    return (
      <div {...(LOCKED_STATES.includes(exam.state) ? CAPTURE_LOCK_EVENTS : {})} className="flex min-h-full flex-col" data-sitting-state={exam.state}>
        <ScreenHeader end={<Button tone="ghost" onClick={actions.onLeave}><Icon name="go-back" size={16} />{t("mock.back")}</Button>}>
          <h1 className="min-w-0 truncate text-ui font-semibold text-ink"><PackInline text={paper?.title ?? t("mock.title")} /></h1>
          <span className="text-meta text-muted">{t("mock.report.title")}</span>
        </ScreenHeader>
        <div className="flex-1 px-8 pt-6 pb-12">
          <div className="mx-auto w-full max-w-260">
            {view.timeUp && <div className="mb-6"><Callout tone="hint" icon="timer" heading={t("mock.time_up")} /></div>}
            <MockReport exam={exam} result={view.result} paper={paper} names={names} actions={actions} kindLabel={kindLabel} questionExtra={questionExtra} />
          </div>
        </div>
      </div>
    );
  }
  if (exam !== null) return <Sitting view={view} exam={exam} now={now} actions={actions} paper={paper} kindLabel={kindLabel} />;
  return (
    <div className="flex min-h-full flex-col" data-mock-place={view.status}>
      <ScreenHeader><h1 className="text-ui font-semibold text-ink">{t("mock.title")}</h1></ScreenHeader>
      <div className="flex-1 px-8 pt-6 pb-12">
        <div className="mx-auto w-full max-w-260">
          {view.opening ? <div className="flex justify-center py-16"><Loader label={t("mock.opening")} /></div> : (
            <>
              {view.openFailed && <div className="mb-6"><ErrorState message={t("mock.open_failed")} /></div>}
              <Papers view={view} subjects={subjects} extra={extraPercent} onChoose={actions.onChoose} onOpen={actions.onOpen} onRetry={actions.onRetry} now={now}
                titleOf={(paperId) => pastPapers.find((p) => p.paper_id === paperId)?.title ?? null} />
              {below !== undefined && <div className="mt-10 flex flex-col gap-10">{below}</div>}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** Focus away from the app and large pastes during a sitting, reported as counts (ASM-18): the core keeps those over
 * its thresholds; nothing of what was pasted is sent. */
function useIntegrity(controller: MockController, open: boolean): void {
  useEffect(() => {
    if (!open) return;
    let away: number | null = null;
    const leave = () => {
      away ??= Date.now();
    };
    const back = () => {
      if (away === null) return;
      controller.integrity("focus_loss", Date.now() - away);
      away = null;
    };
    const visible = () => (document.visibilityState === "hidden" ? leave() : back());
    const paste = (event: ClipboardEvent) => controller.integrity("large_paste", event.clipboardData?.getData("text").length ?? 0);
    window.addEventListener("blur", leave);
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", visible);
    document.addEventListener("paste", paste, true);
    return () => {
      window.removeEventListener("blur", leave);
      window.removeEventListener("focus", back);
      document.removeEventListener("visibilitychange", visible);
      document.removeEventListener("paste", paste, true);
    };
  }, [controller, open]);
}

/** The Mock exam place for the unlocked profile, on its controller. */
const NO_PAST = { subscribe: () => () => undefined, getView: () => null };

export function MockPlace({ controller, subjects, extraPercent, names, onShowWhy, onPractise, onPractiseTopic, onHelp, pastPapers, examDates, questionExtra }: {
  controller: MockController; subjects: MockSubject[]; extraPercent: number; names: (subject: string) => ReportNames;
  onShowWhy?: (reference: string, subject: string) => void; onPractise?: (setRef: string, subject: string) => void;
  onPractiseTopic?: (topicId: string, subject: string) => void;
  onHelp?: (reference: string, kind: HelpKind, subject: string) => void;
  /** Imported past papers and exam dates (ASM-27, LRN-44), under the papers on this computer. */
  pastPapers?: PastPaperController; examDates?: ExamDateController;
  questionExtra?: (questionId: string, item: PracticeItem | null) => ReactNode;
}) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const past = useSyncExternalStore(pastPapers?.subscribe ?? NO_PAST.subscribe, pastPapers?.getView ?? NO_PAST.getView, pastPapers?.getView ?? NO_PAST.getView);
  const dates = useSyncExternalStore(examDates?.subscribe ?? NO_PAST.subscribe, examDates?.getView ?? NO_PAST.getView, examDates?.getView ?? NO_PAST.getView);
  useEffect(() => {
    if (past?.status === "idle") void pastPapers?.load();
  }, [pastPapers, past?.status]);
  useEffect(() => {
    if (dates?.status === "idle") void examDates?.load();
  }, [examDates, dates?.status]);
  const exam = view.exam;
  const open = exam !== null && LOCKED_STATES.includes(exam.state);
  const listedOpen = view.sittings.some((s) => s.state === "in_progress");
  const now = useNow(open || listedOpen);
  useIntegrity(controller, open);
  useEffect(() => {
    if (view.status === "idle") void controller.load();
  }, [controller, view.status]);
  // The clock is the core's: read again every `ui_mock_refresh_ms` while it runs, and asked at zero (time-up).
  const running = exam?.state === "in_progress";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void controller.refresh(), CLIENT_KEYS.ui_mock_refresh_ms);
    return () => clearInterval(timer);
  }, [controller, running]);
  const left = exam === null ? 1 : remainingAt({ state: exam.state, remainingMs: exam.remaining_ms, readAt: view.readAt }, now);
  useEffect(() => {
    if (running && left <= 0) void controller.timeUp();
  }, [controller, running, left]);
  const imported = exam === null ? undefined : past?.papers.find((p) => p.paper_id === exam.paper_id);
  const paper = exam === null ? null : view.papers.find((p) => p.paper_id === exam.paper_id) ?? (imported !== undefined ? pastPaperShown(imported) : null);
  const subject = paper?.subject ?? "";
  const reportNames = useMemo(() => names(subject), [names, subject]);
  const actions: MockActions = {
    onChoose: (paperId) => void controller.choose(paperId),
    onOpen: (examId) => void controller.open(examId),
    onStart: () => void controller.start(),
    onPause: () => void controller.pause(),
    onResume: () => void controller.resume(),
    onShow: (questionId) => controller.show(questionId),
    onAnswer: (questionId, answer) => controller.setAnswer(questionId, { answer }),
    onFlag: (questionId) => controller.toggleFlag(questionId),
    onClear: (questionId) => controller.clear(questionId),
    onSubmit: () => void controller.submit(),
    onRetryMark: () => void controller.retryMark(),
    onRetrySave: () => { void controller.flush(); },
    onRetry: () => void controller.load(),
    onLeave: () => controller.leave(),
    ...(onShowWhy !== undefined ? { onShowWhy: (reference: string) => onShowWhy(reference, subject) } : {}),
    ...(onPractise !== undefined ? { onPractise: (setRef: string) => onPractise(setRef, subject) } : {}),
    ...(onPractiseTopic !== undefined ? { onPractiseTopic: (topicId: string) => onPractiseTopic(topicId, subject) } : {}),
    onFigure: (examId: string, questionId: string) => controller.figure(examId, questionId),
    ...(onHelp !== undefined ? { onHelp: (reference: string, kind: HelpKind) => onHelp(reference, kind, subject) } : {}),
  };
  const choices: PastPaperSubjectOption[] = subjects.filter((s): s is PastPaperSubjectOption => ["mathematics", "physics", "chemistry", "biology", "accounting"].includes(s.subject));
  const locked = examLock(view) !== null;
  const below = (
    <>
      {past !== null && pastPapers !== undefined && (
        <PastPaperShelf view={past} subjects={choices} locked={locked} actions={{
          onImport: (draft) => void pastPapers.importPaper(draft), onSit: (paperId) => void controller.choose(paperId),
          onRemove: (paperId) => void pastPapers.remove(paperId), onDismiss: () => pastPapers.dismiss(), onRetry: () => void pastPapers.load(),
        }} />
      )}
      {dates !== null && examDates !== undefined && (
        <ExamDatesPanel view={dates} subjects={choices} actions={{
          onAdd: (draft) => void examDates.add(draft), onReminders: (examId, on) => void examDates.change(examId, { reminders: on }),
          onRemove: (examId) => void examDates.remove(examId),
        }} />
      )}
    </>
  );
  return <MockContent view={view} now={now} actions={actions} subjects={subjects} extraPercent={extraPercent} names={reportNames} pastPapers={past?.papers ?? []}
    below={pastPapers !== undefined || examDates !== undefined ? below : undefined} questionExtra={questionExtra} />;
}
