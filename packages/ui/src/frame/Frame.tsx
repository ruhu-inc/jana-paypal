// The conversation-first app frame (UXS-01, DSN-17, D89; direction A "Studio"): a 272 px sidebar on `panel` beside a
// main column on `paper`. The sidebar holds, from the top, the brand (the lockup, or the mark alone on the rail; DSN-25)
// and its collapse button; "New question", the one primary button; the places (the current one on `wash` with a
// bolder label); recent sessions grouped by day, each with its subject dot; and at its foot the student (avatar and
// name), "Switch student", Settings and the connectivity indicator (UXS-06). By breakpoint: wide shows the sidebar,
// expanded starts it as the 72 px rail (labels shown on hover and focus, never a native tooltip), medium makes it an
// overlay drawer over a scrim opened from the screen's header, and compact puts a bottom tab bar in its place, whose
// More opens the same drawer. The drawer is modal: Escape or the scrim closes it and focus returns where it was.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import type { SessionGroup } from "@jana/client-core";
import { ConnectivityIndicator } from "../connectivity/ConnectivityIndicator.js";
import { useLedger } from "../connectivity/AppFrame.js";
import { useConnectivityController } from "../connectivity/context.js";
import { useT } from "../i18n.js";
import { Icon, type IconName } from "../icons.js";
import { BrandMark } from "../primitives/BrandMark.js";
import { IconButton, MenuButton, SubjectDot, type MenuItem } from "../primitives/index.js";
import { Avatar } from "../profiles/Avatar.js";
import { useLayoutClass, type LayoutClass } from "./layout.js";

export type SidebarVariant = "full" | "rail" | "drawer";

interface FrameApi {
  layout: LayoutClass;
  /** Opens the overlay drawer (medium and compact); null where the sidebar is always on screen. */
  openMenu: (() => void) | null;
}

const FrameContext = createContext<FrameApi>({ layout: "wide", openMenu: null });

/** The frame's layout class and, where the sidebar is a drawer, the way to open it. */
export function useFrame(): FrameApi {
  return useContext(FrameContext);
}

const FOCUSABLE = "button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex='-1'])";

/** The frame. `sidebar` draws the sidebar in the variant the layout asks for; `tabs` draws the compact tab bar. */
export function StudioFrame({ sidebar, tabs, children }: {
  sidebar: (props: { variant: SidebarVariant; onToggle: () => void; onDone: () => void }) => ReactNode;
  tabs?: (props: { onMore: () => void }) => ReactNode;
  children: ReactNode;
}) {
  const t = useT();
  const layout = useLayoutClass();
  useLedger(); // the sidebar, or the drawer at narrower widths, carries the connectivity indicator (UXS-06)
  // The student's own collapse or expand holds until the window crosses a breakpoint, which starts it again (UXS-01).
  const [collapsed, setCollapsed] = useState<boolean | null>(null);
  const [drawer, setDrawer] = useState(false);
  useEffect(() => {
    setCollapsed(null);
    setDrawer(false);
  }, [layout]);
  const inline = layout === "wide" || layout === "expanded";
  const rail = collapsed ?? layout === "expanded";
  const openMenu = useCallback(() => setDrawer(true), []);
  const closeMenu = useCallback(() => setDrawer(false), []);
  const api: FrameApi = { layout, openMenu: inline ? null : openMenu };

  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!drawer) return;
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    return () => {
      if (before?.isConnected) before.focus();
    };
  }, [drawer]);
  const keep = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMenu();
      return;
    }
    if (event.key !== "Tab" || panel.current === null) return;
    const focusable = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <FrameContext.Provider value={api}>
      <div data-layout={layout} className="flex h-full min-h-0 bg-paper text-ink">
        {inline && sidebar({ variant: rail ? "rail" : "full", onToggle: () => setCollapsed(!rail), onDone: () => undefined })}
        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
          {layout === "compact" && tabs?.({ onMore: openMenu })}
        </div>
        {!inline && drawer && (
          <div className="fixed inset-0 z-dialog">
            <div aria-hidden="true" data-scrim="" onClick={closeMenu} className="jana-reveal absolute inset-0 bg-scrim" />
            <div ref={panel} role="dialog" aria-modal="true" aria-label={t("frame.menu")} onKeyDown={keep} className="jana-drawer absolute inset-y-0 start-0 flex shadow-overlay-2">
              {sidebar({ variant: "drawer", onToggle: closeMenu, onDone: closeMenu })}
            </div>
          </div>
        )}
      </div>
    </FrameContext.Provider>
  );
}

