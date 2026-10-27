// The plugin configuration: loopback base URL, execution mode, copied limits
// (RM19, RM12, RM26, RM33; spec sections 7.1 and 7.5).
import { describe, expect, it } from "vitest";

import { RelayMindConfigError, parsePluginConfig } from "../src/config.ts";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";

function errorOf(raw: unknown): RelayMindConfigError {
  try {
    parsePluginConfig(raw);
  } catch (error) {
    expect(error).toBeInstanceOf(RelayMindConfigError);
    return error as RelayMindConfigError;
  }
  throw new Error("expected a configuration error");
}

describe("baseUrl (RM19, section 7.5: loopback only)", () => {
  it.each([
    ["http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"],
    ["http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"],
    ["http://localhost:8080/v1", "http://localhost:8080/v1"],
    ["http://[::1]:8080/v1", "http://[::1]:8080/v1"],
    ["http://127.1.2.3:9000/v1", "http://127.1.2.3:9000/v1"],
  ])("accepts the loopback URL %s", (raw, normalized) => {
    expect(parsePluginConfig({ baseUrl: raw }).baseUrl).toBe(normalized);
  });

  it.each([
    "http://192.168.1.2:8080/v1",
    "https://example.com/v1",
    "http://10.0.0.1:8080/v1",
    "http://[::ffff:192.168.1.2]:8080/v1",
    "http://localhost.example.com:8080/v1",
    "http://0.0.0.0:8080/v1",
  ])("refuses the non-loopback URL %s", (raw) => {
    expect(errorOf({ baseUrl: raw }).message).toMatch(/loopback/);
  });

  it("refuses a URL with credentials and never echoes them", () => {
    const error = errorOf({ baseUrl: `http://user:${TOKEN}@127.0.0.1:8080/v1` });
    expect(error.message).toMatch(/credentials/);
    expect(error.message).not.toContain(TOKEN);
  });

  it("refuses a URL that is not the gateway's /v1 root", () => {
    expect(errorOf({ baseUrl: "http://127.0.0.1:8080" }).message).toMatch(/\/v1/);
    expect(errorOf({ baseUrl: "http://127.0.0.1:8080/v1?x=1" }).message).toMatch(/query/);
  });

  it("refuses a scheme other than http or https", () => {
    expect(errorOf({ baseUrl: "file:///v1" }).message).toMatch(/http/);
  });

  it("names a missing or unreadable configuration", () => {
    expect(errorOf(undefined).message).toMatch(/baseUrl is required/);
    expect(errorOf({}).message).toMatch(/baseUrl is required/);
    expect(errorOf({ baseUrl: "not a url" }).message).toMatch(/not a valid URL/);
    expect(errorOf("http://127.0.0.1:8080/v1").message).toMatch(/object/);
  });
});

describe("executionMode (section 7.5: X-RelayMind-Execution-Mode)", () => {
  const base = { baseUrl: "http://127.0.0.1:8080/v1" };

  it.each(["auto", "local_only", "external_only"])("accepts %s", (mode) => {
    expect(parsePluginConfig({ ...base, executionMode: mode }).executionMode).toBe(mode);
  });

  it("is auto when not set, and auto is still a stated mode", () => {
    expect(parsePluginConfig(base).executionMode).toBe("auto");
  });

  it("refuses parallel_demo for this alias, never changing it", () => {
    const message = errorOf({ ...base, executionMode: "parallel_demo" }).message;
    expect(message).toMatch(/parallel_demo/);
    expect(message).toMatch(/auto, local_only, external_only/);
  });

  it("refuses an unknown mode by name with the allowed set", () => {
    const message = errorOf({ ...base, executionMode: "turbo" }).message;
    expect(message).toContain('"turbo"');
    expect(message).toMatch(/auto, local_only, external_only/);
  });

  it("does not echo a value that may be a secret", () => {
    const message = errorOf({ ...base, executionMode: TOKEN }).message;
    expect(message).not.toContain(TOKEN);
  });
});

describe("limits (section 7.5: copied from the gateway configuration, never estimated)", () => {
  const base = { baseUrl: "http://127.0.0.1:8080/v1" };

  it("has no limit unless the operator set one", () => {
    expect(parsePluginConfig(base).limits).toEqual({});
  });

  it("keeps the configured limits", () => {
    const limits = { externalContextLimit: 128000, localContextLimit: 8192, maxCompletionTokens: 1024 };
    expect(parsePluginConfig({ ...base, limits }).limits).toEqual(limits);
  });

  it.each([0, -1, 1.5, "8192"])("refuses a limit that is not a positive integer (%s)", (value) => {
    expect(errorOf({ ...base, limits: { localContextLimit: value } }).message).toMatch(
      /limits.localContextLimit must be a positive integer/,
    );
  });

  it("refuses an unknown limit", () => {
    expect(errorOf({ ...base, limits: { contextWindow: 1 } }).message).toMatch(/limits.contextWindow/);
  });
});

describe("closed key set (RM12: the token is not plugin configuration)", () => {
  it("refuses an unknown key by name and never echoes its value", () => {
    const message = errorOf({ baseUrl: "http://127.0.0.1:8080/v1", apiKey: TOKEN }).message;
    expect(message).toMatch(/apiKey/);
    expect(message).toMatch(/credential/);
    expect(message).not.toContain(TOKEN);
  });
});
