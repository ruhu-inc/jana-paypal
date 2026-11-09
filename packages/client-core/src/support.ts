// The core's support, licence, setup and recovery routes, for the screens that show them (TABLE UXS-46a, UXS-10,
// UXS-47, OBS-17, OBS-18, PLT-19): the licence a school installed and the subscription's state (`GET /v1/licence`),
// the crash reports waiting for "Send this report?" (`GET /v1/crash-reports`, each sent or dismissed once), the
// student's own "Let support see this session" (`POST /v1/sessions/{id}/support-share`), where first run stands
// (`GET|PUT /v1/setup`), restoring a profile from an archive (`POST /v1/profiles/import`, always a new profile) and the
// app's version (`GET /v1/meta`). Every reply is checked against the command contract at the boundary.
import { errors, type api_commands, type JobAccepted, type Meta } from "@jana/contracts-ts/client";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";

export type LicenceStatus = api_commands.LicenceStatus;
export type CrashReport = api_commands.CrashReportListReportsItem;
export type SetupState = api_commands.SetupState;

function checked<T>(definition: string, value: unknown): T {
  const problems = errors(`api/commands#/$defs/${definition}`, value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as T;
}

/** Settings → Licence: the school licence file, if one is installed, and the subscription's state (PLT-19, D84). */
export async function readLicence(commands: CommandClient): Promise<LicenceStatus> {
  return checked<LicenceStatus>("LicenceStatus", await commands.get<unknown>("/v1/licence"));
}

/** A school lab computer (`install_kind` `school_lab`, PLT-19): shared by students whose profiles open with a tap
 * (D86), so the picker says so plainly (D102). */
export function sharedComputer(status: LicenceStatus): boolean {
  return status.install_kind === "school_lab";
}

/** The crash reports waiting for a decision, each exactly as it would be sent (OBS-17); none while the feature is off. */
export async function waitingCrashReports(commands: CommandClient): Promise<CrashReport[]> {
  return checked<api_commands.CrashReportList>("CrashReportList", await commands.get<unknown>("/v1/crash-reports")).reports;
}

/** "Send" or "Don't send" on "Send this report?": each report is decided once (OBS-17, D53). */
export async function decideCrashReport(commands: CommandClient, crashId: string, action: "send" | "dismiss"): Promise<void> {
  await commands.post<unknown>(`/v1/crash-reports/${encodeURIComponent(crashId)}/${action}`, {}, clientId());
}

/** The student's own "Let support see this session" for one session of the open profile (OBS-18); nothing is sent. */
export async function shareWithSupport(commands: CommandClient, sessionId: string): Promise<void> {
  await commands.post<unknown>(`/v1/sessions/${encodeURIComponent(sessionId)}/support-share`, {}, clientId());
}

/** Where first run stands (UXS-10): `students` until "Start learning" is recorded, then `done`. */
export async function readSetup(commands: CommandClient): Promise<SetupState> {
  return checked<SetupState>("SetupState", await commands.get<unknown>("/v1/setup"));
}

/** "Start learning" recorded: a restart no longer resumes first run. */
export async function finishSetup(commands: CommandClient): Promise<SetupState> {
  return checked<SetupState>("SetupState", await commands.put<unknown>("/v1/setup", { step: "done" }, clientId()));
}

/** "Restore from a profile archive" (UXS-47, DAT-10): the archive at `path` becomes a new profile; the reply is the job. */
export function importProfile(commands: CommandClient, path: string, passphrase: string, source: "file" | "usb" = "file"): Promise<JobAccepted> {
  const body: api_commands.ImportArchive = { source: { kind: source, path }, passphrase };
  return commands.post<JobAccepted>("/v1/profiles/import", body, clientId());
}

/** The app's version as the core reports it (`GET /v1/meta`), for Settings → About. */
export async function serverVersion(commands: CommandClient): Promise<string> {
  return (await commands.get<Meta>("/v1/meta")).server_version;
}

/** One line of THIRD_PARTY_NOTICES ("name version (ecosystem): licence"), for Settings → About → Licences. */
export interface NoticeEntry {
  name: string;
  version: string;
  ecosystem: string;
  licence: string;
}

/** The components THIRD_PARTY_NOTICES lists, in its order; lines that are not entries are left out. */
export function parseNotices(text: string): NoticeEntry[] {
  const out: NoticeEntry[] = [];
  for (const line of text.split(/\r?\n/)) {
    const m = /^(\S+) (\S+) \(([a-z0-9_-]+)\): (.+)$/.exec(line.trim());
    if (m !== null) out.push({ name: m[1] ?? "", version: m[2] ?? "", ecosystem: m[3] ?? "", licence: m[4] ?? "" });
  }
  return out;
}

/** One shipped asset of the asset register (fonts, icons, data sets, models; OPS-36): its name, kind and licence. */
export interface AssetEntry {
  kind: string;
  name: string;
  licence: string;
}

/** The `[[asset]]` entries of the asset register (scripts/governance/shipped-assets.toml), read as plain key = "value". */
export function parseAssets(text: string): AssetEntry[] {
  const out: AssetEntry[] = [];
  for (const block of text.split(/^\[\[asset\]\]\s*$/m).slice(1)) {
    const field = (key: string) => new RegExp(`^${key}\\s*=\\s*"([^"]*)"`, "m").exec(block)?.[1] ?? "";
    if (field("name") !== "") out.push({ kind: field("kind"), name: field("name"), licence: field("licence") });
  }
  return out;
}
