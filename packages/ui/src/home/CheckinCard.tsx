// The check-in card on Home (SPEC §19.5.5, W8 phase 3): the one check-in the core says is due today, in Jana's own
// words (the commitment's `text`, at most 140 characters), shown as plain text, never as markup. "Answer in Study"
// opens **Your tutor** with the check-in as its opener; "Not now" dismisses it, and the core remembers the dismissal.
// Nothing is shown when nothing is due or the core has no check-ins (an older core). Inside Home's Continue learning
// block (`useContinueBlock`) it is a row of the block, its heading one level down and its buttons secondary, so the
// plan's button stays the block's one primary action.
import type { CheckinView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Notice } from "../primitives/index.js";
import { useContinueBlock } from "./Home.js";

export function CheckinCard({ view, onOpen, onDismiss }: { view: CheckinView; onOpen: (commitmentId: string) => void; onDismiss: (commitmentId: string) => void }) {
  const t = useT();
  const embedded = useContinueBlock();
  const item = view.status === "ready" ? view.item : null;
  if (item === null) return null;
  const Heading = embedded ? "h3" : "h2";
  return (
    <section aria-labelledby="checkin-title" data-checkin={item.commitment_id} data-checkin-kind={item.kind}
      className={embedded ? "flex flex-col gap-3 rounded-control bg-panel px-4 py-3" : "flex flex-col gap-3 rounded-card border border-line bg-canvas px-5 py-4"}>
      <Heading id="checkin-title" className="flex items-center gap-2 text-ui font-medium text-ink"><Icon name="study-area" size={16} />{t("home.checkin.title")}</Heading>
      <p data-checkin-text="" className="whitespace-pre-wrap break-words text-body text-ink">{item.text}</p>
      {view.notice === "notifications_denied" && <Notice message={t("home.checkin.notifications_denied")} />}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" tone={embedded ? "secondary" : "primary"} onClick={() => onOpen(item.commitment_id)}>{t("home.checkin.open")}</Button>
        <Button size="sm" tone="ghost" onClick={() => onDismiss(item.commitment_id)}>{t("home.checkin.dismiss")}</Button>
      </div>
    </section>
  );
}
