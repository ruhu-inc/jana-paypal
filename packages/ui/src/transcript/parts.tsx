// The part renderers and the default registry (UIA-09 to UIA-12). Visual renderers are registered by the app from
// the renderers package (VSL-18); here the visual entry falls back to words.
import { memo, useEffect, useState, type ReactNode } from "react";
import { CLIENT_KEYS, memoryReadCount, type PartRec, type ToolRec } from "@jana/client-core";
import type { Card } from "@jana/contracts-ts";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Tex } from "../maths/Tex.js";
import { useOpenKnows } from "../memory/link.js";
import { StreamingMarkdown } from "../markdown/Markdown.js";
import { Button, ErrorState, Loader, Notice, Spinner, chipLook } from "../primitives/index.js";
import { Registry, type Lifecycle, type RenderProps } from "../registry/registry.js";
import { familyOf, objectOf, phaseOf, type ToolFamily } from "./tools.js";
import { useSelect } from "../store.js";
import { lessonOfCommand, useOpenGap, useOpenLesson, topicOfCommand, useOpenTopic } from "../learn/link.js";

/** The tool families whose finished line is a checked value: the step, units, equation and ledger checks. */
const CHECKED_FAMILIES = new Set<ToolFamily>(["step_check", "units", "chem_balance", "ledger"]);

export function lifecycleOf(part: PartRec): Lifecycle {
  switch (part.status) {
    case "started":
    case "streaming":
      return part.kind === "tool" ? "executing" : "in_progress";
    case "complete":
      return "complete";
    case "error":
      return "error";
    case "interrupted":
      return "cancelled";
    case "retracted":
      return "retracted";
  }
}

/** True once `active` has held for the label delay: fast work never strobes a label (UIA-11). */
export function useDelayed(active: boolean, ms: number = CLIENT_KEYS.ui_running_label_after_ms): boolean {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!active) {
      setShown(false);
      return;
    }
    const timer = setTimeout(() => setShown(true), ms);
    return () => clearTimeout(timer);
  }, [active, ms]);
  return active && shown;
}

const TextPart = memo(function TextPart({ data }: RenderProps<PartRec>) {
  return <StreamingMarkdown settled={data.settled_blocks} tail={data.tail} />;
});

function TextFallback({ data }: { data: unknown }) {
  const text = (data as { text?: string } | null)?.text ?? "";
  return <p className="my-2 whitespace-pre-wrap">{text}</p>;
}

/** The memory-use line (MEM-28): shown only when the engine's own result of a memory read says it returned records,
 * with a way to the page that lists them. The tutor's words can neither add nor remove it. */
export function MemoryUseLine({ tool }: { tool: ToolRec }) {
  const t = useT();
  const open = useOpenKnows();
  if (memoryReadCount(tool) === 0) return null; // a read that found nothing shows no line
  return (
    <div className="flex items-center gap-2 py-0.5 text-ui text-muted" data-tool={tool.name} data-phase="done" data-memory-use="true">
      <Icon name="notes" size={16} />
      <span>{t("memory.used")}</span>
      {open !== null && (
        <button type="button" onClick={open} className="jana-press min-h-9 rounded-control font-medium text-ink underline">
          {t("knows.open")}
        </button>
      )}
    </div>
  );
}

