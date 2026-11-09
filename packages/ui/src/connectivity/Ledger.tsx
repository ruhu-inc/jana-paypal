// Settings → "What leaves this device" (ARC-10, PRV-28): the online features that are on and what each last sent, then
// the egress ledger for its retention window, one row per request: time, feature, host, size and payload class. The
// ledger never carries content, so none can be shown. Reachable from Settings and from the indicator's popover.
import { useEffect, useState, useSyncExternalStore } from "react";
import { ONLINE_FEATURES, type ConnectivityController, type EgressLedger, type FeatureStatus } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, EmptyState, ErrorState, Skeleton } from "../primitives/index.js";

const DATA_CLASSES = new Set(["question_text", "board", "image", "learner_summary", "progress_report", "diagnostics", "audio", "transcript", "document", "tutor_notes"]);
const FEATURES = new Set<string>(ONLINE_FEATURES);

export type LedgerState = { status: "loading" } | { status: "failed" } | { status: "ready"; ledger: EgressLedger };

export function LedgerContent({ state, features, onRetry }: { state: LedgerState; features: FeatureStatus[]; onRetry: () => void }) {
  const t = useT();
  const on = features.filter((f) => f.enabled);
  const featureName = (feature: string) => (FEATURES.has(feature) ? t(`settings.online.feature.${feature}`) : feature === "lan_compute" ? t("ledger.feature.lan_compute") : feature);
  const kind = (payload: string) => (DATA_CLASSES.has(payload) ? t(`egress.class.${payload}`) : payload);
  return (
    <div className="flex flex-col gap-3" data-ledger="true">
      {on.length === 0 ? (
        <p className="text-ui text-ink">{t("ledger.enabled_none")}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {on.map((f) => (
            <li key={f.feature} className="text-ui text-ink">
              {f.last_sent_at === null ? t("ledger.never_sent", { feature: featureName(f.feature) }) : t("ledger.last_sent", { feature: featureName(f.feature), at: new Date(f.last_sent_at) })}
            </li>
          ))}
        </ul>
      )}
      {state.status === "loading" && <Skeleton lines={3} />}
      {state.status === "failed" && <ErrorState message={t("ledger.failed")} action={{ label: t("action.try_again"), onClick: onRetry }} />}
      {state.status === "ready" && (
        <>
          <p className="text-meta text-muted">{t("ledger.intro", { days: state.ledger.retention_days })}</p>
          {state.ledger.entries.length === 0 ? (
            <EmptyState message={t("ledger.empty")} />
          ) : (
            <table className="w-full text-start text-meta text-ink">
              <thead>
                <tr className="border-b border-line text-muted">
                  <th scope="col" className="py-1 pe-2 text-start font-medium">{t("ledger.col.at")}</th>
                  <th scope="col" className="py-1 pe-2 text-start font-medium">{t("ledger.col.feature")}</th>
                  <th scope="col" className="py-1 pe-2 text-start font-medium">{t("ledger.col.destination")}</th>
                  <th scope="col" className="py-1 pe-2 text-start font-medium">{t("ledger.col.bytes")}</th>
                  <th scope="col" className="py-1 text-start font-medium">{t("ledger.col.payload_class")}</th>
                </tr>
              </thead>
              <tbody>
                {state.ledger.entries.map((entry, i) => (
                  <tr key={`${entry.at}-${i}`} className="border-b border-line">
                    <td className="py-1 pe-2">{t("ledger.at", { at: new Date(entry.at) })}</td>
                    <td className="py-1 pe-2">{featureName(entry.feature)}</td>
                    <td className="py-1 pe-2 font-mono">{entry.destination}</td>
                    <td className="py-1 pe-2">{t("ledger.bytes", { bytes: entry.bytes })}</td>
                    <td className="py-1">{kind(entry.payload_class)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}

/** The ledger, read when shown (with the profile token: the device's rows and this profile's). */
export function Ledger({ connectivity }: { connectivity: ConnectivityController }) {
  const slice = useSyncExternalStore(connectivity.subscribe, connectivity.getView, connectivity.getView);
  const [state, setState] = useState<LedgerState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let current = true;
    void connectivity.load();
    connectivity.ledger().then(
      (ledger) => current && setState({ status: "ready", ledger }),
      () => current && setState({ status: "failed" }),
    );
    return () => {
      current = false;
    };
  }, [connectivity, attempt]);
  return <LedgerContent state={state} features={slice.features} onRetry={() => setAttempt((n) => n + 1)} />;
}

/** The ledger as a page over the current screen, from the indicator's popover: what is under it stays as it was. */
export function LedgerPage({ connectivity, onClose }: { connectivity: ConnectivityController; onClose: () => void }) {
  const t = useT();
  return (
    <div className="absolute inset-0 z-dialog flex flex-col gap-4 overflow-auto bg-paper p-6 text-ink">
      <div className="flex items-center justify-between">
        <h1 className="text-heading font-semibold text-ink">{t("ledger.title")}</h1>
        <Button onClick={onClose}>{t("action.close")}</Button>
      </div>
      <Ledger connectivity={connectivity} />
    </div>
  );
}
