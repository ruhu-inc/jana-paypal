// UI primitives, tokens, transcript and board components, the renderer registry and the gallery (M5); the knows page,
// the saved-note announcement and the memory-use line (M15); the subject map, the lesson player and first run's
// tutor step (M13.g); the connectivity indicator, the online features screen and the egress ledger (M19).
export { LocaleProvider, useT } from "./i18n.js";
export { StoreProvider, useSelect, useSession, useStoreHolder, shallowArray } from "./store.js";
export { Icon, ICONS, type IconName } from "./icons.js";
export * from "./primitives/index.js";
export { ComposerShell } from "./primitives/ComposerShell.js";
export { Tex } from "./maths/Tex.js";
export { renderTex, katexCache, Lru } from "./maths/katex.js";
export { MarkdownBlock, StreamingMarkdown } from "./markdown/Markdown.js";
export { lex, heal } from "./markdown/lexer.js";
export { Registry, RegistryProvider, Rendered, RenderBoundary, hashOf, renderLog, useRegistry, type Entry, type Lifecycle, type RenderProps } from "./registry/registry.js";
export { defaultRegistry, ToolLine, useDelayed, lifecycleOf } from "./transcript/parts.js";
export { TOOL_FAMILY, FAMILIES, familyOf } from "./transcript/tools.js";
export { TurnView, PartView } from "./transcript/Turn.js";
export { Bubble, StudentMessage } from "./transcript/Message.js";
export { Transcript } from "./transcript/Transcript.js";
export { Composer } from "./composer/Composer.js";
export { NoTutor, type NoTutorReason } from "./composer/NoTutor.js";
export { VoiceBarView, type VoiceBarActions } from "./voice/VoiceBar.js";
export { CalmScreen, type Helpline } from "./safety/CalmScreen.js";
export { HintDock, useOpenHintRequest } from "./composer/HintDock.js";
export { SpeechToolsProvider, ReadAloud, DictationControl, DictationStatus, useSpeechTools, useSpeechView, useVoicePointer, voiceErrorKey } from "./voice/SpeechTools.js";
export { Board, BoardStep } from "./board/Board.js";
export { StatusLine, TurnFooter, ConnectionBanner } from "./status/StatusLine.js";
export { DegradedNotices } from "./status/DegradedNotices.js";
export { TutorScreen } from "./TutorScreen.js";
export { TOKENS } from "./generated/tokens.js";
export { Avatar, AVATAR_DISCS, discOf } from "./profiles/Avatar.js";
export { AddStudent, ProfilePicker, ProfilePickerView, useProfiles } from "./profiles/ProfilePicker.js";
export { ProfileRecovery, ProfileRecoveryView, type RecoveryStep } from "./profiles/ProfileRecovery.js";
export { WelcomeCard } from "./setup/Welcome.js";
export { Settings, PREFERENCE_SECTIONS, SETTINGS_SECTIONS, textSizes } from "./settings/Settings.js";
export { AboutPanel, AboutView, CrashReportView, CrashReview, LicencePanel, LicenceView } from "./settings/Support.js";
export { DamagedPacks, FAMILY_GROUPS, OnlineFeaturesSection, ProfilesSection } from "./settings/FamilyControls.js";
export { DataExport } from "./settings/DataExport.js";
export { AppUpdates, ContentPacks, TutorModels, useInstallations, type Installing } from "./settings/Installations.js";
export { DEFAULT_TUTOR_AVATAR, TUTOR_AVATARS, TutorAvatar, TutorLookProvider, TutorSection, tutorAvatarOf, tutorNameOf, useTutorAvatar, useTutorName } from "./settings/TutorSection.js";
// The tutor characters (DSN-26), the presence (UXS-53) and the one pick (UXS-54).
export { Character, TutorPresence, type CharacterProps } from "./tutor/Character.js";
export {
  CHARACTER_IDS, CHARACTER_MOUTHS, CHARACTER_POSES, DEFAULT_CHARACTER, characterOf, isCharacterId, poseOfVoiceState, slugOf,
  type CharacterId, type CharacterMouth, type CharacterPose, type CharacterSlug, type VoiceStateName,
} from "./tutor/characters.js";
export { FirstRunTutorPick, SettingsTutorPick, type PickSeed } from "./tutor/TutorPick.js";
export { useCharacterName } from "./tutor/look.js";
export { HealthPanel } from "./settings/HealthPanel.js";
export { clearRenderCaches } from "./caches.js";
export { KnowsContent, KnowsPage, KNOWS_SECTIONS, NOT_YET_AVAILABLE, sharingLines, type ForgetReason, type KnowsSection } from "./memory/KnowsPage.js";
export { MemoryChip, MemoryChipView, MemoryChips, type UndoState } from "./memory/MemoryChip.js";
export { KnowsLinkProvider, useOpenKnows } from "./memory/link.js";
export { MemoryUseLine } from "./transcript/parts.js";
export { SubjectMap, SubjectMapContent, type ConceptMapMode, type LessonInProgress, type MapActions, type SubjectChoice } from "./learn/SubjectMap.js";
export { ConceptMapContent, ConceptMapRendererProvider, type ConceptActions, type ConceptDrawEdge, type ConceptDrawNode, type ConceptMapRenderer } from "./learn/ConceptMap.js";
export { FollowUpChips, FollowUpProvider, HelpChips, ReplyFollowUps, type FollowUpHandlers } from "./chips/NextStepChips.js";
export { Practice, PracticeContent, type PracticeActions, type PracticeDraft } from "./learn/Practice.js";
export { Feedback, PartInput, answerOf } from "./learn/items.js";
export { LessonPlayer, LessonPlayerContent, ExampleBoard, type AskContext, type LessonActions } from "./learn/LessonPlayer.js";
export { BandBar, MasteryCell, MasteryMark, MasteryRing, SegmentedBar, useCountsText, useMasteryWord } from "./learn/Mastery.js";
export { PackInline, PackText, packMarkdown } from "./learn/PackText.js";
export { FirstRunTutor } from "./setup/FirstRunTutor.js";
export { FirstRunInterview, FirstRunInterviewView } from "./setup/FirstRunInterview.js";
export { FirstRun } from "./setup/FirstRun.js";
export { textScaleOf, useAppliedPreferences } from "./settings/preferences.js";
export { ProblemReportList, SupportReport } from "./settings/Help.js";
export { ReportProblem } from "./transcript/ReportProblem.js";
export { AppFrame, useLaunchSurface, useLedger } from "./connectivity/AppFrame.js";
export { MenuBar, ScreenHeader, Sidebar, StudioFrame, TabBar, useFrame, type FramePlace, type FrameSession, type SidebarContent, type SidebarVariant } from "./frame/Frame.js";
export { boardBeside, layoutOf, useLayoutClass, type LayoutClass } from "./frame/layout.js";
export { characterKeysStored, inTextField, moveRegion, sessionCharacterKeys, useCharacterKeys, useGlobalShortcuts, type GlobalKeys } from "./frame/shortcuts.js";
export { KeyboardHelp, SHORTCUTS } from "./frame/KeyboardHelp.js";
export { Home, partOfDay, type HomeMaterials, type HomePickup, type HomeSubject } from "./home/Home.js";
export { CourseAsk } from "./home/CourseAsk.js";
export { DailyGoalChip } from "./home/DailyGoal.js";
export { LastTimeCard } from "./home/LastTimeCard.js";
export { DueToday, type DueSet } from "./home/DueToday.js";
export { CheckinCard } from "./home/CheckinCard.js";
export { AskAnswer, type AskActions } from "./home/AskAnswer.js";
export { PlanCard, useBlockText, type PlanActions, type PlanNames } from "./home/PlanCard.js";
export { Progress, ProgressContent, movedToday, type ProgressActions, type ProgressMap, type ProgressNames } from "./progress/Progress.js";
export { MapOverview, OVERVIEW_LAYERS, type MapOverviewProps } from "./progress/MapOverview.js";
export { SessionHistory, SessionHistoryContent, type HistoryActions } from "./progress/SessionHistory.js";
export { Alerts, AlertsContent, ParentSummary, ParentSummaryContent } from "./settings/ParentSummary.js";
export { SearchBox, SearchContent, SearchScreen, SolutionSteps, type SearchActions, type SearchNames } from "./search/Search.js";
export { useDuePractice, useRecentSessions } from "./home/data.js";
export { ConnectivityIndicator, ConnectivityIndicatorView } from "./connectivity/ConnectivityIndicator.js";
export { ConnectivityProvider, useConnectivityController, useConnectivitySelect } from "./connectivity/context.js";
export { EgressNoticeView, PRE_SEND_PATHS, TurnEgress, leavesKey } from "./connectivity/EgressNotice.js";
export { Ledger, LedgerContent, LedgerPage, type LedgerState } from "./connectivity/Ledger.js";
export { OnlineFeatures } from "./settings/OnlineFeatures.js";
export { ModelManager, type ModelManagerActions } from "./settings/ModelManager.js";
export { BRAND_MINIMUM, BRAND_PLACEMENTS, BrandMark, LaunchScreen, type BrandKind } from "./primitives/BrandMark.js";
export { THEME_PREFERENCES, resolveTheme, themeOf, useAppliedTheme, useSystemDark, useThemePreference, type Theme, type ThemePreference } from "./settings/theme.js";
export { VisualViewer, VisualSettingsProvider, revalidate, type VisualHooks, type VisualSettings, type VisualSpec, type LabelMode } from "./visuals/VisualViewer.js";

