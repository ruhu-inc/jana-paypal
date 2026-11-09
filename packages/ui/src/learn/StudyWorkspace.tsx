// Study from your materials (STU, D61–D64): the library with My Sets, then an opened material as a split view, the
// material on the left (a recording with its player, chapters and transcript, STU-22) and its study tools on the
// right: the Generate panel with each tool's settings (STU-21, D118), **Ask this material** (one conversation grounded
// in the material, its history on demand, a one-line ask box that grows as the learner types) and **Your tutor**.
// A quiz is its own practice screen (StudyQuiz.tsx). The library's header keeps Add material; Activity and All study
// sets wait in a More menu, and Explore asks for one topic before the learner chooses research or a concept map
// (the crowding review of 2026-11-01).
import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { followUps, isQuizHelpPrompt, modelNameKey, type GenerateTool, type StudyContracts as W, type StudyWorkspaceController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Dialog, EmptyState, ErrorState, FIELD_LOOK, IconButton, Loader, MenuButton, Notice, ProgressBar } from "../primitives/index.js";
import { FollowUpChips } from "../chips/NextStepChips.js";
import { ComposerShell } from "../primitives/ComposerShell.js";
import { useAutoGrow } from "../primitives/autogrow.js";
import { StudyText as PackText, StudySourceText } from "./StudyText.js";
import { useOpenLesson, useOpenTopic } from "./link.js";
import { defaultRequest, GenerateDialog, GenerateTools, MySets, type GenerateRequest } from "./StudyGenerate.js";
import { StudyPlayer } from "./StudyPlayer.js";
import { StudyOutline } from "./StudyOutline.js";
import { StudyPdfReader } from "./StudyPdfReader.js";
import { StudyLinkAdd } from "./StudyLinkAdd.js";
import { StudyConceptMap } from "./StudyConceptMap.js";
import { StudyResearch } from "./StudyResearch.js";
import { SponsoredPlace } from "./SponsoredPlace.js";
import { StudyLibrary, materialGroups } from "./StudyLibrary.js";
import { StudyJourney } from "./StudyJourney.js";
import { StudyQuiz, type StudyHelpKind } from "./StudyQuiz.js";
import { citationKey, StudyCitations } from "./StudyCitation.js";

const ACCEPT = ".pdf,.txt,.md,.tex,.bib,.rst,.csv,.tsv,.py,.r,.m,.jl,.java,.c,.h,.cpp,.cs,.js,.ts,.go,.rs,.sql,.ipynb,.json,.yaml,.yml,.html,.docx,.odt,.pptx,.odp,.png,.jpg,.jpeg,.webp,.wav,.mp3,.m4a,.mp4,.webm,.ogg,.flac,.aac,.mov,.mkv,.avi";
const TWO_PANES = "(min-width: 1180px)"; // the window width at which @4xl shows the source beside the tools, sidebar open
// Below the two-pane width only one side shows: start on the study tools, the reason a learner opened the material.
const startsOnSource = () => typeof window === "undefined" || window.matchMedia === undefined || window.matchMedia(TWO_PANES).matches;
export type StudyTab = "generate" | "chat" | "companion";
type Tab = StudyTab;
const TABS: readonly Tab[] = ["generate", "chat"];
/** With the companion thread (SPEC §19.4): **Your tutor** is a third tab beside Generate and Ask this material. */
const TABS_WITH_COMPANION: readonly Tab[] = [...TABS, "companion"];
/** Somewhere the app asks Study to go (SPEC §19.3.3, a companion button or the plan's due Study cards): a job, aid or
 * deck opened in its material, the add panel, research with a topic filled in (never started for the learner), or
 * **Your tutor** (a check-in opened from Home, §19.5.5: the library's panel, or its tab inside an open material). */
export type StudyRequest = { kind: "target"; job_id?: string; aid_id?: string; deck_id?: string } | { kind: "add" } | { kind: "research"; topic: string } | { kind: "companion" }
  // From the learner's map (SPEC §21.1): a material opens; "Make flashcards" opens it with the flashcard tool chosen.
  | { kind: "source"; source_id: string } | { kind: "flashcards"; source_id: string }
  // Open the moment (SPEC §20.4, a companion button): the material opens at a moment (the browser for a YouTube video).
  | { kind: "moment"; source_id: string; at_ms: number };
/** The tab an aid of this kind lives under: a chat reply under Ask this material, every generated aid under Generate. */
const tabOf = (kind: string): Tab => (kind === "chat" ? "chat" : "generate");

