// The diagram drill (JM-D10, SPEC §5.2): the student's own photo of a labelled diagram, with each part Muse Spark named
// and SAM 3.1 outlined. "Learn the parts" shows the outlines and names a part when it is tapped; "Practise" covers the
// printed labels and asks "Tap the …", and each tap is marked by code against the part's outline (`markTap`), never by
// a model. Two misses show where the part is. Outlines may still be arriving from SAM when the drill opens, so the
// capture is fetched again for a short while until every part has one (a part without one uses its box meanwhile).

import { useEffect, useMemo, useRef, useState } from "react";
import { drillOrder, drillParts, markTap, MIN_PARTS, partAt, PhotoCommands, type CapturedQuestion, type DrillPart, type DrillPoint, type TapMark } from "@jana/client-core";
import { useSession } from "../store.js";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";
import { useOpenResearch } from "../learn/link.js";

const REFETCH_MS = 1500, REFETCHES = 8, TRIES = 2;
/** Each printed label is covered with this margin (a share of the photo), so no stroke of a letter shows. */
const COVER_PAD = 0.01;

type Feedback = { kind: "correct" | "other_part" | "missed" | "revealed"; name: string | null } | null;

function points(outline: readonly DrillPoint[]): string { return outline.map((p) => `${p.x},${p.y}`).join(" "); }
function centre(outline: readonly DrillPoint[]): DrillPoint {
  const n = outline.length;
  return { x: outline.reduce((s, p) => s + p.x, 0) / n, y: outline.reduce((s, p) => s + p.y, 0) / n };
}

