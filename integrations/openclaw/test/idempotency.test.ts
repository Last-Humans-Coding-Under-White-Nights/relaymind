// The Idempotency-Key of an inference attempt (RM17, RM21, RM33; spec sections
// 7.4 and 7.5, #138).
//
// Section 7.5: a stable key for the same inference payload, a new key for the
// next conversation turn, and the section 7.4 replay rules. The pinned
// transport builds the request headers before it builds the body, and its
// resolveTransportTurnState gets a new random turnId and attempt 1 on every
// call, so neither the final body nor the host's turn ID can name a retry. The
// key is the digest of what the body is built from: the model entry (with the
// execution-mode header), the host session, the conversation, the tools, and
// the request options that change the body.
import { describe, expect, it } from "vitest";

import { canonicalJson, idempotencyKey, retryKey, turnId } from "../src/idempotency.ts";

const model = {
  id: "relaymind-client-tools",
  provider: "relaymind",
  api: "openai-completions",
  baseUrl: "http://127.0.0.1:8080/v1",
  headers: { "X-RelayMind-Execution-Mode": "auto" },
  maxTokens: 512,
};
const ECHO = { name: "relaymind_test_echo", description: "Echo.", parameters: { type: "object" } };
const context = {
  systemPrompt: "You are a test host.",
  messages: [{ role: "user", content: "Echo lantern.", timestamp: 1 }],
  tools: [ECHO],
};
const options = { apiKey: "token-a", sessionId: "session-1", streaming: true };

const key = (overrides: { model?: object; context?: object; options?: object } = {}) =>
  idempotencyKey({
    model: { ...model, ...overrides.model } as never,
    context: { ...context, ...overrides.context } as never,
    options: { ...options, ...overrides.options } as never,
  });

describe("idempotencyKey", () => {
  it("is the same for the same inference payload and mode", () => {
    expect(key()).toBe(key());
    // A copy, not the same objects: the key reads values.
    expect(key({ context: structuredClone(context) })).toBe(key());
  });

  it("is 1 to 255 printable ASCII characters (section 7.4)", () => {
    const value = key();
    expect(value.length).toBeGreaterThanOrEqual(1);
    expect(value.length).toBeLessThanOrEqual(255);
    expect(value).toMatch(/^[\x20-\x7e]+$/);
    expect(value).toMatch(/^relaymind-openclaw-v1-[0-9a-f]{64}$/);
  });

  it.each([
    ["a changed user message", { context: { messages: [{ role: "user", content: "Echo moth.", timestamp: 1 }] } }],
    ["a changed tool", { context: { tools: [{ ...ECHO, description: "Other." }] } }],
    ["no tools", { context: { tools: [] } }],
    ["a changed system prompt", { context: { systemPrompt: "Other." } }],
    ["another execution mode", { model: { headers: { "X-RelayMind-Execution-Mode": "local_only" } } }],
    ["another model", { model: { id: "other" } }],
    ["another output limit", { model: { maxTokens: 256 } }],
    ["the stream flag", { options: { streaming: false } }],
    ["a sampling option", { options: { temperature: 0.2 } }],
    ["a token option", { options: { maxTokens: 64 } }],
    ["a tool choice", { options: { toolChoice: "required" } }],
    ["another host session", { options: { sessionId: "session-2" } }],
  ])("changes for %s", (_name, overrides) => {
    expect(key(overrides)).not.toBe(key());
  });

  it("changes for the next turn: the tool result the host sends after a call", () => {
    const next = {
      messages: [
        ...context.messages,
        {
          role: "assistant",
          content: [{ type: "toolCall", id: "callrun11", name: ECHO.name, arguments: { text: "lantern" } }],
          stopReason: "toolUse",
          timestamp: 2,
        },
        { role: "toolResult", toolCallId: "callrun11", toolName: ECHO.name, content: [{ type: "text", text: "echo: lantern" }], isError: false, timestamp: 3 },
      ],
    };
    expect(key({ context: next })).not.toBe(key());
  });

  it("does not depend on the gateway's address: a key is scoped to the gateway that stores it", () => {
    expect(key({ model: { baseUrl: "http://127.0.0.1:9090/v1" } })).toBe(key());
  });

  it("does not depend on the credential, the signal, the host's hooks, or its headers", () => {
    // A gateway restart issues a new token; the key belongs to the caller, not the token (7.4).
    expect(
      key({
        options: {
          apiKey: "token-b",
          signal: new AbortController().signal,
          onPayload: () => undefined,
          onResponse: () => undefined,
          headers: { "x-trace": "1" },
        },
      }),
    ).toBe(key());
  });
});

describe("retryKey (section 7.4: a retry after a failed run is a new key)", () => {
  it("is a different, valid key per retry", () => {
    const base = key();
    expect(retryKey(base, 1)).not.toBe(base);
    expect(retryKey(base, 2)).not.toBe(retryKey(base, 1));
    expect(retryKey(base, 2).length).toBeLessThanOrEqual(255);
    expect(retryKey(base, 2)).toMatch(/^[\x20-\x7e]+$/);
  });
});

describe("turnId (one OpenClaw turn, several RelayMind runs)", () => {
  it("is the same for the requests after the same user message", () => {
    const first = turnId(context as never, "session-1");
    const followUp = turnId(
      {
        ...context,
        messages: [
          ...context.messages,
          { role: "toolResult", toolCallId: "x", toolName: ECHO.name, content: [], isError: false, timestamp: 3 },
        ],
      } as never,
      "session-1",
    );
    expect(followUp).toBe(first);
    expect(first).toMatch(/^turn-[0-9a-f]{16}$/);
  });

  it("is new for the next user message or another session", () => {
    const first = turnId(context as never, "session-1");
    const next = turnId(
      { ...context, messages: [...context.messages, { role: "user", content: "Again.", timestamp: 9 }] } as never,
      "session-1",
    );
    expect(next).not.toBe(first);
    expect(turnId(context as never, "session-2")).not.toBe(first);
  });
});

describe("canonicalJson", () => {
  it("sorts keys and drops undefined and functions", () => {
    expect(canonicalJson({ b: 1, a: { d: undefined, c: [1, "x"] }, f: () => 1 })).toBe(
      '{"a":{"c":[1,"x"]},"b":1}',
    );
  });
});
