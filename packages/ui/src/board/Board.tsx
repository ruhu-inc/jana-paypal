// The working board (UIA-18, UXS-15): one row per step in index order, each with its maths, reason, owner mark and a
// status that pairs an icon with a word (never colour alone, DSN-04). Rows are memoised by what they show, so a
// status change re-renders one row. Up and Down move between rows. Number provenance is the engine's (AGT-10: no
// final tutor line carries an unchecked number); a student's line is what they typed, typeset as maths the way the
// tutor's lines are when it is maths input (powers, subscripts, units), otherwise text with any \\(…\\) maths, never raw HTML.
// The line the student is on is the neutral `wash` with the 3 px accent rule beside it (D89); Given and Find head the
// board (UXS-14), and a line the check found an error in carries the "look here" note in the margin (UXS-16). Every
// tutor line has a line menu with "Report a problem" (UXS-44), which reports the tutor turn that wrote the board.
// Three highlights stay distinct in shape, never by colour alone (UXS-63, DSN-04): the current teaching step (the wash
// and accent rule), the tutor's temporary pointer (a solid ring with the underline, from `board.highlight` or a spoken
// cue fired by heard playback, VOC-51) and the student's selection (a dashed outline). They can show together, and the
// screen reader names each by kind. The pointer clears with its cue (interruption, flush, session change, a revised or
// removed line); the selection clears when its line is revised or removed.
import { memo, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { CLIENT_KEYS, boardLineLatex, selectBoardLine, type AnnotationRec, type StepRec } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { MarkdownBlock } from "../markdown/Markdown.js";
import { Tex } from "../maths/Tex.js";
import { Button, EmptyState, IconButton, MenuButton, Spinner, chipLook, type ChipTone } from "../primitives/index.js";
import { shallowArray, useSelect, useStoreHolder, useSession, useReadOnly } from "../store.js";
import { ReportSheet } from "../transcript/ReportProblem.js";
import { useTutorName } from "../tutor/look.js";
import { useVisualSettings } from "../visuals/VisualViewer.js";
import { useVoicePen, useVoicePointer, useVoiceWriting } from "../voice/SpeechTools.js";
import { Pen } from "./Pen.js";
import { TermCarry } from "./TermCarry.js";
import { LineAnnotation } from "./Annotation.js";
import { WorkingEntry, type WorkingPhoto } from "./WorkingEntry.js";
import { PhotoRegions } from "./PhotoRegions.js";

/** The error categories of TABLE VER-06a, each with its "look here" words (`board.error.<category>`). */
const ERROR_CATEGORIES: readonly string[] = ["sign", "arithmetic", "dropped_term", "illegal_operation", "units", "copy", "other"];

const STATUS_ICON: Record<string, "verified" | "look-here" | "loading" | "hint" | null> = {
  correct: "verified", equivalent_unsimplified: "verified", incorrect: "look-here", checking: "loading", awaiting: "hint",
};

/** Each status's chip (TABLE UXS-16a): correct is verified, equivalent is hint, not quite is the "look here" mark, the
 * student's turn and checking are neutral; a settled tutor line with a check is verified; draft and unchecked are words. */
const STATUS_TONE: Record<string, ChipTone> = {
  correct: "verified", equivalent_unsimplified: "hint", incorrect: "look", awaiting: "neutral", checking: "neutral",
};

/** A tutor line's menu (UXS-44): "Report a problem" opens the report sheet for the turn that wrote the board. */
function LineMenu({ number, sessionId, turnId }: { number: number; sessionId: string; turnId: string }) {
  const t = useT();
  const [reporting, setReporting] = useState(false);
  return (
    <span className="ms-auto" onClick={(e) => e.stopPropagation()} data-line-menu="">
      <MenuButton label={t("board.line_menu", { index: number })} items={[{ id: "report", label: t("report.action"), onSelect: () => setReporting(true) }]}
        trigger={({ ref, ...menu }) => (
          <button ref={ref} type="button" {...menu} aria-label={t("board.line_menu", { index: number })}
            className="jana-press relative inline-flex size-8 items-center justify-center rounded-control text-muted after:absolute after:-inset-1.5 after:content-[''] hover:bg-wash hover:text-ink aria-expanded:bg-wash aria-expanded:text-ink">
            <Icon name="more" size={16} />
          </button>
        )} />
      <ReportSheet sessionId={sessionId} turnIds={[turnId]} open={reporting} onClose={() => setReporting(false)} />
    </span>
  );
}

/** Reading pace of a write-on without voice (UXS-55): per character, capped per line. */
export const WRITE_ON_MS_PER_CHAR = 40;
export const WRITE_ON_MAX_MS = 1200;

/** The write-on mask over a new tutor line (UXS-55, TABLE DSN-10a): moved by `transform` over the rendered maths, never
 * a stroke trace; presentation only, so the line is selectable and read in full from its arrival. In voice mode the
 * reveal follows the words that cite the line (`progress`); otherwise it runs at a reading pace. */
function WriteOn({ latex, progress, current }: { latex: string; progress: number | null; current: boolean }) {
  const [paced, setPaced] = useState(0);
  useEffect(() => {
    if (progress !== null) return undefined;
    const frame = requestAnimationFrame(() => setPaced(1));
    return () => cancelAnimationFrame(frame);
  }, [progress]);
  const shown = progress ?? paced;
  const duration = Math.min(WRITE_ON_MAX_MS, WRITE_ON_MS_PER_CHAR * latex.length);
  if (shown >= 1 && progress !== null) return null;
  return (
    <span aria-hidden="true" data-write-on={progress === null ? "paced" : "voice"} data-write-on-ms={progress === null ? duration : undefined}
      className={`pointer-events-none absolute inset-0 origin-right ${current ? "bg-board-current" : "bg-canvas"}`}
      style={{ transform: `scaleX(${1 - shown})`, transition: progress === null ? `transform ${duration}ms linear` : undefined }} />
  );
}

export const BoardStep = memo(function BoardStep({ step, number, current, highlighted = false, selected = false, onSelect, onEdit, onWhy, onTermSelect, editing = false, sessionId, reportTurn = null, annotation = null, dimmed = false, writeOn = null }: {
  /** A new tutor line written on (UXS-55): `paced` at a reading pace, a number for the voice's progress, null shown whole. */
  writeOn?: "paced" | number | null;
  /** A term or range selected on this line (UXS-58), or null when cleared: reported so "this" refers to it. */
  onTermSelect?: (ref: string | null) => void;
  step: StepRec; number: number; current: boolean;
  /** Dimmed by the tutor's spotlight on another line (UXS-57): opacity only. */
  dimmed?: boolean;
  /** The tutor's annotation when it targets this line (VSL-40). */
  annotation?: AnnotationRec | null;
  /** The tutor's temporary pointer is on this line (UXS-63). */
  highlighted?: boolean;
  /** The student selected this line (UXS-63, UXS-58). */
  selected?: boolean;
  onSelect?: (stepId: string) => void;
  onEdit?: (stepId: string | null) => void; editing?: boolean; sessionId?: string;
  /** "Why?" on the line (UXS-15): asks the tutor why it holds; offered on a line with something written on it. */
  onWhy?: (stepId: string) => void;
  /** The tutor turn a report on this line names; without one (or a session) the line has no menu. */
  reportTurn?: string | null;
}) {
  const t = useT();
  const checked = step.status === "final" && step.check_id !== null;
  const icon = STATUS_ICON[step.status] ?? (checked ? "verified" : null);
  const tone = STATUS_TONE[step.status] ?? (checked ? "verified" : null);
  // A tutor line is marked with the profile's tutor name, Jana by default, as the turn labels are, in the visible mark
  // and the row's screen-reader name; a student line keeps "You" (UXS-15, D105, D108).
  const tutorName = useTutorName();
  const owner = step.owner === "student" ? t("message.you") : tutorName;
  const status = t(step.status === "checking" && step.owner === "student" ? "board.status.checking_student" : `board.status.${step.status}`);
  // The "look here" note in the margin: the first error the check named (UXS-16, TABLE VER-06a), in the words of its
  // message key, else of its category; a core that names the category as the line's reason gets the same words.
  const category = step.error?.category ?? (step.status === "incorrect" && step.reason !== null && ERROR_CATEGORIES.includes(step.reason) ? step.reason : null);
  const keyed = step.error === null ? null : t(step.error.message_key);
  const note = keyed !== null && keyed !== step.error?.message_key ? keyed : category !== null && ERROR_CATEGORIES.includes(category) ? t(`board.error.${category}`) : null;
  const reason = category !== null && step.reason === category ? null : step.reason;
  const rule = current ? "bg-board-current before:absolute before:inset-y-3.5 before:start-0 before:w-(--border-stepRule) before:rounded-pill before:bg-accent before:content-['']" : "";
  const numberInk = current ? "font-semibold text-ink" : step.status === "incorrect" ? "text-misconception" : "text-faint";
  const student = step.owner === "student";
  const typed = student ? boardLineLatex(step.latex) : null;
  // Each highlight kind is named in words (UXS-63), so the screen reader never depends on the look: the current step by
  // `aria-current="step"` (read as "current step"), the pointer and the selection in the line's name.
  const kinds = [highlighted ? t("board.kind.showing") : null, selected ? t("board.kind.selected") : null]
    .filter((k): k is string => k !== null);
  // Pointing back at a term (UXS-58): with the line focused, Left and Right move between its terms and Shift extends
  // the range; the selection is named in words and "Why this?" asks about exactly that range.
  const terms = step.terms ?? [];
  const [range, setRange] = useState<{ anchor: number; focus: number } | null>(null);
  const termRange = range === null ? null : `t${Math.min(range.anchor, range.focus) + 1}${range.anchor === range.focus ? "" : `-t${Math.max(range.anchor, range.focus) + 1}`}`;
  const fromTerm = range === null ? undefined : terms[Math.min(range.anchor, range.focus)];
  const toTerm = range === null ? undefined : terms[Math.max(range.anchor, range.focus)];
  const termText = fromTerm === undefined || toTerm === undefined ? null : step.latex.slice(fromTerm.start, toTerm.end);
  const moveTerm = (e: { key: string; shiftKey: boolean; preventDefault(): void }): boolean => {
    if ((e.key !== "ArrowLeft" && e.key !== "ArrowRight") || terms.length === 0) return false;
    e.preventDefault();
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const next = range === null ? (delta > 0 ? 0 : terms.length - 1) : Math.max(0, Math.min(terms.length - 1, range.focus + delta));
    const chosen = e.shiftKey && range !== null ? { anchor: range.anchor, focus: next } : { anchor: next, focus: next };
    setRange(chosen);
    const id = `t${Math.min(chosen.anchor, chosen.focus) + 1}${chosen.anchor === chosen.focus ? "" : `-t${Math.max(chosen.anchor, chosen.focus) + 1}`}`;
    onTermSelect?.(`${step.step_id}.${id}`);
    return true;
  };
  return (
    <li
      tabIndex={-1}
      data-step-id={step.step_id}
      data-status={step.status}
      aria-label={`${t("board.step.number", { index: number })}, ${owner}, ${step.latex}, ${status}${kinds.length > 0 ? `, ${kinds.join(", ")}` : ""}`}
      aria-current={current ? "step" : undefined}
      data-highlighted={highlighted ? "true" : undefined}
      data-selected={selected ? "true" : undefined}
      data-current={current ? "true" : undefined}
      data-dimmed={dimmed ? "true" : undefined}
      style={dimmed ? { opacity: CLIENT_KEYS.board_spotlight_dim_opacity } : undefined}
      onClick={onSelect === undefined ? undefined : () => onSelect(step.step_id)}
      onKeyDown={(e) => {
        if ((e.target as HTMLElement).closest('[role="textbox"],input,button') !== null) return;
        if (moveTerm(e)) return;
        if (e.key === "Escape" && range !== null) { setRange(null); onTermSelect?.(null); return; }
        if (e.key === "Enter") onSelect?.(step.step_id);
        if (e.key.toLowerCase() === "e" && student) { e.preventDefault(); onEdit?.(step.step_id); }
      }}
      className={`relative grid grid-cols-[2rem_1fr] gap-3 rounded-card py-4 ps-3 pe-4 ${rule} ${highlighted ? "jana-pointer" : ""} ${selected ? "jana-selected" : ""}`}
    >
      <span className={`h-8 text-center text-meta leading-8 font-medium tabular-nums ${numberInk}`}>{number}</span>
      <span className="grid min-w-0 gap-1">
        <span className={`board-math relative block overflow-x-auto text-ink ${highlighted ? "underline decoration-highlight-line decoration-2 underline-offset-4" : ""}`}>
          {writeOn !== null && step.owner === "tutor" && <WriteOn latex={step.latex} progress={writeOn === "paced" ? null : writeOn} current={current} />}
          {student && typed === null ? (
            <span className="[&_p]:my-0"><MarkdownBlock text={step.latex} /></span>
          ) : student ? (
            <Tex tex={typed ?? ""} display={false} />
          ) : (
            <Tex tex={step.latex} display={false} />
          )}
        </span>
        {termRange !== null && (
          <span data-term-selection={termRange} className="flex flex-wrap items-center gap-2 text-ui">
            <span className="rounded-control bg-highlight-soft px-1 font-mono underline" aria-hidden="true">{termText}</span>
            {onWhy !== undefined && <Button size="sm" tone="ghost" onClick={(e) => { e.stopPropagation(); onWhy(`${step.step_id}.${termRange}`); }}>{t("board.why_this")}</Button>}
            <span role="status" className="sr-only">{t("board.term_selected", { term: termText ?? "" })}</span>
          </span>
        )}
        {annotation !== null && <LineAnnotation step={step} annotation={annotation} />}
        {reason !== null && <span className="block text-ui text-muted">{reason}</span>}
        {note !== null && <span data-look-here="" className="block font-hand text-heading text-misconception">{note}</span>}
        <span className="mt-1 flex flex-wrap items-center gap-2">
          <span className={`inline-flex items-center gap-1.5 text-label font-medium ${student ? "text-board-student-line" : "text-board-tutor-line"}`}>
            <span aria-hidden="true" className={`size-1.5 rounded-pill ${student ? "bg-board-student-line" : "bg-board-tutor-line"}`} />
            {student ? owner : <span data-tutor-name="">{owner}</span>}
          </span>
          <span className={tone === null ? "inline-flex items-center gap-1 text-label font-medium text-muted" : chipLook(tone, "sm")} {...(step.check_id !== null ? { "aria-description": t("board.checked_by"), "data-check-id": step.check_id } : {})}>
            {icon !== null && (icon === "loading" ? <Spinner /> : <Icon name={icon} size={16} />)}
            {status}{checked && <span> · {t("board.status.correct")}</span>}
          </span>
          {onWhy !== undefined && step.latex.trim() !== "" && step.status !== "checking" && <Button size="sm" tone="ghost" aria-label={t("board.why_label", { index: number })} onClick={(e) => { e.stopPropagation(); onWhy(step.step_id); }}>{t("board.why")}</Button>}
          {onEdit !== undefined && student && <Button size="sm" tone="ghost" onClick={(e) => { e.stopPropagation(); onEdit(step.step_id); }}>{t("input.edit")}</Button>}
          {!student && reportTurn !== null && sessionId !== undefined && <LineMenu number={number} sessionId={sessionId} turnId={reportTurn} />}
        </span>
        {editing && sessionId !== undefined && <WorkingEntry sessionId={sessionId} step={step} onClose={() => onEdit?.(null)} />}
      </span>
    </li>
  );
});

interface KnownView {
  known_id?: string;
  symbol: string;
  value?: string;
  unit: string | null;
}

// A quantity named in words ("initial_velocity", "velocity after 2s (m/s)", "speed") is text: as TeX its letters run
// together as one italic product. Symbols ("v_0", "\Delta x", "KE") stay TeX.
const named = (symbol: string) => /[A-Za-z]{3,}/.test(symbol) && !/[\\^{}]/.test(symbol);
const NUMBER = /^[-\u2212]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$/;
const compact = (text: string) => text.replace(/\s+/g, "");
const powers = (text: string) => text.replace(/\^2\b/g, "\u00b2").replace(/\^3\b/g, "\u00b3");

/** Given and Find (UXS-14): the knowns from the question's parse, before the model answers, and what to find. */
const Knowns = memo(function Knowns({ knowns, unknowns }: { knowns: readonly KnownView[]; unknowns: readonly KnownView[] }) {
  const t = useT();
  if (knowns.length === 0 && unknowns.length === 0) return null;
  const term = (k: KnownView, key: string, label: string | null, find: boolean) => {
    // A parse may give an equation as both the symbol and its value ("2x+3=11" = "2x + 3 = 11"): it is shown once.
    const value = find || (k.value !== undefined && compact(k.value) === compact(k.symbol)) ? undefined : k.value?.trim();
    // A parse may write the unit into the value as well ("14 m/s" with unit "m/s"): it is shown once.
    const unit = !find && k.unit !== null && k.unit.trim().toLowerCase() !== "none" && !(value !== undefined && compact(value).endsWith(compact(k.unit))) ? k.unit : null;
    return (
      <span key={key} className="inline-flex items-baseline gap-1">
        {label !== null && <span className="me-1 font-ui text-label text-faint">{label}</span>}
        {named(k.symbol) ? <span className="font-ui">{k.symbol.trim().replace(/_/g, " ")}</span> : <Tex tex={k.symbol} />}
        {value !== undefined && <>
          <span>=</span>
          {NUMBER.test(value) ? <Tex tex={value.replace("\u2212", "-")} /> : <span className="font-ui">{powers(value)}</span>}
        </>}
        {unit !== null && <span>{powers(unit)}</span>}
      </span>
    );
  };
  return (
    <div data-knowns="" className="flex flex-wrap gap-x-5 gap-y-2 rounded-card bg-chrome px-4 py-3 text-body">
      {knowns.map((k, i) => term(k, `k:${k.known_id ?? i}`, i === 0 ? t("board.given") : null, false))}
      {unknowns.map((k, i) => term(k, `u:${k.symbol}:${i}`, i === 0 ? t("board.find") : null, true))}
    </div>
  );
});

/** The working board in its panel: the header with its title and, where the board can be closed, the close button;
 * Given and Find; the lines; the student's entry. */
export function Board({ sessionId, onClose }: { sessionId: string; onClose?: () => void }) {
  const t = useT();
  const session = useSession();
  const readOnly = useReadOnly();
  const [photoInput, setPhotoInput] = useState<WorkingPhoto | null>(null);
  const [draggingPhoto, setDraggingPhoto] = useState(false);
  useEffect(() => { setPhotoInput(null); setDraggingPhoto(false); }, [sessionId]);
  const photoReady = useSelect((s) => {
    const turns = s.sessions[sessionId]?.turn_ids ?? [];
    return turns.length > 0 && !turns.some((id) => s.turns[id]?.status === "streaming");
  });
  const [editing, setEditing] = useState<string | null>(null);
  const edit = useCallback((stepId: string | null) => setEditing(stepId), []);
  const boardId = useSelect((s) => {
    const ids = s.sessions[sessionId]?.board_ids ?? [];
    return ids[ids.length - 1] ?? null;
  });
  const stepIds = useSelect((s) => (boardId === null ? NONE : (s.boards[boardId]?.step_ids ?? NONE)), shallowArray);
  const steps = useSelect((s) => (boardId === null ? NO_STEPS : stepIds.map((id) => s.boards[boardId]?.steps[id]).filter((x): x is StepRec => x !== undefined)), shallowArray);
  const knowns = useSelect((s) => (boardId === null ? NO_KNOWNS : ((s.boards[boardId]?.knowns ?? NO_KNOWNS) as KnownView[])));
  const unknowns = useSelect((s) => (boardId === null ? NO_KNOWNS : ((s.boards[boardId]?.unknowns ?? NO_KNOWNS) as KnownView[])));
  const awaiting = useSelect((s) => (boardId === null ? null : (s.boards[boardId]?.await?.step_id ?? null)));
  const [entryOpen, setEntryOpen] = useState(steps.length === 0);
  // Lines the tutor or a bound visual points at (board.highlight, VSL-20), until their time to live runs out.
  const highlight = useSelect((s) => (boardId === null ? null : (s.boards[boardId]?.highlight ?? null)));
  // A new tutor line is written on (UXS-55): in time with the words that cite it in voice mode, otherwise at a reading
  // pace; lines already there when the board opens, reduced motion and low power show whole.
  const seenAtOpen = useRef<Set<string> | null>(null);
  if (seenAtOpen.current === null) seenAtOpen.current = new Set(stepIds);
  const { reducedMotion, lowPower } = useVisualSettings();
  const writing = useVoiceWriting();
  const pen = useVoicePen();
  const boardBox = useRef<HTMLElement | null>(null);
  const writeOnOf = (step: StepRec): "paced" | number | null => {
    if (reducedMotion || step.owner !== "tutor" || seenAtOpen.current?.has(step.step_id) === true) return null;
    return writing !== null && writing.step_id === step.step_id ? writing.progress : "paced";
  };
  // Term carry (UXS-61): the newest line's verified moves, played once when it arrives; reduced motion and low power
  // show the line at once.
  const newest = steps[steps.length - 1];
  const carry = newest !== undefined && (newest.term_moves?.length ?? 0) > 0 && !reducedMotion && lowPower !== true
    && seenAtOpen.current?.has(newest.step_id) !== true ? newest : null;
  // The tutor's one annotation, drawn on the line it targets by id (VSL-40).
  const annotation = useSelect((s) => (boardId === null ? null : (s.boards[boardId]?.annotation ?? null)));
  // A spotlight (UXS-57) dims the other lines until the next utterance or any student action on the board.
  const [actedAt, setActedAt] = useState<number | null>(null);
  const spotlight = highlight?.spotlight === true && actedAt !== highlight.ts;
  const lit = highlight !== null && (highlight.ttl_ms === null || Date.now() - highlight.ts < highlight.ttl_ms) ? highlight.step_ids : NONE;
  // The spoken cue's pointer (VOC-10, VOC-51): fired by what the student has heard, never by the caption arriving.
  const pointer = useVoicePointer();
  const pointed = pointer?.target.kind === "step" ? pointer.target.step_id : null;
  const pointedNumber = pointed === null ? 0 : stepIds.indexOf(pointed) + 1;
  // Places on the student's own photo (JM-D10): the lit line or sketch part, and a spoken cue's photo region.
  const photoLit = useMemo(() => {
    const live = highlight !== null && (highlight.ttl_ms === null || Date.now() - highlight.ts < highlight.ttl_ms) ? highlight.photo_regions ?? NONE : NONE;
    const cued = pointer?.target.kind === "photo_region" ? [`${pointer.target.capture_id}:${pointer.target.region_id}`] : NONE;
    return [...live, ...cued];
  }, [highlight, pointer]);
  const photoCapture = photoLit[0]?.split(":")[0] ?? null;
  // The student's selection (UXS-63): the line and the maths it had, so a revision or removal clears it.
  const [selection, setSelection] = useState<{ step_id: string; latex: string } | null>(null);
  const chosen = selection !== null && steps.some((s) => s.step_id === selection.step_id && s.latex === selection.latex) ? selection.step_id : null;
  // The current teaching step: the line the board is on (Back, Forward, a bound visual step, VSL-20), else the line the
  // tutor waits on.
  const focused = useSelect((s) => (boardId === null ? null : (s.boards[boardId]?.current ?? null)));
  const current = focused !== null && stepIds.includes(focused) ? focused : awaiting;
  // The turn a report on a tutor line names: the one that opened the board, else the session's latest reply.
  const reportTurn = useSelect((s) => {
    const partId = boardId === null ? null : (s.boards[boardId]?.part_id ?? null);
    const opened = partId === null ? undefined : s.parts[partId]?.turn_id;
    const turns = s.sessions[sessionId]?.turn_ids ?? [];
    return opened ?? turns[turns.length - 1] ?? null;
  });
  const holder = useStoreHolder();
  const why = useCallback((stepId: string) => { session?.askWhy(stepId, t(stepId.includes(".") ? "board.why_this" : "board.why")); }, [session, t]);
  const termSelect = useCallback((ref: string | null) => { void session?.setSelection(ref).catch(() => undefined); }, [session]);
  // The lines as of now, read when a line is selected, so selecting never re-renders every row (UIA-18).
  const latest = useRef(steps);
  latest.current = steps;
  const select = useCallback((stepId: string) => {
    const line = latest.current.find((s) => s.step_id === stepId);
    setSelection(line === undefined ? null : { step_id: stepId, latex: line.latex });
    if (boardId !== null) holder.update((s) => selectBoardLine(s, boardId, stepId));
  }, [holder, boardId]);
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    // Move only the board's reading pane. scrollIntoView also scrolls hidden
    // ancestors and can pull the shared header behind the preview bar.
    const row = [...(list.current?.querySelectorAll<HTMLElement>("li[data-step-id]") ?? [])]
      .find((line) => line.dataset.stepId === current);
    const pane = list.current?.closest<HTMLElement>("[data-board-scroll]");
    if (row === undefined || pane == null) return;
    const reveal = () => {
      const bounds = pane.getBoundingClientRect(), step = row.getBoundingClientRect();
      if (step.top < bounds.top) pane.scrollTop += step.top - bounds.top;
      else if (step.bottom > bounds.bottom) pane.scrollTop += step.bottom - bounds.bottom;
    };
    reveal();
    // Reflow can move the current line below the fold without changing its id.
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(reveal);
    observer.observe(pane); observer.observe(row);
    return () => observer.disconnect();
  }, [current]);
  const onKey = (e: KeyboardEvent<HTMLOListElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = [...(list.current?.querySelectorAll<HTMLElement>("li[data-step-id]") ?? [])];
    const at = rows.indexOf(document.activeElement as HTMLElement);
    const next = rows[Math.min(rows.length - 1, Math.max(0, at + (e.key === "ArrowDown" ? 1 : -1)))];
    if (next !== undefined) {
      e.preventDefault();
      next.focus();
    }
  };
  return (
    <section ref={boardBox} aria-label={t("board.title")} className="relative flex min-h-0 flex-1 flex-col bg-canvas"
      onPointerDownCapture={() => { if (highlight?.spotlight === true) setActedAt(highlight.ts); }}
      onKeyDownCapture={() => { if (highlight?.spotlight === true) setActedAt(highlight.ts); }}
      onDragOverCapture={(event) => {
      if (session !== null && photoReady && event.dataTransfer.types.includes("Files") && !(event.target instanceof Element && event.target.closest('[role="dialog"]'))) { event.preventDefault(); setDraggingPhoto(true); }
    }} onDragLeave={(event) => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDraggingPhoto(false); }} onDropCapture={(event) => {
      setDraggingPhoto(false);
      if (session === null || !photoReady || event.target instanceof Element && event.target.closest('[role="dialog"]')) return;
      const file = event.dataTransfer.files[0];
      if (file?.type.startsWith("image/")) { event.preventDefault(); event.stopPropagation(); setPhotoInput({ sessionId, file, source: "photo_drop" }); }
    }}>
      {draggingPhoto && <p role="status" className="pointer-events-none absolute inset-4 z-popover border-2 border-dashed border-focus bg-panel p-4 text-ui">{t("photo.drop_here")}</p>}
      <div className="flex min-h-(--layout-headerHeight) shrink-0 items-center gap-2 ps-5 pe-3">
        <h2 className="flex-1 text-ui font-semibold">{t("board.title")}</h2>
        {onClose !== undefined && <IconButton icon="dismiss" size="sm" label={t("board.close")} onClick={onClose} />}
      </div>
      {steps.length > 1 && <BoardToolbar stepIds={stepIds} current={current} onSelect={select} />}
      <div data-board-scroll="" className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pt-2 pb-6">
        <Knowns knowns={knowns} unknowns={unknowns} />
        {photoCapture !== null && <PhotoRegions sessionId={sessionId} captureId={photoCapture} lit={photoLit} />}
        {steps.length === 0 ? (
          <EmptyState message={t("board.empty")} />
        ) : (
          <ol ref={list} tabIndex={0} onKeyDown={onKey} className="flex flex-col gap-1" aria-label={t("board.title")}>
            {steps.map((step, i) => <BoardStep key={step.step_id} step={step} number={i + 1} current={step.step_id === current} highlighted={lit.includes(step.step_id) || step.step_id === pointed} selected={step.step_id === chosen} annotation={annotation !== null && (annotation.target.step_id === step.step_id || annotation.to?.step_id === step.step_id) ? annotation : null} dimmed={spotlight && lit.length > 0 && !lit.includes(step.step_id)} writeOn={writeOnOf(step)} onSelect={select} sessionId={sessionId} reportTurn={readOnly ? null : reportTurn} {...(session !== null ? { onEdit: edit, onWhy: why, onTermSelect: termSelect, editing: editing === step.step_id } : {})} />)}
          </ol>
        )}
      </div>
      <Pen pen={pen} container={boardBox} reducedMotion={reducedMotion} lowPower={lowPower === true} />
      {carry !== null && <TermCarry key={carry.step_id} step={carry} steps={steps} container={boardBox} />}
      {/* The pointer in words for a screen reader (VOC-17, UXS-63): "Showing: step 2", once per pointer. */}
      <p className="sr-only" aria-live="polite" data-board-showing="">{pointedNumber > 0 ? t("board.showing", { index: pointedNumber }) : ""}</p>
      {session !== null && <details data-working-entry="" className="shrink-0 border-t border-line" open={entryOpen || photoInput !== null}
        onToggle={(event) => setEntryOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer px-4 py-3 text-meta font-medium">{t("input.working")}</summary>
        {/* Keep the editor and draft mounted, but exclude folded content from
            overflow/focus scrolling as well as the native disclosure's paint. */}
        <div hidden={!entryOpen && photoInput === null}>
          <WorkingEntry sessionId={sessionId} firstLine={steps.length + 1} photoInput={photoInput?.sessionId === sessionId ? photoInput : null} onPhotoClose={() => setPhotoInput(null)} />
        </div>
      </details>}
    </section>
  );
}

