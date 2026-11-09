// The tutor's presence in voice mode (UXS-53, D103): the student's character, in the pose of the voice state with the
// state's words beside it, placed by the layout. It is docked at the top of the board panel while the board is open,
// where it points while the tutor points at a step; it floats beside the composer with the board closed; at phone
// width it sits above the composer. It is never drawn on the board, which belongs to the content (the pen is the
// tutor's hand there). Its state is the voice controller's own, read synchronously, so it follows `voice.state` with
// no delay of its own; the voice bar's polite live region announces each change once, and the drawing announces
// nothing. The speaking mouth follows the loudness of the audio heard (DSN-27). Low-power mode shows the static pose,
// reduced motion the static pose and indicator (presence.css), and the calm screen shows no presence: the tutor screen
// leaves it out there.
import { useSyncExternalStore } from "react";
import type { VoiceView } from "@jana/client-core";
import { useSpeechTools } from "../voice/SpeechTools.js";
import { useVisualSettings } from "../visuals/VisualViewer.js";
import { TutorPresence } from "./Character.js";
import { useTutorAvatar } from "./look.js";
import type { LayoutClass } from "../frame/layout.js";

export type PresencePlacement = "docked" | "floating" | "phone";

/** Where the presence sits: above the composer at phone width, else docked on the open board, else by the composer. */
export function presencePlacement(layout: LayoutClass, boardOpen: boolean): PresencePlacement {
  if (layout === "compact") return "phone";
  return boardOpen ? "docked" : "floating";
}

const none = () => () => undefined;
const offline = (): VoiceView | null => null;

const LOOK: Record<PresencePlacement, string> = {
  docked: "flex shrink-0 justify-start px-4 pt-2",
  floating: "flex shrink-0 justify-end px-4 pb-1",
  phone: "flex shrink-0 justify-center pb-1",
};

export function VoicePresence({ placement }: { placement: PresencePlacement }) {
  const voice = useSpeechTools()?.voice ?? null;
  const view = useSyncExternalStore(voice?.subscribe ?? none, voice === null ? offline : voice.getState, offline);
  const avatar = useTutorAvatar();
  const { lowPower } = useVisualSettings();
  if (view === null || !view.connected) return null;
  // Pointing while the tutor's pointer is on a board line or a part (VOC-10), from the docked place only.
  const pointing = placement === "docked" && view.state === "speaking" && view.pointer !== null;
  return (
    <div data-presence-placement={placement} data-presence-pointing={pointing ? "true" : undefined} className={LOOK[placement]}>
      <TutorPresence id={avatar} state={view.state} pointing={pointing} size={placement === "phone" ? 40 : 48}
        animate={lowPower !== true} {...(view.mouth !== undefined ? { mouth: view.mouth } : {})} {...(view.micLevel !== undefined ? { level: view.micLevel } : {})} />
    </div>
  );
}
