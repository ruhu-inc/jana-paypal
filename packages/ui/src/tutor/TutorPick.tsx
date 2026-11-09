// The one pick of the tutor (UXS-54, PRM-03, D103): the six characters of TABLE DSN-26a as tiles (the character, its
// default name and its hint), the name field and "Reset to Jana", in Settings → Tutor and at the start of the
// first-session interview (UXS-10 step 2), where "Keep Jana" skips it. Choosing a tile fills the name with the
// character's default name unless the student typed their own; saving writes `tutor_avatar` and `tutor_name` in one
// settings patch, and a name the core refuses (PRV-03) shows the field's error. Re-skinning or renaming changes the look
// and the name of the same tutor: one tutor per profile, its memory and learner model untouched. The selected tile is
// the neutral wash with an ink border and a tick, so it reads in greyscale and never by blue alone (DSN-04).
// The voice sample of a character (VOC-48) comes with stage 3.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { ProfileController } from "@jana/client-core";
import type { Settings as SettingsRecord } from "@jana/contracts-ts";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, ErrorState, FIELD_LOOK, Loader } from "../primitives/index.js";
import { WelcomeCard } from "../setup/Welcome.js";
import { Character } from "./Character.js";
import { CHARACTER_IDS, DEFAULT_CHARACTER, characterOf, slugOf, type CharacterId } from "./characters.js";
import { tutorAvatarOf, tutorNameOf, useCharacterName } from "./look.js";

export type PickError = "name_refused" | "failed";

/** A gallery or test start for the pick: a draft not yet saved, and the error it met. */
export interface PickSeed {
  avatar?: CharacterId;
  name?: string;
  error?: PickError;
}

/** The pick's state over the profile's settings: the saved tutor, the draft, saving and its error. */
export function useTutorPick(profiles: ProfileController, settings: SettingsRecord | null, seed: PickSeed = {}) {
  const nameOf = useCharacterName();
  const saved = { avatar: tutorAvatarOf(settings), name: tutorNameOf(settings) ?? nameOf(tutorAvatarOf(settings)) };
  const [draft, setDraft] = useState({ avatar: seed.avatar ?? saved.avatar, name: seed.name ?? saved.name });
  const [error, setError] = useState<PickError | null>(seed.error ?? null);
  const [saving, setSaving] = useState(false);
  // What the core keeps becomes the draft again whenever it changes (a save answered, another profile opened).
  const last = useRef(`${saved.avatar}\n${saved.name}`);
  useEffect(() => {
    const key = `${saved.avatar}\n${saved.name}`;
    if (key === last.current) return;
    last.current = key;
    setDraft({ avatar: saved.avatar, name: saved.name });
  }, [saved.avatar, saved.name]);
  const defaults = CHARACTER_IDS.map(nameOf);
  const choose = (avatar: CharacterId) => {
    setError(null);
    setDraft((d) => ({ avatar, name: d.name.trim() === "" || defaults.includes(d.name.trim()) ? nameOf(avatar) : d.name }));
  };
  const rename = (name: string) => {
    setError(null);
    setDraft((d) => ({ ...d, name }));
  };
  /** One settings patch with the character and the name; resolves true once the core keeps them. */
  const save = (avatar: CharacterId, name: string): Promise<boolean> => {
    setError(null);
    setSaving(true);
    return profiles.setPreferences({ tutor_avatar: avatar, tutor_name: name.trim() }).then(
      () => true,
      (e: unknown) => {
        setError((e as { code?: string }).code === "validation_failed" ? "name_refused" : "failed");
        return false;
      },
    ).finally(() => setSaving(false));
  };
  const jana = { avatar: DEFAULT_CHARACTER, name: nameOf(DEFAULT_CHARACTER) };
  return {
    saved, draft, error, saving, jana, choose, rename,
    changed: draft.avatar !== saved.avatar || draft.name.trim() !== saved.name,
    saveDraft: () => save(draft.avatar, draft.name),
    reset: () => {
      setDraft(jana);
      return save(jana.avatar, jana.name);
    },
  };
}

type Pick = ReturnType<typeof useTutorPick>;

