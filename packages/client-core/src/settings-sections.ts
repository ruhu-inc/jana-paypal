// The Settings sections (UXS-46, TABLE UXS-46a): the core lists the sections of this build's stage, in the table's
// order, each open, offline and with its states (`GET /v1/settings/sections`, `SettingsSections`). The Settings page
// shows exactly those, in that order. The reply is checked against the command contract at the boundary; one that does
// not hold is refused. A core without the route (before M24) answers `not_found`: the page then shows the stage 2
// sections of the table (`STAGE_2_SECTIONS`), the stage every such core was built for.
import { errors, type api_commands } from "@jana/contracts-ts/client";
import type { CommandClient } from "./stream/commands.js";
import { CommandError } from "./stream/commands.js";

export type SettingsSectionsReply = api_commands.SettingsSections;
export type SettingsSection = SettingsSectionsReply["sections"][number];

/** The stage 2 section ids, in the table's order: what a core without the route serves. */
export const STAGE_2_SECTIONS = [
  "online_features", "profiles", "tutor_models", "content", "egress", "accessibility_language", "study", "tutor",
  "data_export", "licence", "help", "about",
] as const;

/** The sections reply, checked: a field that does not hold refuses the whole reply (`invalid_reply`). */
export function readSettingsSections(value: unknown): SettingsSectionsReply {
  const problems = errors("api/commands#/$defs/SettingsSections", value);
  if (problems.length > 0) throw new CommandError(200, "invalid_reply", false, { fields: problems });
  return value as SettingsSectionsReply;
}

export class SettingsSections {
  constructor(private readonly commands: CommandClient) {}

  /** The section ids in the core's order; the stage 2 sections when this computer's core does not serve the route. */
  async read(): Promise<string[]> {
    try {
      return readSettingsSections(await this.commands.get<unknown>("/v1/settings/sections")).sections.map((s) => s.id);
    } catch (error) {
      if (error instanceof CommandError && (error.code === "not_found" || error.status === 404)) return [...STAGE_2_SECTIONS];
      throw error;
    }
  }
}
