import { useEffect, useMemo, useState } from "react";
import type { ProfileController } from "@jana/client-core";
import { Disclosure, useProfiles, useT } from "@jana/ui";
import { BrowserLocalSpeech } from "./local-speech.js";

export function VoiceSettings({ profiles, titled = true }: { profiles: ProfileController; titled?: boolean }) {
  const t = useT(), profile = useProfiles(profiles), local = useMemo(() => new BrowserLocalSpeech(), []);
  const [voices, setVoices] = useState(() => local.voices()), [error, setError] = useState(false);
  const [compact, setCompact] = useState(() => local.onlyCompact());
  const [details, setDetails] = useState(false);
  useEffect(() => local.subscribe(() => { setVoices(local.voices()); setCompact(local.onlyCompact()); }), [local]);
  const settings = profile.settings;
  const change = (patch: { speech_rate?: number; speech_voice?: string }) => {
    setError(false); void profiles.setPreferences(patch).catch(() => setError(true));
  };
  return <section aria-label={t("voice.settings")} className="flex min-w-0 flex-col gap-4">
    {titled && <h3 className="text-ui font-semibold">{t("voice.settings")}</h3>}
    <label className="flex flex-wrap items-center gap-3 text-ui">{t("voice.rate")} <input aria-label={t("voice.rate")} type="range" min="0.75" max="1.5" step="0.05"
      value={settings?.speech_rate ?? 1} onChange={(e) => change({ speech_rate: Number(e.target.value) })} /> {settings?.speech_rate ?? 1}×</label>
    <label className="flex min-w-0 flex-col gap-2 text-ui">{t("voice.local_voice")} <select className="w-full min-w-0 rounded-control border border-line-strong bg-canvas px-3 py-2" value={settings?.speech_voice ?? ""} onChange={(e) => change({ speech_voice: e.target.value })}>
      <option value="">{t("voice.automatic_voice")}</option>
      {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
    </select></label>
    <p className="text-meta text-muted">{t("voice.settings_summary")}</p>
    <Disclosure summary={t("voice.settings_details")} open={details} onToggle={() => setDetails(v => !v)}>
      <div className="flex flex-col gap-2 text-meta text-muted">
        <p>{t("voice.voice_scope")}</p>
        {compact && <p data-better-voice="">{t("voice.better_voice")}</p>}
      </div>
    </Disclosure>
    {error && <p role="status">{t("notes.failed")}</p>}
  </section>;
}
