// "Report a problem" (UXS-44, PRV-22): on every finished tutor reply (its action row) and every tutor line of the board
// (its line menu), by pointer and by keyboard. It opens a sheet over the screen, "What's the problem?" with three
// choices, an optional note of up to 300 characters and an unchecked choice to let support see it, then "Save report" /
// "Cancel". The report is saved on this computer only (`POST /v1/problem-reports`, the M23 local report API); nothing is
// sent. The open profile's reports are listed in Settings → Help, where one the student allowed can go into a report
// for support. Escape or Cancel closes the sheet and focus goes back to what opened it.
import { useId, useState } from "react";
import { ProblemReports, type NewProblemReport } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, Dialog, ErrorState, IconButton } from "../primitives/index.js";
import { useSession } from "../store.js";

const CATEGORIES = ["incorrect", "confusing", "unsafe"] as const satisfies readonly NewProblemReport["category"][];
const NOTE_MAX = 300;

/** The report sheet for the given turns; it starts empty each time it opens. */
export function ReportSheet({ sessionId, turnIds, open, onClose }: { sessionId: string; turnIds: string[]; open: boolean; onClose: () => void }) {
  const t = useT();
  return (
    <Dialog open={open} heading={t("report.action")} closeLabel={t("action.close")} onClose={onClose}>
      {open && <ReportForm sessionId={sessionId} turnIds={turnIds} onClose={onClose} />}
    </Dialog>
  );
}

function ReportForm({ sessionId, turnIds, onClose }: { sessionId: string; turnIds: string[]; onClose: () => void }) {
  const t = useT();
  const session = useSession();
  const id = useId();
  const [category, setCategory] = useState<NewProblemReport["category"] | null>(null);
  const [note, setNote] = useState("");
  const [allow, setAllow] = useState(false);
  const [state, setState] = useState<"editing" | "saving" | "saved" | "failed">("editing");
  if (state === "saved") {
    return (
      <div className="flex flex-col gap-4">
        <div role="status" className="flex flex-col gap-1 text-ui text-ink" data-report-saved="">
          <p>{t("report.saved")}</p>
          {category === "unsafe" && <p>{t("report.unsafe_line")}</p>}
        </div>
        <div><Button tone="primary" onClick={onClose}>{t("action.close")}</Button></div>
      </div>
    );
  }
  const save = () => {
    if (category === null) return;
    // Without a session (the gallery's preview of a reply) there is nowhere to save it: the sheet says so.
    if (session === null) return setState("failed");
    setState("saving");
    new ProblemReports(session.commands).save({ session_id: sessionId, turn_ids: turnIds, category, note: note.trim(), allow_support_export: allow })
      .then(() => setState("saved"), () => setState("failed"));
  };
  return (
    <form className="flex flex-col gap-4" data-report-sheet="" onSubmit={(event) => { event.preventDefault(); save(); }}>
      <fieldset className="flex flex-col gap-2">
        <legend id={id} className="mb-1 text-ui font-semibold text-ink">{t("report.title")}</legend>
        {CATEGORIES.map((c) => (
          <label key={c} className="flex min-h-11 cursor-pointer items-center gap-3 rounded-control border border-line-strong px-3 text-ui text-ink hover:bg-wash has-[:checked]:border-ink has-[:checked]:bg-wash">
            <input type="radio" name={`${id}-category`} checked={category === c} onChange={() => setCategory(c)} className="size-4 shrink-0" />
            {t(`report.category.${c}`)}
          </label>
        ))}
      </fieldset>
      {category === "unsafe" && <p className="text-ui text-ink">{t("report.unsafe_line")}</p>}
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
        {t("report.note")}
        <textarea className="min-h-20 rounded-control border border-line-strong bg-canvas px-3 py-2 text-body text-ink" maxLength={NOTE_MAX}
          value={note} onChange={(event) => setNote(event.target.value)} aria-describedby={`${id}-hint`} />
        <span id={`${id}-hint`} className="font-regular text-meta text-muted">{t("report.note_hint")}</span>
      </label>
      <label className="flex items-center gap-2 text-ui text-ink">
        <input type="checkbox" checked={allow} onChange={(event) => setAllow(event.target.checked)} className="size-4 shrink-0" />
        {t("report.allow_support")}
      </label>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" tone="primary" disabled={category === null || state === "saving"}>{t("report.save")}</Button>
        <Button onClick={onClose}>{t("action.cancel")}</Button>
      </div>
      {state === "failed" && <ErrorState message={t("report.failed")} />}
    </form>
  );
}

/** "Report a problem" in a reply's action row: an icon button whose words show on hover and focus. */
export function ReportProblem({ sessionId, turnId }: { sessionId: string; turnId: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <IconButton icon="report" size="sm" label={t("report.action")} aria-haspopup="dialog" data-report-problem="" onClick={() => setOpen(true)} />
      <ReportSheet sessionId={sessionId} turnIds={[turnId]} open={open} onClose={() => setOpen(false)} />
    </>
  );
}