export { StudyNotes } from "./voice/StudyNotes.js";
export {
  AbandonedMockCard, ExamLockNotice, MockContent, MockPlace, MockReport, formatClock, minutesOf, mockAnswerOf, useNow, type LockedPlace, type MockActions,
  type MockSubject, type ReportNames, pastPaperShown,
} from "./mock/MockExam.js";
export {
  ExamCountdownCard, ExamDatesPanel, MarkingLabel, PastPaperShelf, type ExamDateActions, type PastPaperActions, type PastPaperSubjectOption,
} from "./mock/PastPapers.js";
export { GapLinkProvider, LessonLinkProvider, ResearchLinkProvider, useOpenGap, useOpenLesson, useOpenResearch, lessonOfCommand, type GapOnMap } from "./learn/link.js";
export { StudyResearch } from "./learn/StudyResearch.js";
export { FasterTutorProvider, useFasterTutor } from "./status/faster.js";
export * from "./learn/Assessment.js";
export * from "./learn/SavedQuestions.js";
export * from "./learn/SessionNotes.js";
export { StudyWorkspace, type StudyRequest, type StudyTab } from "./learn/StudyWorkspace.js";
export { StudyPlayer, framesOf, type StudyFrame } from "./learn/StudyPlayer.js";
export { StudyOutline } from "./learn/StudyOutline.js";
export { StudyLinkAdd } from "./learn/StudyLinkAdd.js";
export { CompanionThread, type CompanionThreadSource } from "./learn/CompanionThread.js";
export { StudyJourney } from "./learn/StudyJourney.js";
export { LearningJourney } from "./learn/LearningJourney.js";
