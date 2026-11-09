// Past papers and exam dates (ASM-27, LRN-44; D119; direction A "Studio").
//
// In the Mock exam place, under the papers on this computer: "Your past papers", the papers the student imported, each
// with its time, marks and how its marks are decided (checked by tools, against the mark scheme, or judged), "Sit this
// paper" (timed like a mock: `MockController.choose` with its id) and Remove; and "Import a past paper": a title, the
// subject, the time allowed, the paper (a source already read in the study area, or its text pasted here) and, if the
// student has it, the mark scheme the same way. A link is not opened (the app fetches no web page, STU-13): the student
// saves the paper and adds the file in the study area. After an import the questions found are listed with their own
// numbers, marks and marking, before the paper is sat.
//
// "Exam dates": per subject or paper, a name ("Physics paper 2"), a date and an optional time, each with its reminders
// switch (device-local reminders 14, 7, 3 and 1 days before and on the morning, LRN-44); Home shows the countdown to
// the nearest and any reminder due, which the student puts away. Dates and reminders never leave the computer.
import { useId, useState, type ReactNode } from "react";
import type { ExamCountdown, ExamDatesView, ExamReminder, PastPaperDraft, PastPaperSubject, PastPapersView, PastPaperView } from "@jana/client-core";
import type { api_commands } from "@jana/contracts-ts";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, Callout, Chip, EmptyState, ErrorState, FIELD_LOOK, Skeleton, SubjectDot, Switch } from "../primitives/index.js";
import { PackInline } from "../learn/PackText.js";

type Method = api_commands.PastPaperQuestion["marking"];

/** The words for how a mark is decided (ASM-27, STU-18): a judged mark always says so. */
export function MarkingLabel({ method }: { method: Method }) {
  const t = useT();
  const words: Record<Method, string> = {
    checked: t("past_paper.marking.checked"),
    key: t("past_paper.marking.key"),
    judged: t("past_paper.marking.judged"),
    not_marked: t("past_paper.marking.not_marked"),
  };
  return (
    <Chip size="sm" tone={method === "checked" || method === "key" ? "verified" : "hint"} icon={method === "checked" || method === "key" ? "verified" : "info"}>
      <span data-marking={method}>{words[method]}</span>
    </Chip>
  );
}

export interface PastPaperSubjectOption {
  subject: PastPaperSubject;
  label: string;
}

export interface PastPaperActions {
  onImport: (draft: PastPaperDraft) => void;
  onSit: (paperId: string) => void;
  onRemove: (paperId: string) => void;
  onDismiss: () => void;
  onRetry?: () => void;
}

type Choice = { kind: "none" } | { kind: "source"; sourceId: string } | { kind: "text"; text: string };

function SourcePick({ label, value, onChange, view, optional }: { label: string; value: Choice; onChange: (choice: Choice) => void; view: PastPapersView; optional: boolean }) {
  const t = useT();
  const id = useId();
  const selected = value.kind === "source" ? `src:${value.sourceId}` : value.kind;
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-meta font-medium text-muted">{label}</legend>
      <select id={id} aria-label={label} className={FIELD_LOOK} value={selected} onChange={(e) => {
        const next = e.target.value;
        onChange(next === "none" ? { kind: "none" } : next === "text" ? { kind: "text", text: "" } : { kind: "source", sourceId: next.slice(4) });
      }}>
        {optional && <option value="none">{t("past_paper.form.no_scheme")}</option>}
        <option value="text">{t("past_paper.form.paste")}</option>
        {view.sources.map((s) => <option key={s.sourceId} value={`src:${s.sourceId}`}>{s.title ?? t("past_paper.form.private_source")}</option>)}
      </select>
      {value.kind === "text" && (
        <textarea aria-label={t("past_paper.form.paste_label", { what: label })} className={`${FIELD_LOOK} min-h-32 py-2`} value={value.text}
          onChange={(e) => onChange({ kind: "text", text: e.target.value })} />
      )}
    </fieldset>
  );
}

