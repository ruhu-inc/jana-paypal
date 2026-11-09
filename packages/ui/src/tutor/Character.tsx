// The tutor character (DSN-26) on its one shared rig, and the presence (UXS-53) that pairs it with the state's words.
// The rig places any character's body (art.tsx), then a face of a few simple features (eyes, a mouth, an optional brow)
// in the character's `-feature` colour, then the pose's indicator. Every expression of TABLE DSN-26b is a pose; none is a
// frown or a sad face, and none reacts to the student's mood or absence (LRN-35). The drawing is decorative
// (`aria-hidden`): the tutor's name or the state's text label always sits beside it (VOC-17).
// Motion (DSN-27) is the presence primitive's classes (presence.css): transform and opacity only. The static pose is in
// the markup, so reduced motion (the system setting, or the root's `data-reduced-motion` that Reduce motion and
// low-power mode set) and `animate={false}` (low power, a still mark such as the turn label) show it with nothing
// running. The calm screen shows no presence at all (UXS-43): it never renders one.
import { useId, type CSSProperties, type ReactNode } from "react";
import { useT } from "../i18n.js";
import { ART, tokenOf, type Paint } from "./art.js";
import { characterOf, poseOfVoiceState, slugOf, type CharacterId, type CharacterMouth, type CharacterPose, type VoiceStateName } from "./characters.js";

/** The rig's drawing box; a character is drawn `size` wide and in proportion. */
const VIEW_W = 120;
const VIEW_H = 128;

type Eyes = { w: number; h: number; shift?: string; blink: boolean; glance: boolean };

const EYES_REST: Eyes = { w: 5.2, h: 9.4, blink: true, glance: false };
const EYES_WIDE: Eyes = { w: 5.6, h: 10.8, blink: true, glance: false };

/** Each pose's eyes. Thinking glances up; pointing looks toward the target (the board below and to the start side). */
function eyesOf(pose: CharacterPose): Eyes | null {
  switch (pose) {
    case "listening":
      return EYES_WIDE;
    case "interrupted":
      return { ...EYES_WIDE, blink: false };
    case "thinking":
      return { w: 5.2, h: 8, shift: "translate(2px, -2.5px)", blink: false, glance: true };
    case "pointing":
      return { ...EYES_REST, shift: "translate(-2px, 1.5px)" };
    case "speaking":
      return { ...EYES_REST, blink: false };
    case "checked":
    case "silhouette":
      return null;
    default:
      return EYES_REST;
  }
}

/** A line mouth (stroked); every one turns up or is level. */
const line = (d: string, className = "", style?: CSSProperties) => <path data-part="mouth" data-line="" className={className} d={d} {...(style !== undefined ? { style } : {})} />;

const SMILE = "M-4.5 6 Q0 9.8 4.5 6";

/** The four speaking shapes, from the rest (closed) to open, drawn filled except the closed smile. */
function speakingMouth(mouth: CharacterMouth, className: string, colour: string): ReactNode {
  const solid = { fill: colour };
  switch (mouth) {
    case "closed":
      return <path data-part="mouth" data-line="" data-mouth="closed" className={className} d={SMILE} />;
    case "small":
      return <ellipse data-part="mouth" data-mouth="small" className={className} style={solid} cx="0" cy="7.5" rx="3" ry="2" />;
    case "medium":
      return <ellipse data-part="mouth" data-mouth="medium" className={className} style={solid} cx="0" cy="8" rx="2.5" ry="3.2" />;
    case "open":
      return <path data-part="mouth" data-mouth="open" className={className} style={solid} d="M-5.5 5.5 Q0 4.6 5.5 5.5 Q4.6 12.5 0 12.5 Q-4.6 12.5 -5.5 5.5 Z" />;
  }
}

