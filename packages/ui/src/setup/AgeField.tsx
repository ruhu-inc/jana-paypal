// Age stays on this device; only a teenage or adult band is stored.
import { useId } from "react";
import { MINIMUM_AGE, MAX_AGE, isAdultAge, profileAgeBand } from "@jana/client-core";
import { useT } from "../i18n.js";
import { FIELD_LOOK, Notice } from "../primitives/index.js";

export type AgeCheck = "missing" | "invalid" | "under_13" | "school" | "adult";

/** What an age allows: a teenage or adult profile when the value is supported. */
export function ageCheck(value: string): AgeCheck {
  if (value === "") return "missing";
  const adult = isAdultAge(value);
  return adult === null ? "invalid" : adult ? "adult" : profileAgeBand(value) === null ? "under_13" : "school";
}

/** The field, its one-line reason, and what an entered age means when it does not allow a profile. */
export function AgeField({ value, onChange, disabled = false, labelClass }: {
  value: string; onChange: (value: string) => void; disabled?: boolean; labelClass?: string;
}) {
  const t = useT();
  const help = useId();
  const check = ageCheck(value);
  return (
    <div className="flex flex-col gap-2">
      <label className={labelClass ?? "flex flex-col gap-1.5 text-meta font-medium text-muted"}>
        {t("first_run.age")}
        <input type="number" inputMode="numeric" min={MINIMUM_AGE} max={MAX_AGE} step={1} autoComplete="off" className={FIELD_LOOK} value={value}
          disabled={disabled} required aria-describedby={help} aria-invalid={check === "invalid" || check === "under_13"}
          onChange={(event) => onChange(event.target.value)} />
      </label>
      <p id={help} className="text-meta text-muted">{t("first_run.age_help")}</p>
      {check === "under_13" && <Notice tone="warn" message={t("first_run.under_13")} />}
      {check === "school" && <Notice message={t("first_run.school")} />}
      {check === "invalid" && <Notice tone="warn" message={t("first_run.age_invalid")} />}
    </div>
  );
}
