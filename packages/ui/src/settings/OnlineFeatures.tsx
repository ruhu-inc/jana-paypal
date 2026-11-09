// Settings → Online features, inside family settings (UXS-07, ARC-11): one row per feature the core lists for this
// stage, grouped by purpose with short summaries. Expandable privacy details retain what leaves this computer and
// what happens offline (TABLE UXS-07a). All are off on a fresh install, with "Turn off all online features". A switch is the feature's only setting
// (D53). Turning one on asks first, naming the data; for a feature that sends learner or usage data the confirmation
// also shows its consent line (PRV-05) and the guardian attestation (PRV-06), and "Turn on" records the consent with the
// switch (the core does both in `PATCH /v1/connectivity`); turning it off withdraws the consent. Downloads and update
// checks need only the switch. The cloud tutor is not a switch (D70): subscribing turns it on, the guardian's consent
// for each profile under 18 is recorded with it, and ending the subscription turns it off.
//
// Personal tutor memory online (SPEC §19.6, JM-D21) is the open profile's own consent, like Online study materials: off
// by default, with §19.6's "What leaves this computer" line, and it needs the online tutor, so while that is off it
// cannot be turned on and says why. Under it, Check-in notifications (JM-D24): the profile's `checkin_notifications`,
// default off; turning it on asks the computer for permission first, and a refusal leaves it off with a notice.
import { useEffect, useState } from "react";
import {
  CONSENT_FEATURES, CONSENT_NOTICE_VERSIONS, ONLINE_FEATURES, type CheckinNotifier, type FamilySettings, type CloudSubscription, type ConnectivityReply,
  agreesForItself, readProfile, readConsents, switchConsent, type ConsentState, type ProfileController,
} from "@jana/client-core";
import type { ModelCatalogue, OnlineFeature } from "@jana/contracts-ts";
import { useT } from "../i18n.js";
import { Button, ConfirmDialog, Disclosure, Notice, Skeleton, Switch, useToggle } from "../primitives/index.js";
import { useProfiles } from "../profiles/ProfilePicker.js";

type Run = (work: () => Promise<unknown>) => void;
type Sub = CloudSubscription | "loading" | "failed";

const FEATURE_GROUPS: { id: string; features: OnlineFeature[] }[] = [
  { id: "learning", features: ["cloud_tier", "cloud_voice", "study_materials", "tutor_memory_online"] },
  { id: "downloads", features: ["downloads", "update_checks"] },
  { id: "sharing", features: ["cloud_backup", "multi_device", "parent_remote_view"] },
  { id: "diagnostics", features: ["telemetry", "crash_reports"] },
];

function FeaturePrivacy({ feature }: { feature: OnlineFeature }) {
  const t = useT(), [open, toggle] = useToggle(false);
  return <div data-online-privacy={feature}>
    <Disclosure open={open} onToggle={toggle} summary={t("settings.online.privacy_details")} label={t("settings.online.privacy_for", { feature: t(`settings.online.feature.${feature}`) })}>
      <div className="flex flex-col gap-2 pb-1 text-meta text-muted">
        <p>{t("settings.online.leaves_label")} {t(`settings.online.leaves.${feature}`)}</p>
        <p>{t("settings.online.offline_label")} {t(`settings.online.offline.${feature}`)}</p>
      </div>
    </Disclosure>
  </div>;
}

function Cloud({ subscription, names, selfConsent, known, inferenceAllowed, onConsent, onEnd }: { subscription: Sub; names: string[]; selfConsent: boolean; known: boolean; inferenceAllowed: boolean; onConsent: () => void; onEnd: () => void }) {
  const t = useT();
  // Whose consent is awaited depends on the profile, so nothing is said about it until the profile is read.
  if (subscription === "loading" || (subscription !== "failed" && subscription.state === "pending_consent" && !known)) return <Skeleton lines={1} />;
  if (subscription === "failed") return <p className="text-ui text-ink">{t("settings.online.cloud.unavailable")}</p>;
  if (subscription.state === "active") {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-ui text-ink">{t("settings.online.cloud.active")}</p>
        {subscription.period_end !== null && <p className="text-meta text-muted">{t("settings.online.cloud.period_end", { end: new Date(subscription.period_end) })}</p>}
        {subscription.allowance_used_up && <p className="text-meta text-muted">{t("settings.online.cloud.allowance_used_up")}</p>}
        <div>
          <Button onClick={onEnd}>{t("settings.online.cloud.turn_off")}</Button>
        </div>
      </div>
    );
  }
  if (subscription.state === "pending_consent") {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-ui text-ink">{t(selfConsent ? "settings.online.cloud.pending_self" : "settings.online.cloud.pending", { names: names.join(", ") })}</p>
        <div>
          <Button tone="primary" disabled={!inferenceAllowed} onClick={onConsent}>{t("settings.online.cloud.give_consent")}</Button>
        </div>
      </div>
    );
  }
  return <p className="text-ui text-ink">{t("settings.online.cloud.off")}</p>;
}

