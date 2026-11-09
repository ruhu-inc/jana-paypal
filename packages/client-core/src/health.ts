// How the tutor is running (OBS-15) and the degraded-mode notices (ARC-26), read from the core's existing endpoints:
// `GET /v1/health` (each condition is a component of its own, see the core's `engine.health`), `GET /v1/models` (the
// hardware class and why, the models and the model pack's version), `GET /v1/packs` and `GET /v1/connectivity`. Each
// read may fail on its own; the panel shows what arrived.
import type { ConnectivityState, Health, ModelCatalogue, PackList } from "@jana/contracts-ts";
import type { ConnectivitySlice } from "./connectivity.js";
import { subjectName } from "./setup.js";
import type { CommandClient } from "./stream/commands.js";

/** The problem conditions the panel can name, in the order it lists them. */
export const HEALTH_PROBLEMS = [
  "content_only", "runtime_down", "runtime_fallback", "runtime_variant", "speed", "class_lower", "worker", "sandbox", "storage",
  "keychain", "learner_store", "remark",
] as const;
export type HealthProblem = (typeof HEALTH_PROBLEMS)[number];

/** Why the machine is in its class (PLT-08, PLT-29): the `basis` of the core's `hardware_class.<class>.<basis>` key. */
export const CLASS_BASES = ["predicted", "measured", "chosen", "below_minimum_ram", "below_minimum_cpu", "below_minimum_disk", "below_minimum_speed"] as const;
export const HARDWARE_CLASSES = ["content_only", "low", "mid", "high"] as const;
/** Why there is no class yet: the launch plan's reason (`no_class.<reason>`). */
export const NO_CLASS_REASONS = ["scripted", "no_model_manifest", "manifest_refused", "no_verified_model", "no_model_fits", "plan_failed"] as const;

export type HardwareReason =
  | { kind: "class"; hardwareClass: (typeof HARDWARE_CLASSES)[number]; basis: (typeof CLASS_BASES)[number] | null }
  | { kind: "none"; reason: (typeof NO_CLASS_REASONS)[number] | null };

export interface HealthReport {
  health: Health | null;
  models: ModelCatalogue | null;
  packs: PackList["packs"];
  connectivity: ConnectivityState | null;
}

const settle = async <T>(read: Promise<T>): Promise<T | null> => read.catch(() => null);

/** Read everything the panel shows, in parallel; a failed read leaves its part empty. */
export async function readHealthReport(commands: CommandClient): Promise<HealthReport> {
  const [health, models, packs, connectivity] = await Promise.all([
    settle(commands.get<Health>("/v1/health")),
    settle(commands.get<ModelCatalogue>("/v1/models")),
    settle(commands.get<PackList>("/v1/packs")),
    settle(commands.get<ConnectivityState>("/v1/connectivity")),
  ]);
  return { health, models, packs: packs?.packs ?? [], connectivity };
}

/** Only what the tutor screen's degraded-mode notices need (ARC-26, PLT-29, PLT-22): health, the packs and the
 * hardware report. */
export async function readDegraded(commands: CommandClient): Promise<Pick<HealthReport, "health" | "packs" | "models">> {
  const [health, packs, models] = await Promise.all([
    settle(commands.get<Health>("/v1/health")),
    settle(commands.get<PackList>("/v1/packs")),
    settle(commands.get<ModelCatalogue>("/v1/models")),
  ]);
  return { health, packs: packs?.packs ?? [], models };
}

/** The hardware class and why, from `recommended.reason_key`; unknown parts read as null, never as a guess. */
export function hardwareReason(models: ModelCatalogue | null): HardwareReason | null {
  if (models === null) return null;
  const [head, first, second] = models.recommended.reason_key.split(".");
  if (head === "hardware_class") {
    const hardwareClass = HARDWARE_CLASSES.find((c) => c === first);
    if (hardwareClass === undefined) return null;
    return { kind: "class", hardwareClass, basis: CLASS_BASES.find((b) => b === second) ?? null };
  }
  if (head === "no_class") return { kind: "none", reason: NO_CLASS_REASONS.find((r) => r === first) ?? null };
  return null;
}

export interface InstalledModel {
  modelId: string;
  packVersion: string | null;
  serving: boolean;
}

