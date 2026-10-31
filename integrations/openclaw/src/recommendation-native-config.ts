// RM37/RM39; AC28/AC30: operator mappings only; parsing never resolves credentials.
import { isAbsolute } from "node:path";
import { object, identifier } from "./recommendation-contract.ts";
import { wellFormed } from "./recommendation-json.ts";
import type { NativeActionBinding } from "./recommendation-native-authority.ts";
export interface NativeCredentialBinding extends NativeActionBinding {
  readonly actionTokenEnv: string;
}
export interface NativeSkillBinding {
  readonly assetId: string;
  readonly contentHash: string;
  readonly sourceVersion: string | null;
  readonly skillId: string;
  readonly revision: string;
}
export interface NativeRecommendationConfig {
  readonly controlUiOrigin: string;
  readonly applicationTokenEnv: string;
  readonly bindings: readonly NativeCredentialBinding[];
  readonly skills: readonly NativeSkillBinding[];
}
function text(value: unknown, maximum = 256): asserts value is string {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    !wellFormed(value) ||
    value.includes("\0") ||
    Buffer.byteLength(value, "utf8") > maximum
  )
    throw Error();
}
function reference(value: unknown): asserts value is string {
  if (
    typeof value !== "string" ||
    !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(value)
  )
    throw Error();
}
function unique(seen: Set<string>, parts: unknown[]): void {
  const key = JSON.stringify(parts);
  if (seen.has(key)) throw Error();
  seen.add(key);
}
export function parseNativeRecommendationConfig(
  value: unknown,
): NativeRecommendationConfig {
  try {
    object(
      value,
      ["controlUiOrigin", "applicationTokenEnv", "bindings"],
      ["skills"],
    );
    text(value.controlUiOrigin, 2048);
    const origin = new URL(value.controlUiOrigin);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      origin.origin !== value.controlUiOrigin ||
      origin.username ||
      origin.password
    )
      throw Error();
    reference(value.applicationTokenEnv);
    if (
      !Array.isArray(value.bindings) ||
      value.bindings.length < 1 ||
      value.bindings.length > 1000
    )
      throw Error();
    const native = new Set<string>(),
      scopes = new Set<string>(),
      secrets = new Set<string>();
    const bindings: NativeCredentialBinding[] = value.bindings.map((item) => {
      object(item, [
        "profileId",
        "authenticatedUserId",
        "agentId",
        "workspaceDir",
        "userId",
        "workspaceId",
        "actionTokenEnv",
      ]);
      for (const key of [
        "profileId",
        "authenticatedUserId",
        "agentId",
        "userId",
        "workspaceId",
      ])
        text(item[key]);
      text(item.workspaceDir, 2048);
      if (!isAbsolute(item.workspaceDir)) throw Error();
      reference(item.actionTokenEnv);
      unique(native, [
        item.profileId,
        item.authenticatedUserId,
        item.agentId,
        item.workspaceDir,
      ]);
      unique(scopes, [item.userId, item.workspaceId]);
      unique(secrets, [item.actionTokenEnv.toUpperCase()]);
      return Object.freeze({
        profileId: item.profileId,
        authenticatedUserId: item.authenticatedUserId,
        agentId: item.agentId,
        workspaceDir: item.workspaceDir,
        userId: item.userId,
        workspaceId: item.workspaceId,
        actionTokenEnv: item.actionTokenEnv,
      });
    });
    const rawSkills = value.skills ?? [];
    if (!Array.isArray(rawSkills) || rawSkills.length > 1000) throw Error();
    const skillKeys = new Set<string>();
    const skills: NativeSkillBinding[] = rawSkills.map((item) => {
      object(item, [
        "assetId",
        "contentHash",
        "sourceVersion",
        "skillId",
        "revision",
      ]);
      text(item.assetId);
      identifier(item.skillId);
      if (
        typeof item.contentHash !== "string" ||
        !/^[a-f0-9]{64}$/.test(item.contentHash) ||
        typeof item.revision !== "string" ||
        !/^[a-f0-9]{64}$/.test(item.revision)
      )
        throw Error();
      if (item.sourceVersion !== null) text(item.sourceVersion);
      unique(skillKeys, [item.assetId, item.contentHash, item.sourceVersion]);
      return Object.freeze({
        assetId: item.assetId,
        contentHash: item.contentHash,
        sourceVersion: item.sourceVersion,
        skillId: item.skillId,
        revision: item.revision,
      });
    });
    return Object.freeze({
      controlUiOrigin: value.controlUiOrigin,
      applicationTokenEnv: value.applicationTokenEnv,
      bindings: Object.freeze(bindings),
      skills: Object.freeze(skills),
    });
  } catch {
    throw new Error("invalid native recommendation configuration");
  }
}
