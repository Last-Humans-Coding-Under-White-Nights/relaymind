// Test-only instrumentation for #189 (RM08, RM12, RM19, RM33, AC24).
// Registration identifies one disposable application/session, never a person.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";

export function createProbeCommand(config, gatewayRequest, gatewayDispatch = dispatchGatewayMethod) {
  // Kept outside command arguments, descriptions, tool schemas and model inputs.
  const credential = readFileSync(config.credentialFile, "utf8").trim();
  return {
    name: "rmqualification", description: "Disposable direct command qualification",
    acceptsArgs: true, requireAuth: true, requiredScopes: ["operator.write"],
    async handler(ctx) {
      const scopes = ctx.gatewayClientScopes ?? [];
      if (!ctx.isAuthorizedSender || !scopes.some(s => s === "operator.write" || s === "operator.admin") ||
          !Object.entries(config.registration).every(([key, value]) => ctx[key] === value)) {
        return { text: "qualification registration or authorization missing" };
      }
      if (ctx.args === "library" || ctx.args === "library-dispatch") {
        try {
          await (ctx.args === "library" ? gatewayRequest : gatewayDispatch)("skills.library.activate", {
            action: "attach", sessionKey: ctx.sessionKey,
            skillId: "disposable-qualification", revision: config.skill.sha256,
          });
          return { text: "library bridge accepted; loading not yet verified" };
        } catch (error) {
          // Only these static host boundary errors may be returned. Do not expose
          // arbitrary exception text or replace an unknown error with a diagnosis.
          const known = error instanceof Error && (
            error.message.startsWith("Gateway method dispatch is reserved") ||
            error.message.startsWith("Gateway requests are only available to bundled or trusted official plugins.")
          );
          return { text: `library bridge unavailable${known ? `: ${error.message}` : ""}` };
        }
      }
      if (ctx.args !== "recommend" && ctx.args !== "choose 2") return { text: "invalid qualification action" };
      let skillSha256;
      try {
        skillSha256 = createHash("sha256").update(readFileSync(config.skill.path)).digest("hex");
      } catch {
        return { text: "skill unavailable or changed" };
      }
      if (skillSha256 !== config.skill.sha256) return { text: "skill unavailable or changed" };
      const host = Object.fromEntries([
        "senderId", "channel", "isAuthorizedSender", "gatewayClientScopes", "agentId", "sessionKey", "sessionId",
      ].filter(key => ctx[key] !== undefined).map(key => [key, ctx[key]]));
      const action = ctx.args === "recommend" ? "recommend" : "choose";
      const response = await fetch(config.recorderUrl, {
        method: "POST", headers: { authorization: `Bearer ${credential}`, "content-type": "application/json" },
        body: JSON.stringify({ action, ...(action === "choose" ? { choice: 2 } : {}), host, skillSha256 }),
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) return { text: "qualification recorder rejected action" };
      return { text: action === "choose" ? "qualification choice 2 recorded" : "qualification recommendation: 2 (disposable skill)" };
    },
  };
}

export default definePluginEntry({
  id: "relaymind-command-qualification", name: "RelayMind command qualification",
  register(api) {
    api.registerCommand(createProbeCommand(api.pluginConfig, api.runtime.gateway.request));
  },
});