function Face({ pose, mouth, colour, animate }: { pose: CharacterPose; mouth: CharacterMouth; colour: string; animate: boolean }) {
  const eyes = eyesOf(pose);
  const motion = (name: string) => (animate ? `presence-motion ${name}` : "");
  let mouthArt: ReactNode;
  switch (pose) {
    case "listening":
      mouthArt = line("M-3.5 6.5 Q0 9.2 3.5 6.5");
      break;
    case "thinking":
      mouthArt = line("M-2.8 8 L2.8 8");
      break;
    case "hmm":
      mouthArt = line("M-3.8 8.5 Q0.5 9.5 4.3 6.6");
      break;
    case "checked":
      mouthArt = <path data-part="mouth" style={{ fill: colour }} d="M-6.5 5 Q0 15.5 6.5 5 Z" />;
      break;
    case "interrupted":
      // A small "o", then a smile: the "o" is the still pose; the smile comes in only while animating.
      mouthArt = (
        <>
          <ellipse data-part="mouth" data-mouth="o" className={motion("presence-oh")} style={{ fill: colour }} cx="0" cy="8" rx="2.3" ry="2.8" />
          {animate && line(SMILE, "presence-motion presence-oh-smile", { opacity: 0 })}
        </>
      );
      break;
    case "speaking":
      // The shape follows the speech's loudness (DSN-27); still, the mouth keeps its rest shape.
      mouthArt = !animate || mouth === "closed" ? speakingMouth("closed", "", colour) : (
        <>
          {speakingMouth("closed", "presence-rest", colour)}
          {speakingMouth(mouth, "presence-shape", colour)}
        </>
      );
      break;
    default:
      mouthArt = line(SMILE);
  }
  return (
    <g data-part="face">
      {eyes !== null && (
        <g className={eyes.glance ? motion("presence-tb presence-glance") : "presence-tb"} {...(eyes.shift !== undefined ? { style: { transform: eyes.shift } } : {})}>
          <g className={eyes.blink ? motion("presence-tb presence-blink") : ""} style={{ fill: colour }}>
            {[-7.5, 7.5].map((x) => <rect key={x} data-part="eye" x={x - eyes.w / 2} y={-4 - eyes.h / 2} width={eyes.w} height={eyes.h} rx={eyes.w / 2} />)}
          </g>
        </g>
      )}
      <g fill="none" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" style={{ stroke: colour }}>
        {pose === "checked" && (
          <>
            <path data-part="eye" d="M-10.5 -2.5 Q-7.5 -8.5 -4.5 -2.5" />
            <path data-part="eye" d="M4.5 -2.5 Q7.5 -8.5 10.5 -2.5" />
          </>
        )}
        {pose === "hmm" && <path data-part="brow" strokeWidth="2" d="M3.5 -14 Q7.5 -16.8 11.5 -14.8" />}
        {mouthArt}
      </g>
    </g>
  );
}

/** The pose's indicator beside the body (TABLE DSN-27a): the listening ring (a dot when still), the thinking dots and
 * the speaking wave, in the student's and the tutor's roles. */
function Indicator({ pose, animate, level }: { pose: CharacterPose; animate: boolean; level: number }) {
  if (pose === "listening") {
    const dot = <circle data-indicator="listening" className={animate ? "presence-rest" : ""} cx="110" cy="10" r="4" style={{ fill: "var(--color-student)" }} />;
    if (!animate) return dot;
    return (
      <>
        <circle data-indicator="listening" className="presence-tb presence-shape" cx="60" cy="66" r="56" fill="none" strokeWidth="2"
          style={{ stroke: "var(--color-student)", transform: `scale(${1 + 0.08 * Math.min(1, Math.max(0, level))})` }} />
        {dot}
      </>
    );
  }
  if (pose === "thinking") {
    return (
      <g data-indicator="thinking" style={{ fill: "var(--color-tutor)" }}>
        {([[12, 34], [20.5, 26], [29, 18.5]] as const).map(([cx, cy], i) => (
          <circle key={i} className={animate ? `presence-motion presence-dot presence-dot-${i + 1}` : ""} cx={cx} cy={cy} r="3.5" />
        ))}
      </g>
    );
  }
  if (pose === "speaking") {
    return (
      <g data-indicator="speaking" style={{ fill: "var(--color-tutor)" }}>
        {([[102, 6], [108, 10], [114, 6]] as const).map(([x, h], i) => (
          <rect key={i} className={animate ? `presence-motion presence-tb presence-wave presence-dot-${i + 1}` : ""} x={x - 1.5} y={114 - h / 2} width="3" height={h} rx="1.5" />
        ))}
      </g>
    );
  }
  return null;
}

/** The body's motion for a pose: breathing at idle, a bob while speaking, the "Checked" beat. */
const BODY_MOTION: Partial<Record<CharacterPose, string>> = { idle: "presence-breathe", listening: "presence-breathe", speaking: "presence-bob", checked: "presence-beat" };
/** The Jana hook's motion: it leans in to listen and folds out to point. */
const HOOK_MOTION: Partial<Record<CharacterPose, string>> = { listening: "presence-lean", pointing: "presence-point" };

