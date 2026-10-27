// Provider registration through the public plugin SDK, token handover, and the
// truthful capability statement (RM08, RM12, RM19, RM33; section 7.5).
import { describe, expect, it } from "vitest";

import entry from "../src/index.ts";
import { catalogModel, DISCOVERY_TIMEOUT_MS } from "../src/catalog.ts";
import { MODEL_ID, PROVIDER_ID, relaymindModelCompat } from "../src/compat.ts";
import { parsePluginConfig, RelayMindConfigError } from "../src/config.ts";
import { CAPABILITIES, describeCapabilities } from "../src/diagnostics.ts";
import { createRelayMindProvider } from "../src/provider.ts";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";
const BASE = "http://127.0.0.1:8080/v1";

function recordingLogger() {
  const lines: string[] = [];
  return {
    lines,
    logger: {
      debug: (m: string) => lines.push(`debug ${m}`),
      info: (m: string) => lines.push(`info ${m}`),
      warn: (m: string) => lines.push(`warn ${m}`),
      error: (m: string) => lines.push(`error ${m}`),
    },
  };
}

function fakeApi(pluginConfig: unknown) {
  const providers: unknown[] = [];
  const { lines, logger } = recordingLogger();
  const api = {
    id: PROVIDER_ID,
    name: "RelayMind",
    pluginConfig,
    logger,
    registerProvider: (provider: unknown) => providers.push(provider),
  };
  return { api, providers, lines };
}

type Provider = ReturnType<typeof createRelayMindProvider>;

describe("the plugin entry (public SDK definePluginEntry)", () => {
  it("is the relaymind plugin", () => {
    expect(entry.id).toBe(PROVIDER_ID);
    expect(typeof entry.register).toBe("function");
  });

  it("registers one provider, relaymind, with the api-key token method", () => {
    const { api, providers, lines } = fakeApi({ baseUrl: BASE });
    entry.register(api as never);
    expect(providers).toHaveLength(1);
    const provider = providers[0] as Provider;
    expect(provider.id).toBe(PROVIDER_ID);
    expect(provider.envVars).toEqual(["RELAYMIND_APP_TOKEN"]);
    expect(provider.auth).toHaveLength(1);
    expect(provider.auth[0]!.id).toBe("api-key");
    expect(provider.auth[0]!.kind).toBe("api_key");
    expect(typeof provider.catalog?.run).toBe("function");
    // The capability statement is logged once at registration.
    expect(lines.some((l) => l.startsWith("info") && l.includes("buffered"))).toBe(true);
  });

  it("refuses an invalid configuration by name and registers nothing", () => {
    const { api, providers, lines } = fakeApi({ baseUrl: BASE, executionMode: "parallel_demo" });
    expect(() => entry.register(api as never)).toThrow(RelayMindConfigError);
    expect(providers).toEqual([]);
    expect(lines.some((l) => l.startsWith("error") && l.includes("parallel_demo"))).toBe(true);
  });

  it("refuses a missing configuration by name", () => {
    const { api, providers } = fakeApi(undefined);
    expect(() => entry.register(api as never)).toThrow(/baseUrl is required/);
    expect(providers).toEqual([]);
  });
});

describe("the provider (RM33: one alias)", () => {
  const { logger } = recordingLogger();
  const provider = createRelayMindProvider({ pluginConfig: { baseUrl: BASE }, logger });

  it("resolves no model id but the alias, and makes no async preparation", () => {
    // Fact from the macOS host probe (OpenClaw 2026.10.7): headless `agent exec`
    // finds a plugin model only through resolveDynamicModel (section 7.5).
    expect(typeof provider.resolveDynamicModel).toBe("function");
    expect(provider.prepareDynamicModel).toBeUndefined();
    expect(typeof provider.catalog?.run).toBe("function");
    for (const modelId of ["relaymind", "gpt-4o", `${MODEL_ID}-2`, ""]) {
      expect(provider.resolveDynamicModel!(dynamicContext(modelId))).toBeUndefined();
    }
    const other = { ...(dynamicContext(MODEL_ID) as object), provider: "openai" };
    expect(provider.resolveDynamicModel!(other as never)).toBeUndefined();
  });

  it("tells the operator how to hand over the gateway token", () => {
    const message = provider.buildMissingAuthMessage?.({
      env: {},
      provider: PROVIDER_ID,
      listProfileIds: () => [],
    });
    expect(message).toMatch(/relaymind serve/);
    expect(message).toMatch(/openclaw models auth paste-api-key --provider relaymind/);
    expect(message).toMatch(/RELAYMIND_APP_TOKEN/);
    // The pinned host's interactive login needs a terminal (macOS probe).
    expect(message).toMatch(/models auth login.*terminal/);
  });

  it("gives the reconnect path after a gateway restart", async () => {
    const hint = await provider.buildAuthDoctorHint?.({} as never);
    expect(hint).toMatch(/new application token every time it starts/);
  });
});

