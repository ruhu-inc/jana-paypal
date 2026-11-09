// Mastery states on screen (DSN-23, DSN-04, LRN-34): every state is its fill, its pattern, its icon and its TABLE
// LRN-09a word, never colour alone, from tokens.json's `mastery` block. The segmented bar counts a topic's KCs in
// LRN-34's three bands; its words ("4 Secure · 2 Learning · 1 Not started") are the bar's accessible text.
import type { CSSProperties } from "react";
import { BANDS, bandCounts, type KcState, type MapBand, type MapKc } from "@jana/client-core";
import { TOKENS } from "../generated/tokens.js";
import { Icon, type IconName } from "../icons.js";
import { useT } from "../i18n.js";

/** Fill and label ink per state, spelt out so the stylesheet generator sees each class (DSN-01). */
const LOOK: Record<KcState, string> = {
  not_started: "bg-mastery-not-started text-mastery-not-started-ink",
  learning: "bg-mastery-learning text-mastery-learning-ink",
  ready_to_check: "bg-mastery-nearly text-mastery-nearly-ink",
  mastered: "bg-mastery-secure text-mastery-secure-ink",
  needs_review: "bg-mastery-refresh text-mastery-refresh-ink",
  moved_on: "bg-mastery-moved-on text-mastery-moved-on-ink",
};

/** The state each band is drawn as on the bar (LRN-34: the bar has three bands). */
const BAND_STATE: Record<MapBand, KcState> = { secure: "mastered", learning: "learning", not_started: "not_started" };

const STROKE = TOKENS.mastery.patternStroke;
const PATTERN_VARS = { "--mastery-pattern-width": `${STROKE.width}px`, "--mastery-pattern-gap": `${STROKE.spacing}px` } as CSSProperties;

function look(state: KcState): { className: string; icon: IconName } {
  const entry = TOKENS.mastery.states[state];
  return { className: `${LOOK[state]} mastery-pattern-${entry.pattern}`, icon: entry.icon as IconName };
}

/** A state's fill, pattern and icon (tokens.json), for a tile drawn in its look (the learner's map, SPEC §21). */
export const masteryLook = look;
/** The pattern stroke's CSS variables, for an element drawn in a mastery look. */
export const MASTERY_PATTERN_VARS = PATTERN_VARS;

/** A state's word (TABLE LRN-09a): the catalogue key is tokens.json's `labelKey`, `mastery.<state>`. */
export function useMasteryWord(): (state: KcState) => string {
  const t = useT();
  return (state) => t(`mastery.${state}`);
}

/** A state as fill, pattern, icon and word. `compact` drops the word for elements narrower than 48 px; the word is
 * then its accessible name, so it is still read and shown on focus by the element that holds it. */
export function MasteryMark({ state, compact = false }: { state: KcState; compact?: boolean }) {
  const word = useMasteryWord()(state);
  const { className, icon } = look(state);
  return (
    <span data-mastery={state} style={PATTERN_VARS} className={`inline-flex min-h-6 items-center gap-1 rounded-chip border border-mastery-tile-outline px-2 text-meta font-medium ${className}`}>
      <Icon name={icon} size={16} />
      {compact ? <span className="sr-only">{word}</span> : <span>{word}</span>}
    </span>
  );
}

/** The mastery ring of a topic card (DSN-23, UXS-22): the share of the topic's KCs that are secure as an arc in
 * `mastery-secure` on a `line` track. Decorative: the mastery word beside it and the counts say the same in words. */
export function MasteryRing({ kcs, size = 28 }: { kcs: readonly Pick<MapKc, "band">[]; size?: number }) {
  const counts = bandCounts(kcs);
  const total = counts.secure + counts.learning + counts.not_started;
  const stroke = 3;
  const radius = (size - stroke) / 2;
  const length = 2 * Math.PI * radius;
  const share = total === 0 ? 0 : counts.secure / total;
  return (
    <svg aria-hidden="true" data-mastery-ring={`${counts.secure}/${total}`} width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0 -rotate-90">
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" strokeWidth={stroke} className="stroke-line" />
      {share > 0 && (
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" strokeWidth={stroke} strokeLinecap="round" className="stroke-mastery-secure"
          strokeDasharray={`${length * share} ${length}`} />
      )}
    </svg>
  );
}

