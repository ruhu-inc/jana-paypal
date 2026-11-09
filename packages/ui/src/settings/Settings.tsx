// Settings (UXS-29, UXS-46, TABLE UXS-46a; direction A "Studio"): one calm page that shows one section at a time, with
// no PIN and no parent account (D86, D87). The page lists exactly the sections the core names for this build's stage,
// in its order (`GET /v1/settings/sections`; the stage 2 sections from a core without the route): beside the open
// section as a side list where the page is wide enough, and as one compact select above it where it is not (a
// container query, so the learner never scrolls past a long menu to reach a long section). Selecting a section shows
// its body and marks it current; `section` opens the page at one (the student menu's deep links). The sections: Online
// features (the switches, with the study screen's research consents and research export below them, UXS-07, PRV-06,
// EVL-22); Profiles (UXS-29); Tutor models (UXS-11); Content (packs from USB or a file, and packs that failed their
// check); "What leaves this device", the egress ledger (ARC-10, PRV-28) and what a usage report holds (the published
// report schema, OBS-20, PRV-09), which show and switch nothing; the open student's Accessibility and language (Theme,
// "Board and chat text size", Easy-read, Reduce motion, Low-power mode, reading replies aloud, the character-key
// shortcuts of LOC-12 and the keyboard help, and extra time in mock exams, ASM-24, LOC-24); Study (study reminders,
// "Save notes about me" and the way to "What the tutor knows", UXS-28); Tutor (its name, PRM-03); Data export (the
// profile archive and the report for support, DAT-10, OBS-18); Licence (PLT-19); Help with the offline help (OPS-23:
// search, known problems, this computer and the support contacts), "How the tutor is running" (OBS-15), the crash
// reports waiting for a decision (OBS-17), the student's saved problem reports (UXS-44), the way to "Save a report for
// support" and SEC-19's honest line that anyone using the computer can open every profile; About (version, licences
// and updates, PLT-10, PLT-14); from stage 3 Voice, and guardian sections for school-age profiles.
// A section this app does not have yet says so.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { SettingsSections, STAGE_2_SECTIONS, type AssetEntry, type CheckinNotifier, type ChooseInstallSource, type NoticeEntry, type ProfileController, type StudentPreferences } from "@jana/client-core";
import type { Settings as SettingsRecord } from "@jana/contracts-ts";
import { Ledger } from "../connectivity/Ledger.js";
import { ScreenHeader } from "../frame/Frame.js";
import { KeyboardHelp } from "../frame/KeyboardHelp.js";
import { characterKeysStored, sessionCharacterKeys, useCharacterKeys } from "../frame/shortcuts.js";
import { TOKENS } from "../generated/tokens.js";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Disclosure, ErrorState, FIELD_LOOK, Notice, SegmentedControl, Skeleton, Switch } from "../primitives/index.js";
import { useProfiles } from "../profiles/ProfilePicker.js";
import { sessionTheme, THEME_PREFERENCES, themeStored, useThemePreference, type ThemePreference } from "./theme.js";
import { DataExport } from "./DataExport.js";
import { OnlineFeaturesSection, ProfilesSection } from "./FamilyControls.js";
import { HealthPanel } from "./HealthPanel.js";
import { OfflineHelpPanel, ProblemReportList } from "./Help.js";
import { ReportSchema } from "./ReportSchema.js";
import { AppUpdates, ContentPacks, TutorModels, useInstallations } from "./Installations.js";
import { textScaleOf } from "./preferences.js";
import { AboutPanel, CrashReview, LicencePanel } from "./Support.js";
import { TutorSection } from "./TutorSection.js";
import { ParentSummary, Alerts } from "./ParentSummary.js";

// The plain switches; `character_shortcuts` (optional: an older core does not return it) has its own (CharacterKeys),
// `faster_tutor` (API 1.9, D103) sits in Tutor models (StoredSwitch), `wake_word` is retired (D126) and never shown, and
// `checkin_notifications` (SPEC §19.5.5, optional: an older core does not return it) sits under Personal tutor memory
// online in Online features.
type Toggle = Exclude<{ [K in keyof StudentPreferences]-?: NonNullable<StudentPreferences[K]> extends boolean ? K : never }[keyof StudentPreferences], "character_shortcuts" | "faster_tutor" | "wake_word" | "checkin_notifications">;

/** The student preference switches, by the section of TABLE UXS-46a they belong to. */
export const PREFERENCE_SECTIONS: { section: "accessibility" | "study"; keys: Toggle[] }[] = [
  { section: "accessibility", keys: ["easy_read", "reduced_motion", "low_power", "read_aloud"] },
  { section: "study", keys: ["notifications", "memory_saving"] },
];

