// Settings → Online features and Settings → Profiles (TABLE UXS-46a, UXS-29): two sections, both open, with no PIN or
// elevation (D86, D87); online features carry the study screen's research consents below them.
import { useEffect, useState, type ReactNode } from "react";
import { profileAgeBand, type CheckinNotifier, type ChooseInstallSource, type FamilySettings, type ProfileController } from "@jana/client-core";
import type { CreateProfile, PackList } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Button, ConfirmDialog, Disclosure, ErrorState, Notice, useToggle } from "../primitives/index.js";
import { discOf } from "../profiles/Avatar.js";
import { useProfiles } from "../profiles/ProfilePicker.js";
import { AgeField } from "../setup/AgeField.js";
import { TutoringUsage } from "../composer/TutoringUsage.js";
import { useOptionalSession } from "../store.js";
import { OnlineFeatures } from "./OnlineFeatures.js";
import { Studies } from "./Studies.js";

/** The family sections of TABLE UXS-46a, each its own Settings section: Online features and Profiles (D87). */
export const FAMILY_GROUPS = ["online", "profiles"] as const;
const LEVELS = ["O", "A"] as const;
const SUBJECTS = ["mathematics", "physics", "chemistry", "biology", "accounting"] as const;

type Run = (work: () => Promise<unknown>) => void;

function AddStudent({ profiles, run }: { profiles: ProfileController; run: Run }) {
  const t = useT();
  const [open, toggle] = useToggle(false);
  const [name, setName] = useState("");
  const [age, setAge] = useState("");
  const [level, setLevel] = useState<CreateProfile["level"]>("O");
  const [subjects, setSubjects] = useState<string[]>([]);
  const count = useProfiles(profiles).profiles.length;
  // No subject ticked is a learner studying their own course and materials.
  const band = profileAgeBand(age);
  const ready = name.trim() !== "" && band !== null;
  const field = "min-h-8 rounded-control border border-line-strong bg-canvas px-3 text-body text-ink";
  return (
    <Disclosure summary={t("settings.profiles.add")} open={open} onToggle={toggle}>
      <form
        className="flex flex-col gap-2 pt-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!ready || band === null) return;
          const body: CreateProfile = { display_name: name.trim(), avatar: `avatar-${discOf("", count)}`, level, subjects, age_band: band };
          run(async () => {
            await profiles.family.addProfile(body);
            setName("");
            setAge("");
            setSubjects([]);
          });
        }}
      >
        <label className="flex flex-col gap-1 text-ui text-ink">
          {t("settings.profiles.name")}
          <input className={field} value={name} maxLength={40} onChange={(event) => setName(event.target.value)} />
        </label>
        <AgeField value={age} onChange={setAge} labelClass="flex flex-col gap-1 text-ui text-ink" />
        <label className="flex flex-col gap-1 text-ui text-ink">
          {t("settings.profiles.level")}
          <select className={field} value={level} onChange={(event) => setLevel(event.target.value as CreateProfile["level"])}>
            {LEVELS.map((l) => <option key={l} value={l}>{t(`settings.profiles.level_name.${l.toLowerCase()}`)}</option>)}
          </select>
        </label>
        <fieldset className="flex flex-wrap gap-3">
          <legend className="text-ui text-ink">{t("settings.profiles.subjects")}</legend>
          {SUBJECTS.map((subject) => (
            <label key={subject} className="flex items-center gap-1 text-ui text-ink">
              <input type="checkbox" checked={subjects.includes(subject)} onChange={(event) => setSubjects((s) => (event.target.checked ? [...s, subject] : s.filter((x) => x !== subject)))} />
              {t(`settings.profiles.subject.${subject}`)}
            </label>
          ))}
        </fieldset>
        <div>
          <Button type="submit" tone="primary" disabled={!ready}>{t("settings.profiles.create")}</Button>
        </div>
      </form>
    </Disclosure>
  );
}

function Profiles({ profiles, run }: { profiles: ProfileController; run: Run }) {
  const t = useT();
  const view = useProfiles(profiles);
  const [deleting, setDeleting] = useState<{ id: string; name: string } | null>(null);
  return (
    <>
      <ul className="flex flex-col gap-2">
        {view.profiles.map((profile) => (
          <li key={profile.profile_id} className="flex items-center gap-2 text-ui text-ink">
            <span className="flex-1">{profile.display_name}</span>
            {profile.profile_id === view.active && <Button size="sm" tone="ghost" onClick={() => setDeleting({ id: profile.profile_id, name: profile.display_name })}>{t("settings.profiles.delete")}</Button>}
          </li>
        ))}
      </ul>
      <AddStudent profiles={profiles} run={run} />
      <ConfirmDialog
        destructive
        open={deleting !== null}
        message={t("settings.profiles.delete_confirm", { name: deleting?.name ?? "" })}
        confirm={t("settings.profiles.delete")}
        cancel={t("action.cancel")}
        onCancel={() => setDeleting(null)}
        onConfirm={() => {
          const target = deleting;
          setDeleting(null);
          if (target !== null) run(() => profiles.family.deleteProfile(target.id));
        }}
      />
    </>
  );
}

/** Content (TABLE UXS-46a): a notice for each installed pack that failed verification (ARC-26, CUR-08). */
export function DamagedPacks({ family }: { family: FamilySettings }) {
  const t = useT();
  const [damaged, setDamaged] = useState<PackList["packs"]>([]);
  useEffect(() => {
    family.packs().then((packs) => setDamaged(packs.filter((p) => !p.signature_ok)), () => setDamaged([]));
  }, [family]);
  return (
    <>
      {damaged.map((pack, index) => (
        <Notice key={`${pack.pack_id}-${index}`} tone="warn" message={t("pack.damaged", { pack: pack.pack_id })} />
      ))}
    </>
  );
}

/** One family group's body: the section of TABLE UXS-46a it is ("online" is Online features, "profiles" is Profiles),
 * with its own error line. */
function Group({ id, children }: { id: (typeof FAMILY_GROUPS)[number]; children: (run: Run) => ReactNode }) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const run: Run = (work) => {
    setError(null);
    void work().catch((e: unknown) => setError((e as { code?: string }).code ?? "internal_error"));
  };
  return (
    <div data-family-group={id} className="flex flex-col gap-3">
      {children(run)}
      {error !== null && <ErrorState message={t(`error.${error}`)} />}
    </div>
  );
}

/** Settings → Online features (UXS-07): the switches, then the study screen's research consents (PRV-06, D87). */
function SettingsTutoringUsage() {
  return useOptionalSession() === null ? null : <TutoringUsage detail />;
}

export function OnlineFeaturesSection({ profiles, chooseSource, notifier }: { profiles: ProfileController; chooseSource?: ChooseInstallSource | undefined;
  /** The computer's notification permission, for Check-in notifications (SPEC §19.5.5). */ notifier?: Pick<CheckinNotifier, "permission"> | undefined }) {
  return (
    <Group id="online">
      {(run) => (
        <>
          <OnlineFeatures profiles={profiles} run={run} notifier={notifier} />
          <SettingsTutoringUsage />
          {/* The study screen: the open profile's research consents, below the features (UXS-07, PRV-06, D87). */}
          <Studies profiles={profiles} chooseSource={chooseSource} />
        </>
      )}
    </Group>
  );
}

/** Settings → Profiles (UXS-29): every student, removing the open one in one confirmed step, and adding a student. */
export function ProfilesSection({ profiles }: { profiles: ProfileController }) {
  return <Group id="profiles">{(run) => <Profiles profiles={profiles} run={run} />}</Group>;
}
