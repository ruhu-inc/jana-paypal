// What a new learner is studying: their own course and materials, or the O-level curriculum. One choice, never a kind of
// profile: "own materials" is simply a profile with no subjects, and Settings can add subjects later.
import { useId } from "react";
import { useT } from "../i18n.js";

export function StudyFocusField({ curriculum, onChange, disabled = false }: { curriculum: boolean; onChange: (curriculum: boolean) => void; disabled?: boolean }) {
  const t = useT(), name = useId();
  const option = (value: boolean, label: string, help: string) => (
    <label className="flex min-h-11 cursor-pointer items-start gap-3 rounded-control border border-line-strong bg-canvas px-3 py-2 text-ui text-ink">
      <input type="radio" name={name} className="mt-1" checked={curriculum === value} disabled={disabled} onChange={() => onChange(value)} />
      <span className="flex flex-col gap-0.5"><span className="font-medium">{label}</span><span className="text-meta text-muted">{help}</span></span>
    </label>
  );
  return (
    <fieldset className="flex flex-col gap-2" data-study-focus={curriculum ? "curriculum" : "own"}>
      <legend className="mb-1.5 text-meta font-medium text-muted">{t("first_run.focus")}</legend>
      {option(false, t("first_run.focus_own"), t("first_run.focus_own_help"))}
      {option(true, t("first_run.focus_curriculum"), t("first_run.focus_curriculum_help"))}
    </fieldset>
  );
}
