// One captured-question record for every input source (INP-01). Typed input is confirmed by pressing Send; a turn is
// started only from a confirmed capture. Photo, PDF, ink and voice sources extend the same record in S2 and later.
import { ulid } from "./ids.js";
import { InputError, INPUT_LIMITS, normaliseInput } from "./input.js";

import type { input_captured_question } from "@jana/contracts-ts";
export type CapturedQuestion = input_captured_question.CapturedQuestion;
export type CaptureSource = CapturedQuestion["source"];
export type CapturePurpose = CapturedQuestion["purpose"];

/** A typed (or pasted) capture: drafting while the student writes, confirmed when they press Send. */
export function typedCapture(text: string, options: { sessionId: string | null; purpose: CapturePurpose; confirmed: boolean; pasted?: boolean; now?: number }): CapturedQuestion {
  const now = options.now ?? Date.now();
  return {
    schema_version: 1,
    capture_id: `cap_${ulid(now)}`,
    session_id: options.sessionId,
    source: options.pasted === true ? "pasted_text" : "typed",
    purpose: options.purpose,
    locale: "en",
    status: options.confirmed ? "confirmed" : "drafting",
    text: normaliseInput(text),
    regions: [],
    questions: [],
    attachments: [],
    reader: { path: "none", model_id: null, hardware_class: "content_only", duration_ms: 0 },
    quality: { checks: [], overridden: false },
    confirmation: { confirmed: options.confirmed, method: options.confirmed ? "sent" : "pending", edit_distance: 0, confirmed_at: options.confirmed ? now : null },
    injection: { flagged: false, spans_removed: 0 },
    refusal: null,
    egress: { left_device: false, destination: "none" },
    created_at: now,
    updated_at: now,
  };
}

/** A turn starts only from a confirmed capture (INP-01). */
export function assertConfirmed(capture: CapturedQuestion): void {
  if (capture.status !== "confirmed" || !capture.confirmation.confirmed) throw new Error("capture_unconfirmed");
  if (capture.text.length > INPUT_LIMITS.text) throw new InputError("too_long", INPUT_LIMITS.text);
}

/** Draft edits preserve identity and creation time; terminal captures cannot be revived (INP-02). */
export function reviseTypedCapture(capture: CapturedQuestion, text: string, options: { pasted?: boolean; purpose?: CapturePurpose; now?: number } = {}): CapturedQuestion {
  if (capture.status !== "drafting" || !["typed", "pasted_text"].includes(capture.source)) throw new Error("capture_terminal");
  return { ...capture, text: normaliseInput(text), source: options.pasted === true ? "pasted_text" : capture.source,
    purpose: options.purpose ?? capture.purpose, updated_at: options.now ?? Date.now() };
}

export function confirmTypedCapture(capture: CapturedQuestion, now = Date.now()): CapturedQuestion {
  if (capture.status !== "drafting" || !["typed", "pasted_text"].includes(capture.source)) throw new Error("capture_terminal");
  const confirmed: CapturedQuestion = { ...capture, status: "confirmed", updated_at: now,
    confirmation: { confirmed: true, method: "sent", edit_distance: 0, confirmed_at: now } };
  assertConfirmed(confirmed);
  if (confirmed.text.trim() === "") throw new Error("capture_empty");
  return confirmed;
}

export function cancelTypedCapture(capture: CapturedQuestion, now = Date.now()): CapturedQuestion {
  if (capture.status !== "drafting") throw new Error("capture_terminal");
  return { ...capture, text: "", status: "cancelled", updated_at: now };
}

/** Confirmation and Up next must preserve the diagram values and table cells the reader found. */
export function captureRegionText(region: Pick<CapturedQuestion["regions"][number], "text" | "diagram_description" | "table">): string {
  return [region.text, region.diagram_description, region.table == null ? null :
    [region.table.columns.join(" | "), ...region.table.rows.map((row) => row.join(" | "))].join("\n")]
    .filter((part) => part != null && part !== "").join("\n");
}
