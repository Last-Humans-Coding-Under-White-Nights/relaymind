// The key on the wire and the bounded retry rule through the pinned transport
// (RM17, RM20, RM21, RM33; spec sections 7.4 and 7.5, #138).
//
// Every request carries an Idempotency-Key for its inference payload. The
// plugin makes one attempt by default. With maxRetries (at most 2) it retries
// only a 429 overloaded (same key: the gateway stored nothing) and a 504
// deadline_exceeded (a new key: the failed run holds the first one, and section
// 7.4 answers it with run_terminal), and only before any answer content was
// produced. Every other failure is final. A retry never replaces emitted text.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";
import { afterEach, describe, expect, it } from "vitest";

import { PROVIDER_ID, relaymindModelEntry } from "../src/compat.ts";
import { KeyLedger, retryKey } from "../src/idempotency.ts";
import { relaymindStreamFn, type AttemptReport, type RelayMindStreamOptions } from "../src/stream.ts";
// @ts-expect-error recorder.mjs is plain JavaScript without declarations (#135)
import { startRecorder } from "./host-transport/recorder.mjs";

type Recorder = Awaited<ReturnType<typeof startRecorder>>;
let recorder: Recorder | undefined;

afterEach(async () => {
  await recorder?.close();
  recorder = undefined;
});

const ANSWER = sse([
  chunk({ role: "assistant", content: "" }),
  chunk({ content: "Hi." }),
  chunk({}, "stop"),
  "[DONE]",
]);

function sse(events: unknown[]) {
  const text = events
    .map((e) => (typeof e === "string" ? `data: ${e}\n\n` : `data: ${JSON.stringify(e)}\n\n`))
    .join("");
  return {
    status: 200,
    headers: { "content-type": "text/event-stream", "x-relaymind-run-id": "run_ok" },
    body: text,
  };
}

function chunk(delta: Record<string, unknown>, finish: string | null = null) {
  return {
    id: "chatcmpl-run_ok",
    object: "chat.completion.chunk",
    created: 0,
    model: "relaymind-client-tools",
    choices: [{ index: 0, delta, finish_reason: finish }],
  };
}

function error(status: number, code: string, runId: string | null = null) {
  return {
    status,
    headers: { "content-type": "application/json", ...(runId ? { "x-relaymind-run-id": runId } : {}) },
    body: { error: { code, message: `gateway ${code}`, run_id: runId } },
  };
}

const CONTEXT = {
  systemPrompt: "You are a test host.",
  messages: [{ role: "user", content: "Say hi.", timestamp: 1 }],
  tools: [],
};

async function start(replies: unknown[]) {
  recorder = await startRecorder({ replies });
  return {
    ...relaymindModelEntry({ baseUrl: `${recorder.origin}/v1` }),
    api: "openai-completions",
    provider: PROVIDER_ID,
  };
}

async function send(
  model: unknown,
  streamOptions: RelayMindStreamOptions = {},
  { context = CONTEXT, options = {} }: { context?: unknown; options?: Record<string, unknown> } = {},
) {
  const stream = relaymindStreamFn(createOpenAICompletionsTransportStreamFn() as never, {
    retryDelayMs: 0,
    ...streamOptions,
  });
  const events = await stream(model as never, context as never, {
    apiKey: "rm-test-token",
    streaming: true,
    ...options,
  } as never);
  const seen: string[] = [];
  const all: { type: string; reason?: string }[] = [];
  for await (const event of events) {
    seen.push(event.type);
    all.push(event as never);
  }
  return { message: await events.result(), seen, events: all };
}

async function until(condition: () => boolean) {
  for (let i = 0; i < 200 && !condition(); i++) await new Promise((r) => setTimeout(r, 10));
  expect(condition()).toBe(true);
}

const keys = () => recorder!.exchanges.map((x: any) => x.request.headers["idempotency-key"]);