/** The 56 px header of the main column: the drawer's menu button where the sidebar is a drawer, the screen's or
 * session's name and its controls. */
export function ScreenHeader({ children, end, wrap = false }: { children?: ReactNode; end?: ReactNode; wrap?: boolean }) {
  const t = useT();
  const { openMenu } = useFrame();
  return (
    <header className="flex min-h-(--layout-headerHeight) shrink-0 items-center gap-3 py-2 ps-6 pe-3">
      {openMenu !== null && <span className="-ms-3 shrink-0"><IconButton icon="menu" label={t("frame.open_menu")} onClick={openMenu} /></span>}
      <div className={`flex min-w-0 flex-1 items-center gap-3 [&>*:not(h1)]:shrink-0 ${wrap ? "flex-wrap" : ""}`}>{children}</div>
      {end !== undefined && <div className="flex shrink-0 items-center gap-2">{end}</div>}
    </header>
  );
}

/** A header for screens that bring no header of their own: only where the sidebar is a drawer, for its menu button. */
export function MenuBar() {
  const { openMenu } = useFrame();
  return openMenu === null ? null : <ScreenHeader />;
}

export interface FramePlace {
  id: string;
  icon: IconName;
  label: string;
  /** A shorter name for the narrow window's tab bar, where a long one wraps ("Notes" for "Notes and saved"). */
  short?: string;
  /** Localised section name. Consecutive places share a section in the sidebar and drawer. */
  group?: string;
  current: boolean;
  onSelect: () => void;
}

export interface FrameSession {
  id: string;
  title: string;
  subject: string;
  subjectLabel: string;
  current: boolean;
}

export interface SidebarContent {
  places: FramePlace[];
  /** "New question": absent where no tutor can answer (PLT-29). */
  onNewQuestion?: () => void;
  sessions: { group: SessionGroup; items: FrameSession[] }[];
  onOpenSession: (sessionId: string) => void;
  /** The open student: name, avatar disc and, quietly under the name in their menu, their subjects and level. */
  profile: { name: string; disc: number; detail?: string } | null;
  onSwitch: () => void;
  onSettings: () => void;
  settingsCurrent: boolean;
  /** The rest of the student menu (D115): absent ones are left out of it. */
  menu?: StudentMenuActions;
  /** The indicator to show where no connectivity controller is provided (the gallery draws it from a fixture). */
  indicator?: ReactNode;
}

export interface StudentMenuActions {
  onLanguage?: () => void;
  onHelp?: () => void;
  onKeys?: () => void;
  /** "Plan & online features": the plan's name (Free, offline; or the online plan) and the page that explains it. */
  plan?: { label: string; onOpen: () => void };
  /** "Back up this profile": the profile archive (Settings → Data export). */
  onBackup?: () => void;
  /** The menu is opening: the plan is read then, never during first run (PLT-17). */
  onOpen?: () => void;
}

/** The student menu (D115, a follow-up to D109's sidebar): one row at the sidebar's foot, the avatar, the
 * name and a chevron (the avatar alone on the rail), opening a menu above it: the student's name with their subjects and
 * level; Settings, Language, Get help, Keyboard shortcuts; the plan and Back up this profile; Switch student. Never "Log
 * out": there is nothing to log out of (D86). */
