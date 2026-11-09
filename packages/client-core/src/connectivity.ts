// The connectivity slice of the app (UIA-23, API-15): the network state and the online features the core lists for this
// build's stage, from `GET /v1/connectivity` and `connectivity.status`; the egress in flight, from `egress.notice` on
// the app channel (downloads, update checks, usage and crash reports) and on the session channel (the cloud tutor);
// the cloud fallback notices of a turn (MDL-24, UXS-08). The indicator state of TABLE UXS-06a and its TABLE DSN-19a
// alias are derived by pure functions. A `blocked` notice and the network dropping change only this slice: they never
// touch the session store, so rendering, the composer and the board carry on (UIA-23, UXS-08).
//
// The app channel transport arrives in M20.b: until then the slice is seeded from `GET /v1/connectivity`, and
// `receive` already takes `connectivity.status` and app-channel `egress.notice` envelopes, so M20.b only wires the
// stream. Session envelopes reach it from the session controller's commits (`StoreHolder.deliver`).
// TODO(API-42): the school-server rows of TABLE UXS-06a (S4, `hosted` and `local_fallback`) come with API-42.
import type { ConnectivityState, EvConnectivityStatus, OnlineFeature, OnlineFeatureState } from "@jana/contracts-ts";
import { clientId } from "./ids.js";
import { Observable } from "./observable.js";
import type { Envelope } from "./queue.js";
import type { CommandClient } from "./stream/commands.js";

/** The online features of TABLE ARC-07a, in its order, then Personal tutor memory online (SPEC §19.6, appended after
 * crash reports as in the egress policy). The slice lists only those the core lists (the build's stage). */
export const ONLINE_FEATURES = [
  "cloud_tier", "downloads", "update_checks", "cloud_backup", "multi_device", "parent_remote_view", "cloud_voice", "study_materials",
  "telemetry", "crash_reports", "tutor_memory_online",
] as const satisfies readonly OnlineFeature[];

/** Features whose switch is the guardian's consent (ARC-11, UXS-07, PRV-05); downloads and update checks need only the
 * switch. `cloud_tier` is switched on by subscribing (D70). */
export const CONSENT_FEATURES: ReadonlySet<OnlineFeature> = new Set<OnlineFeature>([
  "cloud_tier", "cloud_backup", "multi_device", "parent_remote_view", "cloud_voice", "study_materials", "telemetry", "crash_reports",
  "tutor_memory_online",
]);

/** The version of each consent text the client shows (PRV-05: consent is recorded with its notice version). */
export const CONSENT_NOTICE_VERSIONS = { cloud_tier: "2026-10.d1", tutor_memory_online: "2026-10.d1" } as const satisfies Partial<Record<OnlineFeature, string>>;

/** Notices that say a turn left the cloud tutor for the tutor on this computer (MDL-24, UXS-08, API-15). */
export const CLOUD_FALLBACK_NOTICES = ["cloud_unavailable_using_local", "cloud_continuing_locally"] as const;

export type Network = EvConnectivityStatus["network"];

// Proposed contract additions (contract-additions-m19.json at the worktree root, UXS-06, UXS-08, ARC-10, BIZ-07, D70):
// local types until the specification adopts them; the generated types are not edited.

/** One feature of `ConnectivityState`, with the proposed `queued_items` and `last_sent_at`. */
export interface FeatureReply {
  enabled: boolean;
  state: OnlineFeatureState;
  queued_items?: number;
  last_sent_at?: string | null;
}

/** `ConnectivityState` with the proposed per-feature fields. */
export interface ConnectivityReply extends Omit<ConnectivityState, "features"> {
  features: Record<string, FeatureReply | undefined>;
}

/** One request in the egress ledger (proposed `EgressLedger`, ARC-10): never any content. */
export interface EgressLedgerEntry {
  at: string;
  feature: OnlineFeature | "lan_compute";
  destination: string;
  bytes: number;
  payload_class: string;
}

/** Proposed `EgressLedger` (`GET /v1/connectivity/ledger`). */
export interface EgressLedger {
  retention_days: number;
  entries: EgressLedgerEntry[];
}