export function ToolLine({ tool }: { tool: ToolRec }) {
  const t = useT();
  const phase = phaseOf(tool.state);
  const family = familyOf(tool.name);
  const visible = useDelayed(phase === "running");
  if (phase === "running" && !visible) return null;
  if (family === "recall" && phase === "done") return <MemoryUseLine tool={tool} />;
  const obj = objectOf(tool.state);
  if (phase === "done" && CHECKED_FAMILIES.has(family)) {
    // A value a tool checked is a small "Checked" chip (UXS-14), the verified colour with its tick and words (DSN-04).
    return (
      <div className="py-0.5" data-tool={tool.name} data-phase={phase}>
        <span className={chipLook("verified")}>
          <Icon name="verified" size={16} />
          <span>
            {t(`tool.${family}.${phase}`)}
            {obj !== null && <> <strong className="font-semibold">{obj}</strong></>}
          </span>
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2 py-0.5 text-ui text-muted" data-tool={tool.name} data-phase={phase}>
      {phase === "running" ? (
        <Spinner />
      ) : phase === "done" ? (
        <Icon name="verified" size={16} />
      ) : (
        <Icon name="warning" size={16} />
      )}
      <span className={phase === "running" ? "jana-shimmer" : undefined}>
        {t(`tool.${family}.${phase}`)}
        {obj !== null && <> <strong className="font-semibold text-ink">{obj}</strong></>}
      </span>
    </div>
  );
}

function ToolPart({ data }: RenderProps<PartRec>) {
  const tools = Object.values(data.tools ?? {});
  return <div>{tools.map((tool) => <ToolLine key={tool.call_id} tool={tool} />)}</div>;
}

function ToolFallback({ data }: { data: unknown }) {
  const t = useT();
  const tools = Object.values((data as PartRec | null)?.tools ?? {});
  const memoryRead = (tool: ToolRec) => familyOf(tool.name) === "recall" && phaseOf(tool.state) === "done";
  const shown = tools.filter((tool) => !memoryRead(tool) || memoryReadCount(tool) > 0); // MEM-28, as in ToolLine
  return <div className="text-ui text-muted">{shown.map((tool) => <div key={tool.call_id}>{memoryRead(tool) ? t("memory.used") : t(`tool.${familyOf(tool.name)}.${phaseOf(tool.state)}`)}</div>)}</div>;
}

type CardAnswer = { option_ids?: string[]; text?: string; action?: string; skipped?: boolean };
type OnAnswer = ((answer: CardAnswer) => void) | undefined;

type CardOption = { id: string; label: string; latex?: string; recommended?: boolean };

/** The choices as buttons; with `chosen` they are toggles, each picked one ticked (never a tint alone, DSN-04). */
function OptionButtons({ options, interactive, chosen, onPick }: { options: CardOption[]; interactive: boolean; chosen?: string[]; onPick: (id: string) => void }) {
  const t = useT();
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {options.map((o) => (
        <Button key={o.id} disabled={!interactive} aria-pressed={chosen === undefined ? undefined : chosen.includes(o.id)} onClick={() => onPick(o.id)}>
          {chosen?.includes(o.id) === true && <Icon name="verified" size={16} />}
          {o.latex !== undefined ? <Tex tex={o.latex} /> : o.label}
          {o.recommended === true && <span className="text-label text-muted">{t("card.recommended")}</span>}
        </Button>
      ))}
    </div>
  );
}

function Options({ options, multi, interactive, onAnswer }: { options: CardOption[]; multi: boolean; interactive: boolean; onAnswer: OnAnswer }) {
  const t = useT();
  const [chosen, setChosen] = useState<string[]>([]);
  if (options.length === 0) return null;
  if (!multi) return <OptionButtons options={options} interactive={interactive} onPick={(id) => onAnswer?.({ option_ids: [id] })} />;
  return (
    <>
      <OptionButtons options={options} interactive={interactive} chosen={chosen} onPick={(id) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))} />
      <div className="mt-3 flex"><Button tone="primary" disabled={!interactive || chosen.length === 0} onClick={() => onAnswer?.({ option_ids: chosen })}>{t("card.submit")}</Button></div>
    </>
  );
}

/** Several choices and the student's own words together: one answer, one Send (the interview's goals question). */
function OptionsWithText({ options, interactive, onAnswer }: { options: CardOption[]; interactive: boolean; onAnswer: OnAnswer }) {
  const t = useT();
  const [chosen, setChosen] = useState<string[]>([]);
  const [text, setText] = useState("");
  const own = text.trim();
  return (
    <form className="flex flex-col" onSubmit={(e) => { e.preventDefault(); if (chosen.length > 0 || own !== "") onAnswer?.({ ...(chosen.length > 0 ? { option_ids: chosen } : {}), ...(own !== "" ? { text: own } : {}) }); }}>
      <OptionButtons options={options} interactive={interactive} chosen={chosen} onPick={(id) => setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))} />
      <input aria-label={t("card.answer_label")} placeholder={t("card.something_else")} disabled={!interactive} value={text} onChange={(e) => setText(e.target.value)} className="mt-2 min-h-11 rounded-control border border-line-strong bg-canvas px-3 text-ui" />
      <div className="mt-3 flex"><Button tone="primary" type="submit" disabled={!interactive || (chosen.length === 0 && own === "")}>{t("card.submit")}</Button></div>
    </form>
  );
}

function FreeText({ interactive, onAnswer }: { interactive: boolean; onAnswer: OnAnswer }) {
  const t = useT();
  const [text, setText] = useState("");
  return (
    <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (text.trim() !== "") onAnswer?.({ text: text.trim() }); }}>
      <input aria-label={t("card.answer_label")} placeholder={t("card.something_else")} disabled={!interactive} value={text} onChange={(e) => setText(e.target.value)} className="flex-1 rounded-control border border-line-strong bg-canvas px-2 text-ui" />
      <Button tone="primary" type="submit" disabled={!interactive || text.trim() === ""}>{t("card.submit")}</Button>
    </form>
  );
}

