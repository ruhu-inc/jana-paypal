// The pre-send notice (PRV-28, PRD-21, UIA-23): before and while data leaves the device it names, in plain words, where
// it goes and what goes. A turn shows it while its cloud request is in flight, and "Answered by the online tutor" once
// the cloud answered it (MDL-24). The first use of a feature on a profile is the core's `confirm` card with purpose
// `egress` (ARC-10), rendered by the card renderer with "OK" and "Keep it on this computer".
import { ONLINE_FEATURES, cloudAnswered } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Notice } from "../primitives/index.js";
import { useConnectivitySelect } from "./context.js";

/** Every DAT-18 egress path that ships at S2, file export, and the LAN compute server (S4): each has a notice. */
export const PRE_SEND_PATHS = ["cloud_tier", "downloads", "update_checks", "telemetry", "crash_reports", "file_export", "lan_compute"] as const;

const FEATURES = new Set<string>(ONLINE_FEATURES);

/** The catalogue key naming what leaves for a path: the TABLE UXS-07a line of a feature, or the path's own. */
export function leavesKey(path: string): string | null {
  if (FEATURES.has(path)) return `settings.online.leaves.${path}`;
  if (path === "file_export") return "egress.leaves.file_export";
  if (path === "lan_compute") return "egress.leaves.lan_compute";
  return null;
}

export function EgressNoticeView({ path, destination }: { path: string; destination: string }) {
  const t = useT();
  const key = leavesKey(path);
  return <Notice message={t("egress.notice", { destination, what: key === null ? "" : t(key) })} />;
}

/** A turn's egress: where its data is going while it is sent, then whether the online tutor answered. */
export function TurnEgress({ turnId, live }: { turnId: string; live: boolean }) {
  const t = useT();
  const rec = useConnectivitySelect((slice) => slice.turns[turnId]);
  if (rec === null || rec === undefined) return null;
  if (rec.state === "started") {
    return (
      <div data-turn-egress={rec.feature} className="py-1">
        <EgressNoticeView path={rec.feature} destination={rec.destination_label} />
      </div>
    );
  }
  if (!live && cloudAnswered(rec)) return <p data-cloud-answered="true" className="py-1 text-meta text-muted">{t("turn.cloud_answered")}</p>;
  return null;
}
