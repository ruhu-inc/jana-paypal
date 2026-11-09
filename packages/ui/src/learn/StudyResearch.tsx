// The research agent's study packs (SPEC §18): talk it through, approve a plan, then a pack made from the web.
// 1. "Research a topic" (or "Make a study pack" from a practised diagram) opens a short conversation: Jana asks at
//    most three questions, each with quick replies, and "Skip, just research it" is always there (§18.1a).
// 2. The brief: the learner can change the title, the level and the focus, remove or add a question, and choose what
//    to make; nothing is searched before "Start research".
// 3. The pack: each question's progress while the gateway researches, then each part as Study makes it. The pages
//    are one Study material ("Web research: …"), so "Open the pack" shows its concept map, cards, quiz and podcast in
//    the usual material view, each claim with its cited page.
// The pack is polled while it is still being made; leaving and coming back finds it where it got to.

import { useEffect, useRef, useState } from "react";
import type { ResearchContracts as R, StudyWorkspaceController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, ErrorState, FIELD_LOOK, Notice } from "../primitives/index.js";

const POLL_MS = 3000;
const LIVE = ["sending", "waiting_online", "researching", "making"];
const OUTPUTS: readonly R.Output[] = ["diagram_drill", "concept_map", "notes", "flashcards", "quiz", "audio_overview"];

type Start = { topic: string } | { capture_id: string };
type Phase = { kind: "start" } | { kind: "talking"; start: Start; history: R.ChatEntry[]; turn: R.ChatTurn | null }
  | { kind: "brief"; start: Start; brief: R.Brief } | { kind: "pack"; id: string };

