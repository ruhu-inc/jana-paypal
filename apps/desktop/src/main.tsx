// The desktop client entry: the shell gives the core's address and launch token (API-02); the app starts at the
// profile picker (UXS-09). Screens and navigation beyond the tutor and Settings arrive in M18.
import { StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Launch } from "@jana/client-core";
import "./app.css";
import { BASE } from "@jana/i18n";
import { HOSTED_COPY } from "./hosted-copy.js";
import { LaunchScreen } from "@jana/ui/launch";
import { desktopCheckinNotifier, hasNativeFilePickers, launchInfo, pickInstallSource, pickProfileArchive, rollbackUpdate, updateRecovery } from "./launch.js";

// The hosted demo (`scripts/hosted`) keeps data on a server, so a build for it must not say it stays on this computer.
if (import.meta.env.VITE_JANA_HOSTED === "1") Object.assign(BASE, HOSTED_COPY);

let info: ReturnType<typeof launchInfo> | null = null;
const launch: Launch = {
  endpoint: async (refresh) => {
    if (refresh || info === null) info = launchInfo(refresh);
    const current = await info;
    return { baseUrl: current.base_url, token: current.token, sponsoredStudy: current.sponsored_study === true };
  },
};
const container = document.getElementById("root") as (HTMLElement & { __jana_root?: Root }) | null;
if (container === null) throw new Error("index.html has no #root");
// One root per page: a development module reload re-renders into it rather than mounting a second one.
container.__jana_root ??= createRoot(container);
const root = container.__jana_root;
// The launch screen fills the window while the app loads, as it does once the app is up (DSN-25).
const launching = (failed: boolean) => <div className="flex h-screen"><LaunchScreen failed={failed} onRetry={() => window.location.reload()} /></div>;
root.render(launching(false));
void import("./App.js").then(({ App, createRuntime }) => {
  // Check-in notifications go through the shell (SPEC §19.5.5); a browser build has none.
  const runtime = createRuntime(launch, undefined, desktopCheckinNotifier() ?? undefined);
  if (hasNativeFilePickers()) {
    runtime.chooseSource = pickInstallSource;
    runtime.chooseArchive = pickProfileArchive;
  }
  runtime.updateRecovery = updateRecovery;
  runtime.rollbackUpdate = rollbackUpdate;
  if (import.meta.env.VITE_JANA_MODEL_PREVIEW === "1" && import.meta.env.VITE_JANA_HOSTED !== "1" && !hasNativeFilePickers()) {
    void import("./DevelopmentModels.js").then(({ DevelopmentModels }) => {
      root.render(<StrictMode><div className="flex h-screen flex-col"><DevelopmentModels /><div className="min-h-0 flex-1 overflow-hidden"><App runtime={runtime} fillParent /></div></div></StrictMode>);
    }).catch(() => root.render(launching(true)));
  } else {
    root.render(<StrictMode><App runtime={runtime} /></StrictMode>);
  }
}).catch(() => root.render(launching(true)));
