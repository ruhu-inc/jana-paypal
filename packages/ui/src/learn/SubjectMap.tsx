// The subject map, Learn (UXS-22, LRN-34, DSN-23; direction A "Studio"): the 56 px header names the place and carries
// the subject switch; under it the subject in display type with how many of its topics are secure, the lesson in
// progress when there is one ("Continue") or else the next lesson to take ("Next up", the first lesson of the first
// topic not yet secure), then one subject's topics as rows grouped by syllabus section. Each section is a disclosure:
// the section of the lesson in progress or next up (or of a topic opened by a deep link) is open, the others closed,
// so the page is a short list and not a catalogue. A topic is one row, one button: its title, the first TABLE UXS-22a
// label whose rule holds over its KCs (fill, pattern, icon and word, never colour alone) beside the mastery ring, a
// segmented bar of its KCs by LRN-34 band with the counts in words, and how many lessons it has. A topic with no
// progress shows only its mark ("Not started"), and a topic with no lessons no lesson count. Selecting a row opens the
// topic sheet beside the rows: the topic's lessons (each one row to start it), practice and "Ask the tutor about this",
// every KC with its own TABLE LRN-09a label, "Check my mastery" on a KC that is nearly secure and "I already know this"
// on one that is not secure. An action the app cannot offer yet (no handler) is not shown, never a dead button.
// States: canonical, loading (skeleton rows), empty (no pack, or no topics), error-retriable (the read failed) and the
// unverified pack. When the map carries board syllabuses (CUR-06, D113) a "Syllabus" switch shows every topic or one
// syllabus's: a chosen syllabus hides the topics outside it (and a section left empty), each row shows its codes, and
// a line under the switch names every board shown as not endorsing Jana. The topic sheet lists the topic's syllabus
// points, "Partly covers" on a partial one.
import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent, type ReactNode } from "react";
import { syllabusCodes, topicLabel, type ConceptMapController, type ConceptMapView, type ConceptNode, type MapKc, type MapLesson, type MapSection, type MapSyllabus, type MapTopic, type MapView, type SubjectMapController } from "@jana/client-core";
import { ConceptMapContent, type ConceptActions, type ConceptLens } from "./ConceptMap.js";
import { useVoicePointer } from "../voice/SpeechTools.js";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, EmptyState, ErrorState, IconButton, ProgressBar, SegmentedControl, Skeleton, SuggestionChip } from "../primitives/index.js";
import { MasteryMark, MasteryRing, SegmentedBar, useCountsText } from "./Mastery.js";

export interface MapActions {
  onJourney?: (topic: MapTopic, kc: MapKc) => void;
  onStartLesson?: (lessonId: string) => void;
  onPractise?: (topic: MapTopic) => void;
  onAsk?: (topic: MapTopic) => void;
  /** "Check my mastery" (`now` false) and "I already know this" (`now` true), both the mastery check of UXS-41. */
  onMasteryCheck?: (kc: MapKc, now: boolean) => void;
}

export interface SubjectChoice {
  subjectId: string;
  name: string;
}

/** The lesson the student is part way through, offered above the topics with "Continue". */
export interface LessonInProgress {
  lessonId: string;
  title: string;
  /** The part shown, from 1, and how many parts the lesson has. */
  part: number;
  parts: number;
}

/** Most options a segmented control holds (DSN-12); more subjects than this are chips. */
const SEGMENTS_MAX = 4;

/** The value the syllabus switch uses for "All topics" (scheme ids never start with a dash). */
const ALL = "-all";

