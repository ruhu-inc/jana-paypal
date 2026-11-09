// Ask anything's answer on Home (STU-20, D117). The search comes first ("Looked in your notes", "Looked at 4 trusted
// sources"), then the answer, each supported statement with its citations and every other one with the
// general-knowledge label; nothing here is ever shown as checked. Teach me (the default, and the only mode under 18)
// ends with one check question and shows the explanation on request; a learner of 18 or over may switch to Just
// explain and the wider web. There is no model choice. Offline, or without the online plan, the message offers the
// tutor and the study area instead. Statements are model text and may hold maths: they go through the one renderer.
import type { ReactNode } from "react";
import type { AskMode, AskReach, AskStatement, AskView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { MarkdownBlock } from "../markdown/Markdown.js";
import { Button, Notice, SegmentedControl, Skeleton } from "../primitives/index.js";

export interface AskActions {
  /** Ask the tutor instead (offline, or any time). */
  onTutor: () => void;
  /** Open the study area. */
  onStudy: () => void;
  /** Show the explanation after the check question (Teach me). */
  onExplain: () => void;
  /** Ask the same question again in another mode or reach (18 or over only). */
  onAgain: (options: { mode: AskMode; reach: AskReach }) => void;
}

function Statements({ items, number }: { items: AskStatement[]; number: (ref: string) => number }) {
  const t = useT();
  return (
    <ul className="flex flex-col gap-3">
      {items.map((s, i) => (
        <li key={i} data-ask-statement={s.supported ? "supported" : "general"} className="flex flex-col gap-1">
          <div className="text-body text-ink"><MarkdownBlock text={s.text} /></div>
          {s.supported
            ? <span className="flex flex-wrap gap-1 text-meta text-faint">{s.refs.map((r) => <span key={r} data-cite={r}>{t("ask.cite", { number: number(r) })}</span>)}</span>
            : <span data-general-label="" className="text-meta text-warn">{t("ask.general_label")}</span>}
        </li>
      ))}
    </ul>
  );
}

export function AskAnswer({ view, actions, followUps }: { view: AskView; actions: AskActions; followUps?: ReactNode }) {
  const t = useT();
  if (view.status === "idle") return null;
  const elsewhere = (
    <div className="flex flex-wrap gap-2">
      <Button tone="primary" onClick={actions.onTutor}><Icon name="tutor" size={16} />{t("ask.offline_tutor")}</Button>
      <Button onClick={actions.onStudy}><Icon name="study-area" size={16} />{t("ask.offline_study")}</Button>
    </div>
  );
  if (view.status === "asking") {
    return (
      <section aria-labelledby="ask-answer" aria-busy="true" data-ask="asking" className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5">
        <h2 id="ask-answer" className="text-heading">{view.question}</h2>
        <p role="status" className="text-ui text-muted">{t("ask.searching")}</p>
        <Skeleton lines={3} />
      </section>
    );
  }
  if (view.status === "spoken") {
    // A question spoken in the voice conversation (VOC-53, D130): the tutor answered it by voice, with every check of
    // a tutor turn, and the board continues it; or it was material for the study area.
    const tutor = view.route === "tutor";
    return (
      <section aria-labelledby="ask-answer" data-ask="spoken" className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-5">
        <h2 id="ask-answer" className="text-heading">{view.question}</h2>
        {tutor && <p className="text-meta text-muted">{t("ask.tutor_said")}</p>}
        {view.spoken ? <p className="text-body text-ink">{view.spoken}</p> : null}
        <div className="flex flex-wrap gap-2">
          {tutor
            ? <Button tone="primary" onClick={actions.onTutor}><Icon name="tutor" size={16} />{t("ask.continue_tutor")}</Button>
            : <Button tone="primary" onClick={actions.onStudy}><Icon name="study-area" size={16} />{t("ask.offline_study")}</Button>}
        </div>
      </section>
    );
  }
  if (view.status !== "answered" || view.answer === null) {
    const message = view.status === "unavailable"
      ? t(view.reason === "allowance_exhausted" ? "ask.allowance_used" : view.reason === "notice_required" ? "ask.notice_required" : "ask.offline")
      : t(view.status === "blocked" ? "ask.blocked" : "ask.failed");
    return (
      <section aria-label={t("ask.title")} data-ask={view.status} className="flex flex-col gap-3">
        <Notice tone={view.status === "unavailable" ? "info" : "warn"} message={message} />
        {elsewhere}
      </section>
    );
  }
  const a = view.answer;
  const refs = a.sources.map((s) => s.ref);
  const number = (ref: string) => refs.indexOf(ref) + 1;
  const own = a.steps.find((s) => s.kind === "own")?.count ?? 0;
  const searched = a.steps.find((s) => s.kind !== "own");
  return (
    <section aria-labelledby="ask-answer" data-ask="answered" className="flex flex-col gap-5 rounded-card border border-line bg-canvas p-5">
      <h2 id="ask-answer" className="text-heading">{a.question}</h2>
      {/* The search, before the answer (STU-20). */}
      <ol aria-label={t("ask.search_label")} data-ask-steps="" className="flex flex-col gap-1 text-meta text-muted">
        <li className="flex items-center gap-2"><Icon name="notes" size={16} />{own > 0 ? t("ask.looked_own", { count: own }) : t("ask.looked_own_none")}</li>
        {searched !== undefined && <li className="flex items-center gap-2"><Icon name="search" size={16} />{t(searched.kind === "web" ? "ask.looked_web" : "ask.looked_trusted", { count: searched.count })}</li>}
      </ol>
      <Statements items={a.answer} number={number} />
      {a.own_excerpts.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-ui font-medium text-ink">{t("ask.from_your_material")}</h3>
          {a.own_excerpts.map((e) => <blockquote key={e.ref} data-cite={e.ref} className="border-s-2 border-line-strong ps-3 text-ui text-muted">{e.text} <span className="text-meta text-faint">{t("ask.cite", { number: number(e.ref) })}</span></blockquote>)}
        </div>
      )}
      {a.check_question !== null && (
        <div data-ask-check="" className="flex flex-col gap-3 rounded-card bg-panel p-4">
          <p className="text-ui font-medium text-ink">{t("ask.check_title")}</p>
          <div className="text-body text-ink"><MarkdownBlock text={a.check_question} /></div>
          {!view.explained && a.explanation.length > 0 && <span><Button size="sm" onClick={actions.onExplain}>{t("ask.show_explanation")}</Button></span>}
        </div>
      )}
      {(a.check_question === null || view.explained) && a.explanation.length > 0 && <Statements items={a.explanation} number={number} />}
      {a.sources.length > 0 && (
        <div className="flex flex-col gap-2">
          <h3 className="text-ui font-medium text-ink">{t("ask.sources")}</h3>
          <ol className="flex flex-col gap-1 text-meta text-muted">
            {a.sources.map((s, i) => (
              <li key={s.ref} data-source={s.ref}>
                {s.kind === "web" && s.url?.startsWith("https://")
                  ? <a href={s.url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-focus">{t("ask.source_line", { number: i + 1, title: s.title, where: s.domain ?? "" })}</a>
                  : t("ask.source_line", { number: i + 1, title: s.title, where: t(s.kind === "own_source" ? "ask.your_material" : "ask.your_notes") })}
              </li>
            ))}
          </ol>
        </div>
      )}
      {a.can_switch && (
        <div className="flex flex-wrap items-center gap-3">
          <SegmentedControl label={t("ask.mode_label")} value={a.mode} onChange={(mode) => actions.onAgain({ mode, reach: a.reach })}
            options={[{ value: "teach", label: t("ask.mode.teach") }, { value: "explain", label: t("ask.mode.explain") }]} />
          <SegmentedControl label={t("ask.reach_label")} value={a.reach} onChange={(reach) => actions.onAgain({ mode: a.mode, reach })}
            options={[{ value: "allow_list", label: t("ask.reach.allow_list") }, { value: "wider_web", label: t("ask.reach.wider_web") }]} />
        </div>
      )}
      {followUps}
    </section>
  );
}
