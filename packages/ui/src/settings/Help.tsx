// Settings → Help (TABLE UXS-46a, OPS-23, UXS-44, OBS-18), all of it on this computer: the offline help (the core's
// articles, in the student's language from the catalogue and searched here as the student types (LOC-01, D93), the
// known problems of this version with the article that says what to do, this computer's device id, app version and
// update channel, and the support contacts; `GET /v1/help`); the open profile's own problem reports
// (`/v1/problem-reports`), newest first, each removable; and "Save a report for support": a diagnostic bundle sealed to
// support, without learner content, written to a folder chosen on this computer (`POST /v1/exports`). Nothing is sent
// from here.
import { useEffect, useId, useState, type ReactNode } from "react";
import {
  ARTICLE_KEY_PREFIX, OfflineHelp, ProblemReports, issueArticle, searchHelp, type ChooseInstallSource, type HelpArticle, type OfflineHelpReply, type ProblemReport,
  type ProfileController,
} from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, Disclosure, EmptyState, ErrorState, FIELD_LOOK, Loader, Notice, Skeleton } from "../primitives/index.js";

/** An article's words from the catalogue: only keys under the help articles' prefix are shown. */
function useArticleWords(): (key: string) => string {
  const t = useT();
  return (key) => (key.startsWith(ARTICLE_KEY_PREFIX) ? t(`help.article.${key.slice(ARTICLE_KEY_PREFIX.length)}`) : "");
}

function Article({ article, open, onToggle }: { article: HelpArticle; open: boolean; onToggle: () => void }) {
  const id = useId();
  const words = useArticleWords();
  return (
    <li data-help-article={article.id} className="rounded-card border border-line">
      <button type="button" aria-expanded={open} aria-controls={id} onClick={onToggle}
        className="jana-press flex min-h-12 w-full items-center gap-3 rounded-card px-4 text-start text-ui font-medium text-ink hover:bg-wash">
        <span className="flex-1">{words(article.title_key)}</span>
        <span aria-hidden="true" className="text-faint"><Icon name={open ? "collapse" : "expand"} size={16} /></span>
      </button>
      {open && (
        <div id={id} className="flex flex-col gap-2 px-4 pb-4 text-ui text-ink">
          <p>{words(article.body_key)}</p>
          <ol className="flex list-decimal flex-col gap-1 ps-5 text-muted">
            {article.step_keys.map((step) => <li key={step}>{words(step)}</li>)}
          </ol>
        </div>
      )}
    </li>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-meta text-muted">{label}</dt>
      <dd className="text-ui text-ink tabular-nums">{children}</dd>
    </div>
  );
}

/** What the offline help shows: the core's reply, while it loads, when it could not be read, or `null` when this
 * computer's core does not serve help yet. */
export type HelpState = OfflineHelpReply | null | "loading" | "failed";

/** The offline help from its state (the gallery and the panel render this): known problems, search over the articles'
 * words in the student's language, the articles matching it, this computer and the support contacts. */
