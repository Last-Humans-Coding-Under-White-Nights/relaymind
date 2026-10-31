// The RelayMind OpenClaw plugin entry (RM33, spec section 7.5).
//
// The host loads the built dist/index.js (package.json openclaw.extensions) and
// calls register() with its plugin API. An invalid configuration is refused by
// name before anything is registered or sent.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { parsePluginConfig } from "./config.ts";
import { registerNativeRecommendations } from "./recommendation-native.ts";

import { PROVIDER_ID } from "./compat.ts";
import { describeCapabilities } from "./diagnostics.ts";
import { createRelayMindProvider } from "./provider.ts";

export default definePluginEntry({
  id: PROVIDER_ID,
  name: "RelayMind",
  description:
    "RelayMind local gateway as an OpenClaw model provider (relaymind-client-tools; OpenClaw keeps its tools)",
  register(api) {
    let provider;
    try {
      provider = createRelayMindProvider({ pluginConfig: api.pluginConfig, logger: api.logger });
    } catch (error) {
      api.logger.error(error instanceof Error ? error.message : String(error));
      throw error;
    }
    api.registerProvider(provider);
    registerNativeRecommendations(api, parsePluginConfig(api.pluginConfig));
    api.logger.info(describeCapabilities());
  },
});
