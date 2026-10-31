// RM37/RM39; AC28/AC30: pinned native registration, no caller qualification switches.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import type { RelayMindPluginConfig } from "./config.ts";
import { PINNED_OPENCLAW } from "./compat.ts";
import {
  createRecommendationClient,
  createUserActionClient,
} from "./recommendations.ts";
import { createNativeActionHandler } from "./recommendation-native-actions.ts";
import { createNativeSkillHandler } from "./recommendation-native-skill.ts";
import { NATIVE_QUALIFICATIONS } from "./recommendation-native-qualification.ts";
import type { NativeOperation } from "./recommendation-native-qualification.ts";
import type { NativeActionBinding } from "./recommendation-native-authority.ts";

/** Exact shipped JavaScript, excluding only the separate reviewed registry data. */
export function nativeAdapterRevision(): string {
  const root = fileURLToPath(new URL("./", import.meta.url));
  const files: string[] = [];
  let total = 0;
  function visit(directory: string, prefix = "") {
    for (const item of readdirSync(directory, { withFileTypes: true })) {
      const relative = prefix + item.name;
      if (item.isSymbolicLink()) throw Error("invalid_native_package");
      if (item.isDirectory()) visit(join(directory, item.name), relative + "/");
      else if (
        item.isFile() &&
        item.name.endsWith(".js") &&
        relative !== "recommendation-native-qualification.js"
      )
        files.push(relative);
      if (files.length > 128) throw Error("invalid_native_package");
    }
  }
  visit(root);
  files.sort();
  if (!files.includes("recommendation-native.js"))
    throw Error("native_build_required");
  const digest = createHash("sha256").update("relaymind-native-adapter.v1\0");
  for (const name of files) {
    const bytes = readFileSync(join(root, name));
    total += bytes.length;
    if (total > 8 * 1024 * 1024) throw Error("invalid_native_package");
    digest
      .update(name)
      .update("\0")
      .update(String(bytes.length))
      .update("\0")
      .update(bytes);
  }
  return digest.digest("hex");
}
function qualified(operation: NativeOperation, version: string): boolean {
  if (version !== PINNED_OPENCLAW || NATIVE_QUALIFICATIONS.length === 0)
    return false;
  let revision: string;
  try {
    revision = nativeAdapterRevision();
  } catch {
    return false;
  }
  return NATIVE_QUALIFICATIONS.some(
    (entry) =>
      entry.hostVersion === version &&
      entry.adapterRevision === revision &&
      entry.operations.includes(operation),
  );
}
function environment(name: string): string {
  const value = process.env[name];
  if (typeof value !== "string" || !value || !/^[\x21-\x7e]+$/.test(value))
    throw Error("native_credentials_unavailable");
  return value;
}
function expectedScope(binding: NativeActionBinding) {
  return { userId: binding.userId, workspaceId: binding.workspaceId };
}
/** Shared production dependencies also exercised by the explicitly labeled qualification harness. */
export function nativeRecommendationDependencies(
  api: OpenClawPluginApi,
  config: RelayMindPluginConfig,
) {
  const settings = config.recommendations;
  if (!settings) throw Error("native_recommendations_not_configured");
  const actionReference = new Map<NativeActionBinding, string>(
    settings.bindings.map((binding) => [binding, binding.actionTokenEnv]),
  );
  const clientConfig = {
    baseUrl: config.baseUrl,
    applicationToken: async () => environment(settings.applicationTokenEnv),
    fetch: globalThis.fetch,
    monotonicNow: () => performance.now(),
    maxResponseBytes: 32 * 1024 * 1024,
  };
  return {
    bindings: settings.bindings,
    skills: settings.skills,
    controlUiOrigin: settings.controlUiOrigin,
    resolveWorkspace: (
      value: Parameters<typeof api.runtime.agent.resolveAgentWorkspaceDir>[0],
      agentId: string,
    ) => api.runtime.agent.resolveAgentWorkspaceDir(value, agentId),
    monotonicNow: () => performance.now(),
    wallNow: () => Date.now(),
    // Spec 0.63: every request carries the binding's own gateway scope, so a
    // shared base URL or a credential registered elsewhere fails closed.
    recommendationClient: async (binding: NativeActionBinding) => {
      if (!actionReference.has(binding)) throw Error("unregistered_person");
      return createRecommendationClient({
        ...clientConfig,
        expectedScope: expectedScope(binding),
      });
    },
    actionClient: async (binding: NativeActionBinding) => {
      const reference = actionReference.get(binding);
      if (!reference) throw Error("unregistered_person");
      return createUserActionClient({
        ...clientConfig,
        expectedScope: expectedScope(binding),
        actionToken: async () => {
          const action = environment(reference);
          if (action === environment(settings.applicationTokenEnv))
            throw Error("native_credentials_unavailable");
          return action;
        },
      });
    },
  };
}
export function registerNativeRecommendations(
  api: OpenClawPluginApi,
  config: RelayMindPluginConfig,
): void {
  if (!config.recommendations) return;
  if (!qualified("direct_actions", api.runtime.version)) {
    api.logger.warn(
      "Native recommendation actions unavailable: qualification pending",
    );
    return;
  }
  const dependencies = nativeRecommendationDependencies(api, config);
  const options = {
    scope: "operator.write",
    profileAccess: "required",
    sessionAccess: { mode: "write" },
  } as const;
  api.registerGatewayMethod(
    "relaymind.recommendations",
    createNativeActionHandler(dependencies),
    options,
  );
  if (qualified("exact_skill_selection", api.runtime.version))
    api.registerGatewayMethod(
      "relaymind.skill",
      createNativeSkillHandler(dependencies),
      options,
    );
}