/** The sections this app has a body for, by the ids of TABLE UXS-46a the core lists; any other id the core names is
 * shown with its heading and a line saying this version does not have it yet. */
export const SETTINGS_SECTIONS = [...STAGE_2_SECTIONS, "voice", "parent_summary", "alerts"] as const;
type SectionId = string;

/** "Board and chat text size" (DSN-07, LOC-17): the default, a larger and the largest scale the token contract allows. */
export function textSizes(): { value: string; scale: number; key: string }[] {
  const { min, max } = TOKENS.textScale;
  return [
    { value: "default", scale: textScaleOf(min), key: "settings.text_size.default" },
    { value: "larger", scale: textScaleOf((min + max) / 2), key: "settings.text_size.larger" },
    { value: "largest", scale: textScaleOf(max), key: "settings.text_size.largest" },
  ];
}

function Card({ children }: { children: ReactNode }) {
  return <div className="flex flex-col divide-y divide-line rounded-card border border-line bg-canvas px-5 py-2">{children}</div>;
}

function Section({ id, heading, children }: { id: SectionId; heading: string; children: ReactNode }) {
  return (
    <section id={`settings-${id}`} data-settings-section={id} aria-labelledby={`settings-${id}-title`} className="flex scroll-mt-4 flex-col gap-3">
      <h2 id={`settings-${id}-title`} tabIndex={-1} className="text-heading text-ink">{heading}</h2>
      {children}
    </section>
  );
}

/** One row of a card: its words (and a line of help), then its control. */
function Row({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 py-3">
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-ui font-medium text-ink">{label}</span>
        {help !== undefined && <span className="text-meta text-muted">{help}</span>}
      </span>
      {children}
    </div>
  );
}

/** Theme (DSN-05): "Match this computer", "Light" or "Dark", applied at once. Kept with the profile's settings when the
 * core returns the key (D91), else for this session (see theme.ts). */
function ThemeSetting({ profiles, settings, onError }: { profiles: ProfileController; settings: SettingsRecord; onError: (code: string | null) => void }) {
  const t = useT();
  const view = useProfiles(profiles);
  const value = useThemePreference(view.active, settings);
  const choose = (theme: ThemePreference) => {
    onError(null);
    if (view.active !== null) sessionTheme.set(view.active, theme);
    if (!themeStored(settings)) return;
    const patch: StudentPreferences & { theme: ThemePreference } = { theme };
    profiles.setPreferences(patch).catch((e: unknown) => onError((e as { code?: string }).code ?? "internal_error"));
  };
  return (
    <Row label={t("settings.pref.theme")}>
      <SegmentedControl wrap label={t("settings.pref.theme")} value={value} onChange={choose}
        options={THEME_PREFERENCES.map((option) => ({ value: option, label: t(`settings.theme.${option}`) }))} />
    </Row>
  );
}

function TextSize({ settings, change }: { settings: SettingsRecord; change: (patch: StudentPreferences) => void }) {
  const t = useT();
  const sizes = textSizes();
  const scale = textScaleOf(settings.text_scale);
  const nearest = sizes.reduce<(typeof sizes)[number] | null>((best, s) => (best === null || Math.abs(s.scale - scale) < Math.abs(best.scale - scale) ? s : best), null);
  return (
    <Row label={t("settings.pref.text_scale")} help={t("settings.pref.text_scale_help")}>
      <SegmentedControl wrap label={t("settings.pref.text_scale")} value={nearest?.value ?? "default"} onChange={(value) => change({ text_scale: sizes.find((s) => s.value === value)?.scale ?? scale })}
        options={sizes.map((s) => ({ value: s.value, label: t(s.key) }))} />
    </Row>
  );
}

/** Extra time in mock exams (ASM-24, LOC-24): an accessibility preference, none, +25% or +50%; a sitting copies it when
 * it starts and its header says "Extra time: 25%". */
export const EXTRA_TIME = [0, 25, 50] as const;

function ExtraTime({ settings, change }: { settings: SettingsRecord; change: (patch: StudentPreferences) => void }) {
  const t = useT();
  return (
    <Row label={t("settings.pref.extra_time")} help={t("settings.pref.extra_time_help")}>
      <SegmentedControl wrap label={t("settings.pref.extra_time")} value={String(settings.extra_time_percent)} onChange={(value) => change({ extra_time_percent: Number(value) as SettingsRecord["extra_time_percent"] })}
        options={EXTRA_TIME.map((percent) => ({ value: String(percent), label: t(`settings.extra_time.${percent}`) }))} />
    </Row>
  );
}

