// Settings → Tutor models (UXS-11): the installed and available model packs (PLT-10) with their size, the class they
// suit and the "Recommended" badge with why; downloads with progress, time left, Pause and Resume (PLT-11); the file
// check before use ("Checking the file…"); and "Install from USB or folder", always offered, offline too (PLT-12).
// Below the minimum laptop the section opens with that notice and recommends nothing (PLT-29); with downloads off (the
// default) it says how to copy a model from a USB stick instead.
import { useId, useState } from "react";
import type { ModelManagerView, ModelRow } from "@jana/client-core";
import type { DownloadLimits } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Button, Disclosure, Loader, Notice } from "../primitives/index.js";
import { useShortfallText } from "../status/DegradedNotices.js";

const GB = 1024 ** 3;

export interface ModelManagerActions {
  onDownload: (id: string) => void;
  onPause: (downloadId: string) => void;
  onResume: (downloadId: string) => void;
  onDelete: (id: string) => void;
  onImport: () => void;
  onFreeSpace: () => void;
  onCancel?: (downloadId: string) => void;
  onLimits?: (downloadId: string, limits: DownloadLimits) => void;
  importDisabled?: boolean;
  onOnlineFeatures?: () => void;
}

function gb(bytes: number): string {
  return (Math.round((bytes / GB) * 10) / 10).toFixed(1);
}

function Row({ row, actions, freeBytes, downloadsOff }: { row: ModelRow; actions: ModelManagerActions; freeBytes: number | null; downloadsOff: boolean }) {
  const t = useT();
  const [optionsOpen, setOptionsOpen] = useState(false);
  const percent = row.sizeBytes > 0 ? Math.min(1, row.bytesDone / row.sizeBytes) : 0;
  const active = row.downloadId !== null && ["queued", "downloading", "paused", "error_retriable", "disk_full"].includes(row.state);
  const configure = (patch: Partial<DownloadLimits>) => {
    if (row.downloadId !== null) actions.onLimits?.(row.downloadId, {
      bandwidth_bytes_per_s: row.limits?.bandwidth_bytes_per_s ?? null,
      allow_metered: row.limits?.allow_metered ?? false, ...patch,
    });
  };
  const status = () => {
    switch (row.state) {
      case "installed":
        return <span className="text-meta text-muted">{t("models.installed")}</span>;
      case "queued":
        return <span className="text-meta text-muted">{t("models.waiting")}</span>;
      case "downloading":
      case "paused":
        return (
          <div className="flex flex-col gap-1">
            <progress className="w-full" max={1} value={percent} aria-label={t("models.progress", { name: row.id })} />
            <span className="text-meta text-muted tabular-nums">
              {row.state === "paused" ? t("models.paused") : row.etaS !== null ? t("models.time_left", { minutes: Math.max(1, Math.ceil(row.etaS / 60)) }) : t("models.downloading")}
            </span>
          </div>
        );
      case "checking":
        return <Loader label={t("models.checking")} />;
      case "error_retriable":
        return <Notice tone="warn" message={t("models.connection_lost")} />;
      case "error_final":
        return <Notice tone="warn" message={t("models.damaged")} action={{ label: t("models.delete_file"), onClick: () => actions.onDelete(row.id) }} />;
      case "disk_full":
        return (
          <Notice tone="warn" message={t("models.disk_full", { need: gb(row.sizeBytes), have: gb(freeBytes ?? 0) })}
            action={{ label: t("models.free_space"), onClick: actions.onFreeSpace }} />
        );
      default:
        return null;
    }
  };
  return (
    <li className="flex flex-col gap-2 rounded-card border border-line p-3" data-model-row={row.id} data-state={row.state}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="min-w-0 font-semibold break-all text-ink">{row.id}</span>
        <span className="text-meta text-muted tabular-nums">
          {t("models.size", { size: gb(row.sizeBytes) })}
          {row.hardwareClass !== null && ` · ${t(`models.class.${row.hardwareClass}`)}`}
        </span>
      </div>
      {row.recommended && <span className="text-meta font-medium text-muted">{t("models.recommended")}</span>}
      {status()}
      {row.meteredPaused === true && <p className="text-meta text-muted">{t("models.metered_paused")}</p>}
      {active && actions.onLimits !== undefined && <Disclosure summary={t("models.download_options")} label={t("models.download_options_for", { name: row.id })} open={optionsOpen} onToggle={() => setOptionsOpen(v => !v)}><div className="flex flex-wrap items-center gap-3 text-ui text-ink">
        <label>{t("models.speed_limit")}
          <select className="rounded-control border border-line-strong bg-canvas p-2 text-ui text-ink" value={row.limits?.bandwidth_bytes_per_s ?? ""}
            onChange={(e) => configure({ bandwidth_bytes_per_s: e.target.value === "" ? null : Number(e.target.value) })}>
            <option value="">{t("models.speed_unlimited")}</option>
            {[256 * 1024, 1024 ** 2, 4 * 1024 ** 2].map((bytes) => <option key={bytes} value={bytes}>{t("models.speed_kb", { speed: bytes / 1024 })}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={row.limits?.allow_metered === true}
          onChange={(e) => configure({ allow_metered: e.target.checked })} />{t("models.allow_metered")}</label>
      </div></Disclosure>}
      <div className="flex flex-wrap justify-end gap-2">
        {row.state === "available" && !downloadsOff && <Button onClick={() => actions.onDownload(row.id)}>{t("models.download")}</Button>}
        {row.state === "downloading" && row.downloadId !== null && <Button onClick={() => actions.onPause(row.downloadId ?? "")}>{t("models.pause")}</Button>}
        {row.state === "paused" && row.downloadId !== null && <Button onClick={() => actions.onResume(row.downloadId ?? "")}>{t("models.resume")}</Button>}
        {active && actions.onCancel !== undefined && <Button onClick={() => actions.onCancel?.(row.downloadId ?? "")}>{t("models.cancel")}</Button>}
      </div>
    </li>
  );
}

/** `titled` false: the manager sits under a heading that already names it (Settings → Tutor models), so it has none. */
export function ModelManager({ view, actions, titled = true }: { view: ModelManagerView; actions: ModelManagerActions; titled?: boolean }) {
  const t = useT();
  const id = useId();
  const shortfallText = useShortfallText();
  return (
    <section {...(titled ? { "aria-labelledby": id } : { "aria-label": t("models.title") })} className="flex flex-col gap-4" data-model-manager={view.kind}>
      {titled && <h2 id={id} className="text-heading font-semibold text-ink">{t("models.title")}</h2>}
      {view.kind === "loading" && <Loader label={t("models.loading")} />}
      {view.kind === "below_minimum" && (
        <Notice tone="warn" message={[t("models.below_minimum"), ...view.shortfalls.map(shortfallText)].join(" ")}
          {...(actions.onOnlineFeatures !== undefined ? { action: { label: t("models.online_features"), onClick: actions.onOnlineFeatures } } : {})} />
      )}
      {view.kind !== "loading" && view.downloadsOff && <p className="text-meta text-muted">{t("models.downloads_off")}</p>}
      {view.kind !== "loading" && (
        <ul className="flex flex-col gap-2">
          {view.rows.map((row) => <Row key={row.id} row={row} actions={actions} freeBytes={view.freeBytes} downloadsOff={view.downloadsOff} />)}
        </ul>
      )}
      <div className="flex justify-end">
        <Button tone="primary" disabled={actions.importDisabled === true} onClick={actions.onImport}>{t("models.import")}</Button>
      </div>
    </section>
  );
}
