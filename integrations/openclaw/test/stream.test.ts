// The plugin's stream wrapper over the pinned transport (RM20, RM33; spec sections
// 7.2, 7.3, and 7.5, #137).
//
// The gateway's client-owned backend buffers: a streamed response is the replay
// of a finished run (role chunk, content or one tool-call chunk, the finish
// chunk, [DONE]); a failure after the status line is one {"error": ...} data
// event and no finish chunk. The pinned transport already turns a missing
// finish chunk, an error event, malformed JSON, and a malformed call into a
// failure. What the plugin adds: an answer with no text and no call is a
// failure, never an empty success; a response with more than one call is a
// failure (the gateway exposes one); and the guard refuses unsupported input
// before anything is sent. Keep-alive comments are never content.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { readFileSync } from "node:fs";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";
import { afterEach, describe, expect, it } from "vitest";

import { PROVIDER_ID, relaymindModelEntry } from "../src/compat.ts";
import { checkAnswer, relaymindStreamFn } from "../src/stream.ts";
// @ts-expect-error recorder.mjs is plain JavaScript without declarations (#135)
import { startRecorder } from "./host-transport/recorder.mjs";

const FIXTURES = new URL("./fixtures/v2026.10.7/transport/", import.meta.url);

const ECHO = {
  name: "relaymind_test_echo",
  description: "Sandbox host tool.",
  parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
};

function fixtureReply(name: string, index = 0) {
  const fixture = JSON.parse(readFileSync(new URL(`${name}.json`, FIXTURES), "utf8"));
  const { response } = fixture.exchanges[index];
  if (response.body?.sse) return sse(response.body.sse);
  return { status: response.status, body: response.body };
}

function sse(events: unknown[]) {
  const text = events
    .map((event) => {
      if (typeof event === "string") return `data: ${event}\n\n`;
      if (event && typeof event === "object" && "comment" in event) {
        return `: ${(event as { comment: string }).comment}\n\n`;
      }
      return `data: ${JSON.stringify(event)}\n\n`;
    })
    .join("");
  return { status: 200, headers: { "content-type": "text/event-stream" }, body: text };
}

function chunk(delta: Record<string, unknown>, finish: string | null = null) {
  return {
    id: "chatcmpl-run_1",
    object: "chat.completion.chunk",
    created: 0,
    model: "relaymind-client-tools",
    choices: [{ index: 0, delta, finish_reason: finish }],
  };
}

function callDelta(index: number, id: string, args: string) {
  return { index, id, type: "function", function: { name: ECHO.name, arguments: args } };
}

type Recorder = Awaited<ReturnType<typeof startRecorder>>;
let recorder: Recorder | undefined;

afterEach(async () => {
  await recorder?.close();
  recorder = undefined;
});

