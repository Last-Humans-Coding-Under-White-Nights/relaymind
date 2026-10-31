// Validator for the keywords in the frozen published gateway schemas (RM36/RM37).
import view from "./recommendation-schemas/recommendation_view.ts";
import result from "./recommendation-schemas/recommendation_result.ts";
import event from "./recommendation-schemas/recommendation_event.ts";
import readiness from "./recommendation-schemas/recommendation_readiness.ts";
import asset from "./recommendation-schemas/asset_record.ts";
import action from "./recommendation-schemas/action_evidence.ts";
import feedback from "./recommendation-schemas/feedback_record.ts";
import {
  encodeJson,
  isWideInteger,
  MAX_SQLITE_INTEGER,
  utf8Bytes,
  floatingNumber,
  numericValue,
  materializeNumbers,
} from "./recommendation-json.ts";
export type Schema = { [key: string]: any };
const schemas: Record<string, Schema> = {
  "recommendation_view.v1.json": view,
  "recommendation_result.v1.json": result,
  "recommendation_event.v1.json": event,
  "recommendation_readiness.v1.json": readiness,
  "asset_record.v1.json": asset,
  "action_evidence.v1.json": action,
  "feedback_record.v1.json": feedback,
};
export const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function invalid(): never {
  throw Error("invalid_protocol");
}
export function bytes(text: string, check: () => void = () => {}): number {
  return utf8Bytes(text, check);
}
function dateTime(value: string, check: () => void): boolean {
  const head = /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})$/.exec(
    value.slice(0, 19),
  );
  if (!head) return false;
  let pos = 19,
    fraction = "";
  if (value[pos] === ".") {
    const start = ++pos;
    while (pos < value.length) {
      if ((pos - start) % 1024 === 0) check();
      const ch = value.charCodeAt(pos);
      if (ch < 48 || ch > 57) break;
      pos++;
    }
    if (pos === start) return false;
    fraction = "." + value.slice(start, Math.min(pos, start + 3));
  }
  check();
  const remaining = value.length - pos;
  if (remaining !== 1 && remaining !== 6) return false;
  const suffix = value.slice(pos);
  const zone = /^(?:[Zz]|[+-](\d{2}):(\d{2}))$/.exec(suffix);
  if (!zone) return false;
  const [, y, m, d, h, mi, s] = head;
  const [, oh, om] = zone;
  return (
    +m >= 1 &&
    +m <= 12 &&
    +d >= 1 &&
    +d <= new Date(Date.UTC(+y, +m, 0)).getUTCDate() &&
    +h < 24 &&
    +mi < 60 &&
    +s < 60 &&
    (!oh || (+oh < 24 && +om < 60)) &&
    Number.isFinite(Date.parse(value.slice(0, 19) + fraction + suffix))
  );
}
export function validateSchema(
  value: unknown,
  schema: Schema,
  file = "recommendation_view.v1.json",
  key = "",
  check: () => void = () => {},
): void {
  check();
  if (schema.$ref) {
    const [path, fragment] = schema.$ref.split("#") as string[];
    let target = schemas[path || file];
    if (!target) invalid();
    for (const part of (fragment ?? "").split("/").filter(Boolean))
      target = target[part.replace(/~1/g, "/").replace(/~0/g, "~")];
    return validateSchema(value, target, path || file, key, check);
  }
  const matches = (s: Schema) => {
    try {
      validateSchema(value, s, file, key, check);
      return true;
    } catch {
      return false;
    }
  };
  if (Object.hasOwn(schema, "const") && value !== schema.const) invalid();
  if (schema.enum && !schema.enum.includes(value)) invalid();
  if (schema.anyOf && !schema.anyOf.some(matches)) invalid();
  if (schema.oneOf && schema.oneOf.filter(matches).length !== 1) invalid();
  if (schema.allOf)
    for (const part of schema.allOf)
      validateSchema(value, part, file, key, check);
  if (schema.if) {
    const branch = matches(schema.if) ? schema.then : schema.else;
    if (branch) validateSchema(value, branch, file, key, check);
  }
  const typeMatches = (t: string) =>
    t === "null"
      ? value === null
      : t === "object"
        ? typeof value === "object" &&
          value !== null &&
          !Array.isArray(value) &&
          !floatingNumber(value)
        : t === "array"
          ? Array.isArray(value)
          : t === "integer"
            ? isWideInteger(key)
              ? typeof value === "bigint" &&
                value >= 0n &&
                value <= MAX_SQLITE_INTEGER
              : typeof value === "number" && Number.isSafeInteger(value)
            : t === "number"
              ? numericValue(value) !== undefined &&
                Number.isFinite(numericValue(value))
              : typeof value === t;
  if (
    schema.type &&
    !(Array.isArray(schema.type) ? schema.type : [schema.type]).some(
      typeMatches,
    )
  )
    invalid();
  const number = numericValue(value);
  if (number !== undefined || typeof value === "bigint") {
    const comparable = typeof value === "bigint" ? value : number!;
    if (schema.minimum !== undefined && comparable < schema.minimum) invalid();
    if (schema.maximum !== undefined && comparable > schema.maximum) invalid();
  }
  if (typeof value === "string") {
    let length = 0;
    for (const ch of value) {
      if (length % 1024 === 0) check();
      length++;
    }
    if (
      (schema.minLength !== undefined && length < schema.minLength) ||
      (schema.maxLength !== undefined && length > schema.maxLength)
    )
      invalid();
    const label =
      /^(?:.*_id|.*_name|.*_version|version|name|mechanism|reason|device|representation_revision|index_revision|evidence_refs|dependencies)$/;
    if (
      bytes(value, check) >
      (key === "description"
        ? 8192
        : key === "source_locator"
          ? 2048
          : label.test(key)
            ? 256
            : 33554432)
    )
      invalid();
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) invalid();
    if (
      (schema.format === "date-time" && !dateTime(value, check)) ||
      (schema.format === "uuid" && !UUID.test(value))
    )
      invalid();
  }
  if (Array.isArray(value)) {
    if (
      (schema.minItems !== undefined && value.length < schema.minItems) ||
      (schema.maxItems !== undefined && value.length > schema.maxItems)
    )
      invalid();
    if (
      schema.uniqueItems &&
      new Set(value.map((v) => encodeJson(v))).size !== value.length
    )
      invalid();
    if (schema.items)
      for (const item of value)
        validateSchema(item, schema.items, file, key, check);
  }
  if (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    !floatingNumber(value)
  ) {
    const object = value as Record<string, unknown>;
    for (const required of schema.required ?? [])
      if (!Object.hasOwn(object, required)) invalid();
    for (const name in object) {
      check();
      if (!Object.hasOwn(object, name)) continue;
      const child = object[name];
      if (
        schema.additionalProperties === false &&
        !Object.hasOwn(schema.properties ?? {}, name)
      )
        invalid();
      if (schema.properties?.[name])
        validateSchema(child, schema.properties[name], file, name, check);
    }
  }
}
export function published(
  name: string,
  value: unknown,
  check: () => void = () => {},
): void {
  validateSchema(value, schemas[name], name, "", check);
  materializeNumbers(value, check);
}
