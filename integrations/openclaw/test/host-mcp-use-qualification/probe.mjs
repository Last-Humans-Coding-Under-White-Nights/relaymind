// RM39/AC35: observe public dispatch within the original authenticated person RPC.
// No tools.invoke, synthetic requester, approval resolution or private runtime import.
import {appendFileSync, existsSync, statSync} from "node:fs";
import {definePluginEntry} from "openclaw/plugin-sdk/plugin-entry";
import {dispatchGatewayMethod} from "openclaw/plugin-sdk/gateway-method-runtime";
import {createPersonProbe} from "../host-person-qualification/probe.mjs";

const inventory = (method, result) => ({method,
  status: result?.ok === true ? "ok" : "refused",
  tool_names: Array.isArray(result?.payload?.tools)
    ? result.payload.tools.slice(0, 4096).map(tool => tool?.name)
      .filter(name => typeof name === "string" && name.length <= 256) : [],
});

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
        record({method: "mcp.app.listTools", status: "unavailable", tool_names: []});
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
