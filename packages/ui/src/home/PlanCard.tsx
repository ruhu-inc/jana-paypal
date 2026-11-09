// Home's plan card (UXS-12, LRN-28 to LRN-33; direction A): "Next up", the activities the planner proposed with an
// approximate duration each (estimates, never a timer or a goal), the next one first and the rest optional. A proposed
// plan has Start, Change and Not today (the card's `start`, `change` and `skipped`, LRN-31); Change offers the ranked
// alternatives for the main activity and a shorter plan, for this proposal only. A started plan ticks what is done and
// offers to continue at the next activity (stopping kept the rest, LRN-31). A finished plan offers Keep learning: a
// fresh proposal, started at once (the `start` action of the new card), at any hour and as often as wanted (D54). Not
// today asks nothing further and leaves lessons, practice and the tutor open (LRN-32). The streak with its freezes sits
// at the foot, said neutrally (LRN-33, LRN-35). Inside Home's Continue learning block (`useContinueBlock`) the card is
// a row of the block: no chrome of its own, "Next up" a quiet label under the block's heading, the hero's Start the
// block's one primary button. Standalone (the gallery) it is the card it was.
import { useState, type ReactNode } from "react";
import { nextBlock, type PlanBlock, type PlanChoice, type PlanState } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, ErrorState, Skeleton } from "../primitives/index.js";
import { useContinueBlock } from "./Home.js";

/** The words for a block's activity: its title and what it is (the core's ids become names here). */
export interface PlanNames {
  /** The topic a KC belongs to, by its title, or null when it is not known yet. */
  topic: (kcId: string) => string | null;
  /** Whether the student has started on the KC (a not-started main block is a lesson then practice). */
  started: (kcId: string) => boolean;
}

export interface PlanActions {
  onStart: () => void;
  onChange: (choice: PlanChoice) => void;
  onNotToday: () => void;
  /** Continue a started plan at its next activity. */
  onContinue: (block: PlanBlock) => void;
  /** A fresh proposal after a finished plan, started at once ("Keep learning"), or after Not today. */
  onKeepLearning: () => void;
  onAnother: () => void;
  onRetry: () => void;
  /** Open the deck of a warm-up's due Study cards (SPEC §19.4, `open_study_deck`). */
  onStudyCards?: (deckId: string) => void;
}

export function useBlockText(names: PlanNames): (block: Pick<PlanBlock, "kind" | "kc_ids" | "reason_code"> & Partial<Pick<PlanBlock, "study_cards">>) => { title: string; detail: string | null } {
  const t = useT();
  return (block) => {
    const first = block.kc_ids[0];
    const topic = first === undefined ? null : names.topic(first);
    switch (block.kind) {
      case "warmup":
        // Due cards from the learner's own materials (SPEC §19.4): counted from the plan, opened in Study.
        if (block.study_cards !== undefined && block.study_cards.length > 0) return { title: t("plan.block.study_cards", { count: block.study_cards.length }), detail: null };
        return { title: t("plan.block.warmup", { count: block.kc_ids.length }), detail: null };
      case "main": {
        if (block.reason_code === "mock_follow_up") {
          const topics = [...new Set(block.kc_ids.map((kc) => names.topic(kc)).filter((name): name is string => name !== null))];
          // Several topics joined into one title read as one long name; the title counts them and the names follow.
          if (topics.length > 1) return { title: t("plan.block.mock_topics", { count: topics.length }), detail: topics.join(" · ") };
          return { title: topics[0] ?? t("plan.block.main_fallback"), detail: t("plan.block.mock_follow_up") };
        }
        return { title: topic ?? t("plan.block.main_fallback"), detail: first !== undefined && names.started(first) ? t("plan.block.practice") : t("plan.block.lesson_then_practice") };
      }
      case "mastery_check":
        return { title: topic === null ? t("plan.block.check_fallback") : t("plan.block.check", { topic }), detail: null };
      default:
        return { title: t(`plan.block.${block.kind}`), detail: null };
    }
  };
}

