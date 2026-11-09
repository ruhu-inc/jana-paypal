// Settings → Tutor (TABLE UXS-46a, UXS-54, PRM-02, PRM-03, D47, D99, D103, D105): the one pick of the tutor, per
// profile. The tutor is Jana with the Jana character by default; the student picks one of the six characters of TABLE
// DSN-26a and its name here, and the core keeps both with the open profile's settings (`tutor_avatar`, `tutor_name`, in
// one patch). The core checks a chosen name against the display-name rules (PRV-03) and refuses one that fails. The
// name labels the tutor's turns and fills the tutor's own words (the soul's placeholder, PRM-03); the character marks
// its turns. A core older than API 1.7 does not return the keys, so the default is shown and choosing is said to come
// later.
import type { ProfileController } from "@jana/client-core";
import type { Settings as SettingsRecord } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Character } from "../tutor/Character.js";
import { SettingsTutorPick, type PickSeed } from "../tutor/TutorPick.js";
import { tutorAvatarOf, tutorNameOf, useCharacterName } from "../tutor/look.js";

export {
  DEFAULT_TUTOR_AVATAR, TUTOR_AVATARS, TutorLookProvider, tutorAvatarOf, tutorNameOf, useTutorAvatar, useTutorName,
} from "../tutor/look.js";

/** The tutor's character as a still mark: `mark` beside the name label of a turn, `lg` in Settings. Decorative: the
 * words beside it name the tutor. */
export function TutorAvatar({ avatar, size = "mark" }: { avatar: string; size?: "mark" | "lg" }) {
  return <Character id={avatar} size={size === "mark" ? 16 : 48} animate={false} />;
}

/** `seed` starts the pick from a draft and an error (the gallery's states); the app passes none. */
export function TutorSection({ profiles, settings, seed }: { profiles: ProfileController; settings: SettingsRecord | null; seed?: PickSeed }) {
  const t = useT();
  const nameOf = useCharacterName();
  const stored = settings !== null && "tutor_name" in settings;
  const avatar = tutorAvatarOf(settings);
  const name = tutorNameOf(settings) ?? nameOf(avatar);
  return (
    <div className="flex flex-col gap-5 py-4" data-tutor-identity={stored ? "stored" : "default"}>
      <div className="flex items-center gap-4">
        <Character id={avatar} size={56} />
        <div className="flex flex-col gap-1">
          <p className="text-ui text-ink" data-tutor-name={name}>{t("settings.tutor.name", { name })}</p>
          <p className="text-meta text-muted">{t("tutor.pick.same_tutor")}</p>
        </div>
      </div>
      {stored ? <SettingsTutorPick profiles={profiles} settings={settings} {...(seed !== undefined ? { seed } : {})} /> : <p className="text-meta text-muted">{t("settings.tutor.later")}</p>}
    </div>
  );
}
