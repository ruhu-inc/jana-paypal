// Compiled only into the explicitly requested synthetic local model preview.
import { useEffect, useState } from "react";
import { developmentModelRequest } from "./launch.js";

interface ModelStatus {
  selected: string;
  mode?: "offline";
  choices?: { value: string; label: string }[];
  cloud_ready: boolean;
  cloud_budget_limited?: boolean;
  budget: { spent_usd?: number; held_usd?: number };
  cumulative_cap_usd: number;
}

export function DevelopmentModels() {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function request(model?: string): Promise<ModelStatus> {
    return await developmentModelRequest(model) as ModelStatus;
  }
  useEffect(() => {
    let live = true;
    const read = () => void request().then((data) => { if (live) setStatus(data); }).catch(() => { if (live) setError("Model preview unavailable. Reload to reconnect."); });
    read();
    const timer = setInterval(read, 5000);
    return () => { live = false; clearInterval(timer); };
  }, []);
  async function select(model: string) {
    setBusy(true); setError("");
    try { setStatus(await request(model)); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not switch models."); }
    finally { setBusy(false); }
  }
  const budget = status?.budget;
  const used = typeof budget?.spent_usd === "number" && Number.isFinite(budget.spent_usd)
    && typeof budget.held_usd === "number" && Number.isFinite(budget.held_usd)
    ? budget.spent_usd + budget.held_usd : null;
  return <aside aria-label="Development model preview" className="flex flex-wrap items-center gap-3 border-b bg-panel px-4 py-2 text-sm">
    <strong>Synthetic test profiles only</strong>
    <label htmlFor="development-tutor-model">Tutor model</label>
    <select id="development-tutor-model" value={status?.selected ?? "gemma"} disabled={!status || busy}
      onChange={(event) => void select(event.target.value)} className="rounded border bg-panel px-2 py-1">
      {status?.mode === "offline" ? status.choices?.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>) : <>
        <option value="gemini" disabled={!status?.cloud_ready || status.cloud_budget_limited}>Gemini 3.8 Flash · online{status?.cloud_budget_limited ? " (test budget limit)" : status && !status.cloud_ready ? " (connecting or unavailable)" : ""}</option>
        <option value="gemma">Gemma 12B · on this computer</option>
      </>}
    </select>
    <span>{status?.mode === "offline" ? "Local models only. No personal data. Deployment on hold." : "No personal data. Deployment on hold. Gemini may fall back to Gemma when unavailable."}</span>
    {status && status.mode !== "offline" && <span>{used === null
      ? `Gemini test balance unavailable. The approved $${status.cumulative_cap_usd.toFixed(2)} cap is unchanged.`
      : `Inference: $${used.toFixed(3)} / $${status.cumulative_cap_usd.toFixed(2)} including reservations`}</span>}
    {status?.cloud_budget_limited && <span role="status">The approved Gemini test budget is nearly used up. Continue with Gemma.</span>}
    {error && <span role="alert">{error}</span>}
  </aside>;
}