function CardFrame({ label, icon, tone, children }: { label: string; icon: "question" | "hint" | "info" | "verified"; tone: "hint" | "plain"; children: ReactNode }) {
  return (
    // A hint is a notice in the learning colour, filled and never edged; other cards sit on `canvas` with a `line` edge
    // (DSN-08: nothing gets a coloured border).
    <section aria-label={label} data-card-tone={tone} className={`my-1 max-w-150 rounded-card p-4 ${tone === "hint" ? "bg-hint-soft" : "border border-line bg-canvas"}`}>
      <p className={`mb-1 flex items-center gap-2 text-label font-medium ${tone === "hint" ? "text-hint" : "text-muted"}`}>
        <Icon name={icon} size={16} />
        {label}
      </p>
      {children}
    </section>
  );
}

/** One card of each `card_type` (UXS-36): icon and word with every state (DSN-04); never folded (UIA-10). */
export function CardBody({ card, interactive, onAnswer }: { card: Card; interactive: boolean; onAnswer?: (answer: CardAnswer) => void }) {
  const t = useT();
  switch (card.card_type) {
    case "question":
      return (
        <CardFrame label={t("card.question")} icon="question" tone="plain">
          <StreamingMarkdown settled={[card.prompt]} tail="" />
          {card.multi && card.allow_free_text && card.options.length > 0 ? <OptionsWithText options={card.options} interactive={interactive} onAnswer={onAnswer} /> : <>
            <Options options={card.options} multi={card.multi} interactive={interactive} onAnswer={onAnswer} />
            {card.allow_free_text && <FreeText interactive={interactive} onAnswer={onAnswer} />}
          </>}
        </CardFrame>
      );
    case "hint":
      return (
        <CardFrame label={t("card.hint_rung", { rung: card.rung, of: card.of })} icon="hint" tone="hint">
          <StreamingMarkdown settled={[card.body]} tail="" />
          {card.actions.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2">
              {card.actions.map((a) => <Button key={a} disabled={!interactive} onClick={() => onAnswer?.({ action: a })}>{t(`card.action.${a}`)}</Button>)}
            </div>
          )}
        </CardFrame>
      );
    case "plan":
      return (
        <CardFrame label={t("card.plan")} icon="info" tone="plain">
          <p className="text-ui font-semibold">{card.title}</p>
          <ol className="mt-1 list-decimal ps-6 text-ui">
            {card.items.map((item) => (
              <li key={item.id} data-status={item.status}>
                {item.text} <span className="inline-flex items-center gap-1 text-meta text-muted">{item.status === "done" && <Icon name="verified" size={16} />}{t(`card.plan_status.${item.status}`)}</span>
              </li>
            ))}
          </ol>
          {card.actions.length > 0 && (
            <div className="mt-2 flex gap-2">
              {card.actions.map((a) => <Button key={a} disabled={!interactive} onClick={() => onAnswer?.({ action: a })}>{t(`card.action.${a}`)}</Button>)}
            </div>
          )}
        </CardFrame>
      );
    case "reflection":
      return (
        <CardFrame label={t("card.reflection")} icon="verified" tone="plain">
          <dl className="text-ui">
            <dt className="text-label font-medium text-muted">{t("card.reflection.did")}</dt>
            <dd className="mb-1">{card.did}</dd>
            <dt className="text-label font-medium text-muted">{t("card.reflection.right")}</dt>
            <dd className="mb-1">{card.right}</dd>
            <dt className="text-label font-medium text-muted">{t("card.reflection.next_goal")}</dt>
            <dd>{card.next_goal}</dd>
          </dl>
          {card.marks.length > 0 && (
            <ul className="mt-2 text-ui">
              {card.marks.map((m, i) => (
                <li key={i} className="flex items-center gap-2">
                  {m.awarded ? <Icon name="verified" size={16} /> : <Icon name="look-here" size={16} />}
                  <span className="text-meta text-muted">{t(`card.mark_type.${m.type}`)}</span>
                  <span className="flex-1">{m.criterion}</span>
                  <span className="text-meta text-muted">{t(m.awarded ? "card.mark.awarded" : "card.mark.not_awarded")}</span>
                </li>
              ))}
            </ul>
          )}
        </CardFrame>
      );
    case "confirm":
      return (
        <CardFrame label={t("card.confirm")} icon="info" tone="plain">
          <p className="text-ui font-semibold">{card.title}</p>
          <p className="text-ui">{card.body}</p>
          <div className="mt-2 flex gap-2">
            <Button disabled={!interactive} onClick={() => onAnswer?.({ action: "cancel" })}>{t(card.cancel_label_key in CONFIRM_KEYS ? card.cancel_label_key : "card.confirm_no")}</Button>
            <Button tone="primary" disabled={!interactive} onClick={() => onAnswer?.({ action: "confirm" })}>{t(card.confirm_label_key in CONFIRM_KEYS ? card.confirm_label_key : "card.confirm_yes")}</Button>
          </div>
        </CardFrame>
      );
  }
}

