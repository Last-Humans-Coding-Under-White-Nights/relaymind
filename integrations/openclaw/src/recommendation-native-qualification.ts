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
      adapterRevision: "653902615edfd3b6bd1074f9d50d560e3a9eaf4318a39e96feb85e512e06441f",
      operations: Object.freeze(["direct_actions", "exact_skill_selection"] as const),
      evidenceRevision: "3fa0614ba5127e0e08182e40293dbc3c5f15854e2cc474f53e8c0098ce408dc3",
    }),
  ]);
