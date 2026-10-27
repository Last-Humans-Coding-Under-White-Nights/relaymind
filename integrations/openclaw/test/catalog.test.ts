// Catalog discovery and diagnostics (RM08, RM19, RM26, RM33; section 7.5).
//
// The catalog asks the gateway, with the operator's token, for its aliases
// (GET /v1/models) and the readiness of the configured mode (GET /readyz). It
// publishes only relaymind-client-tools, and only when the gateway lists it. Its
// diagnosis tells an unreachable gateway, a rejected token, an absent alias,
// and a mode that is not ready apart; an aggregate /readyz 503 is never read as
// "every mode is unavailable".
import { describe, expect, it } from "vitest";

import { catalogResult, discoverRelayMind } from "../src/catalog.ts";
import { MODEL_ID, relaymindModelCompat } from "../src/compat.ts";
import { parsePluginConfig, type RelayMindPluginConfig } from "../src/config.ts";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";
const BASE = "http://127.0.0.1:8080/v1";

type Call = { url: string; init: RequestInit };

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function models(...ids: string[]) {
  return json(200, {
    object: "list",
    data: ids.map((id) => ({ id, object: "model", created: 0, owned_by: "relaymind" })),
  });
}

function readiness(status: number, modes: Record<string, { ready: boolean; reason: string | null }>) {
  return json(status, { ready: status === 200, modes, models: {}, local_model: {} });
}

const ALL_READY = {
  auto: { ready: true, reason: null },
  local_only: { ready: true, reason: null },
  external_only: { ready: true, reason: null },
};

function fakeFetch(routes: Record<string, () => Response | Promise<Response>>) {
  const calls: Call[] = [];
  const impl = async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    const route = routes[url];
    if (!route) throw new Error(`unexpected request to ${url}`);
    return route();
  };
  return { calls, fetch: impl as typeof fetch };
}

function config(extra: Record<string, unknown> = {}): RelayMindPluginConfig {
  return parsePluginConfig({ baseUrl: BASE, ...extra });
}

describe("discoverRelayMind requests (RM19, RM12)", () => {
  it("asks /v1/models and /readyz on the configured loopback gateway with the token", async () => {
    const { calls, fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    });
    const signal = new AbortController().signal;
    await discoverRelayMind({ config: config(), token: TOKEN, fetch, signal });
    expect(calls.map((c) => c.url)).toEqual([`${BASE}/models`, "http://127.0.0.1:8080/readyz"]);
    for (const call of calls) {
      expect(new Headers(call.init.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
      // A redirect could carry the credential elsewhere; it is never followed.
      expect(call.init.redirect).toBe("manual");
      expect(call.init.signal).toBeDefined();
    }
  });

  it("does not ask the gateway without a credential and says how to store one", async () => {
    const { calls, fetch } = fakeFetch({});
    const discovery = await discoverRelayMind({ config: config(), token: undefined, fetch });
    expect(calls).toEqual([]);
    expect(discovery.diagnosis.kind).toBe("no_credential");
    expect(discovery.diagnosis.message).toMatch(/RELAYMIND_APP_TOKEN/);
    expect(discovery.diagnosis.message).toMatch(/openclaw models auth paste-api-key --provider relaymind/);
    expect(discovery.models).toEqual([]);
  });

  it("propagates the host's abort instead of reporting an unreachable gateway", async () => {
    const controller = new AbortController();
    controller.abort();
    const fetch = (async (_url: unknown, init: RequestInit = {}) => {
      init.signal?.throwIfAborted();
      return models(MODEL_ID);
    }) as typeof globalThis.fetch;
    await expect(
      discoverRelayMind({ config: config(), token: TOKEN, fetch, signal: controller.signal }),
    ).rejects.toThrow();
  });
});

describe("the published entry (RM33, section 7.5)", () => {
  it("publishes only the client-owned alias with the declared switches", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models("relaymind", MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(discovery.diagnosis.kind).toBe("ready");
    expect(discovery.models).toHaveLength(1);
    const [entry] = discovery.models;
    expect(entry!.id).toBe(MODEL_ID);
    expect(entry!.compat).toEqual(relaymindModelCompat());
    expect(entry!.reasoning).toBe(false);
    expect(entry!.input).toEqual(["text"]);
    expect(entry!.headers).toEqual({ "X-RelayMind-Execution-Mode": "auto" });
  });

  it.each(["auto", "local_only", "external_only"])(
    "states the configured mode %s in the execution mode header",
    async (mode) => {
      const { fetch } = fakeFetch({
        [`${BASE}/models`]: () => models(MODEL_ID),
        "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
      });
      const discovery = await discoverRelayMind({
        config: config({ executionMode: mode }),
        token: TOKEN,
        fetch,
      });
      expect(discovery.models[0]!.headers).toEqual({ "X-RelayMind-Execution-Mode": mode });
    },
  );

  const limits = { externalContextLimit: 128000, localContextLimit: 8192, maxCompletionTokens: 1024 };

  it.each([
    ["auto", limits, 128000],
    ["external_only", limits, 128000],
    ["local_only", limits, 8192],
    // A local window in auto would refuse requests the gateway sends external.
    ["auto", { localContextLimit: 8192 }, undefined],
    ["local_only", { externalContextLimit: 128000 }, undefined],
    ["external_only", {}, undefined],
  ])("advertises the window of mode %s from the configured limits %j", async (mode, set, window) => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    });
    const discovery = await discoverRelayMind({
      config: config({ executionMode: mode, limits: set }),
      token: TOKEN,
      fetch,
    });
    const [entry] = discovery.models;
    if (window === undefined) expect("contextWindow" in entry!).toBe(false);
    else expect(entry!.contextWindow).toBe(window);
    expect(entry!.name).toContain(mode);
  });

  it("copies the output limit or omits it", async () => {
    const routes = {
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    };
    const set = await discoverRelayMind({
      config: config({ limits: { maxCompletionTokens: 1024 } }),
      token: TOKEN,
      fetch: fakeFetch(routes).fetch,
    });
    expect(set.models[0]!.maxTokens).toBe(1024);
    const unset = await discoverRelayMind({ config: config(), token: TOKEN, fetch: fakeFetch(routes).fetch });
    expect("maxTokens" in unset.models[0]!).toBe(false);
  });

  it("states which limit and mode the advertised window describes", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    });
    const discovery = await discoverRelayMind({
      config: config({ executionMode: "local_only", limits }),
      token: TOKEN,
      fetch,
    });
    expect(discovery.diagnosis.message).toMatch(/context window 8192 \(local limit, mode local_only\)/);
  });
});

