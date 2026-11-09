// The Settings sections the core serves beyond the student's preferences (TABLE UXS-46a): Licence (PLT-19, D84: the
// school licence file if one is installed and the subscription's state; nothing to type, nothing it does ever blocks
// learning), the crash reports waiting for "Send this report?" (OBS-17: each shown exactly as it would be sent, decided
// once, and listed only while crash reports are switched on) and About (version; the licences of everything the app
// ships, PLT-10, from THIRD_PARTY_NOTICES and the asset register; the brand's About placement, DSN-25).
import { useEffect, useState, type ReactNode } from "react";
import {
  decideCrashReport, readLicence, serverVersion, waitingCrashReports, type AssetEntry, type CrashReport, type LicenceStatus, type NoticeEntry, type ProfileController,
} from "@jana/client-core";
import { useT } from "../i18n.js";
import { BrandMark } from "../primitives/BrandMark.js";
import { Button, Disclosure, EmptyState, ErrorState, Notice, Skeleton } from "../primitives/index.js";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 py-3">
      <span className="text-ui font-medium text-ink">{label}</span>
      <span className="text-ui text-muted">{children}</span>
    </div>
  );
}

/** Settings → Licence: what a school installed, and the online subscription's state. */
export function LicenceView({ status }: { status: LicenceStatus | "loading" | "failed" }) {
  const t = useT();
  if (status === "loading") return <Skeleton lines={2} />;
  if (status === "failed") return <ErrorState message={t("licence.failed")} />;
  const date = status.expires_at === null ? "" : t("licence.date", { date: new Date(status.expires_at) });
  const line = {
    none: t("licence.none"),
    active: status.seats === null ? t("licence.active_no_seats", { id: status.licence_id ?? "", date }) : t("licence.active", { id: status.licence_id ?? "", seats: status.seats, date }),
    expired: t("licence.expired", { date }),
    invalid: t("licence.invalid"),
  }[status.state];
  return (
    <div className="flex flex-col divide-y divide-line" data-licence={status.state}>
      <p className="py-3 text-ui text-ink">{line}</p>
      <Row label={t("licence.install_kind")}>{t(`licence.kind.${status.install_kind}`)}</Row>
      <Row label={t("licence.cloud")}>{t(`licence.cloud.${status.entitlements.cloud_tier}`)}</Row>
      {status.clock_behind && <div className="py-3"><Notice tone="warn" message={t("licence.clock_behind")} /></div>}
      <p className="py-3 text-meta text-muted">{t("licence.never_blocks")}</p>
    </div>
  );
}

export function LicencePanel({ profiles }: { profiles: ProfileController }) {
  const [status, setStatus] = useState<LicenceStatus | "loading" | "failed">("loading");
  useEffect(() => {
    let live = true;
    readLicence(profiles.commands).then((s) => live && setStatus(s), () => live && setStatus("failed"));
    return () => {
      live = false;
    };
  }, [profiles]);
  return <LicenceView status={status} />;
}

/** One crash report exactly as it would be sent, with "Send" and "Don't send" (OBS-17). */
export function CrashReportView({ report, onDecide, busy = false }: { report: CrashReport; onDecide: (action: "send" | "dismiss") => void; busy?: boolean }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <li data-crash-id={report.crash_id} className="flex flex-col gap-3 rounded-card border border-line bg-canvas p-4">
      <p className="text-ui font-semibold text-ink">{t("crash.ask")}</p>
      <p className="text-meta text-muted">{t("crash.summary", { process: report.process, version: report.version })}</p>
      <Disclosure summary={t("crash.show")} open={open} onToggle={() => setOpen((v) => !v)}>
        <pre tabIndex={0} className="mt-2 max-h-60 overflow-auto rounded-control bg-panel p-3 font-mono text-meta whitespace-pre-wrap text-ink">{JSON.stringify(report, null, 2)}</pre>
      </Disclosure>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => onDecide("dismiss")}>{t("crash.dont_send")}</Button>
        <Button tone="primary" disabled={busy} onClick={() => onDecide("send")}>{t("crash.send")}</Button>
      </div>
    </li>
  );
}

