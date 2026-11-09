// Progress (UXS-27, LRN-34, LRN-33, ASM-19; direction A "Studio"): what the learner model says, in the words of the
// mastery scale (TABLE LRN-09a) and never as a number between 0 and 1. At the top, the ideas that moved today (the
// reflection, LRN-34), then one quiet line with the time studied this week and the streak with its freezes (D109). Then each subject: its ideas in the
// three bands as one segmented bar with the counts in words, the topics secure, mastery over time as a chart with a
// "Show as table" alternative and a one-line summary (LOC-16), the grade estimate only where the subject's pack sets
// the exam board's own grade letters (D27: otherwise nothing, never an invented scale), and the topics with their
// ideas as a heat map whose cells carry fill, pattern, icon and word (DSN-23). A broken streak reads as a new one,
// never as a loss; nothing here counts down, ranks or blames (LRN-35). Links lead to "What your tutor knows" (UXS-28),
// the notes (UXS-52) and the session history (UXS-48). States: loading, canonical, empty (nothing studied yet), error-retriable, not served.
// Simpler by default (SPEC §22.1, W11; the live UX review): the header is the title and one overflow menu (tutor
// knows, notes, session history, find my starting point), so nothing clips in a narrow window. The page leads with
// recent activity ("What moved today", the week's line, Study activity), then a subject selector and the chosen
// subject's card; topics not started yet sit collapsed under one disclosure after the touched ones. The learner's map
// (SPEC §21, JM-D35, W10: every installed subject as topic tiles, `MapOverview`) opens from "Show my map" beside the
// selector rather than filling the first screen; while it is open the ideas that moved today are outlined on it.
import { useEffect, useId, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { gradeShown, type KcState, type MapLayer, type MapOverviewController, type ProgressController, type ProgressState, type ProgressSubject, type ProgressView } from "@jana/client-core";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { MasteryCell, MasteryMark, SegmentedBar, useCountsText } from "../learn/Mastery.js";
import { Button, Disclosure, EmptyState, ErrorState, IconButton, MenuButton, SegmentedControl, Skeleton, SubjectDot, type MenuItem } from "../primitives/index.js";
import { MapOverview } from "./MapOverview.js";

export interface ProgressActions {
  onRetry: () => void;
  /** "What your tutor knows" (UXS-28, LRN-36). */
  onKnows?: () => void;
  /** "My notes" (UXS-52). */
  onNotes?: () => void;
  /** "Session history" (UXS-48): the retained sessions, read-only. */
  onHistory?: () => void;
  /** Where to start when nothing has been studied yet. */
  onLearn?: () => void;
  onDiagnostic?: () => void;
  /** A mastery check of one idea practised in Study: how practice becomes mastery. */
  onCheck?: (kcId: string) => void;
  /** Practice of one idea with too few varied questions for a check yet. */
  onPractise?: (kcId: string) => void;
}

export interface ProgressNames {
  subject: (subject: string) => string;
  /** An idea's statement, when the subject map is known; else the topic's title is shown with its state. */
  kc: (kcId: string) => string | null;
}

/** The week's Study activity as one quiet line (SPEC §19.1): what was done in Study, never a mastery claim (JM-D26).
 * Nothing done there this week, nothing said. */
function StudyActivityLine({ activity }: { activity: NonNullable<ProgressView["study_activity"]> }) {
  const t = useT();
  const { quiz_items: questions, quiz_correct: right, cards_reviewed: cards, aids_made: aids } = activity;
  if (questions + cards + aids === 0) return null;
  const parts = [
    ...(questions > 0 ? [t("progress.study_activity.quiz", { count: questions, right })] : []),
    ...(cards > 0 ? [t("progress.study_activity.cards", { count: cards })] : []),
    ...(aids > 0 ? [t("progress.study_activity.aids", { count: aids })] : []),
  ];
  return <p data-progress-study={questions + cards + aids} className="flex items-center gap-2 text-meta text-muted"><Icon name="study" size={16} />{t("progress.study_activity.title")}: {parts.join(" · ")}</p>;
}

/** A topic is secure when every one of its ideas is. */
function topicsSecure(subject: ProgressSubject): number {
  return subject.topics.filter((t) => t.kcs.length > 0 && t.kcs.every((k) => k.band === "secure")).length;
}

/** Streak and time studied as one quiet line under what moved (G1, D109): never the screen's headline, and a
 * streak that has not started is simply not mentioned (LRN-35). */
function StudyLine({ days, freezes, minutes, sessions }: { days: number; freezes: number; minutes: number; sessions: number }) {
  const t = useT();
  const parts = [t("progress.week_line", { minutes, sessions })];
  if (days > 0) parts.push(days === 1 ? t("progress.streak.new") : t("plan.streak", { days }), t("progress.streak.freezes", { count: freezes }));
  return <p data-progress-streak={days} className="flex items-center gap-2 text-meta text-muted"><Icon name="progress" size={16} />{parts.join(" · ")}</p>;
}

/** The ideas whose bars moved today (LRN-34's reflection block), each with its state now. */
function MovedToday({ moved: seen, names, states, onLearn, outlined = false, studied = false, ownCourse = false, studiedToday = [], checkable = [], onCheck, onPractise }: { moved: string[]; names: ProgressNames; states: Map<string, { state: KcState; topic: string }>; onLearn?: () => void; outlined?: boolean; studied?: boolean; ownCourse?: boolean; studiedToday?: readonly string[]; checkable?: readonly string[]; onCheck?: (kcId: string) => void; onPractise?: (kcId: string) => void }) {
  const shownStudied = studiedToday.slice(0, 5);
  const t = useT();
  // The core lists every idea worked on today; one still at Not started has not moved, so it is counted on one quiet
  // line instead of being listed under "moved" beside its Not started mark.
  const moved = seen.filter((kc) => states.get(kc)?.state !== "not_started");
  const still = seen.length - moved.length;
  return (
    <section aria-labelledby="progress-moved" data-progress-moved={moved.length} className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-6">
      <h2 id="progress-moved" className="text-heading text-ink">{moved.length === 0 ? t("progress.moved.lead_none") : t("progress.moved.lead", { count: moved.length })}</h2>
      {moved.length === 0 && still === 0 && shownStudied.length > 0 ? null : moved.length === 0 && still === 0 ? <div className="flex flex-wrap items-center gap-3"><p className="text-ui text-muted">{t(ownCourse ? "progress.moved.own_course" : studied ? "progress.moved.study_note" : "progress.moved.invite")}</p>{onLearn !== undefined && !ownCourse && <Button size="sm" onClick={onLearn}>{t("home.browse_topics")}</Button>}</div> : moved.length > 0 && (
        <ul className="flex flex-col gap-2">
          {moved.map((kc) => {
            const known = states.get(kc);
            return (
              <li key={kc} data-moved-kc={kc} className="flex items-center justify-between gap-3 border-t border-line pt-2 first:border-t-0 first:pt-0">
                <span className="min-w-0 text-ui text-ink">{names.kc(kc) ?? known?.topic ?? kc}</span>
                {known !== undefined && <MasteryMark state={known.state} />}
              </li>
            );
          })}
        </ul>
      )}
      {still > 0 && <p data-progress-still={still} className="text-meta text-muted">{t("progress.moved.still", { count: still })}</p>}
      {shownStudied.length > 0 && (
        <div data-progress-studied={studiedToday.length} className="flex flex-col gap-2 border-t border-line pt-3">
          <h3 className="text-ui font-semibold text-ink">{t("progress.moved.studied_lead")}</h3>
          <p className="text-meta text-muted">{t("progress.moved.studied_help")}</p>
          <ul className="flex flex-col gap-2">
            {shownStudied.map((kc) => (
              <li key={kc} data-studied-kc={kc} className="flex flex-wrap items-center justify-between gap-3">
                <span className="min-w-0 flex-1 text-ui text-ink">{names.kc(kc) ?? states.get(kc)?.topic ?? kc}</span>
                {checkable.includes(kc)
                  ? onCheck !== undefined && <Button size="sm" onClick={() => onCheck(kc)}>{t("progress.moved.check")}</Button>
                  : onPractise !== undefined && <Button size="sm" onClick={() => onPractise(kc)}>{t("progress.moved.practise")}</Button>}
              </li>
            ))}
          </ul>
          {studiedToday.length > shownStudied.length && <p className="text-meta text-muted">{t("progress.moved.studied_more", { count: studiedToday.length - shownStudied.length })}</p>}
        </div>
      )}
      {outlined && moved.length > 0 && <p data-moved-outlined="" className="text-meta text-muted">{t("progress.map.moved_outline")}</p>}
    </section>
  );
}

function Grade({ subject }: { subject: ProgressSubject }) {
  const t = useT();
  const shown = gradeShown(subject.grade);
  if (shown.kind === "none") return null;
  const text = shown.kind === "gate" ? t("progress.grade.gate", { needed: shown.needed, checked: shown.checked })
    : shown.kind === "uncertain" ? t("progress.grade.uncertain") : t("progress.grade.band", { band: shown.band, low: shown.low, high: shown.high });
  return <p data-grade={shown.kind} className="text-ui text-ink">{text}</p>;
}

/** Mastery over time (LRN-34, LOC-16): per week, the ideas started (learning) and secure, as stacked bars in the
 * mastery fills with their patterns, a legend in words, a one-line summary and the same values as a table. */
function History({ subject, label }: { subject: ProgressSubject; label: string }) {
  const t = useT();
  const [table, setTable] = useState(false);
  const id = useId();
  const weeks = subject.history;
  const first = weeks[0];
  const last = weeks[weeks.length - 1];
  if (first === undefined || last === undefined) return null;
  const top = Math.max(1, ...weeks.map((w) => w.learning + w.secure));
  const plotted = weeks.some((w) => w.learning + w.secure > 0);
  if (!plotted) return null; // nothing to chart yet: no "from 0 to 0" (G1, D109)
  // "went from 0 to 0" says nothing: before the first secure idea the line says what makes the chart grow.
  const summary = last.secure === 0 ? t("progress.history.none_secure") : t("progress.history.summary", { from: first.secure, to: last.secure, weeks: weeks.length });
  return (
    <figure className="flex flex-col gap-3" data-history={subject.subject}>
      <figcaption id={id} className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-ui font-medium text-ink">{t("progress.history.title")}</span>
        {plotted && <Button size="sm" tone="ghost" aria-expanded={table} onClick={() => setTable((open) => !open)}>
          <Icon name={table ? "chart" : "table"} size={16} />{t(table ? "progress.history.show_chart" : "progress.history.show_table")}
        </Button>}
      </figcaption>
      <p className="text-meta text-muted">{summary}</p>
      {!plotted ? null : table ? (
        <table className="w-full text-ui text-ink" data-history-table="">
          <caption className="sr-only">{t("progress.history.caption", { subject: label })}</caption>
          <thead>
            <tr className="text-meta text-faint">
              <th scope="col" className="py-1 text-start font-medium">{t("progress.history.week")}</th>
              <th scope="col" className="py-1 text-end font-medium">{t("mastery.learning")}</th>
              <th scope="col" className="py-1 text-end font-medium">{t("progress.history.secure")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {weeks.map((w) => (
              <tr key={w.week_start} data-week={w.week_start}>
                <th scope="row" className="py-1.5 text-start font-regular">{t("progress.history.week_of", { date: new Date(`${w.week_start}T12:00:00`) })}</th>
                <td className="py-1.5 text-end tabular-nums">{w.learning}</td>
                <td className="py-1.5 text-end tabular-nums">{w.secure}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div role="img" aria-labelledby={id} aria-describedby={`${id}-summary`} className="flex flex-col gap-2">
          <span id={`${id}-summary`} className="sr-only">{summary}</span>
          <div className="flex h-28 items-end gap-1.5 border-b border-line-strong" data-history-bars="">
            {weeks.map((w) => (
              <span key={w.week_start} className="flex h-full min-w-3 flex-1 flex-col justify-end gap-0.5" data-week={w.week_start}>
                {w.learning > 0 && <MasteryCell bar state="learning" grow={w.learning / top} />}
                {w.secure > 0 && <MasteryCell bar state="mastered" grow={w.secure / top} />}
              </span>
            ))}
          </div>
          <div aria-hidden="true" className="flex flex-wrap gap-3 text-meta text-muted">
            <span className="inline-flex items-center gap-1.5"><MasteryMark state="learning" compact />{t("mastery.learning")}</span>
            <span className="inline-flex items-center gap-1.5"><MasteryMark state="mastered" compact />{t("progress.history.secure")}</span>
          </div>
        </div>
      )}
    </figure>
  );
}

/** One topic's row of the heat map: its title, its counts in words and one cell per idea (DSN-23). */
function TopicRow({ topic, names }: { topic: ProgressSubject["topics"][number]; names: ProgressNames }) {
  const t = useT();
  const counts = useCountsText();
  return (
    <li data-progress-topic={topic.topic_id} className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3">
      <span className="flex min-w-48 flex-1 flex-col gap-0.5">
        <span className="text-ui text-ink">{topic.title}</span>
        <span className="text-meta text-faint">{counts(topic.kcs)}</span>
      </span>
      <ul aria-label={t("progress.topic_ideas", { topic: topic.title })} className="flex flex-wrap gap-1">
        {topic.kcs.map((kc) => <MasteryCell key={kc.kc_id} state={kc.state} label={names.kc(kc.kc_id) ?? undefined} kcId={kc.kc_id} />)}
      </ul>
    </li>
  );
}

/** A topic is untouched while none of its ideas has left Not started. */
const untouched = (topic: ProgressSubject["topics"][number]) => topic.kcs.every((kc) => kc.state === "not_started");

function SubjectCard({ subject, names }: { subject: ProgressSubject; names: ProgressNames }) {
  const t = useT();
  const counts = useCountsText();
  const [restOpen, setRestOpen] = useState(false);
  const label = names.subject(subject.subject);
  const kcs = subject.topics.flatMap((topic) => topic.kcs);
  const total = kcs.length;
  // The topics worked on lead; the ones not started yet are counted on one line and listed on request, so a
  // syllabus of untouched topics never dominates the page.
  const touched = subject.topics.filter((topic) => !untouched(topic));
  const rest = subject.topics.filter(untouched);
  return (
    <section aria-labelledby={`progress-${subject.subject}`} data-progress-subject={subject.subject} className="flex flex-col gap-6 rounded-card border border-line bg-canvas p-6">
      <div className="flex flex-col gap-3">
        <h2 id={`progress-${subject.subject}`} className="flex items-center gap-2 text-heading text-ink"><SubjectDot subject={subject.subject} />{label}</h2>
        <p className="text-ui text-muted">{t("progress.subject.summary", { secure: subject.bands.secure, total, topics: topicsSecure(subject), topicsTotal: subject.topics.length })}</p>
        <SegmentedBar kcs={kcs} />
        <p className="text-meta text-muted">{counts(kcs)}</p>
        <Grade subject={subject} />
      </div>
      <History subject={subject} label={label} />
      {subject.topics.length > 0 && (
        <div className="flex flex-col gap-3">
          <h3 className="text-ui font-medium text-ink">{t("progress.topics")}</h3>
          {touched.length > 0 && (
            <ul className="flex flex-col divide-y divide-line" data-heat-map={subject.subject}>
              {touched.map((topic) => <TopicRow key={topic.topic_id} topic={topic} names={names} />)}
            </ul>
          )}
          {rest.length > 0 && (
            <Disclosure summary={t("progress.topics.not_started", { count: rest.length })} open={restOpen} onToggle={() => setRestOpen((open) => !open)}>
              <ul className="flex flex-col divide-y divide-line" data-heat-map-rest={subject.subject}>
                {rest.map((topic) => <TopicRow key={topic.topic_id} topic={topic} names={names} />)}
              </ul>
            </Disclosure>
          )}
        </div>
      )}
    </section>
  );
}

/** The ideas that moved today, as LRN-34 lists them: the ones no longer at Not started. */
export function movedToday(progress: ProgressView): string[] {
  const states = new Map(progress.subjects.flatMap((s) => s.topics.flatMap((topic) => topic.kcs.map((kc) => [kc.kc_id, kc.state] as const))));
  return progress.moved_today.filter((kc) => states.get(kc) !== "not_started");
}

/** The subject shown before one is chosen: the one with an idea that moved today, else the one with the most ideas
 * past Not started, else the first. (The 2 November rehearsal opened on Mathematics, 0 of 102, with Physics in use.) */
export function defaultSubject(progress: ProgressView): ProgressSubject | undefined {
  const moved = new Set(progress.moved_today);
  return progress.subjects.find((s) => s.topics.some((topic) => topic.kcs.some((kc) => moved.has(kc.kc_id))))
    ?? [...progress.subjects].sort((a, b) => b.bands.learning + b.bands.secure - (a.bands.learning + a.bands.secure))[0];
}

export function ProgressContent({ state, names, actions, hero: heroIn, ownCourse = false }: { state: ProgressState; names: ProgressNames; actions: ProgressActions; hero?: ReactNode; ownCourse?: boolean }) {
  const hero = ownCourse ? undefined : heroIn;
  const t = useT();
  const progress = state.progress;
  // The subject shown (`defaultSubject` until one is chosen) and whether the map is open; both stay while the screen lives.
  const [chosen, setChosen] = useState<string | null>(null);
  const [mapOpen, setMapOpen] = useState(false);
  const mapId = useId();
  // "What your tutor knows" stays a visible header action: it is a two-click destination from Home (UXS-01, routes.ts).
  const menu: MenuItem[] = [
    ...(actions.onNotes !== undefined ? [{ id: "notes", label: t("notes.title"), icon: "notes" as const, onSelect: actions.onNotes }] : []),
    ...(actions.onHistory !== undefined ? [{ id: "history", label: t("history.title"), icon: "timer" as const, onSelect: actions.onHistory }] : []),
    ...(actions.onDiagnostic !== undefined ? [{ id: "diagnostic", label: t("assessment.find_start"), icon: "search" as const, onSelect: actions.onDiagnostic }] : []),
  ];
  const body = () => {
    if (state.status === "unavailable") return <EmptyState message={t("progress.unavailable")} />;
    if (state.status === "failed") return <ErrorState message={t("progress.failed")} action={{ label: t("action.try_again"), onClick: actions.onRetry }} />;
    if (progress === null) return <div aria-busy="true" className="flex flex-col gap-6"><Skeleton lines={3} /><Skeleton lines={4} /></div>;
    const states = new Map<string, { state: KcState; topic: string }>();
    for (const s of progress.subjects) for (const topic of s.topics) for (const kc of topic.kcs) states.set(kc.kc_id, { state: kc.state, topic: topic.title });
    const started = progress.subjects.some((s) => s.bands.learning + s.bands.secure > 0) || progress.time_studied.minutes_total > 0;
    const subjects = ownCourse ? [] : progress.subjects;
    const subject = subjects.find((s) => s.subject === chosen) ?? defaultSubject(progress);
    const mapShown = hero !== undefined && mapOpen;
    return (
      <div className="flex flex-col gap-8">
        {/* Recent activity leads: what moved today, then the streak and time studied as one quiet line (G1, D109). */}
        <div className="flex flex-col gap-3">
          <MovedToday moved={progress.moved_today} names={names} states={states} outlined={mapShown} studied={(progress.study_activity?.quiz_items ?? 0) + (progress.study_activity?.cards_reviewed ?? 0) > 0} ownCourse={ownCourse} studiedToday={ownCourse ? [] : progress.studied_today ?? []} checkable={progress.studied_checkable ?? []} {...(actions.onCheck !== undefined ? { onCheck: actions.onCheck } : {})} {...(actions.onPractise !== undefined ? { onPractise: actions.onPractise } : {})} {...(actions.onLearn !== undefined && started ? { onLearn: actions.onLearn } : {})} />
          <StudyLine days={progress.streak.days} freezes={progress.streak.freezes_left} minutes={progress.time_studied.minutes_7d} sessions={progress.time_studied.sessions_7d} />
          {progress.study_activity !== undefined && <StudyActivityLine activity={progress.study_activity} />}
        </div>
        {!started && !ownCourse && (
          <section data-progress-empty="" className="flex flex-col items-start gap-3 rounded-card border border-line bg-canvas p-6">
            <p className="text-body text-ink">{t("progress.empty")}</p>
            {actions.onLearn !== undefined && <Button tone="primary" onClick={actions.onLearn}>{t("home.browse_topics")}</Button>}
          </section>
        )}
        {/* The subject selector and, beside it, the one secondary action that opens the detailed maps (SPEC §22.1). */}
        {(subjects.length > 1 || hero !== undefined) && (
          <div data-progress-controls="" className="flex flex-wrap items-center justify-between gap-3">
            {subjects.length > 1 && subject !== undefined
              ? <SegmentedControl label={t("progress.subject_select")} value={subject.subject} onChange={setChosen} options={subjects.map((s) => ({ value: s.subject, label: names.subject(s.subject) }))} />
              : <span />}
            {hero !== undefined && (
              <Button aria-expanded={mapOpen} aria-controls={mapId} onClick={() => setMapOpen((open) => !open)}>
                <Icon name={mapOpen ? "collapse" : "expand"} size={16} />{t(mapOpen ? "progress.map.hide" : "progress.map.show")}
              </Button>
            )}
          </div>
        )}
        {mapShown && <div id={mapId} data-progress-map-open="">{hero}</div>}
        {subject !== undefined && <SubjectCard key={subject.subject} subject={subject} names={names} />}
      </div>
    );
  };
  return (
    <div className="flex min-h-full flex-col" data-progress={state.status}>
      <ScreenHeader {...(menu.length > 0 || actions.onKnows !== undefined ? { end: <span className="flex items-center gap-1">
        {actions.onKnows !== undefined && <Button size="sm" tone="ghost" onClick={actions.onKnows}><Icon name="tutor" size={16} />{t("progress.knows")}</Button>}
        {menu.length > 0 && <MenuButton label={t("frame.more")} items={menu} trigger={(props) => <IconButton {...props} icon="more" label={t("frame.more")} />} />}
      </span> } : {})}>
        <h1 className="text-ui font-semibold text-ink">{t("app.progress")}</h1>
      </ScreenHeader>
      <div className="flex-1 px-6 pt-6 pb-12 lg:px-10"><div className="mx-auto w-full max-w-240">{body()}</div></div>
    </div>
  );
}

/** My map on Progress (SPEC §21): the overview's controller, the layer chosen (shared with the skill map, in memory
 * only), where a tap goes, and the tile each idea sits in (to outline the ideas that moved today). Mounted only while
 * "Show my map" is open, so the overview is read when the learner asks for it. */
export interface ProgressMap {
  controller: MapOverviewController;
  layer: MapLayer;
  onLayer: (layer: MapLayer) => void;
  onOpen: (subjectId: string, topicId?: string) => void;
  tileOf: (kcId: string) => string | null;
}

function Hero({ map, names, progress, version }: { map: ProgressMap; names: ProgressNames; progress: ProgressView | null; version: number }) {
  const view = useSyncExternalStore(map.controller.subscribe, map.controller.getView, map.controller.getView);
  useEffect(() => {
    void map.controller.load();
  }, [map.controller, version]);
  const moved = useMemo(() => new Set((progress === null ? [] : movedToday(progress)).map((kc) => map.tileOf(kc)).filter((t): t is string => t !== null)), [progress, map]);
  return <MapOverview view={view} subject={names.subject} layer={map.layer} onLayer={map.onLayer} onOpen={map.onOpen} moved={moved} onRetry={() => void map.controller.load()} />;
}

/** The Progress place (Ctrl+5): read when it opens, and again whenever a mastery change is reported. */
export function Progress({ controller, names, actions, version = 0, map, ownCourse = false }: {
  controller: ProgressController; names: ProgressNames; actions: Omit<ProgressActions, "onRetry">; version?: number; map?: ProgressMap; ownCourse?: boolean;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  useEffect(() => {
    void controller.load();
  }, [controller, version]);
  const hero = map !== undefined && state.status !== "unavailable" ? <Hero map={map} names={names} progress={state.progress} version={version} /> : undefined;
  return <ProgressContent state={state} names={names} actions={{ ...actions, onRetry: () => void controller.load() }} {...(hero !== undefined ? { hero } : {})} ownCourse={ownCourse} />;
}
