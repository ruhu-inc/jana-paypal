import { errors, type api_commands } from "@jana/contracts-ts/client";
import { CommandError, type CommandClient } from "./stream/commands.js";

/** A 202 reply accepts work; success is the terminal job, never acceptance alone. */
export async function waitForJob(commands: CommandClient, jobId: string, signal?: AbortSignal): Promise<api_commands.Job> {
  for (;;) {
    signal?.throwIfAborted();
    const job = await commands.get<api_commands.Job>(`/v1/jobs/${encodeURIComponent(jobId)}`);
    signal?.throwIfAborted();
    if (errors("api/commands#/$defs/Job", job).length > 0 || job.job_id !== jobId) throw new CommandError(200, "invalid_reply", false, undefined);
    if (job.state === "done") return job;
    if (job.state === "failed" || job.state === "cancelled") throw new CommandError(422, job.error?.error.code ?? "internal_error", false, undefined);
    await new Promise<void>((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); reject(signal?.reason); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", cancel); resolve(); }, 250);
      signal?.addEventListener("abort", cancel, { once: true });
    });
  }
}
