// The one icon pack (DSN-11): Lucide, imported only through this semantic name map; one glyph per concept and one
// concept per glyph. Decorative icons are aria-hidden; meaningful ones carry a keyed label.
import {
  ArrowDown, ArrowUp, ArrowUpFromLine, Check, ChevronDown, ChevronRight, CircleAlert, CircleHelp, Cloud, CloudOff, House, Lightbulb, LoaderCircle,
  MessageCircleQuestion, NotebookText, RefreshCw, Sigma, Square, TriangleAlert, Eye, Info, X, Lock, Settings,
  Users, Circle, CircleDashed, CircleDot, CircleCheckBig, RotateCcw, Hourglass, BookOpen, GraduationCap, ArrowLeft, ArrowRight, MoveUp,
  MoveDown, MessageSquare, ImagePlus, Mic, LayoutDashboard, PanelLeft, PanelRight, Menu, SquarePen, Ellipsis, Volume2, Flag, Keyboard, Target,
  ScrollText, LifeBuoy, Bug, Share2, Wind, UserPlus, ClipboardList, Timer, Bookmark, SquareCheckBig, EyeOff, Calculator, Pause, Play, FlaskConical,
  TrendingUp, ChartColumn, Table, Search, Clock3, LibraryBig, Languages, Archive, ChevronsUpDown, GalleryVerticalEnd, Shapes,
  type LucideIcon,
} from "lucide-react";
import { TOKENS } from "./generated/tokens.js";

