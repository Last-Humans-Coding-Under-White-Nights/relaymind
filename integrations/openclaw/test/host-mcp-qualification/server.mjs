// Disposable MCP SDK server; only OpenClaw starts/connects/calls it (RM08, RM33; AC24).
import { appendFileSync, readFileSync } from "node:fs";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const [controlPath, eventsPath] = process.argv.slice(2);
const control = () => JSON.parse(readFileSync(controlPath, "utf8"));
const record = event => appendFileSync(eventsPath, JSON.stringify(event) + "\n");
const initial = control();
const server = new Server({ name: "relaymind-disposable-echo", version: "1.0.0" }, {
  capabilities: { tools: { listChanged: true } },
});
function schema(state) {
  return state.revision === 1 ? {
    type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false,
  } : {
    type: "object", properties: { count: { type: "integer", minimum: 1 } }, required: ["count"], additionalProperties: false,
  };
}
server.setRequestHandler(ListToolsRequestSchema, async () => {
  const state = control();
  const inputSchema = schema(state);
  record({ kind: "list", server: state.server, revision: state.revision, schema: inputSchema });
  return { tools: [{ name: "echo", description: "Harmless disposable echo marker", inputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false } }] };
});
server.setRequestHandler(CallToolRequestSchema, async request => {
  const state = control();
  if (request.params.name !== "echo") throw new Error("unknown sandbox tool");
  const args = request.params.arguments;
  if (state.revision === 1 ? typeof args?.text !== "string" : !Number.isInteger(args?.count) || args.count < 1) {
    throw new Error("invalid sandbox arguments");
  }
  record({ kind: "call", server: state.server, tool: "echo", arguments: args, result: state.marker });
  return { content: [{ type: "text", text: state.marker }] };
});
await server.connect(new StdioServerTransport());
record({ kind: "started", server: initial.server, pid: process.pid, parent_pid: process.ppid,
  credential_present: Boolean(process.env.RELAYMIND_MCP_TEST_CREDENTIAL) });
let revision = initial.revision;
setInterval(async () => {
  const state = control();
  if (state.disconnected) process.exit(0);
  if (revision !== state.revision) {
    revision = state.revision;
    await server.sendToolListChanged().catch(() => {});
  }
}, 200).unref();