describe("the Idempotency-Key on the wire", () => {
  it("every request carries one, the same for the same conversation", async () => {
    const model = await start([ANSWER]);
    await send(model);
    await send(model);
    const [first, second] = keys();
    expect(first).toMatch(/^relaymind-openclaw-v1-[0-9a-f]{64}$/);
    expect(second).toBe(first);
  });

  it("the next turn gets a new key", async () => {
    const model = await start([ANSWER]);
    await send(model);
    await send(model, {}, {
      context: {
        ...CONTEXT,
        messages: [
          ...CONTEXT.messages,
          { role: "assistant", content: [{ type: "text", text: "Hi." }], stopReason: "stop", timestamp: 2 },
          { role: "user", content: "Again.", timestamp: 3 },
        ],
      },
    });
    const [first, second] = keys();
    expect(second).not.toBe(first);
  });

  it("keeps the host's own request headers", async () => {
    const model = await start([ANSWER]);
    await send(model, {}, { options: { headers: { "x-host-trace": "t1" } } });
    expect(recorder!.exchanges[0].request.headers["x-host-trace"]).toBe("t1");
    expect(keys()[0]).toBeTruthy();
  });

  it("a key the host already set is kept, never replaced", async () => {
    const model = await start([ANSWER]);
    await send(model, {}, { options: { headers: { "Idempotency-Key": "host-key-1" } } });
    expect(keys()).toEqual(["host-key-1"]);
  });
});

describe("one attempt by default (section 7.5)", () => {
  it.each([
    [429, "overloaded"],
    [504, "deadline_exceeded"],
  ])("HTTP %s %s is not retried without maxRetries", async (status, code) => {
    const model = await start([error(status, code), ANSWER]);
    const { message } = await send(model);
    expect(message.stopReason).toBe("error");
    expect(recorder!.exchanges).toHaveLength(1);
  });
});

describe("the bounded retry rule (maxRetries)", () => {
  it("retries a 429 with the same key: nothing was stored", async () => {
    const model = await start([error(429, "overloaded"), ANSWER]);
    const { message, seen } = await send(model, { maxRetries: 1 });
    expect(message.stopReason).toBe("stop");
    const [first, second] = keys();
    expect(second).toBe(first);
    // The failed attempt is not shown to the host: one start, one answer.
    expect(seen.filter((t) => t === "start")).toHaveLength(1);
    expect(seen).not.toContain("error");
  });

  it("retries a 504 under a new key: the failed run holds the first one", async () => {
    const model = await start([error(504, "deadline_exceeded", "run_slow"), ANSWER]);
    const { message } = await send(model, { maxRetries: 1 });
    expect(message.stopReason).toBe("stop");
    const [first, second] = keys();
    expect(second).toBe(retryKey(first, 1));
  });

  it("stops after maxRetries and reports the last failure", async () => {
    const model = await start([error(429, "overloaded"), error(429, "overloaded"), error(429, "overloaded"), ANSWER]);
    const { message, seen } = await send(model, { maxRetries: 2 });
    expect(recorder!.exchanges).toHaveLength(3);
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toMatch(/^RelayMind overloaded \(HTTP 429\)/);
    expect(seen.filter((t) => t === "error")).toHaveLength(1);
  });

  it.each([
    [503, "external_failed"],
    [422, "needs_external"],
    [409, "run_terminal"],
    [409, "idempotency_conflict"],
    [401, "unauthenticated"],
    [400, "context_limit_exceeded"],
  ])("never retries HTTP %s %s", async (status, code) => {
    const model = await start([error(status, code), ANSWER]);
    const { message } = await send(model, { maxRetries: 2 });
    expect(message.stopReason).toBe("error");
    expect(recorder!.exchanges).toHaveLength(1);
  });

  it("never retries after answer content was produced", async () => {
    const late = sse([
      chunk({ role: "assistant", content: "" }),
      chunk({ content: "Part" }),
      { error: { code: "deadline_exceeded", message: "late", run_id: "run_late" } },
    ]);
    const model = await start([late, ANSWER]);
    const { message } = await send(model, { maxRetries: 2 });
    expect(message.stopReason).toBe("error");
    expect(recorder!.exchanges).toHaveLength(1);
  });

  it("never retries an aborted request", async () => {
    const model = await start([error(429, "overloaded"), ANSWER]);
    const controller = new AbortController();
    controller.abort();
    const { message } = await send(model, { maxRetries: 2 }, { options: { signal: controller.signal } });
    expect(message.stopReason).toBe("aborted");
    expect(recorder!.exchanges.length).toBeLessThanOrEqual(1);
  });

  it("an abort during the retry wait ends the request aborted, never as the last failure", async () => {
    // The host's cancel is the outcome: a 429 shown here would read as an
    // overload of this provider for a turn the user cancelled.
    const model = await start([error(429, "overloaded"), ANSWER]);
    const controller = new AbortController();
    const reports: AttemptReport[] = [];
    const pending = send(
      model,
      { maxRetries: 1, retryDelayMs: 10_000, onAttempt: (r) => reports.push(r) },
      { options: { signal: controller.signal } },
    );
    await until(() => recorder!.exchanges.length === 1);
    await new Promise((r) => setTimeout(r, 100));
    controller.abort();
    const { message, events } = await pending;
    expect(message.stopReason).toBe("aborted");
    expect(message.errorMessage ?? "").not.toMatch(/overloaded/);
    expect(events.at(-1)).toMatchObject({ type: "error", reason: "aborted" });
    expect(recorder!.exchanges).toHaveLength(1);
    expect(reports).toHaveLength(1);
    expect(reports[0]!.raw.stopReason).toBe("aborted");
    expect(reports[0]!.retried).toBe(false);
  });

  it("refuses a maxRetries above 2 by name", () => {
    expect(() => relaymindStreamFn((() => undefined) as never, { maxRetries: 3 })).toThrow(/maxRetries/);
  });
});

