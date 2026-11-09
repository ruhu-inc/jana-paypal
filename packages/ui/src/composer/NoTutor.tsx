// Where the composer would be when no tutor can answer now (SPEC §10.3): neither the online tutor nor a model on this
// computer. It says why in plain words and offers what still works without one, practice and mock exams from the
// installed packs, instead of taking a question it cannot answer.
import type { TutorReach } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button } from "../primitives/index.js";

export type NoTutorReason = Exclude<TutorReach, "available" | "checking">;

export function NoTutor({ reason, onPractice, onMock }: { reason: NoTutorReason; onPractice: () => void; onMock: () => void }) {
  const t = useT();
  return (
    <div role="status" aria-live="polite" data-no-tutor={reason} className="flex flex-col gap-3 rounded-card border border-line bg-canvas px-4 py-3 text-ui text-ink">
      <p className="flex items-start gap-3">
        <span className="inline-flex shrink-0 pt-0.5 text-info"><Icon name="info" /></span>
        <span>{t(`composer.no_tutor.${reason}`)} {t("composer.no_tutor.still_works")}</span>
      </p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" tone="primary" onClick={onPractice}>{t("composer.no_tutor.practice")}</Button>
        <Button size="sm" onClick={onMock}>{t("composer.no_tutor.mock")}</Button>
      </div>
    </div>
  );
}
