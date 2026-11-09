// The model manager's view (UXS-11): Settings → Tutor models from `GET /v1/models` (installed and available model
// packs, the recommendation and the hardware report) and the app channel's `model.download` events (PLT-11). Every
// pack is 7B or larger (D58); below the minimum laptop no pack is recommended and the section says so first (PLT-29).
// Downloads are off by default: then the section says how to copy a model from a USB stick, and "Install from USB or
// folder" is always offered, offline too.
import type { DownloadLimits, ModelCatalogue } from "@jana/contracts-ts";
import { hardwareReport, type Shortfall } from "./health.js";

export type ModelRowState =
  | "installed" // verified and usable
  | "available" // can be downloaded
  | "queued" // waiting for internet
  | "downloading"
  | "paused"
  | "checking" // "Checking the file…": size and hash verified before use
  | "error_retriable" // the connection was lost: it resumes when back online
  | "error_final" // damaged or not from us: delete it and copy it again
  | "disk_full";

export interface ModelRow {
  id: string;
  sizeBytes: number;
  hardwareClass: string | null;
  recommended: boolean;
  state: ModelRowState;
  bytesDone: number;
  etaS: number | null;
  licence: string | null;
  downloadId: string | null;
  limits?: DownloadLimits;
  meteredPaused?: boolean;
}

export interface ModelManagerView {
  kind: "loading" | "below_minimum" | "ready";
  shortfalls: Shortfall[];
  downloadsOff: boolean;
  rows: ModelRow[];
  reasonKey: string | null;
  freeBytes: number | null;
}

/** A `model.download` app event, as the app channel delivers it. */
export interface ModelDownloadEvent {
  type: "model.download";
  download_id: string;
  model_id: string;
  state: "queued" | "downloading" | "paused" | "verifying" | "done" | "failed";
  bytes_done: number;
  bytes_total: number;
  eta_s: number | null;
  error_code: string | null;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** The row state a download event means. The shell's `waiting_for_network` arrives as `queued` with that code: before
 * any byte it is "Waiting for internet"; part-way it is the connection lost, resuming when back online. */
export function downloadState(event: Pick<ModelDownloadEvent, "state" | "error_code" | "bytes_done">): ModelRowState {
  switch (event.state) {
    case "queued":
      return event.error_code === "waiting_for_network" && event.bytes_done > 0 ? "error_retriable" : "queued";
    case "downloading":
      return "downloading";
    case "paused":
      return event.error_code === "storage_low" ? "disk_full" : "paused";
    case "verifying":
      return "checking";
    case "done":
      return "installed";
    case "failed":
      if (event.error_code === "storage_full") return "disk_full";
      return event.error_code === "cancelled" ? "available" : "error_final";
  }
}

export function modelManagerView(models: ModelCatalogue | null, options: { downloadsOn: boolean; freeBytes?: number | null;
  downloads?: Record<string, ModelDownloadEvent> }): ModelManagerView {
  if (models === null) return { kind: "loading", shortfalls: [], downloadsOff: !options.downloadsOn, rows: [], reasonKey: null, freeBytes: null };
  const report = hardwareReport(models);
  const shortfalls = (report?.shortfalls ?? []).filter((s) => s.check !== "speed");
  const belowMinimum = report?.hardwareClass === "content_only" && shortfalls.length > 0;
  const recommended = new Set([models.recommended.fast, models.recommended.strong].filter((m): m is string => m !== null));
  const rows: ModelRow[] = [];
  let freeBytes = options.freeBytes ?? null;
  const row = (entry: Record<string, unknown>, state: ModelRowState): ModelRow | null => {
    const id = str(entry.model_pack_id) ?? str(entry.model_id);
    if (id === null) return null;
    return { id, sizeBytes: num(entry.size_bytes), hardwareClass: str(entry.hardware_class), recommended: !belowMinimum && recommended.has(id),
      state, bytesDone: 0, etaS: null, licence: str(entry.licence), downloadId: null };
  };
  for (const entry of models.installed as unknown[]) {
    if (!isRecord(entry)) continue;
    const made = row(entry, entry.state === "error" ? "error_final" : "installed");
    if (made !== null && !rows.some((r) => r.id === made.id)) rows.push(made);
  }
  for (const entry of models.available as unknown[]) {
    if (!isRecord(entry)) continue;
    const made = row(entry, "available");
    if (made === null) continue;
    const pending = isRecord(entry.download) ? entry.download : null;
    if (pending !== null) {
      made.downloadId = str(pending.download_id);
      made.bytesDone = num(pending.bytes_done);
      made.etaS = typeof pending.eta_s === "number" ? pending.eta_s : null;
      made.meteredPaused = pending.error_code === "metered";
      if (isRecord(pending.limits)) {
        made.limits = { bandwidth_bytes_per_s: typeof pending.limits.bandwidth_bytes_per_s === "number" ? pending.limits.bandwidth_bytes_per_s : null,
          allow_metered: pending.limits.allow_metered === true };
      }
      const known = ["queued", "downloading", "paused", "verifying", "done", "failed"] as const;
      const state = known.find((s) => s === pending.state) ?? "queued";
      made.state = downloadState({ state, error_code: str(pending.error_code), bytes_done: made.bytesDone });
      if (typeof pending.free_bytes === "number") freeBytes = pending.free_bytes; // a storage_full refusal says what is free
    }
    rows.push(made);
  }
  for (const event of Object.values(options.downloads ?? {})) {
    const target = rows.find((r) => r.id === event.model_id);
    if (target === undefined) continue;
    target.state = downloadState(event);
    target.bytesDone = event.bytes_done;
    target.etaS = event.eta_s;
    target.downloadId = event.download_id;
  }
  return { kind: belowMinimum ? "below_minimum" : "ready", shortfalls, downloadsOff: !options.downloadsOn, rows, reasonKey: models.recommended.reason_key,
    freeBytes };
}
