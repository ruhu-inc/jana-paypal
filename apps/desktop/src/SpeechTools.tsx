import { useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from "react";
import { commandsFor, readVoiceAnswer, SpeechController, VoiceController, type Launch, type ProfileController, type StoreHolder, type VoiceAnswer, type VoicePlace } from "@jana/client-core";
import { SpeechToolsProvider, useProfiles, useT } from "@jana/ui";
import { BrowserLocalSpeech } from "./local-speech.js";
import { BrowserVoiceAudio } from "./voice-audio.js";
import { VoiceBar } from "./VoiceBar.js";

/** VOC-15: app focus loss (window blur or a hidden document) releases the mic in every mode. Dictation ends first, so
 * what was said is still transcribed for review; hands-free listens again when focus returns. */
export function releaseOnFocusLoss(speech: Pick<SpeechController, "endDictation" | "voice">): () => void {
  const lost = () => { speech.endDictation(); speech.voice.focusLost(); };
  const gained = () => { if (!document.hidden) void speech.voice.focusGained().catch(() => undefined); };
  const visibility = () => { if (document.hidden) lost(); else gained(); };
  window.addEventListener("blur", lost);
  window.addEventListener("focus", gained);
  document.addEventListener("visibilitychange", visibility);
  return () => {
    window.removeEventListener("blur", lost); window.removeEventListener("focus", gained);
    document.removeEventListener("visibilitychange", visibility);
  };
}

/** What the installed speech pack offers, from the core's capabilities (VOC-14, TABLE VOC-03a): `pack`, read-aloud in
 * the pack's own voice (`voice`); `dictation` (`voice_dictation`); `talk`, voice turns (`voice_tutoring`). A
 * read-aloud-only pack offers the first alone; with no pack the OS voice reads aloud. */
export interface VoiceOffer { pack: boolean; dictation: boolean; talk: boolean }
export const NO_VOICE: VoiceOffer = { pack: false, dictation: false, talk: false };
export function voiceOfferFrom(capabilities: readonly string[]): VoiceOffer {
  const pack = capabilities.includes("voice");
  const talk = pack && capabilities.includes("voice_tutoring");
  return { pack, dictation: pack && capabilities.includes("voice_dictation"), talk };
}

/** Read-aloud (VOC-18) and hold-to-dictate (VOC-46) for the children, everywhere. The voice conversation (the voice bar,
 * VOC-06, VOC-49) only in an open chat (D130): `place` names it, Ask anything on Home or a study set's Chat tab, and
 * `onAnswer` hears each spoken question's answer (`voice.answer`, VOC-53). Without a place there is no voice bar. */
export function SpeechTools({ sessionId, launch, holder, voice, profiles, automatic = true, place = null, onAnswer, children }: {
  sessionId: string; launch: Launch; holder: StoreHolder; voice: VoiceOffer; profiles: ProfileController; automatic?: boolean;
  place?: VoicePlace | null; onAnswer?: (answer: VoiceAnswer) => void; children: ReactNode;
}) {
  const { pack, dictation, talk } = voice;
  const controller = useMemo(() => {
    const endpoint = () => launch.endpoint(false);
    return new SpeechController(sessionId, commandsFor(launch),
      new VoiceController(sessionId, endpoint, new BrowserVoiceAudio()), new BrowserLocalSpeech(), pack, true, dictation);
  }, [sessionId, launch, pack, dictation]);
  const profile = useProfiles(profiles), t = useT();
  useEffect(() => { if (profile.active && !profile.settings) void profiles.loadSettings().catch(() => undefined); }, [profiles, profile.active, profile.settings]);
  const preferences = profile.settings;
  useEffect(() => { controller.configure({ speech_rate: preferences?.speech_rate ?? 1, speech_voice: preferences?.speech_voice ?? "", read_aloud: automatic && (preferences?.read_aloud ?? false), low_power: preferences?.low_power ?? false }); }, [controller, preferences, automatic]);
  const view = useSyncExternalStore(controller.subscribe, controller.getState);
  const answered = useRef(onAnswer);
  answered.current = onAnswer;
  useEffect(() => { controller.voice.setPlace(place); }, [controller, place]);
  useEffect(() => {
    controller.activate();
    controller.observe(holder.getState());
    const untap = holder.onEnvelopes((batch) => {
      for (const envelope of batch) {
        if (envelope.session_id !== sessionId) continue;
        controller.voiceEvent(envelope.event);
        const answer = readVoiceAnswer(envelope.event);
        if (answer !== null) answered.current?.(answer);
      }
      controller.observe(holder.getState());
    });
    const escape = (event: KeyboardEvent) => {
      if (event.code !== "Escape") return;
      const current = controller.getState();
      if (current.dictation !== "idle") controller.cancelDictation();
      else if (current.reading) controller.stopReading();
      else return;
      event.preventDefault(); event.stopImmediatePropagation();
    };
    const unwatch = releaseOnFocusLoss(controller);
    const keyup = (event: KeyboardEvent) => { if (event.code === "KeyM") controller.endDictation(); };
    window.addEventListener("keydown", escape, true);
    window.addEventListener("keyup", keyup);
    return () => {
      untap(); unwatch(); window.removeEventListener("keydown", escape, true); window.removeEventListener("keyup", keyup);
      controller.close();
    };
  }, [controller, holder, sessionId]);
  return <SpeechToolsProvider controller={controller}>
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">{children}</div>
      {preferences?.low_power && <p role="status" className="text-meta text-muted">{t("voice.saving_battery")}</p>}
      {place !== null && !preferences?.low_power && talk && ["idle", "review"].includes(view.dictation) && <VoiceBar controller={controller.voice} />}
    </div>
  </SpeechToolsProvider>;
}
