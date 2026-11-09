// The study screen (UXS-07, TABLE UXS-46a's Online features; PRV-05, PRV-06, D87, EVL-22): below the online features,
// the open profile's research scopes, each a switch of its own with its plain-words consent line (TABLE PRV-05a). A new
// grant asks first: the student ticks their own agreement and "Turn on" stays disabled until it is ticked
// (`student_18_plus`). Research participation is restricted to learners aged 18 or over (W5), so no guardian agreement is offered: a profile
// whose band does not read as 18 or over (`GET /v1/profiles/{id}`, D93) is told it can't take part, and the core
// refuses it either way. Any profile pauses its consent and switches it back on in one step, or withdraws it (D87). While
// `research_use` is on, the research export (TABLE DAT-10a) takes the study's approvals from the study host (the
// school's approval reference and the registered protocol reference) and the student's own assent, and saves an
// encrypted file for the study team; nothing is sent from here.
import { useEffect, useId, useState } from "react";
import {
  RESEARCH_SCOPES, agreesForItself, readConsents, readProfile, researchExport, switchConsent, type ChooseInstallSource, type ConsentState, type ProfileController,
} from "@jana/client-core";
import { useT } from "../i18n.js";
import { Icon } from "../icons.js";
import { Button, ConfirmDialog, Dialog, Disclosure, ErrorState, FIELD_LOOK, Notice, Skeleton, useToggle } from "../primitives/index.js";

function ResearchPrivacy({ scope }: { scope: string }) {
  const t = useT(), [open, toggle] = useToggle(false);
  return <Disclosure open={open} onToggle={toggle} summary={t("settings.online.privacy_details")} label={t("settings.online.privacy_for", { feature: t(`settings.study.scope.${scope}`) })}>
    <p className="pb-1 text-meta text-muted">{t(`settings.study.consent.${scope}`)}</p>
  </Disclosure>;
}

/** The agreement before a new grant: the consent line, the student's own agreement, and "Turn on" once it is ticked.
 * A profile that is not 18 or over is told it can't take part; it is never offered a guardian's agreement (W5). */
export function StudyAgreement({ scope, adult, open, busy, refused, onCancel, onAgree }: {
  scope: string; adult: boolean; open: boolean; busy: boolean; refused: string | null; onCancel: () => void; onAgree: () => void;
}) {
  const t = useT();
  const id = useId();
  const [agreed, setAgreed] = useState(false);
  useEffect(() => {
    if (open) setAgreed(false);
  }, [open]);
  return (
    <Dialog open={open} heading={t(`settings.study.scope.${scope}`)} closeLabel={t("action.cancel")} onClose={onCancel}>
      <div className="flex flex-col gap-4" data-study-agreement={scope}>
        <p className="text-body text-ink">{t(`settings.study.consent.${scope}`)}</p>
        <p className="text-ui text-muted">{t("settings.study.what")}</p>
        {adult ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-ui font-medium text-ink">{t("settings.study.who")}</legend>
            <label htmlFor={`${id}-adult`} className="flex min-h-11 cursor-pointer items-start gap-3 rounded-control border border-line-strong bg-canvas px-4 py-3 text-ui text-ink has-[:checked]:border-ink has-[:checked]:bg-wash">
              <input id={`${id}-adult`} type="checkbox" className="mt-0.5 size-4 shrink-0" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} data-agree="adult" />
              <span>{t("settings.study.agree.adult")}</span>
            </label>
          </fieldset>
        ) : <Notice message={t("settings.study.needs_guardian")} />}
        {adult && <p className="text-meta text-muted">{t("settings.study.assent")}</p>}
        {refused !== null && <ErrorState message={t(refused)} />}
        <div className="flex justify-end gap-2">
          <Button onClick={onCancel}>{t("action.cancel")}</Button>
          <Button tone="primary" disabled={!adult || !agreed || busy} onClick={() => adult && agreed && onAgree()}>{t("settings.online.turn_on")}</Button>
        </div>
      </div>
    </Dialog>
  );
}

