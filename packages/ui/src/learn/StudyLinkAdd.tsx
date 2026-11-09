// Add a YouTube video (SPEC §20.1, §20.4, W9): the link, then the transcript pasted from YouTube's own panel (the
// three-step hint), then a title prefilled "YouTube video". Nothing is downloaded (JM-D30); the source is ready at
// once. The refusals name their reason: not a YouTube link, a transcript past two hours; a paste with no timestamps
// is saved as plain text (no moments) and the form says so.
import { useState } from "react";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, FIELD_LOOK, Notice } from "../primitives/index.js";

export const LINK_ERRORS: Record<string, string> = { unsupported_link: "study.link.unsupported", transcript_too_long: "study.link.too_long" };

export function StudyLinkAdd({ busy, error, onAdd }: {
  busy: boolean;
  /** The workspace's last error code (`unsupported_link`, `transcript_too_long`, …), shown in the form's own words. */
  error: string | null;
  /** Adds the source; resolves to its transcript kind (`plain`: saved as text) or null when it was refused. */
  onAdd: (url: string, transcript: string, title: string) => Promise<"timed" | "plain" | null>;
}) {
  const t = useT();
  const [url, setUrl] = useState(""), [transcript, setTranscript] = useState(""), [title, setTitle] = useState(t("study.link.default_title"));
  const [saved, setSaved] = useState<"timed" | "plain" | null>(null);
  const known = error ? LINK_ERRORS[error] : undefined;
  const message = known ? t(known) : null;
  return <form data-study-link-add="" className="flex w-full flex-col items-start gap-3" aria-label={t("study.link.heading")} onSubmit={(e) => {
    e.preventDefault();
    if (busy || !url.trim() || !transcript.trim()) return;
    void onAdd(url.trim(), transcript, title.trim()).then((kind) => { setSaved(kind); if (kind) { setUrl(""); setTranscript(""); setTitle(t("study.link.default_title")); } });
  }}>
    <h3 className="flex items-center gap-2 text-ui font-semibold text-ink"><Icon name="online" size={16} />{t("study.link.heading")}</h3>
    <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.link.url")}<input required value={url} disabled={busy} inputMode="url" maxLength={2048} placeholder={t("study.link.url_placeholder")} className={FIELD_LOOK} onChange={(e) => setUrl(e.target.value)} /></label>
    <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.link.transcript")}<span className="text-meta text-muted">{t("study.link.hint")}</span><textarea required rows={8} maxLength={400000} value={transcript} disabled={busy} className={`${FIELD_LOOK} py-2`} onChange={(e) => setTranscript(e.target.value)} /></label>
    <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.source_title")}<input required maxLength={120} value={title} disabled={busy} className={FIELD_LOOK} onChange={(e) => setTitle(e.target.value)} /></label>
    <p className="text-meta text-muted">{t("study.link.privacy")}</p>
    {message && <Notice message={message} />}
    {saved === "plain" && !message && <p role="status" className="text-meta text-muted">{t("study.link.saved_as_text")}</p>}
    <Button type="submit" tone="primary" disabled={busy || !url.trim() || !transcript.trim() || !title.trim()}>{t("study.link.add")}</Button>
  </form>;
}
