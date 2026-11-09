// Checks for reply shapes the command contract does not hold yet (D72 proposals, such as contract-additions-m33a.json):
// each names the JSON pointer of what does not hold, as the contract's validators do, so a reply that does not match is
// refused at the boundary. Once a shape is adopted its generated validator replaces these.
import { CommandError } from "./stream/commands.js";

const DATE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

type Check = (value: unknown, at: string, problems: string[]) => void;

const fail = (problems: string[], at: string, what: string) => { problems.push(`${at || "/"}: ${what}`); };
export const count: Check = (v, at, p) => { if (!Number.isInteger(v) || (v as number) < 0) fail(p, at, "not a count"); };
export const flag: Check = (v, at, p) => { if (typeof v !== "boolean") fail(p, at, "not a boolean"); };
export const date: Check = (v, at, p) => { if (typeof v !== "string" || !DATE.test(v)) fail(p, at, "not a date"); };
export const text = (max: number, pattern?: RegExp): Check => (v, at, p) => {
  if (typeof v !== "string" || v.length > max || (pattern !== undefined && !pattern.test(v))) fail(p, at, "not the text expected");
};
export const oneOf = (values: readonly unknown[]): Check => (v, at, p) => { if (!values.includes(v)) fail(p, at, "not an allowed value"); };
export const orNull = (check: Check): Check => (v, at, p) => { if (v !== null) check(v, at, p); };
export const marks: Check = (v, at, p) => { if (typeof v !== "number" || !Number.isFinite(v) || v < 0) fail(p, at, "not marks"); };
export const list = (item: Check, length?: number): Check => (v, at, p) => {
  if (!Array.isArray(v) || (length !== undefined && v.length !== length)) { fail(p, at, "not the list expected"); return; }
  v.forEach((entry, i) => item(entry, `${at}/${i}`, p));
};
/** An object with exactly these properties (the proposed definitions allow no others). */
export const shape = (props: Record<string, Check>): Check => (v, at, p) => {
  if (typeof v !== "object" || v === null || Array.isArray(v)) { fail(p, at, "not an object"); return; }
  const record = v as Record<string, unknown>;
  for (const key of Object.keys(record)) if (!(key in props)) fail(p, `${at}/${key}`, "not allowed");
  for (const [key, check] of Object.entries(props)) {
    if (!(key in record)) fail(p, `${at}/${key}`, "missing");
    else check(record[key], `${at}/${key}`, p);
  }
};

/** Problems with a value against one of these checks, as JSON pointers with a reason. */
export function problemsOf(check: Check, value: unknown): string[] {
  const problems: string[] = [];
  check(value, "", problems);
  return problems;
}

export function refuse(problems: string[]): CommandError {
  return new CommandError(200, "invalid_reply", false, { fields: problems });
}
