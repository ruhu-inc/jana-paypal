// The material pane of a recording, a video file or a YouTube video (STU-22; SPEC §20.4, W9).
// - A YouTube video (`video_link`): no player in this release (JM-D42). The transcript is the material: its chapters
//   (by code at create, or the chapters aid's) and every line, each with **Open at m:ss**, which opens the video at
//   that moment in the learner's browser through the app's external-link opener; **Watch on YouTube** opens its start;
//   **Ask about this moment** opens Your tutor with the lines around that moment as its pending context.
// - A video file (`video`): a `<video>` element where a recording has `<audio>`, with a filmstrip of the frames the
//   worker read as text (thumbnails drawn here from the local element at each frame's moment; no image is stored)
//   and the "on screen" text under each; chapters and the transcript are shared with a recording.
// - A recording: as before.
import { useEffect, useMemo, useRef, useState } from "react";
import { chunkLines, momentLabel, type StudyContracts as W } from "@jana/client-core";
import { citationKey, StudyCitations } from "./StudyCitation.js";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Switch } from "../primitives/index.js";

export type StudyFrame = { frame_id: string; at_ms: number; text: string };

/** The frames a video file's chunks carry (text read on screen, SPEC §20.3), in time order. */
export function framesOf(chunks: readonly W.Chunk[]): StudyFrame[] {
  return chunks.flatMap((c) => c.locator.kind === "frame" ? [{ frame_id: c.locator.frame_id, at_ms: c.locator.at_ms, text: c.text.replace(/^On screen at \S+:\s*/, "") }] : []).sort((a, b) => a.at_ms - b.at_ms);
}

type Chapter = { start_ms: number; end_ms: number; title: string; summary?: W.Claim[] };

