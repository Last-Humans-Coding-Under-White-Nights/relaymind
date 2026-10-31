// RM37/RM39: reviewed qualification DATA only. Operator configuration cannot add entries.
export type NativeOperation =
  | "direct_actions"
  | "exact_skill_selection"
  | "skill_loaded_use";
export interface NativeQualification {
  readonly hostVersion: "2026.10.7";
  readonly adapterRevision: string;
  readonly operations: readonly NativeOperation[];
  readonly evidenceRevision: string;
}
// Direct actions only: reviewed signed-person composition and exact ordinary runtime closure.
// Exact skill selection/loaded use, runtime snapshots and MCP remain unqualified.
export const NATIVE_QUALIFICATIONS: readonly NativeQualification[] =
  Object.freeze([
    Object.freeze({
      hostVersion: "2026.10.7" as const,
      adapterRevision: "f3241577e903feddd3da61fd58513b4a3e90889552d9bd7f52a302fd81f52c6c",
      operations: Object.freeze(["direct_actions"] as const),
      evidenceRevision: "8e50cd026bb7da7fa6cb0f8ab93c1209d7e56c6cb793bf9fa81eccb24f1087bb",
    }),
  ]);
