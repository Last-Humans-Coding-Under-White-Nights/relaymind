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
// Empty until the actual composed person/session/action/use controls are reviewed.
export const NATIVE_QUALIFICATIONS: readonly NativeQualification[] =
  Object.freeze([]);