/** Check-in notifications (SPEC §19.5.5): shown once the open profile's settings carry the key (a core with check-ins). */
function CheckinNotifications({ profiles, notifier }: { profiles: ProfileController; notifier?: Pick<CheckinNotifier, "permission"> | undefined }) {
  const t = useT();
  const view = useProfiles(profiles);
  const [problem, setProblem] = useState<"denied" | "failed" | null>(null);
  const [busy, setBusy] = useState(false);
  const settings = view.settings;
  if (view.active === null || settings === null || settings.checkin_notifications === undefined) return null;
  const change = async (on: boolean) => {
    setProblem(null);
    setBusy(true);
    try {
      if (on && notifier !== undefined && await notifier.permission().catch(() => "denied" as const) !== "granted") {
        setProblem("denied");
        return;
      }
      await profiles.setPreferences({ checkin_notifications: on });
    } catch {
      setProblem("failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div data-checkin-notifications="" className="flex flex-col gap-1 border-t border-line pt-2">
      <Switch label={t("settings.checkin_notifications.label")} help={t("settings.checkin_notifications.help")} checked={settings.checkin_notifications} disabled={busy}
        onChange={(on) => void change(on)} />
      {problem !== null && <Notice tone="warn" message={t(problem === "denied" ? "settings.checkin_notifications.denied" : "settings.checkin_notifications.failed")} />}
    </div>
  );
}

export function OnlineFeatures({ profiles, run, notifier }: { profiles: ProfileController; run: Run;
  /** The computer's notification permission (the desktop shell's), asked before check-in notifications are turned on. */
  notifier?: Pick<CheckinNotifier, "permission"> | undefined }) {
  const t = useT();
  const family: FamilySettings = profiles.family;
  const view = useProfiles(profiles);
  const known = view.profiles;
  // Unknown until the profile is read; a failed read receives the conservative school-age controls.
  const [availability, setAvailability] = useState<ModelCatalogue["online_inference"]>();
  const [adult, setAdult] = useState<boolean | null>(null);
  const [studyConsent, setStudyConsent] = useState<ConsentState | null>(null);
  const [cloudConsent, setCloudConsent] = useState<ConsentState | null>(null);
  const [state, setState] = useState<ConnectivityReply | null>(null);
  const [subscription, setSubscription] = useState<Sub>("loading");
  const [asking, setAsking] = useState<OnlineFeature | null>(null);
  const [dialog, setDialog] = useState<"consent" | "end" | "all_off" | null>(null);
  useEffect(() => {
    family.connectivity().then(setState, () => setState(null));
    family.subscription().then(setSubscription, () => setSubscription("failed"));
  }, [family]);
  useEffect(() => {
    let live = true;
    setAdult(null);
    setAvailability(undefined);
    setStudyConsent(null);
    setCloudConsent(null);
    if (view.active !== null) {
      profiles.commands.get<ModelCatalogue>("/v1/models").then(
        (catalogue) => { if (live) setAvailability(catalogue.online_inference); }, () => undefined,
      );
      readConsents(profiles.commands, view.active).then(
        (consents) => { if (live) {
          setStudyConsent(consents.find((c) => c.scope === "study_materials") ?? null);
          setCloudConsent(consents.find((c) => c.scope === "cloud_tier") ?? null);
        } }, () => undefined,
      );
      readProfile(profiles.commands, view.active).then(
        (profile) => live && setAdult(agreesForItself(profile)), () => live && setAdult(false),
      );
    }
    return () => { live = false; };
  }, [profiles, view.active]);
  if (state === null) return <Skeleton lines={2} />;

  const listed = ONLINE_FEATURES.filter((f) => state.features[f] !== undefined);
  const switches = listed.filter((f) => f !== "cloud_tier");
  const active = subscription !== "loading" && subscription !== "failed" && subscription.state === "active";
  const needed = subscription !== "loading" && subscription !== "failed" ? subscription.profiles.filter((p) => p.consent === "needed").map((p) => p.profile_id) : [];
  const names = needed.map((id) => known.find((p) => p.profile_id === id)?.display_name ?? "").filter((n) => n !== "");
  const selfConsent = adult === true && needed.length > 0 && needed.every((id) => id === view.active);
  const studyNeedsCloud = adult === true && (!cloudConsent?.effective || cloudConsent.paused || (view.active !== null && needed.includes(view.active)));
  // Personal tutor memory online works with the online tutor (§19.6): it cannot be turned on while that is off.
  const memoryNeedsCloud = !(state.features.cloud_tier?.enabled ?? false) || cloudConsent?.effective !== true || cloudConsent.paused;
  const set = (change: Partial<Record<OnlineFeature, boolean>>) => run(async () => setState(await family.setOnlineFeatures(change)));
  const refresh = async () => setState(await family.connectivity());
  const end = () => run(async () => {
    setSubscription(await family.endSubscription());
    await refresh();
  });
  const allOff = () => run(async () => {
    if (switches.length > 0) setState(await family.setOnlineFeatures(Object.fromEntries(switches.map((f) => [f, false]))));
    if (active) {
      setSubscription(await family.endSubscription());
      await refresh();
    }
  });

  const featureRow = (feature: OnlineFeature) => (
        <div key={feature} data-online-feature={feature} className="flex min-w-0 flex-col gap-1 py-3 first:pt-0 last:pb-0">
          {feature === "cloud_tier" ? (
            <p className="text-ui font-semibold text-ink">{t(`settings.online.feature.${feature}`)}</p>
          ) : (
            <label className="flex items-center gap-2 text-ui font-semibold text-ink">
              <input
                type="checkbox"
                checked={state.features[feature]?.enabled ?? false}
                disabled={(adult === null && CONSENT_FEATURES.has(feature) && !(state.features[feature]?.enabled ?? false)) || (((["study_materials", "tutor_memory_online", "cloud_voice"].includes(feature) &&
                  !(feature === "study_materials" ? (availability?.study || availability?.research || availability?.narration) : feature === "cloud_voice" ? availability?.voice : availability?.tutor))) || (feature === "tutor_memory_online" && memoryNeedsCloud)) && !(state.features[feature]?.enabled ?? false)}
                onChange={(event) => (event.target.checked ? setAsking(feature) : set({ [feature]: false }))}
              />
              {t(`settings.online.feature.${feature}`)}
            </label>
          )}
          <p className="text-meta text-muted">{t(`settings.online.summary.${feature}`)}</p>
          {feature === "study_materials" && adult !== null && (!studyConsent?.effective || studyConsent.paused || studyNeedsCloud || (adult === false && studyConsent.method !== "checkout_guardian")) && <div className="flex flex-col items-start gap-2">
            <p className="text-ui text-ink">{t(adult === false ? "study.access.settings_guardian" : "study.access.settings_self")}</p>
            {adult && <Button onClick={() => setAsking("study_materials")}>{t("study.access.review_consent")}</Button>}
          </div>}
          {feature === "tutor_memory_online" && memoryNeedsCloud && !(state.features[feature]?.enabled ?? false)
            && <p data-needs-cloud="" className="text-meta text-muted">{t("settings.online.tutor_memory_online.needs_cloud")}</p>}
          {feature === "tutor_memory_online" && <CheckinNotifications profiles={profiles} notifier={notifier} />}
          {feature === "cloud_tier" && <Cloud subscription={subscription} names={names} selfConsent={selfConsent} known={adult !== null || view.active === null} inferenceAllowed={selfConsent && Object.values(availability ?? {}).some(Boolean)} onConsent={() => setDialog("consent")} onEnd={() => setDialog("end")} />}
          <FeaturePrivacy feature={feature} />
        </div>
  );

  return (
    <>
      {!Object.values(availability ?? {}).some(Boolean) && <p className="text-meta text-muted">{t("settings.online.inference_unavailable")}</p>}
      {FEATURE_GROUPS.map(group => {
        const features = listed.filter(feature => group.features.includes(feature));
        return features.length === 0 ? null : <fieldset key={group.id} data-online-group={group.id} className="min-w-0 rounded-card border border-line bg-canvas px-4 pb-4">
          <legend className="px-1 text-ui font-semibold text-ink">{t(`settings.online.group.${group.id}`)}</legend>
          <div className="divide-y divide-line pt-2">{features.map(featureRow)}</div>
        </fieldset>;
      })}
      {!listed.includes("tutor_memory_online") && <CheckinNotifications profiles={profiles} notifier={notifier} />}
      <div className="border-t border-line pt-2">
        <Button onClick={() => (active ? setDialog("all_off") : allOff())}>{t("settings.online.all_off")}</Button>
      </div>
      <ConfirmDialog
        open={asking !== null}
        message={asking === null ? "" : t("settings.online.confirm", { feature: t(`connectivity.short.${asking}`) })}
        details={asking === null ? [] : [
          t(`settings.online.leaves.${asking}`),
          ...(asking === "study_materials" && studyNeedsCloud ? [t("settings.online.consent.cloud_tier")] : []),
          ...(CONSENT_FEATURES.has(asking) ? [t(`settings.online.consent.${asking}`), t(adult === false ? "settings.online.attestation" : "settings.online.self_attestation")] : []),
        ]}
        confirm={t("settings.online.turn_on")}
        cancel={t("action.cancel")}
        onCancel={() => setAsking(null)}
        onConfirm={() => {
          const feature = asking;
          setAsking(null);
          if (feature !== null) run(async () => {
            // The device switch alone is not profile consent. Adults can grant
            // their own through the existing consent API; minors keep the
            // signed guardian-checkout requirement enforced by the core.
            if (feature === "study_materials" && adult === true && view.active !== null) {
              if (needed.includes(view.active)) {
                setSubscription(await family.applySubscription({ entitlement: {}, consents: [
                  { profile_id: view.active, notice_version: CONSENT_NOTICE_VERSIONS.cloud_tier },
                ] }));
              }
              setStudyConsent(await switchConsent(profiles.commands, view.active, feature, "on"));
              if (studyNeedsCloud) setCloudConsent(await switchConsent(profiles.commands, view.active, "cloud_tier", "on"));
            }
            // Cloud voice is a profile feature too (VOC-28): the adult's "Turn on" above, with the consent line, is
            // their own consent for the open profile, or voice stays on this computer.
            if (feature === "cloud_voice" && adult === true && view.active !== null) {
              await switchConsent(profiles.commands, view.active, feature, "on");
            }
            // Personal tutor memory online is the profile's consent too (§19.6), recorded against the notice the core
            // ships (`CONSENT_NOTICE_VERSIONS.tutor_memory_online`).
            if (feature === "tutor_memory_online" && adult === true && view.active !== null) {
              await switchConsent(profiles.commands, view.active, feature, "on");
            }
            setState(await family.setOnlineFeatures({ [feature]: true }));
          });
        }}
      />
      <ConfirmDialog
        open={dialog === "consent"}
        message={t("settings.online.cloud.confirm")}
        details={[t("settings.online.consent.cloud_tier"), t(selfConsent ? "settings.online.self_attestation" : "settings.online.attestation")]}
        confirm={t("settings.online.turn_on")}
        cancel={t("action.cancel")}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          setDialog(null);
          // The entitlement is the one the checkout left with the core, waiting for these consents (D70).
          const consents = needed.map((profile_id) => ({ profile_id, notice_version: CONSENT_NOTICE_VERSIONS.cloud_tier }));
          run(async () => {
            setSubscription(await family.applySubscription({ entitlement: {}, consents }));
            await refresh();
          });
        }}
      />
      <ConfirmDialog
        open={dialog === "end" || dialog === "all_off"}
        message={dialog === "all_off" ? t("settings.online.all_off_confirm") : t("settings.online.cloud.end_confirm")}
        confirm={t("settings.online.turn_off")}
        cancel={t("action.cancel")}
        onCancel={() => setDialog(null)}
        onConfirm={() => {
          const which = dialog;
          setDialog(null);
          if (which === "all_off") allOff();
          else end();
        }}
      />
    </>
  );
}
