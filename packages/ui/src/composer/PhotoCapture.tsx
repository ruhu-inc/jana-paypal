import { EntryEditor, type EntryHandle } from "./EntryEditor.js";
import { useEffect, useRef, useState } from "react";
import { captureRegionText, PhotoCommands, checkPhotoHeader, type CapturedQuestion, type CapturePurpose, type CaptureSource, type Corner } from "@jana/client-core";
import { useT } from "../i18n.js";
import { useSession } from "../store.js";
import { Button, ErrorState } from "../primitives/index.js";
import { StudyInline } from "../learn/StudyText.js";
const QUAD: Corner[] = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }];
export type CapturePreviewStage = "camera-live" | "camera-still" | "camera-light" | "camera-missing" | "camera-blocked" | "camera-busy" | "corner-focused";
interface Props { /** A static gallery fixture may represent processing already in progress. */ previewProcessingElapsed?: boolean; previewStage?: CapturePreviewStage; previewCapture?: CapturedQuestion; previewImage?: string; sessionId: string; purpose: CapturePurpose; file?: Blob; source?: CaptureSource; onConfirmed: (capture: CapturedQuestion) => void; onTyped: (text: string) => void; onDiscardSource?: () => void; onClose: () => void }

export function PhotoCapture({ previewProcessingElapsed = false, previewStage, previewCapture, previewImage, sessionId, purpose, file, source = "photo_file", onConfirmed, onTyped, onDiscardSource, onClose }: Props) {
  const t = useT();
  const session = useSession();
  const commands = useRef<PhotoCommands | null>(null);
  const alive = useRef(true);
  const record = useRef<CapturedQuestion | null>(previewCapture ?? null);
  const stream = useRef<MediaStream | null>(null);
  const cameraRequest = useRef(0);
  const photoRequest = useRef(0);
  const video = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const url = useRef<string | null>(null);
  const transcript = useRef<EntryHandle>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [cloud, setCloud] = useState(false);
  const [slow, setSlow] = useState(false);
  const preview = useRef<HTMLDivElement>(null);
  const [photo, setPhoto] = useState<Blob | null>(null);
  const [photoSource, setPhotoSource] = useState<CaptureSource>(source);
  const [image, setImage] = useState<string | null>(previewImage ?? null);
  const [capture, setCapture] = useState<CapturedQuestion | null>(previewCapture ?? null);
  const [corners, setCorners] = useState<Corner[]>(previewCapture?.outline ?? QUAD.map((p) => ({ ...p })));
  const [rotation, setRotation] = useState(0);
  const [text, setText] = useState(previewCapture?.text ?? "");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(previewStage?.startsWith("camera-") && !["camera-live", "camera-still", "camera-light"].includes(previewStage) ? previewStage.replace("-", "_") : null);
  const [busy, setBusy] = useState(previewCapture?.status === "reading" || previewCapture?.status === "preprocessing");
  const [cameraHint, setCameraHint] = useState<string | null>(previewStage === "camera-still" ? "photo.camera_still" : previewStage === "camera-light" ? "photo.camera_light" : null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [camera, setCamera] = useState(previewStage !== undefined && ["camera-live", "camera-still", "camera-light"].includes(previewStage));
  const [processingVisible, setProcessingVisible] = useState(previewProcessingElapsed);
  useEffect(() => { if (!busy) { setProcessingVisible(false); return; } const timer = setTimeout(() => setProcessingVisible(true), 300); return () => clearTimeout(timer); }, [busy]);
  useEffect(() => { if (previewStage === "corner-focused") preview.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, [previewStage]);
  const [override, setOverride] = useState(false);
  const stopCamera = () => { cameraRequest.current += 1; stream.current?.getTracks().forEach((track) => track.stop()); stream.current = null; if (video.current !== null) video.current.srcObject = null; };
  const show = (blob: Blob) => { if (url.current !== null) URL.revokeObjectURL(url.current); url.current = URL.createObjectURL(blob); setImage(url.current); };
  const update = (value: CapturedQuestion) => { record.current = value; setCapture(value); };
  useEffect(() => {
    alive.current = true;
    commands.current = session === null ? null : new PhotoCommands(session.commands, sessionId);
    const hide = () => { stopCamera(); setCamera(false); };
    const hidden = () => { if (document.visibilityState === "hidden") hide(); };
    window.addEventListener("blur", hide);
    window.addEventListener("pagehide", hide);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      alive.current = false;
      window.removeEventListener("blur", hide);
      window.removeEventListener("pagehide", hide);
      document.removeEventListener("visibilitychange", hidden);
      stopCamera();
      if (url.current !== null) URL.revokeObjectURL(url.current);
      const current = record.current;
      if (current !== null && current.status !== "confirmed") void commands.current?.delete(current.capture_id).catch(() => undefined);
    };
  }, [session, sessionId]);
  useEffect(() => { if (file !== undefined) void choose(file, source); }, [file, source]);
  useEffect(() => {
    const live = video.current;
    if (camera && live !== null) live.srcObject = stream.current;
    return () => { if (live !== null) live.srcObject = null; };
  }, [camera]);
  useEffect(() => {
    if (capture?.status !== "reading") return;
    let stopped = false;
    const timer = setInterval(() => { void (async () => {
      try {
        const value = await commands.current?.get(capture.capture_id);
        if (value === undefined || stopped || !alive.current) return;
        update(value);
        if (value.status === "refused" || (value.status !== "reading" && value.attachments.length === 0)) {
          if (url.current !== null) URL.revokeObjectURL(url.current);
          url.current = null; setImage(null); setPhoto(null); onDiscardSource?.();
        }
        if (value.status !== "reading") { setText(value.text); setBusy(false); }
      } catch { if (!stopped && alive.current) { setError("photo_reading_unavailable"); setBusy(false); } }
    })(); }, 500);
    return () => { stopped = true; clearInterval(timer); };
  }, [capture?.capture_id, capture?.status]);
  useEffect(() => {
    if (!busy) { setSlow(false); return; }
    const timer = setTimeout(() => setSlow(true), capture?.reader.hardware_class === "high" ? 6000 : capture?.reader.hardware_class === "low" ? 15000 : 12000);
    return () => clearTimeout(timer);
  }, [busy]);
  useEffect(() => {
    if (!camera) return;
    const canvas = document.createElement("canvas"); canvas.width = 160; canvas.height = 120;
    const timer = setInterval(() => {
      const live = video.current; const context = canvas.getContext("2d", { willReadFrequently: true });
      if (live === null || live.videoWidth === 0 || context === null) return;
      context.drawImage(live, 0, 0, 160, 120);
      const pixels = context.getImageData(0, 0, 160, 120).data;
      let brightness = 0; let edges = 0;
      for (let i = 0; i < pixels.length; i += 4) { brightness += (pixels[i] ?? 0); if (i >= 4) edges += Math.abs((pixels[i] ?? 0) - (pixels[i - 4] ?? 0)); }
      brightness /= pixels.length / 4; edges /= pixels.length / 4;
      setCameraHint(brightness < 70 ? "photo.camera_light" : edges < 3 ? "photo.camera_still" : null);
    }, 500);
    return () => clearInterval(timer);
  }, [camera]);
  const choose = async (blob: Blob, incoming: CaptureSource) => {
    const request = ++photoRequest.current;
    if (blob.size > 20 * 1024 * 1024) { setError("image_too_large"); return; }
    if (!["image/jpeg", "image/png", "image/webp"].includes(blob.type)) {
      if (!["image/heic", "image/heif"].includes(blob.type)) { setError("unsupported_format"); return; }
      try {
        const bitmap = await createImageBitmap(blob);
        if (Math.max(bitmap.width, bitmap.height) > 8000) { bitmap.close(); setError("image_too_large"); return; }
        const canvas = document.createElement("canvas"); const ratio = Math.min(1, 2048 / Math.max(bitmap.width, bitmap.height));
        canvas.width = Math.round(bitmap.width * ratio); canvas.height = Math.round(bitmap.height * ratio);
        canvas.getContext("2d")?.drawImage(bitmap, 0, 0, canvas.width, canvas.height); bitmap.close();
        const converted = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
        if (converted === null) { setError("unsupported_format"); return; }
        blob = converted;
      } catch { setError("unsupported_format"); return; }
    }
    try { await checkPhotoHeader(blob); } catch (err) { setError(err instanceof Error ? err.message : "unsupported_format"); return; }
    if (!alive.current || request !== photoRequest.current) return;
    if (record.current !== null) await commands.current?.delete(record.current.capture_id).catch(() => undefined);
    if (!alive.current || request !== photoRequest.current) return;
    stopCamera(); setCamera(false); setOverride(false); setPhotoSource(incoming); setPhoto(blob);
    show(blob); onDiscardSource?.();
    setCapture(null); record.current = null; setText(""); setSelected(null); setError(null); setRotation(0); setCorners(QUAD.map((p) => ({ ...p })));
    const api = commands.current;
    if (api === null) return;
    setBusy(true);
    try {
      const created = await api.create(blob, incoming, purpose);
      if (!alive.current || request !== photoRequest.current) { await api.delete(created.capture_id); return; }
      update(created);
      const outlined = await api.crop(created.capture_id, QUAD, 0, true);
      if (alive.current && request === photoRequest.current) { update(outlined); setCorners(outlined.outline ?? QUAD.map((p) => ({ ...p }))); }
    } catch (err) { if (alive.current && request === photoRequest.current) setError(err instanceof Error ? err.message : "photo_reading_unavailable"); }
    finally { if (alive.current && request === photoRequest.current) setBusy(false); }
  };
  const backToCrop = async () => { if (photo !== null && !busy) await choose(photo, photoSource); };
  const run = async (acceptWarnings = false) => {
    const warningsAccepted = override || acceptWarnings;
    if (acceptWarnings) setOverride(true);
    if (commands.current === null || photo === null || busy) return;
    setBusy(true); setError(null);
    try {
      const created = record.current ?? await commands.current.create(photo, photoSource, purpose);
      // Clean up a create response that arrives after cancel/profile close.
      if (!alive.current) { await commands.current.delete(created.capture_id); return; }
      update(created);
      const cropped = created.status === "quality_failed" ? created : await commands.current.crop(created.capture_id, corners, rotation);
      if (!alive.current) return;
      update(cropped);
      if (cropped.status === "refused") {
        if (url.current !== null) URL.revokeObjectURL(url.current);
        url.current = null; setImage(null); setPhoto(null); onDiscardSource?.(); setBusy(false); return;
      }
      const attachment = cropped.attachments[0];
      if (attachment !== undefined) { const derivative = await commands.current.image(cropped.capture_id, attachment.image_id); if (alive.current) show(derivative); }
      if (cropped.quality.checks.some((c) => c.outcome === "blocking") || (cropped.quality.checks.some((c) => c.outcome === "advisory") && !warningsAccepted)) { setBusy(false); return; }
      await commands.current.read(cropped.capture_id, warningsAccepted, cloud);
      if (alive.current) update({ ...cropped, status: "reading" });
    } catch (err) { if (alive.current) { setError(err instanceof Error ? err.message : "photo_reading_unavailable"); setBusy(false); } }
  };
  const startCamera = async (chosen = "") => {
    setError(null); stopCamera(); setCamera(false);
    const request = ++cameraRequest.current;
    try {
      if (navigator.mediaDevices?.getUserMedia === undefined) { setError("camera_missing"); return; }
      const feed = await navigator.mediaDevices.getUserMedia({ video: chosen === "" ? { facingMode: "environment" } : { deviceId: { exact: chosen } }, audio: false });
      if (!alive.current || request !== cameraRequest.current) { feed.getTracks().forEach((track) => track.stop()); return; }
      stopCamera(); stream.current = feed; setDeviceId(chosen); setCamera(true);
      if (navigator.mediaDevices.enumerateDevices !== undefined) setDevices((await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput"));
    } catch (err) { if (!alive.current || request !== cameraRequest.current) return; const name = typeof err === "object" && err !== null && "name" in err && typeof err.name === "string" ? err.name : ""; setError(["NotAllowedError", "SecurityError"].includes(name) ? "camera_blocked" : name === "NotFoundError" ? "camera_missing" : name === "NotReadableError" ? "camera_busy" : "camera_unavailable"); }
  };
  const snap = () => {
    const live = video.current;
    if (live === null || live.videoWidth === 0) return;
    const canvas = document.createElement("canvas");
    const ratio = Math.min(1, 2048 / Math.max(live.videoWidth, live.videoHeight));
    canvas.width = Math.round(live.videoWidth * ratio); canvas.height = Math.round(live.videoHeight * ratio);
    canvas.getContext("2d")?.drawImage(live, 0, 0, canvas.width, canvas.height);
    stopCamera(); setCamera(false);
    canvas.toBlob((blob) => { if (blob !== null && alive.current) void choose(blob, "webcam"); }, "image/jpeg", 0.9);
  };
  const rotate = async () => {
    if (photo === null) return;
    const next = (rotation + 90) % 360;
    const bitmap = await createImageBitmap(photo);
    const canvas = document.createElement("canvas");
    canvas.width = next % 180 === 0 ? bitmap.width : bitmap.height; canvas.height = next % 180 === 0 ? bitmap.height : bitmap.width;
    const context = canvas.getContext("2d");
    if (context !== null) { context.translate(canvas.width / 2, canvas.height / 2); context.rotate(next * Math.PI / 180); context.drawImage(bitmap, -bitmap.width / 2, -bitmap.height / 2); }
    bitmap.close(); setRotation(next); setCorners(QUAD.map((p) => ({ ...p })));
    canvas.toBlob((blob) => { if (blob !== null && alive.current) show(blob); }, "image/jpeg");
    const api = commands.current; const current = record.current;
    if (api !== null && current !== null) {
      setBusy(true);
      try { const outlined = await api.crop(current.capture_id, QUAD, next, true); if (alive.current) { update(outlined); setCorners(outlined.outline ?? QUAD.map((p) => ({ ...p }))); } }
      finally { if (alive.current) setBusy(false); }
    }
  };
  const confirm = async () => {
    if (commands.current === null || capture === null) return;
    setBusy(true);
    // A diagram's parts are named one per line: each edited line renames its part, when the lines still match them.
    const lines = text.split("\n").map((line) => line.trim()).filter((line) => line !== "");
    const named = purpose === "diagram" && lines.length === capture.regions.length ? capture.regions.map((region, i) => ({ ...region, text: lines[i] ?? region.text })) : undefined;
    try { const value = await commands.current.confirm(capture.capture_id, text, selected, named); if (alive.current) { update(value); onConfirmed(value); onClose(); } }
    catch (err) { if (alive.current) { setError(err instanceof Error ? err.message : "photo_reading_unavailable"); setBusy(false); } }
  };
  const confirming = capture?.status === "awaiting_confirmation";
  const unreadable = capture?.status === "unreadable";
  return <div role="dialog" aria-modal="false" aria-label={t(purpose === "working" ? "photo.working_action" : purpose === "sketch" ? "photo.sketch_action" : purpose === "diagram" ? "photo.diagram_action" : "photo.title")} className="my-2 rounded-card border border-line bg-panel p-3" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }} onPaste={(event) => {
    const incoming = event.clipboardData.files[0]; if (incoming !== undefined && incoming.type.startsWith("image/")) { event.preventDefault(); void choose(incoming, "photo_paste"); }
  }} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const incoming = event.dataTransfer.files[0]; if (incoming !== undefined) void choose(incoming, "photo_drop"); }}>
    <p className="font-semibold">{t(confirming ? purpose === "working" ? "photo.working_title" : purpose === "sketch" ? "photo.sketch_title" : purpose === "diagram" ? "photo.diagram_title" : "photo.confirm_title" : purpose === "working" ? "photo.working_action" : purpose === "sketch" ? "photo.sketch_action" : purpose === "diagram" ? "photo.diagram_action" : "photo.title")}</p>
    <p className="text-meta">{t(purpose === "diagram" ? confirming ? "photo.diagram_confirm_instructions" : "photo.diagram_instructions" : confirming ? "photo.confirm_instructions" : "photo.instructions")}</p>
    {capture?.injection.flagged && <p role="status">{t("photo.injection_removed")}</p>}
    {capture?.status === "confirmed" && <p role="status">{t("photo.confirmed")}</p>}
    {capture?.status === "cancelled" && <p role="status">{t("photo.cancelled")}</p>}
    <input ref={fileInput} hidden type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" onChange={(event) => { const incoming = event.target.files?.[0]; if (incoming !== undefined) void choose(incoming, "photo_file"); event.target.value = ""; }} />
    <div className="flex gap-2"><Button disabled={busy} onClick={() => fileInput.current?.click()}>{t(unreadable ? "photo.try_another" : "photo.choose")}</Button><Button disabled={busy} onClick={() => { void startCamera(); }}>{t("photo.camera")}</Button><Button onClick={onClose}>{t("action.cancel")}</Button></div>
    {image !== null && <Button disabled={busy} onClick={() => { if (photo !== null) void backToCrop(); else onClose(); }}>{t("action.back")}</Button>}
    {camera && <div className="relative">
      <video ref={video} autoPlay playsInline muted tabIndex={0} aria-label={t("photo.camera_preview")} className="max-h-80" onKeyDown={(event) => { if (event.key === " " || event.key === "Enter") { event.preventDefault(); snap(); } }} />
      <div aria-hidden="true" className="pointer-events-none absolute inset-x-8 top-4 h-60 border-2 border-focus" />
      {cameraHint !== null && <p role="status">{t(cameraHint)}</p>}
      {devices.length > 1 && <label className="flex min-h-11 items-center gap-2">{t("photo.camera_switch")}<select className="min-h-11" aria-label={t("photo.camera_switch")} value={deviceId} onChange={(e) => { void startCamera(e.target.value); }}>{devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || t("photo.camera_number", { index: index + 1 })}</option>)}</select></label>}
      <Button onClick={snap}>{t("photo.snap")}</Button>
    </div>}
    <div className="mt-2 flex flex-wrap gap-3">
      {image !== null && <div ref={preview} className="relative m-6 min-w-56 max-w-lg flex-1 self-start">
        <img src={image} alt={t("photo.preview")} className="w-full" />
        {(capture === null || (capture.status === "drafting" && capture.attachments.length === 0) || (previewCapture !== undefined && capture.status === "drafting")) && !busy && corners.map((point, i) => <button key={i} type="button" aria-label={t("photo.corner", { index: i + 1 })} aria-description={t("photo.corner_position", { x: Math.round(point.x * 100), y: Math.round(point.y * 100) })} className="absolute size-11 rounded-full border-2 border-focus bg-canvas" style={{ left: `calc(${point.x * 100}% - 22px)`, top: `calc(${point.y * 100}% - 22px)` }}
          onPointerDown={(e) => e.currentTarget.setPointerCapture(e.pointerId)} onPointerMove={(e) => {
            if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
            const rect = preview.current?.getBoundingClientRect(); if (rect === undefined) return;
            setCorners((points) => points.map((p, n) => n === i ? { x: Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)) } : p));
          }} onKeyDown={(e) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
            e.preventDefault(); const amount = e.shiftKey ? 0.05 : 0.01;
            setCorners((points) => points.map((p, n) => n === i ? { x: Math.max(0, Math.min(1, p.x + (e.key === "ArrowRight" ? amount : e.key === "ArrowLeft" ? -amount : 0))), y: Math.max(0, Math.min(1, p.y + (e.key === "ArrowDown" ? amount : e.key === "ArrowUp" ? -amount : 0))) } : p));
          }} />)}
        {confirming && capture?.regions.some((region) => (region.outline?.length ?? 0) >= 3) && <svg aria-hidden="true" viewBox="0 0 1 1" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full">
          {capture.regions.map((region) => (region.outline?.length ?? 0) < 3 ? null : <polygon key={region.region_id} data-photo-region={`${capture.capture_id}:${region.region_id}`} points={(region.outline ?? []).map((p) => `${p.x},${p.y}`).join(" ")} vectorEffect="non-scaling-stroke" strokeWidth={highlight === region.region_id ? 3 : 1} className={highlight === region.region_id ? "fill-focus/15 stroke-focus" : "fill-none stroke-line-strong"} />)}
        </svg>}
        {confirming && capture?.regions.map((region) => region.bbox == null ? null : <div key={region.region_id} role="button" tabIndex={0} aria-label={t("photo.region", { index: region.order + 1 })} onFocus={() => setHighlight(region.region_id)} onMouseEnter={() => setHighlight(region.region_id)} className={`absolute border ${highlight === region.region_id ? "border-2 border-focus" : "border-line-strong"}`} style={{ left: `${region.bbox.x * 100}%`, top: `${region.bbox.y * 100}%`, width: `${region.bbox.w * 100}%`, height: `${region.bbox.h * 100}%` }} />)}
      </div>}
      {confirming && <div className="min-w-60 flex-1">
        {capture.questions.length > 0 && <fieldset><legend>{t("photo.choose_question")}</legend>{capture.questions.map((question) => <label key={question.label} className="flex min-h-11 items-center gap-2"><input type="radio" name="captured-question" checked={selected === question.label} onChange={() => { setSelected(question.label); setText(capture.regions.filter((r) => question.region_ids.includes(r.region_id)).map(captureRegionText).join("\n")); }} /> {question.label}</label>)}</fieldset>}
        {selected !== null && capture.questions.length > 1 && <div><p>{t("photo.up_next")}</p><ol>{capture.questions.filter((question) => question.label !== selected).map((question) => <li key={question.label}>{question.label}</li>)}</ol></div>}
        <EntryEditor key={selected ?? capture.capture_id} ref={transcript} label={t("photo.confirm_text")} placeholder={t("photo.confirm_text")} initialText={text} multiline onChange={setText} onSubmit={() => false} />
        {capture.regions.filter((r) => r.needs_confirmation).map((region) => <p key={region.region_id} tabIndex={0} onFocus={() => setHighlight(region.region_id)} onMouseEnter={() => setHighlight(region.region_id)} className="text-meta"><span>{t("photo.check_line")}: </span><u><StudyInline text={region.text} /></u></p>)}
        <Button onClick={() => transcript.current?.focus()}>{t("photo.edit")}</Button>
        <Button disabled={busy || text.trim() === "" || (capture.questions.length > 0 && selected === null)} onClick={() => { void confirm(); }}>{t("photo.confirm")}</Button>
      </div>}
    </div>
    {!confirming && !unreadable && (photo !== null || previewCapture !== undefined) && <div className="mt-2 flex gap-2"><Button disabled={busy || (capture !== null && (capture.status !== "drafting" || capture.attachments.length > 0))} onClick={() => { void rotate().catch(() => setError("unsupported_format")); }}>{t("photo.rotate")}</Button><Button disabled={busy || capture?.status === "refused"} onClick={() => { void run(); }}>{t("photo.read")}</Button></div>}
    {unreadable && <ul className="mt-2 list-disc ps-5 text-ui">
      {["flat", "light", "closer"].map((tip) => <li key={tip}>{t(`photo.unreadable.${tip}`)}</li>)}
    </ul>}
    {capture?.quality.checks.filter((c) => c.outcome !== "pass").map((check) => <p key={check.check} role="status" className="text-meta">{t(`photo.quality.${check.check}`)}</p>)}
    {capture?.quality.checks.some((c) => c.outcome !== "pass") && !confirming && !unreadable && <div className="flex gap-2">
      {!capture.quality.checks.some((c) => c.outcome === "blocking") && <Button disabled={busy} onClick={() => { void run(true); }}>{t("photo.override")}</Button>}
      <Button disabled={busy} onClick={() => { if (photoSource === "webcam") void startCamera(); else void backToCrop(); }}>{t("photo.retake")}</Button>
      {capture.quality.checks.some((c) => c.outcome === "blocking") && <Button disabled={busy} onClick={() => fileInput.current?.click()}>{t("photo.choose_another")}</Button>}
    </div>}
    {!confirming && !unreadable && <label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={cloud} disabled={busy} onChange={(e) => setCloud(e.target.checked)} /> {t("photo.cloud_accept")} {cloud && t("photo.cloud_notice")}</label>}
    {slow && <p role="status">{t("photo.slow")}</p>}
    {busy && processingVisible && <p role="status">{t("photo.processing")}</p>}
    {capture?.refusal !== null && capture?.refusal !== undefined && <ErrorState message={t(`photo.refusal.${capture.refusal.reason}`)} />}
    {error !== null && <ErrorState message={t(`photo.error.${error}`)} />}
    {error?.startsWith("camera_") && <Button onClick={() => fileInput.current?.click()}>{t("photo.choose_instead")}</Button>}
    <Button onClick={() => { onTyped(unreadable ? "" : text); onClose(); }}>{t("photo.type_instead")}</Button>
  </div>;
}