/** One topic as one row, one button: the whole row opens the topic; its lessons are inside the opened topic. */
function TopicRow({ topic, onOpen, selected, syllabus }: { topic: MapTopic; onOpen: (topic: MapTopic, from: HTMLButtonElement) => void; selected: boolean; syllabus: MapSyllabus | null }) {
  const t = useT();
  const codes = syllabus === null ? [] : syllabusCodes(topic, syllabus.scheme).map((p) => p.code);
  const counts = useCountsText()(topic.kcs);
  const label = topicLabel(topic.kcs.map((k) => k.state));
  // An untouched topic says so once, with its mark; the counts, the bar and the ring appear with the first progress.
  const started = topic.kcs.some((k) => k.band !== "not_started");
  return (
    <li>
      <button
        type="button"
        data-topic-id={topic.node_id}
        data-label={label}
        aria-expanded={selected}
        onClick={(e) => onOpen(topic, e.currentTarget)}
        className="jana-press flex min-h-11 w-full flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-start hover:bg-wash aria-expanded:bg-wash"
      >
        <span className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
          <span className="text-body font-semibold text-ink">{topic.title}</span>
          {syllabus !== null && codes.length > 0 && (
            <span className="text-meta text-muted" data-syllabus-codes={codes.join(" ")}>{t("map.syllabus.codes", { code: syllabus.syllabus_code, points: codes.join(", ") })}</span>
          )}
          {started && <span className="text-meta text-muted">{counts}</span>}
        </span>
        {started && <span className="w-36 shrink-0"><SegmentedBar kcs={topic.kcs} /></span>}
        <span className="flex items-center gap-2">
          {started && <MasteryRing kcs={topic.kcs} />}
          <MasteryMark state={label} />
        </span>
        {topic.lessons.length > 0 && <span className="text-meta text-faint">{t("map.lessons", { count: topic.lessons.length })}</span>}
        <span aria-hidden="true" className="text-faint"><Icon name={selected ? "collapse" : "expand"} size={16} /></span>
      </button>
    </li>
  );
}

/** One syllabus section as a disclosure: its heading is the button, its topics the rows inside. */
function Section({ section, open, onToggle, children }: { section: MapSection; open: boolean; onToggle: () => void; children: ReactNode }) {
  const t = useT();
  const id = `section-${section.node_id}`;
  const secure = section.topics.filter((tp) => topicLabel(tp.kcs.map((k) => k.state)) === "mastered").length;
  return (
    <section aria-labelledby={id} data-section-id={section.node_id} data-section-open={open} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <h2 className="text-heading text-ink">
          <button type="button" id={id} aria-expanded={open} aria-controls={`${id}-topics`} onClick={onToggle}
            className="jana-press -ms-2 flex min-h-11 items-center gap-2 rounded-control px-2 text-start hover:bg-wash">
            <span aria-hidden="true" className="text-faint"><Icon name={open ? "collapse" : "expand"} /></span>
            {section.title}
          </button>
        </h2>
        <span className="text-meta text-muted">{t("map.section.secure", { count: secure, total: section.topics.length })}</span>
      </div>
      <div id={`${id}-topics`} className="jana-disclosure" data-open={open}>
        <div className="overflow-hidden" inert={!open} aria-hidden={!open}>
          <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas">{children}</ul>
        </div>
      </div>
    </section>
  );
}

/** The lesson to take next: the first lesson of the first topic, in syllabus order, that is not yet secure. */
function nextLesson(sections: MapSection[]): { section: MapSection; topic: MapTopic; lesson: MapLesson } | null {
  for (const section of sections) {
    for (const topic of section.topics) {
      const lesson = topic.lessons[0];
      if (lesson !== undefined && topicLabel(topic.kcs.map((k) => k.state)) !== "mastered") return { section, topic, lesson };
    }
  }
  return null;
}

