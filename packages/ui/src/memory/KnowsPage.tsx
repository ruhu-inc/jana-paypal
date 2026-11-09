// "What the tutor knows about you" (LRN-36, UXS-28, MEM-09, MEM-14, MEM-27, LRN-37, PRV-10, PRV-11): the learner record
// in plain words, from the core's learner view. The sections follow LRN-36's order: the level and subjects, what is
// going well and what is being worked on (as words, never a score), the mistakes being watched (each can be called
// not accurate), the ways of learning (each can be set, or left to the tutor), and the notes, with the notes the
// tutor needs a decision on first: a new note that disagrees with an older one, or a change to a note that is the
// student's. A note the core keeps out of every prompt is marked, so it can be removed. The plan reason and
// learning-record inventory come from the same profile's durable projections. No probability or score is ever shown. Note text is rendered as text, never as markup (MEM-27).
// Each note says where it stands (SPEC §19.5.6): tentative until a second session's evidence supports it, supported
// after, fading after 90 days without new evidence, how many sessions the tutor recalled it in, "Not sure this is
// still right" while it waits for the student's decision, and whether it is kept on this computer (§19.6). Under "Why
// your plan looks like this", the companion's rolling summary shows its topics with session counts and its next step,
// labels only (§19.4).
import { useEffect, useSyncExternalStore } from "react";
import {
  PREFERENCE_KEYS, PREFERENCE_VALUES, noteLifecycle, type ForgetReason, type KnowsController, type KnowsKc, type KnowsNote, type KnowsReview, type KnowsView,
  type PreferenceKey, type Sharing,
} from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, EmptyState, ErrorState, Notice, Skeleton } from "../primitives/index.js";

export type { ForgetReason } from "@jana/client-core";

/** The page's sections, in the order LRN-36 lists them. */
export const KNOWS_SECTIONS = ["about", "going_well", "working_on", "mistakes", "preferences", "notes", "study", "plan", "records"] as const;
export type KnowsSection = (typeof KNOWS_SECTIONS)[number];

/** Sections whose data the core cannot serve yet: shown as not available, never as empty. */
export const NOT_YET_AVAILABLE: ReadonlySet<KnowsSection> = new Set();

/** The sharing lines for the consent state: private unless a consent is in effect, and then exactly what is shared. */
export function sharingLines(sharing: Sharing | null): string[] {
  if (sharing === null) return ["unknown"];
  const on: string[] = (["backup", "devices", "parent_view", "cloud_tutor"] as const).filter((flag) => sharing[flag]);
  // Personal tutor memory online (SPEC §19.6): the online tutor then also gets the tutor's notes, so the line says so.
  const lines = sharing.cloud_tutor && sharing.tutor_memory === true ? on.map((l) => (l === "cloud_tutor" ? "cloud_tutor_memory" : l)) : on;
  return [...(lines.length === 0 ? ["private"] : lines), "settings"];
}

export interface KnowsActions {
  onForget: (noteId: string, reason: ForgetReason) => void;
  onResolve?: (reviewId: string, accept: boolean) => void;
  onDisputeMistake?: (misconceptionId: string) => void;
  onSetPreference?: (key: PreferenceKey, value: string | boolean | null) => void;
}

const field = "min-h-8 rounded-control border border-line-strong bg-canvas px-3 text-body text-ink";

/** What the learner did in Study, as evidence the tutor saw (SPEC §19.1): the pack's words for the topic, the result,
 * the day. Study practice never changes mastery (JM-D26), and the page says so. */