/** The installed models as the core lists them: the contract leaves each entry open, so every field is read with
 * its type checked, and an entry without a model id is skipped. */
export function installedModels(models: ModelCatalogue | null): InstalledModel[] {
  const out: InstalledModel[] = [];
  for (const entry of (models?.installed ?? []) as Record<string, unknown>[]) {
    if (typeof entry.model_id !== "string") continue;
    out.push({ modelId: entry.model_id, packVersion: typeof entry.pack_version === "string" ? entry.pack_version : null, serving: entry.serving === true });
  }
  return out;
}

/** A minimum-laptop check the computer failed, with the figures the unsupported-offline notice names (PLT-29). */
export interface Shortfall {
  check: "ram" | "cpu" | "disk" | "speed";
  have: number | string;
  need: number | string;
  unit: string;
}

/** The llama.cpp build the runtime uses and the ones set aside with why (PLT-04). */
export interface RuntimeBuild {
  variant: string;
  fallbacks: { variant: string; reason: string }[];
}

/** The hardware report of `GET /v1/models` (`hardware`, a proposed contract addition: read only when present, every
 * field checked; a core that does not send it leaves the panel as it was). */
export interface HardwareReport {
  hardwareClass: (typeof HARDWARE_CLASSES)[number] | null;
  shortfalls: Shortfall[];
  tutorPath: "local" | "school_server" | "online" | "none" | null;
  runtime: RuntimeBuild | null;
}

const CHECKS = ["ram", "cpu", "disk", "speed"] as const;
const PATHS = ["local", "school_server", "online", "none"] as const;
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const figure = (value: unknown): number | string | null => (typeof value === "number" || typeof value === "string" ? value : null);

export function hardwareReport(models: ModelCatalogue | null): HardwareReport | null {
  const raw = (models as Record<string, unknown> | null)?.hardware;
  if (!isRecord(raw)) return null;
  const shortfalls: Shortfall[] = [];
  for (const item of Array.isArray(raw.shortfalls) ? raw.shortfalls : []) {
    if (!isRecord(item)) continue;
    const check = CHECKS.find((c) => c === item.check);
    const have = figure(item.have);
    const need = figure(item.need);
    if (check === undefined || have === null || need === null) continue;
    shortfalls.push({ check, have, need, unit: typeof item.unit === "string" ? item.unit : "" });
  }
  let runtime: RuntimeBuild | null = null;
  if (isRecord(raw.runtime) && typeof raw.runtime.variant === "string") {
    const fallbacks = (Array.isArray(raw.runtime.fallbacks) ? raw.runtime.fallbacks : [])
      .filter(isRecord)
      .filter((f) => typeof f.variant === "string" && typeof f.reason === "string")
      .map((f) => ({ variant: String(f.variant), reason: String(f.reason) }));
    runtime = { variant: raw.runtime.variant, fallbacks };
  }
  return {
    hardwareClass: HARDWARE_CLASSES.find((c) => c === raw.hardware_class) ?? null,
    shortfalls,
    tutorPath: PATHS.find((p) => p === raw.tutor_path) ?? null,
    runtime,
  };
}

/** What first run's "Set up the tutor" step shows (UXS-10, PLT-29): still checking the computer, below the minimum
 * laptop (with the figures that are short), content-only for another reason (the speed test), no tutor model on the
 * computer, or ready. */
export type TutorSetup =
  | { kind: "checking" }
  | { kind: "below_minimum"; shortfalls: Shortfall[] }
  | { kind: "content_only" }
  | { kind: "no_model" }
  | { kind: "ready" };

export function tutorSetup(models: ModelCatalogue | null): TutorSetup {
  if (models === null) return { kind: "checking" };
  const report = hardwareReport(models);
  const reason = hardwareReason(models);
  const contentOnly = report?.hardwareClass === "content_only" || (reason?.kind === "class" && reason.hardwareClass === "content_only");
  if (contentOnly) {
    const short = (report?.shortfalls ?? []).filter((s) => s.check !== "speed");
    return short.length > 0 ? { kind: "below_minimum", shortfalls: short } : { kind: "content_only" };
  }
  return installedModels(models).length === 0 ? { kind: "no_model" } : { kind: "ready" };
}

