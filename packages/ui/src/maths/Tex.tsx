import { memo } from "react";
import { useT } from "../i18n.js";
import { renderTex } from "./katex.js";

/** One maths expression. KaTeX's output is library HTML built from TeX with trust restricted (UIA-15); a source that
 * does not parse shows as muted code with keyed copy, never an exception. */
export const Tex = memo(function Tex({ tex, display = false }: { tex: string; display?: boolean }) {
  const t = useT();
  const out = renderTex(tex, display);
  if ("error" in out) {
    return (
      <span className="text-muted">
        <code className="font-mono text-meta">{tex}</code> <span className="sr-only">{t("maths.unavailable")}</span>
      </span>
    );
  }
  const Tag = display ? "div" : "span";
  // Library output from TeX, trust restricted to allow-listed classes (UIA-15).
  return <Tag className={display ? "my-2 overflow-x-auto" : undefined} dangerouslySetInnerHTML={{ __html: out.html }} />;
});
