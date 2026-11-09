// Home (UXS-12, UXS-67; direction A "Studio"; one primary task after the 2026-11 UX review): it tells the student what
// to do next. "Ready to learn, {name}?" in display type leads to Continue learning, one block that merges the Last time
// sentence (the core's own words, shown verbatim, never reworded or renumbered here; SPEC §19.5.2) with the study
// plan's next activity and its one primary button, Start, Continue or Keep learning (LRN-28 to LRN-33), today's
// check-in from the tutor when one is due (SPEC §19.5.5) and, quiet at its foot, the exam countdown (LRN-44). The
// cards that join the block read `useContinueBlock` and drop their own chrome; standalone (Study's library, the
// gallery) they are unchanged. Under the block a compact row of secondary actions: Ask a question reveals the one box
// (UXS-67, D117: a problem in an installed subject opens the tutor, material the study area, any other question Ask
// anything, whose answer shows above the box, STU-20) and Add material reveals Upload, Paste and Record, each opening
// the study area. Then the quiet rows (`more`: worked-solution search, the diagnostic; UXS-51, PLT-29), the subjects as
// chips, and two cards, "Pick up where you left off" (the newest sessions, each reopening where it was left, UXS-19)
// and today's practice, the ideas the learner model says are due, each with its mastery word (DSN-23). The header
// carries the gentle daily goal chip (LRN-45).
import { createContext, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ChangeEvent, type ReactNode, type RefObject } from "react";
import { CLIENT_KEYS, type DueIdea, type SessionGroup } from "@jana/client-core";
import { ScreenHeader } from "../frame/Frame.js";
import { useT } from "../i18n.js";
import { Icon, type IconName } from "../icons.js";
import { MasteryMark } from "../learn/Mastery.js";
import { Button, SubjectDot, SuggestionChip } from "../primitives/index.js";

export interface HomeSubject {
  subjectId: string;
  subject: string;
  label: string;
  chosen: boolean;
}

export interface HomePickup {
  sessionId: string;
  title: string;
  subject: string;
  subjectLabel: string;
  day: SessionGroup;
}

/** A secondary, de-emphasised row under the actions (worked-solution search, the diagnostic): never a card. */
export interface HomeEntry {
  id: string;
  icon: IconName;
  label: string;
  onClick: () => void;
}

/** The greeting's part of the day, by the computer's local hour. */
export function partOfDay(hour: number): "morning" | "afternoon" | "evening" {
  if (hour >= CLIENT_KEYS.ui_greeting_evening_hour) return "evening";
  return hour >= CLIENT_KEYS.ui_greeting_afternoon_hour ? "afternoon" : "morning";
}

const ContinueBlock = createContext(false);

/** True inside Home's Continue learning block: the cards that join it (Last time, the check-in, the plan) render as
 * rows of the one block, without their own card chrome, instead of as cards that compete with it. */
export function useContinueBlock(): boolean {
  return useContext(ContinueBlock);
}

/** Whether the block has anything to show. Its slots are nodes Home cannot see into (a plan still loading, a card the
 * core did not give, an older core with neither), so the DOM says: any words, or any of the cards' own markers. */
