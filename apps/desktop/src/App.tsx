import { LearningJourneyController, nextJourneyAction, type JourneyAction, type LearningJourneyView } from "@jana/client-core";
import { LearningJourney, StudyJourney } from "@jana/ui";
import type { S2Screen } from "./routes.js";
import { BrowserPodcastAudio } from "./podcast-audio.js";
import { BrowserLocalSpeech } from "./local-speech.js";
import { objectiveForKc, objectiveForLesson, objectiveForTopic, type LearningObjective } from "./learning-navigation.js";
// The desktop client (M5, M10): first run on a computer with no students yet ("Who's learning?", then the hand-off,
// UXS-10, PLT-17), the profile picker (UXS-09), then the open profile's Home (where a profile lands: Home is the hub,
// UXS-01), tutor, notes and Learn, and Settings
// (UXS-29), from which (and from a turn's memory-use line or a saved note) the "What the tutor knows" page opens (UXS-28,
// MEM-28). Switching student closes the profile: the store, the open session, the knows page's data and the render
// caches are cleared before the picker shows (SEC-19); so does a quiet spell longer than the core's inactivity window,
// and the picker says why. While the student is using the app, a profile the core closed (its idle auto-lock counts
// only profile requests; a restarted core has no tokens) is opened again at once with no PIN (D86, D87), and opening a
// profile continues its newest session for the chosen subject (`GET /v1/sessions`) instead of starting again.
// The tutor's subject is chosen from the subjects installed on this computer and is the subject the student last worked
// on; accounting's ledger entry comes with the accounting session. Above the screen, the degraded-mode notices of
// ARC-26 say when the tutor cannot run or the open subject's content failed verification. Learn opens the subject map
// of the subjects on this computer and its lessons (UXS-22, UXS-23); a mastery change on any session re-reads the map.
// Where no tutor can answer (the content-only class, below the minimum laptop, no tutor model, PLT-29) the app opens on
// Learn, New question is not offered, and Home offers lessons instead of an Ask box.
// An open profile's screens sit in the studio frame (UXS-01, direction A): the sidebar with New question (a new session
// for the subject), Home, Learn and Study notes, the recent sessions grouped by day (each reopens where it was left,
// UXS-19), and at its foot the student, Switch student, Settings and the connectivity indicator; the global keys of
// TABLE LOC-12a work on every one of them. The Mock exam place (Ctrl+4, UXS-25) runs a sitting on the core's clock;
// while one is open (in progress, paused or interrupted) no tutor session is opened and every place that would reach
// the tutor, voice, capture, lessons or practice shows why it waits and how long is left (AGT-24); a sitting left open
// comes back first when the profile opens. A report's "Show me why" opens a review session with the item and answer
// loaded (UXS-26), whose header leads back to the report; "Practise these topics" opens the set the report made.
// Home carries the study plan ("Next up", UXS-12): Start opens the next activity (practice, a lesson for an idea not
// started, the mock place, or Progress to look back), Change and Not today answer the plan card, and a finished plan
// offers Keep learning. While the tutor model loads, Home's Ask box still takes a question and sends it once the tutor
// is ready. The Progress place (Ctrl+5, UXS-27) shows the learner model in words. Where no tutor answers, Home's Ask
// box searches the installed worked solutions instead (UXS-51).
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import {
  AskController, CHECKIN_REFRESH_MS, CompanionController, StudyWorkspaceController, SessionNotesController, SavedQuestionsController, AssessmentController, CLIENT_KEYS, ExamDateController, HistoryController, KnowsController, LessonController, LOCKED_STATES, MockController, PastPaperController, PlanController, PracticeController, ProfileController, ProgressController, SUBJECT_CODES, SUBJECT_NAMES,
  SearchController, SessionController, StoreHolder, SubjectMapController, ConceptMapController, MapOverviewController, clientId, examLock, groupSessions, installedSubjects, finishSetup, kcSubjectCode, nextBlock,
  parseAssets, parseNotices, readDegraded, readLicence, readProfile, readSetup, readSubjectMap, subjectName, tutorAnswers, tutorReach, tutorSetup, typedCapture, helpTurn, type HelpKind, DailyGoal, goalProgress,
  type CheckinNotifier, type CompanionAction, type ConceptNode, type KcLinks, type NoteReference, type VoiceAnswer, type VoicePlace, type AssessmentStart, type ChooseInstallSource, type HealthReport, type Launch, type MapTopic, type NewSubjectStart, type PlanBlock, type SessionTarget, type SubjectMap as SubjectMapData,
} from "@jana/client-core";
import {
  AskAnswer, CourseAsk, DueToday, CompanionThread, StudyWorkspace, FollowUpProvider, type FollowUpHandlers, DailyGoalChip, Transcript, SessionNotes, NotesPlace, SavedQuestions, SavedQuestionsProvider, AssessmentPlace, AbandonedMockCard, AppFrame, ExamCountdownCard, LastTimeCard, CheckinCard, Button, Composer, ComposerShell, ExamLockNotice, FasterTutorProvider, IconButton, Loader, MenuBar, MockPlace, NoTutor, Skeleton, StudyNotes, ErrorState, DegradedNotices, FirstRun, FirstRunInterview, FirstRunTutor, FirstRunTutorPick, Home,
  ConceptMapRendererProvider, GapLinkProvider, ResearchLinkProvider, KnowsLinkProvider, KnowsPage, LaunchScreen, useLaunchSurface, LessonLinkProvider, LessonPlayer, KeyboardHelp, LocaleProvider, Notice, PlanCard, Practice, ProfilePicker, Progress, RegistryProvider, ScreenHeader, SearchBox, SearchScreen,
  SessionHistory, Settings, Sidebar, StoreProvider, StudioFrame, SubjectDot, SubjectMap, TabBar, TutorLookProvider, TutorScreen, ProfileRecovery, clearRenderCaches, defaultRegistry, discOf, tutorAvatarOf, tutorNameOf,
  useAppliedPreferences, useCharacterKeys, useDuePractice, useGlobalShortcuts, useNow,
  useProfiles, useRecentSessions, useT, type AskContext, type ConceptActions, type StudyRequest, type StudyTab, type FramePlace, type LessonInProgress, type LockedPlace, type ReportNames, type SidebarContent, type SubjectChoice,
} from "@jana/ui";
import { ConceptMapSvg, RENDERERS } from "@jana/renderers";
import { errors, type api_commands } from "@jana/contracts-ts/client";
import thirdPartyNotices from "../../../THIRD_PARTY_NOTICES?raw";
const assetRegister = ""; // Hosted workshop has no downloadable installation assets.
import { registerVisuals } from "./visuals.js";
import { VoiceSettings } from "./VoiceSettings.js";
import { NO_VOICE, SpeechTools, voiceOfferFrom, type VoiceOffer } from "./SpeechTools.js";

/** About → Licences (PLT-10): every component the release bundles and every shipped asset, as the build carries them. */
const NOTICES = parseNotices(thirdPartyNotices);
const ASSETS = parseAssets(assetRegister);

export interface AppRuntime {
  holder: StoreHolder;
  profiles: ProfileController;
  /** The "What the tutor knows" page's reads and forgets for the open profile (LRN-36, UXS-28). */
  knows: KnowsController;
  /** The subject map and the lesson player for the open profile (UXS-22, UXS-23). */
  map: SubjectMapController;
  conceptMap: ConceptMapController;
  /** My map, Progress's hero: every installed subject as topic tiles (SPEC §21). */
  overview: MapOverviewController;
  lessons: LessonController;
  /** Practice sets for the open profile (UXS-24, API-27). */
  practice: PracticeController;
  assessments: AssessmentController;
  savedQuestions: SavedQuestionsController;
  sessionNotes: SessionNotesController;
  studyWorkspace: StudyWorkspaceController;
  /** Mock exams for the open profile: papers, sittings and the exam lock (UXS-25, UXS-26, AGT-24). */
  mocks: MockController;
  /** Imported past papers and exam dates with their countdown and reminders (ASM-27, LRN-44). */
  pastPapers: PastPaperController;
  examDates: ExamDateController;
  /** Home's study plan, the Progress screen and worked-solution search (UXS-12, UXS-27, UXS-51). */
  plans: PlanController;
  progress: ProgressController;
  /** Progress → Session history, read-only (UXS-48). */
  history: HistoryController;
  search: SearchController;
  /** The gentle daily goal (LRN-45). */
  goal: DailyGoal;
  /** Home's one input and Ask anything (UXS-67, STU-20). */
  ask: AskController;
  /** The Last time card on Home and in Study (SPEC §19.5.2, W8 phase 1), **Your tutor** (phase 2) and the check-in
   * card (phase 3). */
  companion: CompanionController;
  /** Check-in notifications through the desktop shell (SPEC §19.5.5, JM-D24); absent outside the shell. */
  notifier?: CheckinNotifier & { takeClick?: () => Promise<string | null> };
  launch: Launch;
  fetch?: typeof fetch;
  chooseSource?: ChooseInstallSource;
  /** Native file dialog for restoring a profile archive (UXS-47). */
  chooseArchive?: () => Promise<string | null>;
  updateRecovery?: () => Promise<string | null>;
  rollbackUpdate?: () => Promise<void>;
}

/** VOC-53, D130: the Ask anything place on Home (one value, so the voice socket hears it once). */
const ASK_PLACE: VoicePlace = { place: "ask" };
// The tutor screen's voice conversation, offered while Cloud voice is switched on (the owner's choice for this entry,
// replacing D130 there): spoken turns go through the whole tutor turn, filler and accepted words only (JM-D6).
const TUTOR_PLACE: VoicePlace = { place: "tutor" };

/** The study set's Chat tab as the voice conversation's place: the open source with its grant, or none. */
function StudyPlace({ controller, chat, children }: { controller: StudyWorkspaceController; chat: boolean; children: (place: VoicePlace | null) => ReactNode }) {
  const view = useSyncExternalStore(controller.subscribe, controller.getView, controller.getView);
  const place = chat && view.source !== null ? controller.voicePlace() : null;
  const key = place === null ? null : JSON.stringify(place);
  const stable = useMemo(() => (key === null ? null : (JSON.parse(key) as VoicePlace)), [key]);
  return <>{children(stable)}</>;
}

export function createRuntime(launch: Launch, fetchImpl?: typeof fetch, notifier?: AppRuntime["notifier"]): AppRuntime {
  const holder = new StoreHolder();
  const profiles = new ProfileController(launch, holder, fetchImpl === undefined ? {} : { fetch: fetchImpl });
  profiles.onClear(clearRenderCaches);
  const knows = new KnowsController(profiles);
  const runtime: AppRuntime = {
    holder, profiles, knows, map: new SubjectMapController(profiles), conceptMap: new ConceptMapController(profiles), overview: new MapOverviewController(profiles), lessons: new LessonController(profiles), practice: new PracticeController(profiles), mocks: new MockController(profiles), pastPapers: new PastPaperController(profiles), examDates: new ExamDateController(profiles),
    studyWorkspace: new StudyWorkspaceController(profiles, new BrowserLocalSpeech(), new BrowserPodcastAudio()), sessionNotes: new SessionNotesController(profiles), savedQuestions: new SavedQuestionsController(profiles), assessments: new AssessmentController(profiles), plans: new PlanController(profiles), progress: new ProgressController(profiles), history: new HistoryController(profiles), search: new SearchController(profiles), goal: new DailyGoal(profiles), ask: new AskController(profiles), companion: new CompanionController(profiles, { ...(fetchImpl === undefined ? {} : { fetch: fetchImpl }), ...(notifier === undefined ? {} : { notifier }) }), launch,
    ...(notifier === undefined ? {} : { notifier }),
  };
  return fetchImpl === undefined ? runtime : { ...runtime, fetch: fetchImpl };
}

type TutorTarget = SessionTarget;
type Screen = S2Screen | "study" | "notes" | "history" | "session-history" | "knows" | "knows-from-settings" | "knows-from-study" | "knows-from-learn";
type Opened = SessionController | "failed" | null;
/** First run: "Who's learning?", then the hand-off for the student who was added (UXS-10). */
type FirstRunStep = { step: "who"; resume?: boolean } | { step: "restore" } | { step: "handoff"; name: string } | { step: "tutor"; next: Screen } | { step: "interview"; next: Screen } | null;

function askTarget(subject: TutorTarget["subject"], level: "O" | "A"): TutorTarget {
  return { mode: "guided", subject, level, origin: { kind: "ask", ref: null } };
}

function topicTarget(nodeId: string, origin: TutorTarget["origin"]): TutorTarget | null {
  const [, code, level] = nodeId.split(".");
  const subject = code === undefined ? null : subjectName(code);
  if (subject === null || (level !== "o" && level !== "a")) return null;
  return { mode: "guided", subject, level: level === "o" ? "O" : "A", topic_id: nodeId, origin };
}

/** Session opening per runtime, profile and target, so StrictMode's second effect run reuses the first one's. */
const opening = new WeakMap<AppRuntime, Map<string, Promise<string>>>();