export const ICONS = {
  verified: { glyph: Check, mirrorInRtl: false },
  hint: { glyph: Lightbulb, mirrorInRtl: false },
  "look-here": { glyph: Eye, mirrorInRtl: false },
  stop: { glyph: Square, mirrorInRtl: false },
  send: { glyph: ArrowUp, mirrorInRtl: false },
  "on-this-computer": { glyph: House, mirrorInRtl: false },
  online: { glyph: Cloud, mirrorInRtl: false },
  sending: { glyph: ArrowUpFromLine, mirrorInRtl: false },
  "cloud-unavailable": { glyph: CloudOff, mirrorInRtl: false },
  loading: { glyph: LoaderCircle, mirrorInRtl: false },
  retry: { glyph: RefreshCw, mirrorInRtl: false },
  expand: { glyph: ChevronRight, mirrorInRtl: true },
  collapse: { glyph: ChevronDown, mirrorInRtl: false },
  question: { glyph: MessageCircleQuestion, mirrorInRtl: false },
  stuck: { glyph: CircleHelp, mirrorInRtl: false },
  "new-below": { glyph: ArrowDown, mirrorInRtl: false },
  "system-error": { glyph: CircleAlert, mirrorInRtl: false },
  warning: { glyph: TriangleAlert, mirrorInRtl: false },
  info: { glyph: Info, mirrorInRtl: false },
  maths: { glyph: Sigma, mirrorInRtl: false },
  dismiss: { glyph: X, mirrorInRtl: false },
  "pin-set": { glyph: Lock, mirrorInRtl: false },
  settings: { glyph: Settings, mirrorInRtl: false },
  "switch-profile": { glyph: Users, mirrorInRtl: false },
  notes: { glyph: NotebookText, mirrorInRtl: false },
  // TABLE LRN-09a states (DSN-23), named as tokens.json's mastery block names them.
  "mastery-not-started": { glyph: Circle, mirrorInRtl: false },
  "mastery-learning": { glyph: CircleDashed, mirrorInRtl: false },
  "mastery-nearly": { glyph: CircleDot, mirrorInRtl: false },
  "mastery-secure": { glyph: CircleCheckBig, mirrorInRtl: false },
  "mastery-refresh": { glyph: RotateCcw, mirrorInRtl: false },
  "mastery-moved-on": { glyph: Hourglass, mirrorInRtl: false },
  learn: { glyph: BookOpen, mirrorInRtl: false },
  tutor: { glyph: GraduationCap, mirrorInRtl: false },
  "go-back": { glyph: ArrowLeft, mirrorInRtl: true },
  "go-next": { glyph: ArrowRight, mirrorInRtl: true },
  "move-up": { glyph: MoveUp, mirrorInRtl: false },
  "move-down": { glyph: MoveDown, mirrorInRtl: false },
  ask: { glyph: MessageSquare, mirrorInRtl: false },
  // The composer's tools (UXS-15): add a photo (UXS-13) and hold-to-dictate voice (VOC-46).
  photo: { glyph: ImagePlus, mirrorInRtl: false },
  diagram: { glyph: Shapes, mirrorInRtl: false },
  voice: { glyph: Mic, mirrorInRtl: false },
  // The app frame (UXS-01, UXS-14): Home, the sidebar's collapse, the drawer's menu, New question, the board toggle and
  // the compact tab bar's More.
  home: { glyph: LayoutDashboard, mirrorInRtl: false },
  sidebar: { glyph: PanelLeft, mirrorInRtl: true },
  board: { glyph: PanelRight, mirrorInRtl: true },
  menu: { glyph: Menu, mirrorInRtl: false },
  "new-question": { glyph: SquarePen, mirrorInRtl: false },
  more: { glyph: Ellipsis, mirrorInRtl: false },
  // A reply's quiet action row (UXS-14): read it aloud (VOC-46) and report a problem (UXS-44).
  "read-aloud": { glyph: Volume2, mirrorInRtl: false },
  report: { glyph: Flag, mirrorInRtl: false },
  // Practice (UXS-24), the keyboard help (LOC-12), Settings → Licence and About → Licences (PLT-19, PLT-10), crash
  // review (OBS-07), the support share (OBS-18) and the calm screen (UXS-43).
  practice: { glyph: Target, mirrorInRtl: false },
  flashcards: { glyph: GalleryVerticalEnd, mirrorInRtl: false },
  keyboard: { glyph: Keyboard, mirrorInRtl: false },
  licence: { glyph: ScrollText, mirrorInRtl: false },
  support: { glyph: LifeBuoy, mirrorInRtl: false },
  crash: { glyph: Bug, mirrorInRtl: false },
  share: { glyph: Share2, mirrorInRtl: false },
  calm: { glyph: Wind, mirrorInRtl: false },
  // The picker's "Add profile" (UXS-09).
  "add-student": { glyph: UserPlus, mirrorInRtl: false },
  // Mock exams (UXS-25): the place, its clock, the navigator's answered / flagged for review / not seen yet, the
  // calculator note, pause and continue; and the research studies of the study screen (PRV-06).
  mock: { glyph: ClipboardList, mirrorInRtl: false },
  timer: { glyph: Timer, mirrorInRtl: false },
  "flag-review": { glyph: Bookmark, mirrorInRtl: false },
  answered: { glyph: SquareCheckBig, mirrorInRtl: false },
  unseen: { glyph: EyeOff, mirrorInRtl: false },
  calculator: { glyph: Calculator, mirrorInRtl: false },
  pause: { glyph: Pause, mirrorInRtl: false },
  continue: { glyph: Play, mirrorInRtl: true },
  study: { glyph: FlaskConical, mirrorInRtl: false },
  "study-area": { glyph: LibraryBig, mirrorInRtl: false },
  // The student menu at the sidebar's foot: its trigger's chevrons, Language and "Back up this profile".
  "student-menu": { glyph: ChevronsUpDown, mirrorInRtl: false },
  language: { glyph: Languages, mirrorInRtl: false },
  backup: { glyph: Archive, mirrorInRtl: false },
  // UXS-27: the Progress place, a chart shown as a chart, and as a table (LOC-16); UXS-51: worked-solution search.
  progress: { glyph: TrendingUp, mirrorInRtl: true },
  chart: { glyph: ChartColumn, mirrorInRtl: false },
  table: { glyph: Table, mirrorInRtl: false },
  search: { glyph: Search, mirrorInRtl: false },
  // LRN-20 (D112): the quiet line after a long pause, "Take your time. A hint is one tap away when you want it."
  "take-your-time": { glyph: Clock3, mirrorInRtl: false },
} as const satisfies Record<string, { glyph: LucideIcon; mirrorInRtl: boolean }>;

export type IconName = keyof typeof ICONS;

export function Icon({ name, size = TOKENS.icon.default as 16 | 20 | 24, label }: { name: IconName; size?: 16 | 20 | 24; label?: string }) {
  const Glyph = ICONS[name].glyph;
  return label === undefined ? (
    <Glyph size={size} strokeWidth={TOKENS.icon.stroke} aria-hidden="true" focusable="false" />
  ) : (
    <Glyph size={size} strokeWidth={TOKENS.icon.stroke} role="img" aria-label={label} />
  );
}
