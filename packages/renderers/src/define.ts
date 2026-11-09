// One renderer per kind (VSL-18), built the same way for every kind: the params check against the kind's schema
// (the specification's contract where it has one, the renderer's own otherwise), the resolved spec's part list
// (VSL-19), the generated alt text (VSL-14), the shared words fallback (VSL-16) and the render budget (VSL-33).
import type { ReactElement } from "react";
import { errors } from "@jana/contracts-ts/client";
import type { LabelMode, ResolvedSpec, TableData, VisualProps, VisualRenderer } from "./contract.js";
import { CLIENT_KEYS } from "./generated/keys.js";
import { WordsFallback, scene, specParts, type Scene } from "./kit.js";
import { schemaErrors, type Schema } from "./schema.js";

interface Definition {
  kind: string;
  /** A specification contract name (`visuals/kinds/<kind>`) or the renderer's own schema object. */
  schema: string | Schema;
  labelModes?: readonly LabelMode[];
  library?: boolean;
  draw(s: Scene, props: VisualProps): ReactElement;
  table?(resolved: ResolvedSpec): TableData;
}

export const SHOWN_HIDDEN: readonly LabelMode[] = ["shown", "hidden"];

export function define(d: Definition): VisualRenderer {
  const schemaName = typeof d.schema === "string" ? d.schema : `renderers/kinds/${d.kind}`;
  function Component(props: VisualProps): ReactElement {
    if (props.spec.status === "text_only") return WordsFallback({ spec: props.spec, reason: "text_only" });
    return d.draw(scene(props), props);
  }
  const renderer: VisualRenderer = {
    kind: d.kind,
    specVersion: 1,
    schema: schemaName,
    check: (params) => (typeof d.schema === "string" ? errors(d.schema, params) : schemaErrors(d.schema, params)),
    labelModes: d.labelModes ?? SHOWN_HIDDEN,
    parts: (resolved) => specParts(resolved),
    alt: (resolved, step) => (step >= 0 ? (resolved.alt.steps[step] ?? resolved.alt.summary) : resolved.alt.summary),
    Component,
    Fallback: WordsFallback,
    budget: { renderMs: CLIENT_KEYS.visual_first_render_ms, maxParts: d.library === true ? CLIENT_KEYS.visual_library_max_parts : CLIENT_KEYS.visual_template_max_parts },
  };
  if (d.table !== undefined) renderer.table = d.table;
  return renderer;
}