describe("diagnosis: gateway, credential, and alias (section 7.5)", () => {
  it("reports a refused connection as an unreachable gateway, not an auth failure", async () => {
    const fetch = (async () => {
      throw new TypeError("fetch failed", { cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }) });
    }) as typeof globalThis.fetch;
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(discovery.diagnosis.kind).toBe("gateway_unreachable");
    expect(discovery.diagnosis.message).toContain(BASE);
    expect(discovery.diagnosis.message).toMatch(/relaymind serve/);
    expect(discovery.diagnosis.message).not.toMatch(/token/i);
    expect(discovery.models).toEqual([]);
  });

  it("reports a 401 as a rejected token with the reconnect instruction", async () => {
    const { calls, fetch } = fakeFetch({
      [`${BASE}/models`]: () =>
        json(401, { error: { code: "unauthenticated", message: "missing or invalid token" } }),
    });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(calls).toHaveLength(1);
    expect(discovery.diagnosis.kind).toBe("auth_failed");
    expect(discovery.diagnosis.message).toMatch(/new application token every time it starts/);
    expect(discovery.diagnosis.message).toMatch(/openclaw models auth paste-api-key --provider relaymind/);
    expect(discovery.diagnosis.message).not.toContain(TOKEN);
    expect(discovery.models).toEqual([]);
  });

  it("refuses a redirect and does not follow it", async () => {
    const { calls, fetch } = fakeFetch({
      [`${BASE}/models`]: () =>
        new Response(null, { status: 307, headers: { location: "https://elsewhere.example/v1/models" } }),
    });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(calls).toHaveLength(1);
    expect(discovery.diagnosis.kind).toBe("gateway_error");
    expect(discovery.diagnosis.message).toMatch(/redirect/);
    expect(discovery.diagnosis.message).not.toContain("elsewhere.example");
  });

  it("reports another gateway error with its status and code", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => json(500, { error: { code: "internal_error", message: "boom" } }),
    });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(discovery.diagnosis.kind).toBe("gateway_error");
    expect(discovery.diagnosis.message).toMatch(/500/);
    expect(discovery.diagnosis.message).toMatch(/internal_error/);
  });

  it("publishes nothing when the gateway does not serve the client-owned alias", async () => {
    const { calls, fetch } = fakeFetch({ [`${BASE}/models`]: () => models("relaymind") });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(calls).toHaveLength(1);
    expect(discovery.diagnosis.kind).toBe("alias_absent");
    expect(discovery.diagnosis.message).toMatch(/client-owned execution is not configured on the gateway/);
    expect(discovery.models).toEqual([]);
  });
});

