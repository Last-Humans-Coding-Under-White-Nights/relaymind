// Disposable stdio fixture (RM39/AC35). Only the host launches it in qualification.
import {appendFileSync, existsSync, readFileSync, statSync} from "node:fs";
import {randomUUID} from "node:crypto";
import {Server} from "@modelcontextprotocol/sdk/server/index.js";
import {StdioServerTransport} from "@modelcontextprotocol/sdk/server/stdio.js";
import {CallToolRequestSchema, ListToolsRequestSchema} from "@modelcontextprotocol/sdk/types.js";

const [controlPath, eventsPath, sandboxPath] = process.argv.slice(2);
// A concurrent rewrite can expose a partial or absent file. Keep the last good
// state so a transient read never terminates the process or invents a state.
let lastControl;
const control = () => {
  try {
    lastControl = JSON.parse(readFileSync(controlPath, "utf8"));
  } catch {
    if (lastControl === undefined) throw Error("control_unavailable");
  }
  return lastControl;
};
const initial = control();
const record = value => {
  if (existsSync(eventsPath) && statSync(eventsPath).size > 1048576) throw Error("capture_bound");
  appendFileSync(eventsPath, JSON.stringify({...value, server: initial.server,
    monotonic_ns: process.hrtime.bigint().toString()}) + "\n", {mode: 0o600});
};
const schema = state => ({type: "object", properties: {
  text: {type: "string", minLength: 1, maxLength: 256},
  ...(state.revision > 1 ? {tag: {type: "string", maxLength: 256}} : {}),
}, required: ["text"], additionalProperties: false});
const server = new Server({name: "relaymind-disposable-append", version: "1.0.0"}, {
  capabilities: {tools: {listChanged: true}},
});
server.setRequestHandler(ListToolsRequestSchema, async () => {
  const state = control(), inputSchema = schema(state);
  record({kind: "list", original: "append", schema: inputSchema});
  return {tools: [{name: "append", description: "Append one bounded string to this disposable sandbox",
    inputSchema, annotations: {readOnlyHint: false, destructiveHint: true,
      idempotentHint: false, openWorldHint: false}}]};
});
server.setRequestHandler(CallToolRequestSchema, async request => {
  const state = control(), args = request.params.arguments;
  const call_id = randomUUID();
  // Log every actual call, including rejected ones: stale trials require zero calls.
  record({kind: "call", call_id, original: request.params.name, arguments: args});
  if (request.params.name !== "append" || typeof args?.text !== "string" ||
      !args.text.length || args.text.length > 256 ||
      Object.keys(args).some(key => !["text", ...(state.revision > 1 ? ["tag"] : [])].includes(key)) ||
      args.tag !== undefined && (typeof args.tag !== "string" || args.tag.length > 256)) {
    return {isError: true, content: [{type: "text", text: "invalid sandbox arguments"}]};
  }
  const prior = existsSync(sandboxPath) ? readFileSync(sandboxPath, "utf8") : "";
  if (prior.length > 16384) throw Error("sandbox_bound");
  const counter = prior.split("\n").filter(Boolean).length + 1;
  appendFileSync(sandboxPath, JSON.stringify(args.text) + "\n", {mode: 0o600});
  const result = `${initial.server}:${counter}`;
  record({kind: "effect", call_id, original: "append", arguments: args, counter, result});
  return {content: [{type: "text", text: result}]};
});
await server.connect(new StdioServerTransport());
record({kind: "started", pid: process.pid, parent_pid: process.ppid});
let revision = initial.revision;
setInterval(async () => {
  const state = control();
  if (state.disconnected) process.exit(0);
  if (state.revision !== revision) {
    revision = state.revision;
    await server.sendToolListChanged().catch(() => {});
  }
}, 100).unref();
