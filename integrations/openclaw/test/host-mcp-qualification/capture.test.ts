// RM08, RM09, RM12, RM16, RM33; AC24: passive capture preserves host ownership.
import { expect, it } from "vitest";
// @ts-expect-error test-only plain JS is loaded unchanged by the host
import { observeTools, decorateProvider } from "./capture.mjs";

const tool = { name: "alpha__echo", parameters: {
  type: "object", properties: { text: { type: "string" } }, required: ["text"],
} };

it("never infers MCP provenance from a client function name", () => {
  const [record] = observeTools([tool]);
  expect(record.source).toBe("unproven");
  expect(record.mcp).toBeNull();
  expect(record.schema).toEqual(tool.parameters);
  expect(record.availability).toBe("unknown");
  expect(record.host_revision).toBeNull();
  expect(record.session).toBeNull();
});

it("reads schema and provenance from the same instance, keeping duplicate original names distinct", () => {
  const second = { ...tool, name: "alpha-2__echo" };
  const metadata = new Map([
    [tool, { pluginId: "bundle-mcp", mcp: { serverName: "alpha.one", toolName: "echo" } }],
    [second, { pluginId: "bundle-mcp", mcp: { serverName: "alpha?one", toolName: "echo" } }],
  ]);
  const records = observeTools([tool, second], (instance: typeof tool) => metadata.get(instance));
  expect(records.map((r: any) => [r.exposed_name, r.mcp.serverName, r.mcp.toolName])).toEqual([
    ["alpha__echo", "alpha.one", "echo"], ["alpha-2__echo", "alpha?one", "echo"],
  ]);
  expect(records[0].schema).toEqual(tool.parameters);
  expect(records[0].schema_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(records.every((r: any) => r.availability === "unknown" && r.host_revision === null)).toBe(true);
});

it("captures post-normalization tools and preserves original hook results and objects", () => {
  const normalized = { ...tool, parameters: { type: "object", properties: { count: { type: "integer" } } } };
  const diagnostics = [{ toolName: "example", violations: ["example"] }];
  const captured: any[] = [];
  const provider = {
    normalizeToolSchemas: () => [normalized], inspectToolSchemas: () => diagnostics,
    marker: "unchanged",
  };
  const decorated = decorateProvider(provider, (record: any) => captured.push(record));
  const context = { tools: [tool], config: { header: "never-export-this-secret" }, env: { TOKEN: "secret" } };
  expect(decorated.normalizeToolSchemas(context)[0]).toBe(normalized);
  expect(decorated.inspectToolSchemas({ ...context, tools: [normalized] })).toBe(diagnostics);
  expect(captured.at(-1).tools[0].schema).toEqual(normalized.parameters);
  expect(JSON.stringify(captured)).not.toContain("never-export-this-secret");
  expect(JSON.stringify(captured)).not.toContain('"TOKEN"');
  expect(decorated.marker).toBe("unchanged");
});

it("records changed schemas as distinct observations without inventing freshness", () => {
  const before = observeTools([tool])[0];
  const after = observeTools([{ ...tool, parameters: { type: "object", required: ["count"] } }])[0];
  expect(after.schema_sha256).not.toBe(before.schema_sha256);
  expect(after.availability).toBe("unknown");
  expect(after.host_revision).toBeNull();
});

it("records the real plugin inventory boundary without exporting arbitrary error contents", async () => {
  // @ts-expect-error test-only JS
  const { observeGatewayInventory } = await import("./capture.mjs");
  const record = await observeGatewayInventory({ sessionKey: "agent:main:sandbox", sessionId: "actual-session" },
    async (method: string, params: unknown) => {
      expect(method).toBe("tools.effective");
      expect(params).toEqual({ sessionKey: "agent:main:sandbox" });
      throw new Error('Gateway requests are only available to bundled or trusted official plugins. Plugin "relaymind-mcp-qualification" is neither. See https://docs.openclaw.ai/plugins/sdk-runtime#api-runtime-gateway');
    });
  expect(record.host_context).toEqual({ sessionKey: "agent:main:sandbox", sessionId: "actual-session" });
  expect(record.inventory.status).toBe("refused");
  const unknown = await observeGatewayInventory({ sessionKey: "agent:main:sandbox" },
    async () => { throw new Error("sensitive unexpected error"); });
  expect(unknown.inventory.status).toBe("failed");
  expect(JSON.stringify(unknown)).not.toContain("sensitive unexpected error");
});
