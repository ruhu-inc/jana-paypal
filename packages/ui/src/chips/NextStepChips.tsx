// Help before the answer and the next step after it (D120). HelpChips are the three ways to ask for help on a question
// in practice mode (ASM-28): Give me a hint, Walk me through it, Explain simply; timed papers and mocks never show them.
// FollowUpChips are at most two next steps after a tutor, study or Ask anything answer (UXS-68): practise this, make
// flashcards from this, explain a step; a chip's words come from the catalogue and never carry the answer. Both compose
// the SuggestionChip primitive and only report the choice: the screen that shows them decides what it does.
import { createContext, useContext, type ReactNode } from "react";
import { HELP_KINDS, MAX_FOLLOW_UPS, followUps, type FollowUpChip, type FollowUpKind, type HelpKind } from "@jana/client-core";
import { useT } from "../i18n.js";
import type { IconName } from "../icons.js";
import { SuggestionChip } from "../primitives/index.js";
import { useTutorLearning } from "../tutor/LearningState.js";
import { useSelect } from "../store.js";

const HELP_ICONS: Record<HelpKind, IconName> = { hint: "hint", walk_through: "go-next", explain_simply: "learn" };
const FOLLOW_UP_ICONS: Record<FollowUpKind, IconName> = { practise: "practice", flashcards: "study", explain_step: "question" };

/** Give me a hint · Walk me through it · Explain simply (ASM-28). */
export function HelpChips({ onHelp, disabled = false, hintKeys, kinds = HELP_KINDS }: { kinds?: readonly HelpKind[]; onHelp: (kind: HelpKind) => void; disabled?: boolean; /** The hint chip's shortcut, e.g. "Alt+H". */ hintKeys?: string }) {
  const t = useT();
  return (
    <div role="group" aria-label={t("help.chips_label")} data-help-chips="" className="flex flex-wrap gap-2">
      {kinds.map((kind) => (
        <SuggestionChip key={kind} icon={HELP_ICONS[kind]} tone={kind === "hint" ? "hint" : "neutral"} data-help-chip={kind} disabled={disabled} {...(kind === "hint" && hintKeys !== undefined ? { "aria-keyshortcuts": hintKeys } : {})} onClick={() => onHelp(kind)}>
          {t(`help.chip.${kind}`)}
        </SuggestionChip>
      ))}
    </div>
  );
}

/** At most two follow-up chips after an answer (UXS-68); nothing when there are none. */
export function FollowUpChips({ chips, onChoose, disabled = false }: { chips: readonly FollowUpChip[]; onChoose: (chip: FollowUpChip) => void; disabled?: boolean }) {
  const t = useT();
  const shown = chips.slice(0, MAX_FOLLOW_UPS);
  if (shown.length === 0) return null;
  return (
    <div role="group" aria-label={t("followup.label")} data-follow-ups="" className="flex flex-wrap gap-2">
      {shown.map((chip) => (
        <SuggestionChip key={chip.kind} icon={FOLLOW_UP_ICONS[chip.kind]} data-follow-up={chip.kind} disabled={disabled} onClick={() => onChoose(chip)}>
          {chip.label}
        </SuggestionChip>
      ))}
    </div>
  );
}

/** What the app offers after a tutor reply and what a chosen chip does (UXS-68). */
export interface FollowUpHandlers {
  /** The kinds offered after this session's newest reply, in order; [] for none. */
  offered: (sessionId: string) => readonly FollowUpKind[];
  /** A next-step chip was chosen. Message help is kept in the composer menu. */
  onChoose: (kind: FollowUpKind, at: { sessionId: string; turnId: string }) => void;
}

const FollowUps = createContext<FollowUpHandlers | null>(null);

export function FollowUpProvider({ value, children }: { value: FollowUpHandlers; children: ReactNode }) {
  return <FollowUps.Provider value={value}>{children}</FollowUps.Provider>;
}

/** The follow-ups under a finished tutor reply: only where the app offers them, only while no question or hint waits for
 * the student (the dock holds that), and never while a turn runs. */
export function ReplyFollowUps({ sessionId, turnId }: { sessionId: string; turnId: string }) {
  const t = useT();
  const handlers = useContext(FollowUps);
  const learning = useTutorLearning();
  const waiting = useSelect((s) => (s.turns[turnId]?.part_ids ?? []).some((id) => {
    const part = s.parts[id];
    const card = part?.card?.card as { card_type?: string } | undefined;
    return part?.kind === "card" && (card?.card_type === "question" || card?.card_type === "hint") && (part.card?.state === "open" || part.card?.state === "answering");
  }));
  const hasText = useSelect((s) => (s.turns[turnId]?.part_ids ?? []).some((id) => s.parts[id]?.kind === "text"));
  if (handlers === null || learning !== "complete" || waiting || !hasText) return null;
  const chips = followUps(handlers.offered(sessionId).filter(kind => kind === "practise"), (kind) => t(kind === "practise" ? "lesson.practise" : `followup.${kind}`));
  return (
    <FollowUpChips chips={chips} onChoose={chip => handlers.onChoose(chip.kind, { sessionId, turnId })} />
  );
}