function resumeOnce(runtime: AppRuntime, key: string, target: TutorTarget, options: { fetch?: typeof fetch }): Promise<string> {
  const mine = opening.get(runtime) ?? new Map<string, Promise<string>>();
  opening.set(runtime, mine);
  const at = `${key}:${runtime.profiles.generation}:${JSON.stringify(target)}`;
  let pending = mine.get(at);
  if (pending === undefined) {
    pending = SessionController.resume(runtime.profiles.launch, target, options);
    mine.clear(); // only the current profile's session is kept
    mine.set(at, pending);
  }
  return pending;
}

/** A new question in another subject (AGT-15, D94): `start` opens a session there for the card's question, and the
 * session asks it once it is open (`take` hands the words over once). */
interface FirstQuestions {
  start: (start: NewSubjectStart) => void;
  /** The first message a session opened for it sends at once: a question in another subject (intent ask), or the help
   * a practice question's help chip asked for (ASM-28). */
  take: (sessionId: string) => { content: NewSubjectStart["content"]; intent: string } | undefined;
}

/** The open session's subject as the core last said it (`session.updated`): the engine moves a new asked session to
 * its first question's subject (AGT-15, D94). */
function useOpenSubject(holder: StoreHolder, sessionId: string | null): string | null {
  const read = useCallback(() => (sessionId === null ? null : holder.getState().sessions[sessionId]?.subject ?? null), [holder, sessionId]);
  return useSyncExternalStore(holder.subscribe, read, read);
}

/** The tutor's target as the header chip, Home and "New question" show it: the open session's subject when the
 * engine moved it (D94), without reopening the session. */
function followSubject(target: TutorTarget | null, subject: string | null, installed: readonly { subject: string; level: "O" | "A" }[]): TutorTarget | null {
  const name = subject === null ? null : subjectName(subject);
  if (target === null || name === null || name === target.subject) return target;
  return askTarget(name, installed.find((s) => s.subject === name)?.level ?? target.level ?? "O");
}

/** The open profile's session for `target`: the one the student chose (`chosen`, a recent session or a new question),
 * else its newest one for that subject and topic, or a new one; closed when the profile closes or is switched. If the
 * profile ends while the session is open, the picker takes over. */
function useProfileSession(runtime: AppRuntime, key: string | null, target: TutorTarget | null, chosen: string | null, firstQuestion?: FirstQuestions): Opened {
  const [openedState, setOpenedState] = useState<{ runtime: AppRuntime; key: string; target: TutorTarget; chosen: string | null; generation: number; session: Opened } | null>(null);
  const generation = runtime.profiles.generation;
  const firstQuestions = useRef(firstQuestion);
  firstQuestions.current = firstQuestion;
  useEffect(() => {
    if (key === null || target === null) return;
    setOpenedState(null);
    const { profiles, holder } = runtime;
    const options = runtime.fetch === undefined ? {} : { fetch: runtime.fetch };
    let current: SessionController | null = null;
    let cancelled = false;
    const close = () => {
      cancelled = true;
      current?.close();
      setOpenedState(null);
    };
    const off = profiles.onClear(close);
    (async () => {
      const sessionId = chosen ?? await resumeOnce(runtime, key, target, options);
      if (cancelled || profiles.generation !== generation) return;
      current = new SessionController(profiles.launch, holder, sessionId, options);
      current.onProgress((kcId) => { runtime.map.onProgress(kcId); runtime.conceptMap.onProgress(kcId); }); // a mastery change re-reads the maps (UXS-22)
      current.onAuthLost(() => void profiles.expire()); // the profile could not be opened again (SEC-19)
      const opened = current;
      current.onNewSubject((start) => firstQuestions.current?.start(start)); // the card naming another subject (D94)
      await current.open();
      if (cancelled) return;
      setOpenedState({ runtime, key, target, chosen, generation, session: current });
      // A session opened for a question in another subject asks it there at once (AGT-15, D94).
      const question = firstQuestions.current?.take(sessionId);
      if (question !== undefined && question.content.length > 0) opened.send(question.content, question.intent);
    })().catch(() => {
      if (!cancelled && profiles.generation === generation) setOpenedState({ runtime, key, target, chosen, generation, session: "failed" });
    });
    return () => {
      off();
      cancelled = true;
      current?.close();
    };
  }, [runtime, key, target, chosen, generation]);
  // A changed subject/profile is already a new request during render. Do not expose the previous
  // controller for the frame before the opening effect runs: typing there would be lost on remount.
  return openedState?.runtime === runtime && openedState.key === key && openedState.target === target
    && openedState.chosen === chosen && openedState.generation === generation ? openedState.session : null;
}

/** The core's health, packs and hardware report for the degraded-mode notices (ARC-26, PLT-29, PLT-22): read at start and whenever the window comes
 * back into focus, so a runtime that gave up, or a pack that failed verification, is said without a reload. */
function useDegraded(profiles: ProfileController): Pick<HealthReport, "health" | "packs" | "models"> & { read: boolean } {
  const [state, setState] = useState<Pick<HealthReport, "health" | "packs" | "models"> & { read: boolean }>({ health: null, packs: [], models: null, read: false });
  useEffect(() => {
    let current = true;
    const read = () => {
      void readDegraded(profiles.commands).then((next) => {
        if (current) setState({ ...next, read: true });
      });
    };
    read();
    window.addEventListener("focus", read);
    return () => {
      current = false;
      window.removeEventListener("focus", read);
    };
  }, [profiles]);
  // While the tutor model loads, read again until it is ready, so Home's Ask box and a question kept meanwhile follow it
  // (UXS-12).
  const loading = state.health?.readiness === "model_loading";
  useEffect(() => {
    if (!loading) return;
    let current = true;
    const timer = setInterval(() => {
      void readDegraded(profiles.commands).then((next) => {
        if (current) setState({ ...next, read: true });
      });
    }, CLIENT_KEYS.ui_health_poll_ms);
    return () => {
      current = false;
      clearInterval(timer);
    };
  }, [profiles, loading]);
  return state;
}

/** Keys, pointer and wheel count as the student using the app: the profile's inactivity window starts again (SEC-19). */
function useActivity(profiles: ProfileController): void {
  useEffect(() => {
    const seen = () => profiles.noteActivity();
    const events = ["keydown", "pointerdown", "wheel", "touchstart"] as const;
    for (const name of events) window.addEventListener(name, seen, { capture: true, passive: true });
    return () => {
      for (const name of events) window.removeEventListener(name, seen, { capture: true });
    };
  }, [profiles]);
}

/** What the open session shows the sidebar: a turn ending, a title arriving or the subject moving (D94) reads the recent
 * sessions again. */
function useSessionVersion(holder: StoreHolder, sessionId: string | null): string {
  const read = useCallback(() => {
    const session = sessionId === null ? undefined : holder.getState().sessions[sessionId];
    if (session === undefined) return `${sessionId ?? ""}`;
    const last = session.turn_ids[session.turn_ids.length - 1];
    return `${sessionId}:${session.turn_ids.length}:${session.title}:${session.subject}:${last === undefined ? "" : (holder.getState().turns[last]?.status ?? "")}`;
  }, [holder, sessionId]);
  return useSyncExternalStore(holder.subscribe, read, read);
}

function Starting({ failed }: { failed: boolean }) {
  // The launch screen is one of the brand's placements (DSN-25); it fills the screen with its midnight surface, the
  // status line at its foot included.
  useLaunchSurface();
  return (
    <div className="flex h-full">
      <LaunchScreen failed={failed} onRetry={() => window.location.reload()} />
    </div>
  );
}

/** A session opening inside the app: a quiet loader on the workspace's own surface, never the midnight launch screen
 * flashing up in the middle of the frame. */
function Opening({ failed }: { failed: boolean }) {
  const t = useT();
  return (
    <div className="flex h-full items-center justify-center p-6">
      {failed ? <ErrorState message={t("app.core_unavailable")} action={{ label: t("action.try_again"), onClick: () => window.location.reload() }} /> : <Loader label={t("app.starting")} />}
    </div>
  );
}

/** A card centred on paper; when the window is too short for it, it scrolls from its top instead of spilling over the
 * status line at the foot of the screen. */
function Centred({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col overflow-y-auto bg-paper p-6 text-ink" data-centred="">
      <div className="my-auto flex w-full justify-center">{children}</div>
    </div>
  );
}

/** The tutor's subject, from the subjects on this computer (UXS-14's subject chip in the header). */
function SubjectChooser({ choices, value, subject, onChange }: { choices: SubjectChoice[]; value: string | null; subject: string; onChange: (subjectId: string) => void }) {
  const t = useT();
  if (choices.length < 2 || value === null) return null;
  return (
    <label className="inline-flex min-h-9 items-center gap-2 rounded-chip bg-panel ps-3 pe-1 text-meta font-medium text-ink">
      <SubjectDot subject={subject} />
      <span className="sr-only">{t("tutor.subject")}</span>
      <select className="min-h-8 rounded-control bg-transparent pe-1 text-meta font-medium text-ink" value={value} onChange={(event) => onChange(event.target.value)}>
        {choices.map((c) => <option key={c.subjectId} value={c.subjectId}>{c.name}</option>)}
      </select>
    </label>
  );
}

/** The global keys of TABLE LOC-12a, while the frame is on screen, and the keyboard help "?" opens. */
function FrameKeys({ places, onSettings, characterKeys, help, onHelp }: { places: readonly ((() => void) | null)[]; onSettings: () => void; characterKeys: boolean;
  /** The keyboard help is open: from "?" here, or from the student menu's Keyboard shortcuts. */
  help: boolean; onHelp: (open: boolean) => void }) {
  useGlobalShortcuts({ places, onSettings, onHelp: () => onHelp(true), characterKeys });
  return <KeyboardHelp open={help} onClose={() => onHelp(false)} characterKeys={characterKeys} />;
}

