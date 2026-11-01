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

async function observe(effective: unknown, app: unknown = {ok: false, error: {code: "INVALID_REQUEST"}}) {
  const calls: Array<{method: string; params: unknown}> = [];
  const events: Array<Record<string, unknown>> = [];
  const dispatch = async (method: string, params: unknown) => {
    calls.push({method, params});
    return method === "tools.effective" ? effective : app;
  };
  const result = await observeDispatch(dispatch, (event: Record<string, unknown>) => events.push(event))(
    "tools.effective", {sessionKey: "s"}, {timeoutMs: 3000},
  );
  return {result, calls, events};
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
    {method: "tools.effective", params: {sessionKey: "s"}},
    {method: "mcp.app.listTools", params: {}},
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
  expect(events[1]).toEqual({method: "mcp.app.listTools", status: "unavailable", inventory: "none", tool_names: []});
});