export interface CharacterProps {
  /** One of the six ids of TABLE DSN-26a; anything else draws Jana (G119). */
  id: CharacterId | string;
  pose?: CharacterPose;
  /** The speaking mouth (DSN-27), picked from the loudness by the caller (S3); a small open shape when omitted. */
  mouth?: CharacterMouth;
  /** The microphone level while listening, 0 to 1 (the ring's scale). */
  level?: number;
  /** Width in pixels; the height follows the rig's proportion. */
  size?: number;
  /** False for low-power mode and still marks: the static pose with nothing running (DSN-27). */
  animate?: boolean;
  className?: string;
}

export function Character({ id, pose = "idle", mouth = "small", level = 0, size = 48, animate = true, className = "" }: CharacterProps) {
  const character = characterOf(id);
  const slug = slugOf(character);
  const art = ART[slug];
  const uid = useId().replace(/[^A-Za-z0-9]/g, "");
  // Unique per drawing; the character's name is in the id too, so drawings from two React roots that happen to share
  // an id still paint with their own character's gradients.
  const key = `tc-${slug}-${uid}`;
  const ids = { body: `${key}-b`, accent: `${key}-a`, sheen: `${key}-s`, clip: `${key}-c` };
  const silhouette = pose === "silhouette";
  const moving = animate && !silhouette;
  const paint: Paint = silhouette
    ? { body: "var(--color-ink)", accent: "var(--color-ink)", sheen: null, clip: ids.clip }
    : { body: `url(#${ids.body})`, accent: `url(#${ids.accent})`, sheen: ids.sheen, clip: ids.clip };
  const gradient = (gid: string, from: string, to: string) => (
    <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" style={{ stopColor: from }} />
      <stop offset="1" style={{ stopColor: to }} />
    </linearGradient>
  );
  const bodyMotion = moving ? BODY_MOTION[pose] : undefined;
  const hookMotion = moving ? HOOK_MOTION[pose] : undefined;
  return (
    <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} width={size} height={Math.round((size * VIEW_H) / VIEW_W)} overflow="visible" aria-hidden="true" focusable="false"
      data-character={character} data-pose={pose} data-animate={moving ? "true" : "false"} {...(pose === "speaking" ? { "data-mouth": moving ? mouth : "closed" } : {})}
      className={`presence shrink-0 ${className}`}>
      <defs>
        {!silhouette && (
          <>
            {gradient(ids.body, tokenOf(slug, "start"), tokenOf(slug, "end"))}
            {gradient(ids.accent, tokenOf(slug, "accent-start"), tokenOf(slug, "accent-end"))}
            <radialGradient id={ids.sheen} cx="0.3" cy="0.22" r="0.6">
              <stop offset="0" stopColor="white" stopOpacity="0.5" />
              <stop offset="1" stopColor="white" stopOpacity="0" />
            </radialGradient>
          </>
        )}
        <clipPath id={ids.clip}>{art.clip}</clipPath>
      </defs>
      {!silhouette && <Indicator pose={pose} animate={moving} level={level} />}
      <g className={bodyMotion !== undefined ? `presence-motion presence-tb ${bodyMotion}` : "presence-tb"}>
        <g className={moving && pose === "hmm" ? "presence-motion presence-pivot presence-tilt" : "presence-pivot"} {...(pose === "hmm" ? { style: { transform: "rotate(-8deg)" } } : {})}>
          {art.body(paint, pose, hookMotion !== undefined ? `presence-motion ${hookMotion}` : "")}
          {!silhouette && (
            <g transform={art.face}>
              <Face pose={pose} mouth={mouth} colour={tokenOf(slug, "feature")} animate={moving} />
            </g>
          )}
        </g>
      </g>
    </svg>
  );
}

/** The presence (UXS-53): the character in the pose of the voice state, with the state's words beside it, never
 * instead of them. `label` overrides the words where VOC-04 gives the state another label (read-aloud, wake word).
 * The polite announcement of a change is the voice bar's live region; the drawing announces nothing. */
export function TutorPresence({ id, state, label, mouth, level, size = 48, animate = true, pointing = false }: {
  id: CharacterId | string;
  state: VoiceStateName;
  label?: string;
  /** Turned toward the step the tutor points at (UXS-53, TABLE DSN-27a), while speaking. */
  pointing?: boolean;
  mouth?: CharacterMouth;
  level?: number;
  size?: number;
  animate?: boolean;
}) {
  const t = useT();
  return (
    <div className="flex items-center gap-3" data-presence={state}>
      <Character id={id} pose={pointing ? "pointing" : poseOfVoiceState(state)} size={size} animate={animate && state !== "unavailable"}
        {...(mouth !== undefined ? { mouth } : {})} {...(level !== undefined ? { level } : {})} />
      <span className="text-ui text-ink">{label ?? t(`voice.state.${state}`)}</span>
    </div>
  );
}
