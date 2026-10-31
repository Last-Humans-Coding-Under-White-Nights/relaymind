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
      evidenceRevision: "27c2d8a1d36d59e68295b06c64e55eebca0ed31e22e6173ca34a6ee04adcd238",
    }),
  ]);