export function DiagramDrill({ sessionId, capture: initial, onClose }: { sessionId: string; capture: CapturedQuestion; onClose: () => void }) {
  const session = useSession(), t = useT(), openResearch = useOpenResearch();
  const [capture, setCapture] = useState(initial);
  const [url, setUrl] = useState<string | null>(null);
  const [mode, setMode] = useState<"learn" | "drill" | "done">("learn");
  const [seed, setSeed] = useState(() => Date.now() % 100000);
  const [index, setIndex] = useState(0);
  const [tries, setTries] = useState(0);
  const [found, setFound] = useState(0);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [lit, setLit] = useState<string | null>(null);
  const picture = useRef<HTMLDivElement>(null);
  const parts = useMemo(() => drillParts(capture), [capture]);
  const order = useMemo(() => drillOrder(parts, seed), [parts, seed]);
  const asked = mode === "drill" ? order[index] ?? null : null;
  const settled = feedback?.kind === "correct" || feedback?.kind === "revealed";

  useEffect(() => {
    let alive = true, objectUrl: string | null = null, timer: ReturnType<typeof setTimeout> | null = null;
    if (session === null) return;
    const api = new PhotoCommands(session.commands, sessionId);
    const photo = initial.attachments.find((a) => a.kind === "photo");
    if (photo !== undefined) {
      void api.image(initial.capture_id, photo.image_id).then((blob) => {
        if (!alive) return;
        objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
      }).catch(() => undefined);
    }
    const waiting = (record: CapturedQuestion) => record.regions.some((r) => r.kind === "diagram" && r.bbox != null && (r.outline == null || r.outline.length < 3));
    let left = REFETCHES;
    const refetch = (record: CapturedQuestion) => {
      if (!waiting(record) || left-- <= 0) return;
      timer = setTimeout(() => {
        void api.get(initial.capture_id).then((next) => { if (alive) { setCapture(next); refetch(next); } }).catch(() => undefined);
      }, REFETCH_MS);
    };
    refetch(initial);
    return () => { alive = false; if (timer !== null) clearTimeout(timer); if (objectUrl !== null) URL.revokeObjectURL(objectUrl); };
  }, [session, sessionId, initial]);

  const start = (next: number) => { setSeed(next); setMode("drill"); setIndex(0); setTries(0); setFound(0); setFeedback(null); setLit(null); };
  const next = () => {
    setFeedback(null); setLit(null); setTries(0);
    if (index + 1 >= order.length) setMode("done"); else setIndex(index + 1);
  };
  const tap = (point: DrillPoint) => {
    if (mode === "learn") {
      const part = partAt(parts, point);
      setLit(part?.regionId ?? null); setFeedback(part === null ? null : { kind: "correct", name: part.name });
      return;
    }
    if (asked === null || settled) return;
    const mark: TapMark = markTap(parts, asked, point);
    if (mark.outcome === "correct") {
      if (tries === 0) setFound(found + 1);
      setLit(asked.regionId); setFeedback({ kind: "correct", name: asked.name });
      return;
    }
    const missed = tries + 1;
    setTries(missed);
    if (missed >= TRIES) { setLit(asked.regionId); setFeedback({ kind: "revealed", name: asked.name }); return; }
    setLit(mark.outcome === "other_part" ? mark.part.regionId : null);
    setFeedback(mark.outcome === "other_part" ? { kind: "other_part", name: mark.part.name } : { kind: "missed", name: null });
  };
  const onPicture = (event: React.MouseEvent<HTMLDivElement>) => {
    const rect = picture.current?.getBoundingClientRect();
    if (rect === undefined || rect.width === 0 || rect.height === 0) return;
    tap({ x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height });
  };
  const choose = (part: DrillPart) => (event: React.KeyboardEvent) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault(); event.stopPropagation(); tap(centre(part.outline));
  };

  const labels = capture.regions.flatMap((r) => (r.label_box == null ? [] : [{ id: r.region_id, box: r.label_box }]));
  const message = feedback === null ? null
    : mode === "learn" ? t("diagram.this_is", { name: feedback.name ?? "" })
    : feedback.kind === "missed" ? t("diagram.missed")
    : t(`diagram.${feedback.kind}`, { name: feedback.name ?? "" });

  return <section aria-label={t("diagram.title")} data-diagram-drill={capture.capture_id} className="my-2 rounded-card border border-line bg-panel p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="font-semibold">{t("diagram.title")}</p>
      <div className="flex flex-wrap gap-2">
        {mode !== "learn" && <Button onClick={() => { setMode("learn"); setFeedback(null); setLit(null); }}>{t("diagram.learn")}</Button>}
        {mode === "learn" && parts.length >= MIN_PARTS && <Button onClick={() => start(seed)}>{t("diagram.practise")}</Button>}
        {openResearch !== null && parts.length >= MIN_PARTS && <Button onClick={() => openResearch(capture.capture_id)}>{t("diagram.study_pack")}</Button>}
        <Button onClick={onClose}>{t("action.close")}</Button>
      </div>
    </div>
    {parts.length < MIN_PARTS ? <p role="status" className="mt-2">{t("diagram.too_few")}</p> : <>
      <p className="mt-1 text-meta">{mode === "learn" ? t("diagram.learn_hint") : t("diagram.checked")}</p>
      {mode === "drill" && asked !== null && <p className="mt-2 font-semibold" data-diagram-ask={asked.regionId}>
        <span className="text-meta">{t("diagram.progress", { index: index + 1, total: order.length })} · </span>{t("diagram.ask", { name: asked.name })}
      </p>}
      {mode === "done" && <p role="status" className="mt-2 font-semibold">{t("diagram.done", { found, total: order.length })}</p>}
      {url !== null && mode !== "done" && <div ref={picture} className="relative mt-2 max-w-xl cursor-crosshair select-none" onClick={onPicture}>
        <img src={url} alt={t("diagram.your_photo")} className="block w-full" draggable={false} />
        <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="absolute inset-0 size-full">
          {mode === "drill" && labels.map((label) => <rect key={label.id} data-diagram-label={label.id} x={Math.max(0, label.box.x - COVER_PAD)} y={Math.max(0, label.box.y - COVER_PAD)}
            width={label.box.w + 2 * COVER_PAD} height={label.box.h + 2 * COVER_PAD} className="fill-panel" />)}
          {parts.map((part, n) => {
            const on = lit === part.regionId;
            const shown = mode === "learn" || on;
            return <polygon key={part.regionId} data-diagram-part={part.regionId} data-lit={on ? "" : undefined} role="button" tabIndex={0}
              aria-label={mode === "learn" ? part.name : t("diagram.part", { index: n + 1 })} onKeyDown={choose(part)}
              points={points(part.outline)} vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeWidth={on ? 3 : 1}
              className={`outline-none focus:stroke-focus ${on ? "fill-focus/15 stroke-focus" : shown ? "fill-transparent stroke-line-strong" : "fill-transparent stroke-transparent"}`} />;
          })}
        </svg>
      </div>}
      <p aria-live="polite" className="mt-2 min-h-6">{message}</p>
      <div className="flex flex-wrap gap-2">
        {mode === "drill" && settled && <Button onClick={next}>{t("diagram.next")}</Button>}
        {mode === "done" && <Button onClick={() => start((seed * 31 + 7) % 100000)}>{t("diagram.again")}</Button>}
      </div>
      {mode === "learn" && <ul aria-label={t("diagram.parts")} className="mt-2 flex flex-wrap gap-2">
        {parts.map((part) => <li key={part.regionId}><Button onClick={() => { setLit(part.regionId); setFeedback({ kind: "correct", name: part.name }); }}>{part.name}</Button></li>)}
      </ul>}
    </>}
  </section>;
}
