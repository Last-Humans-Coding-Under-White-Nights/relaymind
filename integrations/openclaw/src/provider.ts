// The RelayMind provider for OpenClaw (RM08, RM12, RM19, RM33; spec section 7.5).
//
// Built only from the public plugin SDK. The provider publishes the client-owned
// alias the gateway serves, holds the gateway-generated application token in the
// host's credential store through the api-key auth method (env
// RELAYMIND_APP_TOKEN), and never prints it. OpenClaw owns the conversation, its
// tools, and its approvals; the provider executes nothing.
//
// The alias is published two ways. The catalog hook discovers it on the gateway
// (and diagnoses why it is not there); resolveDynamicModel resolves the same
// entry, synchronously and without asking the gateway, for the one alias ID only.
// The pinned host needs the second: headless `openclaw agent exec` never runs the
// catalog hook and found a plugin model only through resolveDynamicModel (macOS
// host probe, OpenClaw 2026.10.7). When the gateway does not serve the alias, a
// request then fails with the gateway's own error; nothing else is resolved.
// Its stream wrapper (src/stream.ts) refuses unsupported input by name before a
// request is sent and turns an empty answer into a failure (#137).
import type {
  ProviderCatalogContext,
  ProviderPlugin,
  ProviderResolveDynamicModelContext,
} from "openclaw/plugin-sdk/plugin-entry";
import { createProviderApiKeyAuthMethod } from "openclaw/plugin-sdk/provider-auth";

import { catalogModel, catalogResult, discoverRelayMind, type RelayMindCatalogModel } from "./catalog.ts";
import { MODEL_ID, PROVIDER_ID } from "./compat.ts";
import { parsePluginConfig, type RelayMindPluginConfig } from "./config.ts";
import { RECONNECT, TOKEN_HANDOVER } from "./diagnostics.ts";
import { redactingLogger, type Logger } from "./redaction.ts";
import { relaymindStreamFn, type StreamFn } from "./stream.ts";

export const TOKEN_ENV_VAR = "RELAYMIND_APP_TOKEN";

export interface RelayMindProviderOptions {
  /** The plugin's own configuration (plugins.entries.relaymind.config). */
  pluginConfig: unknown;
  logger: Logger;
  /** Injected for tests; the global fetch otherwise. */
  fetch?: typeof fetch;
  /** Injected for tests; DISCOVERY_TIMEOUT_MS otherwise. */
  discoveryTimeoutMs?: number;
}

/** The runtime model the host resolves for the alias: the catalog entry, routed. */
export interface RelayMindRuntimeModel extends RelayMindCatalogModel {
  api: "openai-completions";
  provider: typeof PROVIDER_ID;
  /** The gateway reports no price; the host's cost fields are zero, as for local providers. */
  cost: { input: 0; output: 0; cacheRead: 0; cacheWrite: 0 };
}

export function runtimeModel(config: RelayMindPluginConfig): RelayMindRuntimeModel {
  return {
    ...catalogModel(config),
    api: "openai-completions",
    provider: PROVIDER_ID,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
}

/** The provider object; throws RelayMindConfigError for an invalid configuration. */
export function createRelayMindProvider(options: RelayMindProviderOptions): ProviderPlugin {
  const config = parsePluginConfig(options.pluginConfig);
  // The credential values seen in this process, redacted from every message.
  const secrets = new Set<string>();
  const logger = redactingLogger(options.logger, () => [...secrets]);
  return {
    id: PROVIDER_ID,
    label: "RelayMind",
    envVars: [TOKEN_ENV_VAR],
    auth: [
      createProviderApiKeyAuthMethod({
        providerId: PROVIDER_ID,
        methodId: "api-key",
        label: "RelayMind application token",
        hint: "The token `relaymind serve` printed when it started",
        optionKey: "relaymindAppToken",
        flagName: "--relaymind-app-token",
        envVar: TOKEN_ENV_VAR,
        promptMessage: "Paste the application token that `relaymind serve` printed",
        defaultModel: `${PROVIDER_ID}/${MODEL_ID}`,
        preserveExistingPrimary: true,
      }),
    ],
    catalog: {
      order: "late",
      run: async (ctx: ProviderCatalogContext) => {
        const { apiKey, discoveryApiKey } = ctx.resolveProviderApiKey(PROVIDER_ID);
        const token = discoveryApiKey ?? apiKey;
        for (const value of [apiKey, discoveryApiKey]) if (value) secrets.add(value);
        const discovery = await discoverRelayMind({
          config,
          token,
          ...(options.fetch ? { fetch: options.fetch } : {}),
          // The pinned host passes no signal here; the plugin's timeout bounds it.
          ...(ctx.signal ? { signal: ctx.signal } : {}),
          ...(options.discoveryTimeoutMs !== undefined ? { timeoutMs: options.discoveryTimeoutMs } : {}),
        });
        const write = discovery.diagnosis.kind === "ready" ? logger.info : logger.warn;
        write(discovery.diagnosis.message);
        // The structural shape is the host's ModelProviderConfig; maxTokens and
        // contextWindow are omitted when not configured, which the host's model
        // schema accepts (section 7.5: never estimated).
        return catalogResult(config, discovery, apiKey) as never;
      },
    },
    // Sync and cheap, as the host requires: no request, no credential. maxTokens
    // and contextWindow are omitted when not configured (section 7.5: never
    // estimated). The pinned host resolved the entry without limits in the
    // Linux VPS functional check (docs/evidence/2026-10-28-openclaw-vps-functional-136/).
    resolveDynamicModel: (ctx: ProviderResolveDynamicModelContext) =>
      ctx.provider === PROVIDER_ID && ctx.modelId === MODEL_ID ? (runtimeModel(config) as never) : undefined,
    // The pinned host (applyExtraParamsToAgent) calls this first, on its base
    // transport (ctx.streamFn), so the plugin is the innermost wrapper: the
    // host's generic wrappers (extra params, string content, strict message
    // keys, tools compat, payload policy, parallel_tool_calls) are layered
    // outside it and have not run when it is called. They patch the request
    // through options.onPayload, which the plugin's onPayload calls before
    // checkPayload, so the guard reads the final payload (src/stream.ts). A
    // change an outer wrapper makes outside onPayload is not seen by the guard.
    wrapStreamFn: (ctx) =>
      relaymindStreamFn(ctx.streamFn ?? hostStreamSimple),
    buildMissingAuthMessage: () => `No RelayMind application token is stored. ${TOKEN_HANDOVER}`,
    buildAuthDoctorHint: () => RECONNECT,
  };
}

/** The host's default stream function, loaded only when the host passes none. */
const hostStreamSimple: StreamFn = async (model, context, options) => {
  const { streamSimple } = await import("openclaw/plugin-sdk/llm");
  return streamSimple(model, context, options);
};
