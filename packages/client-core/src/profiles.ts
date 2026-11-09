// Profiles, the profile token and family settings on the client (UXS-09, UXS-29, SEC-19, API-04, API-28, TCH-13).
// Profile tokens live in private fields of these objects only: never in the store, never in web storage, never in a URL.
// Locking, or unlocking another profile, empties the store and every registered cache before anything of the next
// profile can arrive (SEC-19); a reply that lands after a switch is dropped.
// The profile closes after the inactivity window the core gives with the token (SEC-19): when the student has not
// touched the app for that long, the profile is closed here too and the picker says why. The core counts only profile
// requests, so it can close a profile the student is still using (reading, typing a long answer) or lose every token
// when it restarts; then the profile is opened again at once, with no PIN to ask for (D86, D87), and the request that
// found it closed is sent again.
import type {
  PackList, Profile,
  CreateProfile, OnlineFeature, ProfileList, ProfileSummary, ProfileToken, Settings,
  SettingsPatch,
} from "@jana/contracts-ts";
import { ConnectivityController, type CloudSubscription, type ConnectivityReply, type SubscriptionApply } from "./connectivity.js";
import { clientId } from "./ids.js";
import { Observable } from "./observable.js";
import type { Launch, StoreHolder } from "./session.js";
import { emptyStore } from "./store.js";
import { CommandClient, CommandError } from "./stream/commands.js";
import { StreamClient, type StreamOptions } from "./stream/client.js";
import { readEnvelope } from "./tolerant.js";
import type { Envelope } from "./queue.js";

type Listener = () => void;

/** The last profile-open error and any API rate-limit delay. */
export interface OpenState {
  error: string | null;
  retry_at: number | null;
}

export interface ProfilesView {
  status: "loading" | "ready" | "failed";
  /** What the picker may show before unlock (API-28): id, display name and avatar. */
  profiles: ProfileSummary[];
  active: string | null;
  /** Per profile, so one profile's delay never holds up another (UXS-09). */
  opening: Record<string, OpenState>;
  /** The unlocked profile's own settings; gone with the profile. */
  settings: Settings | null;
  /** Why the last profile ended without the student switching (`profile_token_expired`: closed after inactivity, or
   * it could not be opened again); cleared when a profile opens. */
  ended: string | null;
}

export interface ProfileTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

/** The open profile's own preferences. */
export type StudentPreferences = SettingsPatch;


export interface ProfileOptions {
  fetch?: typeof fetch;
  now?: () => number;
  timers?: ProfileTimers;
}

const realTimers: ProfileTimers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

function openFailure(error: unknown, now: number): OpenState {
  const code = error instanceof CommandError ? error.code : "internal_error";
  const wait = error instanceof CommandError ? error.retryAfterMs : null;
  return { error: code, retry_at: code === "rate_limited" && wait !== null ? now + wait : null };
}

export class ProfileController {
  #token: string | null = null;
  /** Bumped by every clear: a reply to a request made before it belongs to a profile that has gone. */
  #generation = 0;
  /** The core's inactivity window for the open profile (`expires_in_s` of its token), when it gave one. */
  #idleMs: number | null = null;
  /** When the student last touched the app (keys, pointer, wheel); the inactivity window counts from here. */
  #lastActivity = 0;
  #idleTimer: unknown = null;
  #renewing: Promise<boolean> | null = null;
  /** Opens profiles with the launch token alone: never carries a profile token, never renews. */
  readonly #opener: CommandClient;
  readonly #timers: ProfileTimers;
  /** The launch with the unlocked profile's `X-Profile-Token`: sessions opened with it see only that profile; a
   * request that finds the profile closed by the core opens it again (`renew`). */
  readonly launch: Launch;
  readonly family: FamilySettings;
  /** The device-wide connectivity slice (UIA-23): fed by every session's commits, its session egress cleared with the
   * profile. It switches nothing: online features change only through `family` (ARC-11, PRD-21). */
  readonly connectivity: ConnectivityController;
  readonly commands: CommandClient;
  private readonly view = new Observable<ProfilesView>({ status: "loading", profiles: [], active: null, opening: {}, settings: null, ended: null });
  private readonly clearers = new Set<() => void>();
  readonly now: () => number;
  private readonly appOptions: StreamOptions;
  private appStream: StreamClient | null = null;
  private appUsers = 0;
  private appCursor: string | null = null;
  private readonly appListeners = new Set<(envelope: Envelope) => void>();
  private readonly appResets = new Set<() => void>();