/** Proposed `CloudSubscription` (`GET|POST|DELETE /v1/subscription`, BIZ-07, D70). */
export interface CloudSubscription {
  state: "none" | "pending_consent" | "active" | "ended";
  subscription_id: string | null;
  period_end: string | null;
  allowance_used_up: boolean;
  profiles: { profile_id: string; consent: "recorded" | "needed" }[];
}

/** Proposed `SubscriptionApply` (`POST /v1/subscription`): the checkout's entitlement and the guardian's consents. */
export interface SubscriptionApply {
  entitlement: Record<string, unknown>;
  consents: { profile_id: string; notice_version: string }[];
}

export interface FeatureStatus {
  feature: OnlineFeature;
  enabled: boolean;
  state: OnlineFeatureState;
  queued_items: number;
  last_sent_at: string | null;
}

/** One `egress.notice` as it stands: the latest state of a feature's request on the app channel, or of a turn's. */
export interface EgressRec {
  /** An id of TABLE ARC-07a, or one this build does not know (open enum, API-35): still shown while it sends. */
  feature: string;
  destination_label: string;
  data_classes: string[];
  state: "started" | "ended" | "blocked";
  /** A cloud fallback notice arrived after the request started (a turn's only). */
  fallback: boolean;
  /** A request of this record ever started. */
  started: boolean;
}

export interface ConnectivitySlice {
  status: "loading" | "ready" | "failed";
  network: Network;
  checked_ts: number | null;
  features: FeatureStatus[];
  /** App-channel egress, by feature. */
  app: Record<string, EgressRec>;
  /** Session-channel egress, by turn id (`session:<id>` for one outside a turn). */
  turns: Record<string, EgressRec>;
  /** The latest turn seen: its fallback notice shows as "Using local tutor" until the next turn. */
  latest_turn: string | null;
  /** The last envelope seen per channel, so a replayed one changes nothing. */
  cursors: Record<string, { epoch: string; seq: number }>;
}

export function emptyConnectivity(): ConnectivitySlice {
  return { status: "loading", network: "unknown", checked_ts: null, features: [], app: {}, turns: {}, latest_turn: null, cursors: {} };
}

const KNOWN = new Set<string>(ONLINE_FEATURES);
const isFeature = (value: string): value is OnlineFeature => KNOWN.has(value);
const order = (a: FeatureStatus, b: FeatureStatus) => ONLINE_FEATURES.indexOf(a.feature as never) - ONLINE_FEATURES.indexOf(b.feature as never);

/** Seed (or refresh) from `GET /v1/connectivity`, a `PATCH` or "Send now". The egress in flight is kept. */
export function seedConnectivity(slice: ConnectivitySlice, reply: ConnectivityReply): ConnectivitySlice {
  const features: FeatureStatus[] = [];
  for (const [feature, value] of Object.entries(reply.features)) {
    if (!isFeature(feature) || value === undefined) continue; // a feature this build does not know is not listed
    features.push({ feature, enabled: value.enabled, state: value.state, queued_items: value.queued_items ?? 0, last_sent_at: value.last_sent_at ?? null });
  }
  return { ...slice, status: "ready", network: reply.network, features: features.sort(order) };
}

function cursorKey(envelope: Envelope): string {
  return envelope.channel === "session" ? `session:${envelope.session_id ?? ""}` : envelope.channel;
}

