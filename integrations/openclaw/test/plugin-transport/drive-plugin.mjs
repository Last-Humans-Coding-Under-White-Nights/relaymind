// Drive the built plugin and the pinned transport against a gateway (RM33, AC24,
// #136, #137).
//
// Usage: node drive-plugin.mjs <scenario.json> <output.json>
// Environment: RELAYMIND_BASE_URL (the gateway's /v1 URL) and RELAYMIND_APP_TOKEN
// (the credential the host holds for the provider).
//
// The driver plays the host around the plugin: it calls the plugin entry's
// register() with a recording API, runs the registered provider's catalog hook
// with the host credential, and then sends the conversation through the pinned
// openai-completions transport wrapped by the provider's own wrapStreamFn, with
// the model entry the catalog published, unchanged. With `"resolve": "dynamic"`
// it plays the headless `agent exec` path the macOS host probe observed instead:
// no catalog run, the model from the provider's resolveDynamicModel, and the
// host credential for the request. A transparent recording proxy
// (../host-transport/recorder.mjs) captures the catalog and inference
// exchanges; the Authorization value is never recorded and the token is never
// written.
//
// Host tools (#137). With scenario.hostTools the driver owns sandbox tools and
// executes each returned call itself, once, then sends the result in the next
// request, the way OpenClaw does; RelayMind executes nothing. The mutating tool
// relaymind_test_write appends one line to a file in scenario.sandboxDir and is
// gated by scenario.approval ("deny" or "allow"), which stands in for the
// host's permission and approval policy: a denied call is not executed and the
// host sends an error result instead. This is the deterministic stand-in; the
// real host's approval prompt is the host CLI tier (scripts/openclaw_qualification.py
// host --scenario tool-deny|tool-approve), run by an operator or CI.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";

import entry from "../../src/index.ts";
import { startRecorder } from "../host-transport/recorder.mjs";

export const HOST_TOOLS = {
  relaymind_test_echo: {
    name: "relaymind_test_echo",
    description: "Sandbox host tool: returns its text argument. Executes nothing else.",
    parameters: {
      type: "object",
      properties: { text: { type: "string", description: "The text to echo." } },
      required: ["text"],
      additionalProperties: false,
    },
  },
  relaymind_test_write: {
    name: "relaymind_test_write",
    description: "Sandbox host tool: appends one line to a file in the test sandbox (mutating).",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", description: "The file name in the sandbox." },
        line: { type: "string", description: "The line to append." },
      },
      required: ["name", "line"],
      additionalProperties: false,
    },
  },
  // A credential argument, to show the gateway's trace redacts it (RM12).
  relaymind_test_login: {
    name: "relaymind_test_login",
    description: "Sandbox host tool: checks a sandbox credential. Executes nothing else.",
    parameters: {
      type: "object",
      properties: { user: { type: "string" }, password: { type: "string" } },
      required: ["user", "password"],
      additionalProperties: false,
    },
  },
  // Named like a gateway tool on purpose: the gateway still executes nothing.
  get_service_health: {
    name: "get_service_health",
    description: "Sandbox host tool: a canned health record for a service.",
    parameters: {
      type: "object",
      properties: { service: { type: "string" } },
      required: ["service"],
      additionalProperties: false,
    },
  },
};

const MUTATING = new Set(["relaymind_test_write"]);

/** The host's side of one call: its approval policy, then its own execution. */
function executeHostTool(scenario, call, approvals) {
  if (MUTATING.has(call.name)) {
    const decision = scenario.approval ?? "deny";
    approvals.push({ id: call.id, name: call.name, decision });
    if (decision !== "allow") {
      return {
        executed: false,
        isError: true,
        text: `denied by the host approval policy: ${call.name} was not run`,
      };
    }
    appendFileSync(join(scenario.sandboxDir, call.arguments.name), `${call.arguments.line}\n`);
    return { executed: true, isError: false, text: `wrote one line to ${call.arguments.name}` };
  }
  if (scenario.toolResults?.[call.name] !== undefined) {
    return { executed: true, isError: false, text: scenario.toolResults[call.name] };
  }
  if (call.name === "relaymind_test_echo") {
    return { executed: true, isError: false, text: `echo: ${call.arguments?.text ?? ""}` };
  }
  if (call.name === "relaymind_test_login") {
    return { executed: true, isError: false, text: "sandbox login accepted" };
  }
  if (call.name === "get_service_health") {
    const record = { service: call.arguments.service, status: "healthy" };
    return { executed: true, isError: false, text: JSON.stringify(record) };
  }
  return { executed: false, isError: true, text: `unknown host tool ${call.name}` };
}

function summarize(message) {
  return {
    stop_reason: message.stopReason,
    text: message.content
      .filter((block) => block.type === "text")
      .map((block) => block.text)
      .join(""),
    tool_calls: message.content
      .filter((block) => block.type === "toolCall")
      .map((block) => ({ id: block.id, name: block.name, arguments: block.arguments })),
    error_message: message.errorMessage ?? null,
  };
}

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

/**
 * The host's conversation for one model entry: the pinned transport wrapped by
 * the plugin's wrapStreamFn, with the host's own tool loop around it.
 */
async function converse(provider, fullModel, scenario, apiKey, output) {
  const transport = createOpenAICompletionsTransportStreamFn();
  const stream =
    provider.wrapStreamFn?.({
      provider: provider.id,
      modelId: fullModel.id,
      model: fullModel,
      extraParams: {},
      streamFn: transport,
    }) ?? transport;
  const context = {
    ...(scenario.systemPrompt ? { systemPrompt: scenario.systemPrompt } : {}),
    messages: (scenario.history ?? []).map((m) => ({ timestamp: Date.now(), ...m })),
    tools: (scenario.hostTools ?? []).map((name) => HOST_TOOLS[name]),
  };
  const content = scenario.userContent ?? scenario.prompt;
  context.messages.push({ role: "user", content, timestamp: Date.now() });
  const executions = [];
  const approvals = [];
  const maxRequests = scenario.maxRequests ?? (scenario.hostTools ? 3 : 1);
  for (let attempt = 0; attempt < maxRequests; attempt++) {
    const events = await stream(fullModel, context, {
      apiKey,
      streaming: scenario.stream !== false,
      ...(scenario.toolChoice ? { toolChoice: scenario.toolChoice } : {}),
      ...(scenario.extraBody
        ? { onPayload: (payload) => ({ ...payload, ...scenario.extraBody }) }
        : {}),
    });
    const deltas = [];
    for await (const event of events) if (event.type === "text_delta") deltas.push(event.delta);
    const message = await events.result();
    output.results.push({ ...summarize(message), text_deltas: deltas.join("") });
    if (message.stopReason !== "toolUse") break;
    context.messages.push(message);
    for (const call of message.content.filter((block) => block.type === "toolCall")) {
      const outcome = executeHostTool(scenario, call, approvals);
      if (outcome.executed) {
        executions.push({ id: call.id, name: call.name, arguments: call.arguments });
      }
      context.messages.push({
        role: "toolResult",
        toolCallId: call.id,
        toolName: call.name,
        content: [{ type: "text", text: outcome.text }],
        isError: outcome.isError,
        timestamp: Date.now(),
      });
    }
  }
  output.host_tool_executions = executions;
  output.host_approvals = approvals;
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
      await converse(provider, model, scenario, token, output);
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
    await converse(provider, routed, scenario, published.apiKey, output);
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
