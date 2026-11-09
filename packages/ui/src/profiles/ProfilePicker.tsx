// The profile picker (UXS-09, D86, D87; direction A "Studio"): on paper with the lockup (DSN-25), the students as large
// avatars with their names; a tap opens that learner's isolated learning space, with no PIN anywhere. "Add profile" is
// open to anyone and asks for a name and an age, which is reduced to the learner’s age band;
// everything else takes first run's defaults (TABLE PLT-17b), corrected later in Settings → Profiles. A profile that cannot be opened says so on screen, whatever the reason, and a profile that
// closed after inactivity says why (SEC-19). When the profile store cannot be read, "Show recovery options" opens
// profile recovery (UXS-47). On a school lab computer the picker says plainly that anyone using it can open these
// profiles (D102); nothing locks them (D86).
import { useEffect, useId, useState, useSyncExternalStore, type ReactNode } from "react";
import { firstRunProfile, profileAgeBand, readLicence, sharedComputer, type ProfileController, type ProfilesView } from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, EmptyState, ErrorState, FIELD_LOOK, Loader, Notice } from "../primitives/index.js";
import { AgeField } from "../setup/AgeField.js";
import { StudyFocusField } from "../setup/StudyFocusField.js";
import { WelcomeCard } from "../setup/Welcome.js";
import { ProfileRecovery } from "./ProfileRecovery.js";
import { Avatar, discOf } from "./Avatar.js";

export function useProfiles(profiles: ProfileController) {
  return useSyncExternalStore(profiles.subscribe, profiles.getView, profiles.getView);
}

const TILE = "jana-press flex min-w-26 flex-col items-center gap-2.5 rounded-card px-2 py-3 text-ui font-medium text-ink hover:bg-wash disabled:cursor-not-allowed";

/** "Add profile": a name and a supported age, then the learner is added with first run's defaults for the
 * installed subjects. */
export function AddStudent({ profiles, onAdded }: { profiles: ProfileController; onAdded?: (profileId: string) => void }) {
  const t = useT();
  const count = useProfiles(profiles).profiles.length;
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [age, setAge] = useState("");
  const band = profileAgeBand(age);
  const [curriculum, setCurriculum] = useState(true);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className={TILE}>
        <span aria-hidden="true" className="flex size-16 items-center justify-center rounded-pill border border-dashed border-line-strong text-muted"><Icon name="add-student" /></span>
        {t("settings.profiles.create")}
      </button>
    );
  }
  return (
    <form className="flex w-full flex-col gap-3 text-start" onSubmit={(event) => {
      event.preventDefault();
      if (pending || name.trim() === "" || band === null) return;
      setPending(true);
      setFailed(null);
      void profiles.family.packs().catch(() => [])
        .then((packs) => profiles.family.addProfile(firstRunProfile(name, packs, count, curriculum, band)))
        .then((created) => {
          setName("");
          setAge("");
          setOpen(false);
          onAdded?.(created.profile_id);
        })
        .catch((error: unknown) => setFailed((error as { code?: string }).code ?? "internal_error"))
        .finally(() => setPending(false));
    }}>
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
        {t("first_run.name")}
        <input autoFocus className={FIELD_LOOK} autoComplete="off" maxLength={40} value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <AgeField value={age} onChange={setAge} disabled={pending} />
      <StudyFocusField curriculum={curriculum} onChange={setCurriculum} disabled={pending} />
      <div className="flex gap-2">
        <Button type="submit" tone="primary" disabled={pending || name.trim() === "" || band === null}>{t("settings.profiles.create")}</Button>
        <Button onClick={() => setOpen(false)}>{t("action.cancel")}</Button>
      </div>
      {failed !== null && <ErrorState message={t(`error.${failed}`)} />}
    </form>
  );
}

