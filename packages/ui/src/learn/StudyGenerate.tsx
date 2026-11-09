// The study area's Generate panel, its settings dialogs and My Sets (STU-21, D118). The material pane's player,
// chapters and transcript live in StudyPlayer.tsx (STU-22; SPEC §20.4 for a video or a YouTube link).
import { useEffect, useState } from "react";
import { GENERATE_TOOLS, type GenerateTool, type StudyContracts as W } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon, type IconName } from "../icons.js";
import { Button, Dialog, Disclosure, FIELD_LOOK, IconButton, Notice, SegmentedControl } from "../primitives/index.js";

const TOOL_ICON: Record<GenerateTool, IconName> = { summary: "notes", quiz: "question", flashcards: "flashcards", notes: "learn", audio_overview: "read-aloud", lesson_plan: "timer", concept_map: "share" };
/** Small, decorative accents use Jana's existing palette; names and icons still identify every tool. */
function StudyToolIcon({ kind, size = 20 }: { kind: W.AidKind; size?: 16 | 20 }) {
  const colour = { summary: "blue", quiz: "pink", flashcards: "orange", notes: "amber", audio_overview: "purple", lesson_plan: "blue-green", chapters: "blue-green", chat: "blue", concept_map: "green", teaching_outline: "green" }[kind];
  return <span aria-hidden="true" className={`study-tool-icon study-tool-icon-${colour} inline-flex shrink-0 items-center justify-center rounded-control p-1`}><Icon name={kind === "chapters" ? "table" : TOOL_ICON[kind as GenerateTool] ?? "notes"} size={size} /></span>;
}
/** The question types a learner picks from (STU-21); a number and an expression are one choice. */
const QUESTION_TYPES = [["choice"], ["multi_select"], ["numeric", "expression"], ["fill_blank"], ["true_false"], ["free_response"]] as const;
type QuizType = W.QuizType;
export type GenerateRequest = { count: number; settings: W.GenerateSettings };
export const defaultRequest = (tool: GenerateTool): GenerateRequest => ({ count: 10, settings: tool === "quiz" ? { question_types: ["choice", "fill_blank", "true_false"], difficulty: "medium" } : tool === "audio_overview" ? { podcast_length: "standard", voices: 1 } : tool === "lesson_plan" ? { minutes_available: 45 } : {} });
const SUMMARY_STYLES = { detailed: "", cheat: "Format as concise revision bullet points. Preserve all source qualifiers and cover the whole material.", short: "Choose the essential source-supported points from every section, with brief wording and all qualifiers intact." } as const;

/** Three common tools first; additional formats remain available in a disclosure. */
export function GenerateTools({ disabled, empty = false, onPick, onConfigure, pendingKinds = [] }: { disabled: boolean; empty?: boolean; pendingKinds?: readonly string[]; onPick: (tool: GenerateTool) => void; onConfigure?: (tool: GenerateTool) => void }) {
  const t = useT();
  const [moreOpen, setMoreOpen] = useState(false);
  const card = (tool: GenerateTool) => <div key={tool} className="flex items-center rounded-card border border-line bg-canvas"><button type="button" disabled={disabled || empty || pendingKinds.includes(tool)} onClick={() => onPick(tool)}
        className="jana-press flex min-h-16 min-w-0 flex-1 items-start gap-3 rounded-card px-3 py-3 text-start hover:bg-wash disabled:cursor-not-allowed disabled:bg-chrome disabled:text-faint">
        <StudyToolIcon kind={tool} />
        <span className="flex min-w-0 flex-col gap-0.5"><span className="text-ui font-medium">{t(`study.tool.${tool}`)}</span><span className="text-meta text-muted">{t(`study.tool_help.${tool}`)}</span></span>
      </button>{onConfigure && <IconButton icon="settings" size="sm" label={t("study.configure_tool", { tool: t(`study.tool.${tool}`) })} disabled={disabled || empty || pendingKinds.includes(tool)} onClick={() => onConfigure(tool)} />}</div>;
  return <section className="flex flex-col gap-3" aria-labelledby="study-generate">
    <h2 id="study-generate" className="text-ui font-semibold text-ink">{t("study.generate_heading")}</h2>
    <p className="text-meta text-muted">{t(empty ? "study.generate_needs_material" : "study.generate_help")}</p>
    <div className="grid grid-cols-1 gap-2 @xl:grid-cols-3">{GENERATE_TOOLS.filter((tool) => ["summary", "quiz", "flashcards"].includes(tool)).map(card)}</div>
    <Disclosure summary={t("study.more_tools")} open={moreOpen} onToggle={() => setMoreOpen((open) => !open)}><div className="grid grid-cols-1 gap-2 pt-2 @xl:grid-cols-2">{GENERATE_TOOLS.filter((tool) => !["summary", "quiz", "flashcards"].includes(tool)).map(card)}</div></Disclosure>
  </section>;
}

