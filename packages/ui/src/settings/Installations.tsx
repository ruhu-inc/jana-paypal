// Offline media and optional downloads share the same verified installation path (PLT-12). One installation controller
// serves three Settings sections of TABLE UXS-46a: Tutor models (UXS-11, the model manager), Content (packs from USB
// or a file, and the packs that failed verification, ARC-26) and About's updates (PLT-14: an update from USB or a file,
// a staged update and going back).
import { useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import {
  InstallationController, modelManagerView, type ChooseInstallSource, type InstallationView, type ProfileController,
} from "@jana/client-core";
import { Button, Disclosure, ErrorState, Notice } from "../primitives/index.js";
import { useT } from "../i18n.js";
import { DamagedPacks } from "./FamilyControls.js";
import { ModelManager } from "./ModelManager.js";

type Kind = "folder" | "pack" | "update";

export interface Installing {
  controller: InstallationController;
  view: InstallationView;
  downloadsOn: boolean;
  picking: boolean;
  pickFailed: boolean;
  /** Choose a file or folder and install it; nothing when the shell cannot choose or a choice is open. */
  select: (kind: Kind) => void;
  canPick: boolean;
}

/** The installation controller of one Settings page, watching the core's installation events while the page is open. */
export function useInstallations(profiles: ProfileController, chooseSource?: ChooseInstallSource): Installing {
  const controller = useMemo(() => new InstallationController(profiles.commands), [profiles]);
  const view = useSyncExternalStore(controller.view.subscribe, controller.view.get);
  const connectivity = useSyncExternalStore(profiles.connectivity.subscribe, profiles.connectivity.getView);
  const [picking, setPicking] = useState(false);
  const [pickFailed, setPickFailed] = useState(false);
  useEffect(() => {
    const stop = controller.watch();
    const events = profiles.onAppEnvelope(controller.receive);
    const resets = profiles.onAppReset(() => { void controller.load(true); });
    const app = profiles.watchApp();
    return () => { stop(); events(); resets(); app(); };
  }, [controller, profiles]);
  const select = (kind: Kind) => {
    if (chooseSource === undefined || picking) return;
    setPicking(true);
    setPickFailed(false);
    void (async () => {
      try {
        const path = await chooseSource(kind);
        if (path !== null) await controller.install(kind, path);
      } catch { setPickFailed(true); } finally { setPicking(false); }
    })();
  };
  const downloadsOn = connectivity.features.some((feature) => feature.feature === "downloads" && feature.enabled);
  return { controller, view, downloadsOn, picking, pickFailed, select, canPick: chooseSource !== undefined && !view.pending && !picking };
}

/** What every installation section shows below its own part: the check in progress and any failure. */
function Status({ installing }: { installing: Installing }) {
  const t = useT();
  return (
    <>
      {installing.view.pending && <Notice message={t("installations.checking")} />}
      {installing.view.error !== null && <ErrorState message={t(`error.${installing.view.error}`)} />}
      {installing.pickFailed && <ErrorState message={t("installations.pick_failed")} />}
    </>
  );
}

function Part({ installing, children }: { installing: Installing; children: ReactNode }) {
  return <div className="flex flex-col gap-6" aria-busy={installing.view.pending || installing.picking}>{children}<Status installing={installing} /></div>;
}

/** Settings → Tutor models (UXS-11): the model manager; a model folder from USB or a file. */
export function TutorModels({ installing }: { installing: Installing }) {
  const t = useT();
  const [spaceAdvice, setSpaceAdvice] = useState(false);
  const { controller } = installing;
  const models = modelManagerView(installing.view.models, { downloadsOn: installing.downloadsOn });
  return (
    <Part installing={installing}>
      <ModelManager view={models} titled={false} actions={{
        onDownload: (id) => { void controller.download(id); },
        onPause: (id) => { void controller.control(id, "pause"); },
        onResume: (id) => { void controller.control(id, "resume"); },
        onCancel: (id) => { void controller.control(id, "cancel"); },
        onLimits: (id, limits) => { void controller.configure(id, limits); },
        onDelete: (id) => { const row = models.rows.find((r) => r.id === id); if (row?.downloadId) void controller.control(row.downloadId, "cancel"); },
        onImport: () => { installing.select("folder"); }, onFreeSpace: () => { setSpaceAdvice(true); },
        importDisabled: !installing.canPick,
      }} />
      {spaceAdvice && <Notice message={t("models.space_advice")} action={{ label: t("models.check_space"),
        onClick: () => { void controller.load(true); } }} />}
      {models.rows.some((row) => row.state === "installed") && <Notice message={t("models.restart_after_install")} />}
    </Part>
  );
}

/** Settings → Content (PLT-12, PLT-13): a pack from USB or a file, and a notice for each pack that failed its check. */
export function ContentPacks({ installing, profiles }: { installing: Installing; profiles: ProfileController }) {
  const t = useT();
  return (
    <Part installing={installing}>
      <DamagedPacks family={profiles.family} />
      <div><Button disabled={!installing.canPick} onClick={() => { installing.select("pack"); }}>{t("installations.content")}</Button></div>
    </Part>
  );
}

/** About's updates (PLT-14): the installed version, a staged update, going back, and an update from USB or a file. */
export function AppUpdates({ installing }: { installing: Installing }) {
  const t = useT();
  const [changesOpen, setChangesOpen] = useState(false);
  const { controller, view } = installing;
  const updates = view.updates;
  return (
    <Part installing={installing}>
      <section aria-labelledby="app-updates" className="flex flex-col gap-3">
        <h3 id="app-updates" className="text-ui font-semibold text-ink">{t("updates.title")}</h3>
        {updates !== null && <p className="text-ui text-muted">{t("updates.installed", { version: updates.installed_version })}</p>}
        {updates?.checks !== "online" && <p className="text-meta text-muted">{t("updates.offline")}</p>}
        {updates?.staged != null && <>
          <p className="text-ui text-ink">{t("updates.staged", { version: updates.staged.version, size: Math.ceil(updates.staged.size_bytes / 1024 ** 2) })}</p>
          {updates.staged.changes.length > 0 && <Disclosure summary={t("updates.changes")} open={changesOpen} onToggle={() => setChangesOpen(v => !v)}>
            <ul className="list-disc ps-6 text-ui text-ink">{updates.staged.changes.map((change, i) => <li key={i}>{change}</li>)}</ul>
          </Disclosure>}
          {updates.apply_blocked != null && <Notice message={t(`updates.blocked.${updates.apply_blocked}`)} />}
          {updates.apply_blocked === "rollback_unavailable" && <div>
            <Button disabled={!installing.canPick} onClick={() => { installing.select("update"); }}>{t("updates.cache_current")}</Button>
          </div>}
          {updates.staged.accepted !== true ? <div>
            <Button disabled={view.pending || updates.apply_blocked != null} onClick={() => { void controller.acceptUpdate(); }}>{t("updates.at_quit")}</Button>
          </div> : <Notice message={t("updates.accepted")} />}
          <div><Button disabled={view.pending || updates.apply_blocked != null} onClick={() => { void controller.restart(); }}>{t("updates.restart")}</Button></div>
        </>}
        {updates?.rollback_offered != null && <>
          <Notice tone="warn" message={t("updates.failed", { version: updates.rollback_offered })} />
          <div><Button disabled={view.pending || updates.apply_blocked != null} onClick={() => { void controller.rollback(); }}>{t("updates.rollback")}</Button></div>
        </>}
        <div><Button disabled={!installing.canPick} onClick={() => { installing.select("update"); }}>{t("updates.import")}</Button></div>
      </section>
    </Part>
  );
}
