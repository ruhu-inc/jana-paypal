// Settings → Data export (TABLE UXS-46a, DAT-10): for the open profile, a profile archive sealed with a passphrase the
// student chooses now, written to a folder they pick (`POST /v1/exports`, `profile_archive`), and the report for support
// (OBS-18). Conversations and photos stay out of the archive: they need the student's own archive authorization
// (API-30). The passphrase goes to the core only and is cleared from the field once the export is accepted (SEC-17).
import { useId, useState } from "react";
import { ProblemReports, type ChooseInstallSource, type ProfileController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, ErrorState, Notice } from "../primitives/index.js";
import { SupportReport } from "./Help.js";

/** The archive passphrase's shortest length (`CreateExport.passphrase`, TABLE DAT-10a). */
export const ARCHIVE_PASSPHRASE_MIN = 12;

function ProfileArchive({ profiles, chooseSource }: { profiles: ProfileController; chooseSource?: ChooseInstallSource | undefined }) {
  const t = useT();
  const id = useId();
  const [includeStudy, setIncludeStudy] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const ready = chooseSource !== undefined && passphrase.length >= ARCHIVE_PASSPHRASE_MIN && state !== "saving";
  const save = async () => {
    if (chooseSource === undefined) return;
    const folder = await chooseSource("folder").catch(() => null);
    if (folder === null) return;
    setState("saving");
    try {
      await new ProblemReports(profiles.commands).profileArchive(profiles.scope().profileId, folder, passphrase, includeStudy);
      setPassphrase("");
      setState("saved");
    } catch {
      setState("failed");
    }
  };
  return (
    <form data-profile-archive="" className="flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); if (ready) void save(); }}>
      <h3 className="text-ui font-semibold text-ink">{t("settings.export.archive_title")}</h3>
      <p className="text-ui text-muted">{t("settings.export.archive_help")}</p>
      {chooseSource === undefined && <Notice message={t("settings.export.desktop_required")} />}
      <div className="flex flex-col gap-1">
        <label htmlFor={id} className="text-ui text-ink">{t("settings.export.passphrase")}</label>
        <input id={id} type="password" autoComplete="new-password" minLength={ARCHIVE_PASSPHRASE_MIN} maxLength={256} value={passphrase}
          aria-describedby={`${id}-help`} onChange={(event) => setPassphrase(event.target.value)}
          className="min-h-8 rounded-control border border-line-strong bg-canvas px-3 text-body text-ink" />
        <span id={`${id}-help`} className="text-meta text-muted">{t("settings.export.passphrase_help")}</span>
      </div>
      <label className="flex min-h-11 items-center gap-2 text-ui text-ink"><input type="checkbox" checked={includeStudy} onChange={(event) => setIncludeStudy(event.target.checked)} />{t("settings.export.include_study")}</label>
      <div><Button type="submit" disabled={!ready}>{t("settings.export.save_archive")}</Button></div>
      {state === "saved" && <Notice message={t("settings.export.archive_saved")} />}
      {state === "failed" && <ErrorState message={t("settings.export.archive_failed")} />}
    </form>
  );
}

export function DataExport({ profiles, chooseSource }: { profiles: ProfileController; chooseSource?: ChooseInstallSource | undefined }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-6 py-4" data-data-export="">
      <ProfileArchive profiles={profiles} chooseSource={chooseSource} />
      <section className="flex flex-col gap-3 border-t border-line pt-4">
        <h3 className="text-ui font-semibold text-ink">{t("settings.export.support_title")}</h3>
        {chooseSource !== undefined && <SupportReport profiles={profiles} chooseSource={chooseSource} />}
      </section>
    </div>
  );
}