export function OfflineHelpView({ state, query, open, onQuery, onToggle, onOpenArticle, onRetry }: {
  state: HelpState; query: string; open: string | null;
  onQuery: (query: string) => void; onToggle: (articleId: string) => void; onOpenArticle: (articleId: string) => void; onRetry: () => void;
}) {
  const t = useT();
  const words = useArticleWords();
  const [computerOpen, setComputerOpen] = useState(false);
  if (state === "loading") return <Loader label={t("help.loading")} />;
  if (state === "failed") return <ErrorState message={t("help.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} />;
  if (state === null) return <Notice message={t("help.unavailable")} />;
  const shown = searchHelp(state.articles, query, words);
  const known = state.known_issues.map((issue) => {
    const title = t(issue.title_key);
    const article = issueArticle(issue, state.articles);
    return { issue, title: title !== issue.title_key ? title : t("help.known_issue_untitled"), article };
  });
  return (
    <div className="flex flex-col gap-5" data-offline-help="">
      {known.map(({ issue, title, article }) => (
        <div key={issue.issue_id} data-known-issue={issue.issue_id} className="flex flex-col items-start gap-2">
          <Notice tone="warn" message={t("help.known_issue", { title, article: article !== null ? words(article.title_key) : t("help.known_issue_no_article") })} />
          {article !== null && <Button size="sm" onClick={() => onOpenArticle(article.id)}>{t("help.open_article", { title: words(article.title_key) })}</Button>}
        </div>
      ))}
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
        {t("help.search")}
        <input type="search" className={FIELD_LOOK} autoComplete="off" value={query} onChange={(event) => onQuery(event.target.value)} />
      </label>
      {shown.length === 0 ? (
        <EmptyState message={t("help.no_results", { query: query.trim() })} />
      ) : (
        <ul className="flex flex-col gap-2" aria-label={t("help.articles")}>
          {shown.map((article) => <Article key={article.id} article={article} open={open === article.id} onToggle={() => onToggle(article.id)} />)}
        </ul>
      )}
      <Disclosure summary={t("help.this_computer")} open={computerOpen} onToggle={() => setComputerOpen(v => !v)}>
        <dl className="grid grid-cols-[repeat(auto-fit,minmax(10rem,1fr))] gap-4" data-help-computer="">
          <Fact label={t("help.app_version")}>{state.app_version}</Fact>
          <Fact label={t("help.channel")}>{t(`help.channel.${state.channel}`)}</Fact>
          <Fact label={t("help.device_id")}><span className="break-all">{state.device_id ?? t("help.device_id_none")}</span></Fact>
        </dl>
      </Disclosure>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-semibold text-ink">{t("help.contacts")}</h3>
        {state.contacts.length === 0 ? <p className="text-ui text-muted">{t("help.contacts_empty")}</p> : (
          <ul className="flex flex-col gap-1 text-ui text-ink" data-help-contacts="">
            {state.contacts.map((contact) => <li key={contact}>{contact}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}

/** The offline help for the open profile's core: read once, then searched on this computer as the student types. */
export function OfflineHelpPanel({ profiles }: { profiles: ProfileController }) {
  const [query, setQuery] = useState("");
  const [state, setState] = useState<HelpState>("loading");
  const [open, setOpen] = useState<string | null>(null);
  const load = () => {
    let live = true;
    new OfflineHelp(profiles.commands).read().then((found) => live && setState(found), () => live && setState("failed"));
    return () => {
      live = false;
    };
  };
  useEffect(load, [profiles]);
  return (
    <OfflineHelpView state={state} query={query} open={open}
      onQuery={setQuery}
      onToggle={(articleId) => setOpen((current) => (current === articleId ? null : articleId))}
      onOpenArticle={(articleId) => { setQuery(""); setOpen(articleId); }}
      onRetry={() => { setState("loading"); load(); }} />
  );
}

export function ProblemReportList({ profiles }: { profiles: ProfileController }) {
  const t = useT();
  const [reports, setReports] = useState<ProblemReport[] | "loading" | "failed">("loading");
  const store = new ProblemReports(profiles.commands);
  const load = () => {
    store.list().then(setReports, () => setReports("failed"));
  };
  useEffect(load, [profiles]);
  if (reports === "loading") return <Skeleton lines={2} />;
  if (reports === "failed") return <ErrorState message={t("help.reports_failed")} action={{ label: t("action.try_again"), onClick: load }} />;
  if (reports.length === 0) return <EmptyState message={t("help.reports_empty")} />;
  return (
    <ul className="flex flex-col gap-2">
      {reports.map((report) => (
        <li key={report.report_id} data-report-id={report.report_id} className="flex items-start gap-2 rounded-card border border-line p-3 text-ui text-ink">
          <span className="flex flex-1 flex-col gap-1">
            <span>{t(`report.category.${report.category}`)}</span>
            <span className="text-meta text-muted">{t("help.report_date", { date: new Date(report.created_at) })}</span>
            {report.note !== "" && <span className="text-meta text-muted">{report.note}</span>}
          </span>
          <Button onClick={() => {
            void store.remove(report.report_id).then(load, () => setReports("failed"));
          }}>{t("help.report_remove")}</Button>
        </li>
      ))}
    </ul>
  );
}

export function SupportReport({ profiles, chooseSource }: { profiles: ProfileController; chooseSource: ChooseInstallSource }) {
  const t = useT();
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const save = async () => {
    const folder = await chooseSource("folder").catch(() => null);
    if (folder === null) return;
    setState("saving");
    try {
      await new ProblemReports(profiles.commands).supportReport(profiles.scope().profileId, folder);
      setState("saved");
    } catch {
      setState("failed");
    }
  };
  return (
    <div className="flex flex-col gap-2">
      <div><Button disabled={state === "saving"} onClick={() => void save()}>{t("help.support_report")}</Button></div>
      {state === "saved" && <Notice message={t("help.support_saved")} />}
      {state === "failed" && <ErrorState message={t("help.support_failed")} />}
    </div>
  );
}
