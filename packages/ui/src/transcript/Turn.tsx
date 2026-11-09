import { useTutorLearning } from "../tutor/LearningState.js";
import { TurnFailure } from "../status/TurnFailure.js";
// One turn of the transcript: the tutor's parts in order, the live status line or the footer (the student's message is
// its own item in the transcript, so it keeps its node from send to acceptance).
// Ephemeral parts (tool lines, board updates) fold into one run summary once the turn ends; cards never fold and a
// failed item stays outside the fold (UIA-10, UXS-04), and so does the memory-use line, which the student must see
// (MEM-28). Notes saved in the turn because the student asked are announced after its parts (MEM-11). While the turn's
// data is sent to an online feature it says where it goes, and a turn the online tutor answered says so (UIA-23, MDL-24).
// A live reply has one changing activity line. Successful tool receipts, timing, read aloud, save and reporting live
// behind the finished reply's menu. Failed work and memory-use disclosures stay visible.
import { memo, useState } from "react";
import { memoryReadCount, type PartRec, type TurnRec } from "@jana/client-core";
import { TurnEgress } from "../connectivity/EgressNotice.js";
import { useT } from "../i18n.js";
import { MemoryChips } from "../memory/MemoryChip.js";
import { Button, Dialog, Disclosure, IconButton, MenuButton, chipLook } from "../primitives/index.js";
import { Rendered, useRegistry } from "../registry/registry.js";
import { StatusLine, TurnFooter } from "../status/StatusLine.js";
import { useFasterTutor } from "../status/faster.js";
import { shallowArray, useReadOnly, useSelect, useSession } from "../store.js";
import { lifecycleOf } from "./parts.js";
import { phaseOf } from "./tools.js";
import { ReadAloud } from "../voice/SpeechTools.js";
import { ReportSheet } from "./ReportProblem.js";
import { ReplyFollowUps } from "../chips/NextStepChips.js";
import { SaveQuestion } from "../learn/SavedQuestions.js";
import { Character } from "../tutor/Character.js";
import { useTutorAvatar, useTutorName } from "../tutor/look.js";

function keysOf(part: PartRec): string[] {
  if (part.kind === "card" && part.card !== undefined) return [`card:${(part.card.card as { card_type: string }).card_type}`, "part:card"];
  if (part.kind === "tool") {
    const names = Object.values(part.tools ?? {}).map((t) => `tool:${t.name}`);
    return [...names, "part:tool"];
  }
  return [`part:${part.kind}`];
}

/** An online sketch (VSL-43, VSL-44): the rough drawing with "Rough sketch, not checked", or, when every checkable
 * element passed its tool check, the check chip instead; "Remove sketch" takes it off the board and the history. */
interface SketchData { sketch_id: string; status: "checked" | "not_checked"; checks: { result: string; check_id: string | null }[] }

function SketchPart({ sketch, partId }: { sketch: SketchData; partId: string }) {
  const t = useT();
  const session = useSession();
  const [busy, setBusy] = useState(false);
  const checks = sketch.checks.filter((c) => c.result === "pass" && c.check_id !== null).map((c) => c.check_id as string);
  return (
    <div className="grid gap-2" data-sketch-part={sketch.sketch_id}>
      <Rendered keys={["visual:sketch"]} data={sketch} lifecycle="complete" partId={partId} />
      <div className="flex flex-wrap items-center gap-2">
        {sketch.status === "not_checked" && <span data-sketch-label="" className="font-hand text-ui text-muted">{t("visual.sketch_label_unchecked")}</span>}
        {sketch.status === "checked" && <span className={chipLook("verified", "sm")} data-check-id={checks.join(" ")}>{t("visual.sketch_checked")}</span>}
        {session !== null && <Button size="sm" tone="ghost" disabled={busy} onClick={() => { setBusy(true); void session.removeSketch(sketch.sketch_id).catch(() => setBusy(false)); }}>{t("visual.sketch_remove")}</Button>}
      </div>
    </div>
  );
}

/** A visual part renders its resolved spec through `visual:<type>`, else in words (`visual:*`, VSL-16); a sketch
 * part draws its sketch (VSL-43). */
function VisualPart({ part }: { part: PartRec }) {
  const visual = useSelect((s) => (part.visual_id === undefined ? undefined : s.visuals[part.visual_id]));
  if (part.sketch !== undefined) return <SketchPart sketch={part.sketch as unknown as SketchData} partId={part.part_id} />;
  if (visual === undefined) return null;
  const type = (visual.spec as { type?: string }).type ?? "unknown";
  return <Rendered keys={[`visual:${type}`, "visual:*"]} data={visual.spec} lifecycle={lifecycleOf(part)} partId={part.part_id} />;
}

// Quiet until the reply is hovered or focused, never hidden on touch (D109).
const QUIET = "pointer-fine:opacity-0 pointer-fine:group-hover/turn:opacity-100 pointer-fine:group-focus-within/turn:opacity-100";