/** The six tiles, the name field with its help and error, and the actions the caller places under them. */
export function TutorPickFields({ pick, actions }: { pick: Pick; actions: ReactNode }) {
  const t = useT();
  const nameOf = useCharacterName();
  const id = useId();
  const refused = pick.error === "name_refused";
  return (
    <div className="flex w-full flex-col gap-5 text-start">
      <div className="flex flex-col gap-3">
        <p id={`${id}-legend`} className="text-ui font-medium text-ink">{t("tutor.pick.character")}</p>
        <div role="radiogroup" aria-labelledby={`${id}-legend`} className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {CHARACTER_IDS.map((choice) => {
            const slug = slugOf(choice);
            const selected = choice === pick.draft.avatar;
            return (
              <label key={choice} data-character-tile={slug}
                className={`relative flex cursor-pointer flex-col items-center gap-1 rounded-card border p-3 text-center has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-focus ${selected ? "border-ink bg-wash" : "border-line hover:bg-wash"}`}>
                <input type="radio" name={`${id}-character`} checked={selected} disabled={pick.saving} onChange={() => pick.choose(choice)}
                  aria-labelledby={`${id}-${slug}-name`} aria-describedby={`${id}-${slug}-hint`} className="sr-only" />
                {selected && <span data-selected-mark="" className="absolute end-2 top-2 inline-flex text-ink"><Icon name="verified" size={16} /></span>}
                <Character id={choice} size={64} animate={false} />
                <span id={`${id}-${slug}-name`} data-tutor-name="" className={`text-ui text-ink ${selected ? "font-semibold" : ""}`}>{nameOf(choice)}</span>
                <span id={`${id}-${slug}-hint`} className="text-meta text-muted">{t(`tutor.character.${slug}.hint`)}</span>
              </label>
            );
          })}
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-name`} className="text-ui font-medium text-ink">{t("settings.tutor.rename")}</label>
        <input id={`${id}-name`} value={pick.draft.name} maxLength={40} onChange={(event) => pick.rename(event.target.value)}
          aria-describedby={refused ? `${id}-help ${id}-error` : `${id}-help`} {...(refused ? { "aria-invalid": true } : {})}
          className={`${FIELD_LOOK} max-w-sm`} />
        <p id={`${id}-help`} className="text-meta text-muted">{t("tutor.pick.name_help")}</p>
        {refused && <div id={`${id}-error`}><ErrorState message={t("settings.tutor.name_refused")} /></div>}
      </div>
      {pick.error === "failed" && <ErrorState message={t("settings.tutor.failed")} />}
      <div className="flex flex-wrap items-center gap-3">{actions}</div>
    </div>
  );
}

/** Settings → Tutor's pick (TABLE UXS-46a): Save keeps the draft; "Reset to Jana" keeps Jana at once. */
export function SettingsTutorPick({ profiles, settings, seed }: { profiles: ProfileController; settings: SettingsRecord | null; seed?: PickSeed }) {
  const t = useT();
  const pick = useTutorPick(profiles, settings, seed);
  const atJana = pick.saved.avatar === pick.jana.avatar && pick.saved.name === pick.jana.name;
  return (
    <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); if (pick.draft.name.trim() !== "") void pick.saveDraft(); }}>
      <TutorPickFields pick={pick} actions={(
        <>
          <Button type="submit" tone="primary" disabled={pick.saving || !pick.changed || pick.draft.name.trim() === ""}>{t("settings.tutor.save")}</Button>
          <Button tone="ghost" disabled={pick.saving || (atJana && !pick.changed)} onClick={() => void pick.reset()}><span data-tutor-name="">{t("settings.tutor.reset", { name: pick.jana.name })}</span></Button>
        </>
      )} />
    </form>
  );
}

/** The interview's opening pick (UXS-10 step 2): choose a tutor, or "Keep Jana" and go on. Nothing here ever blocks
 * the hand-off: while the settings load the pick can still be skipped, and a core that keeps no tutor choice (before
 * API 1.7) goes straight on. */
export function FirstRunTutorPick({ profiles, settings, onDone, seed }: { profiles: ProfileController; settings: SettingsRecord | null; onDone: () => void; seed?: PickSeed }) {
  const t = useT();
  const id = useId();
  const pick = useTutorPick(profiles, settings, seed);
  const heading = useRef<HTMLHeadingElement>(null);
  const finished = useRef(onDone);
  finished.current = onDone;
  const unsupported = settings !== null && !("tutor_name" in settings);
  useEffect(() => {
    if (unsupported) finished.current();
  }, [unsupported]);
  useEffect(() => heading.current?.focus(), []);
  // The words that name the tutor are marked as such: the tutor's name is not a brand placement (DSN-25, D105).
  const keep = <span data-tutor-name="">{t("tutor.pick.keep", { name: pick.jana.name })}</span>;
  const choose = () => {
    if (!pick.changed) return onDone();
    if (pick.draft.name.trim() === "") return;
    void pick.saveDraft().then((ok) => { if (ok) finished.current(); });
  };
  return (
    <WelcomeCard labelledBy={id} data-first-run-step="tutor">
      <div className="flex flex-col items-center gap-2">
        <Character id={characterOf(pick.draft.avatar)} size={72} />
        <h1 id={id} ref={heading} tabIndex={-1} className="text-display">{t("tutor.pick.title")}</h1>
        <p className="text-body text-muted">{t("tutor.pick.intro")}</p>
      </div>
      {settings === null ? <Loader label={t("tutor.pick.loading")} /> : (
        <form className="w-full" onSubmit={(event) => { event.preventDefault(); choose(); }}>
          <TutorPickFields pick={pick} actions={(
            <>
              {/* Choose is always available: with nothing changed it keeps the selected tutor, as "Keep Jana" does, so a
                  learner who likes the tutor shown is never left with a button that does nothing (§10.6). */}
              <Button type="submit" tone="primary" disabled={pick.saving || pick.draft.name.trim() === ""}>{t("tutor.pick.choose")}</Button>
              <Button tone="ghost" disabled={pick.saving} onClick={onDone}>{keep}</Button>
            </>
          )} />
        </form>
      )}
      {settings === null && <Button tone="ghost" onClick={onDone}>{keep}</Button>}
    </WelcomeCard>
  );
}
