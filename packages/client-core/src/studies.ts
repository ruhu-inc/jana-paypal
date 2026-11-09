// Research studies and what a usage report holds (M23's UI pieces): the study screen's per-profile research consents
// (PRV-05, PRV-06, D87: on a profile under 18 the parent or guardian ticks the agreement in the app and the student's
// own "Turn on" is their assent, the screen telling which from the open profile's age band, D93; any profile pauses,
// resumes or withdraws its own consent with no guardian step; `PUT /v1/profiles/{id}/consents/{scope}`, API-28), the
// research export with the study's approvals (EVL-22,
// `CreateExport.research_approval`: the student's assent, the school's approval reference and the registered protocol
// reference; a file for the study team, encrypted to its key, made only under an active `research_use` consent), and the
// published telemetry report schema (`GET /v1/telemetry/schema`, OBS-20, PRV-09) read into what Settings → "What leaves
// this device" lists: the report's fields with their fixed values, its measurements and its breakdowns. Replies are
// checked against the command contract at the boundary.
import { errors, type api_commands, type JobAccepted } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";

export type ConsentState = api_commands.ConsentState;

/** The research scopes of TABLE PRV-05a, in the study screen's order; a scope shows only where the core lists it. */
export const RESEARCH_SCOPES = ["research_use", "model_training_research"] as const;
export type ResearchScope = (typeof RESEARCH_SCOPES)[number];

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as T;
}

export type Profile = api_commands.Profile;
export type AgeBand = NonNullable<Profile["age_band"]>;

/** The open profile's own record, with its age band (`GET /v1/profiles/{id}`, PRV-06, API-28, D93). */
export async function readProfile(commands: CommandClient, profileId: string): Promise<Profile> {
  return checked<Profile>("Profile", await commands.get<unknown>(`/v1/profiles/${encodeURIComponent(profileId)}`));
}

/** Whether the profile agrees for itself (18 or over); every other profile is a minor, and so is one whose band could
 * not be read (a core older than API 1.4, or a failed read): the parent or guardian agrees (privacy principle 4). */
export function agreesForItself(profile: Profile | null): boolean {
  return profile?.age_band === "18_plus";
}

/** The open profile's consent states. */
export async function readConsents(commands: CommandClient, profileId: string): Promise<ConsentState[]> {
  return checked<ConsentState[]>("ConsentList", await commands.get<unknown>(`/v1/profiles/${encodeURIComponent(profileId)}/consents`));
}

/** Turn a research consent on (a new grant: `guardian` when the parent or guardian ticked the agreement), pause it or
 * withdraw it. A minor's new grant without the guardian's agreement is refused by the core (`validation_failed`). */
export async function switchConsent(commands: CommandClient, profileId: string, scope: string, action: "on" | "pause" | "withdraw", guardian = false): Promise<ConsentState> {
  const body: api_commands.ConsentSwitch = { action, ...(action === "on" && guardian ? { guardian_agreed: true as const } : {}) };
  return checked<ConsentState>("ConsentState", await commands.put<unknown>(`/v1/profiles/${encodeURIComponent(profileId)}/consents/${encodeURIComponent(scope)}`, body, clientId()));
}

/** The study's references, as the study host gives them to the student. */
export interface StudyApproval {
  school_approval_ref: string;
  protocol_ref: string;
}

/** The research export for the open profile into `folder` (EVL-22, TABLE DAT-10a), with the student's assent and the
 * study's approvals; nothing is sent anywhere. */
export function researchExport(commands: CommandClient, profileId: string, folder: string, approval: StudyApproval): Promise<JobAccepted> {
  const body: api_commands.CreateExport = {
    kind: "research_export", profile_ids: [profileId], destination: { kind: "file", path: folder },
    research_approval: { student_assent: true, school_approval_ref: approval.school_approval_ref.trim(), protocol_ref: approval.protocol_ref.trim() },
  };
  return commands.post<JobAccepted>("/v1/exports", body, clientId());
}

/** One field of a usage report, with its fixed values where it has them. */
export interface ReportField {
  name: string;
  values: string[] | null;
}

/** What a usage report may hold, read from the published schema. */
export interface TelemetrySummary {
  fields: ReportField[];
  metrics: string[];
  breakdowns: ReportField[];
  maxDays: number | null;
}

type Schema = { properties?: Record<string, Schema>; enum?: unknown[]; $ref?: string; $defs?: Record<string, Schema>; items?: Schema; maximum?: number };

/** The telemetry report schema as Settings shows it: every top-level field (the measurements and breakdowns listed on
 * their own), the measurement names and each breakdown with its values. A reply that is not a schema is refused. */
export async function readTelemetrySchema(commands: CommandClient): Promise<TelemetrySummary> {
  const schema = await commands.get<Schema>("/v1/telemetry/schema");
  const resolve = (node: Schema | undefined): Schema | undefined => {
    const ref = node?.$ref;
    return ref?.startsWith("#/$defs/") ? schema.$defs?.[ref.slice(8)] : node;
  };
  const properties = schema.properties;
  const metrics = resolve(properties?.metrics)?.properties;
  if (typeof properties !== "object" || properties === null || typeof metrics !== "object" || metrics === null) {
    throw new CommandError(200, "invalid_reply", false, { fields: ["$: not the telemetry report schema"] });
  }
  const values = (node: Schema | undefined) => Array.isArray(node?.enum) ? node.enum.map(String) : null;
  const fields = Object.entries(properties).filter(([name]) => !["metrics", "series", "labels"].includes(name)).map(([name, node]) => ({ name, values: values(resolve(node)) }));
  const labels = resolve(properties.labels)?.properties ?? resolve(resolve(properties.series)?.items)?.properties?.labels?.properties ?? {};
  const breakdowns = Object.entries(labels).map(([name, node]) => ({ name, values: values(resolve(node)) }));
  return { fields, metrics: Object.keys(metrics), breakdowns, maxDays: typeof properties.days?.maximum === "number" ? properties.days.maximum : null };
}