export function StudyPlayer({ source, chunks, chapters, playhead, loadMedia, onSeek, onOpenMoment, onAskMoment, onCite, canMakeChapters, onMakeChapters, canOutline = false, outlinePending = false, onMakeOutline, busy }: {
  source: W.Source; chunks: W.Chunk[];
  /** The chapters aid's chapters (with cited summaries), else null: a YouTube video then shows its own chapters by code. */
  chapters: W.Chapter[] | null;
  playhead: { ms: number; seq: number } | null; loadMedia: () => Promise<Blob | null>;
  onSeek: (ms: number) => void;
  /** Open at m:ss (SPEC §20.4): the browser for a YouTube video, the player for a file. */
  onOpenMoment: (ms: number) => void;
  /** Ask about this moment: Your tutor opens with the lines around `ms` as its pending context (SPEC §20.5 step 4). */
  onAskMoment?: ((ms: number) => void) | undefined;
  onCite: (citation: W.Citation) => void; canMakeChapters: boolean; onMakeChapters: () => void;
  canOutline?: boolean; outlinePending?: boolean; onMakeOutline?: (() => void) | undefined; busy: boolean;
}) {
  const t = useT();
  const link = source.kind === "video_link";
  const video = source.kind === "video";
  const player = useRef<HTMLMediaElement>(null), transcript = useRef<HTMLOListElement>(null);
  const [url, setUrl] = useState<string | null>(null), [now, setNow] = useState(0), [follow, setFollow] = useState(true);
  const lines = useMemo(() => chunks.flatMap((c, i) => chunkLines(c).map((line, j) => ({ ...line, key: `${i}-${j}` }))), [chunks]);
  const frames = useMemo(() => video ? framesOf(chunks) : [], [chunks, video]);
  const shownChapters: Chapter[] | null = chapters ?? (link && source.chapters ? source.chapters : null);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  const at = (ms: number) => { setNow(ms); if (player.current) player.current.currentTime = ms / 1000; };
  useEffect(() => { if (playhead) at(playhead.ms); }, [playhead?.seq]);
  const current = (() => { let found = -1; lines.forEach((line, i) => { if (line.ms <= now) found = i; }); return found; })();
  useEffect(() => {
    if (!follow || current < 0) return;
    transcript.current?.querySelector<HTMLElement>(`[data-line="${current}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [current, follow]);
  const seek = (ms: number) => { onSeek(ms); at(ms); };
  const open = (ms: number) => { if (link) onOpenMoment(ms); else seek(ms); };
  const label = (ms: number) => momentLabel(ms);
  const openAt = (ms: number) => <Button size="xs" tone="reference" data-open-at={ms} aria-label={t("study.video.open_at_full", { time: label(ms) })} onClick={() => onOpenMoment(ms)}>{t("study.video.open_at", { time: label(ms) })}</Button>;
  const ask = (ms: number) => onAskMoment && <Button size="xs" tone="ghost" data-ask-moment={ms} aria-label={t("study.video.ask_moment_full", { time: label(ms) })} onClick={() => onAskMoment(ms)}><Icon name="ask" size={16} />{t("study.video.ask_moment")}</Button>;
  return <section className="flex flex-col gap-4" aria-label={t(link ? "study.video.title" : video ? "study.video.file_title" : "study.recording.title")}>
    {link && <div className="flex flex-wrap items-center gap-2">
      <Button tone="primary" data-watch-on-youtube="" onClick={() => onOpenMoment(0)}><Icon name="online" size={16} />{t("study.video.watch_on_youtube")}</Button>
      <span className="text-meta text-muted">{t("study.video.no_player")}</span>
    </div>}
    {!link && (url
      ? video
        ? <video ref={player as React.RefObject<HTMLVideoElement>} controls src={url} className="w-full rounded-card bg-black" onTimeUpdate={(e) => setNow(Math.round(e.currentTarget.currentTime * 1000))} aria-label={t("study.video.player")} />
        : <audio ref={player as React.RefObject<HTMLAudioElement>} controls src={url} className="w-full" onTimeUpdate={(e) => setNow(Math.round(e.currentTarget.currentTime * 1000))} aria-label={t("study.recording.player")} />
      : <span><Button size="sm" disabled={busy} onClick={() => { void loadMedia().then((blob) => { if (blob) setUrl(URL.createObjectURL(blob)); }); }}><Icon name="read-aloud" size={16} />{t(video ? "study.video.load" : "study.recording.load")}</Button></span>)}
    {!link && onAskMoment && <span>{ask(now)}</span>}
    {video && (source.frames ?? 0) > 0 && <Filmstrip url={url} frames={frames} screened={source.screened_frames ?? 0} noAudio={lines.length === 0} onPick={(ms) => seek(ms)} />}
    {onMakeOutline && <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" disabled={busy || !canOutline || outlinePending} data-make-outline="" onClick={onMakeOutline}><Icon name="learn" size={16} />{t("study.outline.make")}</Button>
      <span className="text-meta text-muted">{t(outlinePending ? "study.outline.pending" : "study.outline.help")}</span>
    </div>}
    <section className="flex flex-col gap-2" aria-labelledby="study-chapters">
      <h2 id="study-chapters" className="text-ui font-semibold text-ink">{t("study.recording.chapters")}</h2>
      {shownChapters === null ? <div className="flex flex-col items-start gap-2"><p className="text-meta text-muted">{t("study.recording.no_chapters")}</p><Button size="sm" disabled={busy || !canMakeChapters} onClick={onMakeChapters}>{t("study.generate.chapters")}</Button></div>
        : <ol className="flex flex-col gap-3">{shownChapters.map((chapter, i) => { const active = chapter.start_ms <= now && now < chapter.end_ms; return <li key={chapter.start_ms} className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" aria-current={active || undefined} onClick={() => open(chapter.start_ms)} className="jana-press flex min-h-9 min-w-0 flex-1 items-center rounded-control px-2 text-start text-ui font-medium text-ink hover:bg-wash aria-[current]:bg-wash">{label(chapter.start_ms)} · {chapter.title}</button>
            {link && openAt(chapter.start_ms)}
          </div>
          {chapter.summary?.map((claim, j) => <div key={j} className="flex flex-wrap items-baseline gap-2 ps-2"><p className="text-meta text-ink">{claim.text}</p><StudyCitations citations={claim.citations} locate={(ref) => chunks.find((chunk) => citationKey(chunk) === citationKey(ref))?.locator} title={() => source.title} disabled={busy} onOpen={onCite} /></div>)}
          <span className="sr-only">{t("study.recording.chapter_number", { number: i + 1 })}</span>
        </li>; })}</ol>}
    </section>
    <section className="flex flex-col gap-2" aria-labelledby="study-transcript">
      <div className="flex items-center gap-3"><h2 id="study-transcript" className="text-ui font-semibold text-ink">{t("study.recording.transcript")}</h2><span className="flex-1" />{!link && <Switch label={t("study.recording.auto_scroll")} checked={follow} onChange={setFollow} />}</div>
      {lines.length === 0 && video && <p className="text-meta text-muted">{t("study.video.no_audio")}</p>}
      {/* Each line is a column: the text row takes the pane's full width, the buttons sit on their own row under it
          (live-run fix 2026-11-01: beside the buttons the text was squeezed to a word or two a line). */}
      <ol ref={transcript} className="flex max-h-96 flex-col gap-1 overflow-y-auto">{lines.map((line, i) => <li key={line.key} data-line={i} className="flex flex-col gap-1">
        <button type="button" aria-current={i === current || undefined} onClick={() => open(line.ms)}
          className="flex w-full gap-3 rounded-control px-2 py-1 text-start hover:bg-wash aria-[current]:bg-highlight-soft">
          <span className="shrink-0 text-meta text-faint">{label(line.ms)}</span><span className="min-w-0 flex-1 text-body text-ink">{line.text}</span>
        </button>
        {link && <span data-line-actions="" className="flex flex-wrap items-center gap-1 ps-2">{openAt(line.ms)}{ask(line.ms)}</span>}
      </li>)}</ol>
    </section>
  </section>;
}

/** The frames of a video file (SPEC §20.1): a thumbnail drawn from the local element at each moment, the writing read
 * from it under it, and the count of frames the privacy pass refused. Selecting a frame seeks the player. */
function Filmstrip({ url, frames, screened, noAudio, onPick }: { url: string | null; frames: StudyFrame[]; screened: number; noAudio: boolean; onPick: (ms: number) => void }) {
  const t = useT();
  const [thumbs, setThumbs] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!url || frames.length === 0 || typeof document === "undefined") return;
    const hidden = document.createElement("video");
    hidden.muted = true; hidden.preload = "auto"; hidden.src = url;
    let cancelled = false;
    const drawn: Record<string, string> = {};
    const draw = async () => {
      for (const frame of frames) {
        if (cancelled) return;
        await new Promise<void>((resolve) => { const done = () => { hidden.removeEventListener("seeked", done); resolve(); }; hidden.addEventListener("seeked", done); hidden.currentTime = frame.at_ms / 1000; });
        const canvas = document.createElement("canvas");
        canvas.width = 160; canvas.height = Math.max(1, Math.round(160 * (hidden.videoHeight || 9) / (hidden.videoWidth || 16)));
        const context = canvas.getContext("2d");
        if (!context) return;
        try { context.drawImage(hidden, 0, 0, canvas.width, canvas.height); drawn[frame.frame_id] = canvas.toDataURL("image/png"); } catch { return; }
        if (!cancelled) setThumbs({ ...drawn });
      }
    };
    const start = () => { void draw(); };
    hidden.addEventListener("loadeddata", start, { once: true });
    return () => { cancelled = true; hidden.removeEventListener("loadeddata", start); hidden.removeAttribute("src"); hidden.load(); };
  }, [url, frames]);
  return <section className="flex flex-col gap-2" aria-labelledby="study-frames">
    <h2 id="study-frames" className="text-ui font-semibold text-ink">{t("study.video.frames")}</h2>
    <p className="text-meta text-muted">{t(noAudio ? "study.video.frames_only_help" : "study.video.frames_help")}{screened > 0 && <> {t("study.video.screened", { count: screened })}</>}</p>
    <ol className="flex gap-3 overflow-x-auto pb-2">{frames.map((frame) => <li key={frame.frame_id} className="flex w-44 shrink-0 flex-col gap-1">
      <button type="button" data-frame={frame.frame_id} onClick={() => onPick(frame.at_ms)} className="jana-press flex flex-col gap-1 rounded-card border border-line p-1 text-start hover:bg-wash">
        {thumbs[frame.frame_id] ? <img src={thumbs[frame.frame_id]} alt="" className="h-auto w-full rounded-control" /> : <span aria-hidden="true" className="flex h-24 w-full items-center justify-center rounded-control bg-panel text-meta text-faint">{momentLabel(frame.at_ms)}</span>}
        <span className="text-meta text-faint">{t("study.ref.frame", { time: momentLabel(frame.at_ms) })}</span>
      </button>
      <p className="line-clamp-4 whitespace-pre-wrap text-meta text-ink">{frame.text}</p>
    </li>)}</ol>
  </section>;
}
