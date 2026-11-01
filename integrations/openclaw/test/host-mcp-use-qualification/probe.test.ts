// RM39/AC35: candidate App discovery cannot invent a view or mutate a tool.
import {expect, test} from "vitest";
// @ts-expect-error test-only JavaScript observer
import {observeDispatch} from "./probe.mjs";

// The pinned 2026.10.7 host's own grouping of effective inventory entries
// (src/agents/tools-effective-inventory-groups.ts). The tools.effective
// handler returns {agentId, profile, groups: [{id, label, source, tools}],
// notices?}; entries are keyed by `id`, never `name`.
const hostInventoryChunk = "../../node_modules/openclaw/dist/tools-effective-inventory-D_n_e8wP.mjs";
type Entry = Record<string, unknown>;
async function hostGroups(entries: Entry[]): Promise<unknown[]> {
  const chunk = (await import(hostInventoryChunk)) as {i: (entries: Entry[]) => unknown[]};
  return chunk.i(entries);
}

const entry = (id: string, source: string, extra: Entry = {}): Entry => ({
  id, label: id, description: "private description", rawDescription: "private raw", source, ...extra,
});
const mcpEntry = (id: string, server: string): Entry => entry(id, "mcp", {
  pluginId: "bundle-mcp", mcpServer: server, mcpToolName: "append",
});

const DISCOVERED = {ok: true, payload: {servers: [], onboarding: []}};

async function observe(effective: unknown, app: unknown = {ok: false, error: {code: "INVALID_REQUEST"}},
  discover: unknown = DISCOVERED) {
  const calls: Array<{method: string; params: unknown; options?: unknown}> = [];
  const events: Array<Record<string, unknown>> = [];
  const dispatch = async (method: string, params: unknown, options?: unknown) => {
    calls.push({method, params, options});
    if (method === "mcp.app.discover") {
      if (discover instanceof Error) throw discover;
      return discover;
    }
    return method === "tools.effective" ? effective : app;
  };
  const result = await observeDispatch(dispatch, (event: Record<string, unknown>) => events.push(event))(
    "tools.effective", {sessionKey: "s", agentId: "main"}, {timeoutMs: 3000},
  );
  // The session-discovery record precedes the inventory records.
  const discovered = events.shift();
  return {result, calls, events, discovered};
}

test("projects the pinned host's grouped inventory with both colliding MCP tools", async () => {
  const groups = await hostGroups([
    entry("read", "core"),
    mcpEntry("alpha-one__append", "alpha.one"),
    mcpEntry("alpha-one-2__append", "alpha?one"),
  ]);
  const inventory = {ok: true, payload: {agentId: "main", profile: "full", groups,
    notices: [{id: "mcp-server-diagnostic:alpha.one", severity: "warning", message: "private message"}]}};
  const {result, calls, events} = await observe(inventory);
  expect(result).toEqual(inventory);
  expect(calls).toEqual([
    {method: "mcp.app.discover", params: {sessionKey: "s", agentId: "main"}, options: {timeoutMs: 15000}},
    {method: "tools.effective", params: {sessionKey: "s", agentId: "main"}, options: {timeoutMs: 3000}},
    {method: "mcp.app.listTools", params: {}, options: {timeoutMs: 3000}},
  ]);
  expect(events).toEqual([
    {method: "tools.effective", status: "ok", inventory: "listed",
      tool_names: ["read", "alpha-one__append", "alpha-one-2__append"],
      mcp_tools: [
        {name: "alpha-one__append", server: "alpha.one", original: "append"},
        {name: "alpha-one-2__append", server: "alpha?one", original: "append"},
      ],
      notices: ["mcp-server-diagnostic:alpha.one"]},
    {method: "mcp.app.listTools", status: "refused", inventory: "none", tool_names: []},
  ]);
  expect(JSON.stringify(events)).not.toContain("private");
});

test("records a session-denied MCP row instead of hiding it", async () => {
  const groups = await hostGroups([{...mcpEntry("alpha-one__append", "alpha.one"), deniedBySession: true}]);
  const {events} = await observe({ok: true, payload: {agentId: "main", profile: "full", groups}});
  expect(events[0]).toEqual({method: "tools.effective", status: "ok", inventory: "listed",
    tool_names: ["alpha-one__append"],
    mcp_tools: [{name: "alpha-one__append", server: "alpha.one", original: "append", denied_by_session: true}],
    notices: []});
});

test("an empty native inventory is recorded as empty with its notices", async () => {
  const {events} = await observe({ok: true, payload: {agentId: "main", profile: "minimal",
    groups: await hostGroups([]),
    notices: [{id: "mcp-not-yet-connected", severity: "info", message: "m", servers: ["alpha.one"]}]}});
  expect(events[0]).toEqual({method: "tools.effective", status: "ok", inventory: "empty",
    tool_names: [], mcp_tools: [], notices: ["mcp-not-yet-connected"]});
});