function ImportForm({ view, subjects, onImport, onCancel }: { view: PastPapersView; subjects: PastPaperSubjectOption[]; onImport: (draft: PastPaperDraft) => void; onCancel: () => void }) {
  const t = useT();
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState<PastPaperSubject>(subjects[0]?.subject ?? "mathematics");
  const [minutes, setMinutes] = useState(60);
  const [paper, setPaper] = useState<Choice>({ kind: "text", text: "" });
  const [scheme, setScheme] = useState<Choice>({ kind: "none" });
  const asDraft = (c: Choice): PastPaperDraft["paper"] | null => c.kind === "source" ? { sourceId: c.sourceId } : c.kind === "text" && c.text.trim() !== "" ? { text: c.text } : null;
  const paperDraft = asDraft(paper);
  const schemeDraft = scheme.kind === "none" ? null : asDraft(scheme);
  const ready = title.trim() !== "" && paperDraft !== null && (scheme.kind === "none" || schemeDraft !== null) && minutes >= 1;
  return (
    <form className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-5" data-past-paper-form=""
      onSubmit={(e) => { e.preventDefault(); if (ready && paperDraft !== null) onImport({ title, subject, minutes, paper: paperDraft, scheme: schemeDraft }); }}>
      <h3 className="text-ui font-semibold text-ink">{t("past_paper.import")}</h3>
      <p className="text-meta text-muted">{t("past_paper.form.intro")}</p>
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">{t("past_paper.form.title")}
        <input className={FIELD_LOOK} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <div className="flex flex-wrap gap-4">
        <label className="flex min-w-48 flex-1 flex-col gap-1.5 text-meta font-medium text-muted">{t("past_paper.form.subject")}
          <select className={FIELD_LOOK} value={subject} onChange={(e) => setSubject(e.target.value as PastPaperSubject)}>
            {subjects.map((s) => <option key={s.subject} value={s.subject}>{s.label}</option>)}
          </select>
        </label>
        <label className="flex w-40 flex-col gap-1.5 text-meta font-medium text-muted">{t("past_paper.form.minutes")}
          <input className={FIELD_LOOK} type="number" min={1} max={600} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} />
        </label>
      </div>
      <SourcePick label={t("past_paper.form.paper")} value={paper} onChange={setPaper} view={view} optional={false} />
      <SourcePick label={t("past_paper.form.scheme")} value={scheme} onChange={setScheme} view={view} optional />
      <p className="flex items-start gap-2 text-meta text-faint"><Icon name="info" size={16} />{t("past_paper.form.links")}</p>
      <div className="flex flex-wrap gap-2">
        <Button tone="primary" type="submit" disabled={!ready || view.importing}>{t(view.importing ? "past_paper.importing" : "past_paper.form.go")}</Button>
        <Button tone="ghost" type="button" onClick={onCancel}>{t("action.cancel")}</Button>
      </div>
    </form>
  );
}

/** Why an import was refused, by the core's reason code. */
const REFUSALS: Record<string, string> = {
  no_questions_found: "no_questions_found", feature_disabled: "feature_disabled", confirm_needed: "unread", unreadable_source: "unread",
  grant_expired: "unread", paper_too_long: "too_long", too_many_past_papers: "too_many", tutor_locked: "locked",
};

