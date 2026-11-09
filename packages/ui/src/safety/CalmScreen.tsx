// The calm screen (UXS-43, PRV-14): when the engine answers with the distress template it sends a `calm_screen` notice,
// and the tutor workspace is covered by this screen on `paper` with no board, timer, streak, progress, sound or motion.
// In order: the template's text exactly as the engine wrote it; "Talk to someone you trust"; the verified helplines of
// the safety-resources pack, each with its name, number as selectable text and hours (none, and no helpline line, when
// none is verified); and one action, "Back to my lesson when I'm ready", which uncovers the workspace exactly as it was.
// It asks nothing, offers no rating or report, and never names what was detected.
import { useEffect, useId, useRef } from "react";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";

/** One verified helpline of the safety-resources pack (content/safety-resources): only verified entries reach here. */
export interface Helpline {
  name: string;
  number: string;
  hours: string | null;
}

export function CalmScreen({ text, helplines, onBack }: { text: string; helplines: readonly Helpline[]; onBack: () => void }) {
  const t = useT();
  const id = useId();
  const words = useRef<HTMLParagraphElement>(null);
  useEffect(() => words.current?.focus(), []);
  return (
    // Instant changes whatever the motion setting: everything inside reads as reduced motion (DSN-09).
    <section aria-labelledby={id} data-calm-screen="" data-reduced-motion="true" className="flex h-full min-h-0 flex-col items-center overflow-y-auto bg-paper px-6 py-16 text-ink">
      <div className="flex w-full max-w-150 flex-col gap-8">
        <p ref={words} tabIndex={-1} className="text-body whitespace-pre-line">{text}</p>
        <div className="flex flex-col gap-4">
          <h1 id={id} className="text-title">{t("calm.heading")}</h1>
          {helplines.length > 0 && (
            <ul className="flex flex-col gap-3" data-helplines="">
              {helplines.map((line) => (
                <li key={`${line.name}:${line.number}`} className="flex flex-col gap-1 rounded-card border border-line bg-canvas px-5 py-4">
                  <span className="text-ui font-semibold">{line.name}</span>
                  <span className="font-mono text-heading tabular-nums select-all">{line.number}</span>
                  {line.hours !== null && <span className="text-meta text-muted">{line.hours}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <Button tone="primary" onClick={onBack}>{t("calm.back")}</Button>
        </div>
      </div>
    </section>
  );
}
