// The app wires the vetted visual renderers (packages/renderers) into the UI's registry (UIA-09, VSL-18): the UI
// package never imports renderers (ARC-18), so this adapter is the only place they meet. Each kind is hosted by the
// visual viewer (UIA-20), which re-validates the spec, owns the step, label, table and part-panel controls, and hands
// the renderer its props.
import type { ReactElement } from "react";
import { VisualViewer, type RenderProps, type Registry, type VisualHooks, type VisualSpec } from "@jana/ui";
import { SketchView, type ResolvedSketch, type ResolvedSpec, type VisualRenderer } from "@jana/renderers";

export function hooksOf(renderer: VisualRenderer): VisualHooks {
  const R = renderer;
  const hooks: VisualHooks = {
    check: (params) => R.check(params),
    labelModes: R.labelModes,
    View: ({ spec, onSimEvent, ...props }) => (
      <R.Component spec={spec as unknown as ResolvedSpec} size={{ width: 480, height: 320 }} onSimEvent={(event) => onSimEvent({ ...event })} {...props} />
    ),
    Fallback: ({ spec, reason }) => <R.Fallback spec={spec} reason={reason} />,
  };
  if (R.table !== undefined) {
    const table = R.table;
    hooks.table = (spec) => table(spec as unknown as ResolvedSpec);
  }
  return hooks;
}

export function registerVisuals(registry: Registry, renderers: readonly VisualRenderer[]): Registry {
  for (const renderer of renderers) {
    const hooks = hooksOf(renderer);
    function View({ data }: RenderProps<ResolvedSpec>): ReactElement {
      return <VisualViewer spec={data as unknown as VisualSpec} hooks={hooks} />;
    }
    function Fallback({ data, reason }: { data: unknown; reason: string }): ReactElement {
      return <renderer.Fallback spec={data} reason={reason} />;
    }
    registry.register<ResolvedSpec>(`visual:${renderer.kind}`, { schema: "visuals/visual-spec#/$defs/resolved", Component: View, Fallback, renderClass: "card" });
  }
  // The online sketch (VSL-43): drawn by its vetted renderer from the engine-resolved form only; in words otherwise.
  registry.register<ResolvedSketch>("visual:sketch", {
    schema: "visuals/sketch#/$defs/resolved",
    Component: ({ data }) => <SketchView sketch={data as ResolvedSketch} />,
    Fallback: ({ data }) => <p className="text-ui">{(data as { alt?: string } | null)?.alt ?? ""}</p>,
    renderClass: "card",
  });
  return registry;
}