function dynamicContext(modelId: string) {
  return {
    provider: PROVIDER_ID,
    modelId,
    config: {},
    modelRegistry: {
      getAll: () => [],
      getAvailable: () => [],
      find: () => undefined,
      hasConfiguredAuth: () => false,
    },
  } as never;
}

describe("resolveDynamicModel (the headless agent exec path; RM19, RM33, section 7.5)", () => {
  function resolved(pluginConfig: Record<string, unknown>, fetch?: typeof globalThis.fetch) {
    const { logger } = recordingLogger();
    const provider = createRelayMindProvider({
      pluginConfig: { baseUrl: BASE, ...pluginConfig },
      logger,
      ...(fetch ? { fetch } : {}),
    });
    return provider.resolveDynamicModel!(dynamicContext(MODEL_ID)) as Record<string, unknown> | undefined;
  }

  it("is the catalog's alias entry on the configured loopback URL, with the declared switches", () => {
    const model = resolved({});
    const expected = catalogModel(parsePluginConfig({ baseUrl: BASE }));
    expect(model).toMatchObject({ ...expected, api: "openai-completions", provider: PROVIDER_ID });
    expect(model!.compat).toEqual(relaymindModelCompat());
    expect(model!.baseUrl).toBe(BASE);
    expect(model!.reasoning).toBe(false);
    expect(model!.input).toEqual(["text"]);
    expect(model!.headers).toEqual({ "X-RelayMind-Execution-Mode": "auto" });
  });

  it("advertises only the configured limits, the same as the catalog", () => {
    const none = resolved({});
    expect(none).not.toHaveProperty("contextWindow");
    expect(none).not.toHaveProperty("maxTokens");
    const limits = { externalContextLimit: 128000, localContextLimit: 8192, maxCompletionTokens: 1024 };
    expect(resolved({ limits })).toMatchObject({ contextWindow: 128000, maxTokens: 1024 });
    expect(resolved({ limits, executionMode: "local_only" })).toMatchObject({
      contextWindow: 8192,
      headers: { "X-RelayMind-Execution-Mode": "local_only" },
    });
  });

  it("states no price (the gateway reports none)", () => {
    expect(resolved({})!.cost).toEqual({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
  });

  it("is synchronous and asks the gateway nothing", () => {
    const fetch = (async () => {
      throw new Error("must not be called");
    }) as typeof globalThis.fetch;
    const model = resolved({}, fetch);
    expect(model).toBeDefined();
    expect(model).not.toBeInstanceOf(Promise);
  });

  it("carries no credential", () => {
    const text = JSON.stringify(resolved({}));
    expect(text).not.toMatch(/authorization|apiKey|Bearer/i);
  });
});

describe("catalog.run (the host hook)", () => {
  function context(apiKey: string | undefined, discoveryApiKey?: string, withSignal = true) {
    return {
      config: {},
      env: {},
      ...(withSignal ? { signal: new AbortController().signal } : {}),
      resolveProviderApiKey: (id?: string) => {
        expect(id).toBe(PROVIDER_ID);
        return { apiKey, ...(discoveryApiKey ? { discoveryApiKey } : {}) };
      },
      resolveProviderAuth: () => ({ apiKey, mode: "api_key", source: "profile" }),
    };
  }

  it("discovers with the host credential and logs the diagnosis without it", async () => {
    const seen: string[] = [];
    const fetch = (async (url: string | URL | Request, init: RequestInit = {}) => {
      seen.push(new Headers(init.headers).get("authorization") ?? "");
      if (String(url).endsWith("/models")) {
        return Response.json({ object: "list", data: [{ id: MODEL_ID, object: "model" }] });
      }
      throw new Error(`fetch failed for ${TOKEN}`);
    }) as typeof globalThis.fetch;
    const { lines, logger } = recordingLogger();
    const provider = createRelayMindProvider({ pluginConfig: { baseUrl: BASE }, logger, fetch });
    const result = await provider.catalog!.run(context(TOKEN) as never);
    expect(seen).toEqual([`Bearer ${TOKEN}`, `Bearer ${TOKEN}`]);
    expect(result).toMatchObject({ provider: { apiKey: TOKEN, models: [{ id: MODEL_ID }] } });
    expect(lines.join("\n")).not.toContain(TOKEN);
    expect(lines.some((l) => l.startsWith("warn") && l.includes("readiness"))).toBe(true);
  });

  it("prefers the discovery credential for the discovery requests", async () => {
    const seen: string[] = [];
    const fetch = (async (_url: unknown, init: RequestInit = {}) => {
      seen.push(new Headers(init.headers).get("authorization") ?? "");
      return Response.json({ object: "list", data: [] });
    }) as typeof globalThis.fetch;
    const { logger } = recordingLogger();
    const provider = createRelayMindProvider({ pluginConfig: { baseUrl: BASE }, logger, fetch });
    await provider.catalog!.run(context("secret-ref-marker", TOKEN) as never);
    expect(seen).toEqual([`Bearer ${TOKEN}`]);
  });

  it("bounds a hung gateway with its own timeout when the host gives no signal", async () => {
    // macOS host probe: during `models list --refresh` the pinned host called
    // catalog.run with no abort signal, so only the plugin's timeout ends a hang.
    const fetch = ((_url: unknown, init: RequestInit = {}) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
      })) as typeof globalThis.fetch;
    const { lines, logger } = recordingLogger();
    const provider = createRelayMindProvider({
      pluginConfig: { baseUrl: BASE },
      logger,
      fetch,
      discoveryTimeoutMs: 50,
    });
    const started = Date.now();
    const result = await provider.catalog!.run(context(TOKEN, undefined, false) as never);
    expect(Date.now() - started).toBeLessThan(2_000);
    expect(result).toMatchObject({ outcomes: [{ provider: PROVIDER_ID, status: "unavailable" }] });
    expect(lines.some((l) => l.startsWith("warn") && l.includes("did not answer within"))).toBe(true);
  });

  it("uses a discovery timeout of five seconds by default", () => {
    expect(DISCOVERY_TIMEOUT_MS).toBe(5_000);
  });

  it("returns nothing and warns how to store a token when none is set", async () => {
    const fetch = (async () => {
      throw new Error("must not be called");
    }) as typeof globalThis.fetch;
    const { lines, logger } = recordingLogger();
    const provider = createRelayMindProvider({ pluginConfig: { baseUrl: BASE }, logger, fetch });
    expect(await provider.catalog!.run(context(undefined) as never)).toBeNull();
    expect(lines.some((l) => l.startsWith("warn") && l.includes("paste-api-key"))).toBe(true);
  });
});

describe("the capability statement (section 7.5: nothing the backend does not do)", () => {
  it("claims text and one host-owned call per buffered response only", () => {
    expect(CAPABILITIES).toEqual({
      input: ["text"],
      toolCallsPerResponse: 1,
      toolExecution: "host",
      answerDelivery: "buffered",
      liveTokenStreaming: false,
      parallelToolCalls: false,
      vision: false,
      audio: false,
      responsesApi: false,
      managedSkillSelection: false,
      managedPlacement: false,
      usage: "unknown",
    });
  });

  it("states the limits in words", () => {
    const text = describeCapabilities();
    expect(text).toMatch(/buffered/);
    expect(text).toMatch(/no live token streaming/);
    expect(text).toMatch(/no parallel tool calls/);
    expect(text).toMatch(/no managed skill selection/);
    expect(text).toMatch(/no managed device-aware placement/);
    expect(text).toMatch(/OpenClaw executes its own tools/);
    expect(text).toMatch(/usage is unknown/);
  });
});
