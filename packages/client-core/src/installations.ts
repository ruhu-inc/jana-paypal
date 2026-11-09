// Device-wide installation commands (PLT-11 to PLT-15). Files are selected by the desktop bridge; no path is typed.
import type { DownloadLimits, JobAccepted, ModelCatalogue, UpdateStatus } from "@jana/contracts-ts";
import { Observable } from "./observable.js";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";
import type { Envelope } from "./queue.js";

export type InstallSourceKind = "folder" | "pack" | "update";
export type ChooseInstallSource = (kind: InstallSourceKind) => Promise<string | null>;
export interface InstallationView {
  models: ModelCatalogue | null;
  updates: UpdateStatus | null;
  pending: boolean;
  error: string | null;
  jobId: string | null;
}

export class InstallationController {
  readonly view = new Observable<InstallationView>({ models: null, updates: null, pending: false, error: null, jobId: null });
  private generation = 0;
  private reading: Promise<void> | null = null;
  private refreshQueued = false;

  constructor(private readonly commands: CommandClient) {}

  private set(patch: Partial<InstallationView>): void { this.view.set({ ...this.view.get(), ...patch }); }

  load(refreshModels = false): Promise<void> {
    if (this.reading !== null) {
      this.refreshQueued ||= refreshModels;
      return this.reading;
    }
    const generation = this.generation;
    this.reading = (async () => {
      const [models, updates] = await Promise.allSettled([
        refreshModels || this.view.get().models === null ? this.commands.get<ModelCatalogue>("/v1/models")
          : Promise.resolve(this.view.get().models), this.commands.get<UpdateStatus>("/v1/updates"),
      ]);
      if (generation !== this.generation) return;
      this.set({ ...(models.status === "fulfilled" ? { models: models.value } : {}),
        ...(updates.status === "fulfilled" ? { updates: updates.value } : {}) });
      const jobId = this.view.get().jobId;
      if (jobId !== null) {
        const job = await this.commands.get<{ state: string; error?: { code: string } | null }>(`/v1/jobs/${encodeURIComponent(jobId)}`);
        if (generation !== this.generation) return;
        if (["done", "failed", "cancelled"].includes(job.state)) {
          this.set({ pending: false, jobId: null, error: job.state === "failed" ? job.error?.code ?? "validation_failed" : null });
          // The status read may have preceded the job commit; reread after observing completion.
          const status = await this.commands.get<UpdateStatus>("/v1/updates");
          if (generation === this.generation) this.set({ updates: status });
        }
      }
    })().catch((error: unknown) => {
      if (generation !== this.generation) return;
      if (error instanceof CommandError && error.code === "not_found" && this.view.get().jobId !== null) {
        this.set({ pending: false, jobId: null, error: "installation_interrupted" });
      } else this.set({ error: "core_starting" });
    })
      .finally(async () => {
        this.reading = null;
        if (this.refreshQueued) {
          this.refreshQueued = false;
          if (generation === this.generation) await this.load(true);
        }
      });
    return this.reading;
  }

  watch(): () => void {
    void this.load();
    const timer = setInterval(() => { void this.load(); }, 2000);
    return () => { clearInterval(timer); this.generation += 1; };
  }

  receive = (envelope: Envelope): void => {
    const event = envelope.event;
    if (event.type === "model.download" && typeof event.model_id === "string") {
      const models = this.view.get().models;
      if (models === null) return;
      const available = models.available.map((row) => {
        const record = row as Record<string, unknown>;
        if (record.model_pack_id !== event.model_id) return row;
        const limits = (record.download as { limits?: DownloadLimits } | undefined)?.limits;
        return { ...row, download: { ...event, ...(limits === undefined ? {} : { limits }) } };
      });
      this.set({ models: { ...models, available } });
      if (event.state === "done") void this.load(true);
    } else if (event.type === "job.update" && event.job_id === this.view.get().jobId) {
      void this.load(true);
    }
  };

  private async mutate(path: string, body: unknown, method: "POST" | "PUT" = "POST", job = false): Promise<void> {
    if (this.view.get().pending) return;
    this.set({ pending: true, error: null });
    try {
      const result = await this.commands.request<JobAccepted>(method, path, body, clientId());
      this.set({ pending: job, jobId: job ? result.job_id : null });
      await this.load(true);
    } catch (error) {
      this.set({ pending: false, jobId: null, error: (error as { code?: string }).code ?? "internal_error" });
    }
  }

  install(kind: InstallSourceKind, path: string): Promise<void> {
    return kind === "update" ? this.mutate("/v1/updates/install", { source: { kind: "file", path } }, "POST", true)
      : this.mutate("/v1/packs/install", { source: { kind: kind === "folder" ? "folder" : "file", path } }, "POST", true);
  }

  download(id: string): Promise<void> {
    return this.mutate(`/v1/models/${encodeURIComponent(id)}/download`, undefined);
  }
  control(downloadId: string, action: "pause" | "resume" | "cancel"): Promise<void> {
    return this.mutate(`/v1/models/downloads/${encodeURIComponent(downloadId)}/${action}`, undefined);
  }
  configure(downloadId: string, limits: DownloadLimits): Promise<void> {
    return this.mutate(`/v1/models/downloads/${encodeURIComponent(downloadId)}/limits`, limits, "PUT");
  }
  restart(): Promise<void> { return this.mutate("/v1/updates/restart-now", undefined); }
  acceptUpdate(): Promise<void> { return this.mutate("/v1/updates/install-at-quit", undefined); }
  rollback(): Promise<void> { return this.mutate("/v1/updates/rollback", undefined); }
}