function StudentMenu({ rail, content, done }: { rail: boolean; content: SidebarContent; done: (action: () => void) => () => void }) {
  const t = useT();
  const profile = content.profile;
  const menu = content.menu ?? {};
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const items: MenuItem[] = [
    { id: "settings", group: "app", icon: "settings", label: t("app.settings"), hint: t(mac ? "frame.menu.settings_keys_mac" : "frame.menu.settings_keys"), onSelect: done(content.onSettings) },
    ...(menu.onLanguage !== undefined ? [{ id: "language", group: "app", icon: "language" as const, label: t("frame.menu.language"), onSelect: done(menu.onLanguage) }] : []),
    ...(menu.onHelp !== undefined ? [{ id: "help", group: "app", icon: "support" as const, label: t("frame.menu.help"), onSelect: done(menu.onHelp) }] : []),
    ...(menu.onKeys !== undefined ? [{ id: "keys", group: "app", icon: "keyboard" as const, label: t("keys.title"), onSelect: done(menu.onKeys) }] : []),
    ...(menu.plan !== undefined ? [{ id: "plan", group: "account", icon: "online" as const, label: t("frame.menu.plan"), hint: menu.plan.label, onSelect: done(menu.plan.onOpen) }] : []),
    ...(menu.onBackup !== undefined ? [{ id: "backup", group: "account", icon: "backup" as const, label: t("frame.menu.backup"), onSelect: done(menu.onBackup) }] : []),
    { id: "switch", group: "switch", icon: "switch-profile", label: t("app.switch_profile"), onSelect: done(content.onSwitch) },
  ];
  const name = profile?.name ?? t("app.switch_profile");
  return (
    <MenuButton label={t("frame.menu.label", { name })} placement="above" align="start" block={!rail} items={items} {...(menu.onOpen !== undefined ? { onOpen: menu.onOpen } : {})}
      header={profile === null ? undefined : <>
        <span className="truncate text-ui font-semibold text-ink">{profile.name}</span>
        {profile.detail !== undefined && <span className="text-meta text-muted">{profile.detail}</span>}
      </>}
      trigger={(props) => (
        <span className={`group relative flex ${rail ? "" : "w-full"}`}>
          <button type="button" {...props} aria-current={content.settingsCurrent ? "page" : undefined} data-student-menu=""
            className={`jana-press relative flex min-h-11 items-center gap-3 rounded-control text-ui font-medium text-ink hover:bg-wash aria-expanded:bg-wash aria-[current=page]:bg-wash ${rail ? "size-11 justify-center" : "w-full px-2"}`}>
            {profile !== null ? <Avatar name={profile.name} disc={profile.disc} size="sm" /> : <Icon name="switch-profile" />}
            {/* Named in words for assistive technology; the avatar and the visible name are its picture. */}
            <span className="sr-only">{t("frame.menu.label", { name })}</span>
            {!rail && <span aria-hidden="true" className="min-w-0 flex-1 truncate text-start">{name}</span>}
            {!rail && <span aria-hidden="true" className="inline-flex text-muted"><Icon name="student-menu" size={16} /></span>}
          </button>
          {rail && <RailLabel label={name} />}
        </span>
      )} />
  );
}

/** One sidebar row: an icon and its words. On the rail the words are the accessible name and show beside the icon on
 * hover and keyboard focus. */
function Item({ rail, icon, label, current = false, onClick }: { rail: boolean; icon: IconName; label: string; current?: boolean; onClick: () => void }) {
  const look = rail ? "size-11 justify-center" : "min-h-10 w-full gap-3 px-3 after:absolute after:inset-x-0 after:-inset-y-0.5 after:content-['']";
  return (
    <span className="group relative flex">
      <button type="button" aria-current={current ? "page" : undefined} onClick={onClick}
        className={`jana-press relative flex items-center rounded-control text-ui font-medium text-muted hover:bg-wash hover:text-ink aria-[current=page]:bg-wash aria-[current=page]:font-semibold aria-[current=page]:text-ink ${look}`}>
        <Icon name={icon} />
        <span className={rail ? "sr-only" : "min-w-0 flex-1 truncate text-start"}>{label}</span>
      </button>
      {rail && <RailLabel label={label} />}
    </span>
  );
}

function RailLabel({ label }: { label: string }) {
  return (
    <span aria-hidden="true" data-icon-label="" className="pointer-events-none absolute start-full top-1/2 z-popover ms-2 hidden -translate-y-1/2 rounded-control bg-ink px-2.5 py-1.5 text-meta font-medium whitespace-nowrap text-canvas shadow-overlay-1 group-hover:block group-has-[:focus-visible]:block">
      {label}
    </span>
  );
}

