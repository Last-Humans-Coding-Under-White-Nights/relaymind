// Test-only person/profile mechanics. No recommendation action or tool use.
import { appendFileSync, existsSync, statSync } from "node:fs";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";

export function createPersonProbe({ origin, record, dispatch = dispatchGatewayMethod,
  pause = async _hold => {}, resolveWorkspace }) {
  return async options => {
    let requestId = null;
    let reason = "admission_refused";
    let removeRetirementObserver = () => {};
    try {
      const id = options.req.id;
      // Only a non-empty string request ID is ever admitted or recorded.
      if (typeof id !== "string" || !id) throw new Error("refused");
      requestId = id;
      const { client, params, sessionMutationAuthorization: mutation, sessionAccessAuthority: access } = options;
      const target = access?.target;
      if (!client?.authenticatedUserId || !client.preparedSessionProfile?.profileId ||
          client.internal?.authenticatedControlUi !== true || client.internal?.agentToolCaller ||
          client.browserOrigin?.origin !== origin ||
          !client.connect?.device?.id || !client.presenceKey || !mutation || !target ||
          !params || typeof params !== "object" || Array.isArray(params) ||
          Object.keys(params).some(key => !["sessionKey", "agentId", "expectedSessionId", "expectedLifecycleRevision", "hold"].includes(key)) ||
          params.sessionKey !== target.sessionKey || params.expectedSessionId !== target.sessionId ||
          !target.lifecycleRevision || params.expectedLifecycleRevision !== target.lifecycleRevision ||
          params.agentId !== undefined && params.agentId !== target.agentId ||
          params.hold !== undefined && typeof params.hold !== "boolean") throw new Error("refused");
      const current = (afterAwait = false) => {
        try {
        if (client.invalidated || client.connectionSignal?.aborted || options.signal?.aborted ||
            options.hasCurrentClientAuthority?.() !== true) throw new Error("retired");
        access.assertCurrent(); mutation.assertCurrent();
        } catch (error) {
          reason = afterAwait ? "post_await_authority_retired" : "initial_authority_retired";
          throw error;
        }
      };
      current();
      if (params.hold === true) {
        const signal = client.connectionSignal;
        if (!signal || typeof signal.aborted !== "boolean" ||
            typeof signal.addEventListener !== "function" ||
            typeof signal.removeEventListener !== "function") throw new Error("missing native connection signal");
        let recorded = false;
        const onRetired = () => {
          if (recorded || signal.aborted !== true) return;
          recorded = true;
          record({ phase: "connection_retired", requestId, connectionId: client.presenceKey, connection_aborted: true });
        };
        signal.addEventListener("abort", onRetired, { once: true });
        removeRetirementObserver = () => signal.removeEventListener("abort", onRetired);
        if (signal.aborted) { onRetired(); throw new Error("native connection already retired"); }
      }
      record({ phase: "prepared", requestId, connectionId: client.presenceKey });
      try { await pause(params.hold === true); }
      catch (error) { reason = error?.message === "fixture_hold_timeout" ? "fixture_hold_timeout" : "fixture_pause_failed"; throw error; }
      current(true);
      reason = "inventory_unavailable";
      const tools = await dispatch("tools.effective", { sessionKey: target.sessionKey, agentId: target.agentId }, { timeoutMs: 3000 });
      current(true);
      if (!tools.ok) throw new Error("inventory_unavailable");
      const receipt = {
        requestId, profileId: client.preparedSessionProfile.profileId,
        authenticatedUserId: client.authenticatedUserId, authenticatedControlUi: true,
        sessionKey: target.sessionKey,
        sessionId: target.sessionId, lifecycleRevision: target.lifecycleRevision,
        connectionId: client.presenceKey, deviceId: client.connect.device.id,
      };
      if (resolveWorkspace) {
        reason = "workspace_unavailable";
        const workspaceDir = resolveWorkspace(options, target.agentId);
        current(true);
        if (typeof workspaceDir !== "string" || !workspaceDir) throw new Error("workspace_unavailable");
        receipt.workspaceDir = workspaceDir;
      }
      record({ phase: "admitted", ...receipt });
      options.respond(true, receipt, undefined);
    } catch {
      // A failing diagnostic write never prevents the fixed refusal.
      try { record({ phase: ["admission_refused", "initial_authority_retired", "post_await_authority_retired"].includes(reason) ? "guard_denied" : "probe_failed",
        requestId, connectionId: options?.client?.presenceKey ?? null, reason }); } catch {}
      if (typeof options?.respond === "function") options.respond(false, undefined, { code: "INVALID_REQUEST", message: "person/session qualification authority unavailable or changed" });
    } finally {
      removeRetirementObserver();
    }
  };
}

export default definePluginEntry({
  id: "relaymind-person-qualification", name: "Disposable person ingress qualification",
  register(api) {
    const record = event => {
      if (statSync(api.pluginConfig.eventFile).size > 1024 * 1024) throw new Error("capture_bound");
      appendFileSync(api.pluginConfig.eventFile, JSON.stringify({...event, monotonic_ns: process.hrtime.bigint().toString()}) + "\n", { encoding: "utf8" });
    };
    api.registerGatewayMethod("rmqualification.person", createPersonProbe({
      origin: api.pluginConfig.origin, record,
      resolveWorkspace: (options, agentId) => api.runtime.agent.resolveAgentWorkspaceDir(options.context.getRuntimeConfig(), agentId),
      pause: async hold => {
        if (!hold) return;
        const deadline = performance.now() + 5000;
        while (!existsSync(api.pluginConfig.releaseFile)) {
          if (performance.now() >= deadline) throw new Error("fixture_hold_timeout");
          await new Promise(resolve => setTimeout(resolve, 10));
        }
      },
    }), { scope: "operator.write", profileAccess: "required", sessionAccess: { mode: "write" } });
  },
});