/** The character-key shortcuts (LOC-12, WCAG 2.1.4): on unless the student turns them off; kept with the profile's
 * settings when the core returns the key (D92), else for this session. */
function CharacterKeys({ profiles, settings, onError }: { profiles: ProfileController; settings: SettingsRecord; onError: (code: string | null) => void }) {
  const t = useT();
  const view = useProfiles(profiles);
  const on = useCharacterKeys(view.active, settings);
  const set = (next: boolean) => {
    onError(null);
    if (view.active !== null) sessionCharacterKeys.set(view.active, next);
    if (!characterKeysStored(settings)) return;
    const patch: StudentPreferences & { character_shortcuts: boolean } = { character_shortcuts: next };
    profiles.setPreferences(patch).catch((e: unknown) => onError((e as { code?: string }).code ?? "internal_error"));
  };
  return <Switch label={t("settings.pref.character_shortcuts")} help={t("settings.pref.character_shortcuts_help")} checked={on} onChange={set} />;
}

/** A student preference switch shown only where the core returns the key (an older core does not): "Use the faster
 * tutor when this computer is slow" in Tutor models (UXS-03, MDL-05), D103. */
function StoredSwitch({ profiles, settings, name }: { profiles: ProfileController; settings: SettingsRecord | null; name: "faster_tutor" }) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const value = settings?.[name];
  if (typeof value !== "boolean") return null;
  const change = (checked: boolean) => {
    setError(null);
    profiles.setPreferences({ [name]: checked }).catch((e: unknown) => setError((e as { code?: string }).code ?? "internal_error"));
  };
  return (
    <div className="py-1" data-preference={name}>
      <Switch label={t(`settings.pref.${name}`)} help={t(`settings.pref.${name}_help`)} checked={value} onChange={change} />
      {error !== null && <ErrorState message={t(`error.${error}`)} />}
    </div>
  );
}

function Preferences({ profiles, settings, section, onOpenKnows, onKeys }: {
  profiles: ProfileController; settings: SettingsRecord; section: "accessibility" | "study"; onOpenKnows?: (() => void) | undefined; onKeys: () => void;
}) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const change = (patch: StudentPreferences) => {
    setError(null);
    profiles.setPreferences(patch).catch((e: unknown) => setError((e as { code?: string }).code ?? "internal_error"));
  };
  const keys = PREFERENCE_SECTIONS.find((s) => s.section === section)?.keys ?? [];
  const toggles = (names: Toggle[]) => names.map(key => <Switch key={key} label={t(`settings.pref.${key}`)} checked={settings[key]} onChange={checked => change({ [key]: checked })} />);
  return (
    <>
      {section === "accessibility" ? <>
        <h3 className="text-ui font-semibold text-ink">{t("settings.group.display")}</h3>
        <Card>
          <ThemeSetting profiles={profiles} settings={settings} onError={setError} />
          <TextSize settings={settings} change={change} />
          {toggles(["easy_read", "read_aloud"])}
        </Card>
        <h3 className="text-ui font-semibold text-ink">{t("settings.group.controls")}</h3>
        <Card>
          {toggles(["reduced_motion", "low_power"])}
          <CharacterKeys profiles={profiles} settings={settings} onError={setError} />
          <ExtraTime settings={settings} change={change} />
          <div className="flex flex-col items-start gap-1 py-3">
            <Button onClick={onKeys}><Icon name="keyboard" size={16} />{t("keys.open")}</Button>
            <p className="text-meta text-muted">{t("keys.hint")}</p>
          </div>
        </Card>
      </> : <Card>
        {toggles(keys)}
        {onOpenKnows !== undefined && (
          <div className="py-3"><Button onClick={onOpenKnows}>{t("knows.open")}</Button></div>
        )}
      </Card>}
      {error !== null && <ErrorState message={t(`error.${error}`)} />}
    </>
  );
}