/** The research export with the study host's approvals (EVL-22, `CreateExport.research_approval`). */
export function ResearchExport({ profiles, chooseSource }: { profiles: ProfileController; chooseSource: ChooseInstallSource }) {
  const t = useT();
  const [school, setSchool] = useState("");
  const [protocol, setProtocol] = useState("");
  const [assent, setAssent] = useState(false);
  const [state, setState] = useState<"idle" | "saving" | "saved" | { failed: string }>("idle");
  const ready = school.trim() !== "" && protocol.trim() !== "" && assent && state !== "saving";
  const save = async () => {
    const folder = await chooseSource("folder").catch(() => null);
    if (folder === null) return;
    setState("saving");
    try {
      await researchExport(profiles.commands, profiles.scope().profileId, folder, { school_approval_ref: school, protocol_ref: protocol });
      setState("saved");
    } catch (error) {
      setState({ failed: (error as { code?: string }).code ?? "internal_error" });
    }
  };
  return (
    <form data-research-export="" className="flex flex-col gap-3 border-t border-line pt-3" onSubmit={(event) => {
      event.preventDefault();
      if (ready) void save();
    }}>
      <h4 className="text-ui font-semibold text-ink">{t("settings.study.export.title")}</h4>
      <p className="text-meta text-muted">{t("settings.study.export.body")}</p>
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
        {t("settings.study.export.school")}
        <input className={FIELD_LOOK} autoComplete="off" value={school} maxLength={120} onChange={(e) => setSchool(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1.5 text-meta font-medium text-muted">
        {t("settings.study.export.protocol")}
        <input className={FIELD_LOOK} autoComplete="off" value={protocol} maxLength={120} onChange={(e) => setProtocol(e.target.value)} />
      </label>
      <p className="text-meta text-faint">{t("settings.study.export.refs_help")}</p>
      <label className="flex items-start gap-3 text-ui text-ink">
        <input type="checkbox" className="mt-0.5 size-4 shrink-0" checked={assent} onChange={(e) => setAssent(e.target.checked)} />
        <span>{t("settings.study.export.assent")}</span>
      </label>
      <div><Button type="submit" disabled={!ready}>{t("settings.study.export.save")}</Button></div>
      {state === "saved" && <Notice message={t("settings.study.export.saved")} />}
      {typeof state === "object" && <ErrorState message={t(state.failed === "validation_failed" ? "settings.study.export.refused" : `error.${state.failed}`)} />}
    </form>
  );
}

/** The open profile's research scopes, each with its switch, below the online features. */
export function Studies({ profiles, chooseSource }: { profiles: ProfileController; chooseSource?: ChooseInstallSource | undefined }) {
  const t = useT();
  const [consents, setConsents] = useState<ConsentState[] | "loading" | "failed">("loading");
  const [adult, setAdult] = useState<boolean | null>(null); // unknown until read: no agreement is offered before
  const [asking, setAsking] = useState<string | null>(null);
  const [withdrawing, setWithdrawing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    let profileId: string;
    try {
      profileId = profiles.scope().profileId;
    } catch {
      setConsents("failed");
      return;
    }
    readConsents(profiles.commands, profileId).then((list) => live && setConsents(list), () => live && setConsents("failed"));
    setAdult(null);
    readProfile(profiles.commands, profileId).then((profile) => live && setAdult(agreesForItself(profile)), () => live && setAdult(false)); // unread: a minor
    return () => {
      live = false;
    };
  }, [profiles]);
  if (consents === "loading" || adult === null) return <Skeleton lines={2} />;
  if (consents === "failed") return <ErrorState message={t("settings.study.unavailable")} />;
  const scopes = RESEARCH_SCOPES.filter((scope) => consents.some((c) => c.scope === scope));
  if (scopes.length === 0) return null;
  const replace = (next: ConsentState) => setConsents((list) => Array.isArray(list) ? [...list.filter((c) => c.scope !== next.scope), next] : list);
  const change = async (scope: string, action: "on" | "pause" | "withdraw"): Promise<boolean> => {
    setBusy(true);
    setError(null);
    setRefused(null);
    try {
      replace(await switchConsent(profiles.commands, profiles.scope().profileId, scope, action));
      return true;
    } catch (e) {
      const code = (e as { code?: string }).code ?? "internal_error";
      // A new grant the core refused: a profile not 18 or over can't take part (PRV-06, W5).
      if (asking !== null) setRefused(code === "validation_failed" ? "settings.study.needs_guardian" : `error.${code}`);
      else setError(code);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const research = consents.find((c) => c.scope === "research_use");
  return (
    <section data-studies="" aria-labelledby="studies-title" className="flex flex-col gap-3 border-t border-line pt-3">
      <h4 id="studies-title" className="flex items-center gap-2 text-ui font-semibold text-ink"><Icon name="study" size={16} />{t("settings.study.title")}</h4>
      <p className="text-meta text-muted">{t("settings.study.intro")}</p>
      <ul className="flex flex-col gap-3">
        {scopes.map((scope) => {
          const c = consents.find((x) => x.scope === scope);
          const state = c === undefined || !c.granted ? "off" : c.paused ? "paused" : "on";
          return (
            <li key={scope} data-study-scope={scope} data-study-state={state} className="flex flex-col gap-2 rounded-card border border-line bg-canvas px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-ui font-medium text-ink">{t(`settings.study.scope.${scope}`)}</span>
                <span className="text-meta text-muted">{t(`settings.study.state.${state}`)}</span>
              </div>
              <p className="text-meta text-muted">{t(`settings.study.summary.${scope}`)}</p>
              <ResearchPrivacy scope={scope} />
              <div className="flex flex-wrap gap-2">
                {state === "off" && <Button size="sm" disabled={busy} onClick={() => { setRefused(null); setAsking(scope); }}>{t("settings.study.join")}</Button>}
                {state === "on" && <Button size="sm" disabled={busy} onClick={() => void change(scope, "pause")}>{t("settings.study.pause")}</Button>}
                {state === "paused" && <Button size="sm" disabled={busy} onClick={() => void change(scope, "on")}>{t("settings.study.resume")}</Button>}
                {state !== "off" && <Button size="sm" tone="ghost" disabled={busy} onClick={() => setWithdrawing(scope)}>{t("settings.study.withdraw")}</Button>}
              </div>
            </li>
          );
        })}
      </ul>
      {error !== null && <ErrorState message={t(`error.${error}`)} />}
      {research?.effective === true && chooseSource !== undefined && <ResearchExport profiles={profiles} chooseSource={chooseSource} />}
      <StudyAgreement scope={asking ?? "research_use"} adult={adult} open={asking !== null} busy={busy} refused={refused} onCancel={() => setAsking(null)}
        onAgree={() => {
          const scope = asking;
          if (scope !== null) void change(scope, "on").then((ok) => ok && setAsking(null));
        }} />
      <ConfirmDialog open={withdrawing !== null} message={t("settings.study.withdraw_confirm")} confirm={t("settings.study.withdraw")} cancel={t("action.cancel")} destructive
        onCancel={() => setWithdrawing(null)} onConfirm={() => {
          const scope = withdrawing;
          setWithdrawing(null);
          if (scope !== null) void change(scope, "withdraw");
        }} />
    </section>
  );
}
