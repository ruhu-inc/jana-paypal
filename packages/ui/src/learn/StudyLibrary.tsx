// Library organisation is a view: every duplicate upload keeps its own source, revisions and saved sets.
import { useState } from "react";
import type { StudyContracts as W } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, Dialog, EmptyState, FIELD_LOOK, IconButton, MenuButton, SegmentedControl } from "../primitives/index.js";

type Source = W.SourceList["items"][number];
export function materialGroups(sources: Source[], sets: W.StudySet[]) {
  const withSets = new Set(sets.filter((s) => s.status === "ready").flatMap((s) => s.sources.map((r) => r.source_id)));
  const groups = new Map<string, [Source, ...Source[]]>();
  for (const source of sources) {
    // A matching filename is never enough, and private metadata is never used to correlate sources.
    const key = !source.private && "sha256" in source && source.sha256 ? `${source.kind}:${source.sha256}` : source.source_id;
    const copies = groups.get(key);
    if (copies) copies.push(source); else groups.set(key, [source]);
  }
  const quality = (s: Source) => "state" in s && ["ready", "partial"].includes(s.state) ? 1 : 0;
  return [...groups.values()].map((copies) => copies.sort((a, b) => quality(b) - quality(a)
    || Number(withSets.has(b.source_id)) - Number(withSets.has(a.source_id)) || b.updated_at.localeCompare(a.updated_at)))
    .sort((a, b) => b[0].updated_at.localeCompare(a[0].updated_at));
}

export function StudyLibrary({ sources, sets, archivedIds, busy, onOpen, onArchive, onRename }: {
  sources: Source[]; sets: W.StudySet[]; archivedIds: string[]; busy: boolean;
  onOpen: (source: Source) => void; onArchive: (ids: string[], archived: boolean) => void;
  /** Every copy of a material takes the new name, so a library of "YouTube video" rows can be told apart. */
  onRename?: (ids: string[], title: string) => void;
}) {
  const t = useT(), [query, setQuery] = useState(""), [shelf, setShelf] = useState<"library" | "archive">("library");
  const [renaming, setRenaming] = useState<{ ids: string[]; title: string } | null>(null);
  const [limit, setLimit] = useState(8), [expanded, setExpanded] = useState<string[]>([]);
  const archived = new Set(archivedIds);
  const groups = materialGroups(sources.filter((s) => archived.has(s.source_id) === (shelf === "archive")), sets)
    .filter((copies) => copies.some((s) => ("title" in s ? s.title : t("study.private_source")).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())));
  const title = (s: Source) => "title" in s ? s.title.replace(/^(?:\s*[>#]+\s*)+/, "") : t("study.private_source");
  const row = (source: Source, copy = false) => <div className="min-w-0 flex-1 [&>button]:w-full [&>button]:justify-start [&>button]:text-start [&>button]:min-w-0"><Button size="sm" tone="ghost" disabled={busy} onClick={() => onOpen(source)}>
    <Icon name={source.private ? "pin-set" : source.kind === "recording" ? "read-aloud" : "notes"} size={20} />
    <span className="flex min-w-0 flex-col gap-0.5"><span className="truncate">{title(source)}</span>
      <span className="text-meta font-normal text-muted">{copy ? t("study.library_added", { when: new Date(source.created_at) }) : "state" in source ? t(source.state === "partial" && source.unreadable_units.length === 0 ? "study.source.review" : `study.source.${source.state}`) : t("study.private_source")}</span>
    </span>
  </Button></div>;
  return <section className="flex flex-col gap-4" aria-labelledby="study-library">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 id="study-library" className="text-ui font-semibold text-ink">{t("study.library")}</h2>
      <SegmentedControl label={t("study.library_shelf")} value={shelf} options={[{ value: "library", label: t("study.library_active") }, { value: "archive", label: t("study.library_archive") }]} onChange={(next) => { setShelf(next); setLimit(8); }} />
    </div>
    <label className="flex items-center gap-2 rounded-control border border-line bg-canvas px-3"><Icon name="search" size={20} /><span className="sr-only">{t("study.library_search")}</span><input type="search" value={query} onChange={(e) => { setQuery(e.target.value); setLimit(8); }} placeholder={t("study.library_search")} className={`${FIELD_LOOK} min-w-0 flex-1 border-0 bg-transparent`} /></label>
    {groups.length === 0 && <EmptyState message={t(query ? "study.library_no_matches" : shelf === "archive" ? "study.library_archive_empty" : "study.empty")} />}
    <ul aria-label={t("study.library")} className="flex flex-col divide-y divide-line">
      {groups.slice(0, limit).map((copies) => { const source = copies[0], open = expanded.includes(source.source_id); return <li key={source.source_id} className="flex flex-col gap-1 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {row(source)}
          {copies.length > 1 && <Button size="sm" tone="ghost" aria-expanded={open} aria-label={t("study.library_copies_of", { count: copies.length, title: title(source) })} onClick={() => setExpanded((ids) => open ? ids.filter((id) => id !== source.source_id) : [...ids, source.source_id])}>{t("study.library_copies", { count: copies.length })}<Icon name={open ? "collapse" : "expand"} size={16} /></Button>}
          <MenuButton label={t("study.library_actions", { title: title(source) })} trigger={(props) => <IconButton {...props} icon="more" size="sm" label={t("study.library_actions", { title: title(source) })} disabled={busy} />}
            items={[
              ...(onRename && "title" in source ? [{ id: "rename", label: t("study.library_rename"), onSelect: () => setRenaming({ ids: copies.map((s) => s.source_id), title: title(source) }) }] : []),
              { id: "archive", label: t(shelf === "archive" ? "study.library_restore" : "study.library_archive_action"), onSelect: () => onArchive(copies.map((s) => s.source_id), shelf !== "archive") },
            ]} />
        </div>
        {open && <ul className="ms-6 flex flex-col gap-1 border-s border-line ps-2" aria-label={t("study.library_copies_of", { count: copies.length, title: title(source) })}>{copies.map((copy) => <li key={copy.source_id} className="flex">{row(copy, true)}</li>)}</ul>}
      </li>; })}
    </ul>
    {groups.length > limit && <span><Button size="sm" tone="ghost" onClick={() => setLimit((n) => n + 8)}>{t("study.library_show_more", { count: groups.length - limit })}</Button></span>}
    {shelf === "archive" && <p className="text-meta text-muted">{t("study.library_archive_help")}</p>}
    <Dialog open={renaming !== null} heading={t("study.library_rename")} closeLabel={t("action.cancel")} onClose={() => setRenaming(null)}>
      <form className="flex flex-col items-start gap-3" onSubmit={(e) => { e.preventDefault(); const name = renaming?.title.trim(); if (renaming && name) { onRename?.(renaming.ids, name); setRenaming(null); } }}>
        <label className="flex w-full flex-col gap-2 text-ui text-ink">{t("study.source_title")}<input autoFocus required maxLength={200} value={renaming?.title ?? ""} onChange={(e) => setRenaming((now) => now && { ...now, title: e.target.value })} className={FIELD_LOOK} /></label>
        <Button type="submit" tone="primary" disabled={busy || !renaming?.title.trim()}>{t("study.library_rename_save")}</Button>
      </form>
    </Dialog>
  </section>;
}