function useHasContent(body: RefObject<HTMLDivElement | null>): boolean {
  const [has, setHas] = useState(true);
  useLayoutEffect(() => {
    const node = body.current;
    if (node === null) return;
    const check = () => setHas((node.textContent ?? "").trim() !== "" || node.querySelector("[data-plan],[data-last-time],[data-checkin],[data-exam-countdown]") !== null);
    check();
    const observer = new MutationObserver(check);
    observer.observe(node, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [body]);
  return has;
}

function Row({ icon, name, meta, subject, onClick, children }: { icon: IconName; name: string; meta: string; subject: string; onClick: () => void; children?: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="jana-press flex min-h-11 w-full items-start gap-3 rounded-control px-2 py-3 text-start text-ink hover:bg-wash">
      <span aria-hidden="true" className="flex size-9 shrink-0 items-center justify-center rounded-control bg-panel text-muted"><Icon name={icon} /></span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-ui font-medium">{name}</span>
        <span className="flex items-center gap-1.5 text-meta text-faint"><SubjectDot subject={subject} />{meta}</span>
        {children}
      </span>
      <span aria-hidden="true" className="self-center text-faint"><Icon name="expand" size={16} /></span>
    </button>
  );
}

/** The three ways to add material, behind the Add material action (UXS-67); each opens the study area. */
export interface HomeMaterials {
  onUpload: (file: File) => void;
  onPaste: () => void;
  onRecord: (file: File) => void;
}

/** Accepted on Home's Upload: what the study area reads (STU-02). Record takes a recording the student holds. */
const UPLOAD_ACCEPT = ".pdf,.docx,.pptx,.txt,.md,.png,.jpg,.jpeg,.webp,application/pdf,text/plain,text/markdown,image/png,image/jpeg,image/webp";
const RECORDING_ACCEPT = ".mp3,.m4a,.wav,.ogg,.webm,audio/*";

function Materials({ id, materials }: { id: string; materials: HomeMaterials }) {
  const t = useT();
  const upload = useRef<HTMLInputElement>(null), record = useRef<HTMLInputElement>(null);
  const picked = (pick: (file: File) => void) => (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) pick(file);
  };
  return (
    <div id={id} role="group" aria-label={t("home.add_material")} data-home-materials="" className="flex flex-wrap gap-2">
      <SuggestionChip icon="study" onClick={() => upload.current?.click()}>{t("home.material.upload")}</SuggestionChip>
      <SuggestionChip icon="notes" onClick={materials.onPaste}>{t("home.material.paste")}</SuggestionChip>
      <SuggestionChip icon="voice" onClick={() => record.current?.click()}>{t("home.material.record")}</SuggestionChip>
      <input ref={upload} type="file" tabIndex={-1} aria-hidden="true" accept={UPLOAD_ACCEPT} onChange={picked(materials.onUpload)} className="sr-only" />
      <input ref={record} type="file" tabIndex={-1} aria-hidden="true" accept={RECORDING_ACCEPT} onChange={picked(materials.onRecord)} className="sr-only" />
    </div>
  );
}

export function Home({ materialsOpen = false, name, ask, materials, answer, notice, plan, countdown, lastTime, checkin, goal, mock, more, subjects, onSubject, pickup, onPickup, practice, onPractice }: {
  name: string;
  /** The one box (the composer, inline), revealed by Ask a question; or null where no tutor answers here. */
  /** Add material starts open, for a learner whose study is their own material. */
  materialsOpen?: boolean;
  ask: ReactNode | null;
  /** The three ways to add material, revealed by Add material, or null where the study area is not available. */
  materials?: HomeMaterials | null;
  /** Ask anything's answer to the box's last general question (STU-20), above the box. */
  answer?: ReactNode;
  /** What still works where no tutor answers (PLT-29), or why the tutor waits while a mock exam is open (AGT-24). */
  notice?: ReactNode;
  /** The study plan card (UXS-12): "Next up", a row of the Continue learning block with its one primary button. */
  plan?: ReactNode;
  /** The daily goal chip in the header (LRN-45), absent when the learner hid it. */
  goal?: ReactNode;
  /** The countdown to the nearest exam date and any reminder due (LRN-44), quiet at the foot of the block. */
  countdown?: ReactNode;
  /** The Last time card (SPEC §19.5.2): its sentence leads the Continue learning block, verbatim. */
  lastTime?: ReactNode;
  /** Today's check-in from the tutor (SPEC §19.5.5), a row of the block when one is due. */
  checkin?: ReactNode;
  /** A mock exam card: one closed after the abandonment window, with "Start a new mock" (TABLE UXS-25a). */
  mock?: ReactNode;
  /** Secondary, de-emphasised rows under the actions: worked-solution search, the diagnostic. */
  more?: HomeEntry[];
  subjects: HomeSubject[];
  onSubject: (subjectId: string) => void;
  pickup: HomePickup[];
  onPickup: (sessionId: string) => void;
  practice: { ideas: DueIdea[]; refreshes: number; subjectLabel: (subject: string) => string };
  onPractice: (idea: DueIdea) => void;
}) {
  const t = useT();
  const shownPickup = pickup.slice(0, CLIENT_KEYS.ui_home_pickup_max);
  // Arriving with focus nowhere (the picker or first run that had it is gone) puts it on the greeting, so a keyboard
  // or screen-reader user starts on this screen instead of at the top of the page.
  const greeting = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (document.activeElement === null || document.activeElement === document.body) greeting.current?.focus();
  }, []);
  const body = useRef<HTMLDivElement>(null);
  const hasContinue = useHasContent(body);
  const continues = plan !== undefined || lastTime !== undefined || checkin !== undefined || countdown !== undefined;
  // The box stays on Home (UXS-12: the hub is the greeting and the Ask box; UXS-67's one input) but after the block,
  // under its own small heading; Add material reveals its three ways on demand (one more click, nothing removed).
  const addId = useId();
  const [addOpen, setAddOpen] = useState(materialsOpen);
  // The profile reads after the first render: open once it says their study is their own material.
  useEffect(() => { if (materialsOpen) setAddOpen(true); }, [materialsOpen]);
  const addable = ask !== null && materials != null;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ScreenHeader {...(goal !== undefined ? { end: goal } : {})} />
      <div className="min-h-0 flex-1 overflow-y-auto px-6 pt-10 pb-12">
        <div className="mx-auto flex w-full max-w-(--layout-composerMax) flex-col gap-12">
          <section aria-labelledby="home-greeting" className="flex flex-col gap-5">
            <div className="flex flex-col gap-1 px-1">
              <h1 id="home-greeting" ref={greeting} tabIndex={-1} className="text-display">{t("home.ready", { name })}</h1>
            </div>
            {continues && (
              <section aria-labelledby="home-continue" data-home-continue="" hidden={!hasContinue}
                className={`${hasContinue ? "flex" : "hidden"} flex-col gap-4 rounded-card border border-line bg-canvas px-5 pt-5 pb-4`}>
                <h2 id="home-continue" className="text-heading text-ink">{t("home.continue")}</h2>
                <div ref={body} className="flex flex-col gap-4">
                  <ContinueBlock.Provider value={true}>
                    {lastTime}
                    {checkin}
                    {plan}
                  </ContinueBlock.Provider>
                  {countdown !== undefined && (
                    <div className="[&>section]:rounded-none [&>section]:border-x-0 [&>section]:border-b-0 [&>section]:border-t [&>section]:bg-transparent [&>section]:px-0 [&>section]:pt-3 [&>section]:pb-0">{countdown}</div>
                  )}
                </div>
              </section>
            )}
            {ask !== null && (
              <section aria-labelledby="home-ask" data-home-ask="" className="flex flex-col gap-3">
                <h2 id="home-ask" className="px-1 text-heading text-ink">{t("home.ask")}</h2>
                {answer}
                {ask}
              </section>
            )}
            {addable && (
              <div role="group" aria-label={t("home.secondary")} data-home-actions="" className="flex flex-col gap-3 px-1">
                <span><Button size="sm" aria-expanded={addOpen} aria-controls={addId} onClick={() => setAddOpen((open) => !open)}><Icon name="study" size={16} />{t("home.add_material")}</Button></span>
                {addOpen && <Materials id={addId} materials={materials} />}
              </div>
            )}
            {notice}
            {subjects.length > 1 && (
              <div role="group" aria-label={t("home.subjects")} className="flex flex-wrap gap-2">
                {subjects.map((s) => (
                  <SuggestionChip key={s.subjectId} aria-pressed={s.chosen} onClick={() => onSubject(s.subjectId)}>
                    <SubjectDot subject={s.subject} />
                    {s.label}
                  </SuggestionChip>
                ))}
              </div>
            )}
            {more !== undefined && more.length > 0 && (
              <ul aria-label={t("home.more")} data-home-more="" className="flex flex-col divide-y divide-line border-t border-line">
                {more.map((entry) => (
                  <li key={entry.id} data-home-entry={entry.id}>
                    <button type="button" onClick={entry.onClick} className="jana-press flex min-h-11 w-full items-center gap-3 rounded-control px-2 text-start text-ui text-muted hover:bg-wash hover:text-ink">
                      <span aria-hidden="true" className="inline-flex text-faint"><Icon name={entry.icon} size={16} /></span>
                      <span className="flex-1">{entry.label}</span>
                      <span aria-hidden="true" className="inline-flex text-faint"><Icon name="go-next" size={16} /></span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
          {mock}
          {(shownPickup.length > 0 || practice.ideas.length > 0) && (
            <div className="flex flex-wrap items-start gap-4">
              {shownPickup.length > 0 && (
                <section aria-labelledby="home-pickup" className="flex min-w-72 flex-1 flex-col gap-2 rounded-card border border-line bg-canvas px-3 pt-5 pb-3">
                  <h2 id="home-pickup" className="px-2 text-heading">{t("home.pickup")}</h2>
                  <ul className="flex flex-col divide-y divide-line">
                    {shownPickup.map((p) => (
                      <li key={p.sessionId} data-pickup={p.sessionId}>
                        <Row icon="tutor" name={p.title} meta={t("home.pickup_meta", { subject: p.subjectLabel, day: t(`frame.group.${p.day}`) })} subject={p.subject} onClick={() => onPickup(p.sessionId)} />
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {practice.ideas.length > 0 && (
                <section aria-labelledby="home-practice" className="flex min-w-72 flex-1 flex-col gap-3 rounded-card border border-line bg-canvas px-3 pt-5 pb-3">
                  <div className="flex flex-col gap-0.5 px-2">
                    <h2 id="home-practice" className="text-heading">{t("home.practice")}</h2>
                    {practice.refreshes > 0 && <p className="text-meta text-faint">{t("home.practice_due", { count: practice.refreshes })}</p>}
                  </div>
                  <ul className="flex flex-col gap-1">
                    {practice.ideas.map((idea) => (
                      <li key={idea.kc.kc_id} data-due-kc={idea.kc.kc_id}>
                        <button type="button" onClick={() => onPractice(idea)} className="jana-press flex min-h-11 w-full items-center justify-between gap-3 rounded-control px-2 py-2 text-start hover:bg-wash">
                          <span className="flex min-w-0 flex-col">
                            <span className="text-ui font-medium text-ink">{idea.kc.statement}</span>
                            <span className="text-meta text-faint">{practice.subjectLabel(idea.subject)}</span>
                          </span>
                          <MasteryMark state={idea.kc.state} />
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
