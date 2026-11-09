// First run's one step, "Who's learning?" (UXS-10 step 1, TABLE PLT-17a, D84, D86; direction A "Studio"): on paper
// with the lockup (DSN-25), a first name or nickname and an age, the line that everything stays on this
// computer, and "Start learning". Age is checked locally and only its coarse band is sent. Nothing else is asked: no PIN, code, licence or account;
// level, subjects and the tutor model take TABLE PLT-17b's defaults, the subjects from the packs installed on this
// computer. The learner is added through `POST /v1/profiles`, opens, and the hand-off follows; more learners are added
// from the profile picker. Quitting part way resumes here on the next launch with the learners already added shown
// (`existing`, UXS-10).
import { useEffect, useId, useRef, useState } from "react";
import { firstRunProfile, profileAgeBand, type ProfileController } from "@jana/client-core";
import type { PackList } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, ErrorState, FIELD_LOOK } from "../primitives/index.js";
import { Avatar, discOf } from "../profiles/Avatar.js";
import { AgeField } from "./AgeField.js";
import { StudyFocusField } from "./StudyFocusField.js";
import { WelcomeCard } from "./Welcome.js";

export function FirstRun({ profiles, packs, existing = [], onStarted, onRestore }: {
  profiles: ProfileController;
  /** The installed packs (`GET /v1/packs`): the new students' subjects and level. */
  packs: PackList["packs"];
  /** Students added before first run was left part way (UXS-10 resume): shown, not added again. */
  existing?: readonly { profileId: string; name: string; avatar: string }[];
  /** The first student was added and opened: the hand-off follows. */
  /** `curriculum` is false when a new learner chose their own course and materials. */
  onStarted: (name: string, curriculum: boolean) => void;
  onRestore?: () => void;
}) {
  const t = useT();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const [name, setName] = useState("");
  const [age, setAge] = useState("");
  const [curriculum, setCurriculum] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const added = useRef<string | null>(null); // the profile made from the form, so a retry does not add it twice
  useEffect(() => heading.current?.focus(), []);
  const asking = existing.length === 0;
  const band = profileAgeBand(age);
  const ready = !asking || (name.trim() !== "" && band !== null);
  const start = async () => {
    setPending(true);
    setError(null);
    try {
      if (asking && added.current === null) {
        if (band === null) throw new Error("validation_failed");
        added.current = (await profiles.family.addProfile(firstRunProfile(name, packs, 0, curriculum, band))).profile_id;
      }
      const opening = existing[0] !== undefined ? { id: existing[0].profileId, name: existing[0].name } : { id: added.current ?? undefined, name: name.trim() };
      if (opening.id === undefined || !(await profiles.unlock(opening.id))) throw new Error("open_failed");
      onStarted(opening.name, curriculum);
    } catch (failure) {
      setError((failure as { code?: string }).code ?? "internal_error");
    } finally {
      setPending(false);
    }
  };
  return (
    <WelcomeCard brand labelledBy={id} data-first-run-step="who">
      <div className="flex flex-col gap-2">
        <h1 id={id} ref={heading} tabIndex={-1} className="text-display">{t("first_run.title")}</h1>
        <p className="text-body text-muted">{t("first_run.privacy")}</p>
      </div>
      {existing.length > 0 && (
        <ul aria-label={t("first_run.added")} className="flex flex-wrap justify-center gap-2" data-first-run-existing="">
          {existing.map((p, order) => (
            <li key={p.profileId} className="flex min-w-26 flex-col items-center gap-2 rounded-card px-2 py-3 text-ui font-medium">
              <Avatar name={p.name} disc={discOf(p.avatar, order)} size="xl" />
              {p.name}
            </li>
          ))}
        </ul>
      )}
      <form aria-labelledby={id} className="flex w-full flex-col gap-4 text-start"
        onSubmit={(event) => {
          event.preventDefault();
          if (!pending && ready) void start();
        }}>
        {asking && (
          <>
            <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
              {t("first_run.name")}
              <input className={FIELD_LOOK} autoComplete="off" maxLength={40} value={name} disabled={added.current !== null} onChange={(event) => setName(event.target.value)} />
            </label>
            <AgeField value={age} onChange={setAge} disabled={pending || added.current !== null} />
            <StudyFocusField curriculum={curriculum} onChange={setCurriculum} disabled={pending || added.current !== null} />
          </>
        )}
        <Button type="submit" tone="primary" disabled={pending || !ready}>{t("first_run.start")}<Icon name="go-next" size={16} /></Button>
        {error !== null && <ErrorState message={t(`error.${error === "open_failed" ? "internal_error" : error}`)} />}
      </form>
      {onRestore !== undefined && <Button tone="ghost" disabled={pending} onClick={onRestore}>{t("recovery.restore")}</Button>}
    </WelcomeCard>
  );
}
