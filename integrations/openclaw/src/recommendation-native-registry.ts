// RM37/RM39; AC28/AC30/AC35 (spec 0.65 sections 7.6.1, 7.6.2 condition 9):
// each operation is qualified only by its own reviewed entry for the exact
// host version and adapter bytes. Operator configuration never reaches here.
import { PINNED_OPENCLAW } from "./compat.ts";
import type {
  NativeOperation,
  NativeQualification,
} from "./recommendation-native-qualification.ts";

export type QualificationResolver = (
  operation: NativeOperation,
) => NativeQualification | undefined;

const OPERATIONS: readonly NativeOperation[] = [
  "direct_actions",
  "exact_skill_selection",
  "skill_loaded_use",
  "mcp_use",
];
const HEX64 = /^[a-f0-9]{64}$/;
function bounded(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    Buffer.byteLength(value, "utf8") <= 256 &&
    /^[\x21-\x7e]+$/.test(value)
  );
}
function exactKeys(value: object, required: string[], optional: string[] = []) {
  const keys = Object.keys(value);
  return (
    required.every((key) => Object.hasOwn(value, key)) &&
    keys.every((key) => required.includes(key) || optional.includes(key))
  );
}
/** A malformed entry qualifies nothing, not even its other operations. */
export function wellFormedEntry(entry: unknown): entry is NativeQualification {
  if (typeof entry !== "object" || entry === null || Array.isArray(entry))
    return false;
  const value = entry as Record<string, any>;
  if (
    !exactKeys(
      value,
      ["hostVersion", "adapterRevision", "operations", "evidenceRevision"],
      ["mcp"],
    ) ||
    value.hostVersion !== PINNED_OPENCLAW ||
    typeof value.adapterRevision !== "string" ||
    !HEX64.test(value.adapterRevision) ||
    typeof value.evidenceRevision !== "string" ||
    !HEX64.test(value.evidenceRevision) ||
    !Array.isArray(value.operations) ||
    value.operations.length === 0 ||
    new Set(value.operations).size !== value.operations.length ||
    value.operations.some((item: unknown) => !OPERATIONS.includes(item as any))
  )
    return false;
  const listsMcp = value.operations.includes("mcp_use");
  if (!listsMcp) return value.mcp === undefined;
  const mcp = value.mcp;
  return (
    typeof mcp === "object" &&
    mcp !== null &&
    !Array.isArray(mcp) &&
    exactKeys(mcp, ["route", "transport", "toolScope", "maxApprovalWaitMs"]) &&
    bounded(mcp.route) &&
    bounded(mcp.transport) &&
    bounded(mcp.toolScope) &&
    Number.isSafeInteger(mcp.maxApprovalWaitMs) &&
    mcp.maxApprovalWaitMs > 0
  );
}
export function registryResolver(
  entries: readonly NativeQualification[],
  hostVersion: string,
  adapterRevision: () => string,
): QualificationResolver {
  // The loaded adapter bytes cannot change while this process runs: digest
  // them once, on first use, and keep a failure as a failure.
  let revision: string | null | undefined;
  const loaded = (): string | null => {
    if (revision === undefined) {
      try {
        revision = adapterRevision();
      } catch {
        revision = null;
      }
    }
    return revision;
  };
  return (operation) => {
    if (
      !Array.isArray(entries) ||
      hostVersion !== PINNED_OPENCLAW ||
      entries.length === 0
    )
      return undefined;
    const revision = loaded();
    if (revision === null) return undefined;
    return entries.find(
      (entry) =>
        wellFormedEntry(entry) &&
        entry.hostVersion === hostVersion &&
        entry.adapterRevision === revision &&
        entry.operations.includes(operation),
    );
  };
}
