// Plugin diagnostics: one record per inference attempt, correlated with the
// gateway's run ID (RM12, RM22, RM25, RM33; spec section 7.5, #138).
//
// Section 7.5: diagnostics correlate each attempt with X-RelayMind-Run-ID when
// returned, or the run ID in the chat response, without placing trace events,
// secrets, or progress into answer text. One OpenClaw turn can contain several
// RelayMind run IDs. A run ID unavailable before cancellation is recorded as
// unavailable, never invented. Missing usage is unknown, not zero.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";
import { afterEach, describe, expect, it } from "vitest";

import { attemptRecord, createAttemptLog, type AttemptRecord } from "../src/attempts.ts";
import { MODEL_ID, PROVIDER_ID, relaymindModelEntry } from "../src/compat.ts";
import { createRelayMindProvider } from "../src/provider.ts";
// @ts-expect-error recorder.mjs is plain JavaScript without declarations (#135)
import { startRecorder } from "./host-transport/recorder.mjs";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";
const BASE = "http://127.0.0.1:8080/v1";

const message = (fields: Record<string, unknown>) =>
  ({ role: "assistant", content: [], stopReason: "stop", ...fields }) as never;

const report = (fields: Record<string, unknown>) =>
  ({ attempt: 1, idempotencyKey: "k1", turn: "turn-0123456789abcdef", retried: false, ...fields }) as never;

describe("attemptRecord", () => {
  it("takes the run ID from X-RelayMind-Run-ID and links the native run and the UI", () => {
    const record = attemptRecord(
      report({
        response: { status: 200, headers: { "x-relaymind-run-id": "run_a1" } },
        raw: message({ content: [{ type: "text", text: "Hi." }], responseId: "chatcmpl-run_a1" }),
      }),
      BASE,
    );
    expect(record).toMatchObject({
      event: "relaymind.inference_attempt",
      turn: "turn-0123456789abcdef",
      attempt: 1,
      idempotencyKey: "k1",
      runId: "run_a1",
      runIdSource: "header",
      outcome: "answer",
      status: 200,
      code: null,
      replayed: false,
      retried: false,
      nativeRunUrl: "http://127.0.0.1:8080/relay/v1/runs/run_a1",
      uiUrl: "http://127.0.0.1:8080/ui/runs/run_a1",
      usage: "unknown",
    });
  });

  it("a replayed response is marked replayed (Idempotent-Replayed)", () => {
    const record = attemptRecord(
      report({
        response: { status: 200, headers: { "x-relaymind-run-id": "run_a1", "idempotent-replayed": "true" } },
        raw: message({ content: [{ type: "text", text: "Hi." }] }),
      }),
      BASE,
    );
    expect(record.replayed).toBe(true);
  });

  it("a tool call is outcome tool_call", () => {
    const record = attemptRecord(
      report({
        response: { status: 200, headers: { "x-relaymind-run-id": "run_t" } },
        raw: message({ stopReason: "toolUse", content: [{ type: "toolCall", id: "c", name: "x", arguments: {} }] }),
      }),
      BASE,
    );
    expect(record.outcome).toBe("tool_call");
  });

  it("takes the run ID of an HTTP error from the gateway's error body", () => {
    const record = attemptRecord(
      report({
        raw: message({
          stopReason: "error",
          errorMessage: "422 the task cannot complete",
          errorCode: "needs_external",
          errorBody: JSON.stringify({ code: "needs_external", run_id: "run_e", reason_code: "context_limit" }),
        }),
      }),
      BASE,
    );
    expect(record).toMatchObject({
      runId: "run_e",
      runIdSource: "error_body",
      outcome: "failed",
      status: 422,
      code: "needs_external",
      reasonCode: "context_limit",
    });
  });

  it("falls back to the chat response's ID", () => {
    const record = attemptRecord(
      report({ raw: message({ content: [{ type: "text", text: "Hi." }], responseId: "chatcmpl-run_r" }) }),
      BASE,
    );
    expect(record).toMatchObject({ runId: "run_r", runIdSource: "response_id" });
  });

  it("records an unknown run ID as unavailable, never invented", () => {
    const record = attemptRecord(
      report({ raw: message({ stopReason: "aborted", errorMessage: "Request was aborted" }) }),
      BASE,
    );
    expect(record).toMatchObject({
      runId: "unavailable",
      runIdSource: null,
      outcome: "aborted",
      nativeRunUrl: null,
      uiUrl: null,
    });
    // A 429 creates no run; its error body says run_id null.
    const overloaded = attemptRecord(
      report({
        raw: message({
          stopReason: "error",
          errorMessage: "429 busy",
          errorCode: "overloaded",
          errorBody: JSON.stringify({ code: "overloaded", run_id: null }),
        }),
      }),
      BASE,
    );
    expect(overloaded.runId).toBe("unavailable");
  });

  it("never takes a run ID that is not a gateway run ID", () => {
    const record = attemptRecord(
      report({
        response: { status: 200, headers: { "x-relaymind-run-id": "../../etc" } },
        raw: message({ content: [{ type: "text", text: "Hi." }], responseId: "resp_other" }),
      }),
      BASE,
    );
    expect(record.runId).toBe("unavailable");
  });
});

