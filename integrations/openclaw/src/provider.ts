// The RelayMind provider for OpenClaw (RM08, RM12, RM19, RM33; spec section 7.5).
//
// Built only from the public plugin SDK. The provider publishes the client-owned
// alias the gateway serves, holds the gateway-generated application token in the
// host's credential store through the api-key auth method (env
// RELAYMIND_APP_TOKEN), and never prints it. OpenClaw owns the conversation, its
// tools, and its approvals; the provider executes nothing. It has no dynamic
// model resolution: only the discovered alias exists.
import type { ProviderCatalogContext, ProviderPlugin } from "openclaw/plugin-sdk/plugin-entry";
import { createProviderApiKeyAuthMethod } from "openclaw/plugin-sdk/provider-auth";

import { catalogResult, discoverRelayMind } from "./catalog.ts";
import { MODEL_ID, PROVIDER_ID } from "./compat.ts";
import { parsePluginConfig } from "./config.ts";
import { RECONNECT, TOKEN_HANDOVER } from "./diagnostics.ts";
import { redactingLogger, type Logger } from "./redaction.ts";

export const TOKEN_ENV_VAR = "RELAYMIND_APP_TOKEN";

export interface RelayMindProviderOptions {
  /** The plugin's own configuration (plugins.entries.relaymind.config). */
  pluginConfig: unknown;
  logger: Logger;
  /** Injected for tests; the global fetch otherwise. */
  fetch?: typeof fetch;
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
          ...(ctx.signal ? { signal: ctx.signal } : {}),
        });
        const write = discovery.diagnosis.kind === "ready" ? logger.info : logger.warn;
        write(discovery.diagnosis.message);
        // The structural shape is the host's ModelProviderConfig; maxTokens and
        // contextWindow are omitted when not configured, which the host's model
        // schema accepts (section 7.5: never estimated).
        return catalogResult(config, discovery, apiKey) as never;
      },
    },
    buildMissingAuthMessage: () => `No RelayMind application token is stored. ${TOKEN_HANDOVER}`,
    buildAuthDoctorHint: () => RECONNECT,
  };
}