function TopicSheet({ topic, actions, onClose, syllabuses }: { topic: MapTopic; actions: MapActions; onClose: () => void; syllabuses: MapSyllabus[] }) {
  const t = useT();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), [topic.node_id]);
  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };
  return (
    <section role="dialog" aria-modal="false" aria-labelledby={id} onKeyDown={onKey} data-topic-sheet={topic.node_id}
      className="jana-reveal flex flex-col gap-5 rounded-card border border-line bg-canvas p-5 lg:sticky lg:top-4">
      <div className="flex items-start justify-between gap-2">
        <h2 id={id} ref={heading} tabIndex={-1} className="text-heading text-ink">{topic.title}</h2>
        <span className="-me-2 -mt-1"><IconButton icon="dismiss" size="sm" label={t("sheet.close")} onClick={onClose} /></span>
      </div>
      <div className="flex flex-col gap-3">
        {topic.lessons.length === 0 ? (
          <p className="text-ui text-muted">{t("sheet.no_lesson")}</p>
        ) : (
          <>
            <h3 className="text-label text-faint">{t("sheet.lessons")}</h3>
            <ul className="flex flex-col gap-2">
              {topic.lessons.map((lesson) => (
                <li key={lesson.lesson_id} data-lesson-id={lesson.lesson_id}>
                  {actions.onStartLesson !== undefined ? (
                    <button type="button" onClick={() => actions.onStartLesson?.(lesson.lesson_id)}
                      className="jana-press flex min-h-11 w-full items-center gap-3 rounded-control bg-panel px-3 py-2 text-start text-ui text-ink hover:bg-wash">
                      <span aria-hidden="true" className="shrink-0 text-muted"><Icon name="learn" size={16} /></span>
                      <span className="flex-1">{t("sheet.lesson_meta", { title: lesson.title, minutes: lesson.estimated_minutes })}</span>
                      <span aria-hidden="true" className="shrink-0 text-faint"><Icon name="go-next" size={16} /></span>
                    </button>
                  ) : (
                    <span className="flex min-h-11 items-center rounded-control bg-panel px-3 py-2 text-ui text-ink">{t("sheet.lesson_meta", { title: lesson.title, minutes: lesson.estimated_minutes })}</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
        {(actions.onPractise !== undefined || actions.onAsk !== undefined) && (
          <div className="flex flex-wrap gap-2">
            {actions.onPractise !== undefined && (
              <Button onClick={() => actions.onPractise?.(topic)}>
                <Icon name="practice" size={16} />
                {t("sheet.practise")}
              </Button>
            )}
            {actions.onAsk !== undefined && (
              <Button onClick={() => actions.onAsk?.(topic)}>
                <Icon name="ask" size={16} />
                {t("sheet.ask")}
              </Button>
            )}
          </div>
        )}
      </div>
      <SyllabusPoints topic={topic} syllabuses={syllabuses} />
      <div className="flex flex-col gap-2">
        <h3 className="text-label text-faint">{t("sheet.kcs")}</h3>
        <ul className="flex flex-col divide-y divide-line">
          {topic.kcs.map((kc) => (
            <li key={kc.kc_id} data-kc-id={kc.kc_id} data-state={kc.state} className="flex flex-col gap-2 py-3">
              <span className="text-ui text-ink">{kc.statement}</span>
              <span className="flex flex-wrap items-center gap-2">
                <MasteryMark state={kc.state} />
                {actions.onJourney && <Button size="sm" onClick={() => actions.onJourney?.(topic, kc)}>{t("journey.begin")}</Button>}
                {kc.state === "ready_to_check" && actions.onMasteryCheck !== undefined && (
                  <Button size="sm" onClick={() => actions.onMasteryCheck?.(kc, false)}>{t("sheet.check_mastery")}</Button>
                )}
                {kc.state !== "mastered" && actions.onMasteryCheck !== undefined && (
                  <Button size="sm" tone="ghost" onClick={() => actions.onMasteryCheck?.(kc, true)}>{t("sheet.already_know")}</Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** The topic's board syllabus points in the sheet, by syllabus; nothing when it has none. */
function SyllabusPoints({ topic, syllabuses }: { topic: MapTopic; syllabuses: MapSyllabus[] }) {
  const t = useT();
  const rows = syllabuses.flatMap((s) => syllabusCodes(topic, s.scheme).map((p) => ({ syllabus: s, point: p })));
  if (rows.length === 0) return null;
  return (
    <div className="flex flex-col gap-2" data-syllabus-points>
      <h3 className="text-label text-faint">{t("sheet.syllabus")}</h3>
      <ul className="flex flex-col gap-1">
        {rows.map(({ syllabus, point }) => (
          <li key={`${point.scheme}:${point.code}`} className="text-ui text-ink" data-coverage={point.coverage}>
            {t("sheet.syllabus_point", { board: syllabus.board, code: syllabus.syllabus_code, point: point.code })}
            {point.coverage === "partial" && <span className="text-muted">{" · "}{t("sheet.partly_covers")}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** "All topics" or one syllabus, and the line naming every board shown as not endorsing Jana (CUR-06). */
function SyllabusSwitch({ syllabuses, value, onChange }: { syllabuses: MapSyllabus[]; value: string | null; onChange: (scheme: string | null) => void }) {
  const t = useT();
  const options = [{ value: ALL, label: t("map.syllabus.all") }, ...syllabuses.map((s) => ({ value: s.scheme, label: t("map.syllabus.option", { board: s.board, code: s.syllabus_code }) }))];
  const boards = [...new Set(syllabuses.map((s) => s.board))];
  const pick = (v: string) => onChange(v === ALL ? null : v);
  return (
    <div className="flex flex-col gap-2" data-syllabus-switch={value ?? ALL}>
      {options.length <= SEGMENTS_MAX ? (
        <SegmentedControl label={t("map.syllabus")} value={value ?? ALL} onChange={pick} options={options} />
      ) : (
        <div role="group" aria-label={t("map.syllabus")} className="flex flex-wrap gap-2">
          {options.map((o) => <SuggestionChip key={o.value} aria-pressed={o.value === (value ?? ALL)} onClick={() => pick(o.value)}>{o.label}</SuggestionChip>)}
        </div>
      )}
      {boards.map((board) => <p key={board} className="text-meta text-faint" data-not-endorsed={board}>{t("map.not_endorsed", { board })}</p>)}
    </div>
  );
}

function Loading() {
  const t = useT();
  return (
    <div role="status" aria-busy="true" className="flex flex-col gap-4" data-map-loading>
      <span className="sr-only">{t("map.loading")}</span>
      <span className="block h-6 w-64"><Skeleton lines={1} /></span>
      <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas">
        {[0, 1, 2, 3].map((i) => (
          <li key={i} className="flex min-h-16 flex-col justify-center px-4 py-3">
            <Skeleton lines={1} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The subject switch in the header: a segmented control, or chips when there are more subjects than it holds. */
function SubjectSwitch({ subjects, value, onSubject }: { subjects: SubjectChoice[]; value: string | null; onSubject?: ((subjectId: string) => void) | undefined }) {
  const t = useT();
  if (subjects.length < 2) return null;
  if (subjects.length <= SEGMENTS_MAX) {
    return <SegmentedControl label={t("map.subjects")} value={value ?? ""} onChange={(id) => onSubject?.(id)}
      options={subjects.map((s) => ({ value: s.subjectId, label: s.name }))} />;
  }
  return (
    <div role="group" aria-label={t("map.subjects")} className="flex flex-wrap gap-2">
      {subjects.map((s) => <SuggestionChip key={s.subjectId} aria-pressed={s.subjectId === value} onClick={() => onSubject?.(s.subjectId)}>{s.name}</SuggestionChip>)}
    </div>
  );
}

/** The map from its view: the gallery and the tests render this directly. */
/** The concept map shown in the subject map's "Map" mode, with what it needs from the app. */
export interface ConceptMapMode {
  view: ConceptMapView;
  onOpen: () => void;
  onRetry?: () => void;
  onShowGap?: (gap: { misconceptionId: string; rootKcId: string; kcId: string }) => void;
  onClearGap?: () => void;
  /** An open question taken to the tutor in the learner's words. */
  onAskQuestion?: (text: string) => void;
  pointed?: string | null;
  /** Start in "Map" mode (the gallery's map state, or a root-gap card's "See it on the map"). */
  initial?: boolean;
  /** The learner's map lens: layers, the scrubber and each skill's links (SPEC §21). */
  lens?: ConceptLens;
  /** The skill sheet's links and actions into Study, the knows page and the tutor's thread (SPEC §21.1). */
  sheet?: ConceptActions;
}

/** The subject map's actions for one KC of the concept map: through its topic tile (asking and practising are
 * topic actions on the subject map). */
function conceptActions(view: MapView, actions: MapActions, concept: ConceptMapMode): ConceptActions {
  const topicOf = (node: ConceptNode) => view.map?.sections.flatMap((s) => s.topics).find((tp) => tp.kcs.some((k) => k.kc_id === node.kc_id));
  const out: ConceptActions = {};
  if (actions.onAsk !== undefined) out.onAskKc = (node) => { const tp = topicOf(node); if (tp !== undefined) actions.onAsk?.(tp); };
  if (actions.onJourney !== undefined) out.onPractiseKc = (node) => { const tp = topicOf(node), kc = tp?.kcs.find((k) => k.kc_id === node.kc_id); if (tp !== undefined && kc !== undefined) actions.onJourney?.(tp, kc); };
  else if (actions.onPractise !== undefined) out.onPractiseKc = (node) => { const tp = topicOf(node); if (tp !== undefined) actions.onPractise?.(tp); };
  if (concept.onAskQuestion !== undefined) out.onAskQuestion = concept.onAskQuestion;
  return { ...out, ...concept.sheet };
}

export function SubjectMapContent({ view, subjects = [], onSubject, onSyllabus, onRetry, actions = {}, initialTopic, inProgress = null, onContinue, concept }: {
  view: MapView;
  /** The concept map: with it, a "Topics / Map" switch shows the topic cards or the map of skills. */
  concept?: ConceptMapMode;
  subjects?: SubjectChoice[];
  onSubject?: (subjectId: string) => void;
  /** The syllabus switch's choice, kept by the controller; without it the choice is this view's own. */
  onSyllabus?: (scheme: string | null) => void;
  onRetry?: () => void;
  actions?: MapActions;
  /** A topic whose sheet is open from the start (the gallery's topic-sheet state). */
  initialTopic?: string;
  /** The lesson part way through in this subject, offered with "Continue". */
  inProgress?: LessonInProgress | null;
  onContinue?: (lessonId: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState<{ id: string; from: HTMLButtonElement | null } | null>(initialTopic === undefined ? null : { id: initialTopic, from: null });
  const [ownSyllabus, setOwnSyllabus] = useState<string | null>(view.syllabus ?? null);
  const [showing, setShowing] = useState<"tiles" | "map">(concept?.initial === true ? "map" : "tiles");
  // A section the student opened or closed; any other follows the default (the current or next section open).
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const openConcept = concept?.onOpen;
  useEffect(() => {
    if (showing === "map") openConcept?.();
  }, [showing, openConcept, view.subjectId]);
  const map = view.map;
  const syllabuses = map?.syllabuses ?? [];
  const chosen = onSyllabus !== undefined ? view.syllabus ?? null : ownSyllabus;
  const syllabus = syllabuses.find((s) => s.scheme === chosen) ?? null;
  const chooseSyllabus = (scheme: string | null) => (onSyllabus !== undefined ? onSyllabus(scheme) : setOwnSyllabus(scheme));
  // With a syllabus chosen, only the topics it maps, and only the sections that keep one (D113).
  const sections = (map?.sections ?? [])
    .map((s) => (syllabus === null ? s : { ...s, topics: s.topics.filter((tp) => syllabusCodes(tp, syllabus.scheme).length > 0) }))
    .filter((s) => s.topics.length > 0);
  const topics = sections.flatMap((s) => s.topics);
  const topic = open === null ? null : topics.find((tp) => tp.node_id === open.id) ?? null;
  const secure = topics.filter((tp) => topicLabel(tp.kcs.map((k) => k.state)) === "mastered").length;
  const continuing = inProgress !== null && onContinue !== undefined;
  const next = continuing || actions.onStartLesson === undefined ? null : nextLesson(sections);
  const current = (continuing ? sections.find((s) => s.topics.some((tp) => tp.lessons.some((l) => l.lesson_id === inProgress.lessonId))) : undefined)?.node_id
    ?? next?.section.node_id ?? sections[0]?.node_id ?? null;
  const sectionOpen = (section: MapSection) => toggled[section.node_id] ?? (section.node_id === current || section.topics.some((tp) => tp.node_id === open?.id));
  const close = () => {
    open?.from?.focus();
    setOpen(null);
  };
  const body = () => {
    if (concept !== undefined && showing === "map") {
      return (
        <ConceptMapContent view={concept.view} actions={conceptActions(view, actions, concept)} pointed={concept.pointed ?? null}
          {...(concept.onRetry !== undefined ? { onRetry: concept.onRetry } : {})} {...(concept.onShowGap !== undefined ? { onShowGap: concept.onShowGap } : {})}
          {...(concept.onClearGap !== undefined ? { onClearGap: concept.onClearGap } : {})} {...(concept.lens !== undefined ? { lens: concept.lens } : {})} />
      );
    }
    if (view.status === "failed") {
      return <ErrorState message={t("map.failed")} {...(onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: onRetry } } : {})} />;
    }
    if (map === null) return <Loading />;
    if (map.status === "not_installed") return <EmptyState message={t("map.not_installed")} />;
    if (map.status === "unverified") return <ErrorState message={t("map.unverified")} />;
    if (map.sections.length === 0) return <EmptyState message={t("map.no_topics")} />;
    return (
      <div className="flex flex-col gap-8">
        {syllabuses.length > 0 && <SyllabusSwitch syllabuses={syllabuses} value={syllabus?.scheme ?? null} onChange={chooseSyllabus} />}
        {sections.map((section) => (
          <Section key={section.node_id} section={section} open={sectionOpen(section)} onToggle={() => setToggled((known) => ({ ...known, [section.node_id]: !sectionOpen(section) }))}>
            {section.topics.map((tp) => (
              <TopicRow key={tp.node_id} topic={tp} syllabus={syllabus} selected={open?.id === tp.node_id} onOpen={(picked, from) => setOpen({ id: picked.node_id, from })} />
            ))}
          </Section>
        ))}
      </div>
    );
  };
  return (
    <div className="flex min-h-full flex-col" data-map-status={view.status} data-map-subject={view.subjectId ?? undefined}>
      <ScreenHeader end={<SubjectSwitch subjects={subjects} value={view.subjectId} onSubject={onSubject} />}>
        <span className="text-ui font-semibold text-ink">{t("nav.lessons")}</span>
      </ScreenHeader>
      <div className="flex-1 px-6 pt-6 pb-12 lg:px-10">
        <div className={`mx-auto grid w-full grid-cols-1 items-start gap-8 ${showing === "map" ? "max-w-360" : topic !== null ? "max-w-300 lg:grid-cols-[minmax(0,1fr)_22rem]" : "max-w-240"}`}>
          <div className="flex min-w-0 flex-col gap-8">
            <section aria-labelledby="map-subject" className="flex flex-col gap-4">
              <span className="flex flex-wrap items-center justify-between gap-3">
                <h1 id="map-subject" className="text-display text-ink">{map?.title ?? t("map.title")}</h1>
                {concept !== undefined && (
                  <span data-map-mode={showing}>
                    <SegmentedControl label={t("map.mode")} value={showing} onChange={setShowing} options={[{ value: "tiles", label: t("map.mode.tiles") }, { value: "map", label: t("map.mode.map") }]} />
                  </span>
                )}
              </span>
              {map !== null && map.status === "ready" && topics.length > 0 && (
                <span className="block max-w-105">
                  <ProgressBar label={t("map.topics_secure")} value={secure} max={topics.length} valueText={t("map.of", { count: secure, total: topics.length })} />
                </span>
              )}
            </section>
            {inProgress !== null && onContinue !== undefined && (
              <section aria-labelledby="map-in-progress" data-in-progress={inProgress.lessonId} className="flex flex-wrap items-center justify-between gap-x-8 gap-y-4 rounded-card border border-line bg-canvas px-6 py-5">
                <div className="flex min-w-0 flex-1 basis-80 flex-col gap-2">
                  <p className="text-label text-faint">{t("map.in_progress")}</p>
                  <h2 id="map-in-progress" className="text-heading text-ink">{inProgress.title}</h2>
                  <span className="block max-w-90">
                    <ProgressBar bare label={t("lesson.parts")} value={inProgress.part} max={inProgress.parts} valueText={t("lesson.progress", { index: inProgress.part, total: inProgress.parts })} />
                  </span>
                </div>
                <Button tone="primary" onClick={() => onContinue(inProgress.lessonId)}>
                  {t("map.continue")}
                  <Icon name="go-next" size={16} />
                </Button>
              </section>
            )}
            {next !== null && showing === "tiles" && (
              <section aria-labelledby="map-next-up" data-next-up={next.lesson.lesson_id} className="flex flex-wrap items-center justify-between gap-x-8 gap-y-4 rounded-card border border-line bg-canvas px-6 py-5">
                <div className="flex min-w-0 flex-1 basis-80 flex-col gap-1">
                  <h2 id="map-next-up" className="text-label text-faint">{t("map.next_up")}</h2>
                  <p className="text-ui text-muted">{t("map.next_up.meta", { topic: next.topic.title, minutes: next.lesson.estimated_minutes })}</p>
                </div>
                <Button tone="primary" onClick={() => actions.onStartLesson?.(next.lesson.lesson_id)}>
                  {t("map.open_lesson", { title: next.lesson.title })}
                  <Icon name="go-next" size={16} />
                </Button>
              </section>
            )}
            {body()}
          </div>
          {topic !== null && showing === "tiles" && <TopicSheet topic={topic} actions={actions} onClose={close} syllabuses={syllabuses} />}
        </div>
      </div>
    </div>
  );
}

/** The map for the unlocked profile: reads on open and on a subject change, re-reads on a mastery change. */
/** Today's local date, the scrubber's last stop. */
function localToday(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function SubjectMap({ controller, subjectId, subjects, onSubject, actions, inProgress, onContinue, initialTopic, concept, onAskQuestion, pointed, conceptFirst, sheet, today }: {
  controller: SubjectMapController;
  /** The concept map's controller: with it the map has its "Map" mode. */
  concept?: ConceptMapController;
  /** The skill sheet's actions into Study, the knows page and the thread (SPEC §21.1). */
  sheet?: ConceptActions;
  /** Today's local date (tests fix it); the clock's otherwise. */
  today?: string;
  onAskQuestion?: (text: string) => void;
  pointed?: string | null;
  conceptFirst?: boolean;
  subjectId: string;
  initialTopic?: string;
  subjects?: SubjectChoice[];
  onSubject?: (subjectId: string) => void;
  actions?: MapActions;
  inProgress?: LessonInProgress | null;
  onContinue?: (lessonId: string) => void;
}) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const none = useRef(() => () => undefined).current;
  // The tutor's spoken pointer at a skill (`kc:` cue), when the map is on screen.
  const heard = useVoicePointer();
  const conceptView = useSyncExternalStore(concept?.subscribe ?? none, concept?.getView ?? (() => null), concept?.getView ?? (() => null));
  const lensView = useSyncExternalStore(concept?.subscribe ?? none, concept?.getLens ?? (() => null), concept?.getLens ?? (() => null));
  const onLinks = useMemo(() => (concept === undefined ? undefined : (kc: string) => void concept.links(kc)), [concept]);
  useEffect(() => {
    void controller.load(subjectId);
  }, [controller, subjectId]);
  const onOpen = useMemo(() => (concept === undefined ? undefined : () => void concept.load(subjectId)), [concept, subjectId]);
  const mode: ConceptMapMode | undefined = concept === undefined || conceptView === null || onOpen === undefined ? undefined : {
    view: conceptView, onOpen, onRetry: onOpen, onShowGap: (gap) => concept.showGap(gap), onClearGap: () => concept.clearGap(),
    ...(onAskQuestion !== undefined ? { onAskQuestion } : {}), pointed: pointed ?? (heard?.target.kind === "kc" ? heard.target.kc_id : null), initial: conceptFirst === true,
    ...(lensView !== null && onLinks !== undefined ? { lens: { lens: lensView, onLayer: (layer) => concept.setLayer(layer), onScrub: (asOf) => void concept.scrub(asOf), onLinks, today: today ?? localToday() } } : {}),
    ...(sheet !== undefined ? { sheet } : {}),
  };
  return (
    <SubjectMapContent
      view={view}
      {...(mode !== undefined ? { concept: mode } : {})}
      onRetry={() => void controller.load(subjectId)}
      onSyllabus={(scheme) => controller.chooseSyllabus(scheme)}
      {...(initialTopic !== undefined ? { initialTopic } : {})}
      {...(subjects !== undefined ? { subjects } : {})}
      {...(onSubject !== undefined ? { onSubject } : {})}
      {...(actions !== undefined ? { actions } : {})}
      {...(inProgress !== undefined ? { inProgress } : {})}
      {...(onContinue !== undefined ? { onContinue } : {})}
    />
  );
}