function StudyActivity({ view }: { view: KnowsView }) {
  const t = useT();
  const items = view.activity ?? [];
  if (items.length === 0) return <EmptyState message={t("knows.study.empty")} />;
  // With no curriculum topic: a research pack's material is the pack's, never "your own material" (§19 fix).
  const topic = (statement: string | null, fromPack = false) => statement ?? t(fromPack ? "knows.study.from_pack" : "knows.study.no_topic");
  const result = (word: string | null) => (word === null ? "" : ` · ${t(`knows.study.result.${word}`)}`);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-meta text-muted">{t("knows.study.help")}</p>
      <ul className="flex flex-col gap-1 text-body text-ink">
        {items.map((a, i) => (
          <li key={`${a.day}:${i}`} data-study-activity={a.activity}>
            {a.activity === "aid_made"
              ? t(a.count !== null ? "knows.study.aid_made_count" : "knows.study.aid_made", { kind: a.aid_kind ?? "other", count: a.count ?? 0 })
              : t(`knows.study.${a.activity}`, { topic: topic(a.statement, a.from_pack === true) }) + result(a.result)}
            <span className="text-meta text-faint"> · {a.day}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function About({ view }: { view: KnowsView }) {
  const t = useT();
  if (view.about === null) return <EmptyState message={t("knows.about.unavailable")} />;
  const subject = (s: string) => {
    const key = `settings.profiles.subject.${s}`;
    const name = t(`settings.profiles.subject.${s}`);
    return name === key ? s : name;
  };
  const rows: [string, string][] = [
    ...(view.about.subjects.length === 0 ? [[t("knows.about.studying"), t("first_run.focus_own")] as [string, string]] : [
      [t("knows.about.level"), t(`settings.profiles.level_name.${view.about.level.toLowerCase()}`)] as [string, string],
      [t("knows.about.subjects"), view.about.subjects.map(subject).join(", ")] as [string, string],
    ]),
  ];
  if (view.about.exam_date !== null) rows.push([t("knows.about.exam"), view.about.exam_date]);
  return (
    <dl className="flex flex-col gap-1 text-ui">
      {rows.map(([label, value]) => (
        <div key={label} className="flex gap-4">
          <dt className="min-w-24 text-muted">{label}</dt>
          <dd className="text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** KCs in one band, each as its statement and its state in words. */
function Kcs({ kcs, empty }: { kcs: KnowsKc[]; empty: string }) {
  const t = useT();
  if (kcs.length === 0) return <EmptyState message={empty} />;
  return (
    <ul className="flex flex-col gap-1 text-body text-ink">
      {kcs.map((kc) => (
        <li key={kc.kc_id} data-kc-id={kc.kc_id}>
          {kc.statement} <span className="text-meta text-muted">· {t(`knows.state.${kc.state}`)}</span>
        </li>
      ))}
    </ul>
  );
}

function Mistakes({ view, onDispute }: { view: KnowsView; onDispute?: ((id: string) => void) | undefined }) {
  const t = useT();
  if (view.misconceptions.length === 0) return <EmptyState message={t("knows.mistakes.empty")} />;
  return (
    <ul className="flex flex-col">
      {view.misconceptions.map((m) => (
        <li key={m.misconception_id} className="flex flex-wrap items-center gap-2 border-b border-line py-2" data-misconception-id={m.misconception_id}>
          <span className="flex-1 text-body text-ink">
            {m.name}
            {m.status === "resolving" && <span className="text-meta text-muted"> · {t("knows.mistakes.resolving")}</span>}
          </span>
          {onDispute !== undefined && <Button onClick={() => onDispute(m.misconception_id)}>{t("knows.notes.not_right")}</Button>}
        </li>
      ))}
    </ul>
  );
}

function Preferences({ view, onSet }: { view: KnowsView; onSet?: KnowsActions["onSetPreference"] }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-2">
      {PREFERENCE_KEYS.map((key) => {
        const current = view.preferences.find((p) => p.key === key);
        const chosen = current?.source === "override" ? String(current.value) : "";
        return (
          <label key={key} className="flex flex-wrap items-center gap-3 text-ui text-ink" data-preference={key}>
            <span className="min-w-48">{t(`knows.preferences.key.${key}`)}</span>
            <select
              className={field}
              value={chosen}
              disabled={onSet === undefined}
              onChange={(event) => {
                const raw = event.target.value;
                const value = raw === "" ? null : (PREFERENCE_VALUES[key] as readonly (string | boolean)[]).find((v) => String(v) === raw) ?? null;
                onSet?.(key, value);
              }}
            >
              <option value="">{t("knows.preferences.tutor_decides")}</option>
              {(PREFERENCE_VALUES[key] as readonly (string | boolean)[]).map((v) => (
                <option key={String(v)} value={String(v)}>{t(`knows.preferences.value.${key}.${String(v)}`)}</option>
              ))}
            </select>
          </label>
        );
      })}
    </div>
  );
}

function Quoted({ label, note }: { label: string; note: KnowsNote }) {
  return (
    <p className="text-body text-ink">
      <span className="text-meta text-muted">{label} </span>
      {note.text}
    </p>
  );
}

/** One decision the tutor needs (MEM-09): the question, the note text it is about, and its two answers. */
function Review({ item, changed, onResolve }: { item: KnowsReview; changed: boolean; onResolve?: KnowsActions["onResolve"] }) {
  const t = useT();
  const kind = item.kind === "drop" ? "drop" : item.target === null ? "add" : "replace";
  return (
    <li className="flex flex-col gap-2 rounded-card border border-line-strong p-3" data-review-id={item.review_id} data-review-kind={kind}>
      {changed && <Notice message={t("knows.reviews.changed")} />}
      <p className="text-ui font-semibold text-ink">{t(`knows.reviews.question.${kind}`)}</p>
      {item.target !== null && <Quoted label={t(kind === "replace" ? "knows.reviews.older" : "knows.reviews.yours")} note={item.target} />}
      {item.pending !== null && <Quoted label={t("knows.reviews.newer")} note={item.pending} />}
      {onResolve !== undefined && (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => onResolve(item.review_id, true)}>{t(`knows.reviews.accept.${kind}`)}</Button>
          <Button onClick={() => onResolve(item.review_id, false)}>{t(`knows.reviews.reject.${kind}`)}</Button>
        </div>
      )}
    </li>
  );
}

function Note({ note, onForget }: { note: KnowsNote; onForget: KnowsActions["onForget"] }) {
  const t = useT();
  const life = noteLifecycle(note);
  return (
    <li className="flex flex-col gap-1 border-b border-line py-2" data-note-id={note.note_id} data-flagged={note.flagged}
      data-confidence={life.confidence} data-fading={life.fading} data-withheld-online={life.withheld_online}>
      <p className="text-body text-ink">{note.text}</p>
      {note.explanation && <details className="text-meta text-muted" data-memory-explanation="">
        <summary>{t("knows.notes.why_saved")}</summary>
        <p>{note.explanation.meaning}</p>
        <ul>{note.explanation.sources.map((source) => <li key={source.event_id}>
          {source.kind.replaceAll("_", " ")} · {new Date(source.at).toLocaleDateString()}
        </li>)}</ul>
        <p>{note.explanation.correction}</p>
      </details>}
      <p className="flex flex-wrap gap-x-2 text-meta text-muted" data-note-life="">
        <span data-note-label={life.confidence}>{t(`knows.notes.${life.confidence}`)}</span>
        {life.fading && <span data-note-label="fading">· {t("knows.notes.fading")}</span>}
        {life.recall_sessions > 0 && <span data-note-recalled={life.recall_sessions}>· {t("knows.notes.recalled", { count: life.recall_sessions })}</span>}
      </p>
      {note.status === "pending_review" && <p data-note-pending="" className="text-meta text-warn">{t("knows.notes.pending")}</p>}
      {life.withheld_online && (
        <p data-note-withheld="" className="flex items-center gap-1 text-meta text-muted">
          <Icon name="on-this-computer" />
          {t("knows.notes.withheld_online")}
        </p>
      )}
      {note.flagged && (
        <p className="flex items-center gap-1 text-meta text-warn">
          <Icon name="warning" />
          {t("knows.notes.flagged")}
        </p>
      )}
      <p className="text-meta text-muted">
        {t("knows.notes.saved_on", { date: note.updated_ts })}
        {note.source === "student" && <> · {t("knows.notes.you_asked")}</>}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => onForget(note.note_id, "not_right")}>{t("knows.notes.not_right")}</Button>
        <Button onClick={() => onForget(note.note_id, "forget")}>{t("knows.notes.forget")}</Button>
      </div>
    </li>
  );
}

function TeachingObservations({ view }: { view: KnowsView }) {
  const t = useT();
  const observations = view.teaching_observations ?? [];
  if (observations.length === 0) return null;
  return <section className="mt-4 flex flex-col gap-2" data-teaching-observations="">
    <h3 className="text-ui font-semibold">{t("knows.teaching.title")}</h3>
    <p className="text-meta text-muted">{t("knows.teaching.help")}</p>
    <ul className="flex flex-col gap-3">{observations.map((observation) =>
      <li key={`${observation.source_event_id}:${observation.intervention}:${observation.kc_id}`} className="text-body">
        <p>{observation.topic} · {observation.intervention.replaceAll("-", " ")}</p>
        <p className="text-meta text-muted">{new Date(observation.at).toLocaleDateString()} · {t("knows.teaching.tentative")}</p>
        {(["immediate", "delayed"] as const).map((kind) => {
          const result = observation[kind];
          return <p key={kind} className="text-meta">{t(`knows.teaching.${kind}`)}: {result
            ? `${t(`knows.study.result.${result.outcome}`)} · ${t("knows.teaching.hints", { count: result.hints_used })} · ${new Date(result.at).toLocaleDateString()}`
            : t("knows.teaching.not_observed")}</p>;
        })}
        {observation.conflicting_outcomes && <p className="text-meta">{t("knows.teaching.conflict")}</p>}
      </li>)}</ul>
  </section>;
}

function Notes({ view, actions }: { view: KnowsView; actions: KnowsActions }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-3">
      {view.reviews.length > 0 && (
        <div className="flex flex-col gap-2" data-reviews>
          <h3 className="text-ui font-semibold text-ink">{t("knows.reviews.title")}</h3>
          <ul className="flex flex-col gap-2">
            {view.reviews.map((item) => <Review key={item.review_id} item={item} changed={view.changed === item.review_id} onResolve={actions.onResolve} />)}
          </ul>
        </div>
      )}
      {view.notes.length === 0 ? (
        <EmptyState message={t("knows.notes.empty")} />
      ) : (
        <>
        <p className="text-meta text-muted">{t("knows.notes.lifecycle_help")}</p>
        <ul className="flex flex-col">
          {view.notes.map((note) => <Note key={note.note_id} note={note} onForget={actions.onForget} />)}
        </ul>
        </>
      )}
    </div>
  );
}

/** Why the plan looks as it does, from the companion's rolling summary (SPEC §19.4, §19.5.3): the topics the thread
 * saw with how many sessions each came up in, and the plan's next step. Written by code only; shown as labels. */
function CompanionSummary({ view }: { view: KnowsView }) {
  const t = useT();
  const summary = view.companion_summary ?? null;
  if (summary === null || (summary.topics.length === 0 && summary.next_step === null)) return null;
  return (
    <div className="flex flex-col gap-1" data-companion-summary="">
      <h3 className="text-meta font-medium text-muted">{t("knows.summary.title")}</h3>
      {summary.topics.length > 0 && (
        <ul className="flex flex-col gap-1 text-body text-ink">
          {summary.topics.map((topic) => <li key={topic.kc_id} data-summary-kc={topic.kc_id}>{t("knows.summary.topic", { label: topic.label, count: topic.sessions })}</li>)}
        </ul>
      )}
      {summary.next_step !== null && <p data-summary-next="" className="text-body text-ink">{t("knows.summary.next_step", { step: summary.next_step.text })}</p>}
    </div>
  );
}

/** The page from its view: the gallery and the tests render this directly. */
export function KnowsContent({ view, onRetry, onOpenSettings, onClose, ...actions }: KnowsActions & {
  view: KnowsView;
  onRetry?: () => void;
  onOpenSettings?: () => void;
  onClose?: () => void;
}) {
  const t = useT();
  const loading = view.status === "idle" || view.status === "loading";
  const section = (id: KnowsSection) => {
    if (NOT_YET_AVAILABLE.has(id)) return <EmptyState message={t("knows.not_yet_available")} />;
    switch (id) {
      case "about":
        return <About view={view} />;
      case "going_well":
        return <Kcs kcs={view.kcs.filter((k) => k.band === "secure")} empty={t("knows.going_well.empty")} />;
      case "working_on":
        return <><Kcs kcs={view.kcs.filter((k) => k.band === "learning")} empty={t("knows.working_on.empty")} />
          <TeachingObservations view={view} /></>;
      case "mistakes":
        return <Mistakes view={view} onDispute={actions.onDisputeMistake} />;
      case "preferences":
        return <Preferences view={view} {...(actions.onSetPreference !== undefined ? { onSet: actions.onSetPreference } : {})} />;
      case "plan":
        return <>
          <p className="text-body text-ink">{t(view.plan_reason ? `knows.plan.${view.plan_reason}` : "knows.plan.empty")}</p>
          <CompanionSummary view={view} />
        </>;
      case "study":
        return <StudyActivity view={view} />;
      case "records":
        return (view.records?.length ?? 0) === 0 ? <EmptyState message={t("knows.records.empty")} /> : (
          <ul className="flex flex-col gap-1 text-body text-ink">
            {view.records?.map((record) => <li key={record.kind} data-record-kind={record.kind}>{t(`knows.records.${record.kind}`)}: {record.count}</li>)}
          </ul>
        );
      default:
        return <Notes view={view} actions={actions} />;
    }
  };
  return (
    <div className="flex flex-col gap-6 p-6" data-knows-status={view.status}>
      <div className="flex items-center justify-between">
        <h1 className="text-heading font-semibold text-ink">{t("knows.title")}</h1>
        {onClose !== undefined && <Button onClick={onClose}>{t("action.back")}</Button>}
      </div>
      <section aria-labelledby="knows-sharing" className="flex items-start gap-2 text-ui text-ink">
        <Icon name="on-this-computer" />
        <div className="flex flex-col gap-1">
          <h2 id="knows-sharing" className="text-ui font-semibold">{t("knows.sharing.title")}</h2>
          {sharingLines(view.status === "ready" ? view.sharing : null).map((line) => (
            <p key={line} data-sharing={line}>{t(`knows.sharing.${line}`)}</p>
          ))}
        </div>
      </section>
      {view.memory_saving === false && (
        <Notice message={t("knows.saving_off")} {...(onOpenSettings !== undefined ? { action: { label: t("app.settings"), onClick: onOpenSettings } } : {})} />
      )}
      {view.forgotten !== null && <Notice message={t("knows.notes.forgotten")} />}
      {view.error !== null && <ErrorState message={t(view.errorOn === "note" ? "knows.notes.forget_failed" : "knows.correction_failed")} />}
      {view.status === "failed" ? (
        <ErrorState message={t("knows.unavailable")} {...(onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: onRetry } } : {})} />
      ) : (
        KNOWS_SECTIONS.map((id) => (
          <section key={id} aria-labelledby={`knows-${id}`} className="flex flex-col gap-2" data-section={id}>
            <h2 id={`knows-${id}`} className="text-ui font-semibold text-ink">{t(`knows.section.${id}`)}</h2>
            {loading ? <Skeleton lines={2} /> : section(id)}
          </section>
        ))
      )}
    </div>
  );
}

/** The page for the unlocked profile: reads on open, forgets and corrects through the controller. */
export function KnowsPage({ knows, onClose, onOpenSettings }: { knows: KnowsController; onClose?: () => void; onOpenSettings?: () => void }) {
  const view = useSyncExternalStore(knows.subscribe, knows.getView, knows.getView);
  useEffect(() => {
    knows.load().catch(() => undefined); // a locked profile reads nothing; the app shows the picker
  }, [knows]);
  const retry = () => void knows.load().catch(() => undefined);
  return (
    <KnowsContent
      view={view}
      onForget={(noteId, reason) => void knows.forget(noteId, reason).catch(() => undefined)}
      onResolve={(reviewId, accept) => void knows.resolveReview(reviewId, accept).catch(() => undefined)}
      onDisputeMistake={(id) => void knows.disputeMisconception(id).catch(() => undefined)}
      onSetPreference={(key, value) => void knows.setPreference(key, value).catch(() => undefined)}
      onRetry={retry}
      {...(onOpenSettings !== undefined ? { onOpenSettings } : {})}
      {...(onClose !== undefined ? { onClose } : {})}
    />
  );
}