test.each([
  ["flat legacy tools list", {agentId: "main", tools: [{name: "alpha-one__append"}]}],
  ["missing payload", undefined],
  ["groups not an array", {groups: {}}],
  ["group without tools", {groups: [{id: "core"}]}],
  ["entry without id", {groups: [{id: "core", tools: [{name: "read"}]}]}],
  ["overlong id", {groups: [{id: "core", tools: [{id: "x".repeat(257)}]}]}],
  ["non-string MCP server", {groups: [{id: "mcp", tools: [{id: "a__append", mcpServer: 1, mcpToolName: "append"}]}]}],
  ["MCP row without original", {groups: [{id: "mcp", tools: [{id: "a__append", mcpServer: "alpha.one"}]}]}],
  ["notices not an array", {groups: [], notices: "private"}],
  ["too many tools", {groups: [{id: "core", tools: Array.from({length: 4097}, (_, n) => ({id: `t${n}`}))}]}],
])("a malformed tools.effective response is explicit: %s", async (_name, payload) => {
  const {events} = await observe({ok: true, payload});
  expect(events[0]).toEqual({method: "tools.effective", status: "ok", inventory: "malformed",
    tool_names: [], mcp_tools: [], notices: []});
});

test("a refused tools.effective records no inventory", async () => {
  const {events} = await observe({ok: false, error: {code: "INVALID_REQUEST", message: "private"}});
  expect(events[0]).toEqual({method: "tools.effective", status: "refused", inventory: "none",
    tool_names: [], mcp_tools: [], notices: []});
});

test("mcp.app.listTools keeps the MCP tools/list shape", async () => {
  const groups = await hostGroups([]);
  const {events} = await observe({ok: true, payload: {agentId: "main", profile: "full", groups}},
    {ok: true, payload: {tools: [{name: "append", inputSchema: {type: "object"}}]}});
  expect(events[1]).toEqual({method: "mcp.app.listTools", status: "ok", inventory: "listed",
    tool_names: ["append"]});
  const empty = await observe({ok: true, payload: {groups}}, {ok: true, payload: {tools: []}});
  expect(empty.events[1]).toMatchObject({inventory: "empty", tool_names: []});
  const bad = await observe({ok: true, payload: {groups}}, {ok: true, payload: {groups}});
  expect(bad.events[1]).toMatchObject({inventory: "malformed", tool_names: []});
});

test("a thrown listTools dispatch is unavailable", async () => {
  const events: Array<Record<string, unknown>> = [];
  const dispatch = async (method: string) => {
    if (method === "mcp.app.listTools") throw Error("private");
    return {ok: true, payload: {groups: []}};
  };
  await observeDispatch(dispatch, (event: Record<string, unknown>) => events.push(event))(
    "tools.effective", {sessionKey: "s"}, {},
  );
  expect(events[2]).toEqual({method: "mcp.app.listTools", status: "unavailable", inventory: "none", tool_names: []});
});

// The pinned host lists MCP tools in tools.effective only from the session's
// own MCP runtime (peekSessionMcpRuntime); mcp.app.discover acquires that
// runtime through acquireSessionMcpRuntime and awaits its catalog.
test("session MCP discovery runs once, before the inventory read, with the same target", async () => {
  const {calls, discovered} = await observe({ok: true, payload: {groups: []}});
  expect(calls.map(call => call.method)).toEqual(["mcp.app.discover", "tools.effective", "mcp.app.listTools"]);
  expect(calls[0].params).toEqual(calls[1].params);
  expect(discovered).toEqual({method: "mcp.app.discover", status: "ok"});
});

test.each([
  ["MCP Apps are disabled"],
  ["The selected session harness is unavailable"],
  ["The session harness cannot open MCP Apps"],
  ["MCP App session authority is unavailable"],
  ["MCP App session changed"],
])("a known discovery refusal is recorded and the inventory is still read: %s", async message => {
  const {calls, discovered, events} = await observe({ok: true, payload: {groups: []}}, undefined,
    {ok: false, error: {code: "UNAVAILABLE", message}});
  expect(discovered).toEqual({method: "mcp.app.discover", status: "refused", error_code: "UNAVAILABLE", message});
  expect(calls.map(call => call.method)).toContain("tools.effective");
  expect(events[0]).toMatchObject({method: "tools.effective", inventory: "empty"});
});

test("an unknown discovery refusal message is not copied", async () => {
  const {discovered} = await observe({ok: true, payload: {groups: []}}, undefined,
    {ok: false, error: {code: "UNAVAILABLE", message: "connect failed: private-config /home/user"}});
  expect(discovered).toEqual({method: "mcp.app.discover", status: "refused", error_code: "UNAVAILABLE",
    message: "unclassified"});
  const odd = await observe({ok: true, payload: {groups: []}}, undefined, {ok: false, error: {code: "x".repeat(65)}});
  expect(odd.discovered).toEqual({method: "mcp.app.discover", status: "refused", error_code: "unclassified",
    message: "unclassified"});
});

test("a thrown discovery dispatch is unavailable and the inventory is still read", async () => {
  const {calls, discovered} = await observe({ok: true, payload: {groups: []}}, undefined, Error("private"));
  expect(discovered).toEqual({method: "mcp.app.discover", status: "unavailable"});
  expect(calls.map(call => call.method)).toEqual(["mcp.app.discover", "tools.effective", "mcp.app.listTools"]);
});

test("other methods pass through without discovery", async () => {
  const calls: string[] = [];
  const events: unknown[] = [];
  const dispatch = async (method: string) => { calls.push(method); return {ok: true, payload: {}}; };
  await observeDispatch(dispatch, (event: unknown) => events.push(event))("sessions.list", {}, {});
  expect(calls).toEqual(["sessions.list"]);
});