export function Settings({ profiles, onClose, onOpenKnows, chooseSource, voiceSettings, notices, assets, notifier, section = null }: {
  profiles: ProfileController; onClose?: () => void; onOpenKnows?: () => void; chooseSource?: ChooseInstallSource | undefined; voiceSettings?: ReactNode;
  /** The computer's notification permission (the desktop shell's), asked before Check-in notifications turn on. */
  notifier?: Pick<CheckinNotifier, "permission"> | undefined;
  /** The section to open at (the student menu's Language, Get help, plan and back-up), once it is on the page; `at`
   * tells two requests for the same section apart. */
  section?: { id: string; at: number } | null;
  /** About → Licences: THIRD_PARTY_NOTICES and the asset register, as the app ships them. */
  notices?: readonly NoticeEntry[]; assets?: readonly AssetEntry[];
}) {
  const t = useT();
  const view = useProfiles(profiles);
  const [failed, setFailed] = useState(false);
  const [ledgerOpen, setLedgerOpen] = useState(false);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [keysOpen, setKeysOpen] = useState(false);
  const [reportsOpen, setReportsOpen] = useState(false);
  // The core's sections for this build's stage, in its order; the stage 2 sections until it answers (the same list on
  // a stage 2 core) or when it cannot.
  const [sections, setSections] = useState<string[]>([...STAGE_2_SECTIONS]);
  // The selected section, the only one whose body is on the page; the one the page was opened at, else the first.
  const [current, setCurrent] = useState<SectionId>(section?.id ?? STAGE_2_SECTIONS[0]);
  // Counts the requests to land on the selected section (a list click, a deep link, Help's way to Data export), so
  // its heading takes focus once that section is on the page.
  const [landings, setLandings] = useState(0);
  const page = useRef<HTMLDivElement>(null);
  const installing = useInstallations(profiles, chooseSource);
  const characterKeys = useCharacterKeys(view.active, view.settings);
  const missing = view.active !== null && view.settings === null;
  useEffect(() => {
    if (missing) profiles.loadSettings().catch(() => setFailed(true));
  }, [profiles, missing]);
  useEffect(() => {
    let live = true;
    setSections([...STAGE_2_SECTIONS]);
    new SettingsSections(profiles.commands).read().then((ids) => { if (live && ids.length > 0) setSections(ids); }, () => undefined);
    return () => { live = false; };
  }, [profiles, view.active]);
  const title = (id: SectionId): string => {
    const named: Record<string, string> = { egress: "ledger.title", accessibility_language: "settings.section.accessibility" };
    const key = named[id];
    return key !== undefined ? t(key) : t(`settings.section.${id}`);
  };
  // A section the core does not list (a stale deep link) cannot be shown: the first one is.
  const shown: SectionId = sections.includes(current) ? current : (sections[0] ?? current);
  const go = (id: SectionId) => {
    setCurrent(id);
    setLandings((n) => n + 1);
  };
  useEffect(() => {
    if (landings === 0) return;
    const target = page.current?.querySelector<HTMLElement>("[data-settings-section]");
    target?.scrollIntoView?.({ block: "start" });
    target?.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true });
  }, [landings]);
  const shownAt = useRef<number | null>(null);
  useEffect(() => {
    if (section === null || shownAt.current === section.at || !sections.includes(section.id)) return;
    shownAt.current = section.at;
    go(section.id);
  });
  const preferences = (section: "accessibility" | "study") => view.settings !== null
    ? <Preferences profiles={profiles} settings={view.settings} section={section} onOpenKnows={onOpenKnows} onKeys={() => setKeysOpen(true)} />
    : failed ? <ErrorState message={t("settings.unavailable")} /> : <div role="status" aria-busy="true"><span className="sr-only">{t("settings.loading")}</span><Skeleton lines={4} /></div>;
  const body = (id: SectionId): ReactNode => {
    switch (id) {
      case "online_features":
        return <OnlineFeaturesSection profiles={profiles} chooseSource={chooseSource} notifier={notifier} />;
      case "profiles":
        return <Card><div className="py-3"><ProfilesSection profiles={profiles} /></div></Card>;
      case "tutor_models":
        return <Card><div className="py-3"><TutorModels installing={installing} /></div><StoredSwitch profiles={profiles} settings={view.settings} name="faster_tutor" /></Card>;
      case "content":
        return <Card><div className="py-3"><ContentPacks installing={installing} profiles={profiles} /></div></Card>;
      case "egress":
        return (
          <Card>
            <div className="py-3">
              <Disclosure summary={t("ledger.show")} open={ledgerOpen} onToggle={() => setLedgerOpen((v) => !v)}>
                {ledgerOpen && <Ledger connectivity={profiles.connectivity} />}
              </Disclosure>
            </div>
            <div className="py-3">
              <Disclosure summary={t("ledger.schema.title")} open={schemaOpen} onToggle={() => setSchemaOpen((v) => !v)}>
                {schemaOpen && <ReportSchema profiles={profiles} />}
              </Disclosure>
            </div>
          </Card>
        );
      case "accessibility_language":
        return <>{preferences("accessibility")}{voiceSettings !== undefined && !sections.includes("voice") && <Card><div className="py-3">{voiceSettings}</div></Card>}</>;
      case "study":
        return preferences("study");
      case "tutor":
        return <Card><TutorSection profiles={profiles} settings={view.settings} /></Card>;
      case "data_export":
        return <Card>{view.active !== null ? <DataExport profiles={profiles} chooseSource={chooseSource} /> : <div className="py-3"><Skeleton lines={2} /></div>}</Card>;
      case "licence":
        return <Card><LicencePanel profiles={profiles} /></Card>;
      case "help":
        return (
          <>
            <Card>
              <div className="flex flex-col gap-4 py-4">
                <OfflineHelpPanel profiles={profiles} />
              </div>
            </Card>
            <Card>
              <div className="flex flex-col gap-4 py-4">
                <HealthPanel profiles={profiles} />
                <CrashReview profiles={profiles} />
                {view.active !== null && (
                  <>
                    <Disclosure summary={t("help.reports_title")} open={reportsOpen} onToggle={() => setReportsOpen(v => !v)}>
                      {reportsOpen && <ProblemReportList profiles={profiles} />}
                    </Disclosure>
                    {sections.includes("data_export") && <div><Button onClick={() => go("data_export")}>{t("settings.help.support_link")}</Button></div>}
                  </>
                )}
                <div><Button onClick={() => setKeysOpen(true)}><Icon name="keyboard" size={16} />{t("keys.open")}</Button></div>
                <p className="text-ui text-muted">{t("settings.computer_controls")}</p>
              </div>
            </Card>
          </>
        );
      case "about":
        return (
          <Card>
            <AboutPanel profiles={profiles} {...(notices !== undefined ? { notices } : {})} {...(assets !== undefined ? { assets } : {})} />
            <div className="py-4"><AppUpdates installing={installing} /></div>
          </Card>
        );
      case "parent_summary":
        return <Card><div className="py-3"><ParentSummary profiles={profiles} /></div></Card>;
      case "alerts":
        return <Card><div className="py-3"><Alerts profiles={profiles} /></div></Card>;
      case "voice":
        if (voiceSettings !== undefined) return <Card><div className="py-3">{voiceSettings}</div></Card>;
        break;
    }
    return <Card><div className="py-3"><Notice message={t("settings.section.not_built")} /></div></Card>;
  };
  return (
    <div ref={page} className="flex min-h-full flex-col">
      <ScreenHeader end={onClose !== undefined ? <Button tone="ghost" onClick={onClose}><Icon name="go-back" size={16} />{t("action.back")}</Button> : undefined}>
        <h1 className="text-ui font-semibold text-ink">{t("settings.title")}</h1>
      </ScreenHeader>
      <div className="@container flex flex-1 flex-col">
        {/* Wide enough (the list, a gap and a readable section): the side list; otherwise one select above the section. */}
        <div className="flex flex-col gap-6 px-8 pt-4 pb-12 @3xl:flex-row @3xl:items-start @3xl:gap-8">
          <nav aria-label={t("settings.sections")} className="shrink-0 @3xl:sticky @3xl:top-4 @3xl:w-60">
            <select aria-label={t("settings.sections")} data-settings-select value={shown} onChange={(event) => go(event.target.value)} className={`${FIELD_LOOK} max-w-80 @3xl:hidden`}>
              {sections.map((id) => <option key={id} value={id}>{title(id)}</option>)}
            </select>
            <div className="hidden flex-col gap-0.5 @3xl:flex">
              {sections.map((id) => (
                <button key={id} type="button" data-settings-link={id} aria-current={shown === id ? "true" : undefined} onClick={() => go(id)}
                  className="jana-press relative flex min-h-10 items-center rounded-control px-3 text-start text-ui font-medium text-muted after:absolute after:inset-x-0 after:-inset-y-0.5 after:content-[''] hover:bg-wash hover:text-ink aria-[current=true]:bg-wash aria-[current=true]:font-semibold aria-[current=true]:text-ink">
                  {title(id)}
                </button>
              ))}
            </div>
          </nav>
          <div className="flex max-w-170 min-w-0 flex-1 flex-col gap-8">
            <Section key={shown} id={shown} heading={title(shown)}>{body(shown)}</Section>
          </div>
        </div>
      </div>
      <KeyboardHelp open={keysOpen} onClose={() => setKeysOpen(false)} characterKeys={characterKeys} />
    </div>
  );
}
