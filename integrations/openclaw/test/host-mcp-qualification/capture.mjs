// Test-only passive observation (RM08, RM09, RM12, RM16, RM33; AC24).
import { createHash } from "node:crypto";
import { getPluginToolMeta } from "openclaw/plugin-sdk/agent-harness-runtime";

export function observeTools(tools, metadata = getPluginToolMeta) {
  return tools.map(tool => {
    const mcp = metadata(tool)?.mcp;
    const schema = JSON.parse(JSON.stringify(tool.parameters));
    return {
      exposed_name: tool.name,
      source: mcp ? "mcp" : "unproven",
      mcp: mcp ? { serverName: mcp.serverName, toolName: mcp.toolName,
        ...(mcp.deniedBySession ? { deniedBySession: true } : {}) } : null,
      schema,
      schema_sha256: createHash("sha256").update(JSON.stringify(schema)).digest("hex"),
      availability: "unknown", host_revision: null, session: null,
    };
  });
}

export function decorateProvider(provider, emit) {
  return {
    ...provider,
    inspectToolSchemas(context) {
      const result = provider.inspectToolSchemas?.(context);
      // This hook runs after provider normalization. No configuration,
      // environment, credentials, or guessed session identity is serialized.
      emit({ hook: "inspectToolSchemas", provider: context.provider,
        tools: observeTools(context.tools) });
      return result;
    },
  };
}

export async function observeGatewayInventory(context, request) {
  let inventory;
  try {
    inventory = { status: "returned", result: await request("tools.effective", { sessionKey: context.sessionKey }) };
  } catch (error) {
    const refused = error instanceof Error && error.message.startsWith(
      "Gateway requests are only available to bundled or trusted official plugins.");
    inventory = { status: refused ? "refused" : "failed",
      reason: refused ? "Gateway requests are only available to bundled or trusted official plugins." : "unknown error; contents withheld" };
  }
  return { hook: "before_prompt_build", tools: [], host_context: {
    sessionKey: context.sessionKey ?? null, sessionId: context.sessionId ?? null,
  }, inventory };
}