/** Fold one envelope. Anything that is not connectivity or egress changes nothing; a replayed envelope neither. */
export function reduceConnectivity(slice: ConnectivitySlice, envelope: Envelope): ConnectivitySlice {
  const key = cursorKey(envelope);
  const cursor = slice.cursors[key];
  if (cursor !== undefined && cursor.epoch === envelope.epoch && envelope.seq <= cursor.seq) return slice;
  const event = envelope.event;
  const relevant = event.type === "connectivity.status" || event.type === "egress.notice" || event.type === "notice" || event.type === "turn.start" || event.type === "turn.end";
  if (!relevant) return slice;
  const next: ConnectivitySlice = { ...slice, cursors: { ...slice.cursors, [key]: { epoch: envelope.epoch, seq: envelope.seq } } };
  const turnKey = envelope.turn_id ?? `session:${envelope.session_id ?? ""}`;
  switch (event.type) {
    case "connectivity.status": {
      const status = event as unknown as EvConnectivityStatus;
      const before = new Map(slice.features.map((f) => [f.feature, f]));
      const features: FeatureStatus[] = [];
      for (const item of status.features) {
        if (!isFeature(item.feature)) continue;
        features.push({ feature: item.feature, enabled: item.enabled, state: item.state, queued_items: item.queued_items, last_sent_at: before.get(item.feature)?.last_sent_at ?? null });
      }
      return { ...next, status: "ready", network: status.network, checked_ts: status.checked_ts, features: features.sort(order) };
    }
    case "egress.notice": {
      const state = event.state as EgressRec["state"];
      const notice = { feature: String(event.feature), destination_label: String(event.destination_label ?? ""), data_classes: (event.data_classes as string[] | undefined) ?? [], state };
      if (envelope.channel !== "session") {
        const previous = slice.app[notice.feature];
        return { ...next, app: { ...slice.app, [notice.feature]: { ...notice, fallback: false, started: state === "started" || (previous?.started ?? false) } } };
      }
      const previous = slice.turns[turnKey];
      const rec: EgressRec = { ...notice, fallback: previous?.fallback ?? false, started: state === "started" || (previous?.started ?? false) };
      return { ...next, turns: { ...slice.turns, [turnKey]: rec }, latest_turn: envelope.turn_id ?? slice.latest_turn };
    }
    case "notice": {
      if (envelope.channel !== "session" || !(CLOUD_FALLBACK_NOTICES as readonly string[]).includes(String(event.code))) return next;
      const previous = slice.turns[turnKey] ?? { feature: "cloud_tier", destination_label: "", data_classes: [], state: "blocked", started: false, fallback: false };
      const settled = previous.state === "started" ? "ended" : previous.state; // the turn carries on locally
      return { ...next, turns: { ...slice.turns, [turnKey]: { ...previous, state: settled, fallback: true } }, latest_turn: envelope.turn_id ?? slice.latest_turn };
    }
    case "turn.start":
      return envelope.channel === "session" ? { ...next, latest_turn: (event.turn_id as string | undefined) ?? envelope.turn_id } : next;
    case "turn.end": {
      // A turn that ended has nothing in flight, even if its `ended` never arrived.
      const rec = slice.turns[turnKey];
      if (rec === undefined || rec.state !== "started") return next;
      return { ...next, turns: { ...slice.turns, [turnKey]: { ...rec, state: "ended" } } };
    }
    default:
      return next;
  }
}

/** Forget every session's egress (a lock or a profile switch, SEC-19); the device-wide state stays. */
export function clearSessions(slice: ConnectivitySlice): ConnectivitySlice {
  const cursors = Object.fromEntries(Object.entries(slice.cursors).filter(([key]) => !key.startsWith("session:")));
  return { ...slice, turns: {}, latest_turn: null, cursors };
}

/** The requests in flight: the app's first, then the turns'. */
export function activeEgress(slice: ConnectivitySlice): EgressRec[] {
  return [...Object.values(slice.app), ...Object.values(slice.turns)].filter((e) => e.state === "started");
}

export function queuedItems(slice: ConnectivitySlice): number {
  return slice.features.reduce((n, f) => n + f.queued_items, 0);
}

export function turnEgress(slice: ConnectivitySlice, turnId: string): EgressRec | null {
  return slice.turns[turnId] ?? null;
}

/** "Answered by the online tutor" (MDL-24): the turn's cloud request started and no fallback notice followed. */
export function cloudAnswered(rec: EgressRec | null): boolean {
  return rec !== null && rec.feature === "cloud_tier" && rec.started && !rec.fallback && rec.state !== "blocked";
}

/** The S2 states of TABLE UXS-06a, named as the gallery's `context.connectivity`. */
export const INDICATOR_STATES = ["all-off", "offline", "limited", "metered", "online-idle", "sending", "cloud-tutor", "online-unavailable"] as const;
export type IndicatorState = (typeof INDICATOR_STATES)[number];
export type IndicatorAlias = "connectivity-local" | "connectivity-offline" | "connectivity-online" | "connectivity-egress";
export type IndicatorIcon = "house" | "cloud" | "up-arrow" | "cloud-off";

/** TABLE DSN-19a. */
export const INDICATOR_ALIAS: Record<IndicatorState, IndicatorAlias> = {
  "all-off": "connectivity-local", offline: "connectivity-offline", limited: "connectivity-offline", metered: "connectivity-online",
  "online-idle": "connectivity-online", sending: "connectivity-egress", "cloud-tutor": "connectivity-online", "online-unavailable": "connectivity-offline",
};