export function StudyResearch({ controller, initialCapture, topicRequest, start: startRequest, onStartUsed, hideStart = false, onCaptureUsed, onOpenSource, onPracticeDiagram, disabled }: {
  controller: StudyWorkspaceController;
  /** A topic the learner chose to research from Explore's one topic field (the crowding review of 2026-11-01): the
   * conversation starts at once. A new `seq` starts it again. */
  start?: { topic: string; seq: number } | null;
  /** The start was taken: the owner clears it, so coming back to Explore never starts (and bills) it again. */
  onStartUsed?: (() => void) | undefined;
  /** Explore draws the topic field itself: the start phase shows only what is in progress and the saved packs. */
  hideStart?: boolean;
  /** A topic handed over by the companion's "Research this topic" (SPEC §19.3.3): filled in for the learner, who starts
   * the conversation themselves (it is never skipped). A new `seq` fills it again. */
  topicRequest?: { topic: string; seq: number } | null;
  /** A practised diagram's capture: the conversation starts from it (from the diagram drill's "Make a study pack"). */
  initialCapture?: string | null;
  onCaptureUsed?: (() => void) | undefined;
  onOpenSource: (sourceId: string) => void;
  onPracticeDiagram?: (captureId: string) => void;
  disabled?: boolean;
}) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>({ kind: "start" });
  const [topic, setTopic] = useState(""), [reply, setReply] = useState(""), [extra, setExtra] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [packs, setPacks] = useState<R.Record[]>([]);
  // The online tutor's first-use notice (ARC-10): the conversation goes to it, so a profile that has not answered it
  // is asked here, and its answer goes with the same turn.
  const [notice, setNotice] = useState<{ start: Start; history: R.ChatEntry[]; answer: string | null; skip: boolean } | null>(null);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // A Study controller without the research agent (an older or a partial one) shows the panel with no saved packs.
  useEffect(() => { if (typeof controller.researchPacks === "function") void controller.researchPacks().then((items) => { if (alive.current) setPacks(items); }).catch(() => undefined); }, [controller]);

  const failed = (err: unknown) => {
    const code = typeof err === "object" && err !== null && "code" in err ? String((err as { code: unknown }).code) : "generic";
    if (alive.current) setError(code);
  };
  const ask = async (start: Start, history: R.ChatEntry[], answer: string | null, skip = false, cloudNotice?: "accepted") => {
    setBusy(true); setError(null); setNotice(null);
    try {
      const turn = await controller.researchChat({ start, history, answer, skip, ...(cloudNotice ? { cloud_notice: cloudNotice } : {}) });
      if (!alive.current) return;
      const said: R.ChatEntry[] = answer === null ? history : [...history, { role: "learner", text: answer, asks: null }];
      if (turn.kind === "brief" && turn.brief) { setPhase({ kind: "brief", start, brief: turn.brief }); return; }
      if (turn.kind === "question" && turn.question) {
        setPhase({ kind: "talking", start, turn, history: [...said, { role: "jana", text: turn.question, asks: turn.asks }] });
        return;
      }
      setPhase({ kind: "talking", start, turn, history: said }); // out of scope: Jana's own words, then start again
    } catch (err) {
      const reason = typeof err === "object" && err !== null && "details" in err ? (err as { details?: { reason?: unknown } }).details?.reason : undefined;
      if (alive.current && (reason === "notice_required" || reason === "notice_declined")) setNotice({ start, history, answer, skip });
      else failed(err);
    } finally { if (alive.current) setBusy(false); }
  };
  // A capture handed over from the diagram drill starts its conversation once, however often this effect runs (a
  // development build runs effects twice; a second turn at once would collide with the first).
  const handedOver = useRef<string | null>(null);
  useEffect(() => {
    if (!initialCapture || handedOver.current === initialCapture) return;
    handedOver.current = initialCapture;
    onCaptureUsed?.();
    void ask({ capture_id: initialCapture }, [], null);
  }, [initialCapture]);

  const topicField = useRef<HTMLInputElement>(null);
  const started = useRef<number | null>(null);
  useEffect(() => {
    if (!startRequest || started.current === startRequest.seq) return;
    started.current = startRequest.seq;
    onStartUsed?.();
    const asked = startRequest.topic.trim().slice(0, 200);
    if (asked.length >= 3) void ask({ topic: asked }, [], null);
  }, [startRequest?.seq]);
  useEffect(() => {
    if (!topicRequest) return;
    setPhase({ kind: "start" });
    setTopic(topicRequest.topic.slice(0, 200));
    requestAnimationFrame(() => { topicField.current?.scrollIntoView?.({ block: "center" }); topicField.current?.focus({ preventScroll: true }); });
  }, [topicRequest?.seq]);

  // A pack still being made is read again every few seconds (each read brings it up to date on the device).
  const shownId = phase.kind === "pack" ? phase.id : null;
  const shown = packs.find((p) => p.research_id === shownId) ?? null;
  useEffect(() => {
    if (shownId === null) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const read = () => {
      void controller.researchPack(shownId).then((record) => {
        if (!alive.current) return;
        setError(null); // a pack that reads again clears an earlier poll's failure
        setPacks((all) => [record, ...all.filter((p) => p.research_id !== record.research_id)]);
        if (LIVE.includes(record.status)) timer = setTimeout(read, POLL_MS);
      }).catch((err) => { failed(err); if (alive.current && ++misses < 5) timer = setTimeout(read, POLL_MS); }); // one failed poll never ends the watch
    };
    let misses = 0;
    read();
    return () => { if (timer !== null) clearTimeout(timer); };
  }, [controller, shownId]);

  const start = async (brief: R.Brief, captureId: string | null) => {
    setBusy(true); setError(null);
    try {
      const record = await controller.startResearch(brief, captureId);
      if (!alive.current) return;
      setPacks((all) => [record, ...all]); setPhase({ kind: "pack", id: record.research_id });
    } catch (err) { failed(err); } finally { if (alive.current) setBusy(false); }
  };

  const errorLine = <>
    {error !== null && <ErrorState message={t(`study.research.error.${error}`) === `study.research.error.${error}` ? t("study.research.error.generic") : t(`study.research.error.${error}`)} />}
    {notice !== null && <div role="group" aria-labelledby="study-research-notice" className="flex flex-col gap-2 rounded-card border border-line p-3" data-research-notice>
      <p id="study-research-notice" className="text-ui font-semibold text-ink">{t("study.research.notice.title")}</p>
      <p className="text-ui text-ink">{t("study.research.notice.body")}</p>
      <div className="flex flex-wrap gap-2">
        <Button tone="primary" disabled={busy} onClick={() => { const n = notice; void ask(n.start, n.history, n.answer, n.skip, "accepted"); }}>{t("egress.first_use.ok")}</Button>
        <Button tone="ghost" disabled={busy} onClick={() => setNotice(null)}>{t("study.research.notice.not_now")}</Button>
      </div>
    </div>}
  </>;

  if (phase.kind === "talking") {
    const turn = phase.turn, out = turn?.kind === "out_of_scope", fromPhoto = "capture_id" in phase.start;
    return <section className="flex flex-col gap-3" aria-labelledby="study-research" data-research-talk>
      <h2 id="study-research" className="text-ui font-semibold text-ink">{t(fromPhoto ? "diagram.study_pack" : "study.research.heading")}</h2>
      <ol className="flex flex-col gap-2" aria-live="polite">
        {phase.history.map((entry, i) => <li key={i} data-said={entry.role} className={entry.role === "jana" ? "self-start rounded-card bg-panel p-2 text-ui" : "self-end rounded-card border border-line p-2 text-ui"}>{entry.text}</li>)}
        {out && <li data-said="jana" className="self-start rounded-card bg-panel p-2 text-ui">{turn?.question}</li>}
      </ol>
      {!out && turn?.kind === "question" && <>
        {turn.chips.length > 0 && <div className="flex flex-wrap gap-2" aria-label={t("study.research.quick_replies")}>{turn.chips.map((chip) => <Button key={chip} size="sm" disabled={busy} onClick={() => { void ask(phase.start, phase.history, chip); }}>{chip}</Button>)}</div>}
        <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); const said = reply.trim(); if (!said) return; setReply(""); void ask(phase.start, phase.history, said); }}>
          <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1 text-ui text-ink">{t("study.research.your_reply")}<input className={FIELD_LOOK} value={reply} maxLength={400} disabled={busy} onChange={(e) => setReply(e.target.value)} /></label>
          <Button type="submit" disabled={busy || !reply.trim()}>{t("action.send")}</Button>
        </form>
        <Button tone="ghost" disabled={busy} onClick={() => { void ask(phase.start, phase.history, null, true); }}>{t("study.research.skip")}</Button>
      </>}
      {busy && <p role="status" className="text-meta text-muted">{t("study.research.thinking")}</p>}
      {errorLine}
      <Button tone="ghost" onClick={() => setPhase({ kind: "start" })}>{out ? t("study.research.start_again") : t("action.cancel")}</Button>
    </section>;
  }

  if (phase.kind === "brief") {
    const brief = phase.brief, capture = "capture_id" in phase.start ? phase.start.capture_id : null;
    const set = (next: Partial<R.Brief>) => setPhase({ ...phase, brief: { ...brief, ...next } });
    const outputs = OUTPUTS.filter((o) => o !== "diagram_drill" || capture !== null);
    return <section className="flex flex-col gap-3" aria-labelledby="study-research-brief" data-research-brief>
      <h2 id="study-research-brief" className="text-ui font-semibold text-ink">{t("study.research.brief_heading")}</h2>
      <p className="text-meta text-muted">{t("study.research.brief_help")}</p>
      <label className="flex flex-col gap-1 text-ui text-ink">{t("study.research.title")}<input className={FIELD_LOOK} value={brief.title} maxLength={120} onChange={(e) => set({ title: e.target.value })} /></label>
      <label className="flex flex-col gap-1 text-ui text-ink">{t("study.research.level")}<select className={FIELD_LOOK} value={brief.level} onChange={(e) => set({ level: e.target.value as R.Brief["level"] })}><option value="university">{t("study.research.level_university")}</option><option value="o_level">{t("study.research.level_o")}</option><option value="first_year">{t("study.research.level_first_year")}</option></select></label>
      <label className="flex flex-col gap-1 text-ui text-ink">{t("study.research.focus")}<input className={FIELD_LOOK} value={brief.focus} maxLength={300} onChange={(e) => set({ focus: e.target.value })} /></label>
      <fieldset className="flex flex-col gap-2"><legend className="text-ui text-ink">{t("study.research.questions")}</legend>
        <ol className="flex flex-col gap-1">{brief.sub_questions.map((q, i) => <li key={i} className="flex items-center justify-between gap-2 text-ui" data-sub-question>
          <span>{q}</span><Button size="sm" tone="ghost" disabled={brief.sub_questions.length <= 4} aria-label={t("study.research.remove", { question: q })} onClick={() => set({ sub_questions: brief.sub_questions.filter((_, n) => n !== i) })}>{t("action.remove")}</Button></li>)}</ol>
        {brief.sub_questions.length < 8 && <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); const q = extra.trim(); if (q.length < 8) return; set({ sub_questions: [...brief.sub_questions, q.slice(0, 200)] }); setExtra(""); }}>
          <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1 text-meta text-ink">{t("study.research.add_question")}<input className={FIELD_LOOK} value={extra} maxLength={200} onChange={(e) => setExtra(e.target.value)} /></label>
          <Button size="sm" type="submit" disabled={extra.trim().length < 8}>{t("study.research.add")}</Button>
        </form>}
      </fieldset>
      <fieldset className="flex flex-wrap gap-3"><legend className="text-ui text-ink">{t("study.research.make")}</legend>
        {outputs.map((o) => <label key={o} className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={brief.outputs.includes(o)} onChange={(e) => {
          const next = e.target.checked ? [...brief.outputs, o] : brief.outputs.filter((x) => x !== o);
          if (next.length > 0) set({ outputs: next });
        }} />{t(`study.research.output.${o}`)}</label>)}
      </fieldset>
      <fieldset className="flex flex-wrap gap-3"><legend className="text-ui text-ink">{t("study.research.length")}</legend>
        {(["short", "long"] as const).map((l) => <label key={l} className="flex min-h-11 items-center gap-2 text-ui"><input type="radio" name="research-length" checked={brief.length === l} onChange={() => set({ length: l })} />{t(`study.research.length_${l}`)}</label>)}
      </fieldset>
      <p className="text-meta text-muted">{t("study.research.what_leaves")}</p>
      {errorLine}
      <div className="flex flex-wrap gap-2">
        <Button tone="primary" disabled={busy || brief.title.trim().length < 3} onClick={() => { void start({ ...brief, title: brief.title.trim() }, capture); }}>{t("study.research.start")}</Button>
        <Button tone="ghost" onClick={() => setPhase({ kind: "start" })}>{t("action.cancel")}</Button>
      </div>
    </section>;
  }

  if (phase.kind === "pack" && shown !== null) {
    const pack = shown;
    return <section className="flex flex-col gap-3" aria-labelledby="study-research-pack" data-research-pack={pack.research_id} data-status={pack.status}>
      <h2 id="study-research-pack" className="text-ui font-semibold text-ink">{pack.brief.title}</h2>
      <p className="text-meta text-muted" data-research-made-by>{t("study.research.made_by")}</p>
      <p role="status" className="text-ui">{t(`study.research.status.${pack.status}`, { pages: pack.pages })}</p>
      {pack.failure !== null && <ErrorState message={t(`study.research.failure.${pack.failure}`)} />}
      {["sending", "waiting_online", "researching"].includes(pack.status) && <ol className="flex flex-col gap-1">{pack.sub_questions.map((q, i) => <li key={i} className="flex justify-between gap-2 text-ui" data-state={q.state}>
        <span>{q.text}</span><span className="text-meta text-muted">{t(`study.research.question_state.${q.state}`, { sources: q.sources })}</span></li>)}</ol>}
      {pack.parts.length > 0 && <ul className="flex flex-col gap-1">{pack.parts.map((p) => <li key={p.kind} className="flex justify-between gap-2 text-ui" data-part={p.kind} data-state={p.state}>
        <span>{t(`study.research.output.${p.kind}`)}</span><span className="text-meta text-muted">{t(`study.research.part_state.${p.state}`)}</span></li>)}</ul>}
      {pack.status === "ready" && <Notice message={t("study.research.checked")} />}
      <div className="flex flex-wrap gap-2">
        {pack.source_id !== null && <Button tone="primary" onClick={() => { if (pack.source_id) onOpenSource(pack.source_id); }}>{t("study.research.open")}</Button>}
        {pack.capture_id !== null && onPracticeDiagram && pack.brief.outputs.includes("diagram_drill") && <Button onClick={() => { if (pack.capture_id) onPracticeDiagram(pack.capture_id); }}>{t("study.research.practise_diagram")}</Button>}
        {["sending", "waiting_online", "researching"].includes(pack.status) && <Button tone="ghost" onClick={() => { void controller.cancelResearch(pack.research_id).then((r) => setPacks((all) => [r, ...all.filter((p) => p.research_id !== r.research_id)])).catch(failed); }}>{t("study.research.cancel")}</Button>}
        <Button tone="ghost" onClick={() => setPhase({ kind: "start" })}>{t("study.research.back")}</Button>
      </div>
      {errorLine}
    </section>;
  }

  return <section className="flex flex-col gap-3" aria-labelledby={hideStart ? undefined : "study-research"} aria-label={hideStart ? t("study.research.your_packs") : undefined} data-research-start>
    {!hideStart && <>
      <h2 id="study-research" className="text-ui font-semibold text-ink">{t("study.research.heading")}</h2>
      <p className="text-meta text-muted">{t("study.research.help")}</p>
      <form className="flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); const asked = topic.trim(); if (asked.length < 3) return; void ask({ topic: asked.slice(0, 200) }, [], null); }}>
        <label className="flex min-w-0 flex-1 basis-56 flex-col gap-1 text-ui text-ink">{t("study.research.topic_label")}<input ref={topicField} className={FIELD_LOOK} value={topic} maxLength={200} disabled={busy || disabled} onChange={(e) => setTopic(e.target.value)} /></label>
        <Button type="submit" disabled={busy || disabled || topic.trim().length < 3}>{t("study.research.research")}</Button>
      </form>
    </>}
    {busy && <p role="status" className="text-meta text-muted">{t("study.research.thinking")}</p>}
    {errorLine}
    {packs.length > 0 && <div className="flex flex-col gap-2" aria-label={t("study.research.your_packs")}>{packs.slice(0, 6).map((p) => <Button key={p.research_id} size="sm" tone="ghost" onClick={() => setPhase({ kind: "pack", id: p.research_id })}>
      {p.brief.title} · {t(`study.research.status.${p.status}`, { pages: p.pages })}</Button>)}</div>}
  </section>;
}
