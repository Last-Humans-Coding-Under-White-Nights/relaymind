// Bounded lossless JSON for RM36/RM37. No integer token passes through Number.
export const MAX_SQLITE_INTEGER = 9223372036854775807n;
const WIDE = new Set([
  "seq",
  "next_after_seq",
  "state_version",
  "catalog_revision",
  "rechecked_catalog_revision",
  "feedback_revision",
  "expected_version",
  "expected_feedback_revision",
]);
// Keep lexical float kind until the gateway's strict integer/const checks ran.
class FloatingNumber {
  readonly value: number;
  constructor(value: number) {
    this.value = value;
  }
}
export function floatingNumber(value: unknown): value is FloatingNumber {
  return value instanceof FloatingNumber;
}
export function numericValue(value: unknown): number | undefined {
  return floatingNumber(value)
    ? value.value
    : typeof value === "number"
      ? value
      : undefined;
}
export function materializeNumbers(
  value: unknown,
  check: () => void = () => {},
): unknown {
  check();
  if (floatingNumber(value)) return value.value;
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++)
      value[i] = materializeNumbers(value[i], check);
  } else if (typeof value === "object" && value !== null) {
    for (const key in value) {
      check();
      if (!Object.hasOwn(value, key)) continue;
      const record = value as Record<string, unknown>;
      record[key] = materializeNumbers(record[key], check);
    }
  }
  return value;
}
export function wellFormed(
  value: string,
  check: () => void = () => {},
): boolean {
  let scanned = 0;
  for (let i = 0; i < value.length; i++) {
    if (scanned++ % 1024 === 0) check();
    const ch = value.charCodeAt(i);
    if (ch >= 0xd800 && ch <= 0xdbff) {
      const low = value.charCodeAt(++i);
      if (!(low >= 0xdc00 && low <= 0xdfff)) return false;
    } else if (ch >= 0xdc00 && ch <= 0xdfff) return false;
  }
  check();
  return true;
}
export function utf8Bytes(value: string, check: () => void = () => {}): number {
  let size = 0;
  let scanned = 0;
  for (let i = 0; i < value.length; i++) {
    if (scanned++ % 1024 === 0) check();
    const ch = value.charCodeAt(i);
    if (ch < 0x80) size++;
    else if (ch < 0x800) size += 2;
    else if (ch >= 0xd800 && ch <= 0xdbff) {
      const low = value.charCodeAt(++i);
      if (!(low >= 0xdc00 && low <= 0xdfff)) throw Error("invalid_json");
      size += 4;
    } else if (ch >= 0xdc00 && ch <= 0xdfff) throw Error("invalid_json");
    else size += 3;
  }
  check();
  return size;
}
export function isWideInteger(key: string): boolean {
  return WIDE.has(key);
}
export function encodeJson(value: unknown, depth = 0): string {
  if (depth > 64) throw Error("invalid_json");
  if (value === null || typeof value === "boolean" || typeof value === "string")
    return JSON.stringify(value);
  if (typeof value === "bigint") {
    if (value < 0n || value > MAX_SQLITE_INTEGER) throw Error("invalid_json");
    return String(value);
  }
  if (typeof value === "number" && Number.isFinite(value))
    return JSON.stringify(value);
  if (Array.isArray(value))
    return "[" + value.map((v) => encodeJson(v, depth + 1)).join(",") + "]";
  if (
    typeof value === "object" &&
    value !== null &&
    (Object.getPrototypeOf(value) === Object.prototype ||
      Object.getPrototypeOf(value) === null)
  )
    return (
      "{" +
      Object.entries(value)
        .map(([k, v]) => JSON.stringify(k) + ":" + encodeJson(v, depth + 1))
        .join(",") +
      "}"
    );
  throw Error("invalid_json");
}
export function decodeJson(
  text: string,
  check: () => void = () => {},
): unknown {
  let i = 0,
    tokens = 0;
  function whitespace() {
    let scanned = 0;
    while (i < text.length) {
      if (scanned++ % 1024 === 0) check();
      const ch = text.charCodeAt(i);
      if (ch !== 0x20 && ch !== 0x09 && ch !== 0x0a && ch !== 0x0d) break;
      i++;
    }
    check();
  }
  function string(): string {
    i++;
    let segment = i,
      value = "",
      scanned = 0;
    while (i < text.length) {
      if (scanned++ % 1024 === 0) {
        check();
        value += text.slice(segment, i);
        segment = i;
      }
      const ch = text[i++];
      if (ch === '"') {
        value += text.slice(segment, i - 1);
        if (!wellFormed(value, check)) throw Error("invalid_json");
        check();
        return value;
      }
      if (ch === "\\") {
        value += text.slice(segment, i - 1);
        const escaped = text[i++];
        const escapes: Record<string, string> = {
          '"': '"',
          "\\": "\\",
          "/": "/",
          b: "\b",
          f: "\f",
          n: "\n",
          r: "\r",
          t: "\t",
        };
        if (escaped === "u") {
          const hex = text.slice(i, i + 4);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw Error("invalid_json");
          value += String.fromCharCode(Number.parseInt(hex, 16));
          i += 4;
        } else if (Object.hasOwn(escapes, escaped)) {
          value += escapes[escaped];
        } else throw Error("invalid_json");
        segment = i;
      } else if (ch.charCodeAt(0) < 0x20) throw Error("invalid_json");
    }
    throw Error("invalid_json");
  }
  function parse(depth: number, key = ""): unknown {
    if (depth > 64 || ++tokens > 200000) throw Error("invalid_json");
    check();
    whitespace();
    const ch = text[i];
    if (ch === '"') return string();
    if (ch === "{") {
      i++;
      whitespace();
      const value: Record<string, unknown> = Object.create(null);
      if (text[i] === "}") {
        i++;
        return value;
      }
      while (i < text.length) {
        if (text[i] !== '"') throw Error("invalid_json");
        const k = string();
        if (Object.hasOwn(value, k)) throw Error("invalid_json");
        whitespace();
        if (text[i++] !== ":") throw Error("invalid_json");
        value[k] = parse(depth + 1, k);
        whitespace();
        const next = text[i++];
        if (next === "}") return value;
        if (next !== ",") throw Error("invalid_json");
        whitespace();
      }
    }
    if (ch === "[") {
      i++;
      whitespace();
      const value: unknown[] = [];
      if (text[i] === "]") {
        i++;
        return value;
      }
      while (i < text.length) {
        value.push(parse(depth + 1));
        whitespace();
        const next = text[i++];
        if (next === "]") return value;
        if (next !== ",") throw Error("invalid_json");
      }
    }
    for (const [literal, value] of [
      ["null", null],
      ["true", true],
      ["false", false],
    ] as const)
      if (text.startsWith(literal, i)) {
        i += literal.length;
        return value;
      }
    const match = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(
      text.slice(i, i + 257),
    );
    if (!match) throw Error("invalid_json");
    const token = match[0];
    check();
    if (token.length > 256) throw Error("invalid_json");
    i += token.length;
    if (!/[.eE]/.test(token)) {
      if (token.length > 20) throw Error("invalid_json");
      const n = BigInt(token);
      if (isWideInteger(key)) {
        if (n < 0n || n > MAX_SQLITE_INTEGER) throw Error("invalid_json");
        return n;
      }
      if (
        n > BigInt(Number.MAX_SAFE_INTEGER) ||
        n < BigInt(Number.MIN_SAFE_INTEGER)
      )
        throw Error("invalid_json");
      return Number(n);
    }
    if (isWideInteger(key)) throw Error("invalid_json");
    const n = Number(token);
    if (!Number.isFinite(n)) throw Error("invalid_json");
    return new FloatingNumber(n);
  }
  const value = parse(0);
  whitespace();
  if (i !== text.length) throw Error("invalid_json");
  check();
  return value;
}
