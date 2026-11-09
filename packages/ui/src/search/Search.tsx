// Worked-solution search (UXS-51, ASM-22, PED-09; direction A): where no tutor answers (the content-only class, or
// while the tutor cannot run), Home's Ask box becomes "Search worked solutions". Input is typed; the photo action
// says photos need the tutor. Results list each match with its topic, a one-line preview and the matching words
// marked; the core has already left out mock-reserved and protected items and recorded each preview as seen. Opening a
// match shows it as a question on the board, with its full worked solution on request (no attempt needed, D87): the
// steps one at a time with each step's check, or all at once. Empty: what to try next and "Browse topics".
import { useEffect, useId, useState, useSyncExternalStore } from "react";
import { markedRuns, type SearchController, type SearchMatch, type SearchState, type WorkedSolution } from "@jana/client-core";
import { BoardStep } from "../board/Board.js";
import { ScreenHeader } from "../frame/Frame.js";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { PackInline, PackText } from "../learn/PackText.js";
import { Tex } from "../maths/Tex.js";
import { Button, EmptyState, ErrorState, IconButton, Notice, Skeleton, SubjectDot } from "../primitives/index.js";

export interface SearchNames {
  /** The topic a KC belongs to, by its title. */
  topic: (kcId: string) => string | null;
  /** The subject (by its profile name) of an item's first KC. */
  subject: (kcId: string) => string | null;
}

export interface SearchActions {
  onSearch: (query: string) => void;
  onOpen: (itemId: string) => void;
  onClose: () => void;
  onSolution: () => void;
  onBrowse: () => void;
  /** Practice on the opened item's ideas. */
  onPractise?: (kcIds: string[], subject: string) => void;
}

/** The search box: Home's Ask box where no tutor answers, and the search screen's own field. */
export function SearchBox({ initial = "", onSearch, autoFocus = false }: { initial?: string; onSearch: (query: string) => void; autoFocus?: boolean }) {
  const t = useT();
  const id = useId();
  const [query, setQuery] = useState(initial);
  const [photo, setPhoto] = useState(false);
  useEffect(() => setQuery(initial), [initial]);
  return (
    <form role="search" aria-label={t("search.title")} data-search-box="" className="flex flex-col gap-2"
      onSubmit={(event) => { event.preventDefault(); if (query.trim() !== "") onSearch(query.trim()); }}>
      <div className="flex items-center gap-2 rounded-card border border-line-strong bg-canvas py-2 ps-3 pe-2 focus-within:border-focus">
        <Icon name="search" />
        <label htmlFor={id} className="sr-only">{t("search.label")}</label>
        <input id={id} type="search" value={query} autoFocus={autoFocus} placeholder={t("search.placeholder")} onChange={(event) => setQuery(event.target.value)}
          className="min-h-10 min-w-0 flex-1 bg-transparent text-body text-ink placeholder:text-placeholder" />
        <IconButton icon="photo" label={t("photo.title")} tip="above" onClick={() => setPhoto(true)} />
        <Button type="submit" tone="primary" disabled={query.trim() === ""}>{t("search.action")}</Button>
      </div>
      {photo && <p role="status" className="text-meta text-muted">{t("search.photo_needs_tutor")}</p>}
    </form>
  );
}

function MatchRow({ match, names, onOpen }: { match: SearchMatch; names: SearchNames; onOpen: () => void }) {
  const t = useT();
  const kc = match.item.kc_ids[0] ?? "";
  const subject = names.subject(kc) ?? "";
  return (
    <li data-search-match={match.item.item_id}>
      <button type="button" onClick={onOpen} className="jana-press flex min-h-11 w-full items-start gap-3 rounded-control px-3 py-3 text-start text-ink hover:bg-wash">
        <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-control bg-panel text-muted"><Icon name="learn" /></span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex items-center gap-1.5 text-meta text-faint"><SubjectDot subject={subject} />{names.topic(kc) ?? t("search.no_topic")}</span>
          <span className="text-ui text-ink" data-preview="">
            {/* A preview holding maths is typeset as the pack writes it; plain text has its matching words marked. */}
            {/\$|\\\(/.test(match.preview) ? <PackInline text={match.preview} />
              : markedRuns(match.preview, match.words).map((run, i) => run.matched ? <mark key={i} className="rounded-control bg-highlight-soft text-ink">{run.text}</mark> : <span key={i}>{run.text}</span>)}
          </span>
          {match.words.length > 0 && <span className="text-meta text-muted">{t("search.matched", { words: match.words.join(", ") })}</span>}
        </span>
        <span aria-hidden="true" className="self-center text-faint"><Icon name="expand" size={16} /></span>
      </button>
    </li>
  );
}

/** The worked solution, revealed a step at a time ("Next step") or all at once, each step with its check. */
export function SolutionSteps({ solution }: { solution: WorkedSolution }) {
  const t = useT();
  const [shown, setShown] = useState(1);
  const all = shown >= solution.steps.length;
  return (
    <div className="flex flex-col gap-4" data-solution={solution.worked_example_id}>
      <ol className="flex flex-col gap-1" aria-label={t("search.solution")}>
        {solution.steps.slice(0, shown).map((step, i) => (
          <BoardStep key={step.n} number={i + 1} current={i === shown - 1 && !all}
            step={{ step_id: `${solution.worked_example_id}-${step.n}`, index: i, latex: step.latex, reason: step.reason, owner: "tutor", status: "final", check_id: step.check_id, error: null }} />
        ))}
      </ol>
      {all ? (
        <p className="flex flex-wrap items-center gap-2 border-t border-line pt-4 text-ui text-ink" data-check-id={solution.final_answer.check_id}>
          <span className="font-semibold">{t("lesson.example.answer")}</span>
          <span className="inline-flex items-center gap-1.5 rounded-chip bg-verified-soft px-3 py-1 text-verified">
            <Icon name="verified" size={16} />
            <span className="board-math text-ink"><Tex tex={solution.final_answer.latex} /></span>
          </span>
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setShown((n) => n + 1)}>{t("search.next_step")}</Button>
          <Button tone="ghost" onClick={() => setShown(solution.steps.length)}>{t("search.show_all")}</Button>
        </div>
      )}
    </div>
  );
}

