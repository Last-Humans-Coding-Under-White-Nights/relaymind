// The host compatibility switches the plugin declares (RM33, spec section 7.5).
//
// The pinned host derives request defaults from the base URL; for a loopback URL
// they send stream_options on every streamed request, which section 7.1 rejects.
// The plugin declares the switches below so the host stays inside the 7.1 subset.
// #135 confirms every switch on the wire (test/fixtures/v2026.10.7/transport/).
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { resolveOpenAICompletionsCompat } from "@openclaw/ai/transports";

import {
  MODEL_ID,
  PINNED_OPENCLAW,
  PROVIDER_ID,
  relaymindModelCompat,
  relaymindModelEntry,
} from "../src/compat.ts";

describe("relaymindModelCompat (RM33, section 7.5)", () => {
  it("turns off every host default that section 7.1 rejects", () => {
    const compat = relaymindModelCompat();
    expect(compat.supportsUsageInStreaming).toBe(false); // no stream_options
    expect(compat.supportsStrictMode).toBe(false); // no strict flag on tools
    expect(compat.supportsReasoningEffort).toBe(false); // no reasoning_effort
    expect(compat.supportsDeveloperRole).toBe(false); // no mid-conversation developer
    expect(compat.maxTokensField).toBe("max_completion_tokens");
    expect(compat.supportsJsonSchemaResponseFormat).toBe(false);
    expect(compat.supportsPromptCacheKey).toBe(false); // no prompt_cache_key
    expect(compat.supportsStore).toBe(false); // store false is accepted, but not needed
  });

  it("keeps the message keys a tool round trip needs", () => {
    // strictMessageKeys true would strip tool_calls and tool_call_id from the
    // messages, which breaks every host tool round trip (#135 finding).
    const compat = relaymindModelCompat();
    expect(compat.strictMessageKeys).toBe(false);
    // Section 7.1 rejects a name field on a tool message and an assistant
    // message the host invents after tool results.
    expect(compat.requiresToolResultName).toBe(false);
    expect(compat.requiresAssistantAfterToolResult).toBe(false);
    expect(compat.requiresStringContent).toBe(false);
    expect(compat.supportsTools).toBe(true);
  });

  it("resolves to the declared switches under the pinned transport for a loopback URL", () => {
    const model = {
      ...relaymindModelEntry({ baseUrl: "http://127.0.0.1:8080/v1" }),
      provider: PROVIDER_ID,
      api: "openai-completions",
      maxTokens: 1024,
    };
    const resolved = resolveOpenAICompletionsCompat(model as never);
    expect(resolved.supportsUsageInStreaming).toBe(false);
    expect(resolved.supportsStrictMode).toBe(false);
    expect(resolved.supportsReasoningEffort).toBe(false);
    expect(resolved.supportsDeveloperRole).toBe(false);
    expect(resolved.maxTokensField).toBe("max_completion_tokens");
    expect(resolved.supportsPromptCacheKey).toBe(false);
    expect(resolved.requiresToolResultName).toBe(false);
    expect(resolved.requiresAssistantAfterToolResult).toBe(false);
  });
});

describe("relaymindModelEntry (RM33, section 7.5)", () => {
  it("is the client-owned alias, text only, reasoning off", () => {
    const entry = relaymindModelEntry({ baseUrl: "http://127.0.0.1:8080/v1" });
    expect(PROVIDER_ID).toBe("relaymind");
    expect(MODEL_ID).toBe("relaymind-client-tools");
    expect(entry.id).toBe(MODEL_ID);
    expect(entry.reasoning).toBe(false);
    expect(entry.input).toEqual(["text"]);
    expect(entry.baseUrl).toBe("http://127.0.0.1:8080/v1");
    expect(entry.compat).toEqual(relaymindModelCompat());
  });

  it("omits a limit the operator did not set and never estimates one", () => {
    const entry = relaymindModelEntry({ baseUrl: "http://127.0.0.1:8080/v1" });
    expect("contextWindow" in entry).toBe(false);
    expect("maxTokens" in entry).toBe(false);
  });

  it("copies the configured limits unchanged", () => {
    const entry = relaymindModelEntry({
      baseUrl: "http://127.0.0.1:8080/v1",
      contextWindow: 8192,
      maxTokens: 1024,
    });
    expect(entry.contextWindow).toBe(8192);
    expect(entry.maxTokens).toBe(1024);
  });

  it("pins the qualified OpenClaw release", () => {
    expect(PINNED_OPENCLAW).toBe("2026.10.7");
  });
});

// The probe declared these switches on a config-file provider
// (models.providers); no capture shows them on a plugin-supplied model, so
// this checks the values only, not that the host honors them there (#136).
describe("the switches on the real host (#135, docs/evidence/2026-10-27-openclaw-mac-probe)", () => {
  const configs = new URL(
    "../../../docs/evidence/2026-10-27-openclaw-mac-probe/configs/",
    import.meta.url,
  );
  const compatOf = (name: string): Record<string, unknown> =>
    JSON.parse(readFileSync(new URL(name, configs), "utf8")).models.providers["relaymind-capture"]
      .models[0].compat;

  it("agree with every switch of the base config the host ran inside section 7.1", () => {
    const declared = relaymindModelCompat() as unknown as Record<string, unknown>;
    for (const [key, value] of Object.entries(compatOf("config-b-base.json"))) {
      expect(declared[key], key).toEqual(value);
    }
  });

  it("differ from the config whose strictMessageKeys stripped the tool linkage", () => {
    expect(compatOf("config-b.json").strictMessageKeys).toBe(true);
    expect(relaymindModelCompat().strictMessageKeys).toBe(false);
  });
});