/** The answer labels a confirm card may name: yes/no, the first-use egress notice's two (ARC-10, UXS-07; cloud voice
 * keeps voice rather than the question local, VOC-28) and the new question in another subject's two (AGT-15, D94). */
const CONFIRM_KEYS: Record<string, true> = {
  "card.confirm_yes": true, "card.confirm_no": true, "egress.first_use.ok": true, "egress.first_use.keep_local": true,
  "egress.first_use.keep_voice_local": true,
  "card.new_subject.start": true, "card.new_subject.stay": true,
};

export type CardState = "open" | "answering" | "answered" | "cancelled" | "failed" | "expired";

/** A card with its request state (UXS-02 states for cards): sending shows the loader, then an honest clock; a failed
 * send keeps the card with "Try again"; a card no longer needed says so. */
export function CardView({ card, state, elapsedMs = 0, onAnswer, onRetry }: { card: Card; state: CardState; elapsedMs?: number; onAnswer?: (answer: CardAnswer) => void; onRetry?: () => void }) {
  const t = useT();
  return (
    <div data-card-state={state}>
      <CardBody card={card} interactive={state === "open"} {...(onAnswer !== undefined ? { onAnswer } : {})} />
      {state === "answering" && (
        elapsedMs > CLIENT_KEYS.ui_still_working_ms ? <p role="status" className="text-meta text-muted">{t("tutor.status.still_working")}</p>
        : elapsedMs > CLIENT_KEYS.ui_first_token_wait_label_ms ? <p role="status" className="text-meta text-muted">{t("tutor.status.waiting_first_token_long", { seconds: Math.floor(elapsedMs / 1000) })}</p>
        : <Loader label={t("composer.sending")} />
      )}
      {state === "failed" && <ErrorState message={t("error.core_starting")} {...(onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: onRetry } } : {})} />}
      {state === "answered" && <p className="text-meta text-muted">{t("card.answered")}</p>}
      {(state === "cancelled" || state === "expired") && <p className="text-meta text-muted">{t("card.cancelled")}</p>}
    </div>
  );
}

function CardPart({ data, onAction }: RenderProps<PartRec>) {
  // The request's own state, which the hint dock answers too (UXS-17): the card and the dock wait together.
  const requestState = useSelect((s) => (data.card === undefined ? undefined : s.requests[data.card.request_id]?.state));
  if (data.card === undefined) return null;
  const requestId = data.card.request_id;
  const state = requestState === "answering" && data.card.state === "open" ? "answering" : data.card.state;
  return (
    <>
      <CardView card={data.card.card} state={state} onAnswer={(answer) => onAction?.("answer", { request_id: requestId, answer })} />
      {data.card.about?.kind === "root_gap" && <GapLink about={data.card.about} />}
    </>
  );
}

/** "See it on the map" under a root-gap card: the concept map of the KC's subject with the gap path lit. */
function GapLink({ about }: { about: { kc_id: string; root_kc_id: string; misconception_id: string } }) {
  const t = useT();
  const open = useOpenGap();
  if (open === null) return null;
  return (
    <span className="mt-2 flex" data-gap-link={about.misconception_id}>
      <Button size="sm" tone="ghost" onClick={() => open({ kc_id: about.kc_id, root_kc_id: about.root_kc_id, misconception_id: about.misconception_id })}>
        <Icon name="learn" size={16} />{t("concept.gap.see_on_map")}
      </Button>
    </span>
  );
}

function CardFallback({ data }: { data: unknown }) {
  const card = (data as PartRec | null)?.card?.card as { prompt?: string; body?: string; title?: string; options?: { id: string; label: string }[] } | undefined;
  return (
    <div className="my-2">
      <p>{card?.prompt ?? card?.body ?? card?.title ?? ""}</p>
      {(card?.options ?? []).map((o) => <button key={o.id} type="button" className="me-2 underline">{o.label}</button>)}
    </div>
  );
}

/** Catalogue keys the engine names in `notice.message_key`: a request to save something that must never be kept is
 * declined with this line and nothing is stored (MEM-11, MEM-10); the cloud tutor's fallback to the tutor on this
 * computer, before the first token and mid-stream (UXS-08, MDL-24); answers marked again after a corrected key, with
 * their number in `params.count` (ASM-23, CUR-36). */
