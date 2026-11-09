// Settings → Help → "How the tutor is running" (OBS-15): what a family needs to see why the tutor behaves as it does,
// in plain words. Each problem is one line with one action; then the computer's hardware class and why (PLT-08), the
// model and content versions, and the connection with the online features that are on. It reads only (an open
// section, TABLE UXS-46a), so it is not one of the family settings groups (D53).
import { useEffect, useState, type ReactNode } from "react";
import {
  hardwareReason, hardwareReport, healthProblems, installedModels, readHealthReport, unverifiedSubjects, type HealthReport, type ProfileController,
} from "@jana/client-core";
import type { OnlineFeature } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Disclosure, ErrorState, Skeleton, useToggle } from "../primitives/index.js";
import { useShortfallText } from "../status/DegradedNotices.js";

const SUBJECTS = new Set(["mathematics", "physics", "chemistry", "biology", "accounting"]);

/** A subject's name, or its id when this version does not know it. */
export function useSubjectName(): (subject: string) => string {
  const t = useT();
  return (subject) => (SUBJECTS.has(subject) ? t(`settings.profiles.subject.${subject}`) : subject);
}

function Line({ id, message, action }: { id: string; message: string; action: string }) {
  return (
    <li data-health-line={id} className="flex flex-col gap-1 rounded-card border border-line p-3">
      <span className="text-ui text-ink">{message}</span>
      <span className="text-meta text-muted">{action}</span>
    </li>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-meta text-muted">{label}</span>
      <div className="text-ui text-ink">{children}</div>
    </div>
  );
}

function Report({ report }: { report: HealthReport }) {
  const t = useT();
  const subjectName = useSubjectName();
  const problems = healthProblems(report);
  const unverified = unverifiedSubjects(report.packs);
  const reason = hardwareReason(report.models);
  const hardware = hardwareReport(report.models);
  const shortfallText = useShortfallText();
  const models = installedModels(report.models);
  const online = report.connectivity === null ? [] : (Object.entries(report.connectivity.features) as [OnlineFeature, { enabled: boolean } | undefined][]).filter(([, f]) => f?.enabled === true).map(([name]) => name);
  return (
    <div className="flex flex-col gap-4 pt-2">
      {problems.length === 0 && unverified.length === 0 ? (
        <p className="text-ui text-ink">{t("health.all_ok")}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {problems.map((id) => <Line key={id} id={id} message={t(`health.problem.${id}`)} action={t(`health.action.${id}`)} />)}
          {unverified.map((subject) => (
            <Line key={`pack-${subject}`} id="pack_unverified" message={t("health.problem.pack_unverified", { subject: subjectName(subject) })} action={t("health.action.pack_unverified")} />
          ))}
        </ul>
      )}
      <Fact label={t("health.label.hardware")}>
        {reason === null ? (
          t("health.class.unknown")
        ) : reason.kind === "class" ? (
          <>
            <p>{t(`health.class.${reason.hardwareClass}`)}</p>
            {reason.basis !== null && <p className="text-meta text-muted">{t(`health.basis.${reason.basis}`)}</p>}
            {hardware?.shortfalls.map((short) => (
              <p key={short.check} data-shortfall={short.check} className="text-meta text-muted">{shortfallText(short)}</p>
            ))}
          </>
        ) : (
          <>
            <p>{t("health.class.unknown")}</p>
            {reason.reason !== null && <p className="text-meta text-muted">{t(`health.no_class.${reason.reason}`)}</p>}
          </>
        )}
      </Fact>
      {hardware?.runtime && (
        <Fact label={t("health.label.runtime")}>
          <p>{t("health.runtime.variant", { variant: hardware.runtime.variant })}</p>
          {hardware.runtime.fallbacks.map((f) => (
            <p key={f.variant} data-runtime-fallback={f.variant} className="text-meta text-muted">
              {t("health.runtime.fallback", { variant: f.variant, reason: f.reason })}
            </p>
          ))}
        </Fact>
      )}
      <Fact label={t("health.label.model")}>
        {models.length === 0 ? (
          t("health.model.none")
        ) : (
          <ul>
            {models.map((model) => (
              <li key={model.modelId}>{t(model.serving ? "health.model.serving" : "health.model.installed", { model: model.modelId, version: model.packVersion ?? "" })}</li>
            ))}
          </ul>
        )}
      </Fact>
      <Fact label={t("health.label.packs")}>
        {report.packs.length === 0 ? (
          t("health.packs.none")
        ) : (
          <ul>
            {report.packs.map((pack, index) => (
              <li key={`${pack.pack_id}-${index}`}>{t("health.pack", { subject: subjectName(pack.subject), level: pack.level, version: pack.version })}</li>
            ))}
          </ul>
        )}
      </Fact>
      {report.health?.perception && report.health.perception.length > 0 && <Fact label={t("health.photo.title")}>
        <ul>{report.health.perception.map((row) => <li key={row.hardware_class}>{t("health.photo.counts", {
          hardware: row.hardware_class, captures: row.captures, confirmed: row.confirmed, edited: row.edited,
          unreadable: row.unreadable,
        })}</li>)}</ul>
      </Fact>}
      <Fact label={t("health.label.connection")}>
        {report.connectivity === null ? (
          t("health.network.unknown")
        ) : (
          <>
            <p>{t(`health.network.${report.connectivity.network}`)}</p>
            <p className="text-meta text-muted">
              {online.length === 0 ? t("health.online_none") : t("health.online_on", { features: online.map((f) => t(`settings.online.feature.${f}`)).join(", ") })}
            </p>
          </>
        )}
      </Fact>
    </div>
  );
}

/** "How the tutor is running", read when it is opened. */
export function HealthPanel({ profiles }: { profiles: ProfileController }) {
  const t = useT();
  const [open, toggle] = useToggle(false);
  const [report, setReport] = useState<HealthReport | "failed" | null>(null);
  useEffect(() => {
    if (!open) return;
    let current = true;
    setReport(null);
    void readHealthReport(profiles.commands).then((read) => {
      if (current) setReport(read.health === null && read.models === null ? "failed" : read);
    });
    return () => {
      current = false;
    };
  }, [open, profiles]);
  return (
    <Disclosure summary={t("health.title")} open={open} onToggle={toggle}>
      {open && (report === null ? <Skeleton lines={4} /> : report === "failed" ? <ErrorState message={t("health.unavailable")} /> : <Report report={report} />)}
    </Disclosure>
  );
}
