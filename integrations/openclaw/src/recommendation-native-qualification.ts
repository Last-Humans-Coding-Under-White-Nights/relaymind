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
      adapterRevision: "adb212d0b9f337df3d03a870711d23d5b348b135d24545913a0304dded22cfa9",
      operations: Object.freeze(["direct_actions", "exact_skill_selection"] as const),
      evidenceRevision: "babba902bb57e95cc607520e9c0db91a217fb37cdf485f7c93bc84e245c8a20f",
    }),
  ]);
