// Where the core is and the per-launch token (API-02). The webview gets both from the shell through a Tauri command
// and keeps the token in memory only; in development the Vite dev server provides them.
export interface LaunchInfo {
  base_url: string;
  sponsored_study?: boolean;
  token: string;
}

/** Only the opt-in synthetic browser preview exposes this guarded core route. No provider address or key enters the UI. */
export async function developmentModelRequest(model?: string): Promise<unknown> {
  const info = await launchInfo();
  const reply = await fetch(info.base_url + "/__jana_dev/models", {
    method: model === undefined ? "GET" : "POST",
    headers: { Authorization: "Bearer " + info.token, ...(model === undefined ? {} : { "Content-Type": "application/json" }) },
    ...(model === undefined ? {} : { body: JSON.stringify({ model }) }),
    cache: "no-store",
  });
  const data = await reply.json();
  if (!reply.ok) throw new Error(data.error ?? "Model preview unavailable.");
  return data;
}

interface TauriInternals {
  invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown>;
}

export function hasNativeFilePickers(): boolean {
  return (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__ !== undefined;
}

export async function pickInstallSource(kind: "folder" | "pack" | "update"): Promise<string | null> {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  if (tauri === undefined) return null;
  return (await tauri.invoke("pick_install_source", { kind })) as string | null;
}

/** Profile archives use a native file picker with their own fixed filter and command capability. */
export async function pickProfileArchive(): Promise<string | null> {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  return tauri === undefined ? null : await tauri.invoke("pick_profile_archive") as string | null;
}

export async function updateRecovery(): Promise<string | null> {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  return tauri === undefined ? null : (await tauri.invoke("update_recovery")) as string | null;
}

export async function rollbackUpdate(): Promise<void> {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  if (tauri !== undefined) await tauri.invoke("rollback_update");
}

/** Check-in notifications (SPEC §19.5.5, JM-D24), shown by the shell: only a check-in's id and kind cross, and the
 * shell words the notification from the kind's fixed template. Null outside the desktop shell (a browser build). */
export interface DesktopCheckinNotifier {
  permission(): Promise<"granted" | "denied">;
  notify(checkin: { commitment_id: string; kind: string }): Promise<void>;
  cancel(): Promise<void>;
  /** The check-in whose notification was clicked since the last call, once: the app then opens Home. */
  takeClick(): Promise<string | null>;
}

export function desktopCheckinNotifier(): DesktopCheckinNotifier | null {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  if (tauri === undefined) return null;
  return {
    permission: async () => ((await tauri.invoke("checkin_permission")) === "granted" ? "granted" : "denied"),
    notify: async ({ commitment_id, kind }) => { await tauri.invoke("notify_checkin", { commitmentId: commitment_id, kind }); },
    cancel: async () => { await tauri.invoke("cancel_checkins"); },
    takeClick: async () => ((await tauri.invoke("take_checkin_click")) as string | null) ?? null,
  };
}

export async function launchInfo(refresh = false): Promise<LaunchInfo> {
  const tauri = (globalThis as { __TAURI_INTERNALS__?: TauriInternals }).__TAURI_INTERNALS__;
  if (tauri !== undefined) return (await tauri.invoke("launch_info", { refresh })) as LaunchInfo;
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch("/__jana_dev/launch", { cache: "no-store" });
    if (response.ok) return (await response.json()) as LaunchInfo;
    await new Promise((r) => setTimeout(r, Math.min(2000, 200 * (attempt + 1))));
  }
}
