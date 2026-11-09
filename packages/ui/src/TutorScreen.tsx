import { TutorLearningProvider, useTutorLearning } from "./tutor/LearningState.js";
// The tutor workspace (UXS-14, D89; direction A "Studio"): the main column holds the header (the session's title, its
// subject, the mode chip, the hint rung while a hint is open and the board toggle), the conversation centred at most
// 720 px wide and the docked composer with the suggestions above it; the working board is a panel beside the
// conversation on `canvas`, the way a canvas opens beside a chat. The board opens when the tutor works
// a problem on it and the student can close it; below 560 px it is a bottom sheet over the conversation, and the
// toggle shows a dot when the board changed while it was closed. Ctrl+Shift+B shows or hides it (LOC-12). The mode chip
// is a menu (UXS-18: Guided, Solution, Check my working; Solution whenever asked, D87); More holds "Let support see this
// session" (OBS-18, confirmed first, nothing sent) and "Rename": the chat is titled from its first exchange and the
// student can rename it in place (UXS-68). When the engine sends the calm screen's notice the workspace is
// covered by the calm screen (UXS-43) and stays mounted under it, so going back finds the board and draft as they were.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { CLIENT_KEYS, shareWithSupport } from "@jana/client-core";
import type { Card } from "@jana/contracts-ts";
import { Board } from "./board/Board.js";
import { Composer } from "./composer/Composer.js";
import { TutoringUsage } from "./composer/TutoringUsage.js";
import { ScreenHeader, useFrame } from "./frame/Frame.js";
import { boardBeside } from "./frame/layout.js";
import { useT } from "./i18n.js";
import { Icon } from "./icons.js";
import { Button, Chip, ConfirmDialog, FIELD_LOOK, IconButton, MenuButton, SubjectDot, Toast } from "./primitives/index.js";
import { CalmScreen, type Helpline } from "./safety/CalmScreen.js";
import { useStepIdle } from "./status/idle.js";
import { ConnectionBanner } from "./status/StatusLine.js";
import { useSelect, useSession } from "./store.js";
import { TutorAnnouncements } from "./transcript/Announcements.js";
import { Transcript } from "./transcript/Transcript.js";
import { VoicePresence, presencePlacement, type PresencePlacement } from "./tutor/VoicePresence.js";

/** The modes the mode menu offers (UXS-18, PED-01): Guided, Solution and Check my working. */
const MODES = ["guided", "solution", "check_working"] as const;

/** The open hint's rung, "2/5", while the newest turn holds a hint card (UXS-14 header). */
function useOpenHint(sessionId: string): { rung: number; of: number } | null {
  const shown = useSelect((s) => {
    const ids = s.sessions[sessionId]?.turn_ids ?? [];
    const last = ids[ids.length - 1];
    const parts = last === undefined ? [] : (s.turns[last]?.part_ids ?? []);
    for (const id of [...parts].reverse()) {
      const card = s.parts[id]?.card?.card as (Card & { rung?: number; of?: number }) | undefined;
      if (card?.card_type === "hint" && card.rung !== undefined && card.of !== undefined) return `${card.rung}/${card.of}`;
    }
    return null;
  });
  if (shown === null) return null;
  const [rung, of] = shown.split("/").map(Number);
  return { rung: rung ?? 0, of: of ?? 0 };
}

/** The latest `calm_screen` notice of the session (UXS-43, PRV-14) and the distress template's words from its turn. */
function useCalmNotice(sessionId: string): { noticeId: string; text: string } | null {
  const found = useSelect((s) => {
    const ids = s.sessions[sessionId]?.turn_ids ?? [];
    for (const turnId of [...ids].reverse()) {
      const notice = Object.values(s.parts).find((p) => p.kind === "notice" && p.turn_id === turnId && (p.notice as { code?: string } | undefined)?.code === "calm_screen");
      if (notice !== undefined) return `${notice.part_id}|${turnId}`;
    }
    return null;
  });
  const [noticeId = "", turnId = ""] = found?.split("|") ?? [];
  const text = useSelect((s) => (s.turns[turnId]?.part_ids ?? []).map((id) => s.parts[id]).filter((p) => p?.kind === "text").map((p) => p?.text ?? "").join(PARAGRAPH));
  return found === null ? null : { noticeId, text };
}