/** "4 Secure · 2 Learning · 1 Not started": the bands that have KCs, in bar order. */
export function useCountsText(): (kcs: readonly Pick<MapKc, "band">[]) => string {
  const t = useT();
  return (kcs) => {
    const counts = bandCounts(kcs);
    const shown = BANDS.filter((b) => counts[b] > 0);
    return (shown.length === 0 ? ["not_started" as const] : shown).map((b) => t(`map.count.${b}`, { count: counts[b] })).join(" · ");
  };
}

/** The topic's KCs as one bar of three segments, each as wide as its count; decorative, the counts text says it. */
export function SegmentedBar({ kcs }: { kcs: readonly Pick<MapKc, "band">[] }) {
  const counts = bandCounts(kcs);
  return (
    <span aria-hidden="true" className="flex h-3 w-full overflow-hidden rounded-pill border border-mastery-tile-outline" data-segments={BANDS.map((b) => counts[b]).join(",")}>
      {BANDS.filter((b) => counts[b] > 0).map((band) => {
        const { className } = look(BAND_STATE[band]);
        return <span key={band} data-band={band} style={{ ...PATTERN_VARS, flexGrow: counts[band] }} className={`block h-full border-e border-mastery-tile-outline last:border-e-0 ${className}`} />;
      })}
    </span>
  );
}

/** One idea as a heat-map cell (UXS-27, DSN-23): fill, pattern and icon, with the word as its accessible name and shown
 * on focus, since a cell is narrower than 48 px. `bar` draws it as a segment of a column chart instead, as tall as
 * `grow` (a share of the column), decorative (the chart's table and summary say the values). */
export function MasteryCell({ state, label, kcId, bar = false, grow = 1 }: { state: KcState; label?: string | undefined; kcId?: string; bar?: boolean; grow?: number }) {
  const word = useMasteryWord()(state);
  const { className, icon } = look(state);
  if (bar) {
    return <span aria-hidden="true" data-mastery={state} style={{ ...PATTERN_VARS, flexGrow: grow, flexBasis: 0 }} className={`block min-h-1 rounded-control border border-mastery-tile-outline ${className}`} />;
  }
  return (
    <li tabIndex={0} data-heat-kc={kcId} data-mastery={state} aria-label={label === undefined ? word : `${label}: ${word}`} className="group relative rounded-control">
      <span aria-hidden="true" style={PATTERN_VARS} className={`flex size-7 items-center justify-center rounded-control border border-mastery-tile-outline ${className}`}><Icon name={icon} size={16} /></span>
      <span aria-hidden="true" data-cell-word="" className="pointer-events-none absolute start-0 top-full z-popover mt-1 hidden whitespace-nowrap rounded-control border border-line bg-canvas px-2 py-1 text-meta text-ink group-focus-visible:block">{word}</span>
    </li>
  );
}

/** Low to high, the order LRN-34's bar fills in. */
const RISING: readonly MapBand[] = ["not_started", "learning", "secure"];

/** One idea's band on LRN-34's three-band bar (practice, after a checked answer): the bands up to the idea's band are
 * filled in their mastery look, with the word beside it, so the bar moves when the band does. */
export function BandBar({ band, label }: { band: MapBand; label: string }) {
  const t = useT();
  const word = t(`practice.band.${band}`);
  const reached = RISING.indexOf(band);
  return (
    <span data-band-bar={band} role="img" aria-label={`${label}: ${word}`} className="inline-flex items-center gap-2">
      <span aria-hidden="true" className="flex h-3 w-24 overflow-hidden rounded-pill border border-mastery-tile-outline">
        {RISING.map((b, i) => {
          const { className } = look(BAND_STATE[b]);
          return <span key={b} data-segment={b} data-filled={i <= reached ? "" : undefined} style={PATTERN_VARS} className={`block h-full flex-1 border-e border-mastery-tile-outline last:border-e-0 ${i <= reached ? className : "bg-canvas"}`} />;
        })}
      </span>
      <span aria-hidden="true" className="text-meta font-medium text-ink">{word}</span>
    </span>
  );
}
