// Drive the built plugin and the pinned transport against a gateway (RM33, AC24, #136).
//
// Usage: node drive-plugin.mjs <scenario.json> <output.json>
// Environment: RELAYMIND_BASE_URL (the gateway's /v1 URL) and RELAYMIND_APP_TOKEN
// (the credential the host holds for the provider).
//
// The driver plays the host around the plugin: it calls the plugin entry's
// register() with a recording API, runs the registered provider's catalog hook
// with the host credential, and then sends one request through the pinned
// openai-completions transport with the model entry the catalog published,
// unchanged. With `"resolve": "dynamic"` it plays the headless `agent exec`
// path the macOS host probe observed instead: no catalog run, the model from
// the provider's resolveDynamicModel, and the host credential for the request.
// A transparent recording proxy (../host-transport/recorder.mjs) captures the
// catalog and inference exchanges; the Authorization value is never recorded
// and the token is never written.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";

import entry from "../../src/index.ts";
import { startRecorder } from "../host-transport/recorder.mjs";

function recordingApi(pluginConfig) {
  const logs = [];
  const providers = [];
  const log = (level) => (message) => logs.push({ level, message });
  return {
    logs,
    providers,
    api: {
      id: "relaymind",
      name: "RelayMind",
      pluginConfig,
      logger: { debug: log("debug"), info: log("info"), warn: log("warn"), error: log("error") },
      registerProvider: (provider) => providers.push(provider),
    },
  };
}

/** One request through the pinned transport; the host's view of the answer. */
async function send(model, scenario, apiKey) {
  const stream = createOpenAICompletionsTransportStreamFn();
  const events = await stream(
    model,
    {
      ...(scenario.systemPrompt ? { systemPrompt: scenario.systemPrompt } : {}),
      messages: [{ role: "user", content: scenario.prompt, timestamp: Date.now() }],
      tools: [],
    },
    { apiKey, streaming: scenario.stream !== false },
  );
  for await (const _event of events);
  const message = await events.result();
  return {
    stop_reason: message.stopReason,
    text: message.content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join(""),
    error_message: message.errorMessage ?? null,
  };
}

async function runScenario(scenario) {
  const gatewayUrl = new URL(process.env.RELAYMIND_BASE_URL);
  const token = process.env.RELAYMIND_APP_TOKEN;
  const recorder = await startRecorder({ upstream: gatewayUrl.origin });
  const baseUrl = scenario.baseUrl ?? recorder.origin + gatewayUrl.pathname;
  const { api, logs, providers } = recordingApi({ ...scenario.pluginConfig, baseUrl });
  const output = { register_error: null, catalog: null, dynamic: null, results: [] };
  try {
    try {
      entry.register(api);
    } catch (error) {
      output.register_error = { name: error.name, message: error.message };
      return output;
    }
    const [provider] = providers;
    if (scenario.resolve === "dynamic") {
      const model = provider.resolveDynamicModel({
        provider: provider.id,
        modelId: "relaymind-client-tools",
        config: {},
        modelRegistry: {
          getAll: () => [],
          getAvailable: () => [],
          find: () => undefined,
          hasConfiguredAuth: () => false,
        },
      });
      output.dynamic = model ?? null;
      if (!model || scenario.prompt === undefined) return output;
      output.results.push(await send(model, scenario, token));
      return output;
    }
    const result = await provider.catalog.run({
      config: {},
      env: {},
      // The pinned host gives catalog.run no abort signal (macOS host probe).
      resolveProviderApiKey: () => ({ apiKey: token }),
      resolveProviderAuth: () => ({ apiKey: token, mode: "api_key", source: "profile" }),
    });
    const published = result?.provider;
    output.catalog = {
      result: result === null ? null : "provider",
      outcomes: result?.outcomes ?? null,
      api: published?.api ?? null,
      base_url_is_configured: published ? published.baseUrl === baseUrl : null,
      api_key_is_the_host_credential: published ? published.apiKey === token : null,
      models: published?.models ?? [],
    };
    const [model] = published?.models ?? [];
    if (!model || scenario.prompt === undefined) return output;
    const routed = { ...model, api: published.api, provider: provider.id, baseUrl: published.baseUrl };
    output.results.push(await send(routed, scenario, published.apiKey));
    return output;
  } finally {
    output.logs = logs;
    await new Promise((resolve) => setTimeout(resolve, 50));
    await recorder.close();
    output.exchanges = recorder.exchanges;
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
  const output = await runScenario(scenario);
  writeFileSync(process.argv[3], `${JSON.stringify({ scenario, ...output }, null, 2)}\n`);
}