function BlockRow({ block, text, next, status, action }: {
  block: PlanBlock; text: { title: string; detail: string | null }; next: boolean; status: string; action?: ReactNode;
}) {
  const t = useT();
  const done = block.status === "done";
  return (
    <li data-plan-block={block.kind} data-block-status={block.status} data-next={next ? "" : undefined}
      className={`flex flex-wrap items-center gap-x-4 gap-y-2 rounded-control px-3 py-3 ${next ? "bg-panel" : ""}`}>
      <span aria-hidden="true" className={`flex size-7 shrink-0 items-center justify-center rounded-pill text-meta font-medium tabular-nums ${done ? "bg-verified-soft text-verified" : "bg-wash text-muted"}`}>
        {done ? <Icon name="verified" size={16} /> : block.index + 1}
      </span>
      <span className="flex min-w-40 flex-1 flex-col gap-0.5">
        <span className={`text-ui ${next ? "font-medium text-ink" : "text-ink"}`}>{text.title}</span>
        <span className="text-meta text-faint">
          {[text.detail, t("plan.minutes", { minutes: block.minutes }), done ? t("plan.done") : !next && status === "proposed" ? t("plan.optional") : null].filter(Boolean).join(" · ")}
        </span>
      </span>
      {action}
    </li>
  );
}

/** The next activity as the card's one primary action (H1, D109): its name large, why it is next, and Start. */
function HeroRow({ block, text, action, secondary }: { block: PlanBlock; text: { title: string; detail: string | null }; action: ReactNode; secondary?: ReactNode }) {
  const t = useT();
  const reason = block.reason_code && block.reason_code !== "mock_follow_up" ? t(`plan.reason.${block.reason_code}`) : null;
  return (
    <li data-plan-block={block.kind} data-block-status={block.status} data-next="" className="flex flex-col gap-4 px-2 pt-1 pb-3">
      <span className="flex flex-col gap-1.5">
        <span className="text-title text-ink">{text.title}</span>
        {reason !== null && <span className="max-w-[56ch] text-ui text-muted">{reason}</span>}
        <span className="text-meta text-faint">{[text.detail, t("plan.minutes", { minutes: block.minutes })].filter(Boolean).join(" · ")}</span>
      </span>
      <span className="flex flex-wrap items-center gap-2">{action}{secondary}</span>
    </li>
  );
}