describe("createAttemptLog", () => {
  const record: AttemptRecord = attemptRecord(
    report({
      response: { status: 200, headers: { "x-relaymind-run-id": "run_a1" } },
      raw: message({ content: [{ type: "text", text: "Hi." }] }),
    }),
    BASE,
  );

  it("writes one log line through the host logger", () => {
    const lines: string[] = [];
    const log = createAttemptLog({ logger: { info: (m) => lines.push(m), warn: (m) => lines.push(m), error: (m) => lines.push(m) } });
    log(record);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/^RelayMind inference attempt /);
    expect(JSON.parse(lines[0]!.replace(/^RelayMind inference attempt /, ""))).toEqual(record);
  });

  it("appends one JSON line per attempt to diagnosticsPath", () => {
    const dir = mkdtempSync(join(tmpdir(), "rm-diag-"));
    try {
      const path = join(dir, "attempts.jsonl");
      const log = createAttemptLog({ logger: { info: () => {}, warn: () => {}, error: () => {} }, path });
      log(record);
      log({ ...record, attempt: 2 });
      const lines = readFileSync(path, "utf8").trim().split("\n").map((l) => JSON.parse(l));
      expect(lines.map((l) => l.attempt)).toEqual([1, 2]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("a file it cannot write is one warning, never a failed request", () => {
    const warnings: string[] = [];
    const log = createAttemptLog({
      logger: { info: () => {}, warn: (m) => warnings.push(m), error: () => {} },
      path: "/nonexistent-dir-rm/attempts.jsonl",
    });
    log(record);
    log(record);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toMatch(/diagnosticsPath/);
  });
});

describe("through the provider and the pinned transport", () => {
  type Recorder = Awaited<ReturnType<typeof startRecorder>>;
  let recorder: Recorder | undefined;
  afterEach(async () => {
    await recorder?.close();
    recorder = undefined;
  });

  const sse = (runId: string, text: string) => ({
    status: 200,
    headers: { "content-type": "text/event-stream", "x-relaymind-run-id": runId },
    body: [
      { id: `chatcmpl-${runId}`, object: "chat.completion.chunk", created: 0, model: MODEL_ID, choices: [{ index: 0, delta: { role: "assistant", content: text }, finish_reason: null }] },
      { id: `chatcmpl-${runId}`, object: "chat.completion.chunk", created: 0, model: MODEL_ID, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    ]
      .map((e) => `data: ${JSON.stringify(e)}\n\n`)
      .join("")
      .concat("data: [DONE]\n\n"),
  });

  async function converse(replies: unknown[], pluginConfig: Record<string, unknown> = {}) {
    recorder = await startRecorder({ replies });
    const lines: string[] = [];
    const logger = { info: (m: string) => lines.push(m), warn: (m: string) => lines.push(m), error: (m: string) => lines.push(m) };
    const baseUrl = `${recorder.origin}/v1`;
    const provider = createRelayMindProvider({ pluginConfig: { baseUrl, ...pluginConfig }, logger });
    const stream = provider.wrapStreamFn!({
      provider: PROVIDER_ID,
      modelId: MODEL_ID,
      streamFn: createOpenAICompletionsTransportStreamFn(),
    } as never)!;
    const model = { ...relaymindModelEntry({ baseUrl }), api: "openai-completions", provider: PROVIDER_ID };
    const events = await stream(
      model as never,
      { messages: [{ role: "user", content: "Say hi.", timestamp: 1 }] } as never,
      { apiKey: TOKEN, sessionId: "s1" } as never,
    );
    const deltas: string[] = [];
    for await (const event of events) if (event.type === "text_delta") deltas.push(event.delta);
    const result = await events.result();
    const records = lines
      .filter((l) => l.startsWith("RelayMind inference attempt "))
      .map((l) => JSON.parse(l.replace("RelayMind inference attempt ", "")) as AttemptRecord);
    return { result, deltas, records, lines };
  }

  it("records the run ID and leaves the answer text untouched", async () => {
    const { result, deltas, records, lines } = await converse([sse("run_p1", "Hi.")]);
    expect(deltas.join("")).toBe("Hi.");
    expect(result.content).toEqual([{ type: "text", text: "Hi." }]);
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({ runId: "run_p1", outcome: "answer", usage: "unknown" });
    // Never the token or the Authorization value (RM12).
    expect(lines.join("\n")).not.toContain(TOKEN);
  });

  it("an empty answer the plugin fails is recorded as failed, not as an answer", async () => {
    const { result, records } = await converse([sse("run_empty", "")]);
    expect(result.stopReason).toBe("error");
    expect(records[0]).toMatchObject({ runId: "run_empty", outcome: "failed" });
  });

  it("two attempts of one turn record two run IDs under one turn", async () => {
    const { records } = await converse(
      [
        {
          status: 504,
          headers: { "content-type": "application/json", "x-relaymind-run-id": "run_slow" },
          body: { error: { code: "deadline_exceeded", message: "slow", run_id: "run_slow" } },
        },
        sse("run_p2", "Hi."),
      ],
      { maxRetries: 1 },
    );
    expect(records.map((r) => [r.attempt, r.runId, r.retried])).toEqual([
      [1, "run_slow", true],
      [2, "run_p2", false],
    ]);
    expect(records[0]!.turn).toBe(records[1]!.turn);
    expect(records[1]!.idempotencyKey).not.toBe(records[0]!.idempotencyKey);
  });
});