/** The sidebar in its variant: `full` 272 px, `rail` 72 px of icons, `drawer` the full sidebar over a scrim. */
export function Sidebar({ variant, onToggle, onDone, content }: { variant: SidebarVariant; onToggle: () => void; onDone: () => void; content: SidebarContent }) {
  const t = useT();
  const openLedger = useLedger();
  const connectivity = useConnectivityController();
  const rail = variant === "rail";
  const done = (action: () => void) => () => {
    action();
    onDone();
  };
  const width = rail ? "w-(--layout-railWidth-collapsed) items-center px-2" : "w-(--layout-railWidth-expanded) px-3";
  const toggle = variant === "drawer" ? t("frame.close_menu") : rail ? t("frame.expand") : t("frame.collapse");
  const groups: { label: string | undefined; places: FramePlace[] }[] = [];
  for (const place of content.places) {
    const previous = groups.at(-1);
    if (previous && previous.label === place.group) previous.places.push(place);
    else groups.push({ label: place.group, places: [place] });
  }
  const placeItem = (place: FramePlace) => <li key={place.id}><Item rail={rail} icon={place.icon} label={place.label} current={place.current} onClick={done(place.onSelect)} /></li>;
  return (
    <nav aria-label={t("app.nav")} data-region="sidebar" data-variant={variant} tabIndex={-1} className={`flex h-full min-h-0 shrink-0 flex-col gap-2 bg-panel pt-3 pb-2 text-ink ${width}`}>
      <div className={`flex shrink-0 items-center ${rail ? "flex-col gap-2" : "min-h-11 justify-between ps-2"}`}>
        <span className={`inline-flex items-center ${rail ? "" : "min-h-9"}`}><BrandMark kind="mark" height={24} /></span>
        <IconButton icon={variant === "drawer" ? "dismiss" : "sidebar"} size="sm" label={toggle} onClick={onToggle} />
      </div>
      <ul className="flex shrink-0 flex-col gap-0.5">
        {/* New question is the first row, in ink, not a filled button (owner, 2026-10-19: option B). */}
        {content.onNewQuestion !== undefined && (
          <li className="[&_button]:font-semibold [&_button]:text-ink"><Item rail={rail} icon="new-question" label={t("composer.new_question")} onClick={done(content.onNewQuestion)} /></li>
        )}
        {groups.map(({ label, places }, index) => label === undefined ? places.map(placeItem) : (
          <li key={`${index}-${label}`}>
            <div role="group" aria-label={label} className={rail ? "mt-2 border-t border-line pt-2" : "pt-3"}>
              <p className={rail ? "sr-only" : "px-3 pb-1 text-label text-faint"}>{label}</p>
              <ul className="flex flex-col gap-0.5">{places.map(placeItem)}</ul>
            </div>
          </li>
        ))}
      </ul>
      {rail ? <span className="flex-1" /> : (
        <section aria-label={t("frame.recent")} className="-mx-1 flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto px-1 pt-3">
          {content.sessions.map(({ group, items }) => (
            <div key={group} className="flex flex-col gap-0.5">
              <p className="px-3 pt-3 pb-1 text-label text-faint first:pt-0">{t(`frame.group.${group}`)}</p>
              <ul className="flex flex-col gap-0.5">
                {items.map((session) => (
                  <li key={session.id}>
                    <button type="button" data-session-id={session.id} aria-current={session.current ? "true" : undefined} onClick={done(() => content.onOpenSession(session.id))}
                      className="jana-press relative flex min-h-9 w-full items-center gap-3 rounded-control px-3 text-ui text-muted after:absolute after:inset-x-0 after:-inset-y-0.5 after:content-[''] hover:bg-wash hover:text-ink aria-[current=true]:bg-wash aria-[current=true]:font-medium aria-[current=true]:text-ink">
                      <SubjectDot subject={session.subject} />
                      <span className="relative min-w-0 flex-1 truncate text-start">{session.title}<span className="sr-only">, {session.subjectLabel}</span></span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
      <div className={`flex shrink-0 flex-col gap-0.5 border-t border-line pt-2 ${rail ? "items-center border-t-0" : ""}`}>
        <StudentMenu rail={rail} content={content} done={done} />
      </div>
      <div className={`shrink-0 ${rail ? "[&_[data-connectivity-indicator]>span]:sr-only" : ""}`}>
        {connectivity !== null ? <ConnectivityIndicator onOpenLedger={() => {
          onDone();
          openLedger();
        }} /> : content.indicator}
      </div>
    </nav>
  );
}

/** The compact layout's bottom tab bar (UXS-35): the places, then More, which opens the drawer with the rest. */
export function TabBar({ places, onMore }: { places: FramePlace[]; onMore: () => void }) {
  const t = useT();
  const tab = (key: string, icon: IconName, label: string, current: boolean, onClick: () => void, short?: string) => (
    <button key={key} type="button" aria-current={current ? "page" : undefined} onClick={onClick} {...(short !== undefined ? { "aria-label": label } : {})}
      className="jana-press flex min-h-13 min-w-0 flex-col items-center justify-center gap-1 rounded-control text-label text-muted hover:text-ink aria-[current=page]:font-semibold aria-[current=page]:text-ink [&[aria-current=page]>span:first-child]:bg-wash">
      <span className="flex h-7 w-14 items-center justify-center rounded-chip"><Icon name={icon} /></span>
      <span className="max-w-full truncate">{short ?? label}</span>
    </button>
  );
  return (
    <nav aria-label={t("app.nav")} data-region="tabs" className="grid shrink-0 auto-cols-fr grid-flow-col border-t border-line bg-panel px-1 pt-1.5 pb-2.5">
      {places.map((p) => tab(p.id, p.icon, p.label, p.current, p.onSelect, p.short))}
      {tab("more", "more", t("frame.more"), false, onMore)}
    </nav>
  );
}
