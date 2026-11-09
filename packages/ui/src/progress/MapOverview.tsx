// My map, Progress's hero (SPEC §21.1, JM-D35, TABLE UXS-22a; W10): every installed subject as topic tiles. Each tile
// is drawn in the look of TABLE UXS-22a's first matching rule over its skills (fill, pattern, icon and the state's
// word), or on the Freshness layer in the look of its freshest touch with that word, and says in words how many of its
// skills are secure, whether one is refresh due, the mistakes being watched and the skills that moved since last time
// (never colour alone, DSN-04; never a probability, JM-D36). A tile whose ideas moved today is outlined, as "What moved
// today" under the hero says. A tap opens the subject's skill map with the layer chosen here. Simpler by default
// (SPEC §22.1, JM-D51): the tiles open on the Mastery layer with no switch in sight; "Layers" reveals the switch, and
// it is already revealed when the map arrives on another layer (the lens is shared with the skill map, in memory).
import { useId, useState, type CSSProperties } from "react";
import { FRESHNESS_LOOK, type MapLayer, type OverviewSubject, type OverviewTopic, type OverviewView } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { MASTERY_PATTERN_VARS, masteryLook } from "../learn/Mastery.js";
import { Button, EmptyState, ErrorState, SegmentedControl, Skeleton, SubjectDot } from "../primitives/index.js";

/** The layers a tile can show: the overview carries the rule, the freshness and the mistakes of each tile. */
export const OVERVIEW_LAYERS = ["mastery", "freshness", "mistakes"] as const satisfies readonly MapLayer[];
type OverviewLayer = (typeof OVERVIEW_LAYERS)[number];

export interface MapOverviewProps {
  view: OverviewView;
  subject: (subject: string) => string;
  layer: MapLayer;
  onLayer: (layer: MapLayer) => void;
  /** Open a subject's skill map (on the layer chosen here), at a topic when a tile was tapped. */
  onOpen: (subjectId: string, topicId?: string) => void;
  /** Tiles holding an idea that moved today (LRN-34's reflection, outlined on the map). */
  moved?: ReadonlySet<string>;
  onRetry?: () => void;
}

function Tile({ topic, layer, moved, onOpen }: { topic: OverviewTopic; layer: OverviewLayer; moved: boolean; onOpen: () => void }) {
  const t = useT();
  const state = layer === "freshness" ? FRESHNESS_LOOK[topic.freshness] : topic.rule;
  const { className, icon } = masteryLook(state);
  const word = layer === "freshness" ? t(`map.freshness.${topic.freshness}`) : t(`mastery.${topic.rule}`);
  const secure = topic.bands.secure;
  const flags = [
    ...(topic.refresh_due ? [t("progress.map.refresh_due")] : []),
    ...(topic.mistakes > 0 ? [t("progress.map.mistakes", { count: topic.mistakes })] : []),
    ...(topic.changed > 0 ? [t("progress.map.changed", { count: topic.changed })] : []),
  ];
  const label = [t("progress.map.tile", { topic: topic.title, state: word, secure, total: topic.kcs, freshness: t(`map.freshness.${topic.freshness}`) }), ...flags,
    ...(moved ? [t("progress.map.moved_outline")] : [])].join(", ");
  const ringed = layer === "mistakes" && topic.mistakes > 0;
  return (
    <li className="flex">
      <button type="button" onClick={onOpen} aria-label={label} data-overview-topic={topic.topic_id} data-rule={topic.rule} data-freshness={topic.freshness}
        data-moved={moved ? "" : undefined} data-ringed={ringed ? topic.mistakes : undefined} style={MASTERY_PATTERN_VARS as CSSProperties}
        className={`flex min-h-20 w-full flex-col items-start gap-1 rounded-card border p-3 text-start ${className} ${moved ? "border-2 border-ink" : "border-mastery-tile-outline"} ${ringed ? "outline-2 outline-offset-2 outline-series-3" : ""} ${topic.refresh_due ? "border-dashed" : ""}`}>
        <span className="flex items-center gap-1 text-meta font-semibold"><Icon name={icon} size={16} />{word}</span>
        <span className="text-ui font-medium">{topic.title}</span>
        <span className="text-meta">{t("progress.map.secure", { secure, total: topic.kcs })}</span>
        {flags.length > 0 && <span className="text-meta font-medium" data-tile-flags="">{flags.join(" · ")}</span>}
      </button>
    </li>
  );
}

