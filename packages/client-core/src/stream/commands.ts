// Commands to the local core (API-23, API-31). Every mutating command carries an Idempotency-Key (a `cli_` id), so a
// retry after a failure or a core restart never runs twice. Commands that fail to reach the core are kept and retried
// with the same key when the stream is back (UIA-07); a reconnect itself never re-sends anything.
import type { Endpoint } from "./client.js";

export interface CommandOptions {
  endpoint(refresh: boolean): Promise<Endpoint>;
  /** Open the profile again after the core closed it (`profile_token_expired`: idle auto-lock or a core restart,
   * SEC-19); true when the request may be sent again with the new token. */
  renew?: () => Promise<boolean>;
  fetch?: typeof fetch;
  /** A bound request must stop when its owning profile changes, including before any retry. */
  isCurrent?: () => boolean;
}

export class CommandError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryable: boolean,
    readonly details: Record<string, unknown> | undefined,
    /** How long to wait before trying again (`rate_limited`), when the core says. */
    readonly retryAfterMs: number | null = null,
  ) {
    super(code);
  }
}

/** Raised when the core could not be reached at all (restarting or gone): the command may be retried as it is. */
export class Unreachable extends Error {}

export class CommandClient {
  private readonly fetchImpl: typeof fetch;

  constructor(private readonly options: CommandOptions) {
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  scoped(isCurrent: () => boolean): CommandClient {
    return new CommandClient({ ...this.options, fetch: this.fetchImpl,
      isCurrent: () => (this.options.isCurrent?.() ?? true) && isCurrent() });
  }

  private assertCurrent(): void {
    if (this.options.isCurrent && !this.options.isCurrent()) throw new CommandError(401, "profile_token_expired", false, undefined);
  }

  /** `headers` are this request's own; the endpoint's (the profile token) go on every one. */
  async request<T>(method: string, path: string, body: unknown, key: string | null, headers: Record<string, string> = {}, binary = false): Promise<T> {
    let refreshed = false; // a restarted core may be on another port or have a new launch token: ask the shell again once
    let renewed = false; // the profile token ended: open the profile again once (D87: no PIN, so nothing to ask)
    for (;;) {
      this.assertCurrent();
      let response: Response;
      try {
        const endpoint = await this.options.endpoint(refreshed);
        this.assertCurrent();
        const all: Record<string, string> = { ...endpoint.headers, ...headers, authorization: `Bearer ${endpoint.token}`, ...(body instanceof FormData ? {} : { "content-type": "application/json" }) };
        if (key !== null) all["idempotency-key"] = key;
        const init: RequestInit = { method, headers: all };
        if (body !== undefined) init.body = body instanceof FormData ? body : JSON.stringify(body);
        response = await this.fetchImpl(`${endpoint.baseUrl}${path}`, init);
      } catch {
        this.assertCurrent();
        if (!refreshed) {
          refreshed = true;
          continue;
        }
        throw new Unreachable(path);
      }
      this.assertCurrent();
      if (response.status === 204) return undefined as T;
      if (response.ok && binary) { const blob = await response.blob(); this.assertCurrent(); return blob as T; }
      const payload = (await response.json().catch(() => null)) as { error?: { code: string; retryable: boolean; details?: Record<string, unknown>; retry_after_ms?: number } } | null;
      this.assertCurrent();
      const error = payload?.error;
      if (response.status === 401 && error?.code === "profile_token_expired") {
        if (!renewed && this.options.renew !== undefined && await this.options.renew()) {
          renewed = true;
          continue;
        }
      } else if (response.status === 401 && !refreshed) {
        refreshed = true;
        continue;
      }
      if (!response.ok) {
        throw new CommandError(response.status, error?.code ?? "internal_error", error?.retryable ?? false, error?.details, error?.retry_after_ms ?? null);
      }
      return payload as T;
    }
  }

  get<T>(path: string, headers?: Record<string, string>): Promise<T> {
    return this.request<T>("GET", path, undefined, null, headers);
  }

  post<T>(path: string, body: unknown, key: string, headers?: Record<string, string>): Promise<T> {
    return this.request<T>("POST", path, body, key, headers);
  }

  patch<T>(path: string, body: unknown, key: string, headers?: Record<string, string>): Promise<T> {
    return this.request<T>("PATCH", path, body, key, headers);
  }

  put<T>(path: string, body: unknown, key: string, headers?: Record<string, string>): Promise<T> {
    return this.request<T>("PUT", path, body, key, headers);
  }

  delete<T>(path: string, key: string, headers?: Record<string, string>): Promise<T> {
    return this.request<T>("DELETE", path, undefined, key, headers);
  }
}
