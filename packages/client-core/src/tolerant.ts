// The tolerant reader (API-35): a client may meet a newer core of the same major. Unknown envelope and event fields
// are dropped, events of an unknown type are skipped, an unknown value of an open enum (`x-open-enum`) becomes
// `unknown` (the UI shows a generic label), and what remains is validated strictly against this build's contract.
import { ENVELOPE_FIELDS, EVENT_FIELDS, OPEN_ENUMS, validators, type EventEnvelope } from "@jana/contracts-ts/client";

export const UNKNOWN = "unknown";

export type ReadResult =
  | { kind: "accept"; envelope: EventEnvelope; unknown: string[] }
  | { kind: "skip"; type: string }
  | { kind: "reject"; errors: string[] };

type Json = Record<string, unknown>;

function pick(source: Json, fields: readonly string[]): Json {
  const out: Json = {};
  for (const key of fields) if (key in source) out[key] = source[key];
  return out;
}

/** Visit each value at `path` (a `*` step walks every array item), letting `visit` replace it. */
function atPath(node: unknown, path: readonly string[], visit: (value: unknown) => unknown): unknown {
  if (path.length === 0) return visit(node);
  const [head, ...rest] = path;
  if (head === "*") return Array.isArray(node) ? node.map((item) => atPath(item, rest, visit)) : node;
  if (node === null || typeof node !== "object" || Array.isArray(node) || head === undefined || !(head in node)) return node;
  return { ...(node as Json), [head]: atPath((node as Json)[head], rest, visit) };
}

export function readEnvelope(raw: unknown): ReadResult {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return { kind: "reject", errors: ["$: not an object"] };
  const envelope = pick(raw as Json, ENVELOPE_FIELDS);
  const event = envelope.event;
  if (event === null || typeof event !== "object" || Array.isArray(event)) return { kind: "reject", errors: ["$.event: not an object"] };
  const type = (event as Json).type;
  if (typeof type !== "string") return { kind: "reject", errors: ["$.event.type: missing"] };
  const fields = EVENT_FIELDS[type];
  if (fields === undefined) return { kind: "skip", type };
  let delivered: unknown = pick(event as Json, fields);
  let checked: unknown = delivered;
  const unknown: string[] = [];
  for (const { path, values } of OPEN_ENUMS[type] ?? []) {
    const known = (value: unknown) => typeof value !== "string" || values.includes(value);
    delivered = atPath(delivered, path, (value) => {
      if (known(value)) return value;
      unknown.push(`$.event.${path.join(".")}=${String(value)}`);
      return UNKNOWN;
    });
    // Strict validation of what remains: an unknown open value stands in as a known one for the check only.
    checked = atPath(checked, path, (value) => (known(value) ? value : values[0]));
  }
  const validate = validators["api/events"];
  if (validate !== undefined && !validate({ ...envelope, event: checked })) {
    return { kind: "reject", errors: (validate.errors ?? []).map((e) => `${e.instancePath || "$"} ${e.message ?? ""}`) };
  }
  return { kind: "accept", envelope: { ...envelope, event: delivered } as EventEnvelope, unknown };
}