/** `readAloud` false leaves a text part's Read aloud to the reply's action row. `quiet` and `quietText` keep a Read
 * aloud out of view until its reply is hovered: a card's on an older reply, and an earlier text part's on any reply. */
export const PartView = memo(function PartView({ partId, readAloud = true, cardReadAloud = true, quiet = false, quietText = false }: { partId: string; readAloud?: boolean; cardReadAloud?: boolean; quiet?: boolean; quietText?: boolean }) {
  const part = useSelect((s) => s.parts[partId]);
  const session = useSession();
  const ended = useSelect((s) => s.turns[s.parts[partId]?.turn_id ?? ""]?.status !== "streaming");
  if (part === undefined) return null;
  const onAction = (action: string, payload?: unknown) => {
    if (session === null || action !== "answer") return;
    const { request_id: requestId, answer } = payload as { request_id: string; answer: Record<string, unknown> };
    void session.answerCard(requestId, answer);
  };
  return (
    <div data-anchor data-part-id={partId} data-kind={part.kind} className="jana-reveal">
      {part.kind === "visual" ? (
        <VisualPart part={part} />
      ) : (
        <Rendered keys={keysOf(part)} data={part} lifecycle={lifecycleOf(part)} partId={partId} onAction={onAction} />
      )}
      {/* A question the learner has answered is not read back: there is nothing left to decide. A hint stays worth hearing. */}
      {cardReadAloud && part.kind === "card" && part.status === "complete" && ended && part.card && (part.card.card.card_type === "hint" || (part.card.card.card_type === "question" && part.card.state === "open")) && <div className={`-ms-2 flex flex-wrap items-center ${quiet ? QUIET : ""}`}><ReadAloud source={{ kind: "session_card", part_id: partId }} /></div>}
      {readAloud && part.kind === "text" && part.status === "complete" && ended && <div className={`-ms-2 flex flex-wrap items-center ${quietText ? QUIET : ""}`}><ReadAloud partId={partId} /></div>}
    </div>
  );
});

/** A part holding a memory read that returned records: its line never folds away (MEM-28). */
function readsMemory(part: PartRec): boolean {
  return Object.values(part.tools ?? {}).some((tool) => memoryReadCount(tool) > 0);
}

function failed(part: PartRec): boolean {
  if (part.status === "error") return true;
  return Object.values(part.tools ?? {}).some((tool) => phaseOf(tool.state) === "failed");
}