/** The picker from its view: the gallery renders this directly. */
export function ProfilePickerView({ view, chosen, onOpen, add, onRecovery, onRestore, shared = false }: {
  view: ProfilesView; chosen: string | null; onOpen: (profileId: string) => void; add?: ReactNode; onRecovery?: () => void;
  /** A school lab computer (D102): anyone using it can open these profiles, said plainly. */
  shared?: boolean;
  onRestore?: () => void;
}) {
  const t = useT();
  const id = useId();
  if (view.status === "loading") return <Loader label={t("profiles.loading")} />;
  const failure = (profileId: string, name: string): string | null => {
    const error = view.opening[profileId]?.error ?? null;
    if (error === null) return null;
    // Saved by a newer app (DAT-12, G51): the core refused before issuing a token and left the files as they were.
    if (error === "upgrade_required") return t("profile.newer_app", { name });
    if (error === "rate_limited") return t("error.rate_limited");
    return t("profiles.open_failed", { name });
  };
  return (
    <WelcomeCard brand labelledBy={id} data-profile-picker="">
      <div className="flex flex-col gap-2">
        <h1 id={id} className="text-display">{t("profiles.title")}</h1>
        {view.status !== "failed" && view.profiles.length > 0 && <p className="text-body text-muted">{t("profiles.subtitle")}</p>}
      </div>
      {view.ended === "profile_token_expired" && <Notice message={t("error.profile_token_expired")} />}
      {shared && <Notice message={t("lab.shared_notice")} />}
      {view.status === "failed" ? (
        <ErrorState message={t("profiles.unreadable")} {...(onRecovery !== undefined ? { action: { label: t("recovery.show"), onClick: onRecovery } } : {})} />
      ) : (
        <>
          {view.profiles.length === 0 && <EmptyState message={t("profiles.empty")} />}
          <div className="flex flex-wrap justify-center gap-2">
          <ul className="contents">
            {view.profiles.map((profile, order) => (
              <li key={profile.profile_id}>
                <button type="button" aria-busy={chosen === profile.profile_id} disabled={chosen !== null} onClick={() => onOpen(profile.profile_id)} className={`${TILE} disabled:opacity-60`}>
                  <Avatar name={profile.display_name} disc={discOf(profile.avatar, order)} size="xl" />
                  {profile.display_name}
                </button>
              </li>
            ))}
          </ul>
          {add !== undefined && <div className="flex has-[form]:basis-full">{add}</div>}
          </div>
          {view.profiles.map((profile) => {
            const message = failure(profile.profile_id, profile.display_name);
            return message === null ? null : (
              <div key={profile.profile_id} className="w-full text-start">
                <ErrorState message={message} {...(view.opening[profile.profile_id]?.error === "upgrade_required" ? {} : { action: { label: t("action.try_again"), onClick: () => onOpen(profile.profile_id) } })} />
              </div>
            );
          })}
        </>
      )}
      {view.status === "ready" && onRestore !== undefined && <Button tone="ghost" onClick={onRestore}>{t("recovery.restore")}</Button>}
    </WelcomeCard>
  );
}

export function ProfilePicker({ profiles, onUnlocked, chooseArchive }: {
  profiles: ProfileController; onUnlocked?: (profileId: string) => void;
  /** A file dialog for a profile archive (UXS-47's restore); absent where the shell offers none. */
  chooseArchive?: (() => Promise<string | null>) | undefined;
}) {
  const view = useProfiles(profiles);
  const [chosen, setChosen] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<"recover" | "restore" | null>(null);
  const [shared, setShared] = useState(false);
  // Read once there are profiles to pick from (D102): first run, with none yet, reads no licence at all (PLT-17).
  const picking = view.status !== "loading" && view.status !== "failed" && view.profiles.length > 0;
  useEffect(() => {
    if (!picking) return;
    let live = true;
    readLicence(profiles.commands).then((status) => live && setShared(sharedComputer(status)), () => undefined);
    return () => {
      live = false;
    };
  }, [profiles, picking]);
  const open = async (profileId: string) => {
    setChosen(profileId);
    const ok = await profiles.unlock(profileId);
    setChosen(null);
    if (ok) onUnlocked?.(profileId);
  };
  if (recovery !== null) {
    return <ProfileRecovery mode={recovery} profiles={profiles} chooseArchive={chooseArchive} onDone={() => { setRecovery(null); void profiles.load(); }} />;
  }
  return <ProfilePickerView view={view} chosen={chosen} onOpen={(id) => void open(id)} add={<AddStudent profiles={profiles} />} onRecovery={() => setRecovery("recover")}
    {...(chooseArchive === undefined ? {} : { onRestore: () => setRecovery("restore") })} shared={shared} />;
}