function Opened({ opened, names, actions }: { opened: NonNullable<SearchState["opened"]>; names: SearchNames; actions: SearchActions }) {
  const t = useT();
  const { item } = opened;
  const kc = item.kc_ids[0] ?? "";
  const subject = names.subject(kc);
  return (
    <article data-search-opened={item.item_id} className="flex flex-col gap-5">
      <div><Button tone="ghost" onClick={actions.onClose}><Icon name="go-back" size={16} />{t("search.back")}</Button></div>
      <section className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-6">
        <p className="flex items-center gap-1.5 text-meta text-faint"><SubjectDot subject={subject ?? ""} />{names.topic(kc) ?? t("search.no_topic")}</p>
        <PackText text={item.stem} />
        {item.parts.length > 1 && (
          <ol className="flex list-[lower-alpha] flex-col gap-2 ps-6 text-body text-ink">
            {item.parts.map((part) => <li key={part.part_id}><PackText text={part.prompt} /></li>)}
          </ol>
        )}
        {opened.solutionStatus === "ready" && opened.solution !== null ? <SolutionSteps solution={opened.solution} /> : (
          <div className="flex flex-wrap gap-2">
            <Button tone="primary" disabled={opened.solutionStatus === "loading"} onClick={actions.onSolution}>{t("search.show_solution")}</Button>
            {actions.onPractise !== undefined && subject !== null && <Button onClick={() => actions.onPractise?.(item.kc_ids, subject)}>{t("search.practise")}</Button>}
          </div>
        )}
        {opened.solutionStatus === "failed" && <Notice tone="warn" message={t("search.solution_failed")} />}
      </section>
    </article>
  );
}

export function SearchContent({ state, names, actions }: { state: SearchState; names: SearchNames; actions: SearchActions }) {
  const t = useT();
  const body = () => {
    if (state.opened !== null) return <Opened opened={state.opened} names={names} actions={actions} />;
    if (state.status === "unavailable") return <EmptyState message={t("search.unavailable")} />;
    if (state.status === "failed") return <ErrorState message={t("search.failed")} action={{ label: t("action.try_again"), onClick: () => actions.onSearch(state.query) }} />;
    if (state.status === "searching") return <div aria-busy="true"><Skeleton lines={3} /></div>;
    if (state.status === "idle") return <p className="text-ui text-muted">{t("search.intro")}</p>;
    if (state.matches.length === 0) {
      return (
        <section data-search-empty="" className="flex flex-col items-start gap-3 rounded-card border border-line bg-canvas p-6">
          <p className="text-body text-ink">{t("search.empty")}</p>
          <Button onClick={actions.onBrowse}>{t("home.browse_topics")}</Button>
        </section>
      );
    }
    return (
      <section aria-labelledby="search-results" className="flex flex-col gap-2 rounded-card border border-line bg-canvas px-3 pt-5 pb-3">
        <h2 id="search-results" className="px-2 text-heading text-ink">{t("search.results", { count: state.matches.length })}</h2>
        <ul className="flex flex-col divide-y divide-line">
          {state.matches.map((match) => <MatchRow key={match.item.item_id} match={match} names={names} onOpen={() => actions.onOpen(match.item.item_id)} />)}
        </ul>
      </section>
    );
  };
  return (
    <div className="flex min-h-full flex-col" data-search={state.status}>
      <ScreenHeader><h1 className="text-ui font-semibold text-ink">{t("search.title")}</h1></ScreenHeader>
      <div className="flex-1 px-6 pt-6 pb-12">
        <div className="mx-auto flex w-full max-w-(--layout-composerMax) flex-col gap-6">
          {state.opened === null && <SearchBox initial={state.query} onSearch={actions.onSearch} />}
          {body()}
        </div>
      </div>
    </div>
  );
}

export function SearchScreen({ controller, names, actions }: { controller: SearchController; names: SearchNames; actions: Pick<SearchActions, "onBrowse" | "onPractise"> }) {
  const state = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  return (
    <SearchContent state={state} names={names} actions={{
      ...actions,
      onSearch: (query) => void controller.search(query),
      onOpen: (itemId) => controller.open(itemId),
      onClose: () => controller.close(),
      onSolution: () => void controller.showSolution(),
    }} />
  );
}
