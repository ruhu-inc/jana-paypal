// The student's own photo on the board, with the places Jana points at outlined on it (JM-D10). Muse Spark read the
// lines of working or the parts of a sketch; SAM 3.1 outlined each one; the engine lights the line that went wrong
// (`board.highlight` with `photo_regions`) or a spoken cue points at it (`photo_region`). Outlines are drawn from the
// capture record's 0..1 polygons over the same screened image the reader saw; a region without an outline falls back
// to its box. Each outline carries `data-photo-region` so the pen and the voice pointer can find it (VSL-45).

import { Fragment, useEffect, useState } from "react";
import { PhotoCommands, type CapturedQuestion } from "@jana/client-core";
import { useSession } from "../store.js";
import { useT } from "../i18n.js";
import { StudyInline } from "../learn/StudyText.js";

type Region = CapturedQuestion["regions"][number];

/** The region's outline as SVG points on a 0..1 square, or its box when SAM made no outline; null without either. */
export function regionPoints(region: Region): string | null {
  const outline = region.outline ?? null;
  if (outline !== null && outline.length >= 3) return outline.map((p) => `${p.x},${p.y}`).join(" ");
  const box = region.bbox ?? null;
  if (box === null) return null;
  return [[box.x, box.y], [box.x + box.w, box.y], [box.x + box.w, box.y + box.h], [box.x, box.y + box.h]].map(([x, y]) => `${x},${y}`).join(" ");
}

export function PhotoRegions({ sessionId, captureId, lit }: { sessionId: string; captureId: string; lit: readonly string[] }) {
  const session = useSession(), t = useT();
  const [view, setView] = useState<{ capture: CapturedQuestion; url: string } | null>(null);
  useEffect(() => {
    let alive = true; let url: string | null = null;
    setView(null);
    if (session === null) return;
    const api = new PhotoCommands(session.commands, sessionId);
    void (async () => {
      try {
        const capture = await api.get(captureId);
        const photo = capture.attachments.find((a) => a.kind === "photo");
        if (photo === undefined || !alive) return;
        const blob = await api.image(captureId, photo.image_id);
        if (!alive) return;
        url = URL.createObjectURL(blob);
        setView({ capture, url });
      } catch { /* the photo is gone (deleted, expired): the board line still shows the place */ }
    })();
    return () => { alive = false; if (url !== null) URL.revokeObjectURL(url); };
  }, [session, sessionId, captureId]);
  if (view === null) return null;
  const shown = view.capture.regions.filter((region) => regionPoints(region) !== null);
  const litRegions = shown.filter((region) => lit.includes(`${captureId}:${region.region_id}`));
  // A line read from a photo can hold maths, so the words go around it and it renders as maths.
  const [before, after = ""] = t("photo.showing_region", { text: "\u0000" }).split("\u0000");
  return <figure data-photo-capture={captureId} className="rounded-card border border-line bg-panel p-2">
    <div className="relative">
      <img src={view.url} alt={t("photo.your_photo")} className="block w-full" />
      <svg aria-hidden="true" viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full">
        {shown.map((region) => {
          const on = litRegions.includes(region);
          return <polygon key={region.region_id} data-photo-region={`${captureId}:${region.region_id}`} data-lit={on ? "" : undefined}
            points={regionPoints(region) ?? ""} vectorEffect="non-scaling-stroke" strokeLinejoin="round"
            className={on ? "fill-focus/15 stroke-focus" : "fill-none stroke-line-strong"} strokeWidth={on ? 3 : 1} />;
        })}
      </svg>
    </div>
    {/* The place in words for a screen reader, and for anyone who cannot see the outline. */}
    <figcaption className="mt-1 text-meta" aria-live="polite">
      {litRegions.length > 0 ? <>{before}{litRegions.map((r, i) => <Fragment key={r.region_id}>{i > 0 && "; "}<StudyInline text={r.text} /></Fragment>)}{after}</> : t("photo.your_photo")}
    </figcaption>
  </figure>;
}
