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
      adapterRevision: "c18a165334e97390b089750e28150251e24c5f7a6d182ee5c0c0b8d3209f393f",
      operations: Object.freeze(["direct_actions"] as const),
      evidenceRevision: "5c44e243c1e76f0a889c34454fd55a9885516bd224d8dcaac1b768dc2a861522",
    }),
  ]);