export function PlanCard({ state, names, streak, actions }: { state: PlanState; names: PlanNames; streak: { days: number; freezes: number } | null; actions: PlanActions }) {
  const t = useT();
  const text = useBlockText(names);
  const embedded = useContinueBlock();
  const [changing, setChanging] = useState(false);
  const plan = state.plan;
  if (state.status === "unavailable" || state.status === "idle") return null;
  const frame = (children: ReactNode, status: string) => {
    const change = plan?.status === "proposed" && !changing && <Button size="sm" tone="ghost" disabled={state.busy} aria-expanded={changing} onClick={() => setChanging(true)}>{t("plan.change")}</Button>;
    const foot = <>
      {state.actionFailed && <div className="px-2"><ErrorState message={t("plan.action_failed")} /></div>}
      {streak !== null && streak.days > 0 && (
        <p data-plan-streak={streak.days} className="border-t border-line px-2 pt-3 text-meta text-muted">
          {streak.days === 1 ? t("progress.streak.new") : t("plan.streak", { days: streak.days })} · {t("progress.streak.freezes", { count: streak.freezes })}
        </p>
      )}
    </>;
    if (embedded) {
      return (
        <section aria-labelledby="home-plan" data-plan={status} aria-busy={state.busy} className="flex flex-col gap-3 border-t border-line pt-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2 px-2">
            <h3 id="home-plan" className="text-label font-medium text-faint">{t("plan.title")} <span className="font-regular">{t("plan.estimates")}</span></h3>
            {change}
          </div>
          {children}
          {foot}
        </section>
      );
    }
    return (
      <section aria-labelledby="home-plan" data-plan={status} aria-busy={state.busy} className="flex flex-col gap-4 rounded-card border border-line bg-canvas px-3 pt-5 pb-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2 px-2">
          <h2 id="home-plan" className="text-heading text-ink">{t("plan.title")} <span className="text-meta font-regular text-faint">{t("plan.estimates")}</span></h2>
          {change}
        </div>
        {children}
        {foot}
      </section>
    );
  };
  if (state.status === "failed") return frame(<div className="px-2"><ErrorState message={t("plan.failed")} action={{ label: t("action.try_again"), onClick: actions.onRetry }} /></div>, "failed");
  // Shaped like the hero it stands in for (title, two lines, Start), so the Ask box below does not jump when it lands.
  if (plan === null) return frame(<div className="flex flex-col items-start gap-3 px-2"><div className="w-full"><Skeleton lines={3} /></div><span aria-hidden="true" className="block h-11 w-24 rounded-control bg-panel" /></div>, "loading");
  if (plan.status === "done") {
    return frame(
      <div className="flex flex-col items-start gap-3 px-2">
        <p className="text-ui text-ink">{t("plan.finished")}</p>
        <Button tone="primary" disabled={state.busy} onClick={actions.onKeepLearning}><Icon name="continue" size={16} />{t("plan.keep_learning")}</Button>
      </div>, "done");
  }
  if (plan.status === "not_today") {
    return frame(
      <div className="flex flex-col items-start gap-3 px-2">
        <p className="text-ui text-ink">{t("plan.not_today_note")}</p>
        <Button disabled={state.busy} onClick={actions.onAnother}>{t("plan.another")}</Button>
      </div>, "not_today");
  }
  const next = nextBlock(plan);
  if (plan.blocks.length === 0 || (next === null && plan.status === "proposed")) {
    return frame(<p className="px-2 text-ui text-muted">{t("plan.nothing")}</p>, "empty");
  }
  if (changing && plan.status === "proposed") {
    return frame(
      <div className="flex flex-col gap-2 px-2" data-plan-change="">
        <p className="text-ui text-muted">{t("plan.change_intro")}</p>
        <ul className="flex flex-col gap-1">
          {plan.alternatives.map((alt, index) => (
            <li key={alt.kc_ids.join(",")}>
              <button type="button" disabled={state.busy} data-alternative={index} onClick={() => { setChanging(false); actions.onChange({ alternative: index }); }}
                className="jana-press flex min-h-11 w-full items-center justify-between gap-3 rounded-control px-3 py-2 text-start text-ink hover:bg-wash disabled:opacity-60">
                <span className="text-ui">{text(alt).title}</span>
                <span className="shrink-0 text-meta text-faint">{t("plan.minutes", { minutes: alt.minutes })}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-2 pt-1">
          <Button disabled={state.busy} onClick={() => { setChanging(false); actions.onChange("shorten"); }}>{t("plan.shorten")}</Button>
          <Button tone="ghost" onClick={() => setChanging(false)}>{t("plan.keep_as_is")}</Button>
        </div>
      </div>, "changing");
  }
  const keepLearning = plan.keep_learning && plan.status === "proposed";
  // The next activity leads, large, with Start; the rest follow quietly under "Then" (H1, D109).
  const earlier = next === null ? plan.blocks : plan.blocks.filter((block) => block.index < next.index);
  const later = next === null ? [] : plan.blocks.filter((block) => block.index > next.index);
  // A warm-up of due Study cards opens its deck from its own row too (SPEC §19.4).
  const cards = (block: PlanBlock) => {
    const deck = block.study_cards?.[0]?.deck_id;
    return deck === undefined || actions.onStudyCards === undefined ? undefined
      : <Button size="sm" tone="ghost" data-study-cards={deck} onClick={() => actions.onStudyCards?.(deck)}>{t("plan.open_study_cards")}</Button>;
  };
  const notToday = plan.status === "proposed" ? <Button tone="ghost" disabled={state.busy} onClick={actions.onNotToday}>{t("plan.not_today")}</Button> : undefined;
  return frame(
    <ol className="flex flex-col gap-1">
      {earlier.map((block) => <BlockRow key={block.index} block={block} text={text(block)} next={false} status={plan.status} action={cards(block)} />)}
      {next !== null && <HeroRow block={next} text={text(next)} secondary={notToday} action={plan.status === "proposed"
        ? <Button tone="primary" disabled={state.busy} onClick={actions.onStart}>{t(keepLearning ? "plan.keep_learning" : "plan.start")}<Icon name="go-next" size={16} /></Button>
        : <Button tone="primary" onClick={() => actions.onContinue(next)}><Icon name="continue" size={16} />{t("plan.continue")}</Button>} />}
      {later.length > 0 && <li aria-hidden="true" className="border-t border-line px-2 pt-3 text-label font-medium text-faint">{t("plan.then")}</li>}
      {later.map((block) => <BlockRow key={block.index} block={block} text={text(block)} next={false} status={plan.status} action={cards(block)} />)}
    </ol>, plan.status);
}