/** One tool's settings (STU-21): what it takes, all topics kept by default, and Generate. */
export function GenerateDialog({ tool, topics, syllabi, busy, error = null, reviewRequired = false, reviewed = false, contextual = false, onReview, onClose, onGenerate }: {
  tool: GenerateTool | null; topics: W.MaterialTopics["items"]; syllabi: { source_id: string; revision: number; title: string }[];
  busy: boolean; error?: string | null; reviewRequired?: boolean; reviewed?: boolean; contextual?: boolean; onReview?: (value: boolean) => void; onClose: () => void; onGenerate: (tool: GenerateTool, request: GenerateRequest) => void;
}) {
  const t = useT();
  const [types, setTypes] = useState<QuizType[]>(["choice", "fill_blank", "true_false"]);
  const [difficulty, setDifficulty] = useState<"easy" | "medium" | "hard">("medium");
  const [count, setCount] = useState(10), [focus, setFocus] = useState("");
  const [customTopics, setCustomTopics] = useState(false);
  const [kept, setKept] = useState<string[]>([]), [syllabus, setSyllabus] = useState<string[]>([]);
  const [length, setLength] = useState<"short" | "standard">("standard"), [voices, setVoices] = useState<"1" | "2">("1");
  const [minutes, setMinutes] = useState(45), [readyBy, setReadyBy] = useState("");
  const [summaryStyle, setSummaryStyle] = useState<keyof typeof SUMMARY_STYLES>("detailed");
  useEffect(() => setKept(topics.map((topic) => topic.title)), [topics]);
  if (tool === null) return null;
  // A concept map takes no settings: it maps the whole material (its job refuses topics and focus).
  const takesTopics = tool !== "audio_overview" && tool !== "concept_map";
  const quiz = tool === "quiz", cards = tool === "flashcards";
  const ready = (!reviewRequired || reviewed) && (!quiz || types.length > 0) && (!customTopics || !takesTopics || topics.length === 0 || kept.length > 0);
  const toggle = (group: readonly QuizType[]) => setTypes((now) => group.every((x) => now.includes(x)) ? now.filter((x) => !group.includes(x)) : [...now, ...group.filter((x) => !now.includes(x))]);
  const build = (): GenerateRequest => {
    const settings: W.GenerateSettings = {};
    if (quiz) { settings.question_types = types; settings.difficulty = difficulty; }
    if (customTopics && takesTopics && topics.length > 0) settings.topics = kept;
    if (takesTopics && focus.trim()) settings.focus = focus.trim();
    if (tool === "summary" && SUMMARY_STYLES[summaryStyle]) settings.focus = [SUMMARY_STYLES[summaryStyle], focus.trim()].filter(Boolean).join("\n");
    if (quiz && syllabus.length > 0) settings.syllabus = syllabi.filter((s) => syllabus.includes(s.source_id)).map(({ source_id, revision }) => ({ source_id, revision }));
    if (tool === "audio_overview") { settings.podcast_length = length; settings.voices = voices === "2" ? 2 : 1; }
    if (tool === "lesson_plan") { settings.minutes_available = minutes; if (readyBy) settings.ready_by = readyBy; }
    return { count, settings };
  };
  return <Dialog open heading={t(`study.tool.${tool}`)} closeLabel={t("action.cancel")} onClose={onClose} wide>
    <form className="flex flex-col gap-5" onSubmit={(e) => { e.preventDefault(); if (ready && !busy) onGenerate(tool, build()); }}>
      <p className="text-ui text-muted">{t(contextual ? "study.context_material" : "study.whole_material")}</p>
      {reviewRequired && <label className="flex items-start gap-2 text-ui text-ink"><input type="checkbox" checked={reviewed} disabled={busy} onChange={(e) => onReview?.(e.target.checked)} />{t("study.confirm_readable")}</label>}
      {tool === "summary" && <SegmentedControl label={t("study.summary_style")} value={summaryStyle} onChange={setSummaryStyle} options={(Object.keys(SUMMARY_STYLES) as (keyof typeof SUMMARY_STYLES)[]).map((value) => ({ value, label: t(`study.summary_style.${value}`) }))} />}
      {quiz && <fieldset className="flex flex-col gap-2"><legend className="text-ui font-semibold">{t("study.setting.types")}</legend>
        <div className="grid grid-cols-2 gap-x-4">{QUESTION_TYPES.map((group) => <label key={group[0]} className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={group.every((x) => types.includes(x))} onChange={() => toggle(group)} />{t(`study.qtype.${group[0]}`)}</label>)}</div>
      </fieldset>}
      {quiz && <div className="flex flex-col gap-2"><span className="text-ui font-semibold">{t("study.setting.difficulty")}</span><SegmentedControl label={t("study.setting.difficulty")} value={difficulty} onChange={setDifficulty} options={(["easy", "medium", "hard"] as const).map((v) => ({ value: v, label: t(`study.difficulty.${v}`) }))} /></div>}
      {(quiz || cards) && <label className="flex items-center gap-3 text-ui text-ink">{t(quiz ? "study.count_questions" : "study.count_cards")}<span className="w-20 shrink-0"><input type="number" min={1} max={40} value={count} className={FIELD_LOOK} onChange={(e) => setCount(Math.max(1, Math.min(40, Math.trunc(Number(e.target.value)) || 1)))} /></span></label>}
      {tool === "audio_overview" && <>
        <div className="flex flex-col gap-2"><span className="text-ui font-semibold">{t("study.setting.length")}</span><SegmentedControl label={t("study.setting.length")} value={length} onChange={setLength} options={[{ value: "short", label: t("study.length.short") }, { value: "standard", label: t("study.length.standard") }]} /></div>
        <div className="flex flex-col gap-2"><span className="text-ui font-semibold">{t("study.setting.voices")}</span><SegmentedControl label={t("study.setting.voices")} value={voices} onChange={setVoices} options={[{ value: "1", label: t("study.voices.one") }, { value: "2", label: t("study.voices.two") }]} /></div>
      </>}
      {tool === "lesson_plan" && <div className="flex flex-wrap gap-4">
        <label className="flex flex-col gap-2 text-ui text-ink">{t("study.setting.minutes")}<span className="w-28 shrink-0"><input type="number" min={10} max={600} step={5} value={minutes} className={FIELD_LOOK} onChange={(e) => setMinutes(Math.max(10, Math.min(600, Math.trunc(Number(e.target.value)) || 10)))} /></span></label>
        <label className="flex flex-col gap-2 text-ui text-ink">{t("study.setting.ready_by")}<span className="w-44 shrink-0"><input type="date" value={readyBy} className={FIELD_LOOK} onChange={(e) => setReadyBy(e.target.value)} /></span></label>
      </div>}
      {takesTopics && topics.length > 0 && <details><summary className="cursor-pointer text-ui font-semibold">{t("study.setting.custom_topics")}</summary><label className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={customTopics} onChange={(e) => setCustomTopics(e.target.checked)} />{t("study.setting.limit_topics")}</label>{customTopics && <fieldset className="flex flex-col gap-2"><legend className="text-ui font-semibold">{t("study.setting.topics")}</legend>
        <p className="text-meta text-muted">{t("study.setting.topics_help")}</p>
        <div className="flex flex-wrap gap-2">{topics.map((topic) => { const on = kept.includes(topic.title); return <Button key={topic.title} size="sm" tone={on ? "secondary" : "ghost"} aria-pressed={on} onClick={() => setKept((now) => on ? now.filter((x) => x !== topic.title) : [...now, topic.title])}>{topic.title}</Button>; })}</div>
        {kept.length === 0 && <p role="status" className="text-meta text-muted">{t("study.setting.topics_none")}</p>}
      </fieldset>}</details>}
      {takesTopics && <label className="flex flex-col gap-2 text-ui text-ink">{t("study.setting.focus")}<textarea rows={2} maxLength={tool === "summary" ? 350 : 500} value={focus} placeholder={t("study.setting.focus_placeholder")} className={`${FIELD_LOOK} py-2`} onChange={(e) => setFocus(e.target.value)} /></label>}
      {quiz && syllabi.length > 0 && <fieldset className="flex flex-col gap-1"><legend className="text-ui font-semibold">{t("study.setting.syllabus")}</legend>
        <p className="text-meta text-muted">{t("study.setting.syllabus_help")}</p>
        {syllabi.map((s) => { const on = syllabus.includes(s.source_id); return <label key={s.source_id} className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={on} disabled={!on && syllabus.length >= 3} onChange={() => setSyllabus((now) => on ? now.filter((x) => x !== s.source_id) : [...now, s.source_id])} />{s.title}</label>; })}
      </fieldset>}
      {error && <Notice message={error} />}
      <div className="flex gap-2"><Button type="submit" tone="primary" disabled={busy || !ready}>{t("study.generate_action")}</Button><Button tone="ghost" onClick={onClose}>{t("action.cancel")}</Button></div>
    </form>
  </Dialog>;
}