/** The icons of TABLE UXS-06a. */
export const INDICATOR_ICON: Record<IndicatorState, IndicatorIcon> = {
  "all-off": "house", offline: "house", limited: "house", metered: "cloud", "online-idle": "cloud", sending: "up-arrow", "cloud-tutor": "cloud",
  "online-unavailable": "cloud-off",
};

export interface Indicator {
  state: IndicatorState;
  alias: IndicatorAlias;
  icon: IndicatorIcon;
  /** The request shown while sending (`sending`, `cloud-tutor`). */
  egress: EgressRec | null;
  /** The features switched on for this computer, in table order. */
  enabled: OnlineFeature[];
  queued: number;
}

/** The indicator state (TABLE UXS-06a). Egress in flight always shows, whatever else holds (PRV-28: "Everything stays
 * on this computer" only while nothing leaves); then a cloud fallback in the latest turn; then the network. */
function stateOf(slice: ConnectivitySlice, active: EgressRec[], enabled: OnlineFeature[]): IndicatorState {
  if (active.some((e) => e.feature === "cloud_tier")) return "cloud-tutor";
  if (active.length > 0) return "sending";
  const latest = slice.latest_turn === null ? undefined : slice.turns[slice.latest_turn];
  if (latest?.fallback === true) return "online-unavailable";
  if (slice.network === "off" || enabled.length === 0) return "all-off";
  if (slice.network === "offline" || slice.network === "unknown") return "offline";
  if (slice.network === "limited") return "limited";
  if (slice.features.some((f) => f.enabled && f.state === "unavailable")) return "online-unavailable";
  return slice.network === "metered" ? "metered" : "online-idle";
}

export function indicatorOf(slice: ConnectivitySlice): Indicator {
  const active = activeEgress(slice);
  const enabled = slice.features.filter((f) => f.enabled).map((f) => f.feature);
  const state = stateOf(slice, active, enabled);
  const shown = state === "cloud-tutor" ? active.find((e) => e.feature === "cloud_tier") ?? null : state === "sending" ? active[0] ?? null : null;
  return { state, alias: INDICATOR_ALIAS[state], icon: INDICATOR_ICON[state], egress: shown, enabled, queued: queuedItems(slice) };
}

/** "Send now" is offered only online, with something queued (UXS-08). */
export function canSendNow(slice: ConnectivitySlice): boolean {
  return (slice.network === "online" || slice.network === "metered") && queuedItems(slice) > 0;
}

/** The app slice: one per client, device-wide; the session egress in it is cleared with the profile. */
export class ConnectivityController {
  private readonly view = new Observable<ConnectivitySlice>(emptyConnectivity());

  constructor(private readonly commands: CommandClient) {}

  getView = (): ConnectivitySlice => this.view.get();
  subscribe = (listener: () => void): (() => void) => this.view.subscribe(listener);

  /** Seed or refresh from the core; a failed read keeps what was known. */
  async load(): Promise<void> {
    try {
      this.seed(await this.commands.get<ConnectivityReply>("/v1/connectivity"));
    } catch {
      if (this.view.get().status === "loading") this.view.set({ ...this.view.get(), status: "failed" });
    }
  }

  seed(reply: ConnectivityReply): void {
    this.view.set(seedConnectivity(this.view.get(), reply));
  }

  receive(envelope: Envelope): void {
    this.view.set(reduceConnectivity(this.view.get(), envelope));
  }

  receiveBatch(batch: readonly Envelope[]): void {
    let next = this.view.get();
    for (const envelope of batch) next = reduceConnectivity(next, envelope);
    this.view.set(next);
  }

  /** The popover's "Send now" (UXS-08): the core drains the outbox of enabled features once. */
  async sendNow(): Promise<void> {
    this.seed(await this.commands.post<ConnectivityReply>("/v1/connectivity/send-now", {}, clientId()));
  }

  /** Settings → "What leaves this device" (ARC-10): with a profile token, the device's rows and that profile's. */
  ledger(): Promise<EgressLedger> {
    return this.commands.get<EgressLedger>("/v1/connectivity/ledger");
  }

  clearSessions(): void {
    this.view.set(clearSessions(this.view.get()));
  }
}
