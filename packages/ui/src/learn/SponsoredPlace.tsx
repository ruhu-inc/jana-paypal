import { useEffect, useRef, useState } from "react";
import type { StudyWorkspaceController } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";

export function SponsoredPlace({ controller, source, revision }: {
  controller: StudyWorkspaceController; source: string; revision: number;
}) {
  const t = useT();
  const [place, setPlace] = useState<{ state: string; deadline: number | null } | null>(null);
  const [busy, setBusy] = useState(false), [failed, setFailed] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      const before = generation.current;
      try {
        const next = await controller.sponsoredPlace?.(source, revision);
        if (!alive || !next) return;
        if (before === generation.current) setPlace(next);
        if (["available", "claimed", "committed", "unavailable"].includes(next.state)) timer = setTimeout(refresh, 10000);
      } catch { /* Non-enrolled profiles have no workshop controls. */ }
    }
    void refresh();
    return () => { alive = false; generation.current++; clearTimeout(timer); };
  }, [controller, source, revision]);
  if (!place || !["available", "claimed", "committed", "funded", "applied", "payment_review", "ended", "excluded"].includes(place.state)) return null;
  async function act(action: "claim" | "withdraw" | "activate") {
    const current = ++generation.current;
    setBusy(true); setFailed(false);
    try {
      const next = await controller.sponsoredPlace(source, revision, action);
      if (generation.current !== current) return;
      setPlace(next);
      if (action === "activate") await controller.loadAccess();
    } catch { if (generation.current === current) setFailed(true); }
    finally { if (generation.current === current) setBusy(false); }
  }
  const action = place.state === "available" ? "claim" : place.state === "claimed" ? "withdraw" : place.state === "funded" ? "activate" : null;
  return <section className="flex flex-col gap-2 rounded-control bg-chrome p-3" aria-label={t("study.sponsor.title")}>
    <p role="status">{t(`study.sponsor.${place.state}`)}</p>
    {place.deadline && ["claimed", "available"].includes(place.state) && <p>{t("study.sponsor.closes", { time: new Date(place.deadline * 1000).toLocaleTimeString() })}</p>}
    {action && <div><Button disabled={busy} onClick={() => void act(action)}>{t(`study.sponsor.${action}`)}</Button></div>}
    {failed && <p role="alert">{t("study.sponsor.retry")}</p>}
  </section>;
}
