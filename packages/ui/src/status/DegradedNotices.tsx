// The degraded-mode notices above the tutor (ARC-26): the model runtime cannot run, so the tutor is content-only (the
// "model cannot run" state of UXS-20a, shown as the system error state); the computer is below the minimum laptop, so
// the tutor cannot run offline, naming the figure that is short (PLT-29); the open subject's content pack failed
// verification, while other subjects still work; and the app is saving battery (PLT-22).
import { hardwareReport, savingPower, tutorCannotRun, unverifiedSubjects, type Shortfall } from "@jana/client-core";
import type { Health, ModelCatalogue, PackList } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Button, ErrorState, Notice } from "../primitives/index.js";
import { useSubjectName } from "../settings/HealthPanel.js";

/** One failed minimum-laptop check in words, with its figures (PLT-29). */
export function useShortfallText(): (short: Shortfall) => string {
  const t = useT();
  return (short) => {
    const figures = { have: String(short.have), need: String(short.need) };
    if (short.check === "cpu") return t(short.unit === "year" ? "unsupported.cpu_year" : "unsupported.cpu", figures);
    return t(`unsupported.${short.check}`, figures);
  };
}

export function DegradedNotices({ health, packs, subject, models = null, onOpenLessons, onOnlineFeatures }: {
  health: Health | null; packs: PackList["packs"]; subject: string; models?: ModelCatalogue | null;
  onOpenLessons?: () => void; onOnlineFeatures?: () => void;
}) {
  const t = useT();
  const subjectName = useSubjectName();
  const shortfallText = useShortfallText();
  const report = hardwareReport(models);
  // Below the minimum laptop with no school server and no online tier: the unsupported-offline copy, figures named.
  const belowMinimum = report?.hardwareClass === "content_only" && report.tutorPath === "none" && report.shortfalls.length > 0;
  const cannotRun = tutorCannotRun(health) && !belowMinimum && !(report?.tutorPath === "school_server" || report?.tutorPath === "online");
  const unverified = unverifiedSubjects(packs).includes(subject);
  const saving = savingPower(health);
  if (!cannotRun && !belowMinimum && !unverified && !saving) return null;
  return (
    <div className="flex flex-col gap-2 px-4 py-2">
      {belowMinimum && (
        <><Notice tone="warn" message={[t("unsupported.summary"), ...report.shortfalls.map(shortfallText), t("unsupported.offline")].join(" ")} />
          <div className="flex gap-2">{onOpenLessons && <Button onClick={onOpenLessons}>{t("action.open_lessons")}</Button>}{onOnlineFeatures && <Button onClick={onOnlineFeatures}>{t("settings.section.online_features")}</Button>}</div></>
      )}
      {cannotRun && <ErrorState message={t("tutor.cannot_run")} {...(onOpenLessons ? { action: { label: t("action.open_lessons"), onClick: onOpenLessons } } : {})} />}
      {unverified && <Notice tone="warn" message={t("tutor.subject_unverified", { subject: subjectName(subject) })} />}
      {saving && <Notice tone="info" message={t("power.saving")} />}
    </div>
  );
}
