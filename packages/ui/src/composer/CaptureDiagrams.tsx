import { useEffect, useState } from "react";
import { PhotoCommands, type CapturedQuestion } from "@jana/client-core";
import { useSession } from "../store.js";
import { useT } from "../i18n.js";

/** Student-visible local crops. Only confirmed text is the alternative; stale reader descriptions stay out. */
export function CaptureDiagrams({ capture }: { capture: CapturedQuestion }) {
  const session = useSession(), t = useT();
  const [images, setImages] = useState<{ capture: string; id: string; url: string }[]>([]);
  const confirmed = capture.status === "confirmed" && capture.confirmation.confirmed;
  useEffect(() => {
    let alive = true; const urls: string[] = [];
    setImages([]);
    if (session !== null && confirmed && capture.session_id != null) {
      const api = new PhotoCommands(session.commands, capture.session_id);
      void Promise.all(capture.attachments.filter((a) => a.kind === "diagram_crop").map(async (a) => {
        try {
          const blob = await api.image(capture.capture_id, a.image_id);
          if (!alive) return null;
          const url = URL.createObjectURL(blob); urls.push(url);
          return { capture: capture.capture_id, id: a.image_id, url };
        } catch { return null; }
      })).then((values) => { if (alive) setImages(values.filter((v) => v !== null)); });
    }
    return () => { alive = false; urls.forEach((url) => URL.revokeObjectURL(url)); };
  }, [session, capture, confirmed]);
  if (!confirmed) return null;
  return <figure className="my-2 rounded-card border border-line bg-panel p-3">
    <figcaption><p className="font-semibold">{t("photo.confirmed_diagram")}</p><p className="whitespace-pre-wrap break-words">{capture.text}</p></figcaption>
    {images.filter((image) => image.capture === capture.capture_id).map((image) => <img key={image.id} src={image.url} alt={capture.text} className="mt-2 max-h-64 max-w-full object-contain" />)}
  </figure>;
}
