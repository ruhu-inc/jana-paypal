// The characters' art (DSN-26, TABLE DSN-26a), ported from the tutor-characters design canvas: each character is a
// body on the shared rig of Character.tsx, drawn as vector shapes in a 100-unit box, with its face placed by `face`.
// Bodies are a two-stop gradient (`paint.body`), details an accent gradient (`paint.accent`) and a white sheen clipped
// to the body; there is no outline and no halo. Colours come only from the character tokens, by CSS variable.
import type { ReactNode } from "react";
import type { CharacterPose, CharacterSlug } from "./characters.js";

/** What a body is painted with: the instance's gradient ids (or ink for the silhouette) and its clip and sheen. */
export interface Paint {
  body: string;
  accent: string;
  sheen: string | null;
  clip: string;
}

/** The character's token colours, by role (DSN-02a's character group). */
export function tokenOf(slug: CharacterSlug, role: "start" | "end" | "accent-start" | "accent-end" | "feature"): string {
  return `var(--color-character-${slug}-${role})`;
}

const fill = (paint: string) => ({ fill: paint });

/** The Jana mark's two folded panels (D47, D101): the hook folds on its hinge to lean, react and point. */
const JANA_PANEL = "M37.4 20.74 A1.87 1.87 0 0 1 38.31 19.13 L66.88 2.02 A2.23 2.23 0 0 1 70.26 3.93 L70.26 67.07 A9.57 9.57 0 0 1 65.7 75.22 L39.13 91.57 A1.13 1.13 0 0 1 37.4 90.61 Z";
const JANA_HOOK = "M0 64.53 A2.37 2.37 0 0 1 1.18 62.48 L33.11 43.91 A0.3 0.3 0 0 1 33.55 44.17 L33.55 97.54 A1.54 1.54 0 0 1 31.34 98.92 L5.32 86.24 A9.47 9.47 0 0 1 0 77.73 Z";
const PRISM_BODY = "M55.2 21.7 L84.8 76.3 Q90.0 86.0 79.0 86.0 L21.0 86.0 Q10.0 86.0 15.2 76.3 L44.8 21.7 Q50.0 12.0 55.2 21.7 Z";
const KITE_BODY = "M56.0 12.7 L80.0 39.3 Q86.0 46.0 80.0 52.7 L56.0 79.3 Q50.0 86.0 44.0 79.3 L20.0 52.7 Q14.0 46.0 20.0 39.3 L44.0 12.7 Q50.0 6.0 56.0 12.7 Z";
const RAY_ANGLES = [0, 45, 90, 135, 180, 225, 270, 315];

/** How far the Jana hook folds in each pose (degrees about its top edge). */
const HOOK_FOLD: Partial<Record<CharacterPose, number>> = { listening: -6, interrupted: 8, pointing: 16 };

export interface CharacterArt {
  /** The body's outline, for the sheen's clip. */
  clip: ReactNode;
  /** The body, accents and sheen; `hookClass` animates the Jana hook (lean or point) where the pose has one. */
  body: (paint: Paint, pose: CharacterPose, hookClass: string) => ReactNode;
  /** Where the face sits on the body (an SVG transform in the 120 × 128 rig). */
  face: string;
}

const sheen = (paint: Paint, x: number, y: number, w: number, h: number) =>
  paint.sheen === null ? null : <rect x={x} y={y} width={w} height={h} style={fill(`url(#${paint.sheen})`)} clipPath={`url(#${paint.clip})`} />;

export const ART: Record<CharacterSlug, CharacterArt> = {
  jana: {
    clip: <path d={JANA_PANEL} />,
    body: (paint, pose, hookClass) => {
      const fold = HOOK_FOLD[pose];
      return (
        <g transform="translate(25 12)">
          <path className={`presence-hinge ${hookClass}`} style={{ ...fill(paint.accent), ...(fold !== undefined ? { transform: `rotate(${fold}deg)` } : {}) }} d={JANA_HOOK} />
          <path style={fill(paint.body)} d={JANA_PANEL} />
          {sheen(paint, 37.4, 2, 32.9, 90)}
        </g>
      );
    },
    face: "translate(79 48) skewY(-29.6) scale(1.15)",
  },
  orbit: {
    clip: <circle cx="50" cy="50" r="27" />,
    body: (paint) => (
      <g transform="translate(10 11)">
        <path style={fill(paint.accent)} d="M6.25 64.21 A46 15 -18 0 1 93.75 35.79 L88.04 37.64 A40 10 -18 0 0 11.96 62.36 Z" />
        <circle cx="50" cy="50" r="27" style={fill(paint.body)} />
        {sheen(paint, 23, 23, 54, 54)}
        <path style={fill(paint.accent)} d="M93.75 35.79 A46 15 -18 0 1 6.25 64.21 L11.96 62.36 A40 10 -18 0 0 88.04 37.64 Z" />
        <circle cx="86" cy="15" r="6" style={fill(paint.accent)} />
      </g>
    ),
    face: "translate(60 55) scale(1.2)",
  },
  prism: {
    clip: <path d={PRISM_BODY} />,
    body: (paint) => (
      <g transform="translate(10 11)">
        <path style={fill(paint.body)} d={PRISM_BODY} />
        <path style={fill(paint.accent)} opacity="0.9" clipPath={`url(#${paint.clip})`} d="M50 10 L92 88 L66 88 Z" />
        {sheen(paint, 10, 12, 80, 74)}
      </g>
    ),
    face: "translate(54 77) scale(1.15)",
  },
  kite: {
    clip: <path d={KITE_BODY} />,
    body: (paint) => (
      <g transform="translate(10 11)">
        <path style={fill(paint.accent)} d="M47 89 L51 93 L47 97 L43 93 Z" />
        <path style={fill(paint.accent)} d="M55 96 L58.5 99.5 L55 103 L51.5 99.5 Z" />
        <path style={fill(paint.body)} d={KITE_BODY} />
        <path style={fill(paint.accent)} opacity="0.9" clipPath={`url(#${paint.clip})`} d="M88 46 L50 88 L50 46 Z" />
        {sheen(paint, 14, 6, 72, 80)}
      </g>
    ),
    face: "translate(52 56) scale(1.15)",
  },
  pebble: {
    clip: <rect x="12" y="38" width="76" height="54" rx="27" />,
    body: (paint) => (
      <g transform="translate(10 11)">
        <ellipse cx="62" cy="31" rx="18" ry="10" style={fill(paint.accent)} />
        <rect x="12" y="38" width="76" height="54" rx="27" style={fill(paint.body)} />
        {sheen(paint, 12, 38, 76, 54)}
      </g>
    ),
    face: "translate(56 75) scale(1.25)",
  },
  ray: {
    clip: <circle cx="50" cy="50" r="27" />,
    body: (paint) => (
      <g transform="translate(10 11)">
        <g style={fill(paint.accent)}>
          {RAY_ANGLES.map((angle) => <rect key={angle} x="46" y="1" width="8" height="18" rx="4" {...(angle === 0 ? {} : { transform: `rotate(${angle} 50 50)` })} />)}
        </g>
        <circle cx="50" cy="50" r="27" style={fill(paint.body)} />
        {sheen(paint, 23, 23, 54, 54)}
      </g>
    ),
    face: "translate(60 61) scale(1.25)",
  },
};