/** `onTab` hears which tab is open: the voice conversation lives in Ask this material (VOC-53, D130). */
export function StudyWorkspace({ controller, curriculum = true, companion, request, onRequestDone, onPractice, onTab, onReviewAccess, initialAdd, onJourneyStarted, initialJourney, onJourneyOpened, initialSource, onSourceOpened, initialResearch, onResearchOpened, onAskMoment, onSkill, skillName }: { controller: StudyWorkspaceController;
  /** The Last time card (SPEC §19.5.2) is Home's Continue learning block now; the library no longer repeats it. */ lastTime?: ReactNode;
  /** Ask about this moment (SPEC §20.5 step 4): Your tutor opens with the moment as its pending context. */ onAskMoment?: (source: W.Source, at_ms: number) => void;
  /** A skill chip in the outline: the learner's map at that skill (SPEC §21). */ onSkill?: (kcId: string) => void; skillName?: (kcId: string) => string | null;
  /** Off for a learner with no curriculum subjects: no syllabus topics, journeys or pack labels, only their material. */ curriculum?: boolean;
  /** **Your tutor** (SPEC §19.4): the library's panel above the sources and a third tab inside a material, one thread. */ companion?: (place: "panel" | "tab") => ReactNode;
  /** A place to go, once (a companion button, the plan's due Study cards); `onRequestDone` hears it was handled. */ request?: StudyRequest | null; onRequestDone?: () => void; /** A practised diagram's capture: Study's research starts from it (SPEC §18). */ initialResearch?: string | null; onResearchOpened?: () => void; onPractice?: (subject: string, kcIds: string[]) => void; onTab?: (tab: Tab) => void; onReviewAccess?: () => void; initialAdd?: "paste" | null; onJourneyStarted?: (node: string, subject: string) => void; initialJourney?: "next" | "lesson" | "quiz" | null; onJourneyOpened?: () => void; initialSource?: Pick<W.Source, "source_id" | "revision"> | null; onSourceOpened?: () => void }) {
  const openLesson = useOpenLesson(), openTopic = useOpenTopic();
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView), t = useT();
  const [journeyChoices, setJourneyChoices] = useState<Record<string, string>>({});
  const [topicsSearched, setTopicsSearched] = useState(false);
  const [topicText, setTopicText] = useState("");
  const [teachReview, setTeachReview] = useState(false);
  const [libraryTab, setLibraryTab] = useState<"materials" | "companion" | "explore">(initialResearch ? "explore" : "materials");
  const [sourceOpen, setSourceOpen] = useState(startsOnSource);
  const [sourcePage, setSourcePage] = useState(1), [pageLoading, setPageLoading] = useState(false), [pageFailed, setPageFailed] = useState(false);
  const studyReady = view.status === "ready";
  useEffect(() => { if (studyReady) controller.loadTopicMaps().catch(() => undefined); }, [controller, studyReady]);
  // The materials research packs made (SPEC §18): their concept maps say their concepts came from the web.
  const [webSources, setWebSources] = useState<ReadonlySet<string>>(new Set());
  useEffect(() => {
    if (!studyReady || typeof controller.researchPacks !== "function") return;
    void controller.researchPacks().then((packs) => setWebSources(new Set(packs.flatMap((p) => (p.source_id ? [p.source_id] : []))))).catch(() => undefined);
  }, [controller, studyReady, view.source?.source_id]);
  useEffect(() => setTopicsSearched(false), [view.source?.source_id, view.source?.revision]);
  const [adding, setAdding] = useState(false), [activityOpen, setActivityOpen] = useState(false), [allSetsOpen, setAllSetsOpen] = useState(false), [activityLimit, setActivityLimit] = useState(8);
  const [linking, setLinking] = useState(false);
  const [title, setTitle] = useState(""), [text, setText] = useState(""), [confirm, setConfirm] = useState<string | null>(null);
  const [pageImage, setPageImage] = useState<{ url: string; page: number } | null>(null);
  const pageUrl = useRef<string | null>(null), pageRequest = useRef(0), pageFigure = useRef<HTMLElement>(null);
  useEffect(() => {
    if (pageUrl.current) URL.revokeObjectURL(pageUrl.current);
    pageUrl.current = null; setPageImage(null); setSourcePage(1); setPageLoading(false); setPageFailed(false); setSourceOpen(startsOnSource());
    return () => { pageRequest.current++; if (pageUrl.current) URL.revokeObjectURL(pageUrl.current); pageUrl.current = null; };
  }, [view.source?.source_id, view.source?.revision]);
  useEffect(() => { if (pageImage) pageFigure.current?.scrollIntoView?.({ block: "start" }); }, [pageImage]);
  const showPage = (page: number, revision: number, sourceId: string) => { const request = ++pageRequest.current;
    setSourcePage(page); setPageLoading(true); setPageFailed(false); setSourceOpen(true);
    if (pageUrl.current) URL.revokeObjectURL(pageUrl.current);
    pageUrl.current = null; setPageImage(null);
    void controller.loadPage(page, revision, sourceId).then((blob) => {
    if (request !== pageRequest.current) return;
    setPageLoading(false);
    if (blob === null) { setPageFailed(true); return; }
    if (pageUrl.current) URL.revokeObjectURL(pageUrl.current);
    const url = URL.createObjectURL(blob); pageUrl.current = url; setPageImage({ url, page });
  }).catch(() => { if (request === pageRequest.current) { setPageLoading(false); setPageFailed(true); } }); };
  useEffect(() => {
    if (!sourceOpen || view.source?.kind !== "pdf" || view.busy || pageImage || pageLoading || pageFailed) return;
    const first = view.chunks.find((c) => c.locator.kind === "page" && c.source_id === view.source?.source_id);
    if (first?.locator.kind === "page") showPage(first.locator.page, first.revision, first.source_id);
  }, [sourceOpen, view.source?.source_id, view.chunks, view.busy, pageImage, pageLoading, pageFailed]);
  // Study processing uses the online plan; saved passages remain readable on this computer.
  const preferCloud = true;
  const [helpReview, setHelpReview] = useState<{ itemId: string; kind: StudyHelpKind } | null>(null);
  const [chatPrompt, setChatPrompt] = useState(""), [noteConfirm, setNoteConfirm] = useState<string | null>(null), [extraConfirm, setExtraConfirm] = useState<string | null>(null);
  const [chatContext, setChatContext] = useState<string | null>(null);
  useEffect(() => { setChatContext(null); }, [view.aid?.aid_id, view.source?.source_id, view.source?.revision]);
  const [readableConfirmed, setReadableConfirmed] = useState(false);
  const reviewedRevisions = useRef(new Set<string>());
  const reviewKey = `${view.source?.source_id}:${view.source?.revision}`;
  useEffect(() => setReadableConfirmed(reviewedRevisions.current.has(reviewKey)), [reviewKey]);
  const review = (value: boolean) => { setReadableConfirmed(value); if (value) reviewedRevisions.current.add(reviewKey); else reviewedRevisions.current.delete(reviewKey); };
  const [correcting, setCorrecting] = useState(false), [correctedText, setCorrectedText] = useState("");
  const [deleting, setDeleting] = useState(false), [revealed, setRevealed] = useState(false);
  const [citation, setCitation] = useState<W.Citation | null>(null), [tool, setTool] = useState<GenerateTool | null>(null);
  const [toolContext, setToolContext] = useState<string | null>(null);
  useEffect(() => { setToolContext(null); }, [view.aid?.aid_id, view.source?.source_id, view.source?.revision]);
  const [duplicate, setDuplicate] = useState<{ tool: GenerateTool; request: GenerateRequest; aid: W.Aid } | null>(null);
  const [pasting, setPasting] = useState(initialAdd === "paste"), [managing, setManaging] = useState(false), [dragging, setDragging] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [tab, setTab] = useState<Tab>(view.aid ? tabOf(view.aid.kind) : "generate");
  useEffect(() => { onTab?.(tab); }, [onTab, tab]);
  useEffect(() => { if (!companion && tab === "companion") setTab("generate"); }, [companion, tab]);
  useEffect(() => { if (view.aid) { setTab(tabOf(view.aid.kind)); setSourceOpen(false); setHistoryOpen(false); } }, [view.aid?.aid_id, view.aid?.kind]);
  const resultPanel = useRef<HTMLElement>(null);
  useEffect(() => { if (view.aid) { resultPanel.current?.focus({ preventScroll: true }); resultPanel.current?.scrollIntoView?.({ block: "start" }); } }, [view.aid?.aid_id, tab]);
  const passage = useRef<HTMLDivElement>(null), returnFocus = useRef<HTMLElement | null>(null), ask = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(ask, chatPrompt);
  useEffect(() => { void controller.load(); void controller.loadAccess?.(); return () => controller.close(); }, [controller]);
  const journeyOpened = useRef(onJourneyOpened);
  journeyOpened.current = onJourneyOpened;
  useEffect(() => {
    if (!initialJourney) return;
    let live = true;
    // Wait for mounting/StrictMode cleanup and the profile-local snapshot before opening the saved activity.
    void controller.load().then(async () => {
      if (!live) return;
      await controller.continueJourney(initialJourney === "next" ? undefined : initialJourney);
      if (live) journeyOpened.current?.();
    });
    return () => { live = false; };
  }, [controller, initialJourney]);
  // A place the app asks for (SPEC §19.3.3): handled after the workspace mounts and its library is read, as a source
  // the app opens is, so a development build's double mount never cancels it.
  const requestDone = useRef(onRequestDone);
  requestDone.current = onRequestDone;
  const [researchStart, setResearchStart] = useState<{ topic: string; seq: number } | null>(null);
  const researchSeq = useRef(0);
  const addPanel = useRef<HTMLDivElement>(null), exploreField = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!request) return;
    let live = true;
    void controller.load().then(async () => {
      if (!live) return;
      if (request.kind === "target") await controller.openTarget(request);
      else if (request.kind === "companion") { if (controller.getView().source) { setTab("companion"); setSourceOpen(false); } else setLibraryTab("companion"); }
      else if (request.kind === "source" || request.kind === "flashcards") {
        await controller.open(request.source_id);
        if (live && request.kind === "flashcards" && controller.getView().source) { setTab("generate"); setTool("flashcards"); }
      }
      else if (request.kind === "moment") {
        await controller.open(request.source_id);
        if (live && controller.getView().source) controller.openMoment(request.at_ms);
      }
      else {
        if (controller.getView().source) controller.close();
        if (request.kind === "add") { setLibraryTab("materials"); setAdding(true); setPasting(false); requestAnimationFrame(() => addPanel.current?.scrollIntoView?.({ block: "start" })); }
        // A topic handed over by the companion is filled in, never started for the learner (SPEC §19.3.3).
        else { setLibraryTab("explore"); setTopicText(request.topic.slice(0, 200)); requestAnimationFrame(() => { exploreField.current?.scrollIntoView?.({ block: "center" }); exploreField.current?.focus({ preventScroll: true }); }); }
      }
      if (live) requestDone.current?.();
    });
    return () => { live = false; };
  }, [controller, request]);
  const sourceOpened = useRef(onSourceOpened);
  sourceOpened.current = onSourceOpened;
  useEffect(() => {
    if (!initialSource) return;
    let live = true;
    // Open after the workspace mounts: its cleanup closes source grants and would
    // otherwise cancel a navigation started by the parent during StrictMode setup.
    void controller.load().then(async () => {
      if (!live) return;
      await controller.open(initialSource.source_id, initialSource.revision);
      if (live) sourceOpened.current?.();
    });
    return () => { live = false; };
  }, [controller, initialSource]);
  const active = view.reading || view.cleanupPending || view.jobs.some((j) => ["running", "queued", "waiting_online"].includes(j.state));
  useEffect(() => {
    if (!active && !view.source?.private) return;
    const timer = setInterval(() => { void controller.refreshOpened(); }, 2000);
    return () => clearInterval(timer);
  }, [controller, active, view.source?.private]);
  const card = view.deck?.cards.find((c) => !c.suspended);
  useEffect(() => setRevealed(false), [card?.card_id]);
  // ASM-28 in a quiz: a first wrong answer never shows the answer. Help on a partially readable PDF asks for the
  // extraction review first (StudyQuiz holds the rest of the quiz's state).
  const help = (kind: StudyHelpKind, itemId: string) => {
    if (view.source?.state === "partial" && !readableConfirmed) setHelpReview({ itemId, kind });
    else void controller.requestQuizHelp(itemId, kind, readableConfirmed);
  };
  useEffect(() => setHelpReview(null), [view.quiz?.quiz_id, view.source?.revision]);
  useEffect(() => { setCitation(null); setDeleting(false); setCorrecting(false); }, [view.source?.source_id]);
  useEffect(() => {
    if (citation) {
      passage.current?.focus({ preventScroll: true });
      passage.current?.querySelector("mark")?.scrollIntoView?.({ block: "center" });
    }
  }, [citation]);
  const cite = (c: W.Citation) => {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    void controller.readCitation(c).then((ok) => { if (ok) {
      setCitation(c); setSourceOpen(true);
      const material = view.source?.source_id === c.source_id ? view.source : view.sources.find((s) => s.source_id === c.source_id);
      const locator = locations.current.values.get(citationKey(c)) ?? controller.getView().chunks.find((chunk) => citationKey(chunk) === citationKey(c))?.locator;
      if (material && "kind" in material && material.kind === "pdf" && locator?.kind === "page") showPage(locator.page, c.revision, c.source_id);
    } });
  };
  // Following a reference may replace view.chunks with another material. Retain only location metadata for this
  // open workspace; clear it on material/revision/controller changes, including a return to the library.
  const locations = useRef({ controller, scope: reviewKey, values: new Map<string, W.Locator>() });
  if (locations.current.controller !== controller || locations.current.scope !== reviewKey) locations.current = { controller, scope: reviewKey, values: new Map() };
  for (const chunk of view.chunks) locations.current.values.set(citationKey(chunk), chunk.locator);
  const sourceTitle = (ref: W.Citation) => {
    const source = view.source?.source_id === ref.source_id ? view.source : view.sources.find((source) => source.source_id === ref.source_id);
    return source && "title" in source ? source.title : source?.private ? t("study.private_source") : undefined;
  };
  const claim = (c: W.Claim, key?: string) => <div key={key} className="flex flex-col items-start gap-2">
    <PackText text={c.text} />
    <StudyCitations citations={c.citations} locate={(ref) => locations.current.values.get(citationKey(ref))} title={sourceTitle} disabled={view.busy} onOpen={cite} />
  </div>;
  // A revision is worth naming only once the material has been replaced: "revision 1" on a first upload is noise.
  const revisionTag = (revision: number) => revision > 1 ? ` · ${t("study.revision", { revision })}` : "";
  const errorText = (code: string) => t(`study.error.${code}`) === `study.error.${code}` ? t("study.error.generic") : t(`study.error.${code}`);
  const accessBlocked = (view.access && view.access.status !== "ready") || view.error === "consent_required";
  // Explore (the crowding review of 2026-11-01): one topic, then research with sources or a concept map of the topic
  // (the pack's curated skills, or AI-generated with no source). Each result says where it came from.
  const topicMap = view.conceptTopic ?? null;
  const exploreTopic = topicText.trim();
  const explore = <section className="flex flex-col gap-4" aria-labelledby="study-explore" data-explore>
    <div className="flex flex-col gap-1"><h2 id="study-explore" className="text-ui font-semibold text-ink">{t("study.explore.heading")}</h2><p className="text-meta text-muted">{t("study.explore.help")}</p></div>
    <form className="flex flex-col gap-3" onSubmit={(e) => { e.preventDefault(); if (exploreTopic.length < 3 || accessBlocked || view.busy) return; setResearchStart({ topic: exploreTopic, seq: ++researchSeq.current }); }}>
      <label className="flex flex-col gap-1 text-ui text-ink">{t("study.research.topic_label")}<input ref={exploreField} className={FIELD_LOOK} value={topicText} maxLength={200} disabled={view.busy} onChange={(e) => setTopicText(e.target.value)} /></label>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" tone="primary" disabled={view.busy || Boolean(accessBlocked) || exploreTopic.length < 3}>{t("study.explore.research")}</Button>
        <Button disabled={view.busy || exploreTopic.length === 0} onClick={() => { void controller.makeTopicMap(exploreTopic, preferCloud).then(() => { if (!controller.getView().error) setTopicText(""); }); }}>{t("study.explore.map")}</Button>
      </div>
      <p className="text-meta text-muted">{t("study.explore.provenance")}</p>
    </form>
    <StudyResearch controller={controller} hideStart start={researchStart} onStartUsed={() => setResearchStart(null)} initialCapture={initialResearch ?? null} onCaptureUsed={onResearchOpened} disabled={Boolean(accessBlocked)} onOpenSource={(id) => { setLibraryTab("materials"); void controller.load().then(() => controller.open(id)); }} />
    {(view.conceptTopics ?? []).length > 0 && <div className="flex flex-col gap-2" aria-label={t("study.concept.topic_saved")}><p className="text-meta text-muted">{t("study.concept.topic_saved")}</p><div className="flex flex-wrap gap-2">{(view.conceptTopics ?? []).slice(0, 8).map((m) => <Button key={m.map_id} size="sm" tone="ghost" aria-pressed={topicMap?.map_id === m.map_id} disabled={view.busy} onClick={() => { void controller.openTopicMap(m.map_id); }}>{m.topic}</Button>)}</div></div>}
    {topicMap !== null && <StudyConceptMap map={topicMap.concept_map} claim={(c) => claim(c)} busy={view.busy} onEdit={(ops) => { void controller.editTopicMap(ops); }}
      heading={<div className="flex flex-wrap items-center justify-between gap-2" data-topic-map={topicMap.map_id} data-origin={topicMap.origin}>
        <div className="flex flex-col"><h3 className="text-heading text-ink">{topicMap.topic}</h3><p className="text-meta text-muted">{t(`study.concept.topic_origin.${topicMap.origin}`)}</p></div>
        <span className="flex gap-2">{openTopic && topicMap.node_id && <Button size="sm" onClick={() => { if (topicMap.node_id) openTopic(topicMap.node_id); }}>{t("study.topic_guide")}</Button>}<Button size="sm" tone="ghost" onClick={() => { void controller.openTopicMap(null); }}>{t("action.close")}</Button></span>
      </div>} />}
  </section>;
  const ready = !accessBlocked && (view.source?.state === "ready" || (view.source?.state === "partial" && readableConfirmed));

  const status = <>
    {view.source && !view.source.private && <SponsoredPlace key={reviewKey} controller={controller} source={view.source.source_id} revision={view.source.revision} />}
    {view.cleanupPending && <Notice message={t("study.cleanup_pending")} />}
    {view.status === "loading" && <Loader label={t("study.loading")} />}
    {view.status === "failed" && <ErrorState message={t("study.load_failed")} action={{ label: t("action.try_again"), onClick: () => { void controller.load(); } }} />}
    {accessBlocked && view.source?.state !== "queued" && <Notice message={t(view.access?.needsGuardian ? "study.access.guardian" : view.access?.status === "plan_required" ? "study.access.plan" : view.access?.status === "study_off" ? "study.access.off" : "study.access.consent")}
      {...(onReviewAccess ? { action: { label: t("study.access.review"), onClick: onReviewAccess } } : {})} />}
    {view.error && view.error !== "consent_required" && <Notice message={view.safetyTemplate ? t(`study.safety.${view.safetyTemplate}`) : errorText(view.error)} />}
    {view.error === "graded_work" && <span><Button onClick={() => { setChatPrompt(t("study.guided_help")); setTab("chat"); ask.current?.focus(); }}>{t("study.guided_help")}</Button></span>}
    {view.busy && <p role="status" className="text-ui text-muted">{view.uploadTotal > 0 && view.uploadBytes < view.uploadTotal ? t("study.uploading", { done: view.uploadBytes, total: view.uploadTotal }) : t("study.working")}</p>}
  </>;

  const jobs = (list: typeof view.jobs, compact = false) => list.length > 0 && <section className="flex flex-col gap-2" aria-label={t("study.jobs")}>
    {list.filter((job) => !(job.kind === "extract" && job.state === "failed" && view.jobs.some((done) => done.kind === "extract" && done.state === "completed" && done.updated_at >= job.updated_at && JSON.stringify(done.sources) === JSON.stringify(job.sources)))).map((job) => <div key={job.job_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-control bg-chrome px-3 py-2">
      <p className="text-ui font-medium text-ink">{t(`study.generate.${job.kind}`)}</p>
      <p role="status" className="text-meta text-muted">{t(`study.job.${job.state}`)}{!compact && job.total_units !== null ? ` · ${job.completed_units}/${job.total_units}` : ""}{compact ? ` · ${t("study.set_created", { when: new Date(job.updated_at) })}` : ""}</p>
      <span className="flex-1" />
      {job.state === "completed" && view.aids.some((aid) => aid.job_id === job.job_id) && <Button size="sm" onClick={() => { const aid = view.aids.find((a) => a.job_id === job.job_id); if (aid) { setActivityOpen(false); void controller.showAid(aid.aid_id); } }}>{t("study.open_result")}</Button>}
      {["running", "queued", "waiting_online"].includes(job.state) && job.kind !== "extract" && <p className="w-full text-meta text-muted">{t("study.generating_help")}</p>}
      {job.error && !(job.error === "consent_required" && accessBlocked) && (compact ? <details className="w-full text-meta text-muted"><summary className="cursor-pointer">{t("study.activity_details")}</summary><p className="py-2">{errorText(job.error)}</p></details> : <p className="w-full text-meta text-muted">{errorText(job.error)}</p>)}
      {job.notice === "allowance_exhausted" && <Notice message={t("study.allowance_fallback")} />}
      {job.notice === "sampled_long_source" && <Notice message={t("study.sampled_long_source")} />}
      {["queued", "running", "waiting_online", "paused"].includes(job.state) && <Button size="sm" tone="ghost" disabled={view.busy} onClick={() => { void controller.jobAction(job.job_id, "cancel"); }}>{t("action.cancel")}</Button>}
      {["failed", "paused", "waiting_online"].includes(job.state) && <Button size="sm" disabled={view.busy || !!accessBlocked} onClick={() => { void controller.jobAction(job.job_id, job.state === "failed" ? "retry" : "resume"); }}>{t("action.try_again")}</Button>}
    </div>)}
  </section>;

  const openedSource = view.source;
  const materialFamily = openedSource ? materialGroups([...view.sources.filter((s) => s.source_id !== openedSource.source_id), openedSource], view.sets).find((group) => group.some((s) => s.source_id === openedSource.source_id)) ?? [openedSource] : [];
  const familyIds = new Set(materialFamily.map((s) => s.source_id));
  const activityJobs = view.source ? view.jobs.filter((j) => j.sources.some((ref) => familyIds.has(ref.source_id))) : view.jobs;
  const activityButton = activityJobs.length > 0 && <Button size="sm" tone="ghost" onClick={() => { setActivityLimit(8); setActivityOpen(true); }}><Icon name="timer" size={16} />{t("study.activity")}</Button>;
  const activityDialog = <Dialog open={activityOpen} heading={t("study.activity")} closeLabel={t("action.close")} onClose={() => setActivityOpen(false)} wide>
    <p className="text-meta text-muted">{t("study.activity_help")}</p>
    {jobs(activityJobs.slice(0, activityLimit), true)}
    {activityJobs.length > activityLimit && <Button size="sm" tone="ghost" onClick={() => setActivityLimit((n) => n + 8)}>{t("study.library_show_more", { count: activityJobs.length - activityLimit })}</Button>}
  </Dialog>;

  // --- the library -------------------------------------------------------------------------------------------------
  if (!view.source) {
    // Secondary navigation waits in one menu: the activity log and every study set across materials.
    const more = [
      ...(activityJobs.length > 0 ? [{ id: "activity", icon: "timer" as const, label: t("study.activity"), onSelect: () => { setActivityLimit(8); setActivityOpen(true); } }] : []),
      ...(view.sets.length > 0 ? [{ id: "sets", icon: "learn" as const, label: t("study.all_sets"), onSelect: () => setAllSetsOpen(true) }] : []),
    ];
    return <><section className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-6 py-10" aria-labelledby="study-heading">
    <header className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3"><h1 id="study-heading" className="text-title text-ink">{t("study.title")}</h1>
        {libraryTab === "materials" && <span className="flex items-center gap-1">
          <Button size="sm" tone="primary" aria-expanded={adding || pasting || view.sources.length === 0} onClick={() => { setAdding((v) => !v); setPasting(false); }}>{t("study.library_add")}</Button>
          {more.length > 0 && <MenuButton label={t("study.library_more")} items={more} trigger={(props) => <IconButton icon="more" size="sm" label={t("study.library_more")} {...props} />} />}
        </span>}
      </div>
    </header>
    <div role="tablist" aria-label={t("study.library.navigation")} className="flex gap-1 border-b border-line pb-2">{(["materials", ...(companion ? ["companion"] : []), "explore"] as const).map((name) => <Button key={name} role="tab" size="sm" tone="ghost" aria-selected={libraryTab === name} onClick={() => setLibraryTab(name as typeof libraryTab)}>{t(name === "companion" ? "study.tab.companion" : `study.library.${name}`)}</Button>)}</div>
    {libraryTab === "companion" && companion?.("panel")}
    {status}
    {libraryTab === "materials" && <>
    <div ref={addPanel} hidden={!adding && !pasting && view.sources.length > 0} className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-5">
      {/* A drop zone over the native picker, which stays in the tab order (focus rings the whole zone). */}
      <label data-study-drop={dragging ? "over" : ""} onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragging(true); } }} onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); const file = e.dataTransfer.files[0]; if (file && !view.busy) void controller.upload(file, undefined, preferCloud); }}
        className={`flex cursor-pointer flex-col items-center gap-2 rounded-card border border-dashed px-6 py-8 text-center has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-focus ${dragging ? "border-focus bg-panel" : "border-line-strong hover:bg-wash"}`}>
        <span aria-hidden="true" className="flex size-10 items-center justify-center rounded-pill bg-panel text-muted"><Icon name="sending" /></span>
        <span className="text-ui font-medium text-ink">{t("study.add_file")}</span>
        <span className="text-meta text-muted">{t("study.drop_hint")}</span>
        <input type="file" accept={ACCEPT} disabled={view.busy} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void controller.upload(file, undefined, preferCloud); }} className="sr-only" />
      </label>
      <p className="text-meta text-muted">{t("study.file_help")}</p>
      <span className="flex flex-wrap gap-2"><Button size="sm" tone="ghost" aria-expanded={pasting} onClick={() => setPasting((open) => !open)}><Icon name="notes" size={16} />{t("study.paste")}</Button><Button size="sm" tone="ghost" aria-expanded={linking} onClick={() => setLinking((open) => !open)}><Icon name="online" size={16} />{t("study.add_link")}</Button></span>
      {linking && <StudyLinkAdd busy={view.busy} error={view.error} onAdd={async (url, transcript, title) => { const added = await controller.addLink(url, transcript, title); return added ? (added.transcript_kind ?? "plain") : null; }} />}
      {pasting && <form className="flex flex-col items-start gap-3" onSubmit={(e) => { e.preventDefault(); void controller.addText(title.trim(), text).then(() => { if (!controller.getView().error) { setTitle(""); setText(""); } }); }}>
          <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.source_title")}<input required maxLength={200} value={title} disabled={view.busy} onChange={(e) => setTitle(e.target.value)} className={FIELD_LOOK} /></label>
          <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.paste_label")}<textarea required maxLength={200000} rows={6} disabled={view.busy} value={text} onChange={(e) => setText(e.target.value)} className={FIELD_LOOK} /></label>
          <Button type="submit" disabled={view.busy || !title.trim() || !text.trim()}>{t("study.add_text")}</Button>
        </form>}
      <section className="flex flex-col items-start gap-3" aria-label={t("study.saved_notes")}>
        <Button size="sm" tone="ghost" disabled={view.busy} onClick={() => { void controller.loadNotes(); }}><Icon name="notes" size={16} />{t("study.choose_note")}</Button>
        {view.notes.map((note) => <div key={note.id} className="flex flex-col items-start gap-2"><Button size="sm" disabled={view.busy} onClick={() => { if (note.private) setNoteConfirm(note.id); else void controller.importNote(note, false); }}>{note.title ?? t("study.private_note")}</Button>{noteConfirm === note.id && <><p className="text-ui">{t("study.note_confirm")}</p><div className="flex gap-2"><Button size="sm" disabled={view.busy} onClick={() => { setNoteConfirm(null); void controller.importNote(note, true); }}>{t("study.import_note")}</Button><Button size="sm" tone="ghost" onClick={() => setNoteConfirm(null)}>{t("action.cancel")}</Button></div></>}</div>)}
      </section>
    </div>
    <StudyLibrary sources={view.sources} sets={view.sets} archivedIds={view.archivedSourceIds ?? []} busy={view.busy}
      onOpen={(source) => { if (source.private) setConfirm(source.source_id); else void controller.open(source.source_id); }}
      onArchive={(ids, archived) => { void controller.archiveSources(ids, archived); }} onRename={(ids, name) => { void controller.renameSources(ids, name); }} />
    <Dialog open={confirm !== null} heading={t("study.private_source")} closeLabel={t("action.cancel")} onClose={() => setConfirm(null)}><p className="text-ui">{t("study.private_confirm")}</p><Button onClick={() => { const id = confirm; setConfirm(null); if (id) void controller.open(id); }}>{t("study.open_private")}</Button></Dialog>
    </>}
    {libraryTab === "explore" && explore}
    {jobs(view.jobs.filter((job) => ["running", "queued", "waiting_online", "paused"].includes(job.state)))}
  </section>{activityDialog}<Dialog open={allSetsOpen} heading={t("study.all_sets")} closeLabel={t("action.close")} onClose={() => setAllSetsOpen(false)} wide><MySets sets={view.sets} busy={view.busy} heading={false} onOpen={(set) => { setAllSetsOpen(false); void controller.openSet(set); }} /></Dialog></>;
  }

  // --- an opened material: the material beside its study tools ----------------------------------------------------
  const source = view.source;
  const savedJourney = view.journey?.sources.some((s) => s.source_id === source.source_id && s.revision === source.revision) ? view.journey : null;
  const teach = () => { void controller.generate("chat", 10, readableConfirmed, preferCloud, t("study.journey.teach_prompt"), undefined, undefined, "lesson"); };
  // The conversation's history is what the learner asked; coaching made for a quiz question stays with that question.
  const chats = view.aids.filter((aid) => aid.kind === "chat" && !isQuizHelpPrompt(aid.prompt));
  const madeBy = (by: NonNullable<W.Aid["made_by"]>) => { const key = modelNameKey(by.model); const model = key ? t(key) : by.model; return model ? t(`study.made_by.${by.runtime}`, { model }) : t(`study.made_by.${by.runtime}_unnamed`); };
  const helpAids = view.aids.filter((aid) => aid.kind === "chat" && isQuizHelpPrompt(aid.prompt));
  const shown = view.aid !== null && view.aid !== undefined && tabOf(view.aid.kind) === tab ? view.aid : null;
  const sourceAliases = new Map(materialFamily.map((s) => [s.source_id, source.source_id]));
  const setsHere = view.sets.filter((set) => set.kind !== "chapters" && set.sources.some((s) => familyIds.has(s.source_id)));
  const chapters = view.aids.find((aid) => aid.kind === "chapters" && aid.status === "ready")?.chapters ?? null;
  const syllabi = view.sources.flatMap((s) => s.source_id !== source.source_id && "title" in s && s.state === "ready" ? [{ source_id: s.source_id, revision: s.revision, title: s.title }] : []);
  const sourceJobs = view.jobs.filter((j) => j.sources.some((s) => s.source_id === source.source_id));
  const currentJobs = sourceJobs.filter((j) => j.job_id !== view.quizHelp?.jobId && (["running", "queued", "waiting_online"].includes(j.state) || (!shown && j.job_id === view.requestedJobId && ["failed", "paused"].includes(j.state))));
  const helpJob = view.jobs.find((j) => j.job_id === view.quizHelp?.jobId);
  const helpPending = !!helpJob && ["queued", "running", "waiting_online", "paused"].includes(helpJob.state);
  const safeHelp = helpJob?.state === "failed" && helpJob.error === "insufficient_evidence" && !!view.quiz?.items.some((item) => item.item_id === view.quizHelp?.itemId);
  const pendingKinds = currentJobs.filter((j) => ["running", "queued", "waiting_online", "paused"].includes(j.state)).map((j) => j.kind);
  const canConfigure = !accessBlocked && ["ready", "partial"].includes(source.state);
  const pick = (next: GenerateTool, contextAidId?: string) => { setToolContext(contextAidId ?? null); setTool(next); if (!contextAidId && view.materialTopics.length === 0) void controller.loadMaterialTopics(); };
  const generate = (chosen: GenerateTool, request: GenerateRequest, force = false) => {
    const selections = ["summary", "notes", "lesson_plan"].includes(chosen) ? view.selections : [{ source_id: source.source_id, revision: source.revision }];
    const normalise = (settings: W.GenerateSettings = {}) => JSON.stringify(Object.entries({ ...defaultRequest(chosen).settings, ...settings }).map(([key, value]) => [key, Array.isArray(value) ? [...value].sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))) : value]).sort(([a], [b]) => String(a).localeCompare(String(b))));
    const sameSources = (refs: W.SourceSelection[]) => JSON.stringify(refs.map((s) => `${s.source_id}:${s.revision}`).sort());
    const existing = view.aids.find((a) => {
      const progress = view.sets.find((s) => s.aid_id === a.aid_id)?.progress;
      const count = progress && ("questions_total" in progress ? progress.questions_total : progress.cards_total);
      return a.kind === chosen && a.status === "ready" && (chosen !== "quiz" || !!a.settings?.question_types) && sameSources(a.sources) === sameSources(selections) && normalise(a.settings) === normalise(request.settings) && (!["quiz", "flashcards"].includes(chosen) || count === request.count);
    });
    if (!force && existing && !toolContext) { setTool(null); setDuplicate({ tool: chosen, request, aid: existing }); return; }
    if (source.state === "partial" && !readableConfirmed) { setDuplicate(null); pick(chosen, toolContext ?? undefined); return; }
    const work = toolContext ? controller.generate(chosen, request.count, readableConfirmed, preferCloud, undefined, request.settings, toolContext) : controller.generate(chosen, request.count, readableConfirmed, preferCloud, undefined, request.settings);
    void work.then(() => { if (!controller.getView().error) { setTool(null); setToolContext(null); setDuplicate(null); } });
  };
  const quickGenerate = (chosen: GenerateTool) => generate(chosen, defaultRequest(chosen));
  // Organise existing verified claims by source page/time, without inventing topic headings or changing evidence.
  const summaryGroups = shown?.kind === "summary" || shown?.kind === "notes" ? shown.claims.reduce<{ label: string; claims: W.Claim[] }[]>((groups, c) => {
    const ref = c.citations[0], chunk = view.chunks.find((x) => x.chunk_id === ref?.chunk_id);
    const label = chunk?.locator.kind === "page" ? t("study.source_page", { page: chunk.locator.page }) : chunk?.locator.kind === "time" ? `${Math.floor(chunk.locator.start_ms / 60000)}:${String(Math.floor(chunk.locator.start_ms / 1000) % 60).padStart(2, "0")}` : t("study.key_points");
    const previous = groups.at(-1);
    if (previous?.label === label) previous.claims.push(c); else groups.push({ label, claims: [c] });
    return groups;
  }, []) : null;
  const askMaterial = () => { const work = chatContext ? controller.generate("chat", 10, readableConfirmed, preferCloud, chatPrompt, undefined, chatContext) : controller.generate("chat", 10, readableConfirmed, preferCloud, chatPrompt);
    void work.then(() => { if (!controller.getView().error) setChatPrompt(""); }); };
  const goAsk = () => { setTab("chat"); requestAnimationFrame(() => ask.current?.focus()); };

  // The split needs the screen's own width (a container query), not the window's: the sidebar and the board take room.
  // The settings dialog sits outside the container query: a size container would trap its fixed overlay.
  return <><section className="@container flex h-full min-h-0 w-full flex-col" aria-labelledby="study-heading">
      <header className="flex min-h-14 shrink-0 items-center gap-2 px-4">
        <IconButton icon="go-back" size="sm" label={t("study.back")} onClick={() => controller.close()} />
        <div className="flex min-w-0 flex-col">
          <h1 id="study-heading" className="truncate text-ui font-semibold text-ink">{source.title}</h1>
          <p className="text-meta text-muted">{source.revision > 1 && <>{t("study.revision", { revision: source.revision })} · </>}{t(source.state === "queued" && accessBlocked ? "study.source.saved" : source.state === "partial" && source.unreadable_units.length === 0 ? "study.source.review" : `study.source.${source.state}`)}</p>
        </div>
        <span className="flex-1" />
        <Button size="sm" tone="ghost" aria-expanded={sourceOpen} onClick={() => setSourceOpen((open) => !open)}>{t(sourceOpen ? "study.hide_source" : "study.show_source")}</Button>
        {view.availableRevisions.length > 1 && <label className="flex items-center gap-2 text-meta text-muted">{t("study.choose_revision")}<select className={FIELD_LOOK} value={source.revision} onChange={(e) => { void controller.open(source.source_id, Number(e.target.value)); }}>{view.availableRevisions.map((rev) => <option key={rev} value={rev}>{rev}</option>)}</select></label>}
      </header>
    <div className="flex min-h-0 flex-1 overflow-hidden">
    {/* Source pages are opened deliberately, keeping the answer in focus. */}
    <div className={`${sourceOpen ? "flex" : "hidden"} min-h-0 min-w-0 flex-1 flex-col @4xl:max-w-[48%]`}>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pb-6">
        {source.state !== "ready" && <Notice message={source.state === "queued" && accessBlocked ? t(view.access?.needsGuardian ? "study.saved_pending_guardian" : "study.saved_pending_access") : source.state === "partial" ? source.unreadable_units.length ? t("study.partial", { units: source.unreadable_units.join(", ") }) : t("study.review_extracted") : t(`study.source.${source.state}`)}
          {...(source.state === "queued" && accessBlocked && onReviewAccess ? { action: { label: t("study.access.review"), onClick: onReviewAccess } } : {})} />}
        {(source.kind === "recording" || source.kind === "video") && <Notice message={t("study.recording_help")} />}
        {source.state === "queued" && <span><Button tone="primary" disabled={view.busy || !!accessBlocked} onClick={() => { void controller.startExtraction(preferCloud); }}>{t("study.start_extraction")}</Button></span>}
        {(source.kind === "recording" || source.kind === "video") && source.state === "partial" && <label className="flex min-h-11 items-center gap-2 text-ui text-ink"><input type="checkbox" checked={readableConfirmed} onChange={(e) => review(e.target.checked)} />{t("study.confirm_readable")}</label>}
        {source.state === "uploading" && <label className="flex flex-col gap-2 text-ui text-ink">{t("study.resume_upload")}<input type="file" accept={ACCEPT} disabled={view.busy} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void controller.upload(file, source, preferCloud); }} /></label>}
        <span><Button size="sm" tone="ghost" aria-expanded={managing} onClick={() => setManaging((open) => !open)}>{t("study.manage")}</Button></span>
        {managing && <div className="flex flex-col items-start gap-3 rounded-card bg-chrome p-4">
            {["pdf", "document", "slides", "photo", "recording", "video"].includes(source.kind) && <label className="flex flex-col gap-2 text-ui text-ink">{t("study.replace_file")}<input type="file" accept={ACCEPT} disabled={view.busy} onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void controller.upload(file, source, preferCloud, true); }} /></label>}
            <div className="flex flex-wrap gap-2">
              <Button size="sm" disabled={view.busy} onClick={() => { setCorrectedText(view.chunks.map((c) => c.text).join("\n\n")); setCorrecting(true); }}>{t("study.correct_text")}</Button>
              <Button size="sm" disabled={view.busy} onClick={() => { void controller.markPrivate(); }}>{t(source.private ? "study.make_public" : "study.make_private")}</Button>
              <Button size="sm" disabled={view.busy} onClick={() => { void controller.refreshOpened(); }}>{t("study.refresh")}</Button>
              <Button size="sm" tone="danger" disabled={view.busy} onClick={() => setDeleting(true)}>{t("study.delete")}</Button>
            </div>
        </div>}
        {correcting && <form className="flex flex-col items-start gap-3" onSubmit={(e) => { e.preventDefault(); void controller.correctText(correctedText).then(() => { if (!controller.getView().error) setCorrecting(false); }); }}><label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.correct_help")}<textarea rows={10} required maxLength={200000} className={FIELD_LOOK} value={correctedText} onChange={(e) => setCorrectedText(e.target.value)} /></label><div className="flex gap-2"><Button type="submit" tone="primary" disabled={view.busy || !correctedText.trim()}>{t("study.save_revision")}</Button><Button tone="ghost" onClick={() => setCorrecting(false)}>{t("action.cancel")}</Button></div></form>}
        {deleting && <div className="flex flex-col items-start gap-2"><p className="text-ui">{t("study.delete_confirm")}</p><div className="flex gap-2"><Button tone="danger" disabled={view.busy} onClick={() => { void controller.remove(); }}>{t("study.delete_yes")}</Button><Button tone="ghost" onClick={() => setDeleting(false)}>{t("action.cancel")}</Button></div></div>}
        <div ref={passage} tabIndex={-1} className="mx-auto flex w-full max-w-2xl flex-col gap-4 rounded-card border border-line bg-canvas p-4 outline-none" aria-label={t("study.source_passage")}>
          {citation && <span><Button size="sm" onClick={() => { setCitation(null); setSourceOpen(false); requestAnimationFrame(() => returnFocus.current?.focus()); }}>{t("study.back_to_aid")}</Button></span>}
          {source.kind !== "pdf" && pageImage && <figure ref={pageFigure} className="flex flex-col gap-2"><img className="h-auto w-full" src={pageImage.url} alt={t("study.page_image", { page: pageImage.page })} /><figcaption className="text-meta text-muted">{t("study.page_image", { page: pageImage.page })}</figcaption>{citation && <span><Button size="sm" tone="ghost" onClick={() => passage.current?.querySelector("mark")?.scrollIntoView?.({ block: "center" })}>{t("study.read_extracted_passage")}</Button></span>}</figure>}
          {source.kind === "pdf" && <StudyPdfReader chunks={view.chunks} page={sourcePage} image={pageImage} loading={pageLoading} failed={pageFailed} citation={citation} onPage={(chunk) => { if (chunk.locator.kind === "page") { setCitation(null); showPage(chunk.locator.page, chunk.revision, chunk.source_id); } }} />}
          {(source.kind === "recording" || source.kind === "video" || source.kind === "video_link") && view.chunks.length > 0 && <StudyPlayer source={source} chunks={view.chunks} chapters={chapters} playhead={view.playhead} busy={view.busy}
            loadMedia={() => controller.loadMedia()} onSeek={(ms) => controller.seek(ms)} onOpenMoment={(ms) => { controller.openMoment(ms); }} onCite={cite}
            onAskMoment={onAskMoment && companion ? (ms) => onAskMoment(source, ms) : undefined}
            canMakeChapters={ready} onMakeChapters={() => { void controller.generate("chapters", 10, readableConfirmed, preferCloud); }}
            canOutline={ready && view.chunks.some((c) => c.locator.kind === "time")} outlinePending={pendingKinds.includes("teaching_outline")}
            onMakeOutline={source.kind === "video_link" || source.kind === "video" ? () => { void controller.generate("teaching_outline", 10, readableConfirmed, preferCloud); } : undefined} />}
          {source.kind !== "pdf" && source.kind !== "recording" && source.kind !== "video" && source.kind !== "video_link" && view.chunks.map((chunk) => <article key={chunk.chunk_id} className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <p className="text-meta text-faint">{chunk.locator.kind === "time" ? t("study.timestamp", { start: `${Math.floor(chunk.locator.start_ms / 60000)}:${String(Math.floor(chunk.locator.start_ms / 1000) % 60).padStart(2, "0")}`, end: `${Math.floor(chunk.locator.end_ms / 60000)}:${String(Math.floor(chunk.locator.end_ms / 1000) % 60).padStart(2, "0")}` }) : t("study.location", { kind: "page" in chunk.locator ? "page" : "slide" in chunk.locator ? "slide" : "part", number: "page" in chunk.locator ? chunk.locator.page : "slide" in chunk.locator ? chunk.locator.slide : chunk.ordinal + 1 })}{revisionTag(chunk.revision)}</p>
              {"page" in chunk.locator && <Button size="sm" tone="ghost" disabled={view.busy} onClick={() => { if ("page" in chunk.locator) showPage(chunk.locator.page, chunk.revision, chunk.source_id); }}>{t("study.show_page", { page: chunk.locator.page })}</Button>}
            </div>
            {webSources.has(source.source_id) && !(citation && citationKey(citation) === citationKey(chunk)) ? <PackText text={chunk.text} /> : <p className="whitespace-pre-wrap break-words text-body text-ink">{citation && citationKey(citation) === citationKey(chunk) ? <><StudySourceText text={Array.from(chunk.text).slice(0, citation.start).join("")} /><mark className="bg-highlight-soft text-ink underline decoration-highlight-line"><StudySourceText text={Array.from(chunk.text).slice(citation.start, citation.end).join("")} /></mark><StudySourceText text={Array.from(chunk.text).slice(citation.end).join("")} /></> : <StudySourceText text={chunk.text} />}</p>}
          </article>)}
        </div>
      </div>
    </div>

    {/* The study tools */}
    <aside aria-label={t("study.aids")} className={`${sourceOpen ? "hidden @4xl:flex @4xl:border-s" : "flex"} min-h-0 min-w-0 flex-1 flex-col border-line bg-canvas`}>
      <div role="tablist" aria-label={t("study.aids")} className="flex min-h-14 shrink-0 items-center gap-1 overflow-x-auto border-b border-line px-3">
        {(companion ? TABS_WITH_COMPANION : TABS).map((name) => <Button key={name} role="tab" size="sm" tone="ghost" aria-selected={tab === name} onClick={() => setTab(name)}>{t(`study.tab.${name}`)}</Button>)}
      </div>
      <div role="tabpanel" aria-label={t(`study.tab.${tab}`)} className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4">
        {tab === "companion" ? companion?.("tab") : <>
        {savedJourney && <StudyJourney journey={savedJourney} busy={view.busy} onReviewDue={() => { void controller.refreshDueReview(); }} onContinue={() => { if (savedJourney.phase === "make_quiz" && shown?.aid_id === savedJourney.cards_aid_id) pick("quiz", shown.aid_id); else void controller.continueJourney(); }} onLesson={() => { void controller.continueJourney("lesson"); }} onResults={() => { void controller.continueJourney("quiz"); }} />}
        {status}
        {jobs(currentJobs)}
        {tab === "generate" && !shown && <>
          <GenerateTools disabled={view.busy || !canConfigure} pendingKinds={pendingKinds} onPick={quickGenerate} onConfigure={pick} />
          <MySets sets={setsHere} busy={view.busy} contextual sourceAliases={sourceAliases} onOpen={(set) => { void controller.openSet(set); }} />
          {curriculum && <Button size="sm" tone="ghost" disabled={view.busy || !["ready", "partial"].includes(source.state) || source.readable_units < 1 || !view.followCurrent} onClick={() => { void controller.loadTopics().then((ok) => setTopicsSearched(ok)); }}>{t("study.find_topics")}</Button>}
          {curriculum && topicsSearched && view.topics.length === 0 && <><p className="text-meta text-muted">{t("journey.unmapped")}</p><span><Button size="sm" disabled={view.busy || !canConfigure} onClick={() => source.state === "partial" && !readableConfirmed ? setTeachReview(true) : teach()}>{t("journey.teach_beginning")}</Button></span></>}
          {view.topics.length > 0 && <section className="flex flex-col gap-3" aria-label={t("study.suggested_topics")}><p className="text-meta text-muted">{t("study.topic_help")}</p>{view.topics.map((topic) => <div key={topic.node_id} className="flex flex-col gap-2"><p className="text-ui font-semibold">{topic.title}</p>{onJourneyStarted && topic.objectives && topic.objectives.length > 0 && <div className="flex flex-col items-start gap-2">
            <label className="flex w-full flex-col gap-2 text-ui">{t("journey.choose_objective")}<select className={FIELD_LOOK} value={journeyChoices[topic.node_id] ?? topic.objectives[0]?.kc_id} onChange={(e) => setJourneyChoices((all) => ({ ...all, [topic.node_id]: e.target.value }))}>{topic.objectives.map((objective) => <option key={objective.kc_id} value={objective.kc_id}>{objective.statement}</option>)}</select></label>
            <Button size="sm" disabled={view.busy} onClick={() => { const kc = journeyChoices[topic.node_id] ?? topic.objectives?.[0]?.kc_id; if (kc) void controller.beginJourney(topic.node_id, kc).then((ok) => { if (ok) onJourneyStarted(topic.node_id, topic.subject); }); }}>{t("journey.begin")}</Button>
          </div>}<div className="flex flex-wrap gap-2">{openTopic && <Button size="sm" onClick={() => openTopic(topic.node_id)}>{t("study.topic_guide")}</Button>}{onPractice && <Button size="sm" onClick={() => onPractice(topic.subject, topic.kc_ids)}>{t("study.topic_practice")}</Button>}{openLesson && topic.lesson_ids.map((lesson, i) => <Button size="sm" key={lesson} onClick={() => openLesson(lesson)}>{t("study.topic_lesson", { number: i + 1 })}</Button>)}</div></div>)}</section>}
          <fieldset hidden={view.sources.length < 2} className="flex flex-col gap-2"><legend className="text-ui font-semibold">{t("study.extra_sources")}</legend><p className="text-meta text-muted">{t("study.extra_sources_help")}</p>{view.sources.filter((s) => s.source_id !== source.source_id && (!("state" in s) || s.state === "ready")).map((s) => <div key={s.source_id} className="flex flex-col gap-2"><label className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={view.selections.some((x) => x.source_id === s.source_id)} disabled={view.busy || (view.selections.length >= 8 && !view.selections.some((x) => x.source_id === s.source_id))} onChange={() => { if (s.private && !view.selections.some((x) => x.source_id === s.source_id)) setExtraConfirm(s.source_id); else void controller.selectExtra(s.source_id); }} />{"title" in s ? s.title : t("study.private_source")}</label>{extraConfirm === s.source_id && <><p className="text-ui">{t("study.private_confirm")}</p><div className="flex gap-2"><Button size="sm" onClick={() => { setExtraConfirm(null); void controller.selectExtra(s.source_id); }}>{t("study.open_private")}</Button><Button size="sm" tone="ghost" onClick={() => setExtraConfirm(null)}>{t("action.cancel")}</Button></div></>}</div>)}</fieldset>
        </>}
        {/* Ask this material: the review once per revision, the history on demand, then the conversation. */}
        {tab === "chat" && source.state === "partial" && <label className="flex min-h-11 items-center gap-2 text-ui text-ink"><input type="checkbox" checked={readableConfirmed} onChange={(e) => review(e.target.checked)} />{t("study.confirm_readable")}</label>}
        {tab === "chat" && <div className="flex flex-wrap items-center gap-2">
          <details className="text-meta text-muted"><summary className="cursor-pointer">{t("study.about_sources")}</summary><p className="mt-2">{t("study.grounded_notice")}</p></details>
          <span className="flex-1" />
          {chats.length > 0 && <Button size="sm" tone="ghost" aria-expanded={historyOpen} aria-controls="study-chat-history" onClick={() => setHistoryOpen((open) => !open)}><Icon name="timer" size={16} />{t("study.history", { count: chats.length })}</Button>}
        </div>}
        {tab === "chat" && chats.length > 0 && <ul id="study-chat-history" hidden={!historyOpen} className="flex flex-col divide-y divide-line rounded-card border border-line" aria-label={t("study.previous_questions")}>{chats.map((aid) => <li key={aid.aid_id} className="[&>button]:w-full [&>button]:justify-start [&>button]:text-start"><Button size="sm" tone="ghost" aria-pressed={view.aid?.aid_id === aid.aid_id} disabled={view.busy} onClick={() => { setHistoryOpen(false); void controller.showAid(aid.aid_id); }}><span className="truncate">{aid.prompt ?? aid.title}</span>{aid.status === "stale" ? <span className="text-meta text-muted"> · {t("study.stale")}</span> : null}</Button></li>)}</ul>}
        {tab === "chat" && !shown && <p className="text-meta text-muted">{t("study.ask_intro")}</p>}
        {shown && <article ref={resultPanel} tabIndex={-1} aria-label={`${t(`study.tool.${shown.kind}`)} · ${shown.title}`} className="flex flex-col gap-4 outline-none">
          {tab === "generate" && <span><Button size="sm" tone="ghost" onClick={() => controller.hideAid()}><Icon name="go-back" size={16} />{t("study.all_tools")}</Button></span>}
          {tab === "generate" && <h2 className="text-ui font-semibold text-ink">{t(`study.tool.${shown.kind}`)} · {shown.title}</h2>}
          {view.deck && <p role="status" className="text-meta text-muted">{t("study.cards_in_review", { count: view.deck.cards.filter((c) => !c.suspended).length })}</p>}
          {view.quiz && <p role="status" className="text-meta text-muted">{t("study.quiz_questions", { count: view.quiz.items.length })}</p>}
          {shown.prompt && <p data-said="student" className="self-end whitespace-pre-wrap rounded-card border border-line px-4 py-3 text-ui text-ink">{shown.prompt}</p>}
          {/* Which model made it and where, from the record (SPEC §22): never a guess by the client. */}
          {shown.made_by && <p className="text-meta text-muted" data-aid-made-by={shown.made_by.runtime}>{madeBy(shown.made_by)}</p>}
          {shown.kind === "audio_overview" && <><div className="flex flex-wrap gap-2">{view.reading ? <>
            {view.podcastVoice?.enabled && <Button tone="primary" onClick={() => view.audioPaused ? controller.resumeOverview() : controller.pauseOverview()}>{t(view.audioPaused ? "study.resume_audio" : "study.pause_audio")}</Button>}
            <Button onClick={() => controller.stopReading()}>{t("study.stop_audio")}</Button>
          </> : <Button tone="primary" disabled={view.busy || !view.canSpeak || shown.status !== "ready"} onClick={() => { void controller.playOverview(); }}>{t("study.play_audio")}</Button>}</div>
            {!view.canSpeak && <Notice message={t(view.podcastVoice?.enabled ? view.podcastVoice.configured ? "study.neural_audio_unsupported" : "study.neural_voice_unavailable" : "study.voice_unavailable")} />}
            <p role="status" className="text-meta text-muted">{view.reading ? view.audioPaused ? t("study.audio_paused", { number: view.spokenClaim + 1, total: shown.claims.length }) : view.audioPreparing ? t("study.preparing_neural") : t("study.audio_progress", { number: view.spokenClaim + 1, total: shown.claims.length }) : view.audioCompleted ? t("study.audio_complete", { total: shown.claims.length }) : null}</p>
            {(view.reading || view.audioCompleted) && <ProgressBar bare label={t("study.tool.audio_overview")} max={shown.claims.length} value={view.audioCompleted ? shown.claims.length : view.spokenClaim} valueText={`${view.audioCompleted ? shown.claims.length : view.spokenClaim} / ${shown.claims.length}`} />}
            <p className="text-meta text-muted">{t(view.podcastVoice?.enabled ? "study.audio_online" : "study.audio_local")}</p>
          </>}
          {shown.status === "stale" && <Notice message={t("study.stale_help")} />}
          {shown.kind === "teaching_outline" && shown.outline && <StudyOutline outline={shown.outline} ownCourse={!curriculum} sourceTitle={shown.title} chunks={view.chunks} busy={view.busy} onOpenMoment={(ms) => { controller.openMoment(ms); }} onCite={cite} onSkill={onSkill} skillName={skillName} />}
          {shown.kind === "concept_map" && shown.concept_map && <StudyConceptMap map={shown.concept_map} fromWeb={shown.sources.some((x) => webSources.has(x.source_id))} claim={(c) => claim(c)} busy={view.busy} onEdit={(ops) => { void controller.editConceptMap(ops); }}
            actions={ready ? { onAsk: (name) => { setTab("chat"); void controller.conceptAction(name, "ask", preferCloud); }, onFlashcards: (name) => { void controller.conceptAction(name, "flashcards", preferCloud); }, onQuiz: (name) => { void controller.conceptAction(name, "quiz", preferCloud); } } : {}} />}
          {summaryGroups ? summaryGroups.map((group, i) => <section key={i} className="flex flex-col gap-3"><h3 className="text-ui font-semibold text-ink">{group.label}</h3>{shown.settings?.focus?.startsWith("Format as concise revision bullet points.") ? <ul className="flex list-disc flex-col gap-3 ps-5">{group.claims.map((c, j) => <li key={j}>{claim(c)}</li>)}</ul> : group.claims.map((c, j) => claim(c, String(j)))}</section>) : !view.deck && !view.quiz && shown.kind !== "concept_map" && shown.kind !== "teaching_outline" && shown.claims.map((c, i) => claim(c, String(i)))}
          {shown.kind === "chat" && shown.status === "ready" && savedJourney?.lesson_aid_id === shown.aid_id && savedJourney.phase === "review" && <span><Button tone="primary" disabled={view.busy} onClick={() => { void controller.saveJourney("lesson_reviewed"); }}>{t("study.journey.reviewed_lesson")}</Button></span>}
          {/* After a chat answer, at most two next steps (UXS-68): cards from it, or a step explained again. */}
          {shown.kind === "chat" && shown.status === "ready" && <FollowUpChips disabled={view.busy || !ready}
            chips={followUps(["flashcards", "explain_step"], (kind) => t(`followup.${kind}`))}
            onChoose={(chip) => {
              if (chip.kind === "flashcards") { setTab("generate"); void controller.generate("flashcards", 10, readableConfirmed, preferCloud, undefined, undefined, shown.aid_id); }
              else { setChatContext(shown.aid_id); setChatPrompt(t("followup.ask.explain_step")); ask.current?.focus(); }
            }} />}
          {view.deck && (!card ? <EmptyState message={t("study.cards_done")} /> : <section className="flex flex-col gap-4 rounded-card border border-line p-5" aria-label={t("study.flashcard")}>
            {claim(card.front)}
            {revealed ? <>{claim(card.back)}<div className="flex flex-wrap gap-2">{(["again", "hard", "good", "easy"] as const).map((rating) => {
              const interval = card.review_intervals_ms?.[rating];
              return <Button key={rating} size="sm" disabled={view.busy || shown.status === "stale"} onClick={() => { void controller.rate(card, rating); }}>{t(`study.rating.${rating}`)}{interval !== undefined && <span className="text-meta text-muted">· {interval < 86_400_000 ? t("study.review_interval_minutes", { count: Math.ceil(interval / 60_000) }) : t("study.review_interval_days", { count: Math.round(interval / 86_400_000) })}</span>}</Button>;
            })}</div></> : <span><Button tone="primary" onClick={() => setRevealed(true)}>{t("study.reveal")}</Button></span>}
          </section>)}
          {view.deck && <span><Button disabled={view.busy || !canConfigure || shown.status !== "ready"} onClick={() => pick("quiz", shown.aid_id)}>{t("study.generate.quiz")}</Button></span>}
          {view.quiz && <StudyQuiz key={view.quiz.quiz_id} quiz={view.quiz} result={view.result} resultAnswers={view.resultAnswers ?? {}} feedback={view.quizFeedback ?? {}} checkedAnswers={view.quizChecked ?? {}} busy={view.busy} accessBlocked={Boolean(accessBlocked)} stale={shown.status === "stale"}
            quizHelp={view.quizHelp} helpJob={helpJob} helpPending={helpPending} safeHelp={safeHelp} helpAids={helpAids} claim={claim} jobs={jobs}
            onDraft={(answers, position) => { void controller.saveDraft(answers, position); }}
            onCheck={(id, value) => { const quizId = view.quiz?.quiz_id; return controller.checkAnswer(id, value).then(() => controller.getView().quiz?.quiz_id === quizId && !controller.getView().error); }}
            onSubmit={(answers) => { void controller.submit(answers); }} onClearResult={() => controller.clearResult()} onRetake={() => { void controller.retakeQuiz(); }} ownCourse={!curriculum} onHelp={help}
            onFollowUp={(itemId, kind, text) => controller.requestQuizHelp(itemId, kind, readableConfirmed, text).then(() => !controller.getView().error)} />}
        </article>}
        {tab === "generate" && <div className="flex flex-wrap items-center gap-2 pt-2">{activityButton}<span className="flex-1" /><Button size="sm" tone="ghost" onClick={goAsk}><Icon name="question" size={16} />{t("study.chat_prompt")}</Button></div>}
        </>}
      </div>
      {/* The ask box: one line that grows, at the foot of Ask this material */}
      {tab === "chat" && <form className="flex shrink-0 flex-col gap-3 border-t border-line px-4 pt-3 pb-4" onSubmit={(e) => { e.preventDefault(); askMaterial(); }}>
        <ComposerShell send={<IconButton type="submit" tone="action" icon="send" tip="above" label={t("study.ask_material")} disabled={view.busy || helpPending || !chatPrompt.trim() || !ready} />}>
          <label htmlFor="study-ask" className="sr-only">{t("study.chat_prompt")}</label>
          <textarea id="study-ask" ref={ask} rows={1} required maxLength={4000} placeholder={t("study.ask_placeholder")} className="w-full resize-none bg-transparent px-1 text-body text-ink outline-none placeholder:text-placeholder" value={chatPrompt} onChange={(e) => setChatPrompt(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && chatPrompt.trim() && ready && !view.busy) { e.preventDefault(); askMaterial(); } }} />
        </ComposerShell>
      </form>}
    </aside>
  </div></section>
  {activityDialog}
  <Dialog open={helpReview !== null} heading={t("study.quiz_help_review")} closeLabel={t("action.cancel")} onClose={() => setHelpReview(null)}>
    <p className="text-ui text-muted">{t("study.quiz_help_review_help")}</p>
    <label className="flex min-h-11 items-center gap-2 text-ui"><input type="checkbox" checked={readableConfirmed} onChange={(e) => review(e.target.checked)} />{t("study.confirm_readable")}</label>
    <Button tone="primary" disabled={!readableConfirmed || view.busy || !!accessBlocked} onClick={() => { if (helpReview) { void controller.requestQuizHelp(helpReview.itemId, helpReview.kind, readableConfirmed); setHelpReview(null); } }}>{t("study.quiz_help_request")}</Button>
  </Dialog>
  <GenerateDialog key={`${source.source_id}:${source.revision}:${tool}:${toolContext}`} tool={tool} contextual={!!toolContext} reviewRequired={source.state === "partial"} reviewed={readableConfirmed} onReview={review} topics={toolContext ? [] : view.materialTopics} syllabi={toolContext ? [] : syllabi} busy={view.busy} error={view.error && !view.safetyTemplate ? errorText(view.error) : null} onClose={() => { setTool(null); setToolContext(null); }}
        onGenerate={(chosen, request) => generate(chosen, request)} />
  {teachReview && <Dialog open heading={t("journey.teach_beginning")} closeLabel={t("action.cancel")} onClose={() => setTeachReview(false)}><div className="flex flex-col gap-4"><label className="flex items-start gap-2 text-ui"><input type="checkbox" checked={readableConfirmed} onChange={(e) => review(e.target.checked)} />{t("study.confirm_readable")}</label><Button tone="primary" disabled={view.busy || !readableConfirmed} onClick={() => { setTeachReview(false); teach(); }}>{t("journey.teach_beginning")}</Button></div></Dialog>}
  {duplicate && <Dialog open heading={t("study.existing_set")} closeLabel={t("action.cancel")} onClose={() => setDuplicate(null)}><div className="flex flex-col gap-4"><p className="text-ui">{t("study.existing_set_help")}</p><div className="flex flex-wrap gap-2"><Button tone="primary" onClick={() => { void controller.showAid(duplicate.aid.aid_id); setDuplicate(null); }}>{t("study.open_existing")}</Button><Button disabled={view.busy} onClick={() => generate(duplicate.tool, duplicate.request, true)}>{t("study.generate_new")}</Button></div></div></Dialog>}
  </>;
}