/** The crash reports waiting for a decision; nothing at all while there are none (the feature is off by default). */
export function CrashReview({ profiles }: { profiles: ProfileController }) {
  const t = useT();
  const [reports, setReports] = useState<CrashReport[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const load = () => {
    waitingCrashReports(profiles.commands).then(setReports, () => setReports([]));
  };
  useEffect(load, [profiles]);
  if (reports.length === 0) return null;
  const decide = (crashId: string, action: "send" | "dismiss") => {
    setBusy(crashId);
    setFailed(false);
    decideCrashReport(profiles.commands, crashId, action).then(load, () => setFailed(true)).finally(() => setBusy(null));
  };
  return (
    <div className="flex flex-col gap-3" data-crash-review="">
      <h3 className="text-ui font-semibold text-ink">{t("crash.title")}</h3>
      <ul className="flex flex-col gap-3">
        {reports.map((report) => <CrashReportView key={report.crash_id} report={report} busy={busy === report.crash_id} onDecide={(action) => decide(report.crash_id, action)} />)}
      </ul>
      {failed && <ErrorState message={t("crash.failed")} />}
    </div>
  );
}

/** Settings → About: the brand (DSN-25), the version and the licences of everything the app ships (PLT-10). */
export function AboutView({ version, notices, assets }: { version: string | null; notices: readonly NoticeEntry[]; assets: readonly AssetEntry[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-2" data-about="">
      <div className="py-2"><BrandMark kind="lockup" height={32} /></div>
      <div className="flex flex-col divide-y divide-line">
        <Row label={t("about.version")}>{version ?? t("about.version_unknown")}</Row>
        <div className="py-3">
          <Disclosure summary={t("about.licences", { count: notices.length + assets.length })} open={open} onToggle={() => setOpen((v) => !v)}>
            {notices.length + assets.length === 0 ? <EmptyState message={t("about.licences_empty")} /> : (
              <div className="flex flex-col gap-4 pt-3">
                {assets.length > 0 && (
                  <section aria-labelledby="about-assets" className="flex flex-col gap-1">
                    <h4 id="about-assets" className="text-label text-faint">{t("about.assets")}</h4>
                    <ul className="flex flex-col">
                      {assets.map((a) => <li key={`${a.kind}:${a.name}`} className="flex flex-wrap justify-between gap-x-4 py-1 text-meta"><span className="text-ink">{a.name}</span><span className="font-mono text-muted">{a.licence}</span></li>)}
                    </ul>
                  </section>
                )}
                {notices.length > 0 && (
                  <section aria-labelledby="about-components" className="flex flex-col gap-1">
                    <h4 id="about-components" className="text-label text-faint">{t("about.components")}</h4>
                    <ul className="flex max-h-80 flex-col overflow-y-auto">
                      {notices.map((n) => <li key={`${n.ecosystem}:${n.name}:${n.version}`} className="flex flex-wrap justify-between gap-x-4 py-1 text-meta"><span className="text-ink">{n.name} <span className="text-faint">{n.version}</span></span><span className="font-mono text-muted">{n.licence}</span></li>)}
                    </ul>
                  </section>
                )}
              </div>
            )}
          </Disclosure>
        </div>
      </div>
    </div>
  );
}

export function AboutPanel({ profiles, notices = [], assets = [] }: { profiles: ProfileController; notices?: readonly NoticeEntry[]; assets?: readonly AssetEntry[] }) {
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    serverVersion(profiles.commands).then((v) => live && setVersion(v), () => undefined);
    return () => {
      live = false;
    };
  }, [profiles]);
  return <AboutView version={version} notices={notices} assets={assets} />;
}