const PARAGRAPH = String.fromCharCode(10, 10);

/** Two 280 px reading columns fit before navigation leaves its phone layout. */
function useBoardColumns(layout: Parameters<typeof boardBeside>[0]): boolean {
  const [beside, setBeside] = useState(() => typeof window.matchMedia === "function"
    ? window.matchMedia("(min-width: 560px)").matches : boardBeside(layout));
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(min-width: 560px)");
    const update = () => setBeside(media.matches);
    media.addEventListener("change", update);
    update();
    return () => media.removeEventListener("change", update);
  }, []);
  return beside;
}

/** What the board shows, as one string: lines and their statuses, so a change while it is closed can be marked. */
function useBoardSignature(sessionId: string): { boardId: string | null; lines: number; signature: string } {
  const boardId = useSelect((s) => {
    const ids = s.sessions[sessionId]?.board_ids ?? [];
    return ids[ids.length - 1] ?? null;
  });
  const signature = useSelect((s) => {
    const board = boardId === null ? undefined : s.boards[boardId];
    if (board === undefined) return "";
    return `${board.knowns.length}:${board.step_ids.map((id) => `${id}=${board.steps[id]?.status ?? ""}`).join(",")}`;
  });
  const lines = useSelect((s) => (boardId === null ? 0 : (s.boards[boardId]?.step_ids.length ?? 0) + (s.boards[boardId]?.knowns.length ?? 0)));
  return { boardId, lines, signature };
}

