// The parent views in the open student's Settings (TCH-12, TCH-14, UXS-33, TABLE UXS-46a): "Parent summary", the
// latest weekly digest (`GET /v1/digests/latest`), and "Alerts", the alerts not put away, newest first (`GET
// /v1/alerts`, `POST /v1/alerts/{alert_id}/dismiss`). Both are read from the core on this computer and work with no
// network. Neither is in the command contract yet: the shapes are proposed in contract-additions-m33a.json (D72) and
// each reply is checked here against them before it is accepted; a reply that does not hold is refused. A core without
// the routes answers `not_found` and the sections say so (`unavailable`). Everything belongs to the open profile and is
// dropped when it closes (SEC-19).
import { clientId } from "./ids.js";
import type { ProfileController } from "./profiles.js";
import { count, date, flag, list, marks, oneOf, orNull, problemsOf, refuse, shape, text } from "./shapes.js";
import { CommandError } from "./stream/commands.js";

export const DIGEST_SUBJECTS = ["mathematics", "physics", "chemistry", "biology", "accounting"] as const;
export type DigestSubject = (typeof DIGEST_SUBJECTS)[number];

/** A KC the digest names (`DigestKc`): the pack's own topic title and statement, never student text. */
export interface DigestKc {
  kc_id: string;
  subject: DigestSubject;
  topic_id: string | null;
  topic_title: string | null;
  statement: string | null;
}

export interface DigestWatch extends DigestKc {
  reason: "learning_long" | "moved_on";
}

export interface DigestMock {
  exam_id: string;
  paper_id: string;
  submitted_at: number;
  title: string | null;
  marks_awarded: number | null;
  marks_available: number | null;
}

/** The weekly digest (`ParentDigest`, contract-additions-m33a.json): numbers, ids and pack titles; the words are ours. */
export interface ParentDigest {
  week_start: string;
  week_end: string;
  generated_at: number;
  final: boolean;
  /** `none`: no study that week; `low`: too little to summarise, time studied only (JRN-13 F1). */
  activity: "none" | "low" | "enough";
  time_by_day: { day: string; minutes: number }[];
  minutes_total: number;
  days_studied: number;
  sessions: number;
  mastered: DigestKc[];
  to_watch: DigestWatch[];
  mocks: DigestMock[];
  solutions_viewed: number;
  bottom_out_hints: number;
  steps: number;
  steps_with_hints: number;
  streak_days: number;
  exam: { subject: DigestSubject; days: number } | null;
}

export const ALERT_KINDS = ["inactive", "bottom_out_heavy", "mastery_drop", "mock_result", "wellbeing_message_shown"] as const;
export type AlertKind = (typeof ALERT_KINDS)[number];

/** One alert (`AlertView`): kind, severity and date only; a mastery drop names its KC (TABLE TCH-14a, UXS-46a). */
export interface AlertView {
  alert_id: string;
  kind: AlertKind;
  severity: "info" | "attention" | "urgent";
  created_at: number;
  kc_id: string | null;
}

// --- checking the proposed shapes --------------------------------------------------------------------------------------

const ULID = "[0-9A-HJKMNP-TV-Z]{26}";

const KC = {
  kc_id: text(96), subject: oneOf(DIGEST_SUBJECTS), topic_id: orNull(text(96)), topic_title: orNull(text(200)),
  statement: orNull(text(500)),
};

const DIGEST = shape({
  week_start: date, week_end: date, generated_at: count, final: flag, activity: oneOf(["none", "low", "enough"]),
  time_by_day: list(shape({ day: date, minutes: count }), 7), minutes_total: count, days_studied: count, sessions: count,
  mastered: list(shape(KC)), to_watch: list(shape({ ...KC, reason: oneOf(["learning_long", "moved_on"]) })),
  mocks: list(shape({
    exam_id: text(30, new RegExp(`^exm_${ULID}$`)), paper_id: text(96), submitted_at: count, title: orNull(text(200)),
    marks_awarded: orNull(marks), marks_available: orNull(marks),
  })),
  solutions_viewed: count, bottom_out_hints: count, steps: count, steps_with_hints: count, streak_days: count,
  exam: orNull(shape({ subject: oneOf(DIGEST_SUBJECTS), days: count })),
});

const ALERTS = shape({
  alerts: list(shape({
    alert_id: text(30, new RegExp(`^alr_${ULID}$`)), kind: oneOf(ALERT_KINDS), severity: oneOf(["info", "attention", "urgent"]),
    created_at: count, kc_id: orNull(text(96)),
  })),
});

