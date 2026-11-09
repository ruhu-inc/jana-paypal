// The renderer contract (VSL-18): one renderer per kind, a pure function of its props: no network, no timers beyond
// declared animations, no model calls, no eval. Output is SVG and HTML; colours and strokes come from design tokens.
// User-visible words come through `t`, the host's translator (LOC-01); numbers come from the resolved spec only.
import type { ReactElement } from "react";
import type { VisualsVisualSpecResolved } from "@jana/contracts-ts";

export type ResolvedSpec = VisualsVisualSpecResolved;
export type LabelMode = "shown" | "hidden" | "quiz";
export type Translate = (key: string, args?: Record<string, string | number>) => string;

export interface PartInfo {
  id: string;
  name: string;
  role?: string;
  focusable: boolean;
  bounds?: { x: number; y: number; width: number; height: number };
}

export interface Size {
  width: number;
  height: number;
}

/** A table alternative for graphs and charts (LOC-16): the plotted values, as the tool worker gave them. */
export interface TableData {
  columns: string[];
  rows: string[][];
}

export interface VisualProps {
  spec: ResolvedSpec;
  step: number;
  labels: LabelMode;
  focus: string[];
  /** The tutor's spotlight (UXS-57): every part outside `focus` is dimmed while it lasts. */
  spotlight?: boolean;
  size: Size;
  reducedMotion: boolean;
  onPartActivate(id: string): void;
  /** The host's translator; renderers carry no user-visible literals (LOC-01). */
  t?: Translate;
  /** Text size multiplier (DSN-07: 1.0 to 1.5); label positions are recomputed for it. */
  textScale?: number;
  /** Simulation events to the client store (VSL-30); other kinds ignore it. */
  onSimEvent?(event: SimEvent): void;
  /** Whether the visual is on screen; simulations pause when it is not (VSL-30, UIA-20). */
  visible?: boolean;
}

export interface SimEvent {
  type: "sim.param_changed" | "sim.reading_recorded" | "sim.reset" | "sim.prediction_committed" | "sim.explained";
  visual_id: string;
  param?: string;
  value?: number | string | boolean;
  reading?: Record<string, string>;
  /** Predict, observe, explain (VSL-47): the scenario, its pack misconception and what the student wrote and chose. */
  scenario_id?: string;
  misconception_id?: string;
  prediction?: string;
  choice?: string | null;
  correct?: boolean | null;
  common_guess?: boolean;
  explanation?: string;
}

export interface VisualRenderer {
  kind: string;
  specVersion: 1;
  /** The kind's params contract: a specification contract id, or `renderers/kinds/<kind>` for a renderer-owned one. */
  schema: string;
  /** Errors in the params against the kind's schema (client re-validation, UIA-20); [] when valid. */
  check(params: unknown): string[];
  /** The label modes this kind offers (VSL-11); quiz only where the kind allows it. */
  labelModes: readonly LabelMode[];
  parts(resolved: ResolvedSpec): PartInfo[];
  alt(resolved: ResolvedSpec, step: number, locale: string): string;
  /** Graphs and charts: the plotted values as a table (LOC-16). */
  table?(resolved: ResolvedSpec): TableData;
  Component: (props: VisualProps) => ReactElement;
  Fallback: (props: { spec: unknown; reason: string }) => ReactElement;
  budget: { renderMs: number; maxParts: number };
}
