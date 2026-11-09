// Profile recovery (UXS-47, D84, D86): "Let's get your profiles back", reached from the picker when the profile store
// cannot be read ("Show recovery options"). In order: "Try again" (the core checks and repairs the store, DAT-13) and
// "Restore from a profile archive" (`POST /v1/profiles/import`: an archive from a file or USB stick becomes a new
// profile, never touching another). It works offline, never deletes anything and has no dead end: every state offers
// a way on. States: the flow's canonical step, restoring, restore failed ("Choose another file"), done.
import { useEffect, useId, useRef, useState } from "react";
import { importProfile, waitForJob, type ProfileController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, ErrorState, FIELD_LOOK, Loader, Notice } from "../primitives/index.js";
import { WelcomeCard } from "../setup/Welcome.js";

export type RecoveryStep = "choose" | "passphrase" | "restoring" | "failed" | "done";

/** The flow from its step: the gallery and the tests render this directly. */
export function ProfileRecoveryView({ step, file, onRetry, onRestore, onChoose, onPassphrase, onBack, busy = false, mode = "recover" }: {
  step: RecoveryStep;
  /** `restore`: reached by choice from a working picker or first run, not because the store failed: no "Try again",
   * and its first step is choosing the archive, with the way back to the profiles beside it. */
  mode?: "recover" | "restore";
  file: string | null;
  onRetry: () => void;
  /** "Restore from a profile archive": absent where this computer has no way to choose a file. */
  onRestore?: (() => void) | undefined;
  onChoose?: (() => void) | undefined;
  onPassphrase: (passphrase: string) => void;
  onBack: () => void;
  busy?: boolean;
}) {
  const t = useT();
  const id = useId();
  const [passphrase, setPassphrase] = useState("");
  return (
    <WelcomeCard labelledBy={id} data-recovery-step={step}>
      <div className="flex flex-col gap-2">
        <h1 id={id} className="text-display">{t(mode === "restore" ? "restore.title" : "recovery.title")}</h1>
        <p className="text-body text-muted">{t(mode === "restore" ? "restore.intro" : "recovery.intro")}</p>
      </div>
      <div className="flex w-full flex-col gap-3 text-start">
        {step === "choose" && mode === "restore" && (
          <>
            {onRestore !== undefined && <Button tone="primary" disabled={busy} onClick={onRestore}>{t("restore.choose")}</Button>}
            <Button disabled={busy} onClick={onRetry}>{t("restore.back")}</Button>
            <p className="text-meta text-muted">{t("recovery.nothing_deleted")}</p>
          </>
        )}
        {step === "choose" && mode === "recover" && (
          <>
            <Button tone="primary" disabled={busy} onClick={onRetry}><Icon name="retry" size={16} />{t("action.try_again")}</Button>
            {onRestore !== undefined && <Button disabled={busy} onClick={onRestore}>{t("recovery.restore")}</Button>}
            <p className="text-meta text-muted">{t("recovery.nothing_deleted")}</p>
          </>
        )}
        {step === "passphrase" && (
          <form className="flex flex-col gap-3" onSubmit={(event) => {
            event.preventDefault();
            if (passphrase !== "") { onPassphrase(passphrase); setPassphrase(""); }
          }}>
            <p className="text-ui text-ink">{t("recovery.file", { file: file ?? "" })}</p>
            <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
              {t("recovery.passphrase")}
              <input type="password" className={FIELD_LOOK} autoComplete="off" value={passphrase} onChange={(event) => setPassphrase(event.target.value)} />
            </label>
            <div className="flex flex-wrap gap-2">
              <Button type="submit" tone="primary" disabled={passphrase === ""}>{t("recovery.restore")}</Button>
              <Button onClick={onBack}>{t("action.back")}</Button>
            </div>
          </form>
        )}
        {step === "restoring" && <Loader label={t("recovery.restoring")} />}
        {step === "failed" && (
          <>
            <ErrorState message={t("recovery.failed")} />
            <div className="flex flex-wrap gap-2">
              {onChoose !== undefined && <Button tone="primary" onClick={onChoose}>{t("recovery.choose_another")}</Button>}
              <Button onClick={onBack}>{t("action.back")}</Button>
            </div>
          </>
        )}
        {step === "done" && (
          <>
            <Notice message={t("recovery.done")} />
            <Button tone="primary" onClick={onRetry}>{t("recovery.to_profiles")}</Button>
          </>
        )}
      </div>
    </WelcomeCard>
  );
}

export function ProfileRecovery({ profiles, chooseArchive, onDone, mode = "recover" }: {
  profiles: ProfileController;
  mode?: "recover" | "restore";
  /** A file dialog for a profile archive; absent where the shell offers none. */
  chooseArchive?: (() => Promise<string | null>) | undefined;
  onDone: () => void;
}) {
  const [step, setStep] = useState<RecoveryStep>("choose");
  const [file, setFile] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false), lifetime = useRef<AbortController | null>(null);
  useEffect(() => { const controller = new AbortController(); lifetime.current = controller; return () => controller.abort(); }, []);
  const choose = chooseArchive === undefined ? undefined : async () => {
    if (pending.current) return;
    pending.current = true; setBusy(true);
    const path = await chooseArchive().catch(() => null);
    pending.current = false;
    if (lifetime.current?.signal.aborted) return;
    setBusy(false);
    if (path !== null) { setFile(path); setStep("passphrase"); }
  };
  const restore = async (passphrase: string) => {
    if (file === null || pending.current) return;
    pending.current = true;
    setStep("restoring");
    try {
      const accepted = await importProfile(profiles.commands, file, passphrase);
      const result = await waitForJob(profiles.commands, accepted.job_id, lifetime.current?.signal);
      if (result.kind !== "import" || result.result_ref === null) throw new Error("invalid_import_result");
      if (!lifetime.current?.signal.aborted) setStep("done");
    } catch {
      if (!lifetime.current?.signal.aborted) setStep("failed");
    } finally { pending.current = false; }
  };
  return (
    <ProfileRecoveryView mode={mode} step={step} file={file} busy={busy} onRetry={onDone} onRestore={choose} onChoose={choose}
      onPassphrase={(p) => void restore(p)} onBack={() => setStep("choose")} />
  );
}
