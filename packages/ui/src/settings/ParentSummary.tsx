// Settings → "Parent summary" and "Alerts" (UXS-33, TCH-12, TCH-14, TABLE UXS-46a, JRN-13; DSN-22 for print). The
// summary is the latest weekly digest of the open student, worded here from templates around the core's numbers and
// the packs' own topic names: the opening line, time studied by day, ideas secured, ideas to watch, mock results, help
// used (full solutions and the last hint), the streak and the days to the exam, then one suggested conversation
// starter. An empty week says "No study sessions this week."; a week with too little to summarise says so and shows the
// time studied only (JRN-13 F1). It never holds a conversation, a note or a safety detail. "Print or save as PDF" prints
// the summary alone on A4 in the print theme of tokens.json, with the product's name and the date generated at the top
// and "Page n of m" at the foot (DSN-22); the system's print dialog saves it as a PDF, with no network. Alerts list the
// open student's alerts newest first by kind and date, never message text, each with "Put away"; the wellbeing alert
// names its kind only. Nothing here changes a setting (D53) or leaves the computer.
import { flushSync } from "react-dom";
import { useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { ParentController, conversationStarter, hoursAndMinutes, type AlertView, type ParentDigest, type ParentView, type ProfileController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { MasteryMark } from "../learn/Mastery.js";
import { BrandMark } from "../primitives/BrandMark.js";
import { Button, EmptyState, ErrorState, Notice, Skeleton } from "../primitives/index.js";
import { useProfiles } from "../profiles/ProfilePicker.js";

/** Local noon of a `YYYY-MM-DD` day, so its date never shifts with the time zone. */
const day = (text: string) => new Date(`${text}T12:00:00`);

function Block({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2 break-inside-avoid">
      <h3 className="text-ui font-semibold text-ink">{heading}</h3>
      {children}
    </section>
  );
}

/** The opening line (TCH-12's template): time, days and ideas secured, by subject where there is one. */
function Opening({ digest, name }: { digest: ParentDigest; name: string }) {
  const t = useT();
  const { hours, minutes } = hoursAndMinutes(digest.minutes_total);
  const [only, ...others] = [...new Set(digest.mastered.map((k) => k.subject))];
  const args = { name, hours, minutes, days: digest.days_studied, count: digest.mastered.length };
  const line = only !== undefined && others.length === 0
    ? t("parents.opening.subject", { ...args, subject: t(`settings.profiles.subject.${only}`) })
    : t("parents.opening", args);
  return <p data-parent-opening="" className="text-body text-ink">{line}</p>;
}

function TimeByDay({ digest }: { digest: ParentDigest }) {
  const t = useT();
  const most = Math.max(1, ...digest.time_by_day.map((d) => d.minutes));
  return (
    <table className="w-full text-ui text-ink">
      <caption className="sr-only">{t("parents.time.caption")}</caption>
      <thead className="sr-only"><tr><th scope="col">{t("parents.time.day")}</th><th scope="col">{t("parents.time.minutes")}</th></tr></thead>
      <tbody>
        {digest.time_by_day.map((d) => (
          <tr key={d.day} data-parent-day={d.day} className="border-b border-line last:border-b-0">
            <th scope="row" className="w-36 py-1 text-start font-regular text-muted">{t("parents.time.weekday", { date: day(d.day) })}</th>
            <td className="py-1">
              <span className="flex items-center gap-3">
                <span aria-hidden="true" className="h-2 rounded-chip bg-ink" style={{ width: `${(d.minutes / most) * 60}%` }} />
                <span>{t("parents.time.value", { minutes: d.minutes })}</span>
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Ideas({ kcs, state, empty }: { kcs: ParentDigest["mastered"] | ParentDigest["to_watch"]; state: (reason?: string) => "mastered" | "learning" | "moved_on"; empty: string }) {
  const t = useT();
  if (kcs.length === 0) return <p className="text-ui text-muted">{empty}</p>;
  return (
    <ul className="flex flex-col gap-2">
      {kcs.map((kc) => (
        <li key={kc.kc_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui text-ink">
          <MasteryMark state={state("reason" in kc ? kc.reason : undefined)} />
          <span>{kc.topic_title ?? t(`settings.profiles.subject.${kc.subject}`)}{kc.statement !== null && <span className="text-muted">{" · "}{kc.statement}</span>}</span>
        </li>
      ))}
    </ul>
  );
}

/** The summary's words from a digest, for the section and for print (UXS-33). `printing` adds the report header
 * (DSN-22): the product's lockup and name and the date generated. */
export function ParentSummaryContent({ view, name, printing = false, onRetry, onPrint }: {
  view: Pick<ParentView, "digestStatus" | "digest">; name: string; printing?: boolean; onRetry: () => void; onPrint?: () => void;
}) {
  const t = useT();
  const digest = view.digest;
  if (digest === null) {
    if (view.digestStatus === "unavailable") return <EmptyState message={t("parents.unavailable")} />;
    if (view.digestStatus === "failed") return <ErrorState message={t("parents.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} />;
    return <div role="status" aria-busy="true"><span className="sr-only">{t("parents.loading")}</span><Skeleton lines={5} /></div>;
  }
  const starter = conversationStarter(digest);
  const full = digest.activity === "enough";
  return (
    <article data-print-report="" data-parent-summary={digest.activity} aria-label={t("parents.title", { name })} className="flex flex-col gap-5 py-4">
      {printing && (
        <header data-print-header="" className="flex items-center justify-between gap-4 border-b border-line pb-3">
          <span className="flex items-center gap-3"><BrandMark kind="lockup" height={24} /><span className="text-ui font-semibold text-ink">{t("app.name")}</span></span>
          <span className="text-meta text-muted">{t("parents.generated", { date: new Date(digest.generated_at) })}</span>
        </header>
      )}
      <div className="flex flex-col gap-1">
        <h3 className="text-heading text-ink">{t("parents.title", { name })}</h3>
        <p className="text-meta text-muted">{t("parents.week", { start: day(digest.week_start), end: day(digest.week_end) })}</p>
      </div>
      {digest.activity === "none" ? <p data-parent-opening="" className="text-body text-ink">{t("parents.empty")}</p>
        : digest.activity === "low" ? <p data-parent-opening="" className="text-body text-ink">{t("parents.too_little", { name, minutes: digest.minutes_total })}</p>
        : <Opening digest={digest} name={name} />}
      {digest.activity !== "none" && <Block heading={t("parents.time.title")}><TimeByDay digest={digest} /></Block>}
      {full && (
        <>
          <Block heading={t("parents.secured.title")}>
            <Ideas kcs={digest.mastered} state={() => "mastered"} empty={t("parents.secured.none")} />
          </Block>
          <Block heading={t("parents.watch.title")}>
            <Ideas kcs={digest.to_watch} state={(reason) => (reason === "moved_on" ? "moved_on" : "learning")} empty={t("parents.watch.none")} />
          </Block>
          {digest.mocks.length > 0 && (
            <Block heading={t("parents.mocks.title")}>
              <ul className="flex flex-col gap-1 text-ui text-ink">
                {digest.mocks.map((m) => (
                  <li key={m.exam_id}>
                    {m.title ?? t("parents.mocks.untitled")}{" · "}
                    {m.marks_awarded !== null && m.marks_available !== null
                      ? t("parents.mocks.marks", { awarded: m.marks_awarded, available: m.marks_available })
                      : t("parents.mocks.not_marked")}
                  </li>
                ))}
              </ul>
            </Block>
          )}
          <Block heading={t("parents.help.title")}>
            <p className="text-ui text-ink">{t("parents.help.solutions", { count: digest.solutions_viewed })}</p>
            <p className="text-ui text-ink">{t("parents.help.bottom_out", { count: digest.bottom_out_hints, steps: digest.steps })}</p>
            <p className="text-ui text-ink">{t("parents.help.hints", { count: digest.steps_with_hints, steps: digest.steps })}</p>
          </Block>
        </>
      )}
      {(digest.streak_days > 0 || digest.exam !== null) && (
        <p className="text-ui text-ink">
          {[
            ...(digest.streak_days > 0 ? [t("parents.streak", { days: digest.streak_days })] : []),
            ...(digest.exam !== null ? [t("parents.exam", { days: digest.exam.days, subject: t(`settings.profiles.subject.${digest.exam.subject}`) })] : []),
          ].join(" · ")}
        </p>
      )}
      <Block heading={t("parents.starter.title")}>
        <p data-parent-starter={starter.kind} className="text-ui text-ink">{t(`parents.starter.${starter.kind}`, { name, topic: starter.topic ?? "" })}</p>
      </Block>
      {!printing && (
        <div data-print-hide="" className="flex flex-wrap items-center gap-3">
          {onPrint !== undefined && <Button onClick={onPrint}>{t("parents.print")}</Button>}
          <span className="text-meta text-muted">{t("parents.local")}</span>
        </div>
      )}
    </article>
  );
}

/** Print the summary alone (DSN-22): the header joins the page for the print, and the page footer's words are set on
 * the document for the `@page` rule ("Page n of m"). Printing from the browser's own menu does the same. */
function usePrinting(): [boolean, () => void] {
  const t = useT();
  const [printing, setPrinting] = useState(false);
  useEffect(() => {
    const root = document.documentElement;
    const before = () => {
      root.style.setProperty("--print-page-label", JSON.stringify(t("parents.print.page")));
      root.style.setProperty("--print-page-of", JSON.stringify(t("parents.print.of")));
      flushSync(() => setPrinting(true));
    };
    const after = () => setPrinting(false);
    window.addEventListener("beforeprint", before);
    window.addEventListener("afterprint", after);
    return () => { window.removeEventListener("beforeprint", before); window.removeEventListener("afterprint", after); };
  }, [t]);
  return [printing, () => window.print()];
}

export function ParentSummary({ profiles, controller: given }: { profiles: ProfileController; controller?: ParentController }) {
  const t = useT();
  const controller = useMemo(() => given ?? new ParentController(profiles), [given, profiles]);
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const people = useProfiles(profiles);
  const name = people.profiles.find((p) => p.profile_id === people.active)?.display_name ?? "";
  const [printing, print] = usePrinting();
  useEffect(() => { void controller.loadDigest(); }, [controller, people.active]);
  return (
    <div className="flex flex-col gap-2">
      <Notice message={t("parents.about")} />
      <ParentSummaryContent view={view} name={name} printing={printing} onRetry={() => void controller.loadDigest()} onPrint={print} />
    </div>
  );
}

/** Settings → Alerts: kind and date only, newest first, with "Put away" (TABLE UXS-46a); empty "No alerts.". */
export function AlertsContent({ view, idea, onDismiss, onRetry }: {
  view: Pick<ParentView, "alertsStatus" | "alerts" | "dismissing" | "dismissFailed">; idea: (kcId: string) => string | null;
  onDismiss: (alertId: string) => void; onRetry: () => void;
}) {
  const t = useT();
  if (view.alertsStatus === "unavailable") return <EmptyState message={t("alerts.unavailable")} />;
  if (view.alertsStatus === "failed") return <ErrorState message={t("alerts.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} />;
  if (view.alertsStatus !== "ready") return <div role="status" aria-busy="true"><span className="sr-only">{t("alerts.loading")}</span><Skeleton lines={3} /></div>;
  if (view.alerts.length === 0) return <p className="py-3 text-ui text-muted">{t("alerts.empty")}</p>;
  const label = (alert: AlertView) => {
    const name = alert.kc_id !== null ? idea(alert.kc_id) : null;
    return alert.kind === "mastery_drop" && name !== null ? t("alerts.kind.mastery_drop_named", { idea: name }) : t(`alerts.kind.${alert.kind}`);
  };
  return (
    <div className="flex flex-col gap-2 py-2">
      <ul className="flex flex-col divide-y divide-line">
        {view.alerts.map((alert) => (
          <li key={alert.alert_id} data-alert={alert.kind} data-severity={alert.severity} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 py-3">
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-ui font-medium text-ink">{label(alert)}</span>
              <span className="text-meta text-muted">{t(`alerts.severity.${alert.severity}`)}{" · "}{t("alerts.date", { date: new Date(alert.created_at) })}</span>
            </span>
            <Button size="sm" disabled={view.dismissing !== null} onClick={() => onDismiss(alert.alert_id)}>{t("alerts.dismiss")}</Button>
          </li>
        ))}
      </ul>
      {view.dismissFailed && <ErrorState message={t("alerts.dismiss_failed")} />}
    </div>
  );
}

export function Alerts({ profiles, idea = () => null }: { profiles: ProfileController; idea?: (kcId: string) => string | null }) {
  const controller = useMemo(() => new ParentController(profiles), [profiles]);
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const active = useProfiles(profiles).active;
  useEffect(() => { void controller.loadAlerts(); }, [controller, active]);
  return <AlertsContent view={view} idea={idea} onDismiss={(id) => void controller.dismiss(id)} onRetry={() => void controller.loadAlerts()} />;
}
