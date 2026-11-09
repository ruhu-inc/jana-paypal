// The renderer registry (UIA-09) and its error boundaries (UIA-12). Keys: `part:<kind>`, `tool:<name>`,
// `card:<card_type>`, `visual:<kind>` and the wildcard `*`; lookup is most specific first. Data is validated against
// the entry's contract before rendering; invalid data renders the entry's Fallback. A render exception is logged with
// the key and a hash of the data (never the content) and shows the Fallback, never taking down the transcript.
import { Component, createContext, useContext, type ComponentType, type ErrorInfo, type ReactNode } from "react";
import { errors } from "@jana/contracts-ts/client";

export type Lifecycle = "in_progress" | "executing" | "complete" | "error" | "cancelled" | "retracted" | "validating" | "fallback";

export interface RenderProps<T> {
  data: T;
  lifecycle: Lifecycle;
  partId: string;
  onAction?: (action: string, payload?: unknown) => void;
}

export interface Entry<T = unknown> {
  /** A contract reference (`api/events#/$defs/card`) the data must satisfy, or null for client records. */
  schema: string | null;
  Component: ComponentType<RenderProps<T>>;
  Fallback: ComponentType<{ data: unknown; reason: string }>;
  renderClass: "card" | "ephemeral";
  summarise?: (data: T) => { pre: string; obj?: string; post?: string };
}

export class Registry {
  private readonly entries = new Map<string, Entry<never>>();

  register<T>(key: string, entry: Entry<T>): this {
    this.entries.set(key, entry as unknown as Entry<never>);
    return this;
  }

  /** The most specific entry for `keys` (in order), then the wildcard. */
  lookup(...keys: string[]): { key: string; entry: Entry<unknown> } {
    for (const key of [...keys, "*"]) {
      const entry = this.entries.get(key);
      if (entry !== undefined) return { key, entry: entry as unknown as Entry<unknown> };
    }
    throw new Error("registry has no wildcard entry");
  }

  keys(): string[] {
    return [...this.entries.keys()];
  }
}

const RegistryContext = createContext<Registry | null>(null);

export function RegistryProvider({ registry, children }: { registry: Registry; children: ReactNode }) {
  return <RegistryContext.Provider value={registry}>{children}</RegistryContext.Provider>;
}

export function useRegistry(): Registry {
  const registry = useContext(RegistryContext);
  if (registry === null) throw new Error("RegistryProvider missing");
  return registry;
}

/** FNV-1a over the JSON form: identifies the data in a log line without carrying any of its content (OBS-19). */
export function hashOf(data: unknown): string {
  let hash = 0x811c9dc5;
  const text = JSON.stringify(data) ?? "";
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export type RenderLog = (record: { key: string; kind: string; data_hash: string; error: string }) => void;

export const renderLog: { current: RenderLog } = {
  current: (record) => console.warn("render_failed", record),
};

interface BoundaryProps {
  entryKey: string;
  data: unknown;
  fallback: ReactNode;
  children: ReactNode;
}

export class RenderBoundary extends Component<BoundaryProps, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: Error, _info: ErrorInfo): void {
    renderLog.current({ key: this.props.entryKey, kind: this.props.entryKey.split(":")[0] ?? "", data_hash: hashOf(this.props.data), error: error.name });
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

/** Render `data` through the registry under `keys`, validated and isolated. */
export function Rendered<T>({ keys, data, lifecycle, partId, onAction }: { keys: string[]; data: T; lifecycle: Lifecycle; partId: string; onAction?: RenderProps<T>["onAction"] }) {
  const { key, entry } = useRegistry().lookup(...keys);
  const Fallback = entry.Fallback;
  if (entry.schema !== null) {
    const problems = errors(entry.schema, data);
    if (problems.length > 0) return <Fallback data={data} reason="invalid" />;
  }
  const View = entry.Component;
  const props: RenderProps<unknown> = { data, lifecycle, partId };
  if (onAction !== undefined) props.onAction = onAction;
  return (
    <RenderBoundary entryKey={key} data={data} fallback={<Fallback data={data} reason="render_failed" />}>
      <View {...props} />
    </RenderBoundary>
  );
}