async function run(
  replies: unknown[],
  {
    context = {},
    options = {},
  }: { context?: Record<string, unknown>; options?: Record<string, unknown> } = {},
) {
  recorder = await startRecorder({ replies });
  const model = {
    ...relaymindModelEntry({ baseUrl: `${recorder.origin}/v1` }),
    api: "openai-completions",
    provider: PROVIDER_ID,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
  const stream = relaymindStreamFn(createOpenAICompletionsTransportStreamFn() as never);
  const events = await stream(
    model as never,
    {
      systemPrompt: "You are a test host.",
      messages: [{ role: "user", content: "Echo lantern.", timestamp: 1 }],
      tools: [ECHO],
      ...context,
    } as never,
    { apiKey: "rm-test-token", streaming: true, ...options } as never,
  );
  const seen: string[] = [];
  const deltas: string[] = [];
  for await (const event of events) {
    seen.push(event.type);
    if (event.type === "text_delta") deltas.push(event.delta);
  }
  const message = await events.result();
  return { message, seen, deltas, exchanges: recorder.exchanges };
}

const text = (message: { content: { type: string; text?: string }[] }) =>
  message.content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");

describe("the gateway's buffered replay through the pinned transport", () => {
  it("a text replay (fixture text-streamed) is one text answer", async () => {
    const { message, seen } = await run([fixtureReply("text-streamed")]);
    expect(message.stopReason).toBe("stop");
    expect(text(message)).toBe("Hello there.");
    expect(seen.at(-1)).toBe("done");
  });

  it("a non-streamed response (fixture text-buffered) is one text answer", async () => {
    const { message } = await run([fixtureReply("text-buffered")], {
      options: { streaming: false },
    });
    expect(message.stopReason).toBe("stop");
    expect(text(message)).toBe("Hello.");
  });

  it("a tool-call replay (fixture tool-round-trip) is one complete call", async () => {
    const { message } = await run([fixtureReply("tool-round-trip")]);
    expect(message.stopReason).toBe("toolUse");
    expect(message.content).toEqual([
      {
        type: "toolCall",
        id: "call_echo_1",
        name: "relaymind_test_echo",
        arguments: { text: "lantern" },
      },
    ]);
  });

  it("keep-alive comments before the answer are not content", async () => {
    const { message, deltas } = await run([
      sse([
        { comment: "keep-alive" },
        { comment: "keep-alive" },
        chunk({ role: "assistant", content: "" }),
        chunk({ content: "Hi." }),
        chunk({}, "stop"),
        "[DONE]",
      ]),
    ]);
    expect(text(message)).toBe("Hi.");
    expect(deltas.join("")).toBe("Hi.");
  });

  it("an error event after content is a failure, never a success", async () => {
    const { message, seen } = await run([
      sse([
        chunk({ role: "assistant", content: "" }),
        chunk({ content: "Partial." }),
        {
          error: {
            code: "external_failed",
            message: "the external model failed; no answer was completed",
            run_id: "run_1",
          },
        },
      ]),
    ]);
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toContain("no answer was completed");
    expect(seen.at(-1)).toBe("error");
  });

  it("an error event under early headers, before any content, is a failure", async () => {
    const { message } = await run([
      sse([
        { comment: "keep-alive" },
        {
          error: {
            code: "needs_external",
            message: "the local model cannot complete this request",
            run_id: "run_1",
            reason_code: "context_limit",
          },
        },
      ]),
    ]);
    expect(message.stopReason).toBe("error");
    expect(text(message)).toBe("");
  });

  it("a stream closed without a finish chunk or [DONE] is a failure", async () => {
    const { message } = await run([
      sse([chunk({ role: "assistant", content: "" }), chunk({ content: "Partial." })]),
    ]);
    expect(message.stopReason).toBe("error");
  });

  it("malformed JSON in a data event is a failure", async () => {
    const reply = sse([chunk({ role: "assistant", content: "" })]);
    reply.body += "data: {not json\n\n";
    const { message } = await run([reply]);
    expect(message.stopReason).toBe("error");
  });

  it("an empty answer with finish stop is a failure, not an empty success", async () => {
    const { message, seen } = await run([
      sse([chunk({ role: "assistant", content: "" }), chunk({}, "stop"), "[DONE]"]),
    ]);
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toMatch(/empty answer/);
    expect(seen.at(-1)).toBe("error");
    expect(seen).not.toContain("done");
  });

  it("a blank answer is a failure", async () => {
    const { message } = await run([
      sse([chunk({ role: "assistant", content: "" }), chunk({ content: "  \n" }), chunk({}, "stop"), "[DONE]"]),
    ]);
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toMatch(/empty answer/);
  });

  it("an empty non-streamed answer is a failure", async () => {
    const { message } = await run(
      [
        {
          status: 200,
          body: {
            id: "chatcmpl-run_1",
            object: "chat.completion",
            created: 0,
            model: "relaymind-client-tools",
            choices: [{ index: 0, message: { role: "assistant", content: "" }, finish_reason: "stop" }],
          },
        },
      ],
      { options: { streaming: false } },
    );
    expect(message.stopReason).toBe("error");
  });

  it("two calls in one response are a failure (the gateway exposes one)", async () => {
    const { message } = await run([
      sse([
        chunk({ role: "assistant", content: null, tool_calls: [callDelta(0, "call_1", '{"text":"a"}')] }),
        chunk({ tool_calls: [callDelta(1, "call_2", '{"text":"b"}')] }),
        chunk({}, "tool_calls"),
        "[DONE]",
      ]),
    ]);
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toMatch(/one function call per response/);
  });

  it("an HTTP error is a failure with the gateway's message", async () => {
    const { message } = await run([
      {
        status: 422,
        body: {
          error: {
            code: "needs_external",
            message: "the local model cannot complete this request",
            run_id: "run_1",
            reason_code: "invalid_output",
          },
        },
      },
    ]);
    expect(message.stopReason).toBe("error");
    expect(text(message)).toBe("");
  });
});

describe("refused input is never sent", () => {
  it("an image part fails before the request", async () => {
    const { message, exchanges } = await run([fixtureReply("text-streamed")], {
      context: {
        messages: [
          {
            role: "user",
            content: [
              { type: "text", text: "Look." },
              { type: "image", data: "aGVsbG8=", mimeType: "image/png" },
            ],
            timestamp: 1,
          },
        ],
      },
    });
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toContain("messages[0].content[1].type");
    expect(exchanges).toEqual([]);
  });

  it("an operator parallel_tool_calls true fails before the request", async () => {
    const { message, exchanges } = await run([fixtureReply("text-streamed")], {
      options: { onPayload: (payload: object) => ({ ...payload, parallel_tool_calls: true }) },
    });
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toContain("parallel_tool_calls");
    expect(exchanges).toEqual([]);
  });

  it("a structured response format fails before the request", async () => {
    const { message, exchanges } = await run([fixtureReply("text-streamed")], {
      options: { responseFormat: { type: "json_schema", json_schema: { name: "x", schema: {} } } },
    });
    expect(message.stopReason).toBe("error");
    expect(message.errorMessage).toContain("response_format");
    expect(exchanges).toEqual([]);
  });

  it("the host's own payload hook still runs, before the guard reads the payload", async () => {
    const seen: unknown[] = [];
    const { message, exchanges } = await run([fixtureReply("text-streamed")], {
      options: {
        onPayload: (payload: Record<string, unknown>) => {
          seen.push(payload.model);
          return { ...payload, temperature: 0.2 };
        },
      },
    });
    expect(seen).toEqual(["relaymind-client-tools"]);
    expect(message.stopReason).toBe("stop");
    expect((exchanges[0]!.request.body as Record<string, unknown>).temperature).toBe(0.2);
  });
});

describe("checkAnswer (the plugin's own rule on the final message)", () => {
  const base = {
    role: "assistant",
    api: "openai-completions",
    provider: PROVIDER_ID,
    model: "relaymind-client-tools",
    timestamp: 1,
  };

  it("keeps a text answer and a single call unchanged", () => {
    const answer = { ...base, content: [{ type: "text", text: "Hi." }], stopReason: "stop" };
    expect(checkAnswer(answer as never)).toBe(answer);
    const call = {
      ...base,
      content: [{ type: "toolCall", id: "c1", name: ECHO.name, arguments: {} }],
      stopReason: "toolUse",
    };
    expect(checkAnswer(call as never)).toBe(call);
  });

  it("keeps a failure unchanged", () => {
    const failure = { ...base, content: [], stopReason: "error", errorMessage: "x" };
    expect(checkAnswer(failure as never)).toBe(failure);
  });

  it("turns an empty success into a failure", () => {
    const empty = { ...base, content: [], stopReason: "stop" };
    const out = checkAnswer(empty as never);
    expect(out.stopReason).toBe("error");
    expect(out.errorMessage).toMatch(/empty answer/);
  });
});
