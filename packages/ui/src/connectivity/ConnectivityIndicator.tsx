// The connectivity indicator (UXS-06, ARC-10, PRD-21): a quiet item at the bottom of the left rail on every screen,
// always shown, with the state, icon and words of TABLE UXS-06a and the colour alias of TABLE DSN-19a. Its only motion
// is the label morph; it never blocks. Selecting it opens a popover that lists each online feature the core lists,
// whether it is on, what waits to be sent and what it last sent, "Send now" when online with something queued
// (UXS-08), and the way to "What leaves this device". It switches nothing: online features are switched in Settings (ARC-11).
import { useEffect, useState, useSyncExternalStore } from "react";
import { canSendNow, indicatorOf, queuedItems, type ConnectivitySlice, type IndicatorAlias, type IndicatorIcon } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon, type IconName } from "../icons.js";
import { Button, Popover } from "../primitives/index.js";
import { useRequiredConnectivity } from "./context.js";

const COLOUR: Record<IndicatorAlias, string> = {
  "connectivity-local": "text-connectivity-local",
  "connectivity-offline": "text-connectivity-offline",
  "connectivity-online": "text-connectivity-online",
  "connectivity-egress": "text-connectivity-egress",
};

const GLYPH: Record<IndicatorIcon, IconName> = { house: "on-this-computer", cloud: "online", "up-arrow": "sending", "cloud-off": "cloud-unavailable" };

/** Features with their own "Sending" line; any other names its destination. */
const SENDING_LINES = new Set(["telemetry", "crash_reports", "downloads", "update_checks"]);

function FirstLine({ slice }: { slice: ConnectivitySlice }) {
  const t = useT();
  const indicator = indicatorOf(slice);
  const key = indicator.state.replace(/-/g, "_");
  let line: string;
  if (indicator.state === "offline") line = indicator.queued > 0 ? t(`connectivity.line.${key}`, { count: indicator.queued }) : t("connectivity.line.offline_empty");
  else if (indicator.state === "online-idle") line = t(`connectivity.line.${key}`, { features: indicator.enabled.map((f) => t(`connectivity.short.${f}`)).join(", ") });
  else if (indicator.state === "sending" && indicator.egress !== null && SENDING_LINES.has(indicator.egress.feature)) line = t(`connectivity.sending.${indicator.egress.feature}`);
  else if (indicator.state === "sending") line = t(`connectivity.line.${key}`, { destination: indicator.egress?.destination_label ?? "" });
  else line = t(`connectivity.line.${key}`);
  return <p className="text-ui text-ink">{line}</p>;
}

export function ConnectivityIndicatorView({ slice, open, onToggle, onSendNow, onOpenLedger }: {
  slice: ConnectivitySlice; open: boolean; onToggle: () => void; onSendNow: () => void; onOpenLedger: () => void;
}) {
  const t = useT();
  const indicator = indicatorOf(slice);
  const label = t(`connectivity.label.${indicator.state.replace(/-/g, "_")}`);
  const queued = queuedItems(slice);
  return (
    <div className="relative" onKeyDown={(event) => event.key === "Escape" && open && onToggle()}>
      <Popover open={open} label={t("connectivity.popover")} above>
        <div data-connectivity-popover="true" className="flex flex-col gap-2">
          {slice.status === "failed" ? <p className="text-ui text-ink">{t("connectivity.unavailable")}</p> : <FirstLine slice={slice} />}
          {queued > 0 && <p className="text-meta text-muted">{t("connectivity.waiting", { count: queued })}</p>}
          <ul className="flex flex-col gap-1">
            {slice.features.map((f) => (
              <li key={f.feature} data-feature={f.feature} className="flex flex-wrap items-baseline gap-x-2 text-meta text-muted">
                <span className="text-ui text-ink">{t(`settings.online.feature.${f.feature}`)}</span>
                <span>{f.enabled ? t("connectivity.feature.on") : t("connectivity.feature.off")}</span>
                {f.queued_items > 0 && <span>{t("connectivity.feature.queued", { count: f.queued_items })}</span>}
                {f.enabled && <span>{f.last_sent_at === null ? t("connectivity.feature.never_sent") : t("connectivity.feature.last_sent", { at: new Date(f.last_sent_at) })}</span>}
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            {canSendNow(slice) && <Button onClick={onSendNow}>{t("connectivity.send_now")}</Button>}
            <Button onClick={onOpenLedger}>{t("connectivity.ledger_link")}</Button>
          </div>
        </div>
      </Popover>
      <button
        type="button"
        data-connectivity-indicator="true"
        data-state={indicator.state}
        data-alias={indicator.alias}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={onToggle}
        className={`flex min-h-8 items-center gap-2 rounded-control px-2 text-meta ${COLOUR[indicator.alias]}`}
      >
        <Icon name={GLYPH[indicator.icon]} size={16} />
        <span key={label} className="jana-morph">{label}</span>
      </button>
    </div>
  );
}

/** The live indicator: seeded from the core when it mounts and whenever the window comes back into focus. */
export function ConnectivityIndicator({ onOpenLedger }: { onOpenLedger: () => void }) {
  const connectivity = useRequiredConnectivity();
  const slice = useSyncExternalStore(connectivity.subscribe, connectivity.getView, connectivity.getView);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const read = () => void connectivity.load();
    read();
    window.addEventListener("focus", read);
    return () => window.removeEventListener("focus", read);
  }, [connectivity]);
  return (
    <ConnectivityIndicatorView
      slice={slice}
      open={open}
      onToggle={() => setOpen((v) => !v)}
      onSendNow={() => void connectivity.sendNow().catch(() => connectivity.load())}
      onOpenLedger={() => {
        setOpen(false);
        onOpenLedger();
      }}
    />
  );
}