/** The app saves battery now: the low-battery banner shows (PLT-22). */
export function savingPower(health: Health | null): boolean {
  return health?.components.power === "degraded";
}

/** The model runtime is unavailable and will not come back by itself: content-only (ARC-26, MDL-19). */
export function tutorCannotRun(health: Health | null): boolean {
  if (health === null) return false;
  return health.components.runtime === "down" && health.readiness !== "model_loading" && health.readiness !== "ready";
}

/** The problems to list, each once, in the panel's order. */
export function healthProblems(report: Pick<HealthReport, "health" | "models">): HealthProblem[] {
  const found = new Set<HealthProblem>();
  const reason = hardwareReason(report.models);
  const contentOnly = reason?.kind === "class" && reason.hardwareClass === "content_only";
  if (contentOnly) found.add("content_only");
  const health = report.health;
  if (health !== null) {
    const c = health.components;
    if (tutorCannotRun(health) && !contentOnly) found.add("runtime_down");
    if (c.runtime_fallback === "degraded") found.add("runtime_fallback");
    if (c.runtime_variant === "degraded") found.add("runtime_variant");
    if (c.speed === "degraded") found.add("speed");
    if (c.class_lower === "degraded") found.add("class_lower");
    if (c.worker === "down") found.add("worker");
    if (c.sandbox === "degraded") found.add("sandbox");
    if (c.storage === "degraded") found.add("storage");
    if (c.keychain === "degraded") found.add("keychain");
    if (c.learner_store === "degraded") found.add("learner_store");
    if (c.remark === "degraded") found.add("remark"); // CUR-36: a correction's re-mark keeps failing
  }
  return HEALTH_PROBLEMS.filter((p) => found.has(p));
}

/** Subjects with a pack that failed verification (CUR-08, ARC-26), by profile subject name (the core lists a pack's
 * subject as its curriculum code): each gets its own notice; others still work. */
export function unverifiedSubjects(packs: PackList["packs"]): string[] {
  return [...new Set(packs.filter((p) => !p.signature_ok).map((p) => subjectName(p.subject) ?? p.subject))];
}

/** Whether a tutor can answer on this computer now, or null while that is not known yet (PLT-17, PLT-29, ARC-26). Not
 * on the content-only class, not with no tutor model installed and no runtime ready, and not while the runtime cannot
 * run; a school server or the online tier that carries the tutor answers instead. */
export function tutorAnswers(report: Pick<HealthReport, "health" | "models">): boolean | null {
  if (report.models === null) return null;
  const path = hardwareReport(report.models)?.tutorPath;
  if (path === "school_server" || path === "online") return true;
  const setup = tutorSetup(report.models);
  if (setup.kind === "below_minimum" || setup.kind === "content_only") return false;
  if (tutorCannotRun(report.health)) return false;
  if (setup.kind === "no_model") return report.health?.readiness === "ready" || report.health?.readiness === "model_loading";
  return true;
}

/** Whether a tutor can answer a question now (SPEC §10.3): `available`; `checking` while the core has not said;
 * `switched_off` when the online tutor is off and there is no tutor model on this computer; `offline` when the online
 * tutor is on but cannot be reached (no network, or the core marks it unavailable) and there is no model here. Read from
 * the core's models and health and the connectivity slice only. A model on this computer, a school server, or a build
 * that does not list the online tutor leaves the answer to `tutorAnswers`. */
export type TutorReach = "available" | "checking" | "switched_off" | "offline";

export function tutorReach(report: Pick<HealthReport, "health" | "models">, connectivity: ConnectivitySlice): TutorReach {
  if (report.models === null) return "checking";
  if (hardwareReport(report.models)?.tutorPath === "school_server") return "available";
  if (installedModels(report.models).length > 0 && !tutorCannotRun(report.health)) return "available";
  if (connectivity.status !== "ready") return "checking";
  const cloud = connectivity.features.find((f) => f.feature === "cloud_tier");
  if (cloud === undefined) return "available";
  if (!cloud.enabled) return "switched_off";
  const reachable = (connectivity.network === "online" || connectivity.network === "metered" || connectivity.network === "unknown") && cloud.state !== "unavailable";
  return reachable ? "available" : "offline";
}
