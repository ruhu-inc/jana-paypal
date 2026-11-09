// The Last time card (SPEC §19.5.2, W8 phase 1): the sentence the core made from facts it chose by code (the last
// session's day, mode and subject, cards due, the nearest exam, a research pack ready). The card shows that sentence
// as text and adds no number of its own; a card the core cannot give (an older core, a failed read, nothing loaded
// yet) is simply not shown. On Home it is the first row of the Continue learning block (`useContinueBlock`): the
// sentence verbatim, no card chrome and no heading of its own, the block's heading leading instead. In Study's library
// it stands alone as a card.
import type { CompanionView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button } from "../primitives/index.js";
import { useContinueBlock } from "./Home.js";

export function LastTimeCard({ view, onOpenStudy }: { view: CompanionView; onOpenStudy?: () => void }) {
  const t = useT();
  const embedded = useContinueBlock();
  const opener = view.status === "ready" ? view.opener : null;
  if (opener === null) return null;
  const facts = opener.facts;
  const study = onOpenStudy !== undefined && <Button size="sm" tone="ghost" onClick={onOpenStudy}>{t("home.last_time.open_study")}</Button>;
  if (embedded) {
    return (
      <div data-last-time={opener.opener_id} data-last-mode={facts.last_session_mode ?? ""} className="flex flex-wrap items-start justify-between gap-3">
        <p data-last-time-text="" className="min-w-0 flex-1 text-body text-ink">{opener.text}</p>
        {study}
      </div>
    );
  }
  return (
    <section aria-labelledby="last-time-title" data-last-time={opener.opener_id} data-last-mode={facts.last_session_mode ?? ""}
      className="flex flex-col gap-2 rounded-card border border-line bg-canvas px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="last-time-title" className="flex items-center gap-2 text-ui font-medium text-ink"><Icon name="timer" size={16} />{t("home.last_time.title")}</h2>
        {study}
      </div>
      <p data-last-time-text="" className="text-body text-ink">{opener.text}</p>
    </section>
  );
}