const NONE: string[] = [];
const NO_STEPS: StepRec[] = [];
const NO_KNOWNS: KnownView[] = [];


/** Board controls and scrubber share the line-selection action used by bound visuals. */
export function BoardToolbar({ stepIds, current, onSelect }: { stepIds: string[]; current: string | null; onSelect: (id: string) => void }) {
  const t = useT(); const at = Math.max(0, stepIds.indexOf(current ?? ""));
  const go = (index: number) => { const id = stepIds[Math.max(0, Math.min(stepIds.length - 1, index))]; if (id) onSelect(id); };
  // One quiet row: the arrows either side of "Step 2 of 4", and the scrubber for long boards (redesign, D109).
  return <div role="group" aria-label={t("board.steps")} data-board-toolbar="" className="flex items-center gap-1 px-3">
    <IconButton icon="go-back" size="sm" label={t("board.previous_step")} disabled={at === 0} onClick={() => go(at - 1)} />
    <span className="shrink-0 text-meta whitespace-nowrap text-muted tabular-nums">{t("board.step_of", { n: at + 1, m: stepIds.length })}</span>
    <IconButton icon="go-next" size="sm" label={t("board.next_step")} disabled={at >= stepIds.length - 1} onClick={() => go(at + 1)} />
    <input aria-label={t("board.steps")} type="range" min={1} max={stepIds.length} value={at + 1} onChange={(event) => go(Number(event.target.value) - 1)} className="ms-2 min-h-11 min-w-16 flex-1 accent-ink" />
  </div>;
}