/** The digest as contract-additions-m33a.json proposes it (`ParentDigest`); one that does not hold is refused. */
export function readDigest(value: unknown): ParentDigest {
  const problems = problemsOf(DIGEST, value);
  if (problems.length > 0) throw refuse(problems);
  return value as ParentDigest;
}

/** The alerts as contract-additions-m33a.json proposes them (`AlertList`). */
export function readAlerts(value: unknown): AlertView[] {
  const problems = problemsOf(ALERTS, value);
  if (problems.length > 0) throw refuse(problems);
  return (value as { alerts: AlertView[] }).alerts;
}

// --- the controller ------------------------------------------------------------------------------------------------

type Status = "idle" | "loading" | "ready" | "failed" | "unavailable";

export interface ParentView {
  digestStatus: Status;
  digest: ParentDigest | null;
  alertsStatus: Status;
  alerts: AlertView[];
  /** The alert being put away, and whether the last attempt failed (it stays listed). */
  dismissing: string | null;
  dismissFailed: boolean;
}

export const EMPTY_PARENT_VIEW: ParentView = { digestStatus: "idle", digest: null, alertsStatus: "idle", alerts: [], dismissing: null, dismissFailed: false };

function missing(error: unknown): boolean {
  return error instanceof CommandError && (error.code === "not_found" || error.status === 404 || error.status === 405);
}

export class ParentController {
  private view: ParentView = EMPTY_PARENT_VIEW;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly profiles: ProfileController) {
    profiles.onClear(() => this.set(EMPTY_PARENT_VIEW));
  }

  getView = (): ParentView => this.view;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private set(next: ParentView): void {
    if (next === this.view) return;
    this.view = next;
    for (const listener of [...this.listeners]) listener();
  }

  private patch(change: Partial<ParentView>): void {
    this.set({ ...this.view, ...change });
  }

  /** Read the latest weekly digest; a read again keeps the one shown until the new one arrives. */
  async loadDigest(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.digest === null) this.patch({ digestStatus: "loading" });
    try {
      const digest = readDigest(await this.profiles.commands.get<unknown>("/v1/digests/latest"));
      if (generation === this.profiles.generation) this.patch({ digestStatus: "ready", digest });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.patch(this.view.digest !== null ? { digestStatus: "ready" } : { digestStatus: missing(error) ? "unavailable" : "failed" });
    }
  }

  async loadAlerts(): Promise<void> {
    const generation = this.profiles.generation;
    if (this.view.alertsStatus !== "ready") this.patch({ alertsStatus: "loading" });
    try {
      const alerts = readAlerts(await this.profiles.commands.get<unknown>("/v1/alerts"));
      if (generation === this.profiles.generation) this.patch({ alertsStatus: "ready", alerts });
    } catch (error) {
      if (generation !== this.profiles.generation) return;
      this.patch(this.view.alertsStatus === "ready" ? {} : { alertsStatus: missing(error) ? "unavailable" : "failed" });
    }
  }

  /** Put an alert away. It leaves the list once the core has it; an alert already gone leaves it too. */
  async dismiss(alertId: string): Promise<boolean> {
    if (this.view.dismissing !== null) return false;
    const generation = this.profiles.generation;
    this.patch({ dismissing: alertId, dismissFailed: false });
    try {
      await this.profiles.commands.post(`/v1/alerts/${encodeURIComponent(alertId)}/dismiss`, {}, clientId());
    } catch (error) {
      if (generation !== this.profiles.generation) return false;
      if (!missing(error)) {
        this.patch({ dismissing: null, dismissFailed: true });
        return false;
      }
    }
    if (generation !== this.profiles.generation) return false;
    this.patch({ dismissing: null, alerts: this.view.alerts.filter((a) => a.alert_id !== alertId) });
    return true;
  }
}

/** Hours and minutes of a number of minutes, for the summary's words ("2 h 15 min"). */
export function hoursAndMinutes(minutes: number): { hours: number; minutes: number } {
  return { hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}

/** The one suggested conversation starter (UXS-33), chosen by rule from the digest: a topic to watch first, then a topic
 * secured, then the week's study, else an invitation; the client words it from its template. */
export function conversationStarter(digest: ParentDigest): { kind: "watch" | "secured" | "studied" | "start"; topic: string | null } {
  const watched = digest.to_watch.find((k) => k.topic_title !== null);
  if (watched !== undefined) return { kind: "watch", topic: watched.topic_title };
  const secured = digest.mastered.find((k) => k.topic_title !== null);
  if (secured !== undefined) return { kind: "secured", topic: secured.topic_title };
  return { kind: digest.activity === "none" ? "start" : "studied", topic: null };
}
