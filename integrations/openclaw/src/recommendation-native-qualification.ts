// RM37/RM39: reviewed qualification DATA only. Operator configuration cannot add entries.
export type NativeOperation =
  | "direct_actions"
  | "exact_skill_selection"
  | "skill_loaded_use"
  | "mcp_use";
/** Spec 0.65 7.6.2 condition 9: the reviewed exact route, transport and tool class. */
export interface NativeMcpQualification {
  readonly route: string;
  readonly transport: string;
  readonly toolScope: string;
  /** The finite maximum approval wait declared before an operation starts. */
  readonly maxApprovalWaitMs: number;
}
export interface NativeQualification {
  readonly hostVersion: "2026.10.7";
  readonly adapterRevision: string;
  readonly operations: readonly NativeOperation[];
  readonly evidenceRevision: string;
  /** Present exactly when operations lists mcp_use. */
  readonly mcp?: NativeMcpQualification;
}
// Direct actions and exact Skill Library selection: reviewed signed-person composition,
// exact ordinary runtime closure and the ordinary installed-host skill selection run.
// Loaded instruction use, runtime snapshots and MCP remain unqualified.
export const NATIVE_QUALIFICATIONS: readonly NativeQualification[] =
  Object.freeze([
    Object.freeze({
      hostVersion: "2026.10.7" as const,
      adapterRevision: "37f9b415c47260065d42453b5d85474375a796b8a7416af78732668863357f2d",
      operations: Object.freeze(["direct_actions", "exact_skill_selection"] as const),
      evidenceRevision: "1212d0e28080b08999f936b70416a70c6cef14802eef35d4553a45682a65b093",
    }),
  ]);