function SubjectTiles({ subject, name, layer, moved, onOpen }: { subject: OverviewSubject; name: string; layer: OverviewLayer; moved: ReadonlySet<string>; onOpen: MapOverviewProps["onOpen"] }) {
  const t = useT();
  const kcs = subject.topics.reduce((n, topic) => n + topic.kcs, 0);
  return (
    <section aria-labelledby={`overview-${subject.subject_id}`} data-overview-subject={subject.subject_id} data-kcs={kcs} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={`overview-${subject.subject_id}`} className="flex items-center gap-2 text-ui font-semibold text-ink">
          <SubjectDot subject={subject.subject} />{name}<span className="text-meta font-normal text-muted">{t("progress.map.topics", { count: subject.topics.length })}</span>
        </h3>
        <Button size="sm" tone="ghost" onClick={() => onOpen(subject.subject_id)}>{t("progress.map.open", { subject: name })}</Button>
      </div>
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(10rem,1fr))] gap-2">
        {subject.topics.map((topic) => <Tile key={topic.topic_id} topic={topic} layer={layer} moved={moved.has(topic.topic_id)} onOpen={() => onOpen(subject.subject_id, topic.topic_id)} />)}
      </ul>
    </section>
  );
}

export function MapOverview({ view, subject, layer, onLayer, onOpen, moved = new Set(), onRetry }: MapOverviewProps) {
  const t = useT();
  const shown: OverviewLayer = (OVERVIEW_LAYERS as readonly string[]).includes(layer) ? (layer as OverviewLayer) : "mastery";
  const [layersOpen, setLayersOpen] = useState(shown !== "mastery");
  const layersId = useId();
  const body = () => {
    if (view.status === "failed") return <ErrorState message={t("progress.map.failed")} {...(onRetry !== undefined ? { action: { label: t("action.try_again"), onClick: onRetry } } : {})} />;
    if (view.overview === null) return <div aria-busy="true" data-overview-loading=""><span className="sr-only">{t("progress.map.loading")}</span><Skeleton lines={3} /></div>;
    if (view.overview.subjects.length === 0) return <EmptyState message={t("progress.map.empty")} />;
    // The biggest subject first: the one with the most skills leads the hero.
    const subjects = [...view.overview.subjects].sort((a, b) => b.topics.reduce((n, x) => n + x.kcs, 0) - a.topics.reduce((n, x) => n + x.kcs, 0));
    return <div className="flex flex-col gap-6">{subjects.map((s) => <SubjectTiles key={s.subject_id} subject={s} name={subject(s.subject)} layer={shown} moved={moved} onOpen={onOpen} />)}</div>;
  };
  return (
    <section aria-labelledby="progress-map" data-map-overview={view.status} className="flex flex-col gap-4 rounded-card border border-line bg-canvas p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 id="progress-map" className="text-heading text-ink">{t("progress.map.title")}</h2>
          <p className="text-meta text-muted">{t("progress.map.lead")}</p>
        </div>
        <Button size="sm" tone="ghost" aria-expanded={layersOpen} aria-controls={layersId} onClick={() => setLayersOpen((open) => !open)}>
          <Icon name={layersOpen ? "collapse" : "expand"} size={16} />{t("progress.map.layers")}
        </Button>
      </div>
      {layersOpen && (
        <div id={layersId} data-overview-layers="">
          <SegmentedControl label={t("map.layer")} value={shown} onChange={(v) => onLayer(v)} options={OVERVIEW_LAYERS.map((l) => ({ value: l, label: t(`map.layer.${l}`) }))} />
        </div>
      )}
      {body()}
    </section>
  );
}
