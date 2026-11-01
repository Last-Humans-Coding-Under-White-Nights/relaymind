// RM37/RM39; AC28/AC30: pinned native registration, no caller qualification switches.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";
import type { RelayMindPluginConfig } from "./config.ts";
import {
  createRecommendationClient,
  createUserActionClient,
} from "./recommendations.ts";
import { createNativeActionHandler } from "./recommendation-native-actions.ts";
import { createNativeSkillHandler } from "./recommendation-native-skill.ts";
import { createNativeMcpHandler } from "./recommendation-native-mcp.ts";
import type { NativeMcpRoute } from "./recommendation-native-mcp.ts";
import { NATIVE_QUALIFICATIONS } from "./recommendation-native-qualification.ts";
import { registryResolver } from "./recommendation-native-registry.ts";
import type { QualificationResolver } from "./recommendation-native-registry.ts";
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
/** The shipped composition: only the reviewed built-in registry and these exact bytes. */
export function shippedQualification(
  hostVersion: string,
): QualificationResolver {
  return registryResolver(
    NATIVE_QUALIFICATIONS,
    hostVersion,
    nativeAdapterRevision,
  );
}
export function registerNativeRecommendations(
  api: OpenClawPluginApi,
  config: RelayMindPluginConfig,
): void {
  if (!config.recommendations) return;
  // No qualified exact MCP route exists for the pinned host, so none is supplied.
  registerQualifiedNativeMethods(
    api,
    () => nativeRecommendationDependencies(api, config),
    shippedQualification(api.runtime.version),
    {},
  );
}
/**
 * Registers each native RPC only when its own operation is qualified. Spec 0.65:
 * `relaymind.mcp` is reserved until mcp_use is qualified for the exact route;
 * skill_loaded_use defines no RPC and is enforced by the loaded-use fence.
 */
export function registerQualifiedNativeMethods(
  api: OpenClawPluginApi,
  dependencies: () => ReturnType<typeof nativeRecommendationDependencies>,
  qualification: QualificationResolver,
  host: { mcpRoute?: NativeMcpRoute },
): void {
  if (!qualification("direct_actions")) {
    api.logger.warn(
      "Native recommendation actions unavailable: qualification pending",
    );
    return;
  }
  const shared = dependencies();
  const options = {
    scope: "operator.write",
    profileAccess: "required",
    sessionAccess: { mode: "write" },
  } as const;
  api.registerGatewayMethod(
    "relaymind.recommendations",
    createNativeActionHandler(shared),
    options,
  );
  if (qualification("exact_skill_selection"))
    api.registerGatewayMethod(
      "relaymind.skill",
      createNativeSkillHandler(shared),
      options,
    );
  const mcp = qualification("mcp_use")?.mcp;
  const route = host.mcpRoute;
  if (
    mcp &&
    route &&
    route.route === mcp.route &&
    route.transport === mcp.transport &&
    route.toolScope === mcp.toolScope
  )
    api.registerGatewayMethod(
      "relaymind.mcp",
      createNativeMcpHandler({ ...shared, qualification, route }),
      options,
    );
}