  constructor(base: Launch, readonly holder: StoreHolder, options: ProfileOptions = {}) {
    this.now = options.now ?? (() => Date.now());
    this.#timers = options.timers ?? realTimers;
    this.launch = {
      endpoint: async (refresh) => {
        const endpoint = await base.endpoint(refresh);
        const token = this.#token;
        return token === null ? endpoint : { ...endpoint, headers: { ...endpoint.headers, "x-profile-token": token } };
      },
      renew: () => this.renew(),
    };
    this.#opener = new CommandClient(options.fetch === undefined ? { endpoint: (refresh) => base.endpoint(refresh) }
      : { endpoint: (refresh) => base.endpoint(refresh), fetch: options.fetch });
    const commandOptions: ConstructorParameters<typeof CommandClient>[0] = { endpoint: (refresh) => this.launch.endpoint(refresh), renew: () => this.renew() };
    if (options.fetch !== undefined) commandOptions.fetch = options.fetch;
    this.commands = new CommandClient(commandOptions);
    this.family = new FamilySettings(this);
    this.connectivity = new ConnectivityController(this.commands);
    this.appOptions = { sessionId: "app", path: "/v1/events", endpoint: (refresh) => base.endpoint(refresh),
      cursor: () => this.appCursor, onState: () => {},
      onEnvelope: (raw) => {
        const read = readEnvelope(raw);
        if (read.kind !== "accept" || read.envelope.channel !== "app") return;
        const envelope = read.envelope as Envelope;
        if (this.appCursor !== null) {
          const [epoch, seq] = this.appCursor.split(":");
          if (epoch === envelope.epoch && envelope.seq <= Number(seq)) return;
        }
        this.appCursor = `${envelope.epoch}:${envelope.seq}`;
        this.connectivity.receive(envelope);
        for (const listener of this.appListeners) listener(envelope);
      }, resync: async () => {
        this.appCursor = null;
        await this.connectivity.load();
        for (const reset of this.appResets) reset();
      }, ...(options.fetch === undefined ? {} : { fetch: options.fetch }) };
    holder.onEnvelopes((batch) => this.connectivity.receiveBatch(batch));
  }

  watchApp(): () => void {
    this.appUsers += 1;
    if (this.appStream === null) { this.appStream = new StreamClient(this.appOptions); this.appStream.start(); }
    let closed = false;
    return () => {
      if (closed) return;
      closed = true;
      this.appUsers -= 1;
      if (this.appUsers === 0) { this.appStream?.stop(); this.appStream = null; }
    };
  }

  onAppEnvelope(listener: (envelope: Envelope) => void): () => void {
    this.appListeners.add(listener);
    return () => { this.appListeners.delete(listener); };
  }
  onAppReset(listener: () => void): () => void {
    this.appResets.add(listener);
    return () => { this.appResets.delete(listener); };
  }

  getView = (): ProfilesView => this.view.get();
  subscribe = (listener: Listener): (() => void) => this.view.subscribe(listener);

  get unlocked(): boolean {
    return this.#token !== null;
  }

  /** Changes on every lock and switch: work started under an older value belongs to a profile that has gone. */
  get generation(): number {
    return this.#generation;
  }

  /** Register something that holds the unlocked profile's data outside the store (a render cache, an open session):
   * it is cleared with the store on every lock and switch. */
  onClear(clear: () => void): () => void {
    this.clearers.add(clear);
    return () => this.clearers.delete(clear);
  }

  private patch(change: Partial<ProfilesView>): void {
    this.view.set({ ...this.view.get(), ...change });
  }

  private setOpenState(profileId: string, state: OpenState): void {
    this.patch({ opening: { ...this.view.get().opening, [profileId]: state } });
  }

  async load(): Promise<void> {
    if (this.view.get().status !== "ready") this.patch({ status: "loading" });
    try {
      const list = await this.commands.get<ProfileList>("/v1/profiles");
      const profiles = list.profiles.map(({ profile_id, display_name, avatar }) => ({ profile_id, display_name, avatar }));
      this.patch({ status: "ready", profiles });
    } catch {
      this.patch({ status: "failed" });
    }
  }

  /** Unlock a profile. Switching from another one locks that one first, so none of its data stays (SEC-19). */
  async unlock(profileId: string): Promise<boolean> {
    const current = this.view.get().active;
    if (current !== null && current !== profileId) await this.lock();
    const generation = this.#generation;
    try {
      const token = await this.#opener.post<ProfileToken>(`/v1/profiles/${profileId}/open`, {}, clientId());
      if (generation !== this.#generation) return false;
      this.#accept(token);
      this.setOpenState(profileId, { error: null, retry_at: null });
      this.patch({ active: profileId, settings: null, ended: null });
      return true;
    } catch (error) {
      this.setOpenState(profileId, openFailure(error, this.now()));
      return false;
    }
  }

  #accept(token: ProfileToken): void {
    this.#token = token.profile_token;
    this.#idleMs = typeof token.expires_in_s === "number" && token.expires_in_s > 0 ? token.expires_in_s * 1000 : null;
    this.#lastActivity = this.now();
    this.#armIdle(this.#idleMs);
  }

  #armIdle(wait: number | null): void {
    if (this.#idleTimer !== null) this.#timers.clearTimeout(this.#idleTimer);
    this.#idleTimer = wait === null ? null : this.#timers.setTimeout(() => this.#checkIdle(), wait);
  }

  /** The window has passed since the timer was armed: close the profile if the student has not been active since,
   * otherwise wait out what is left of it from their last activity. */
  #checkIdle(): void {
    this.#idleTimer = null;
    if (this.#token === null || this.#idleMs === null) return;
    const left = this.#lastActivity + this.#idleMs - this.now();
    if (left > 0) this.#armIdle(left);
    else void this.expire();
  }

  /** The student touched the app: the inactivity window starts again (SEC-19). */
  noteActivity(): void {
    if (this.#token !== null) this.#lastActivity = this.now();
  }

  /** Whether the student has been active within the open profile's inactivity window. */
  get recentlyActive(): boolean {
    return this.#token !== null && (this.#idleMs === null || this.now() - this.#lastActivity < this.#idleMs);
  }

  /** Open the unlocked profile again after the core closed it (its idle auto-lock counts only profile requests, and a
   * restarted core has no tokens). Only while the student is active; otherwise, or if it cannot be opened, the profile
   * ends here as well and the picker says so. Concurrent callers share one attempt. */
  renew(): Promise<boolean> {
    this.#renewing ??= this.#reopen().finally(() => {
      this.#renewing = null;
    });
    return this.#renewing;
  }

  async #reopen(): Promise<boolean> {
    const profileId = this.view.get().active;
    const generation = this.#generation;
    if (profileId === null || this.#token === null) return false;
    if (!this.recentlyActive) {
      await this.expire();
      return false;
    }
    try {
      const token = await this.#opener.post<ProfileToken>(`/v1/profiles/${profileId}/open`, {}, clientId());
      if (generation !== this.#generation) return false;
      this.#accept(token);
      return true;
    } catch {
      if (generation === this.#generation) this.clear("profile_token_expired");
      return false;
    }
  }

  /** The profile ended without the student switching (inactivity, or it could not be opened again): it closes here as
   * it would on "Switch student", and the picker says why. */
  async expire(): Promise<void> {
    if (this.view.get().active === null) return;
    await this.lock("profile_token_expired");
  }

  /** End the unlocked profile: everything local goes at once, then the core is told (best effort: it also auto-locks). */
  async lock(reason: string | null = null): Promise<void> {
    const active = this.view.get().active;
    const token = this.#token;
    this.clear(reason);
    if (active === null || token === null) return;
    await this.#opener.post(`/v1/profiles/${active}/close`, {}, clientId(), { "x-profile-token": token }).catch(() => undefined);
  }

  /** Drop the profile token and every in-memory trace of the profile: the store, its settings, registered caches. */
  clear(reason: string | null = null): void {
    this.#token = null;
    this.#idleMs = null;
    this.#armIdle(null);
    this.#generation += 1;
    this.connectivity.clearSessions();
    for (const clear of [...this.clearers]) clear();
    this.holder.update(() => emptyStore());
    this.patch({ active: null, settings: null, ended: reason });
  }

  /** The active profile and the generation it was read in; throws when no profile is unlocked. */
  scope(): { profileId: string; generation: number } {
    const profileId = this.view.get().active;
    if (profileId === null || this.#token === null) throw new CommandError(401, "profile_token_expired", false, undefined);
    return { profileId, generation: this.#generation };
  }

  /** Keep a settings reply only if the profile it was asked for is still the unlocked one. */
  accept(generation: number, settings: Settings): Settings {
    if (generation === this.#generation) this.patch({ settings });
    return settings;
  }

  async loadSettings(): Promise<Settings> {
    const { profileId, generation } = this.scope();
    return this.accept(generation, await this.commands.get<Settings>(`/v1/profiles/${profileId}/settings`));
  }

  /** Change student preferences with the profile token alone (UXS-29). The answer policy is not one of them. */
  async setPreferences(patch: StudentPreferences): Promise<Settings> {
    const { profileId, generation } = this.scope();
    return this.accept(generation, await this.commands.patch<Settings>(`/v1/profiles/${profileId}/settings`, patch, clientId()));
  }
}

/** Family settings use launch authority; learner operations also carry the open profile token (D87). */
export class FamilySettings {
  constructor(private readonly profiles: ProfileController) {}

  private async family<T>(work: (headers: Record<string, string>) => Promise<T>): Promise<T> {
    return work({});
  }

  /** The installed packs, each verified by the core as it lists them; `signature_ok` false is a damaged pack (CUR-08). */
  async packs(): Promise<PackList["packs"]> {
    return (await this.profiles.commands.get<PackList>("/v1/packs")).packs;
  }

  /** Online features: the device-wide switches (launch token; shown in family settings). */
  connectivity(): Promise<ConnectivityReply> {
    return this.profiles.commands.get<ConnectivityReply>("/v1/connectivity");
  }

  /** Switch online features (ARC-11): for a data-sending feature, device reports record device consent with the switch. Paid profile consent comes from checkout. The indicator follows the reply. */
  setOnlineFeatures(features: Partial<Record<OnlineFeature, boolean>>): Promise<ConnectivityReply> {
    return this.family(async (h) => {
      const reply = await this.profiles.commands.patch<ConnectivityReply>("/v1/connectivity", { features }, clientId(), h);
      this.profiles.connectivity.seed(reply);
      return reply;
    });
  }

  /** The cloud tutor's subscription (D70, BIZ-07): subscribing turns the cloud tier on, ending it turns it off. Checkout records the guardian's agreement for named minors. */
  subscription(): Promise<CloudSubscription> {
    return this.family((h) => this.profiles.commands.get<CloudSubscription>("/v1/subscription", h));
  }

  /** Apply the checkout's entitlement with the guardian's consent for each profile under 18 it names (D70, PRV-06). */
  applySubscription(body: SubscriptionApply): Promise<CloudSubscription> {
    return this.family(async (h) => {
      const reply = await this.profiles.commands.post<CloudSubscription>("/v1/subscription", body, clientId(), h);
      void this.profiles.connectivity.load();
      return reply;
    });
  }

  /** End the subscription: the cloud tier goes off and its consents are withdrawn (D70). */
  endSubscription(): Promise<CloudSubscription> {
    return this.family(async (h) => {
      const reply = await this.profiles.commands.delete<CloudSubscription>("/v1/subscription", clientId(), h);
      void this.profiles.connectivity.load();
      return reply;
    });
  }

  /** Profiles: add a student. */
  addProfile(body: CreateProfile): Promise<Profile> {
    return this.family(async (h) => {
      const created = await this.profiles.commands.post<Profile>("/v1/profiles", body, clientId(), h);
      await this.profiles.load();
      return created;
    });
  }

  /** Profiles: one-step delete (DAT-09). Deleting the unlocked profile ends it here too. */
  deleteProfile(profileId: string): Promise<void> {
    return this.family(async (h) => {
      await this.profiles.commands.delete<unknown>(`/v1/profiles/${profileId}`, clientId(), h);
      if (this.profiles.getView().active === profileId) this.profiles.clear();
      await this.profiles.load();
    });
  }

}
