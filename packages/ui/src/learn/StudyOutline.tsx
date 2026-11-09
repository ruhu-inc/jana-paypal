// What this video teaches (SPEC §20.1, §20.4, W9): the `teaching_outline` aid. The model-written, cited part is the
// objectives in order with their moments, the worked examples, the concepts and what the lecture assumes; around it,
// by code on the device: which syllabus skills each concept serves (chips that open the learner's map) or "outside
// the packs", the skills of those topics the video never reaches ("Not covered here", worded without blame), and for
// this learner each skill's band as a word (never a probability). Every moment opens the video there.
import { momentLabel, type StudyContracts as W } from "@jana/client-core";
import { citationKey, StudyCitations } from "./StudyCitation.js";
import { StudyInline } from "./StudyText.js";
import { useT } from "../i18n.js";
import { Button } from "../primitives/index.js";

type Outline = NonNullable<W.Aid["outline"]>;
type Timed = { text?: string; name?: string; at_ms?: number; citations: W.Citation[] };

export function StudyOutline({ ownCourse = false, outline, sourceTitle, chunks, busy, onOpenMoment, onCite, onSkill, skillName }: {
  ownCourse?: boolean; outline: Outline; sourceTitle: string; chunks: readonly W.Chunk[]; busy: boolean;
  /** Open at m:ss: the browser for a YouTube video, the player for a file. */
  onOpenMoment: (ms: number) => void;
  onCite: (citation: W.Citation) => void;
  /** A skill chip: the learner's map at that skill (SPEC §21). */
  onSkill?: ((kcId: string) => void) | undefined;
  /** A skill's statement in the pack's words, when the app knows it; else the chip shows the skill's last name. */
  skillName?: ((kcId: string) => string | null) | undefined;
}) {
  const t = useT();
  const locate = (ref: W.Citation) => chunks.find((chunk) => citationKey(chunk) === citationKey(ref))?.locator;
  const name = (kc: string) => skillName?.(kc) ?? kc.split(".").at(-1)?.replace(/_/g, " ") ?? kc;
  const moment = (item: Timed) => typeof item.at_ms === "number" && <Button size="xs" tone="reference" data-open-at={item.at_ms} aria-label={t("study.video.open_at_full", { time: momentLabel(item.at_ms) })} onClick={() => onOpenMoment(item.at_ms as number)}>{t("study.video.open_at", { time: momentLabel(item.at_ms) })}</Button>;
  // The Open-at button carries the moment, so a timed row's citation badges are reference numbers: the time shows
  // once (live-run fix 2026-11-01: "Open at 4:12" beside "04:12").
  const timed = (item: Timed) => typeof item.at_ms === "number";
  const row = (item: Timed, key: string) => <li key={key} className="flex flex-wrap items-baseline gap-2">
    <span className="text-body text-ink"><StudyInline text={item.text ?? item.name ?? ""} /></span>
    {moment(item)}
    <StudyCitations citations={item.citations} locate={locate} title={() => sourceTitle} disabled={busy} onOpen={onCite} numbered={timed(item)} />
  </li>;
  const bands = new Map(outline.for_you?.map((f) => [f.kc_id, f.band]) ?? []);
  return <article data-study-outline="" className="flex flex-col gap-5" aria-label={t("study.tool.teaching_outline")}>
    <section className="flex flex-col gap-2" aria-labelledby="outline-objectives">
      <h3 id="outline-objectives" className="text-ui font-semibold text-ink">{t("study.outline.objectives")}</h3>
      {outline.objectives.length === 0 ? <p className="text-meta text-muted">{t("study.outline.none")}</p> : <ol className="flex list-decimal flex-col gap-2 ps-5">{outline.objectives.map((o, i) => row(o, `o${i}`))}</ol>}
    </section>
    {outline.examples.length > 0 && <section className="flex flex-col gap-2" aria-labelledby="outline-examples">
      <h3 id="outline-examples" className="text-ui font-semibold text-ink">{t("study.outline.examples")}</h3>
      <ul className="flex flex-col gap-2">{outline.examples.map((e, i) => row(e, `e${i}`))}</ul>
    </section>}
    <section className="flex flex-col gap-2" aria-labelledby="outline-concepts">
      <h3 id="outline-concepts" className="text-ui font-semibold text-ink">{t("study.outline.concepts")}</h3>
      {outline.concepts.length === 0 ? <p className="text-meta text-muted">{t("study.outline.none")}</p> : <ul className="flex flex-col gap-3">{outline.concepts.map((c, i) => <li key={`c${i}`} data-outline-concept={c.name} className="flex flex-col gap-1">
        <div className="flex flex-wrap items-baseline gap-2"><span className="text-body font-medium text-ink"><StudyInline text={c.name} /></span>{moment(c)}<StudyCitations citations={c.citations} locate={locate} title={() => sourceTitle} disabled={busy} onOpen={onCite} numbered={timed(c)} /></div>
        <div className="flex flex-wrap gap-2">
          {(c.kc_ids ?? []).map((kc) => <Button key={kc} size="xs" tone="secondary" data-skill={kc} aria-label={t("study.outline.skill_full", { skill: name(kc), band: t(`study.outline.band.${bands.get(kc) ?? "not_started"}`) })} disabled={!onSkill} onClick={() => onSkill?.(kc)}>{name(kc)}</Button>)}
          {c.outside_packs && !ownCourse && <span data-outside-packs="" className="text-meta text-muted">{t("study.outline.outside_packs")}</span>}
        </div>
      </li>)}</ul>}
    </section>
    {outline.assumes.length > 0 && <section className="flex flex-col gap-2" aria-labelledby="outline-assumes">
      <h3 id="outline-assumes" className="text-ui font-semibold text-ink">{t("study.outline.assumes")}</h3>
      <ul className="flex flex-col gap-2">{outline.assumes.map((a, i) => row(a, `a${i}`))}</ul>
    </section>}
    {(outline.skips?.length ?? 0) > 0 && <section className="flex flex-col gap-2" aria-labelledby="outline-skips">
      <h3 id="outline-skips" className="text-ui font-semibold text-ink">{t("study.outline.skips")}</h3>
      <p className="text-meta text-muted">{t("study.outline.skips_help")}</p>
      <ul className="flex flex-col gap-1">{outline.skips?.map((s) => <li key={s.kc_id} data-skip={s.kc_id} className="flex flex-wrap items-center gap-2"><span className="text-body text-ink">{s.statement}</span>{onSkill && <Button size="xs" tone="ghost" onClick={() => onSkill(s.kc_id)}>{t("study.outline.on_map")}</Button>}</li>)}</ul>
    </section>}
    {(outline.for_you?.length ?? 0) > 0 && <section className="flex flex-col gap-2" aria-labelledby="outline-for-you">
      <h3 id="outline-for-you" className="text-ui font-semibold text-ink">{t("study.outline.for_you")}</h3>
      <ul className="flex flex-col gap-1">{outline.for_you?.map((f) => <li key={f.kc_id} data-for-you={f.kc_id} data-band={f.band} className="flex flex-wrap items-center gap-2 text-body text-ink"><span>{name(f.kc_id)}</span><span className="text-meta text-muted">· {t(`study.outline.band.${f.band}`)}</span></li>)}</ul>
    </section>}
  </article>;
}