function TutorWorkspace({ sessionId, subjectControl, back, note, helplines = [], onEnd, noTutor }: {
  sessionId: string; subjectControl?: ReactNode;
  /** In place of the composer when no tutor can answer now (SPEC §10.3): the conversation stays readable. */
  noTutor?: ReactNode;
  onEnd?: () => Promise<boolean>;
  /** The verified helplines of the safety-resources pack for the calm screen (UXS-43); none until a pack verifies them. */
  helplines?: readonly Helpline[];
  /** The way back to where the tutor was opened from with its context (UXS-01: "Back to practice question 4"). */
  back?: { label: string; onClick: () => void };
  /** The line under the composer, where the tutor was opened with something loaded (a mock question to review, UXS-26). */
  note?: string;
}) {
  const t = useT();
  const [ending, setEnding] = useState(false), [endFailed, setEndFailed] = useState(false);
  const { layout } = useFrame();
  const learning = useTutorLearning();
  const beside = useBoardColumns(layout);
  const title = useSelect((s) => s.sessions[sessionId]?.title ?? "");
  const mode = useSelect((s) => s.sessions[sessionId]?.mode ?? "guided");
  const subject = useSelect((s) => s.sessions[sessionId]?.subject ?? "");
  const hint = useOpenHint(sessionId);
  const session = useSession();
  const calm = useCalmNotice(sessionId);
  // A calm screen the student has left stays left for this visit (UXS-43); locking the profile clears it with the store.
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(() => new Set());
  const [sharing, setSharing] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => {
    if (done === null && failed === null) return;
    const timer = setTimeout(() => {
      setDone(null);
      setFailed(null);
    }, CLIENT_KEYS.ui_toast_ms);
    return () => clearTimeout(timer);
  }, [done, failed]);
  const { boardId, lines, signature } = useBoardSignature(sessionId);
  // The student's own choice for this board; until then the board opens beside the conversation once it has lines.
  const [chosen, setChosen] = useState<{ boardId: string | null; open: boolean } | null>(null);
  const choice = chosen !== null && chosen.boardId === boardId ? chosen.open : null;
  const open = choice ?? (beside && lines > 0);
  const [seen, setSeen] = useState(signature);
  useEffect(() => {
    if (open) setSeen(signature);
  }, [open, signature]);
  const changed = !open && lines > 0 && signature !== seen;
  const toggle = useCallback(() => setChosen({ boardId, open: !open }), [boardId, open]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.code === "KeyB") {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  const subjectName = subject === "" ? "" : t(`settings.profiles.subject.${subject === "maths" ? "mathematics" : subject}`);
  const toggleLabel = open ? t("board.hide") : changed ? t("board.show_changed") : t("board.show");
  // The presence (UXS-53) follows the board: docked at its top while open, by the composer while closed, above the
  // composer at phone width; never on the calm screen.
  const placement = presencePlacement(layout, open);
  const calmShown = calm !== null && !dismissed.has(calm.noticeId);
  const presenceHere = (where: PresencePlacement) => !calmShown && placement === where ? <VoicePresence placement={where} /> : null;
  const board = <>{presenceHere("docked")}<Board sessionId={sessionId} onClose={() => setChosen({ boardId, open: false })} /></>;
  const share = () => {
    setSharing(false);
    if (session === null) return;
    shareWithSupport(session.commands, sessionId).then(() => setDone("support.shared"), () => setFailed("support.share_failed"));
  };
  const covered = calm !== null && !dismissed.has(calm.noticeId);
  useStepIdle(sessionId, session, calm !== null || learning !== "open"); // LRN-20: never while the calm screen holds the lesson (PRV-14)
  return (
    <div data-problem-status={learning} className="relative flex h-full min-h-0 bg-paper text-ink">
      <TutorAnnouncements key={sessionId} sessionId={sessionId} />
      {covered && (
        <div className="absolute inset-0 z-dialog flex">
          <CalmScreen text={calm.text} helplines={helplines} onBack={() => setDismissed((all) => new Set([...all, calm.noticeId]))} />
        </div>
      )}
      <ConfirmDialog open={sharing} message={t("support.share_confirm")} details={[t("support.share_detail")]} confirm={t("support.share_yes")} cancel={t("card.confirm_no")}
        onConfirm={share} onCancel={() => setSharing(false)} />
      {done !== null && <Toast message={t(done)} />}
      {failed !== null && <Toast message={t(failed)} />}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col" inert={covered}>
        <ScreenHeader wrap end={<>
          {hint !== null && <Chip tone="hint" icon="hint">{t("card.hint_rung", { rung: hint.rung, of: hint.of })}</Chip>}
          <span className="relative inline-flex">
            <IconButton icon="board" label={toggleLabel} aria-pressed={open} aria-keyshortcuts="Control+Shift+B" onClick={toggle} />
            {changed && <span aria-hidden="true" data-board-changed="" className="pointer-events-none absolute end-2 top-2 size-2 rounded-pill bg-ink" />}
          </span>
          {session !== null && (
            <MenuButton label={t("frame.more")} items={[
              { id: "rename", label: t("tutor.rename"), onSelect: () => setRenaming(title) },
              { id: "share", label: t("support.share"), onSelect: () => setSharing(true) },
              ...(onEnd === undefined ? [] : [{ id: "end", label: t("notes.end_session"), disabled: ending, onSelect: () => {
                if (ending) return;
                setEnding(true); setEndFailed(false);
                void onEnd().then((ok) => { if (!ok) setEndFailed(true); }).finally(() => setEnding(false));
              } }]),
            ]}
              trigger={({ ref, ...menu }) => (
                <button ref={ref} type="button" {...menu} aria-label={t("frame.more")} className="jana-press relative inline-flex size-10 items-center justify-center rounded-control text-muted after:absolute after:-inset-0.5 after:content-[''] hover:bg-wash hover:text-ink aria-expanded:bg-wash aria-expanded:text-ink">
                  <Icon name="more" />
                </button>
              )} />
          )}
        </>}>
          {back !== undefined && <span className="-ms-3"><Button tone="ghost" size="sm" onClick={back.onClick}><Icon name="go-back" size={16} />{back.label}</Button></span>}
          {renaming !== null && session !== null ? (
            <form className="flex min-w-0 items-center gap-2" data-rename="" onSubmit={(event) => {
              event.preventDefault();
              const words = renaming.trim();
              setRenaming(null);
              if (words !== "" && words !== title) void session.rename(words).catch(() => setFailed("tutor.rename_failed"));
            }}>
              <label htmlFor={`rename-${sessionId}`} className="sr-only">{t("tutor.rename_label")}</label>
              {/* The field takes focus as Rename opens it: the student chose to type here. */}
              <input id={`rename-${sessionId}`} autoFocus maxLength={120} value={renaming} onChange={(event) => setRenaming(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); setRenaming(null); } }} className={`${FIELD_LOOK} min-h-9 w-64 text-ui`} />
              <Button size="sm" type="submit">{t("tutor.rename_save")}</Button>
              <Button size="sm" tone="ghost" onClick={() => setRenaming(null)}>{t("action.cancel")}</Button>
            </form>
          ) : <h1 className="min-w-20 truncate text-ui font-semibold">{title !== "" ? title : t("composer.new_question")}</h1>}
          {subjectControl ?? (subject !== "" && <Chip><SubjectDot subject={subject === "maths" ? "mathematics" : subject} />{subjectName}</Chip>)}
          {/* On a phone the session's title keeps the header's room and the mode chip steps aside. */}
          {layout !== "compact" && (
            <MenuButton label={t("tutor.mode")} align="start"
              items={MODES.map((m) => ({ id: m, label: t(`tutor.mode.${m}`), checked: m === mode, onSelect: () => { if (m !== mode) void session?.setMode(m).catch(() => setFailed("tutor.mode_failed")); } }))}
              trigger={({ ref, ...menu }) => (
                <button ref={ref} type="button" {...menu} data-mode={mode} aria-label={`${t("tutor.mode")}: ${t(`tutor.mode.${mode}`)}`}
                  className="jana-press relative inline-flex min-h-9 items-center gap-1 rounded-chip border border-line px-3 text-meta font-medium text-ink after:absolute after:-inset-1 after:content-[''] hover:bg-wash aria-expanded:bg-wash">
                  <span className="font-regular text-faint">{t("tutor.mode")}: </span>
                  {t(`tutor.mode.${mode}`)}
                  <Icon name="collapse" size={16} />
                </button>
              )} />
          )}
        </ScreenHeader>
        {endFailed && <p role="status" className="px-6 text-ui text-muted">{t("notes.end_failed")}</p>}
        <ConnectionBanner sessionId={sessionId} />
      <div data-tutor-workspace="" className="flex min-h-0 min-w-0 flex-1">
      <div data-tutor-conversation="" className="flex min-h-0 min-w-0 flex-1 flex-col">
        <main className="flex min-h-0 flex-1 flex-col">
          <Transcript sessionId={sessionId} />
          {!beside && open && (
            <div data-region="board" data-board-sheet="" className="jana-sheet flex h-1/2 min-h-0 shrink-0 flex-col overflow-hidden rounded-t-sheet bg-canvas shadow-overlay-2">
              {board}
            </div>
          )}
          {presenceHere("floating")}
          {presenceHere("phone")}
          {noTutor !== undefined && noTutor !== null ? <div className="px-4 pb-4"><TutoringUsage key={sessionId} sessionId={sessionId} />{noTutor}</div> : <Composer sessionId={sessionId} note={note ?? t("tutor.dock_note")} />}
        </main>
      </div>
      {beside && open && (
        <aside data-region="board" data-board-panel="" tabIndex={-1} className="jana-tutor-board jana-panel flex min-h-0 shrink-0 flex-col border-s border-line">
          {board}
        </aside>
      )}
      </div>
      </div>
    </div>
  );
}

export function TutorScreen(props: Parameters<typeof TutorWorkspace>[0]) {
  return <TutorLearningProvider sessionId={props.sessionId}><TutorWorkspace {...props} /></TutorLearningProvider>;
}