function Shell({ runtime }: { runtime: AppRuntime }) {
  const t = useT();
  const { profiles } = runtime;
  const view = useProfiles(profiles);
  const [screen, setScreen] = useState<Screen>("home");
  const [studyAdd, setStudyAdd] = useState<"paste" | null>(null);
  const [studyResume, setStudyResume] = useState<"next" | "lesson" | "quiz" | null>(null);
  const [studySource, setStudySource] = useState<{ source_id: string; revision: number } | null>(null);
  const [studyResearch, setStudyResearch] = useState<string | null>(null); // a practised diagram's capture (SPEC §18)
  // Somewhere Study is asked to go (SPEC §19.3.3): a companion button, or the plan's due Study cards.
  const [studyRequest, setStudyRequest] = useState<StudyRequest | null>(null);
  // "Ask your tutor about this" on the learner's map (SPEC §21.3): the thread opens with this draft, nothing sent.
  const [companionDraft, setCompanionDraft] = useState<string | null>(null);
  // A check-in opened from Home (SPEC §19.5.5): Study opens **Your tutor** with it instead of the day's plain opener.
  const studyCheckin = useRef<string | null>(null);
  // The student menu (D115): Settings at a section, the keyboard help, and what it says about the student.
  const [settingsAt, setSettingsAt] = useState<{ id: string; at: number } | null>(null);
  const studySettingsReturn = useRef<{ profile: string | null; source: string | null } | null>(null);
  useEffect(() => { if (screen !== "settings" && screen !== "knows-from-settings") studySettingsReturn.current = null; }, [screen]);
  const [keysHelp, setKeysHelp] = useState(false);
  const [record, setRecord] = useState<{ subjects: string[]; level: "O" | "A" } | null>(null);
  // A profile with no subjects studies its own course and materials: no curriculum places, no curriculum tutor session.
  const ownCourse = record !== null && record.subjects.length === 0;
  const [online, setOnline] = useState(false);
  const [mapTopic, setMapTopic] = useState<string | null>(null);
  const [mapSubject, setMapSubject] = useState<string | null>(null);
  // A root-gap card's "See it on the map": Learn opens on the concept map with the gap path lit.
  const [mapFirst, setMapFirst] = useState(false);
  const [lessonId, setLessonId] = useState<string | null>(null);
  const [tutorTarget, setTutorTarget] = useState<TutorTarget | null>(null);
  // A session the student chose by name: one of the recent sessions, or the new one "New question" started.
  const [chosenSession, setChosenSession] = useState<string | null>(null);
  const [newQuestionState, setNewQuestionState] = useState<"pending" | "failed" | null>(null);
  const newQuestionVersion = useRef(0);
  const cancelNewQuestion = () => { newQuestionVersion.current++; setNewQuestionState(null); };
  // The tutor opened from a practice question ("Help me", UXS-24): its header offers the way back to that question.
  const [fromPractice, setFromPractice] = useState(false);
  const [fromLesson, setFromLesson] = useState<string | null>(null);
  const [planExpanded, setPlanExpanded] = useState(false);
  // The tutor opened from a mock report's "Show me why" (UXS-26): its header leads back to the report.
  const [fromReport, setFromReport] = useState(false);
  const [studyAvailable, setStudyAvailable] = useState(false);
  const savedStudy = useSyncExternalStore(runtime.studyWorkspace.subscribe, runtime.studyWorkspace.getView, runtime.studyWorkspace.getView);
  // What the installed speech pack offers (VOC-14): read-aloud in its voice, dictation, and voice turns (`/v1/meta`).
  const [voiceOffer, setVoiceOffer] = useState<VoiceOffer>(NO_VOICE);
  // Mock exams (UXS-25): the papers and sittings, and the exam lock while a sitting is open (AGT-24).
  const mockView = useSyncExternalStore(runtime.mocks.subscribe, runtime.mocks.getView, runtime.mocks.getView);
  const lock = examLock(mockView);
  const lockNow = useNow(lock !== null && lock.state === "in_progress");
  // The subject maps the mock report names its topics and ideas from (UXS-26).
  const [maps, setMaps] = useState<Record<string, SubjectMapData>>({});
  const mapsAsked = useRef(new Set<string>());
  const [firstRun, setFirstRun] = useState<FirstRunStep>(null);
  const [noteReference, setNoteReference] = useState<NoteReference | null>(null);
  useActivity(profiles);
  useAppliedPreferences(profiles);
  useEffect(() => {
    document.title = t("app.name");
    void profiles.load();
  }, [profiles, t]);
  // A computer with no students yet starts first run; it stays on until the hand-off is done, even though the first
  // student exists from "Start learning" on.
  const empty = view.status === "ready" && view.profiles.length === 0 && view.active === null;
  useEffect(() => {
    if (empty) setFirstRun((step) => step ?? { step: "who" });
  }, [empty]);
  // First run left part way (students added, "Start learning" never pressed) resumes at the same step with the students
  // already added (UXS-10, `GET /v1/setup`); a core that predates the route leaves the picker as it was.
  const listed = view.status === "ready" && view.profiles.length > 0 && view.active === null;
  useEffect(() => {
    if (!listed) return;
    let live = true;
    void readSetup(profiles.commands).then((state) => {
      if (live && state.step === "students") setFirstRun((step) => step ?? { step: "who", resume: true });
    }).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [profiles, listed]);
  const degraded = useDegraded(profiles);
  const setup = tutorSetup(degraded.models);
  // Whether a tutor answers here: null until the core has said (PLT-29, ARC-26).
  const answers = tutorAnswers(degraded);
  const tutorOffered = answers !== false;
  // No tutor can answer now, though one may later (SPEC §10.3): the online tutor is off or out of reach and there is no
  // model on this computer. The composer then says so and offers practice and mock exams instead of taking questions.
  const connectivity = useSyncExternalStore(profiles.connectivity.subscribe, profiles.connectivity.getView, profiles.connectivity.getView);
  const reach = tutorReach(degraded, connectivity);
  const noTutor = answers !== false && (reach === "offline" || reach === "switched_off") ? reach : null;
  const installed = installedSubjects(degraded.packs);
  // The subjects on this computer, from the installed packs (UXS-22).
  const subjects: SubjectChoice[] = installed.map((s) => ({ subjectId: s.subjectId, name: t(`settings.profiles.subject.${s.subject}`) }));
  const shownSubject = mapSubject ?? subjects[0]?.subjectId ?? null;

  // The tutor's subject once a profile is open: the one the student last worked on, if it is installed, otherwise the
  // first installed subject (every subject at O level when no pack is installed yet).
  const active = view.active;
  useEffect(() => { setStudyResume(null); setStudySource(null); setStudyResearch(null); setStudyRequest(null); }, [active]);
  useEffect(() => { if (screen !== "study") { setStudyResume(null); setStudySource(null); setStudyRequest(null); setCompanionDraft(null); } }, [screen]);
  useEffect(() => { if (active && studyAvailable && screen === "home") void runtime.studyWorkspace.load(); }, [active, studyAvailable, screen, runtime.studyWorkspace]);
  // The Study screen's session of the day (SPEC §19.3.2): opened while the screen shows, ended when it closes; the
  // Last time card in Study's library is read with it, and **Your tutor**'s episode of the day is opened or resumed
  // (SPEC §19.3.3), its stream running while Study shows.
  useEffect(() => {
    if (!active || screen !== "study") return;
    void runtime.studyWorkspace.enter();
    void runtime.companion.loadOpener();
    const checkin = studyCheckin.current;
    studyCheckin.current = null;
    void (checkin !== null ? runtime.companion.openCheckin(checkin) : runtime.companion.open());
    return () => { void runtime.studyWorkspace.leave(); runtime.companion.close(); };
  }, [active, screen, runtime.studyWorkspace, runtime.companion]);
  const journey = useMemo(() => new LearningJourneyController(profiles), [profiles]);
  const journeyView = useSyncExternalStore(journey.subscribe, journey.getView, journey.getView);
  const assessmentView = useSyncExternalStore(runtime.assessments.subscribe, runtime.assessments.getView, runtime.assessments.getView);
  const protectedCheck = assessmentView.assessments.find((a) => a.kind === "mastery_check" && (a.state === "in_progress" || a.state === "stopped") && a.current_item !== null);
  useEffect(() => { if (active !== null) void runtime.assessments.load(); }, [active, runtime.assessments]);
  const installedKey = installed.map((s) => s.subjectId).join(",");
  useEffect(() => {
    if (active === null || !degraded.read) return;
    let live = true;
    void SessionController.latest(profiles.launch, runtime.fetch === undefined ? {} : { fetch: runtime.fetch }).then((latest) => {
      if (!live) return;
      setTutorTarget((current) => {
        if (current !== null) return current;
        const last = latest === null ? null : subjectName(latest.subject);
        const pick = installed.find((s) => s.subject === last) ?? installed[0];
        return pick !== undefined ? askTarget(pick.subject, pick.level) : askTarget(last ?? SUBJECT_NAMES[0] ?? "mathematics", "O");
      });
    });
    return () => {
      live = false;
    };
  }, [profiles, runtime.fetch, active, degraded.read, installedKey]);
  useEffect(() => profiles.onClear(() => {
    newQuestionVersion.current++;
    setNewQuestionState(null);
    studyCheckin.current = null;
    setHeld(null);
    setNoteReference(null);
    setTutorTarget(null);
    setChosenSession(null);
    setFromReport(false);
    setMaps({});
    mapsAsked.current.clear();
    setMapSubject(null);
    setLessonId(null);
    setFromLesson(null);
    setPlanExpanded(false);
    setFromPractice(false);
    setScreen("home");
    setMapTopic(null);
  }), [profiles]);
  // Where no tutor can answer, the app opens on Learn, never on a tutor that cannot reply (PLT-29).
  useEffect(() => {
    if (answers === false && !ownCourse) setScreen((current) => (current === "tutor" || current === "home" ? "learn" : current));
  }, [answers, active, ownCourse]);

  // A sitting left open comes first when a profile opens: the welcome back of an interrupted mock, before any other
  // screen (TABLE UXS-25a), or the sitting itself when the core kept it running. No tutor session is opened while a
  // sitting is open, since the core refuses one (423 `tutor_locked`, AGT-24), nor before the sittings have been read.
  useEffect(() => {
    if (active === null) return;
    let live = true;
    void runtime.mocks.load().then(() => {
      if (!live) return;
      const open = runtime.mocks.getView().sittings.find((s) => LOCKED_STATES.includes(s.state));
      if (open === undefined) return;
      setScreen("mock");
      void runtime.mocks.open(open.exam_id);
    });
    return () => {
      live = false;
    };
  }, [runtime, active]);
  const mocksRead = mockView.status === "ready" || mockView.status === "unavailable" || mockView.status === "failed";
  const shownPaper = mockView.exam === null ? null : mockView.papers.find((p) => p.paper_id === mockView.exam?.paper_id) ?? null;
  const reportMap = shownPaper === null ? null : `cur.${SUBJECT_CODES[shownPaper.subject]}.${shownPaper.level.toLowerCase()}`;
  useEffect(() => {
    if (reportMap === null || maps[reportMap] !== undefined) return;
    let live = true;
    profiles.commands.get<unknown>(`/v1/curriculum/${encodeURIComponent(reportMap)}/map`).then((raw) => {
      if (live) setMaps((known) => ({ ...known, [reportMap]: readSubjectMap(raw) }));
    }).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [profiles, reportMap, maps]);
  const reportNames = useCallback((subject: string): ReportNames => {
    const map = Object.values(maps).find((m) => m.subject === subject);
    const titles = new Map<string, string>();
    const ideas = new Map<string, string>();
    for (const section of map?.sections ?? []) {
      titles.set(section.node_id, section.title);
      for (const topic of section.topics) {
        titles.set(topic.node_id, topic.title);
        for (const kc of topic.kcs) ideas.set(kc.kc_id, kc.statement);
      }
    }
    return { topic: (id) => titles.get(id) ?? id, kc: (id) => ideas.get(id) ?? id };
  }, [maps]);
  // "Start a new question" on the card naming another subject (AGT-15, D94): a new session started by asking in that
  // subject, which asks the same words as soon as it is open.
  const pendingQuestions = useRef(new Map<string, { content: NewSubjectStart["content"]; intent: string }>());
  const firstQuestions: FirstQuestions = {
    start: ({ subject, content }) => {
      const fresh = askTarget(subject, installed.find((s) => s.subject === subject)?.level ?? tutorTarget?.level ?? "O");
      void SessionController.create(profiles.launch, fresh, runtime.fetch === undefined ? {} : { fetch: runtime.fetch }).then((sessionId) => {
        pendingQuestions.current.set(sessionId, { content, intent: "ask" });
        setTutorTarget(fresh);
        setChosenSession(sessionId);
        setFromLesson(null);
        setFromPractice(false);
        setFromReport(false);
        setScreen("tutor");
      }).catch(() => undefined);
    },
    take: (sessionId) => {
      const content = pendingQuestions.current.get(sessionId);
      pendingQuestions.current.delete(sessionId);
      return content;
    },
  };
  const openedSession = useProfileSession(runtime, active !== null && mocksRead && lock === null && protectedCheck === undefined && screen !== "assessment" && newQuestionState === null ? active : null, tutorTarget, chosenSession, firstQuestions);
  const session: Opened = newQuestionState === "failed" ? "failed" : openedSession;
  useEffect(() => {
    if (noteReference === null || screen !== "history" || session === null || session === "failed" || session.sessionId !== noteReference.session_id) return;
    let frame = 0;
    const jump = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const element = [...document.querySelectorAll<HTMLElement>("[data-turn-id]")].find((el) => el.dataset.turnId === noteReference.turn_id);
        if (element === undefined) return;
        element.tabIndex = -1; element.scrollIntoView?.({ block: "center" }); element.focus({ preventScroll: true });
        setNoteReference(null);
      });
    };
    jump(); const off = runtime.holder.subscribe(jump);
    return () => { off(); cancelAnimationFrame(frame); };
  }, [noteReference, screen, session, runtime.holder]);
  const lessonView = useSyncExternalStore(runtime.lessons.subscribe, runtime.lessons.getView, runtime.lessons.getView);
  const practiceView = useSyncExternalStore(runtime.practice.subscribe, runtime.practice.getView, runtime.practice.getView);
  useEffect(() => { if (active !== null) void journey.load(); }, [active, journey, screen, practiceView.tries.length, practiceView.worked, practiceView.item?.item_id]);
  const openId = session === null || session === "failed" ? null : session.sessionId;
  // The subject the open session is in now: the engine moves a new asked session to its question's subject (D94).
  const shownTarget = followSubject(tutorTarget, useOpenSubject(runtime.holder, openId), installed);
  const characterKeys = useCharacterKeys(active, view.settings);
  const recent = useRecentSessions(profiles, active, useSessionVersion(runtime.holder, openId));
  // The open student's subjects and level for the student menu: read when a profile opens and again on leaving
  // Settings, where they can change. The plan is read only as the menu opens (no licence read in first run, PLT-17).
  const inSettings = screen === "settings";
  useEffect(() => {
    if (active === null) { setRecord(null); setOnline(false); return; }
    if (inSettings) return;
    let live = true;
    void readProfile(profiles.commands, active).then((own) => {
      if (live) setRecord({ subjects: own.subjects, level: own.level });
    }).catch(() => undefined);
    return () => { live = false; };
  }, [profiles, active, inSettings]);
  const due = useDuePractice(profiles, active, installed.map((s) => s.subjectId));
  // The study plan and Progress (UXS-12, UXS-27): read when a profile opens and each time Home or Progress shows, and
  // again when a mastery change is reported (LRN-34).
  const planView = useSyncExternalStore(runtime.plans.subscribe, runtime.plans.getView, runtime.plans.getView);
  const goalTarget = useSyncExternalStore(runtime.goal.subscribe, runtime.goal.getTarget, runtime.goal.getTarget);
  const askView = useSyncExternalStore(runtime.ask.subscribe, runtime.ask.getView, runtime.ask.getView);
  const [studyTab, setStudyTab] = useState<StudyTab>("generate");
  const progressView = useSyncExternalStore(runtime.progress.subscribe, runtime.progress.getView, runtime.progress.getView);
  const examDatesView = useSyncExternalStore(runtime.examDates.subscribe, runtime.examDates.getView, runtime.examDates.getView);
  const companionView = useSyncExternalStore(runtime.companion.subscribe, runtime.companion.getView, runtime.companion.getView);
  // The learner's map lens (SPEC §21): the layer chosen on Progress opens the skill map on the same layer.
  const mapLens = useSyncExternalStore(runtime.conceptMap.subscribe, runtime.conceptMap.getLens, runtime.conceptMap.getLens);
  // **Your tutor** (SPEC §19.3.3): left out of Study altogether on a core without the thread, as the Last time card is.
  const companionThread = useSyncExternalStore(runtime.companion.subscribe, runtime.companion.getThread, runtime.companion.getThread);
  const checkinView = useSyncExternalStore(runtime.companion.subscribe, runtime.companion.getCheckin, runtime.companion.getCheckin);
  // The due query again every 30 minutes while a profile is open (§19.5.5); a lock or switch drops the card.
  useEffect(() => {
    if (active === null) return;
    const timer = setInterval(() => void runtime.companion.loadCheckins(), CHECKIN_REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, runtime.companion]);
  // A click on a check-in notification brings the window forward; the app then opens Home, where the card is.
  useEffect(() => {
    const takeClick = runtime.notifier?.takeClick;
    if (active === null || takeClick === undefined) return;
    const focused = () => void takeClick().then((clicked) => {
      if (clicked === null || profiles.getView().active !== active) return;
      setScreen("home");
      void runtime.companion.loadCheckins();
    }, () => undefined);
    window.addEventListener("focus", focused);
    return () => window.removeEventListener("focus", focused);
  }, [active, profiles, runtime.notifier, runtime.companion]);
  const [progressVersion, setProgressVersion] = useState(0);
  // Read once Home or Progress is on screen (not behind first run's hand-off).
  const unlocked = lock === null;
  const shown = firstRun === null && active !== null && (screen === "home" || screen === "progress") ? screen : null;
  const assessmentOutcome = assessmentView.current !== null && ["completed", "failed", "stopped"].includes(assessmentView.current.state) ? assessmentView.current : null;
  useEffect(() => {
    if (active === null || assessmentOutcome === null) return;
    void runtime.plans.load();
    void runtime.progress.load();
  }, [active, assessmentOutcome, runtime.plans, runtime.progress]);
  useEffect(() => {
    if (shown === null) return;
    if (shown === "home" && unlocked) {
      void runtime.plans.load();
      void runtime.examDates.load(); // the countdown and any reminder due, on this computer only (LRN-44)
      void runtime.companion.loadOpener(); // the Last time card: facts by code, read again on every visit (§19.5.2)
      void runtime.companion.loadCheckins(); // today's check-in, if one is due (§19.5.5)
    }
    void runtime.progress.load();
  }, [runtime, shown, unlocked, progressVersion]);
  useEffect(() => {
    if (session === null || session === "failed") return;
    return session.onProgress(() => setProgressVersion((v) => v + 1));
  }, [session]);
  // Every installed subject's map, for the names of ideas and their lessons (Progress, the plan, search). Each map is
  // asked for once per profile and its reply kept unless the profile changed: a map landing must not re-run this and
  // drop the replies still on their way (each subject's map was read up to five times, one after another, before
  // Home's Ask box opened).
  useEffect(() => {
    if (active === null) return;
    const generation = profiles.generation;
    for (const s of installed) {
      if (maps[s.subjectId] !== undefined || mapsAsked.current.has(s.subjectId)) continue;
      mapsAsked.current.add(s.subjectId);
      profiles.commands.get<unknown>(`/v1/curriculum/${encodeURIComponent(s.subjectId)}/map`).then((raw) => {
        if (generation !== profiles.generation) return;
        setMaps((known) => (known[s.subjectId] !== undefined ? known : { ...known, [s.subjectId]: readSubjectMap(raw) }));
      }).catch(() => {
        if (generation === profiles.generation) mapsAsked.current.delete(s.subjectId);
      });
    }
  }, [profiles, active, installedKey]); // `maps` is read, not watched: see above
  const known = useMemo(() => {
    const topics = new Map<string, string>();
    const topicLinks = new Map<string, { id: string; title: string }>();
    const statements = new Map<string, string>();
    const lessons = new Map<string, string>();
    const states = new Map<string, string>();
    for (const map of Object.values(maps)) {
      for (const section of map.sections) {
        for (const topic of section.topics) {
          for (const kc of topic.kcs) {
            statements.set(kc.kc_id, kc.statement);
            topics.set(kc.kc_id, topic.title);
            topicLinks.set(kc.kc_id, { id: topic.node_id, title: topic.title });
          }
          for (const lesson of topic.lessons) for (const kc of lesson.kc_ids) if (!lessons.has(kc)) lessons.set(kc, lesson.lesson_id);
        }
      }
    }
    for (const subject of progressView.progress?.subjects ?? []) {
      for (const topic of subject.topics) {
        for (const kc of topic.kcs) {
          if (!topics.has(kc.kc_id)) topics.set(kc.kc_id, topic.title);
          states.set(kc.kc_id, kc.state);
        }
      }
    }
    return { topics, topicLinks, statements, lessons, states };
  }, [maps, progressView.progress]);
  const kcSubject = (kcId: string) => subjectName(kcSubjectCode(kcId) ?? "");
  // A question asked while the tutor model was still loading (UXS-12): sent as soon as the tutor is ready.
  const warming = degraded.health?.readiness === "model_loading";
  const [held, setHeld] = useState<{ text: string; generation: number } | null>(null);
  useEffect(() => {
    if (held !== null && held.generation !== profiles.generation) { setHeld(null); return; }
    if (held === null || warming || answers !== true || session === null || session === "failed") return;
    session.sendCapture(typedCapture(held.text, { sessionId: session.sessionId, purpose: "question", confirmed: true }), "ask");
    setHeld(null);
    setScreen("tutor");
  }, [held, warming, answers, session, profiles]);
  useEffect(() => {
    let live = true;
    setVoiceOffer(NO_VOICE);
    // Saved Study tools remain available while the independent tutor session opens.
    if (active === null) return;
    void profiles.commands.get<{ capabilities: string[] }>("/v1/meta").then((meta) => {
      if (live) { setVoiceOffer(voiceOfferFrom(meta.capabilities)); setStudyAvailable(meta.capabilities.includes("study_workspace")); }
    }).catch(() => undefined);
    return () => { live = false; };
  }, [profiles, active]);

  if (firstRun === null && empty) return <Starting failed={false} />;
  if (firstRun?.step === "restore") return <Centred><ProfileRecovery mode="restore" profiles={profiles} chooseArchive={runtime.chooseArchive}
    onDone={() => { void profiles.load().then(async () => {
      if (profiles.getView().profiles.length === 0) setFirstRun({ step: "who" });
      else { await finishSetup(profiles.commands).catch(() => undefined); setFirstRun(null); }
    }); }} /></Centred>;
  if (firstRun?.step === "who") {
    return (
      <Centred>
        <FirstRun profiles={profiles} packs={degraded.packs}
          {...(runtime.chooseArchive === undefined ? {} : { onRestore: () => setFirstRun({ step: "restore" }) })}
          existing={firstRun.resume === true ? view.profiles.map((p) => ({ profileId: p.profile_id, name: p.display_name, avatar: p.avatar })) : []}
          onStarted={(name, curriculum) => {
            // "Start learning" is recorded, so a later launch opens the picker, not first run (UXS-10).
            void finishSetup(profiles.commands).catch(() => undefined);
            // Their own course needs no talk of lessons, a tutor model or exam goals: straight to their materials.
            if (curriculum) setFirstRun({ step: "handoff", name }); else { setFirstRun(null); setScreen("home"); }
          }} />
      </Centred>
    );
  }
  if (view.status === "loading" && active === null) return <Starting failed={false} />;
  if (active === null) {
    return (
      <Centred>
        <ProfilePicker profiles={profiles} chooseArchive={runtime.chooseArchive} onUnlocked={() => setScreen(answers === false ? "learn" : "home")} />
      </Centred>
    );
  }
  const handoffAt = view.profiles.findIndex((p) => p.profile_id === active);
  const handoffDisc = handoffAt < 0 ? undefined : discOf(view.profiles[handoffAt]?.avatar ?? "", handoffAt);
  if (firstRun?.step === "handoff") {
    // The student hand-off (UXS-10): what the tutor can do here, said once, then the first-session interview, then
    // Home (or Learn with no tutor).
    const done = (next: Screen) => {
      setFirstRun(null);
      setScreen(next);
    };
    return (
      <Centred>
        <FirstRunTutor
          name={firstRun.name}
          {...(handoffDisc !== undefined ? { disc: handoffDisc } : {})}
          setup={setup}
          warming={degraded.health?.readiness === "model_loading"}
          onContinue={() => setFirstRun({ step: "tutor", next: answers === false ? "learn" : "home" })}
          onOnlineFeatures={() => done("settings")}
          onDiagnostic={() => done("assessment")}
        />
      </Centred>
    );
  }
  if (firstRun?.step === "tutor") {
    // The interview opens with the one pick of the tutor (UXS-10 step 2, UXS-54): a character and its name in one
    // settings patch, or "Keep Jana"; a core that keeps no tutor choice goes straight on to the questions.
    const next = firstRun.next;
    return (
      <Centred>
        <FirstRunTutorPick profiles={profiles} settings={view.settings} onDone={() => setFirstRun({ step: "interview", next })} />
      </Centred>
    );
  }
  if (firstRun?.step === "interview") {
    // The first-session interview (UXS-10, JRN-03 step 3): scripted questions, the same while the tutor warms up.
    const next = firstRun.next;
    return (
      <Centred>
        <FirstRunInterview commands={profiles.commands} onDone={() => { setFirstRun(null); setScreen(next); }} />
      </Centred>
    );
  }
  const subjectLabel = (subject: string) => t(`settings.profiles.subject.${subject}`);
  /** The lesson part way through in a subject, from the lesson player (UXS-22's Learn offers to continue it). */
  const inProgress = (subjectId: string): LessonInProgress | null => {
    const lesson = lessonView.lesson;
    if (lessonView.status !== "ready" || lesson === null || lesson.node_id.split(".")[1] !== subjectId.split(".")[1]) return null;
    return { lessonId: lesson.lesson_id, title: lesson.title, part: lessonView.index + 1, parts: lesson.blocks.length };
  };
  const askTopic = answers === true && protectedCheck === undefined ? (topic: MapTopic) => {
    const target = topicTarget(topic.node_id, { kind: "ask", ref: topic.node_id });
    if (target === null) return;
    const objective = objectiveForTopic(topic, journey.getView().journey);
    void (async () => {
      if (objective && !await startObjective(objective)) return;
      setFromLesson(null); setFromPractice(false); setFromReport(false);
      setTutorTarget(target);
      setChosenSession(null);
      setScreen("tutor");
    })();
  } : undefined;
  // An open question on the concept map (one that matches no skill) is asked again, in the learner's own words, in a
  // new session of the map's subject.
  const askOpenQuestion = (subjectId: string, text: string) => {
    const code = subjectId.split(".")[1];
    const subject = SUBJECT_NAMES.find((name) => SUBJECT_CODES[name] === code);
    if (subject !== undefined) firstQuestions.start({ subject, content: [{ type: "text", text }] });
  };
  const askLesson = answers === true && protectedCheck === undefined ? (context: AskContext) => {
    const lesson = runtime.lessons.getView().lesson;
    if (lesson === null || lesson.lesson_id !== context.lessonId) return;
    const target = topicTarget(lesson.node_id, { kind: "lesson", ref: context.lessonId });
    if (target === null) return;
    setFromLesson(context.lessonId);
    setFromPractice(false);
    setFromReport(false);
    setTutorTarget(target);
    setChosenSession(null);
    setScreen("tutor");
  } : undefined;
  const chosenSubject = shownTarget === null ? null : installed.find((s) => s.subject === shownTarget.subject && s.level === (shownTarget.level ?? "O"))?.subjectId ?? null;
  const chooseSubject = (subjectId: string) => {
    const pick = installed.find((s) => s.subjectId === subjectId);
    if (pick === undefined) return;
    cancelNewQuestion();
    setFromLesson(null); setFromPractice(false); setFromReport(false);
    setTutorTarget(askTarget(pick.subject, pick.level));
    setChosenSession(null);
  };
  const chooser = <SubjectChooser choices={subjects} value={chosenSubject} subject={shownTarget?.subject ?? ""} onChange={chooseSubject} />;
  /** A session from the sidebar or Home: it reopens in the tutor where it was left (UXS-01, UXS-19). */
  /** The Mock exam place (UXS-25, Ctrl+4): the open sitting if there is one, else the papers and the student's mocks. */
  const openMock = () => {
    if (mockView.exam === null && lock !== null) void runtime.mocks.open(lock.examId);
    else if (mockView.exam === null) void runtime.mocks.load();
    setScreen("mock");
  };
  /** A place that waits while a sitting is open: why, how long is left, and the way back to the mock (AGT-24). */
  const locked = (place: LockedPlace, title: string) => lock === null ? null : (
    <div className="flex min-h-full flex-col" data-locked-place={place}>
      <ScreenHeader><h1 className="text-ui font-semibold text-ink">{title}</h1></ScreenHeader>
      <div className="flex-1 px-8 pt-6 pb-12"><ExamLockNotice lock={lock} now={lockNow} place={place} onOpen={openMock} /></div>
    </div>
  );
  /** "Show me why" on a mock report (UXS-26, AGT-24): a review session in the tutor with the item and the submitted
   * answer loaded (`origin.kind: exam_review`); its header leads back to the report. */
  const showWhy = answers === true ? (reference: string, subject: string, help?: HelpKind) => {
    const name = subjectName(subject);
    if (name === null) return;
    const level = installed.find((s) => s.subject === name)?.level ?? "O";
    const review: TutorTarget = { mode: "guided", subject: name, level, origin: { kind: "exam_review", ref: reference } };
    void SessionController.create(profiles.launch, review, runtime.fetch === undefined ? {} : { fetch: runtime.fetch }).then((sessionId) => {
      // The review session is named for the question it opens on, so it reads as that in the sidebar.
      const number = reference.split(":").pop() ?? "";
      void profiles.commands.patch<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}`, { title: t("mock.review_title", { number }) }, clientId()).catch(() => undefined);
      setTutorTarget(askTarget(name, level));
      setChosenSession(sessionId);
      setFromPractice(false);
      setFromReport(true);
      setScreen("tutor");
      if (help !== undefined) {
        // A help chip on a marked past paper (ASM-28): its request is the session's first message.
        const turn = helpTurn(help);
        const body = { client_message_id: clientId(), intent: turn.intent, content: [{ type: "text", text: t(turn.textKey) }] };
        void profiles.commands.post<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/turns`, body, body.client_message_id).catch(() => undefined);
      }
    }).catch(() => undefined);
  } : undefined;
  /** "Practise these topics": the follow-up set the report made on the weakest ideas (ASM-20). */
  const practiseFromReport = (setRef: string, subject: string) => {
    void runtime.practice.open(setRef, subject);
    setScreen("practice");
  };
  const openSession = (sessionId: string) => {
    cancelNewQuestion();
    if (session !== null && session !== "failed" && session.sessionId === sessionId) {
      // Selecting the already open conversation changes the screen, not its controller. Reopening it can
      // discard an in-flight/failed send's retry map while its optimistic bubble remains in the shared store.
      setFromLesson(null); setFromPractice(false); setFromReport(false); setScreen("tutor");
      return;
    }
    const listed = recent.find((s) => s.session_id === sessionId);
    const subject = listed === undefined ? null : subjectName(listed.subject);
    if (subject !== null) {
      const level = installed.find((s) => s.subject === subject)?.level ?? tutorTarget?.level ?? "O";
      setTutorTarget((current) => current?.subject === subject && current.topic_id === undefined ? current : askTarget(subject, level));
    }
    setChosenSession(sessionId);
    setFromLesson(null);
    setFromPractice(false);
    setFromReport(false);
    setScreen("tutor");
  };
  /** "New question": the tutor workspace with an empty conversation, a new session for the subject (UXS-01). */
  const newQuestion = ownCourse && studyAvailable ? () => { setStudyRequest({ kind: "companion" }); setScreen("study"); }
    : tutorOffered && lock === null && protectedCheck === undefined && screen !== "assessment" ? () => {
    const target = shownTarget ?? (installed[0] !== undefined ? askTarget(installed[0].subject, installed[0].level) : null);
    if (target === null || newQuestionState === "pending") return;
    const fresh = askTarget(target.subject, target.level ?? "O");
    cancelNewQuestion();
    const version = newQuestionVersion.current, generation = profiles.generation;
    setNewQuestionState("pending");
    setTutorTarget(fresh);
    setChosenSession(null);
    setFromLesson(null); setFromPractice(false); setFromReport(false);
    setScreen("tutor");
    void SessionController.create(profiles.launch, fresh, runtime.fetch === undefined ? {} : { fetch: runtime.fetch }).then((sessionId) => {
      if (version !== newQuestionVersion.current || generation !== profiles.generation) return;
      setChosenSession(sessionId);
      setNewQuestionState(null);
    }).catch(() => { if (version === newQuestionVersion.current && generation === profiles.generation) setNewQuestionState("failed"); });
  } : undefined;
  /** Practice (UXS-24): a set for the subject (the ideas chosen, or adaptive), or the set already under way. */
  const openPractice = (subject?: string, kcIds: string[] = []) => {
    const pick = subject ?? shownTarget?.subject ?? installed[0]?.subject;
    const current = runtime.practice.getView();
    // Nothing is started while a mock sitting is open: practice waits with the tutor (AGT-24).
    if (lock === null && pick !== undefined && (kcIds.length > 0 || current.status !== "ready" || current.subject !== pick)) void runtime.practice.start(pick, kcIds);
    setScreen("practice");
  };
  /** In place of the composer when no tutor can answer now (SPEC §10.3): what still works without one. */
  const noTutorNotice = (reason: "offline" | "switched_off") => <NoTutor reason={reason} onPractice={() => openPractice()} onMock={openMock} />;
  /** A help chip (ASM-28): the tutor opens with the item loaded and is asked for that help at once (a hint is the
   * ladder's next rung), and its header leads back to the same question (UXS-24, UXS-01). */
  const openHelp = (reason: "help" | "why_right", first: { content: { type: "text"; text: string }[]; intent: "stuck" | "answer" }) => {
    const subject = runtime.practice.getView().subject;
    void runtime.practice.help(reason).then((sessionId) => {
      if (sessionId === null) return;
      // The same question's help session may be open already: it is asked there; otherwise as soon as it opens.
      if (session !== null && session !== "failed" && session.sessionId === sessionId) session.send(first.content, first.intent);
      else pendingQuestions.current.set(sessionId, first);
      const pick = installed.find((s) => s.subject === subject);
      if (pick !== undefined) setTutorTarget(askTarget(pick.subject, pick.level));
      setChosenSession(sessionId);
      setFromLesson(null);
      setFromPractice(true);
      setFromReport(false);
      setScreen("tutor");
    });
  };
  const helpMe = answers === true && protectedCheck === undefined ? (kind: HelpKind = "hint") => {
    const ask = helpTurn(kind);
    openHelp("help", { content: [{ type: "text" as const, text: t(ask.textKey) }], intent: ask.intent });
  } : undefined;
  /** "Why is this right?" on a correct item (UXS-62): the tutor asks the student to explain first; the mark stays. */
  const whyRight = answers === true && protectedCheck === undefined
    ? () => openHelp("why_right", { content: [{ type: "text" as const, text: t("practice.why_right") }], intent: "answer" })
    : undefined;
  /** The next practice stays with this session's installed topic, never an unrelated adaptive set. */
  const practiceTarget = (sessionId: string) => {
    const chat = runtime.holder.getState().sessions[sessionId];
    const subject = subjectName(chat?.subject ?? "");
    if (lock !== null || subject === null || !chat?.topic_id) return null;
    const pack = installed.find(s => s.subject === subject);
    const topic = pack && maps[pack.subjectId]?.sections.flatMap(s => s.topics).find(t => t.node_id === chat.topic_id);
    return topic?.kcs.length ? { subject, kcs: topic.kcs.map(k => k.kc_id) } : null;
  };
  const followUpHandlers: FollowUpHandlers = {
    offered: (sessionId) => practiceTarget(sessionId) ? ["practise"] : [],
    onChoose: (kind, { sessionId }) => {
      const target = practiceTarget(sessionId);
      if (kind === "practise" && target !== null) openPractice(target.subject, target.kcs);
    },
  };
  /** The Progress place (UXS-27, Ctrl+5). */
  const openProgress = () => setScreen("progress");
  const openAssessment = (start?: AssessmentStart) => {
    if (lock === null && start !== undefined) void runtime.assessments.start(start);
    setScreen("assessment");
  };
  const openJourneyStep = async (next: LearningJourneyView, action: JourneyAction) => {
    const generation = profiles.generation;
    if (action === "review" && next.lesson_id) { setLessonId(next.lesson_id); setScreen("lesson"); }
    else if (next.practice_set_id && (action === "practice" || next.phase === "question")) {
      // Returning from tutor help must retain the current answer draft and feedback.
      if (runtime.practice.getView().setId !== next.practice_set_id) await runtime.practice.open(next.practice_set_id, next.goal.subject);
      if (generation !== profiles.generation) return;
      setScreen("practice");
    }
  };
  const journeyStep = async (action: JourneyAction) => {
    const next = await journey.step(action);
    if (!next) return;
    await openJourneyStep(next, action);
  };
  const startObjective = (objective: LearningObjective) => journey.start(objective.node_id, objective.kc_id, objective.lesson_id);
  const enterObjective = async (objective: LearningObjective, action?: JourneyAction) => {
    const next = await startObjective(objective);
    if (!next) return;
    await journeyStep(action === "practice" && next.phase !== "ready" && next.phase !== "question" ? nextJourneyAction(next) : action ?? nextJourneyAction(next));
  };
  const openLearningLesson = (id: string) => {
    const objective = objectiveForLesson(Object.values(maps), id, journey.getView().journey);
    if (objective) void (async () => {
      if (screen === "study" && !await runtime.studyWorkspace.beginJourney(objective.node_id, objective.kc_id)) return;
      await enterObjective(objective, "review");
    })();
    else { setLessonId(id); setScreen("lesson"); }
  };
  const journeyTopic = () => { const goal = journey.getView().journey?.goal; if (goal) { setMapSubject(goal.node_id.split(".").slice(0, 3).join(".")); setMapTopic(goal.node_id); setScreen("learn"); } };
  const journeySource = () => {
    const source = journey.getView().journey?.source;
    if (source) { setStudyResume(null); setStudySource(source); setScreen("study"); }
  };
  const practiseAfterLesson = () => {
    const lesson = runtime.lessons.getView().lesson;
    const current = journey.getView().journey;
    if (lesson && current?.lesson_id === lesson.lesson_id) {
      if (current.phase === "ready_check") { void journeyStep("check"); return; }
      if (current.phase === "ready" && current.lesson_opened) { void journeyStep("practice"); return; }
      if (current.phase === "question") { void journeyStep("practice"); return; }
    }
    if (lesson) openPractice(kcSubject(lesson.kc_ids[0] ?? "") ?? undefined, lesson.kc_ids);
  };
  /** The deck of a warm-up's due Study cards (SPEC §19.4, `open_study_deck`): Study opens it in its material. */
  const openStudyDeck = (deckId: string) => { setStudyRequest({ kind: "target", deck_id: deckId }); setScreen("study"); };
  /** The skill sheet's links and actions on the learner's map (SPEC §21.1): each opens a place that exists elsewhere.
   * "Ask your tutor about this" is a client deep link to the thread with a draft: no event, nothing sent (§21.3). */
  const toStudy = (request: StudyRequest) => { setStudyRequest(request); setScreen("study"); };
  const mapSheet: ConceptActions = {
    onOpenKnows: () => setScreen("knows-from-learn"),
    ...(studyAvailable ? {
      onOpenMaterial: (m: KcLinks["materials"][number]) => toStudy("aid_id" in m ? { kind: "target", aid_id: m.aid_id } : { kind: "source", source_id: m.source_id }),
      onOpenMoment: (m: KcLinks["moments"][number]) => toStudy({ kind: "source", source_id: m.source_id }),
      onMakeFlashcards: (_node: ConceptNode, links: KcLinks) => {
        const source = links.materials.find((m) => "source_id" in m);
        const aid = links.materials.find((m) => "aid_id" in m);
        if (source !== undefined && "source_id" in source) toStudy({ kind: "flashcards", source_id: source.source_id });
        else if (aid !== undefined && "aid_id" in aid) toStudy({ kind: "target", aid_id: aid.aid_id });
      },
      onResearch: (node: ConceptNode) => toStudy({ kind: "research", topic: node.statement }),
      onAskTutor: (node: ConceptNode) => { setCompanionDraft(t("map.sheet.ask_draft", { statement: node.statement })); toStudy({ kind: "companion" }); },
    } : {}),
  };
  /** A button in **Your tutor** (SPEC §19.3.3): each opens the place it names, with what the engine put in its payload
   * (never anything the model wrote, except the learner's own question handed to the tutor). */
  const companionAction = (action: CompanionAction) => {
    const payload = action.payload;
    switch (action.action) {
      case "open_tutor": {
        // A homework question goes to the guided tutor as a new question (PED-01).
        const question = payload.question?.trim() ?? "";
        const subject = subjectName(shownTarget?.subject ?? "") ?? installed[0]?.subject ?? null;
        if (ownCourse) { setStudyRequest({ kind: "companion" }); setScreen("study"); return; }
        if (question === "" || !tutorOffered || lock !== null || protectedCheck !== undefined || subject === null) { setScreen("tutor"); return; }
        firstQuestions.start({ subject, content: [{ type: "text", text: question }] });
        return;
      }
      case "open_practice": {
        const kcIds = payload.kc_ids ?? [];
        openPractice(kcIds[0] === undefined ? undefined : kcSubject(kcIds[0]) ?? undefined, kcIds);
        return;
      }
      case "open_study_job":
        setStudyRequest({ kind: "target", ...(payload.job_id !== undefined ? { job_id: payload.job_id } : {}), ...(payload.aid_id !== undefined ? { aid_id: payload.aid_id } : {}) });
        setScreen("study");
        return;
      case "open_study_deck":
        if (payload.deck_id !== undefined) openStudyDeck(payload.deck_id);
        return;
      case "open_moment":  // SPEC §20.4: the material at the moment the engine resolved from a study_search hit
        if (payload.source_id !== undefined && payload.at_ms !== undefined) { setStudyRequest({ kind: "moment", source_id: payload.source_id, at_ms: payload.at_ms }); setScreen("study"); }
        return;
      case "open_add_source":
        setStudyRequest({ kind: "add" });
        setScreen("study");
        return;
      case "open_research":
        setStudyRequest({ kind: "research", topic: payload.question ?? "" });
        setScreen("study");
        return;
      case "open_plan":
        setPlanExpanded(true);
        setScreen("home");
        return;
      case "open_knows":
        setScreen("knows-from-study");
        return;
    }
  };
  /** A plan activity (UXS-12, LRN-28): reviews and practice open a practice set on its ideas, the main activity a lesson
   * when its idea is not started yet (then practice), a timed paper the mock place, and the look back Progress. */
  const openBlock = (block: PlanBlock | null) => {
    if (block === null) return;
    const deck = block.study_cards?.[0]?.deck_id;
    if (deck !== undefined && studyAvailable) return openStudyDeck(deck);
    const kc = block.kc_ids[0];
    if (block.kind === "reflect") return openProgress();
    if (block.kind === "mock") return openMock();
    if (block.kind === "mastery_check" && kc !== undefined) {
      const subject = kcSubject(kc);
      if (subject !== null) openAssessment({ kind: "mastery_check", subject, kc_id: kc });
      return;
    }
    const lesson = kc === undefined ? undefined : known.lessons.get(kc);
    if (block.kind === "main" && block.reason_code !== "mock_follow_up" && kc !== undefined) {
      const objective = objectiveForKc(Object.values(maps), kc);
      if (objective) {
        const action = (known.states.get(kc) ?? "not_started") === "not_started" && lesson !== undefined ? "review" : undefined;
        void enterObjective(objective, action);
        return;
      }
    }
    if (block.kind === "main" && block.reason_code !== "mock_follow_up" && kc !== undefined && (known.states.get(kc) ?? "not_started") === "not_started" && lesson !== undefined) {
      setLessonId(lesson);
      setScreen("lesson");
      return;
    }
    openPractice(kc === undefined ? undefined : kcSubject(kc) ?? undefined, block.kc_ids);
  };
  const planNames = { topic: (kc: string) => known.topics.get(kc) ?? null, started: (kc: string) => (known.states.get(kc) ?? "not_started") !== "not_started" };
  const planCard = lock !== null ? undefined : (
    <PlanCard state={planView} names={planNames}
      streak={progressView.progress === null ? null : { days: progressView.progress.streak.days, freezes: progressView.progress.streak.freezes_left }}
      actions={{
        onStart: () => void runtime.plans.start().then((ok) => { if (ok) openBlock(nextBlock(runtime.plans.getView().plan)); }),
        onChange: (choice) => void runtime.plans.change(choice),
        onNotToday: () => void runtime.plans.notToday(),
        onContinue: openBlock,
        onKeepLearning: () => void runtime.plans.propose().then(() => runtime.plans.start()).then((ok) => { if (ok) openBlock(nextBlock(runtime.plans.getView().plan)); }),
        onAnother: () => void runtime.plans.propose(),
        onRetry: () => void runtime.plans.load(),
        ...(studyAvailable ? { onStudyCards: openStudyDeck } : {}),
      }} />
  );
  const journeyCard = active && lock === null && protectedCheck === undefined && (["home", "learn", "progress"].includes(screen)
          || (screen === "lesson" && lessonId === journeyView.journey?.lesson_id)
          || (screen === "practice" && practiceView.setId === journeyView.journey?.practice_set_id)
          || (screen === "study" && savedStudy.source?.source_id === journeyView.journey?.source?.source_id)
          || (screen === "tutor" && !fromReport && (fromLesson === journeyView.journey?.lesson_id || (fromPractice && practiceView.setId === journeyView.journey?.practice_set_id) || shownTarget?.topic_id === journeyView.journey?.goal.node_id))) && <LearningJourney view={journeyView.journey} busy={journeyView.busy} error={journeyView.error}
          place={screen === "lesson" && lessonId === journeyView.journey?.lesson_id ? "lesson" : screen === "practice" && practiceView.setId === journeyView.journey?.practice_set_id ? "practice" : screen === "tutor" ? "tutor" : "overview"}
          {...(screen !== "home" && planView.plan?.status === "started" && planView.plan.blocks.some((b) => b.kc_ids.includes(journeyView.journey?.goal.kc_id ?? "")) ? { onPlan: () => { setPlanExpanded(true); setScreen("home"); } } : {})}
          {...(helpMe && journeyView.journey?.practice_set_id ? { onHelp: () => {
            const current = journey.getView().journey;
            const setId = current?.practice_set_id;
            if (current && setId) void (async () => {
              if (runtime.practice.getView().setId !== setId) await runtime.practice.open(setId, current.goal.subject);
              helpMe();
            })();
          } } : {})}
          onStep={(action) => { void journeyStep(action); }} onSource={journeySource} onTopic={journeyTopic} onRetry={() => { void journey.load(); }} />;
  // Read-aloud and dictation everywhere; the voice conversation only where `place` names an open chat: Ask anything on
  // Home, a study set's Chat tab (VOC-53, D130), or the tutor while Cloud voice is on. Lessons and notes have no bar.
  const cloudVoiceOn = connectivity.features.some((f) => f.feature === "cloud_voice" && f.enabled);
  const withSpeech = (children: React.ReactNode, target = session, place: VoicePlace | null = null, onAnswer?: (answer: VoiceAnswer) => void) => target === null || target === "failed" ? <Opening failed={target === "failed"} /> :
    <SpeechTools key={`${target.sessionId}:${screen}`} profiles={profiles} sessionId={target.sessionId} launch={profiles.launch} holder={runtime.holder} voice={voiceOffer} automatic={screen === "tutor"} place={place} {...(onAnswer !== undefined ? { onAnswer } : {})}>{children}</SpeechTools>;
  const me = view.profiles.findIndex((p) => p.profile_id === active);
  const mine = view.profiles[me];
  const profile = mine === undefined ? null : { name: mine.display_name, disc: discOf(mine.avatar, me),
    ...(record !== null && record.subjects.length > 0 ? { detail: t("frame.menu.detail", { subjects: record.subjects.map(subjectLabel).join(", "), level: t(`settings.profiles.level_name.${record.level.toLowerCase()}`) }) } : {}) };
  const openSettingsAt = (id: string) => { setSettingsAt({ id, at: Date.now() }); setScreen("settings"); };
  const home = () => {
    const notice = answers === false && !ownCourse ? (
      // No tutor answers here: lessons and practice instead of an Ask box (UXS-12 content-only state, PLT-17).
      <div className="flex flex-col items-start gap-4">
        <Notice message={setup.kind === "no_model" ? t("first_run.tutor.no_model") : t("first_run.tutor.content_only")} />
        <Button tone="primary" onClick={() => setScreen("learn")}>{t("home.browse_topics")}</Button>
      </div>
    ) : undefined;
    if (answers !== false && session === "failed" && lock === null && !ownCourse) return <Opening failed />;
    // Home shows at once (UXS-12); while the subject's session opens, the Ask box is its quiet frame.
    const waiting = <ComposerShell send={<IconButton icon="send" tone="action" disabled label={t("action.send")} />}><span className="block py-2"><Skeleton lines={1} /></span></ComposerShell>;
    // While the model loads the Ask box still takes the question and keeps it until the tutor is ready (UXS-12).
    const percent = degraded.health?.model_load_progress;
    const warmingProps = warming ? {
      placeholder: percent === undefined ? t("home.warming_indeterminate") : t("home.warming", { percent }),
      hold: (text: string) => setHeld({ text, generation: profiles.generation }),
    } : {};
    // The one input (UXS-67): the core says where the words go. A problem or question in an installed subject goes to
    // the tutor as before; material opens the study area; any other question is answered by Ask anything below the
    // box (STU-20). When the core cannot say, the tutor takes it, as before.
    const routeHome = async (text: string): Promise<boolean> => {
      const decision = await runtime.ask.route(text);
      if (decision === null || decision.route === "tutor") { runtime.ask.clear(); return true; }
      if (decision.route === "study") {
        if (!studyAvailable) return true;
        if (decision.reason === "material") void runtime.studyWorkspace.addText(text.trim().split(/\s+/).slice(0, 6).join(" "), text);
        runtime.ask.clear();
        setScreen("study");
        return false;
      }
      void runtime.ask.ask(text);
      return false;
    };
    const ask = lock !== null || protectedCheck !== undefined ? null : ownCourse ? <CourseAsk onAsk={(text) => { setCompanionDraft(text); setStudyRequest({ kind: "companion" }); setScreen("study"); }} /> : answers === false ? <SearchBox onSearch={(query) => { void runtime.search.search(query); setScreen("search"); }} />
      : noTutor !== null ? noTutorNotice(noTutor)
      : session === null || session === "failed" ? waiting
      : withSpeech(<Composer sessionId={session.sessionId} asking placement="inline" onSent={() => { setFromLesson(null); setFromPractice(false); setFromReport(false); setScreen("tutor"); }} route={routeHome} {...warmingProps} />,
        session, ASK_PLACE, (answer) => runtime.ask.voiced(answer));
    const openStudy = (add: "paste" | null = null) => { runtime.ask.clear(); setStudyAdd(add); setScreen("study"); };
    const materials = studyAvailable && lock === null ? {
      onUpload: (file: File) => { void runtime.studyWorkspace.upload(file, undefined, true); openStudy(); },
      onPaste: () => openStudy("paste"),
      onRecord: (file: File) => { void runtime.studyWorkspace.upload(file, undefined, true); openStudy(); },
    } : null;
    // Ask anything's answer above the box; "Ask the tutor" sends the same question there (held until it is ready).
    const answer = lock !== null ? undefined : <AskAnswer view={askView} actions={{
      // A question spoken here that the tutor answered by voice is already in its session: "Continue in the tutor"
      // opens it on the board (D130); otherwise the question is sent there (held until the tutor is ready).
      onTutor: () => { const text = askView.status === "spoken" ? "" : askView.question; runtime.ask.clear(); if (text !== "") setHeld({ text, generation: profiles.generation }); else setScreen("tutor"); },
      onStudy: () => openStudy(),
      onExplain: () => runtime.ask.explain(),
      onAgain: (options) => void runtime.ask.ask(askView.question, options),
    }} />;
    const heldNote = held === null ? null : <p role="status" data-held-question="" className="px-1 text-meta text-muted">{t("home.question_held")}</p>;
    const newest = mockView.sittings[0];
    const today = Date.now();
    return (
      <Home
        materialsOpen={ownCourse}
        name={profile?.name ?? ""}
        ask={ask}
        materials={materials}
        answer={answer}
        notice={lock !== null ? <ExamLockNotice lock={lock} now={lockNow} place="tutor" onOpen={openMock} /> : <>{heldNote}{notice}</>}
        /* Worked-solution search and the diagnostic are quiet rows under the main actions (the crowding review of 2026-11-01). */
        more={ownCourse ? [] : [
          { id: "search", icon: "search", label: t("search.title"), onClick: () => setScreen("search") },
          { id: "diagnostic", icon: "mastery-nearly", label: t(assessmentView.assessments.some((a) => ["in_progress", "waiting", "stopped"].includes(a.state)) ? "assessment.finish" : "assessment.find_start"), onClick: () => openAssessment() },
        ]}
        plan={<div className="flex flex-col gap-4">
          {journeyCard && <div className="[&>section]:mx-0 [&>section]:my-0">{journeyCard}</div>}
          {savedStudy.journey && <details open={!journeyView.journey}><summary className="cursor-pointer text-meta text-muted">{t("study.journey.title")}</summary><div className="mt-3"><StudyJourney journey={savedStudy.journey} busy={savedStudy.busy} onReviewDue={() => { void runtime.studyWorkspace.load(); }} onContinue={() => { setStudyResume("next"); setScreen("study"); }} onLesson={() => { setStudyResume("lesson"); setScreen("study"); }} onResults={() => { setStudyResume("quiz"); setScreen("study"); }} /></div></details>}
          {ownCourse && studyAvailable ? <DueToday
            sets={savedStudy.sets.flatMap((set) => {
              const due = set.progress !== undefined && "cards_due_today" in set.progress ? set.progress.cards_due_today : 0;
              return due > 0 ? [{ aid_id: set.aid_id, due, ...(set.title !== undefined ? { title: set.title } : {}) }] : [];
            })}
            hasCards={savedStudy.sets.some((set) => set.kind === "flashcards")}
            onReview={(aidId) => toStudy({ kind: "target", aid_id: aidId })} /> : null}
          {ownCourse ? null : journeyView.journey || savedStudy.journey ? <details open={planExpanded} onToggle={(event) => setPlanExpanded(event.currentTarget.open)}><summary className="cursor-pointer text-meta text-muted">{t("study.journey.curriculum_plan")}</summary><div className="mt-3">{planCard}</div></details> : planCard}
        </div>}
        {...(goalTarget > 0 && planView.plan !== null && !ownCourse ? { goal: <DailyGoalChip progress={goalProgress(planView.plan, goalTarget)} onChange={(target) => void runtime.goal.set(target)} /> } : {})}
        countdown={<ExamCountdownCard countdown={examDatesView.countdown} reminders={examDatesView.reminders} onDismiss={(r) => void runtime.examDates.dismiss(r)} onOpen={openMock} />}
        lastTime={<LastTimeCard view={companionView} {...(studyAvailable ? { onOpenStudy: () => openStudy() } : {})} />}
        checkin={lock !== null ? undefined : <CheckinCard view={checkinView} onDismiss={(id) => void runtime.companion.dismissCheckin(id)} onOpen={(id) => {
          // **Your tutor** opens with the check-in as its opener (SPEC §19.5.5): Study's library panel, or its tab.
          runtime.ask.clear();
          studyCheckin.current = id;
          setStudyRequest({ kind: "companion" });
          setScreen("study");
        }} />}
        {...(newest?.state === "abandoned" ? { mock: <AbandonedMockCard onNew={openMock} /> } : {})}
        subjects={ownCourse ? [] : installed.map((s) => ({ subjectId: s.subjectId, subject: s.subject, label: subjectLabel(s.subject), chosen: s.subjectId === chosenSubject }))}
        onSubject={chooseSubject}
        pickup={answers === false ? [] : recent.map((s) => ({
          sessionId: s.session_id, title: s.title !== "" ? s.title : t("composer.new_question"), subject: s.subject, subjectLabel: subjectLabel(s.subject),
          day: groupSessions([s], today)[0]?.group ?? "older",
        }))}
        onPickup={openSession}
        practice={{ ...due, subjectLabel }}
        onPractice={(idea) => openPractice(idea.subject, [idea.kc.kc_id])}
      />
    );
  };
  const main = () => {
    if (screen === "home") return home();
    if (screen === "mock") {
      return (
        <MockPlace controller={runtime.mocks} subjects={installed.map((s) => ({ subject: s.subject, label: subjectLabel(s.subject) }))}
          extraPercent={view.settings?.extra_time_percent ?? 0} names={reportNames} onPractise={practiseFromReport} pastPapers={runtime.pastPapers} examDates={runtime.examDates}
          onPractiseTopic={(topicId, subject) => {
            // A report's topic is a map topic or, for a paper marked by section, a whole section of the map: its ideas
            // are those of the topics under it. Nothing found still opens practice in the subject, never a dead button.
            const sections = Object.values(maps).flatMap((m) => m.sections);
            const topics = sections.find((sec) => sec.node_id === topicId)?.topics ?? sections.flatMap((sec) => sec.topics).filter((tp) => tp.node_id === topicId);
            openPractice(subjectName(subject) ?? undefined, topics.flatMap((tp) => tp.kcs.map((k) => k.kc_id)));
          }} {...(showWhy !== undefined ? { onShowWhy: (reference: string, subject: string) => showWhy(reference, subject), onHelp: (reference: string, kind: HelpKind, subject: string) => showWhy(reference, subject, kind) } : {})} />
      );
    }
    if (lock !== null) {
      if (screen === "study") return locked("notes", t("study.title"));
      if (screen === "notes") return locked("notes", t("notes.title"));
      if (screen === "history") return locked("notes", t("notes.source_turn"));
      if (screen === "saved-questions") return locked("notes", t("saved_questions.title"));
      if (screen === "practice") return locked("practice", t("home.practice"));
      if (screen === "assessment") return locked("assessment", t("assessment.mastery"));
      if (screen === "lesson") return locked("lessons", t("app.learn"));
      if (screen === "tutor" && tutorOffered) return locked("tutor", t("mock.lock.heading"));
    }
    if (protectedCheck !== undefined && (screen === "tutor" || screen === "search" || screen === "study" || screen === "history")) return <>
      <ScreenHeader><h1 className="text-ui font-semibold text-ink">{t("assessment.mastery")}</h1></ScreenHeader>
      <main className="flex flex-col items-start gap-4 p-6"><Notice message={t("assessment.no_help")} />
        <Button onClick={() => { void runtime.assessments.open(protectedCheck.assessment_id); setScreen("assessment"); }}>{t("assessment.continue")}</Button></main>
    </>;
    if (screen === "assessment") return <AssessmentPlace controller={runtime.assessments} subjects={installed.map((s) => s.subject)}
      names={{ subject: subjectLabel, topic: (id) => Object.values(maps).flatMap((m) => m.sections.flatMap((s) => s.topics)).find((tp) => tp.node_id === id)?.title ?? id, kc: (id) => known.statements.get(id) ?? id }}
      plan={planCard} onClose={() => setScreen("home")} />;
    if (screen === "saved-questions") return <SavedQuestions header={notesHeader()} controller={runtime.savedQuestions} onClose={() => setScreen("home")} subject={subjectLabel}
      topic={(id) => Object.values(maps).flatMap((m) => m.sections.flatMap((s) => s.topics)).find((tp) => tp.node_id === id)?.title ?? id} />;
    if (screen === "progress") {
      return (
        <Progress controller={runtime.progress} version={progressVersion} ownCourse={ownCourse}
          map={{ controller: runtime.overview, layer: mapLens.layer, onLayer: (layer) => runtime.conceptMap.setLayer(layer),
            onOpen: (subjectId) => { setMapSubject(subjectId); setMapTopic(null); setMapFirst(true); setScreen("learn"); },
            tileOf: (kc) => Object.values(maps).flatMap((m) => m.sections.flatMap((s) => s.topics)).find((tp) => tp.kcs.some((k) => k.kc_id === kc))?.node_id ?? null }}
          names={{ subject: subjectLabel, kc: (kc) => known.statements.get(kc) ?? null }}
          actions={{ onCheck: (kc) => { const subject = kcSubject(kc); if (subject !== null) openAssessment({ kind: "mastery_check", subject, kc_id: kc }); }, onPractise: (kc) => { const subject = kcSubject(kc); if (subject !== null) openPractice(subject, [kc]); }, onKnows: () => setScreen("knows-from-progress"), onNotes: () => setScreen("notes"), onHistory: () => setScreen("session-history"), onLearn: () => setScreen("learn"), onDiagnostic: () => openAssessment() }} />
      );
    }
    if (screen === "session-history") return <SessionHistory controller={runtime.history} subject={subjectLabel} onClose={() => setScreen("progress")} />;
    if (screen === "search") {
      if (lock !== null) return locked("practice", t("search.title"));
      return (
        <SearchScreen controller={runtime.search}
          names={{ topic: (kc) => known.topics.get(kc) ?? null, subject: (kc) => kcSubject(kc) }}
          actions={{ onBrowse: () => setScreen("learn"), onPractise: (kcIds, subject) => openPractice(subject, kcIds) }} />
      );
    }
    if (screen === "settings") return <Settings section={settingsAt} voiceSettings={<VoiceSettings profiles={profiles} titled={false} />} profiles={profiles} {...(runtime.notifier !== undefined ? { notifier: runtime.notifier } : {})} chooseSource={runtime.chooseSource} notices={NOTICES} assets={ASSETS} onClose={() => {
      const back = studySettingsReturn.current;
      studySettingsReturn.current = null;
      if (back && back.profile === active) {
        void runtime.studyWorkspace.loadAccess().then(async () => {
          if (back.source && profiles.getView().active === back.profile) await runtime.studyWorkspace.open(back.source);
        });
        setScreen("study");
      } else setScreen(tutorOffered ? "tutor" : "learn");
    }} onOpenKnows={() => setScreen("knows-from-settings")} />;
    if (screen === "knows" || screen === "knows-from-settings" || screen === "knows-from-progress" || screen === "knows-from-study" || screen === "knows-from-learn") {
      const back = screen === "knows" ? "tutor" : screen === "knows-from-progress" ? "progress" : screen === "knows-from-study" ? "study" : screen === "knows-from-learn" ? "learn" : "settings";
      return <KnowsPage knows={runtime.knows} onClose={() => setScreen(back)} onOpenSettings={() => setScreen("settings")} />;
    }
    if (screen === "study") {
      const workspace = <StudyWorkspace controller={runtime.studyWorkspace} curriculum={!ownCourse} lastTime={<LastTimeCard view={companionView} />}
        {...(companionThread.status !== "unavailable" ? { companion: (place: "panel" | "tab") => <CompanionThread companion={runtime.companion} onAction={companionAction} place={place} draft={companionDraft} onDraftShown={() => setCompanionDraft(null)} />,
          // Ask about this moment (SPEC §20.5 step 4): the thread opens with the moment as its pending context; nothing is sent.
          onAskMoment: (source: { source_id: string; title: string }, at_ms: number) => { void runtime.companion.openWithContext({ source_id: source.source_id, at_ms }, source.title); setStudyRequest({ kind: "companion" }); } } : {})}
        onSkill={(kc) => { const subject = kcSubject(kc); if (subject) { setMapSubject(subject); setMapTopic(null); setMapFirst(true); setScreen("learn"); } }} skillName={(kc) => known.statements.get(kc) ?? null} request={studyRequest} onRequestDone={() => setStudyRequest(null)} initialAdd={studyAdd} initialJourney={studyResume} onJourneyOpened={() => setStudyResume(null)} initialSource={studySource} onSourceOpened={() => setStudySource(null)} initialResearch={studyResearch} onResearchOpened={() => setStudyResearch(null)} onJourneyStarted={() => {
        void journey.load().then((next) => { if (next) void journeyStep(nextJourneyAction(next)); });
      }} onTab={setStudyTab} onPractice={(subject, kcIds) => {
        const objective = kcIds[0] ? objectiveForKc(Object.values(maps), kcIds[0]) : null;
        if (objective) void runtime.studyWorkspace.beginJourney(objective.node_id, objective.kc_id).then((ok) => { if (ok) void enterObjective(objective, "practice"); }); else openPractice(subject, kcIds);
      }} onReviewAccess={() => {
        studySettingsReturn.current = { profile: active, source: runtime.studyWorkspace.getView().source?.source_id ?? null };
        openSettingsAt("online_features");
      }} />;
      if (session === null || session === "failed") return workspace;
      // The Chat tab holds the voice conversation about the open source (VOC-53, D130).
      return <StudyPlace controller={runtime.studyWorkspace} chat={studyTab === "chat"}>{(place) =>
        withSpeech(workspace, session, place, (answer) => void runtime.studyWorkspace.voiced(answer))}</StudyPlace>;
    }
    // One header with one action: the lists by default, the editor only on New note (live UX review, 2026-11-01).
    if (screen === "notes") return withSpeech(<NotesPlace header={notesHeader} editor={(done) => <StudyNotes embedded show="editor" onDone={done} />}>
      <SessionNotes controller={runtime.sessionNotes} subject={subjectLabel} kc={(id) => known.statements.get(id) ?? id} topicForKc={(id) => known.topicLinks.get(id) ?? null} below={<StudyNotes embedded show="list" />} onReference={async (reference) => {
        if (lock !== null || protectedCheck !== undefined) return false;
        const generation = profiles.generation;
        try {
          const snapshot = await profiles.commands.get<api_commands.Snapshot>(`/v1/sessions/${encodeURIComponent(reference.session_id)}/snapshot`);
          if (generation !== profiles.generation || errors("api/commands#/$defs/Snapshot", snapshot).length > 0
            || !snapshot.messages.some((m) => "turn_id" in m && m.turn_id === reference.turn_id)) return false;
          const subject = subjectName(snapshot.session.subject);
          if (subject === null) return false;
          setTutorTarget(askTarget(subject, installed.find((s) => s.subject === subject)?.level ?? "O"));
          setChosenSession(reference.session_id); setNoteReference(reference); setScreen("history");
          return true;
        } catch { return false; }
      }} />
    </NotesPlace>);
    if (screen === "history") return <>
      <ScreenHeader end={<Button onClick={() => setScreen("notes")}>{t("notes.back")}</Button>}><h1 className="text-ui font-semibold text-ink">{t("notes.source_turn")}</h1></ScreenHeader>
      {session === null || session === "failed" ? <Opening failed={session === "failed"} /> : <Transcript sessionId={session.sessionId} {...(noteReference !== null ? { focusTurnId: noteReference.turn_id } : {})} />}
    </>;
    if (screen === "learn" || (screen === "tutor" && !tutorOffered)) {
      return shownSubject === null ? <>
        <ScreenHeader><h1 className="text-ui font-semibold text-ink">{t("app.learn")}</h1></ScreenHeader>
        <main className="flex flex-col items-start gap-4 p-6">
          <Notice message={t("map.not_installed")} />
          <Button onClick={() => setScreen("settings")}>{t("app.settings")}</Button>
        </main>
      </> : (
        <SubjectMap key={`${shownSubject}:${mapTopic ?? ""}:${mapFirst ? "map" : "tiles"}`}
          conceptFirst={mapFirst}
          {...(mapTopic !== null ? { initialTopic: mapTopic } : {})}
          controller={runtime.map}
          concept={runtime.conceptMap}
          sheet={mapSheet}
          {...(answers === true && protectedCheck === undefined ? { onAskQuestion: (text: string) => askOpenQuestion(shownSubject, text) } : {})}
          subjectId={shownSubject}
          subjects={subjects}
          onSubject={setMapSubject}
          inProgress={inProgress(shownSubject)}
          onContinue={openLearningLesson}
          actions={{
            onJourney: (topic, kc) => { void enterObjective({ node_id: topic.node_id, kc_id: kc.kc_id }); },
            onStartLesson: openLearningLesson,
            onPractise: (topic) => { const objective = objectiveForTopic(topic, journey.getView().journey); if (objective) void enterObjective(objective, "practice"); },
            onMasteryCheck: (kc) => { const subject = kcSubject(kc.kc_id); if (subject !== null) openAssessment({ kind: "mastery_check", subject, kc_id: kc.kc_id }); },
            ...(askTopic !== undefined ? { onAsk: askTopic } : {}),
          }}
        />
      );
    }
    if (screen === "practice") {
      return <Practice controller={runtime.practice} subjectLabel={subjectLabel} onEnd={() => setScreen("home")} onLearn={() => setScreen("learn")} {...(helpMe !== undefined ? { onHelp: helpMe } : {})} {...(whyRight !== undefined ? { onWhyRight: whyRight } : {})} />;
    }
    if (screen === "lesson" && lessonId !== null) {
      return withSpeech(<LessonPlayer controller={runtime.lessons} lessonId={lessonId} teaching={journeyView.journey?.lesson_id === lessonId && journeyView.journey.lesson_opened} onClose={() => setScreen("learn")} onPractise={practiseAfterLesson} {...(askLesson !== undefined ? { onAsk: askLesson } : {})} />);
    }
    return session === null || session === "failed" ? <Opening failed={session === "failed"} /> : withSpeech(
      <FollowUpProvider value={followUpHandlers}><TutorScreen sessionId={session.sessionId} subjectControl={chooser} noTutor={noTutor !== null ? noTutorNotice(noTutor) : undefined}
        onEnd={async () => { const ok = await runtime.sessionNotes.end(session.sessionId); if (ok) setScreen("notes"); return ok; }}
        {...(fromReport ? { back: { label: t("tutor.back_to_report"), onClick: () => setScreen("mock") }, note: t("mock.review_note") }
          : fromLesson ? { back: { label: t("journey.back_lesson"), onClick: () => { setLessonId(fromLesson); setScreen("lesson"); } } }
          : fromPractice && practiceView.status === "ready" ? { back: { label: t("tutor.back_to_practice", { number: practiceView.number }), onClick: () => setScreen("practice") } } : {})} /></FollowUpProvider>, session,
      cloudVoiceOn ? TUTOR_PLACE : null);
  };
  // The place's one header, as every other place has: its name, then the switch between its two lists (and, in a
  // narrow window, the menu button ScreenHeader adds), and at most one action at the end (New note, or the way back
  // from the editor).
  const notesHeader = (end?: React.ReactNode) => (
    <ScreenHeader {...(end === undefined || end === null ? {} : { end })}>
      <h1 className="text-ui font-semibold text-ink">{t("nav.notes_saved")}</h1>
      <div role="group" aria-label={t("nav.notes_saved")} className="flex gap-1">
        {(["notes", "saved-questions"] as const).map((place) => (
          <Button key={place} size="sm" tone="ghost" aria-pressed={screen === place} onClick={() => setScreen(place)}>
            {t(place === "notes" ? "notes.title" : "saved_questions.title")}
          </Button>
        ))}
      </div>
    </ScreenHeader>
  );
  const places: FramePlace[] = [
    { id: "home", icon: "home", label: t("home.title"), current: screen === "home", onSelect: () => setScreen("home") },
    ...(studyAvailable ? [{ id: "study", icon: "study-area", label: t("nav.materials"), short: t("nav.materials_short"), group: t("nav.your_learning"), current: screen === "study", onSelect: () => setScreen("study") } satisfies FramePlace] : []),
    { id: "progress", icon: "progress", label: t("app.progress"), group: t("nav.your_learning"), current: screen === "progress" || screen === "knows-from-progress" || screen === "session-history", onSelect: openProgress },
    // Study notes and saved questions are one place with a switch (owner, 2026-10-19).
    { id: "notes", icon: "notes", label: t("nav.notes_saved"), short: t("nav.notes_short"), group: t("nav.your_learning"), current: screen === "notes" || screen === "saved-questions" || screen === "history", onSelect: () => setScreen("notes") },
    ...(ownCourse ? [] : [{ id: "learn", icon: "learn", label: t("nav.lessons"), group: t(installed.every((s) => s.level === "O") ? "nav.o_level" : "nav.curriculum"), current: screen === "learn" || screen === "lesson" || (screen === "tutor" && !tutorOffered), onSelect: () => setScreen("learn") },
    { id: "practice", icon: "practice", label: t("app.practice"), group: t(installed.every((s) => s.level === "O") ? "nav.o_level" : "nav.curriculum"), current: screen === "practice", onSelect: () => openPractice() },
    { id: "mock", icon: "mock", label: t("app.mock"), group: t(installed.every((s) => s.level === "O") ? "nav.o_level" : "nav.curriculum"), current: screen === "mock", onSelect: openMock },] satisfies FramePlace[]),
  ];
  const sidebar: SidebarContent = {
    places,
    ...(newQuestion !== undefined ? { onNewQuestion: newQuestion } : {}),
    sessions: (tutorOffered && protectedCheck === undefined && screen !== "assessment" ? groupSessions(recent, Date.now()) : []).map(({ group, sessions }) => ({
      group,
      items: sessions.map((s) => ({
        id: s.session_id, title: s.title !== "" ? s.title : t("composer.new_question"), subject: s.subject, subjectLabel: subjectLabel(s.subject),
        current: screen === "tutor" && s.session_id === openId,
      })),
    })),
    onOpenSession: openSession,
    profile,
    onSwitch: () => void profiles.lock(),
    onSettings: () => { setSettingsAt(null); setScreen("settings"); },
    settingsCurrent: screen === "settings" || screen === "knows-from-settings",
    menu: {
      onLanguage: () => openSettingsAt("accessibility_language"),
      onHelp: () => openSettingsAt("help"),
      onKeys: () => setKeysHelp(true),
      plan: { label: t(online ? "frame.menu.plan_online" : "frame.menu.plan_free"), onOpen: () => openSettingsAt("online_features") },
      onBackup: () => openSettingsAt("data_export"),
      onOpen: () => void readLicence(profiles.commands).then((licence) => setOnline(licence.entitlements.cloud_tier === "subscribed")).catch(() => undefined),
    },
  };
  // The screens that bring their own header (Home, the tutor) leave the drawer's menu button to it.
  const ownHeader = screen === "home" || screen === "learn" || screen === "lesson" || screen === "practice" || screen === "mock" || screen === "settings" || screen === "tutor"
    || screen === "progress" || screen === "search" || screen === "assessment" || screen === "saved-questions" || screen === "history"
    || screen === "session-history" || screen === "notes";
  const scrolls = screen === "learn" || screen === "lesson" || screen === "practice" || screen === "mock" || screen === "settings" || screen === "knows" || screen === "knows-from-settings"
    || screen === "knows-from-progress" || screen === "progress" || screen === "session-history" || screen === "search" || screen === "assessment" || screen === "saved-questions" || screen === "study" || screen === "notes"
    || (screen === "tutor" && (!tutorOffered || lock !== null));
  const frame = (
    <StoreProvider holder={runtime.holder} session={session === "failed" ? null : session}>
      {/* Ctrl+1 to Ctrl+5: Home, Learn, Practice, Mock exam and Progress (TABLE LOC-12a). */}
      <FrameKeys places={[() => setScreen("home"), () => setScreen("learn"), () => openPractice(), openMock, openProgress]} onSettings={() => setScreen("settings")} characterKeys={characterKeys} help={keysHelp} onHelp={setKeysHelp} />
      <StudioFrame
        sidebar={({ variant, onToggle, onDone }) => <Sidebar variant={variant} onToggle={onToggle} onDone={onDone} content={sidebar} />}
        tabs={({ onMore }) => <TabBar places={places} onMore={onMore} />}
      >
        {!ownHeader && <MenuBar />}
        <DegradedNotices onOpenLessons={() => setScreen("learn")} onOnlineFeatures={() => setScreen("settings")} health={degraded.health} packs={degraded.packs} models={degraded.models} subject={shownTarget?.subject ?? ""} />
        {screen !== "home" && journeyCard}
        <div data-app-screen={screen} className={scrolls ? "min-h-0 flex-1 overflow-y-auto" : "flex min-h-0 flex-1 flex-col"}>{main()}</div>
      </StudioFrame>
    </StoreProvider>
  );
  // The memory-use line and a saved note's announcement link to the knows page (MEM-28, MEM-11).
  // The tutor's picture the student chose marks the tutor's turns (Settings → Tutor, PRM-03).
  // The not-sure reply's "Open lesson" opens that lesson in the player (UXS-20, PRD-05).
  const openLesson = openLearningLesson;
  // "Use the faster tutor" (UXS-03, MDL-05): offered where the profile's settings carry the choice (`faster_tutor`, API
  // 1.9; an older core leaves it out) and it is not made yet; choosing it remembers it for this profile (D103).
  const fasterTutor = typeof view.settings?.faster_tutor === "boolean" && !view.settings.faster_tutor
    ? () => void profiles.setPreferences({ faster_tutor: true })
    : null;
  return (
    <KnowsLinkProvider open={() => setScreen("knows")}>
      <ResearchLinkProvider open={(captureId) => { setStudyResearch(captureId); setScreen("study"); }}>
      <GapLinkProvider open={(gap) => {
        const subjectId = `cur.${gap.kc_id.split(".").slice(1, 3).join(".")}`;
        runtime.conceptMap.showGapOn(subjectId, { misconceptionId: gap.misconception_id, rootKcId: gap.root_kc_id, kcId: gap.kc_id });
        setMapSubject(subjectId); setMapTopic(null); setMapFirst(true); setScreen("learn");
      }}>
      <LessonLinkProvider open={openLesson} openTopic={(topic) => { setMapSubject(topic.split(".").slice(0, 3).join(".")); setMapTopic(topic); setMapFirst(false); setScreen("learn"); }}>
        <FasterTutorProvider use={fasterTutor}><TutorLookProvider avatar={tutorAvatarOf(view.settings)} name={tutorNameOf(view.settings)}><SavedQuestionsProvider controller={runtime.savedQuestions} onOpen={() => setScreen("saved-questions")}>{frame}</SavedQuestionsProvider></TutorLookProvider></FasterTutorProvider>
      </LessonLinkProvider>
      </GapLinkProvider>
      </ResearchLinkProvider>
    </KnowsLinkProvider>
  );
}

