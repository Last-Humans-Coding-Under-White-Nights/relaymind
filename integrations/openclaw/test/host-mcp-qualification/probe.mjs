// Test-only passive provider decorator; does not execute or intercept MCP calls.
import { appendFileSync } from "node:fs";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createRelayMindProvider } from "../../dist/provider.js";
import { decorateProvider, inventoryHook } from "./capture.mjs";

export default definePluginEntry({
  id: "relaymind-mcp-qualification", name: "RelayMind MCP qualification (test only)",
  register(api) {
    const provider = createRelayMindProvider({
      pluginConfig: { baseUrl: api.pluginConfig.baseUrl }, logger: api.logger,
    });
    const emit = capture => appendFileSync(api.pluginConfig.capturePath, JSON.stringify(capture) + "\n");
    api.registerProvider(decorateProvider(provider, emit));
    api.on("before_prompt_build", inventoryHook(api.runtime, emit));
  },
});