export const TurnView = memo(function TurnView({ turnId }: { turnId: string }) {
  const t = useT();
  const tutorAvatar = useTutorAvatar();
  const tutorName = useTutorName();
  const registry = useRegistry();
  const session = useSession();
  const readOnly = useReadOnly();
  const learning = useTutorLearning();
  const faster = useFasterTutor();
  const turn = useSelect((s) => s.turns[turnId]);
  const partIds = useSelect((s) => s.turns[turnId]?.part_ids ?? EMPTY, shallowArray);
  const parts = useSelect((s) => partIds.map((id) => s.parts[id]).filter((p): p is PartRec => p !== undefined), shallowArray);
  // A notice is not one of the turn's parts in the projection; it belongs to the turn it was sent in (MEM-11 decline).
  const notices = useSelect((s) => noticesOf(s.parts, turnId), shallowArray);
  const offers = useSelect((s) => notices.filter((id) => (s.parts[id]?.notice as { code?: string } | undefined)?.code === "hint_offer"), shallowArray);
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState(false), [details, setDetails] = useState(false);
  // Only the newest reply keeps its actions in view; older ones show them on hover or focus (D109), never hidden on touch.
  // An idle offer's turn (only the hint-offer line) does not take that place from the reply above it.
  const latest = useSelect((s) => {
    const own = s.turns[turnId];
    const ids = own === undefined ? undefined : s.sessions[own.session_id]?.turn_ids;
    if (ids === undefined || ids[ids.length - 1] === turnId) return true;
    const last = ids.findLastIndex((id) => !offerOnly(s.parts, s.turns[id]?.part_ids ?? EMPTY, id));
    return ids.indexOf(turnId) >= last;
  });
  if (turn === undefined) return null;
  const live = turn.status === "streaming";
  // The hint offer's quiet line (LRN-20, D112) is about now: it shows under the newest reply only, and a turn that
  // carries nothing else (the idle offer) is that line alone, with no name or actions.
  const shownNotices = latest && learning === "open" ? notices : notices.filter((id) => !offers.includes(id));
  if (!live && parts.length === 0 && notices.length > 0 && notices.every((id) => offers.includes(id))) {
    return latest && learning === "open" ? <section aria-label={tutorName} data-turn-id={turnId} data-hint-offer-turn="" className="flex flex-col gap-2">{notices.map((id) => <PartView key={id} partId={id} />)}</section> : null;
  }
  const isEphemeral = (p: PartRec) => p.kind !== "visual" && registry.lookup(...keysOf(p)).entry.renderClass === "ephemeral" && !failed(p) && !readsMemory(p);
  const folded = parts.filter(isEphemeral);
  const checks = folded.reduce((n, p) => n + Math.max(1, Object.keys(p.tools ?? {}).length), 0);
  // The reply's own words are read from the action row: its last finished text part.
  const spoken = live ? undefined : [...parts].reverse().find((p) => p.kind === "text" && p.status === "complete" && !folded.includes(p))?.part_id;
  const spokenCard = spoken === undefined ? [...parts].reverse().find(p => p.kind === "card" && p.status === "complete" && p.card && (p.card.card.card_type === "hint" || (p.card.card.card_type === "question" && p.card.state === "open")))?.part_id : undefined;
  return (
    <section aria-label={tutorName} data-turn-id={turnId} className="group/turn flex flex-col gap-2">
      {/* The tutor's turn is unboxed under a small label with the tutor's name, Jana by default (D105), beside a small
          still drawing of the character the student chose (Settings → Tutor, PRM-03, UXS-53 with voice off). */}
      <p aria-hidden="true" className="flex items-center gap-2 text-label text-tutor">
        <Character id={tutorAvatar} size={16} animate={false} />
        <span data-tutor-name="">{tutorName}</span>
      </p>
      <div className="flex max-w-(--reading-measure) flex-col gap-2 text-body">
        {parts.filter((p) => !folded.includes(p)).map((p) => <PartView key={p.part_id} partId={p.part_id} readAloud={false} cardReadAloud={false} />)}
        {shownNotices.map((id) => <PartView key={id} partId={id} />)}
        <TurnFailure turn={turn} />
        <TurnEgress turnId={turnId} live={live} />
        <MemoryChips sessionId={turn.session_id} turnId={turnId} />
        {live ? <div data-live-activity=""><StatusLine turn={turn} onStop={() => void session?.cancel(turn.turn_id)} onRetry={() => void session?.retryTurn(turn.turn_id)} {...(faster !== null ? { onUseFaster: faster } : {})} /></div> : (
          <div data-reply-actions="" className={`-ms-2 flex flex-wrap items-center gap-0.5 ${latest ? "" : QUIET}`}>
            <ReadAloud {...(spoken ? { partId: spoken } : spokenCard ? { source: { kind: "session_card" as const, part_id: spokenCard } } : {})} renderActions={voiceItems => <SaveQuestion sessionId={turn.session_id} turnId={turnId} renderAction={saveItem => <MenuButton constrain label={t("tutor.reply.actions")} align="start"
              trigger={props => <IconButton icon="more" size="sm" label={t("tutor.reply.actions")} {...props} />}
              items={[
                ...voiceItems,
                ...(!readOnly && saveItem ? [saveItem] : []),
                ...(!readOnly && parts.length > 0 ? [{ id: "report", icon: "report" as const, label: t("report.action"), onSelect: () => setReport(true) }] : []),
                { id: "details", icon: "timer", label: t("tutor.reply.details"), onSelect: () => setDetails(true) },
              ]} />} />} />
            {turn.status !== "complete" && <TurnFooter turn={turn} inline />}
          </div>
        )}
        {!live && latest && parts.length > 0 && !readOnly && <ReplyFollowUps sessionId={turn.session_id} turnId={turnId} />}
      </div>
      <ReportSheet sessionId={turn.session_id} turnIds={[turnId]} open={report} onClose={() => setReport(false)} />
      <Dialog open={details} heading={t("tutor.reply.details")} closeLabel={t("action.close")} onClose={() => setDetails(false)}>
        <TurnFooter turn={turn} />
        {folded.length > 0 && <Disclosure summary={t("tool.summary", { count: checks })} open={open} onToggle={() => setOpen(v => !v)}>
          {folded.map(p => <PartView key={p.part_id} partId={p.part_id} readAloud={false} cardReadAloud={false} />)}
        </Disclosure>}
      </Dialog>
    </section>
  );
});

const EMPTY: string[] = [];

/** A finished turn that carries nothing but the hint-offer line (LRN-20's idle offer, D112). */
function offerOnly(parts: Record<string, PartRec>, partIds: string[], turnId: string): boolean {
  if (partIds.length > 0) return false;
  const notices = Object.values(parts).filter((p) => p.kind === "notice" && p.turn_id === turnId);
  return notices.length > 0 && notices.every((p) => (p.notice as { code?: string } | undefined)?.code === "hint_offer");
}

/** The notices sent in a turn, in the order they arrived. */
function noticesOf(parts: Record<string, PartRec>, turnId: string): string[] {
  const ids = Object.values(parts).filter((p) => p.kind === "notice" && p.turn_id === turnId).map((p) => p.part_id);
  return ids.length === 0 ? EMPTY : ids;
}

export type { TurnRec };
