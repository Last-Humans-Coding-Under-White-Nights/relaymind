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
// Direct actions and exact Skill Library selection: reviewed signed-person composition,
// exact ordinary runtime closure and the ordinary installed-host skill selection run.
// Loaded instruction use, runtime snapshots and MCP remain unqualified.
export const NATIVE_QUALIFICATIONS: readonly NativeQualification[] =
  Object.freeze([
    Object.freeze({
      hostVersion: "2026.10.7" as const,
      adapterRevision: "f3241577e903feddd3da61fd58513b4a3e90889552d9bd7f52a302fd81f52c6c",
      operations: Object.freeze(["direct_actions", "exact_skill_selection"] as const),
      evidenceRevision: "cc5bb786dcf2417539bee72ed81e913d2805cb1f5812a96a514a279a60822e79",
    }),
  ]);