export const ENGINE_NOTICE_KEYS = ["notice.hint_offer", "memory.declined", "notice.cloud_unavailable_using_local", "notice.cloud_continuing_locally", "notice.marking_corrected", "notice.tutor_unsure", "notice.beyond_level_topic", "voice.now_local"] as const;
/** Catalogue keys the engine names in a notice's `action.label_key`: "Open lesson" on the not-sure reply (UXS-20). */
export const ENGINE_NOTICE_ACTION_KEYS = ["notice.open_lesson", "notice.open_topic"] as const;

/** The quiet hint-offer line: muted meta text after a clock, announced politely. */
export function HintOffer({ message }: { message: string }) {
  return <p role="status" data-hint-offer="" className="flex items-center gap-2 text-meta text-muted"><Icon name="take-your-time" size={16} />{message}</p>;
}

function NoticePart({ data }: RenderProps<PartRec>) {
  const t = useT();
  const openLesson = useOpenLesson();
  const openTopic = useOpenTopic();
  const notice = data.notice as { level?: string; code?: string; message_key?: string; params?: Record<string, string | number>; action?: { label_key: string; command: string } | null } | undefined;
  // The calm screen's notice is the calm screen itself (UXS-43), drawn over the workspace, never a line in the transcript.
  if (notice?.code === "calm_screen") return null;
  // The engine's hint offer (LRN-20, D112): one quiet line, no card to answer; the hint chip above the composer is the
  // way to take it up.
  if (notice?.code === "hint_offer") return <HintOffer message={t(notice.message_key ?? "notice.hint_offer")} />;
  // The not-sure reply's lesson (UXS-20 "Tutor unsure", PRD-05): "Open lesson" where the app can open one.
  const lesson = lessonOfCommand(notice?.action?.command);
  const topic = topicOfCommand(notice?.action?.command);
  const label = notice?.action?.label_key;
  const action = lesson !== null && openLesson !== null && label !== undefined ? { label: t(label), onClick: () => openLesson(lesson) } : topic !== null && openTopic !== null && label !== undefined ? { label: t(label), onClick: () => openTopic(topic) } : undefined;
  return <Notice tone={notice?.level === "warning" ? "warn" : "info"} message={t(notice?.message_key ?? "part.unavailable", notice?.params)} {...(action !== undefined ? { action } : {})} />;
}

function BoardPart({ data }: RenderProps<PartRec>) {
  const t = useT();
  return (
    <div className="flex items-center gap-2 py-0.5 text-ui text-muted" data-part={data.part_id}>
      <Icon name="verified" size={16} />
      {t("tool.board.done")}
    </div>
  );
}

function VisualWords({ data }: { data: unknown }) {
  const t = useT();
  // A resolved spec carries its words in `alt.summary` (VSL-14); without them, the generic line.
  const alt = (data as { alt?: { summary?: unknown } } | null)?.alt?.summary;
  return <p className="my-2 rounded-card border border-line p-3 text-ui text-muted">{typeof alt === "string" && alt !== "" ? alt : t("visual.fallback")}</p>;
}

function Unavailable() {
  const t = useT();
  return <p className="my-1 text-meta text-muted">{t("part.unavailable")}</p>;
}

function Wildcard({ partId }: RenderProps<unknown>) {
  const t = useT();
  return <div className="text-ui text-muted" data-wildcard={partId}>{t("tool.internal.done")}</div>;
}

export function defaultRegistry(): Registry {
  return new Registry()
    .register<PartRec>("part:text", { schema: null, Component: TextPart, Fallback: TextFallback, renderClass: "card" })
    .register<PartRec>("part:tool", { schema: null, Component: ToolPart, Fallback: ToolFallback, renderClass: "ephemeral" })
    .register<PartRec>("part:card", { schema: null, Component: CardPart, Fallback: CardFallback, renderClass: "card" })
    .register<PartRec>("part:notice", { schema: null, Component: NoticePart, Fallback: Unavailable, renderClass: "card" })
    .register<PartRec>("part:board", { schema: null, Component: BoardPart, Fallback: Unavailable, renderClass: "ephemeral" })
    .register<unknown>("visual:*", { schema: null, Component: ({ data }) => <VisualWords data={data} />, Fallback: VisualWords, renderClass: "card" })
    .register<unknown>("*", { schema: null, Component: Wildcard, Fallback: Unavailable, renderClass: "ephemeral" });
}

export type { ReactNode };