function Imported({ paper, onSit, onDismiss }: { paper: PastPaperView; onSit: () => void; onDismiss: () => void }) {
  const t = useT();
  return (
    <section aria-labelledby="past-paper-found" className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5" data-past-paper-found={paper.paper_id}>
      <h3 id="past-paper-found" className="text-ui font-semibold text-ink">{t("past_paper.found", { count: paper.questions_found.length })}</h3>
      {!paper.linked && <p className="text-meta text-muted">{t("past_paper.not_linked")}</p>}
      <ul className="flex flex-col divide-y divide-line">
        {paper.questions_found.map((q) => (
          <li key={`${q.question_id}:${q.number_label}`} className="flex flex-wrap items-center justify-between gap-2 py-2">
            <span className="text-ui text-ink">{t("past_paper.question", { label: q.number_label })} <span className="text-meta text-muted tabular-nums">{t("check.marks", { marks: q.marks })}</span></span>
            <MarkingLabel method={q.marking} />
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button tone="primary" onClick={onSit}><Icon name="timer" size={16} />{t("past_paper.sit")}</Button>
        <Button tone="ghost" onClick={onDismiss}>{t("past_paper.later")}</Button>
      </div>
    </section>
  );
}

/** "Your past papers" and the import, for the Mock exam place. */
export function PastPaperShelf({ view, subjects, actions, locked }: { view: PastPapersView; subjects: PastPaperSubjectOption[]; actions: PastPaperActions; locked: boolean }) {
  const t = useT();
  const [importing, setImporting] = useState(false);
  const label = (subject: string) => subjects.find((s) => s.subject === subject)?.label ?? subject;
  let body: ReactNode;
  if (view.status === "idle" || view.status === "loading") body = <Skeleton lines={2} />;
  else if (view.status === "unavailable") body = <p className="text-ui text-muted">{t("past_paper.unavailable")}</p>;
  else if (view.status === "failed") body = <ErrorState message={t("past_paper.failed")} {...(actions.onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: actions.onRetry } } : {})} />;
  else {
    const imported = view.imported;
    body = (
      <>
        {view.refused !== null && <Callout tone="hint" icon="info" heading={t(`past_paper.refused.${REFUSALS[view.refused] ?? "other"}`)} />}
        {imported !== null && <Imported paper={imported} onSit={() => actions.onSit(imported.paper_id)} onDismiss={actions.onDismiss} />}
        {importing ? <ImportForm view={view} subjects={subjects} onCancel={() => setImporting(false)} onImport={(draft) => { actions.onImport(draft); setImporting(false); }} />
          : <div><Button disabled={locked || view.importing} onClick={() => setImporting(true)}><Icon name="study-area" size={16} />{t(view.importing ? "past_paper.importing" : "past_paper.import")}</Button></div>}
        {view.papers.length === 0 ? <EmptyState message={t("past_paper.empty")} /> : (
          <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5">
            {view.papers.map((p) => (
              <li key={p.paper_id} data-past-paper-id={p.paper_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <span className="flex min-w-0 flex-col gap-0.5">
                  <span className="flex items-center gap-1.5 text-ui font-medium text-ink"><SubjectDot subject={p.subject} /><PackInline text={p.title} /></span>
                  <span className="text-meta text-muted tabular-nums">
                    {[label(p.subject), t("mock.row_meta", { minutes: p.duration_minutes, questions: p.questions }), t("past_paper.marking_summary", { checked: p.marking.checked + p.marking.key, judged: p.marking.judged })].join(" · ")}
                  </span>
                  {!p.has_mark_scheme && <span className="text-meta text-faint">{t("past_paper.no_scheme")}</span>}
                </span>
                <span className="flex flex-wrap items-center gap-2">
                  <Button size="sm" disabled={locked} onClick={() => actions.onSit(p.paper_id)}>{t("past_paper.sit")}</Button>
                  <Button tone="ghost" size="sm" disabled={locked} onClick={() => actions.onRemove(p.paper_id)}>{t("past_paper.remove")}</Button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </>
    );
  }
  return (
    <section aria-labelledby="past-papers" className="flex flex-col gap-3" data-past-papers={view.status}>
      <div className="flex flex-col gap-1">
        <h2 id="past-papers" className="text-heading text-ink">{t("past_paper.title")}</h2>
        <p className="text-ui text-muted">{t("past_paper.intro")}</p>
      </div>
      {body}
    </section>
  );
}

// --- exam dates --------------------------------------------------------------------------------------------------------

export interface ExamDateActions {
  onAdd: (draft: { subject: PastPaperSubject; name: string; date: string; time: string | null; reminders: boolean }) => void;
  onReminders: (examId: string, on: boolean) => void;
  onRemove: (examId: string) => void;
}

/** A local date "2027-05-20" as the learner reads it. */
function dateWords(date: string): string {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
}

export function ExamDatesPanel({ view, subjects, actions }: { view: ExamDatesView; subjects: PastPaperSubjectOption[]; actions: ExamDateActions }) {
  const t = useT();
  const [adding, setAdding] = useState(false);
  const [subject, setSubject] = useState<PastPaperSubject>(subjects[0]?.subject ?? "mathematics");
  const [name, setName] = useState("");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const label = (s: string) => subjects.find((x) => x.subject === s)?.label ?? s;
  if (view.status === "unavailable") return null;
  return (
    <section aria-labelledby="exam-dates" className="flex flex-col gap-3" data-exam-dates={view.status}>
      <div className="flex flex-col gap-1">
        <h2 id="exam-dates" className="text-heading text-ink">{t("exam_dates.title")}</h2>
        <p className="text-ui text-muted">{t("exam_dates.intro")}</p>
      </div>
      {view.status === "loading" || view.status === "idle" ? <Skeleton lines={2} /> : (
        <>
          {view.exams.length === 0 ? <EmptyState message={t("exam_dates.empty")} /> : (
            <ul className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5">
              {view.exams.map((e) => (
                <li key={e.exam_id} data-exam-date={e.exam_id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <span className="flex min-w-0 flex-col gap-0.5">
                    <span className="flex items-center gap-1.5 text-ui font-medium text-ink"><SubjectDot subject={e.subject} />{e.name}</span>
                    <span className="text-meta text-muted">{e.time === null ? dateWords(e.date) : t("exam_dates.when", { date: dateWords(e.date), time: e.time })} · {label(e.subject)}</span>
                  </span>
                  <span className="flex flex-wrap items-center gap-3">
                    <Switch label={t("exam_dates.reminders")} checked={e.reminders} disabled={view.busy} onChange={(on) => actions.onReminders(e.exam_id, on)} />
                    <Button tone="ghost" size="sm" disabled={view.busy} onClick={() => actions.onRemove(e.exam_id)}>{t("exam_dates.remove")}</Button>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {view.refused !== null && <Callout tone="hint" icon="info" heading={t("exam_dates.refused")} />}
          {adding ? (
            <form className="flex flex-wrap items-end gap-3 rounded-card border border-line bg-canvas p-5" data-exam-date-form=""
              onSubmit={(e) => { e.preventDefault(); if (name.trim() !== "" && date !== "") { actions.onAdd({ subject, name, date, time: time === "" ? null : time, reminders: true }); setAdding(false); setName(""); setDate(""); setTime(""); } }}>
              <label className="flex min-w-40 flex-1 flex-col gap-1.5 text-meta font-medium text-muted">{t("exam_dates.subject")}
                <select className={FIELD_LOOK} value={subject} onChange={(e) => setSubject(e.target.value as PastPaperSubject)}>
                  {subjects.map((s) => <option key={s.subject} value={s.subject}>{s.label}</option>)}
                </select>
              </label>
              <label className="flex min-w-48 flex-1 flex-col gap-1.5 text-meta font-medium text-muted">{t("exam_dates.name")}
                <input className={FIELD_LOOK} value={name} maxLength={80} placeholder={t("exam_dates.name_example")} onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">{t("exam_dates.date")}
                <input className={FIELD_LOOK} type="date" value={date} onChange={(e) => setDate(e.target.value)} />
              </label>
              <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">{t("exam_dates.time")}
                <input className={FIELD_LOOK} type="time" value={time} onChange={(e) => setTime(e.target.value)} />
              </label>
              <div className="flex gap-2">
                <Button tone="primary" type="submit" disabled={name.trim() === "" || date === "" || view.busy}>{t("exam_dates.save")}</Button>
                <Button tone="ghost" type="button" onClick={() => setAdding(false)}>{t("action.cancel")}</Button>
              </div>
            </form>
          ) : <div><Button onClick={() => setAdding(true)}><Icon name="take-your-time" size={16} />{t("exam_dates.add")}</Button></div>}
          <p className="text-meta text-faint">{t("exam_dates.private")}</p>
        </>
      )}
    </section>
  );
}

/** Home's countdown to the nearest exam and the reminders due (LRN-44): plain words, never pressure. */
export function ExamCountdownCard({ countdown, reminders, onDismiss, onOpen }: {
  countdown: ExamCountdown | null; reminders: ExamReminder[]; onDismiss: (reminder: ExamReminder) => void; onOpen?: () => void;
}) {
  const t = useT();
  if (countdown === null && reminders.length === 0) return null;
  return (
    <section aria-label={t("exam_dates.countdown_label")} className="flex flex-col gap-2 rounded-card border border-line bg-canvas px-5 py-4" data-exam-countdown={countdown?.days ?? ""}>
      {countdown !== null && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="flex items-center gap-2 text-ui font-medium text-ink">
            <Icon name="take-your-time" size={16} /><SubjectDot subject={countdown.subject} />
            {countdown.days === 0 ? t("exam_dates.today", { name: countdown.name }) : t("exam_dates.countdown", { days: countdown.days, name: countdown.name })}
          </p>
          {onOpen !== undefined && <Button size="sm" tone="ghost" onClick={onOpen}>{t("exam_dates.open")}</Button>}
        </div>
      )}
      {reminders.map((r) => (
        <div key={`${r.exam_id}:${r.kind_id}`} data-exam-reminder={r.kind_id} className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-2">
          <p className="text-meta text-muted">{r.days === 0 ? t("exam_dates.reminder_today", { name: r.name }) : t("exam_dates.reminder", { days: r.days, name: r.name })}</p>
          <Button size="sm" tone="ghost" onClick={() => onDismiss(r)}>{t("exam_dates.dismiss")}</Button>
        </div>
      ))}
    </section>
  );
}
