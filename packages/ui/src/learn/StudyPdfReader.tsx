import type { StudyContracts as W } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Button, FIELD_LOOK } from "../primitives/index.js";
import { StudySourceText } from "./StudyText.js";

/** The original page is authoritative for diagrams and equations. Extracted text stays literal and inspectable. */
export function StudyPdfReader({ chunks, page, image, loading, failed, citation, onPage }: {
  chunks: readonly W.Chunk[]; page: number; image: { url: string; page: number } | null;
  loading: boolean; failed: boolean; citation: W.Citation | null; onPage: (chunk: W.Chunk) => void;
}) {
  const t = useT();
  const pages = [...new Set(chunks.flatMap((c) => c.locator.kind === "page" ? [c.locator.page] : []))].sort((a, b) => a - b);
  const at = Math.max(0, pages.indexOf(page)), current = pages[at];
  const shown = chunks.filter((c) => c.locator.kind === "page" && c.locator.page === current);
  const choose = (number: number | undefined) => { const chunk = chunks.find((c) => c.locator.kind === "page" && c.locator.page === number); if (chunk) onPage(chunk); };
  if (current === undefined || !shown.length) return null;
  return <div className="flex min-w-0 flex-col gap-4" data-pdf-reader>
    <nav aria-label={t("study.pdf.navigation")} className="flex flex-wrap items-center justify-between gap-2">
      <Button size="sm" tone="ghost" disabled={at === 0 || loading} onClick={() => choose(pages[at - 1])}>{t("study.pdf.previous")}</Button>
      <label className="flex items-center gap-2 whitespace-nowrap text-meta text-muted">{t("study.pdf.page")}<select aria-label={t("study.pdf.page")} className={`${FIELD_LOOK} max-w-24`} value={current} disabled={loading} onChange={(e) => choose(Number(e.target.value))}>{pages.map((p) => <option key={p} value={p}>{p}</option>)}</select><span>{t("study.pdf.of", { count: pages.length })}</span></label>
      <Button size="sm" tone="ghost" disabled={at === pages.length - 1 || loading} onClick={() => choose(pages[at + 1])}>{t("study.pdf.next")}</Button>
    </nav>
    {loading && <p role="status" className="text-meta text-muted">{t("study.pdf.loading")}</p>}
    {image && image.page === current && <figure><img src={image.url} alt={t("study.page_image", { page: current })} className="h-auto w-full rounded-control border border-line bg-white" /><figcaption className="mt-2 text-center text-meta text-muted">{t("study.page_image", { page: current })}</figcaption></figure>}
    {!loading && image?.page !== current && <div className="flex flex-col items-start gap-2 rounded-control bg-panel p-4"><p className="text-meta text-muted">{t(failed ? "study.pdf.failed" : "study.pdf.original_help")}</p><Button size="sm" onClick={() => choose(current)}>{t("study.show_page", { page: current })}</Button></div>}
    {citation && shown.some((c) => c.chunk_id === citation.chunk_id) && <blockquote className="border-s-2 border-highlight-line ps-4 text-body text-ink"><p className="mb-2 text-meta text-muted">{t("study.pdf.evidence")}</p><mark className="bg-highlight-soft text-ink"><StudySourceText text={citation.quote} /></mark></blockquote>}
    <details key={current} className="rounded-control border border-line px-4 py-3">
      <summary className="cursor-pointer text-ui font-medium text-ink">{t("study.read_extracted_passage")}</summary>
      <p className="my-3 text-meta text-muted">{t("study.pdf.extraction_help")}</p>
      {shown.map((c) => <p key={c.chunk_id} className="mb-4 whitespace-pre-wrap break-words text-body text-ink"><StudySourceText text={c.text} /></p>)}
    </details>
  </div>;
}
