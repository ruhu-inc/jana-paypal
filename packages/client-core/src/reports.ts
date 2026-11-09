// "Report a problem" and Settings → Help (UXS-44, TABLE UXS-46a, OBS-18): the open profile's problem reports, kept on
// this computer only (`/v1/problem-reports`, API-28), and "Save a report for support", a diagnostic bundle sealed to
// support and written to a folder the family chooses (`POST /v1/exports`, DAT-10). Nothing is sent anywhere.
import type { JobAccepted, learning_student_records } from "@jana/contracts-ts";
import { clientId } from "./ids.js";
import type { CommandClient } from "./stream/commands.js";

export type ProblemReport = learning_student_records.ProblemReport;
export type NewProblemReport = learning_student_records.CreateProblemReport;

export class ProblemReports {
  constructor(private readonly commands: CommandClient) {}

  async list(): Promise<ProblemReport[]> {
    const reports = await this.commands.get<ProblemReport[]>("/v1/problem-reports");
    return [...reports].sort((a, b) => b.created_at.localeCompare(a.created_at));
  }

  save(report: NewProblemReport): Promise<ProblemReport> {
    return this.commands.post<ProblemReport>("/v1/problem-reports", report, clientId());
  }

  async remove(reportId: string): Promise<void> {
    await this.commands.delete<undefined>(`/v1/problem-reports/${encodeURIComponent(reportId)}`, clientId());
  }

  /** Settings → Data export (DAT-10, TABLE UXS-46a): the open profile's archive, sealed with the passphrase the
   * student chooses now, into `folder`. Conversations and photos stay out: they need the student's own archive
   * authorization (API-30). The passphrase is sent to the core only and kept nowhere here (SEC-17). */
  profileArchive(profileId: string, folder: string, passphrase: string, includeStudySources = false): Promise<JobAccepted> {
    return this.commands.post<JobAccepted>("/v1/exports", {
      kind: "profile_archive", include_study_sources: includeStudySources, profile_ids: [profileId], passphrase, destination: { kind: "file", path: folder },
    }, clientId());
  }

  /** Save a report for support (OBS-18): how the tutor has been running, without learner content, into `folder`. */
  supportReport(profileId: string, folder: string): Promise<JobAccepted> {
    return this.commands.post<JobAccepted>("/v1/exports", {
      kind: "diagnostic_bundle", profile_ids: [profileId], destination: { kind: "file", path: folder },
    }, clientId());
  }
}
