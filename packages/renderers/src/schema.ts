// Client re-validation of renderer-owned params schemas (UIA-20, VSL-15 stage 4 again on the client). A small,
// interpreted JSON Schema subset, so nothing is compiled at run time (no code generation, no eval): type, enum, const,
// required, properties, additionalProperties false, items, min/maxItems, uniqueItems, pattern, min/maxLength,
// minimum, maximum, exclusiveMinimum, exclusiveMaximum, propertyNames, maxProperties, anyOf, allOf, if/then and local
// $ref. The engine applies the full schemas with the same files (VSL-09).
export type Schema = Record<string, unknown>;

function typeOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return typeof value;
}

function typeMatches(want: unknown, value: unknown): boolean {
  const types = Array.isArray(want) ? want : [want];
  const got = typeOf(value);
  return types.some((t) => t === got || (t === "number" && got === "integer"));
}

const PATTERNS = new Map<string, RegExp>();
function pattern(source: string): RegExp {
  let re = PATTERNS.get(source);
  if (re === undefined) {
    re = new RegExp(source, "u");
    PATTERNS.set(source, re);
  }
  return re;
}

export function schemaErrors(schema: Schema, value: unknown, root: Schema = schema, path = "$"): string[] {
  let node = schema;
  for (let hops = 0; typeof node.$ref === "string" && hops < 8; hops += 1) {
    const ref = node.$ref as string;
    if (!ref.startsWith("#/$defs/")) return [`${path}: unsupported reference ${ref}`];
    node = ((root.$defs as Record<string, Schema> | undefined) ?? {})[ref.slice(8)] ?? {};
  }
  const errors: string[] = [];
  if (Array.isArray(node.anyOf)) {
    const ok = (node.anyOf as Schema[]).some((branch) => schemaErrors(branch, value, root, path).length === 0);
    if (!ok) errors.push(`${path}: matches no allowed form`);
  }
  if (Array.isArray(node.allOf)) for (const branch of node.allOf as Schema[]) errors.push(...schemaErrors(branch, value, root, path));
  if (typeof node.if === "object" && node.if !== null && typeof node.then === "object" && node.then !== null && schemaErrors(node.if as Schema, value, root, path).length === 0) {
    errors.push(...schemaErrors(node.then as Schema, value, root, path));
  }
  if (node.type !== undefined && !typeMatches(node.type, value)) return [...errors, `${path}: should be ${String(node.type)}`];
  if (Array.isArray(node.enum) && !(node.enum as unknown[]).includes(value)) errors.push(`${path}: not an allowed value`);
  if ("const" in node && node.const !== value) errors.push(`${path}: should be ${String(node.const)}`);
  if (typeof value === "string") {
    if (typeof node.maxLength === "number" && [...value].length > node.maxLength) errors.push(`${path}: too long`);
    if (typeof node.minLength === "number" && [...value].length < node.minLength) errors.push(`${path}: too short`);
    if (typeof node.pattern === "string" && !pattern(node.pattern).test(value)) errors.push(`${path}: does not match the pattern`);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) errors.push(`${path}: not a finite number`);
    if (typeof node.minimum === "number" && value < node.minimum) errors.push(`${path}: below ${node.minimum}`);
    if (typeof node.maximum === "number" && value > node.maximum) errors.push(`${path}: above ${node.maximum}`);
    if (typeof node.exclusiveMinimum === "number" && value <= node.exclusiveMinimum) errors.push(`${path}: not above ${node.exclusiveMinimum}`);
    if (typeof node.exclusiveMaximum === "number" && value >= node.exclusiveMaximum) errors.push(`${path}: not below ${node.exclusiveMaximum}`);
  }
  if (Array.isArray(value)) {
    if (typeof node.minItems === "number" && value.length < node.minItems) errors.push(`${path}: too few items`);
    if (typeof node.maxItems === "number" && value.length > node.maxItems) errors.push(`${path}: too many items`);
    if (node.uniqueItems === true && new Set(value.map((v) => JSON.stringify(v))).size !== value.length) errors.push(`${path}: items repeat`);
    if (node.items !== undefined && typeof node.items === "object") {
      value.forEach((item, i) => errors.push(...schemaErrors(node.items as Schema, item, root, `${path}[${i}]`)));
    }
  }
  if (typeOf(value) === "object") {
    const record = value as Record<string, unknown>;
    const props = (node.properties as Record<string, Schema> | undefined) ?? {};
    for (const key of (node.required as string[] | undefined) ?? []) if (!(key in record)) errors.push(`${path}: missing ${key}`);
    if (typeof node.maxProperties === "number" && Object.keys(record).length > node.maxProperties) errors.push(`${path}: too many fields`);
    for (const [key, item] of Object.entries(record)) {
      if (node.propertyNames !== undefined && schemaErrors(node.propertyNames as Schema, key, root, `${path}.${key}`).length > 0) errors.push(`${path}: field ${key} not allowed`);
      const child = props[key];
      if (child !== undefined) errors.push(...schemaErrors(child, item, root, `${path}.${key}`));
      else if (node.additionalProperties === false) errors.push(`${path}: unknown field ${key}`);
      else if (typeof node.additionalProperties === "object") errors.push(...schemaErrors(node.additionalProperties as Schema, item, root, `${path}.${key}`));
    }
  }
  return errors;
}