describe("diagnosis: readiness of the configured mode (RM26, section 7.4)", () => {
  it("keeps an external_only setup usable when the aggregate /readyz is 503", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () =>
        readiness(503, {
          auto: { ready: true, reason: null },
          external_only: { ready: true, reason: null },
        }),
    });
    const discovery = await discoverRelayMind({
      config: config({ executionMode: "external_only" }),
      token: TOKEN,
      fetch,
    });
    expect(discovery.diagnosis.kind).toBe("ready");
    expect(discovery.models).toHaveLength(1);
  });

  it("names the reason the configured mode is not ready, and only for that mode", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () =>
        readiness(503, {
          auto: { ready: true, reason: null },
          local_only: { ready: false, reason: "the local model is not ready" },
          external_only: { ready: true, reason: null },
        }),
    });
    const discovery = await discoverRelayMind({
      config: config({ executionMode: "local_only" }),
      token: TOKEN,
      fetch,
    });
    expect(discovery.diagnosis.kind).toBe("mode_not_ready");
    expect(discovery.diagnosis.message).toMatch(/local_only is not ready: the local model is not ready/);
    expect(discovery.diagnosis.message).not.toMatch(/all modes|every mode/i);
    // The alias exists; a request now fails with the gateway's own error.
    expect(discovery.models).toHaveLength(1);
  });

  it("says the gateway policy does not permit a mode /readyz does not list", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () =>
        readiness(200, { auto: { ready: true, reason: null }, external_only: { ready: true, reason: null } }),
    });
    const discovery = await discoverRelayMind({
      config: config({ executionMode: "local_only" }),
      token: TOKEN,
      fetch,
    });
    expect(discovery.diagnosis.kind).toBe("mode_forbidden");
    expect(discovery.diagnosis.message).toMatch(/does not permit local_only/);
  });

  it("does not guess readiness when /readyz cannot be read", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => new Response("not json", { status: 503 }),
    });
    const discovery = await discoverRelayMind({ config: config(), token: TOKEN, fetch });
    expect(discovery.diagnosis.kind).toBe("readiness_unknown");
    expect(discovery.models).toHaveLength(1);
  });
});

describe("catalogResult (the host's catalog shape)", () => {
  it("returns the provider with the host credential reference and a ready outcome", async () => {
    const { fetch } = fakeFetch({
      [`${BASE}/models`]: () => models(MODEL_ID),
      "http://127.0.0.1:8080/readyz": () => readiness(200, ALL_READY),
    });
    const cfg = config();
    const discovery = await discoverRelayMind({ config: cfg, token: TOKEN, fetch });
    const result = catalogResult(cfg, discovery, "RELAYMIND_APP_TOKEN");
    expect(result).toEqual({
      provider: {
        baseUrl: BASE,
        api: "openai-completions",
        apiKey: "RELAYMIND_APP_TOKEN",
        models: discovery.models,
      },
      outcomes: [{ provider: "relaymind", status: "ready" }],
    });
  });

  it.each([
    ["gateway_unreachable", "unavailable"],
    ["gateway_error", "unavailable"],
    ["auth_failed", "auth-rejected"],
    ["alias_absent", "ready"],
    ["mode_not_ready", "ready"],
    ["mode_forbidden", "ready"],
    ["readiness_unknown", "ready"],
  ] as const)("maps %s to the host outcome %s", (kind, status) => {
    const result = catalogResult(config(), { diagnosis: { kind, message: "m" }, models: [] }, "k");
    expect(result).not.toBeNull();
    expect((result as { outcomes: unknown[] }).outcomes).toEqual([{ provider: "relaymind", status }]);
  });

  it("returns nothing without a credential", () => {
    const result = catalogResult(config(), { diagnosis: { kind: "no_credential", message: "m" }, models: [] }, undefined);
    expect(result).toBeNull();
  });
});