function UpdateRecovery({ runtime, children }: { runtime: AppRuntime; children: ReactNode }) {
  const t = useT();
  const [previous, setPrevious] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (runtime.updateRecovery === undefined) return;
    let active = true;
    const read = () => { void runtime.updateRecovery?.().then((version) => {
      if (active) setPrevious(version);
    }).catch(() => {}); };
    read();
    const timer = setInterval(read, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [runtime]);
  if (previous === null) return children;
  const recover = async () => {
    setPending(true);
    setFailed(false);
    try { await runtime.rollbackUpdate?.(); } catch { setFailed(true); setPending(false); }
  };
  return <div className="flex h-full items-center justify-center p-6">
    <section className="flex w-full max-w-160 flex-col gap-4" aria-busy={pending}>
      <h1 className="text-heading font-semibold text-ink">{t("updates.recovery_title")}</h1>
      <p className="text-ui text-ink">{t("updates.failed", { version: previous })}</p>
      <p className="text-ui text-muted">{t("updates.recovery_detail")}</p>
      <div><Button disabled={pending || runtime.rollbackUpdate === undefined} onClick={() => { void recover(); }}>{t("updates.rollback")}</Button></div>
      {pending && <p className="text-ui text-muted">{t("updates.recovering")}</p>}
      {failed && <ErrorState message={t("updates.recovery_failed")} />}
    </section>
  </div>;
}

export function App({ runtime, fillParent = false }: { runtime: AppRuntime; fillParent?: boolean }) {
  const registry = useMemo(() => registerVisuals(defaultRegistry(), RENDERERS), []);
  return (
    <LocaleProvider locale="en">
      <RegistryProvider registry={registry}>
        {/* The concept map's vetted drawing (UXS-22 map view): the UI never imports renderers (ARC-18). */}
        <ConceptMapRendererProvider value={ConceptMapSvg}>
        <div className={fillParent ? "h-full" : "h-screen"}>
          {/* The connectivity indicator on every screen (UXS-06): in the sidebar's foot once a profile is open. */}
          <AppFrame profiles={runtime.profiles}>
            <UpdateRecovery runtime={runtime}><Shell runtime={runtime} /></UpdateRecovery>
          </AppFrame>
        </div>
        </ConceptMapRendererProvider>
      </RegistryProvider>
    </LocaleProvider>
  );
}
