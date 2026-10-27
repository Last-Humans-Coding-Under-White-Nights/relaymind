// Drive the pinned OpenClaw openai-completions transport against a gateway
// (RM33, AC24, #135).
//
// Usage: node drive.mjs <scenario.json> <output.json>
// Environment: RELAYMIND_BASE_URL (the gateway's /v1 URL) and RELAYMIND_APP_TOKEN.
// Writes one JSON document: the captured exchanges (sanitized), the transport's
// results, and the host tool executions. (The host logger writes to stdout, so
// the document goes to a file.)
//
// The transport is the one the host runs: `createOpenAICompletionsTransportStreamFn`
// of @openclaw/ai, under the host's own transport policy, which the public
// plugin SDK subpath `openclaw/plugin-sdk/provider-transport-runtime` installs on
// import (the host classifies a loopback URL as a local endpoint there). The
// driver plays the host: it owns the conversation and executes the sandbox host
// tool `relaymind_test_echo` itself, once per returned call; RelayMind executes
// nothing (RM08, RM33).
import "openclaw/plugin-sdk/provider-transport-runtime";
import { readFileSync, writeFileSync } from "node:fs";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";

import { relaymindModelEntry, PROVIDER_ID } from "../../src/compat.ts";
import { startRecorder } from "./recorder.mjs";

export const ECHO_TOOL = {
  name: "relaymind_test_echo",
  description: "Sandbox host tool: returns its text argument. Executes nothing else.",
  parameters: {
    type: "object",
    properties: { text: { type: "string", description: "The text to echo." } },
    required: ["text"],
    additionalProperties: false,
  },
};

/** The sandbox host tool; the driver runs it, never the gateway. */
function executeHostTool(call) {
  if (call.name !== ECHO_TOOL.name) {
    return { isError: true, text: `unknown host tool ${call.name}` };
  }
  return { isError: false, text: `echo: ${call.arguments?.text ?? ""}` };
}

function modelFor(scenario, baseUrl) {
  const entry = relaymindModelEntry({
    baseUrl,
    ...(scenario.contextWindow ? { contextWindow: scenario.contextWindow } : {}),
    ...(scenario.maxTokens ? { maxTokens: scenario.maxTokens } : {}),
  });
  const model = {
    ...entry,
    api: "openai-completions",
    provider: PROVIDER_ID,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
  if (scenario.compat === "host-default") {
    // The host's own defaults for this URL, with no declared switch.
    delete model.compat;
    model.reasoning = scenario.reasoning ?? false;
  } else if (scenario.compat && typeof scenario.compat === "object") {
    model.compat = { ...model.compat, ...scenario.compat };
  }
  if (scenario.executionMode) {
    model.headers = { "X-RelayMind-Execution-Mode": scenario.executionMode };
  }
  return model;
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
    usage: message.usage ?? null,
  };
}

async function runScenario(scenario) {
  const gatewayUrl = new URL(process.env.RELAYMIND_BASE_URL);
  const token = process.env.RELAYMIND_APP_TOKEN;
  const recorder = await startRecorder({ upstream: gatewayUrl.origin });
  const baseUrl = recorder.origin + gatewayUrl.pathname;
  const model = modelFor(scenario, baseUrl);
  const stream = createOpenAICompletionsTransportStreamFn();
  const context = {
    ...(scenario.systemPrompt ? { systemPrompt: scenario.systemPrompt } : {}),
    messages: (scenario.history ?? []).map((m) => ({ timestamp: Date.now(), ...m })),
    tools: scenario.tools === undefined ? [ECHO_TOOL] : scenario.tools,
  };
  context.messages.push({ role: "user", content: scenario.prompt, timestamp: Date.now() });
  const results = [];
  const hostToolExecutions = [];
  const maxRequests = scenario.maxRequests ?? 3;
  try {
    for (let attempt = 0; attempt < maxRequests; attempt++) {
      const controller = new AbortController();
      let timer;
      if (scenario.abortAfterMs !== undefined) {
        timer = setTimeout(() => controller.abort(), scenario.abortAfterMs);
      }
      const events = await stream(model, context, {
        apiKey: token,
        signal: controller.signal,
        streaming: scenario.stream !== false,
        ...(scenario.maxTokensOption ? { maxTokens: scenario.maxTokensOption } : {}),
        ...(scenario.toolChoice ? { toolChoice: scenario.toolChoice } : {}),
        // Operator model params (extra-params.ts) reach the body through the same
        // payload hook the host's payload policy wrapper uses.
        ...(scenario.extraBody
          ? { onPayload: (payload) => ({ ...payload, ...scenario.extraBody }) }
          : {}),
      });
      const seen = [];
      for await (const event of events) seen.push(event.type);
      const message = await events.result();
      clearTimeout(timer);
      results.push({ ...summarize(message), events: [...new Set(seen)] });
      if (message.stopReason !== "toolUse") break;
      context.messages.push(message);
      for (const call of message.content.filter((block) => block.type === "toolCall")) {
        const outcome = executeHostTool(call);
        hostToolExecutions.push({ id: call.id, name: call.name, arguments: call.arguments });
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
  } finally {
    // Let the proxy record the end of the last response.
    await new Promise((resolve) => setTimeout(resolve, 50));
    await recorder.close();
  }
  return { results, host_tool_executions: hostToolExecutions, exchanges: recorder.exchanges };
}

export function versions() {
  // Read the installed manifests directly: the packages' export maps hide them.
  const read = (name) => {
    try {
      const path = new URL(`../../node_modules/${name}/package.json`, import.meta.url);
      return JSON.parse(readFileSync(path, "utf8")).version;
    } catch {
      return null;
    }
  };
  return {
    openclaw: read("openclaw"),
    "@openclaw/ai": read("@openclaw/ai"),
    openai: read("openai"),
    node: process.version,
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
  const output = await runScenario(scenario);
  const document = { versions: versions(), scenario, ...output };
  writeFileSync(process.argv[3], `${JSON.stringify(document, null, 2)}\n`);
}
