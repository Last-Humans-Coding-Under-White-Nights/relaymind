// Provider registration through the public plugin SDK, token handover, and the
// truthful capability statement (RM08, RM12, RM19, RM33; section 7.5).
import { describe, expect, it } from "vitest";

import entry from "../src/index.ts";
import { MODEL_ID, PROVIDER_ID } from "../src/compat.ts";
import { RelayMindConfigError } from "../src/config.ts";
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

describe("the provider (RM33: one alias, no dynamic models)", () => {
  const { logger } = recordingLogger();
  const provider = createRelayMindProvider({ pluginConfig: { baseUrl: BASE }, logger });

  it("publishes no model it did not discover", () => {
    expect(provider.resolveDynamicModel).toBeUndefined();
    expect(provider.prepareDynamicModel).toBeUndefined();
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
  });

  it("gives the reconnect path after a gateway restart", async () => {
    const hint = await provider.buildAuthDoctorHint?.({} as never);
    expect(hint).toMatch(/new application token every time it starts/);
  });
});

describe("catalog.run (the host hook)", () => {
  function context(apiKey: string | undefined, discoveryApiKey?: string) {
    return {
      config: {},
      env: {},
      signal: new AbortController().signal,
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
