// RM39/AC35: observe public dispatch within the original authenticated person RPC.
// No tools.invoke, synthetic requester, approval resolution or private runtime import.
import {appendFileSync, existsSync, statSync} from "node:fs";
import {definePluginEntry} from "openclaw/plugin-sdk/plugin-entry";
import {dispatchGatewayMethod} from "openclaw/plugin-sdk/gateway-method-runtime";
import {createPersonProbe} from "../host-person-qualification/probe.mjs";

const TOOL_LIMIT = 4096;
const NOTICE_LIMIT = 64;
const bounded = value => typeof value === "string" && value.length > 0 && value.length <= 256;
const isRecord = value => value !== null && typeof value === "object" && !Array.isArray(value);
const malformed = Symbol("malformed");

// Pinned 2026.10.7 tools.effective payload: {groups: [{tools: [entry]}], notices?}.
// Entries are keyed by `id`; MCP rows carry mcpServer/mcpToolName and an
// optional deniedBySession. Any other shape is malformed, never an empty list.
function effectiveRows(payload) {
  if (!isRecord(payload) || !Array.isArray(payload.groups)) return malformed;
  const rows = [];
  for (const group of payload.groups) {
    if (!isRecord(group) || !Array.isArray(group.tools)) return malformed;
    for (const tool of group.tools) {
      if (!isRecord(tool) || !bounded(tool.id) || rows.length >= TOOL_LIMIT) return malformed;
      if (tool.mcpServer === undefined && tool.mcpToolName === undefined) {
        rows.push({name: tool.id});
        continue;
      }
      if (!bounded(tool.mcpServer) || !bounded(tool.mcpToolName)) return malformed;
      rows.push({name: tool.id, server: tool.mcpServer, original: tool.mcpToolName,
        ...tool.deniedBySession === true ? {denied_by_session: true} : {}});
    }
  }
  return rows;
}

function noticeIds(payload) {
  if (payload.notices === undefined) return [];
  if (!Array.isArray(payload.notices) || payload.notices.length > NOTICE_LIMIT) return malformed;
  const ids = payload.notices.map(notice => notice?.id);
  return ids.every(bounded) ? ids : malformed;
}

// MCP tools/list result through mcp.app.listTools: {tools: [{name}]}.
function listedRows(payload) {
  if (!isRecord(payload) || !Array.isArray(payload.tools) || payload.tools.length > TOOL_LIMIT) {
    return malformed;
  }
  const names = payload.tools.map(tool => tool?.name);
  return names.every(bounded) ? names.map(name => ({name})) : malformed;
}

// Records only bounded names, MCP mapping and notice IDs; never descriptions,
// messages, schemas or the session key.
const inventory = (method, result, status = result?.ok === true ? "ok" : "refused") => {
  const effective = method === "tools.effective";
  const event = {method, status, inventory: "none", tool_names: [],
    ...effective ? {mcp_tools: [], notices: []} : {}};
  if (status !== "ok") return event;
  const rows = effective ? effectiveRows(result.payload) : listedRows(result.payload);
  const notices = effective && rows !== malformed ? noticeIds(result.payload) : [];
  if (rows === malformed || notices === malformed) return {...event, inventory: "malformed"};
  return {...event, inventory: rows.length ? "listed" : "empty",
    tool_names: rows.map(row => row.name),
    ...effective ? {mcp_tools: rows.filter(row => row.server !== undefined), notices} : {}};
};

export function observeDispatch(dispatch, record) {
  return async (method, params, options) => {
    const result = await dispatch(method, params, options);
    record(inventory(method, result));
    // Independent tools have no App view. Observe the public read-only route's
    // response without fabricating a viewId; a refusal is not a global API ban.
    if (method === "tools.effective") {
      try {
        const appResult = await dispatch("mcp.app.listTools", {}, {timeoutMs: 3000});
        record(inventory("mcp.app.listTools", appResult));
      } catch {
        record(inventory("mcp.app.listTools", undefined, "unavailable"));
      }
    }
    return result;
  };
}

export default definePluginEntry({id: "relaymind-person-qualification",
  name: "Disposable MCP use person probe", register(api) {
    const append = (path, event) => {
      if (statSync(path).size > 1048576) throw Error("capture_bound");
      appendFileSync(path, JSON.stringify({...event,
        monotonic_ns: process.hrtime.bigint().toString()}) + "\n", {mode: 0o600});
    };
    api.registerGatewayMethod("rmqualification.person", options => createPersonProbe({
      origin: api.pluginConfig.origin,
      record: event => append(api.pluginConfig.eventFile, event),
      dispatch: observeDispatch(dispatchGatewayMethod,
        event => append(api.pluginConfig.inventoryFile, event)),
      resolveWorkspace: (request, agentId) => api.runtime.agent.resolveAgentWorkspaceDir(
        request.context.getRuntimeConfig(), agentId),
      pause: async hold => {
        if (!hold) return;
        const deadline = performance.now() + 5000;
        while (!existsSync(api.pluginConfig.releaseFile)) {
          if (performance.now() >= deadline) throw Error("fixture_hold_timeout");
          await new Promise(resolve => setTimeout(resolve, 10));
        }
      },
    })(options), {scope: "operator.write", profileAccess: "required", sessionAccess: {mode: "write"}});
  },
});
