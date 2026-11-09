// First run's student hand-off (UXS-10 step 2, PLT-17, PLT-29): "You're ready, {name}." with what the tutor can do on
// this computer, said once, after "Who's learning?". Below the minimum laptop it names the figure that is short and
// offers "Continue without the tutor" and "Online features"; a computer that is content-only for another reason, has
// no tutor model, or is still getting the tutor ready says so in its own words (the PLT-17 copy for no model pack);
// while the computer is still being checked it says so with the real elapsed time. It is not a setup step: there is no
// step count, and continuing never waits for the tutor. Direction A: the student's avatar on a card on paper, the
// greeting in display type.
import { useEffect, useId, useRef } from "react";
import type { TutorSetup } from "@jana/client-core";
import { Icon } from "../icons.js";
import { useT } from "../i18n.js";
import { Button, Loader, Notice } from "../primitives/index.js";
import { Avatar } from "../profiles/Avatar.js";
import { WelcomeCard } from "./Welcome.js";
import { useShortfallText } from "../status/DegradedNotices.js";

export function FirstRunTutor({ name, disc, setup, warming = false, elapsedMs = 0, onContinue, onOnlineFeatures, onDiagnostic }: {
  /** The student who was just added (the first, when several were), and their avatar disc. */
  name: string;
  disc?: number;
  setup: TutorSetup;
  /** The recommended model is still being installed in the background (UXS-10 "tutor getting ready"). */
  warming?: boolean;
  /** How long the computer has been checked for, when it still is (UXS-10: the probe's real time). */
  elapsedMs?: number;
  onContinue: () => void;
  onOnlineFeatures?: () => void;
  onDiagnostic?: () => void;
}) {
  const t = useT();
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const shortfallText = useShortfallText();
  useEffect(() => heading.current?.focus(), []);
  const body = () => {
    switch (setup.kind) {
      case "checking":
        return <Loader label={t("first_run.tutor.checking", { seconds: Math.floor(elapsedMs / 1000) })} />;
      case "below_minimum":
        return <Notice tone="warn" message={[t("first_run.tutor.below_minimum"), ...setup.shortfalls.map(shortfallText), t("unsupported.offline")].join(" ")} />;
      case "content_only":
        return <Notice message={t("first_run.tutor.content_only")} />;
      case "no_model":
        return <Notice message={warming ? t("first_run.tutor.getting_ready") : t("first_run.tutor.no_model")} />;
      default:
        return warming ? <Notice message={t("first_run.tutor.getting_ready")} /> : (
          <p className="flex items-center justify-center gap-2 text-body text-ink">
            <span className="inline-flex text-verified"><Icon name="verified" size={16} /></span>
            {t("first_run.tutor.ready")}
          </p>
        );
    }
  };
  const withoutTutor = setup.kind === "below_minimum";
  return (
    <WelcomeCard labelledBy={id} data-first-run-step="handoff" data-setup={setup.kind}>
      {disc !== undefined && <Avatar name={name} disc={disc} size="xl" />}
      <div className="flex flex-col gap-2">
        <h1 id={id} ref={heading} tabIndex={-1} className="text-display">{t("first_run.ready", { name })}</h1>
        <p className="text-body text-muted">{t("first_run.stays")}</p>
      </div>
      <div className="w-full text-start">{body()}</div>
      <div className="flex w-full flex-col gap-2">
        <Button tone="primary" disabled={setup.kind === "checking"} onClick={onContinue}>
          {withoutTutor ? t("first_run.tutor.continue") : t("first_run.continue")}
          <Icon name="go-next" size={16} />
        </Button>
        {withoutTutor && onOnlineFeatures !== undefined && <Button tone="ghost" onClick={onOnlineFeatures}>{t("first_run.tutor.online")}</Button>}
        {onDiagnostic !== undefined && <Button tone="ghost" onClick={onDiagnostic}>{t("assessment.find_start")}</Button>}
      </div>
    </WelcomeCard>
  );
}