describe("a host retry of the same conversation (section 7.4: a retry after a failure uses a new key)", () => {
  it("after a 504 the host's retry gets a new key, not the failed run's", async () => {
    const model = await start([error(504, "deadline_exceeded", "run_slow"), ANSWER]);
    const ledger = new KeyLedger();
    expect((await send(model, { keys: ledger })).message.stopReason).toBe("error");
    expect((await send(model, { keys: ledger })).message.stopReason).toBe("stop");
    const [first, second] = keys();
    expect(second).toBe(retryKey(first, 1));
  });

  it("after a plugin retry that succeeded, the host's retry replays the key that succeeded", async () => {
    const model = await start([error(504, "deadline_exceeded", "run_slow"), ANSWER, ANSWER]);
    const ledger = new KeyLedger();
    expect((await send(model, { maxRetries: 1, keys: ledger })).message.stopReason).toBe("stop");
    expect((await send(model, { keys: ledger })).message.stopReason).toBe("stop");
    const [first, second, third] = keys();
    expect(second).toBe(retryKey(first, 1));
    expect(third).toBe(second);
  });

  it("after a completed answer the host's retry keeps the key, so the gateway replays it", async () => {
    const model = await start([ANSWER]);
    const ledger = new KeyLedger();
    await send(model, { keys: ledger });
    await send(model, { keys: ledger });
    const [first, second] = keys();
    expect(second).toBe(first);
  });

  it("after a 429 the host's retry keeps the key: nothing was stored", async () => {
    const model = await start([error(429, "overloaded"), ANSWER]);
    const ledger = new KeyLedger();
    await send(model, { keys: ledger });
    await send(model, { keys: ledger });
    const [first, second] = keys();
    expect(second).toBe(first);
  });

  it("after a 409 run_terminal (a key spent before this process) the next retry gets a new key", async () => {
    const model = await start([error(409, "run_terminal", "run_old"), ANSWER]);
    const ledger = new KeyLedger();
    expect((await send(model, { keys: ledger })).message.stopReason).toBe("error");
    expect((await send(model, { keys: ledger })).message.stopReason).toBe("stop");
    const [first, second] = keys();
    expect(second).toBe(retryKey(first, 1));
  });

  it("a key the host set itself is never changed", async () => {
    const model = await start([error(504, "deadline_exceeded", "run_slow"), ANSWER]);
    const ledger = new KeyLedger();
    const options = { headers: { "Idempotency-Key": "host-key-1" } };
    await send(model, { keys: ledger }, { options });
    await send(model, { keys: ledger }, { options });
    expect(keys()).toEqual(["host-key-1", "host-key-1"]);
  });

  it("an empty key the host set is still the host's: sent as is, once, never retried", async () => {
    // The gateway refuses it by name (400 invalid_request, section 7.4); the plugin neither adds its own beside it nor retries.
    const model = await start([error(504, "deadline_exceeded", "run_slow"), ANSWER]);
    const { message } = await send(model, { maxRetries: 1 }, { options: { headers: { "idempotency-key": "" } } });
    expect(message.stopReason).toBe("error");
    expect(keys()).toEqual([""]);
  });
});
