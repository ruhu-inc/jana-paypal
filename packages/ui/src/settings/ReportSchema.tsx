// What a usage report holds (OBS-20, PRV-09, PRD-19; Settings → "What leaves this device"): read from the published
// report schema (`GET /v1/telemetry/schema`), so the page always says what this version's reports can carry: each field
// with its fixed values, the measurements (counts and timings only) and what they are broken down by. It shows and
// switches nothing; the switch is Usage reports in Online features.
import { useEffect, useState } from "react";
import { readTelemetrySchema, type ProfileController, type ReportField, type TelemetrySummary } from "@jana/client-core";
import { useT } from "../i18n.js";
import { ErrorState, Skeleton } from "../primitives/index.js";

const CODE = "rounded-control bg-panel px-1.5 py-0.5 font-mono text-meta text-ink";

function Values({ field }: { field: ReportField }) {
  const t = useT();
  return field.values === null ? <span className="text-meta text-muted">{t(`ledger.schema.free.${field.name}`)}</span> : (
    <span className="flex flex-wrap gap-1">{field.values.map((v) => <code key={v} className={CODE}>{v}</code>)}</span>
  );
}

/** The schema as data: the gallery and the tests render this directly. */
export function ReportSchemaView({ summary }: { summary: TelemetrySummary }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-4" data-report-schema="">
      <p className="text-ui text-ink">{t("ledger.schema.intro", { days: summary.maxDays ?? 1 })}</p>
      <dl className="flex flex-col divide-y divide-line">
        {summary.fields.map((field) => (
          <div key={field.name} data-report-field={field.name} className="flex flex-wrap items-start justify-between gap-x-6 gap-y-1 py-2">
            <dt className="text-ui font-medium text-ink">{t(`ledger.schema.field.${field.name}`)}</dt>
            <dd className="max-w-100"><Values field={field} /></dd>
          </div>
        ))}
      </dl>
      <div className="flex flex-col gap-2">
        <h4 className="text-ui font-semibold text-ink">{t("ledger.schema.metrics", { count: summary.metrics.length })}</h4>
        <p className="text-meta text-muted">{t("ledger.schema.metrics_help")}</p>
        <ul className="flex flex-wrap gap-1" data-report-metrics="">{summary.metrics.map((m) => <li key={m}><code className={CODE}>{m}</code></li>)}</ul>
      </div>
      {summary.breakdowns.length > 0 && (
        <div className="flex flex-col gap-2">
          <h4 className="text-ui font-semibold text-ink">{t("ledger.schema.breakdowns")}</h4>
          <ul className="flex flex-col gap-1 text-meta text-muted">
            {summary.breakdowns.map((b) => <li key={b.name} data-report-breakdown={b.name}><code className={CODE}>{b.name}</code> {t("ledger.schema.choices", { count: b.values?.length ?? 0 })}</li>)}
          </ul>
        </div>
      )}
      <p className="text-meta text-muted">{t("ledger.schema.never")}</p>
    </div>
  );
}

/** What a usage report holds, read from the core. */
export function ReportSchema({ profiles }: { profiles: ProfileController }) {
  const t = useT();
  const [summary, setSummary] = useState<TelemetrySummary | "loading" | "failed">("loading");
  useEffect(() => {
    let live = true;
    readTelemetrySchema(profiles.commands).then((s) => live && setSummary(s), () => live && setSummary("failed"));
    return () => {
      live = false;
    };
  }, [profiles]);
  if (summary === "loading") return <Skeleton lines={3} />;
  if (summary === "failed") return <ErrorState message={t("ledger.schema.unavailable")} />;
  return <ReportSchemaView summary={summary} />;
}