/** A set's live progress in words (STU-21): questions left, or cards due today. */
export function progressText(t: ReturnType<typeof useT>, progress: W.SetProgress | undefined): string | null {
  if (!progress) return null;
  if ("questions_left" in progress) return progress.questions_left === 0 ? t("study.sets.quiz_done") : t("study.sets.questions_left", { count: progress.questions_left });
  return t("study.sets.cards_due", { count: progress.cards_due_today });
}

/** Latest ready sets first; earlier generations remain openable with their own progress and privacy gates. */
export function MySets({ sets, busy, onOpen, heading = true, contextual = false, sourceAliases }: { sets: W.StudySet[]; busy: boolean; onOpen: (set: W.StudySet) => void; heading?: boolean; contextual?: boolean; sourceAliases?: Map<string, string> }) {
  const t = useT(), [confirm, setConfirm] = useState<string | null>(null);
  const groups = new Map<string, [W.StudySet, ...W.StudySet[]]>();
  for (const set of sets) {
    const key = `${set.kind}:${set.sources.map((s) => `${sourceAliases?.get(s.source_id) ?? s.source_id}:${s.revision}`).sort().join(",")}`;
    const items = groups.get(key);
    if (items) items.push(set); else groups.set(key, [set]);
  }
  const families = [...groups.values()].map((items) => items.sort((a, b) => Number(b.status === "ready") - Number(a.status === "ready") || b.created_at.localeCompare(a.created_at)));
  // A set is named by what it holds ("Quiz · Biology · 3 questions"), its date in the meta line; inside its material
  // the material's title is left out.
  const size = (set: W.StudySet) => set.progress ? "questions_total" in set.progress ? t("study.quiz_questions", { count: set.progress.questions_total }) : t("study.sets.cards_count", { count: set.progress.cards_total }) : null;
  const row = (set: W.StudySet) => { const progress = progressText(t, set.progress), name = [t(`study.tool.${set.kind}`), ...(contextual ? [] : [set.title ?? t("study.private_source")]), ...(size(set) ? [size(set)] : [])].join(" · "); return <div key={set.aid_id} className="flex flex-col gap-2 py-1">
    <div className="flex items-center gap-3">
      <div className="min-w-0 flex-1 [&>button]:w-full [&>button]:justify-start [&>button]:text-start [&>button]:min-w-0"><Button size="sm" tone="ghost" disabled={busy} onClick={() => { if (set.private && !set.title) setConfirm(set.aid_id); else onOpen(set); }}>
        <StudyToolIcon kind={set.kind} size={16} />
        <span className="flex min-w-0 flex-col gap-0.5"><span className="truncate">{name}</span><span className="text-meta font-normal text-muted">{t("study.set_created", { when: new Date(set.created_at) })}</span></span>
      </Button></div>
      {set.status === "stale" ? <p className="text-meta text-muted">{t("study.stale")}</p> : progress && <p className="text-meta text-muted">{progress}</p>}
    </div>
    {confirm === set.aid_id && <div className="flex flex-col items-start gap-2 px-4"><p className="text-ui">{t("study.private_confirm")}</p><div className="flex gap-2"><Button size="sm" onClick={() => { setConfirm(null); onOpen(set); }}>{t("study.open_private")}</Button><Button size="sm" tone="ghost" onClick={() => setConfirm(null)}>{t("action.cancel")}</Button></div></div>}
  </div>; };
  return <section className="flex flex-col gap-2" aria-labelledby={heading ? "study-sets" : undefined} aria-label={heading ? undefined : t("study.sets.title")}>
    {heading && <h2 id="study-sets" className="text-ui font-semibold text-ink">{t("study.sets.title")}</h2>}
    {sets.length === 0 && heading && <p className="text-meta text-muted">{t("study.sets.empty")}</p>}
    <ul className="flex flex-col divide-y divide-line">{families.map((items) => <li key={items[0].aid_id} className="py-1">{row(items[0])}{items.length > 1 && <details className="ms-6"><summary className="cursor-pointer py-2 text-meta text-muted">{t("study.set_previous", { tool: t(`study.tool.${items[0].kind}`), count: items.length - 1 })}</summary><div className="flex flex-col border-s border-line ps-2">{items.slice(1).map(row)}</div></details>}</li>)}</ul>
  </section>;
}
