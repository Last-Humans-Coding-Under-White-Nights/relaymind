// Unsupported input is refused by name, never removed (RM19, RM33; spec sections
// 7.1 and 7.5, #137).
//
// The pinned host converts what the client-owned backend cannot take instead of
// failing: an image part becomes the text "(image omitted: ...)", and a response
// format or a strict flag the switches turn off is dropped. The guard refuses
// such a request before anything is sent, naming the field, so the host never
// gets an answer to a question it did not ask. A plain text and tools request
// passes unchanged: nothing is trimmed, reordered, or dropped.
import { describe, expect, it } from "vitest";

import { checkContext, checkOptions, checkPayload, RelayMindInputError } from "../src/input-guard.ts";

const ECHO = {
  name: "relaymind_test_echo",
  description: "Sandbox host tool.",
  parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
};

function textContext() {
  return {
    systemPrompt: "You are a test host.",
    messages: [
      { role: "user", content: "Echo lantern.", timestamp: 1 },
      {
        role: "assistant",
        content: [{ type: "toolCall", id: "call_1", name: ECHO.name, arguments: { text: "lantern" } }],
        api: "openai-completions",
        provider: "relaymind",
        model: "relaymind-client-tools",
        stopReason: "toolUse",
        timestamp: 2,
      },
      {
        role: "toolResult",
        toolCallId: "call_1",
        toolName: ECHO.name,
        content: [{ type: "text", text: "echo: lantern" }],
        isError: false,
        timestamp: 3,
      },
    ],
    tools: [ECHO],
  };
}

function textPayload() {
  return {
    model: "relaymind-client-tools",
    stream: true,
    messages: [
      { role: "system", content: "You are a test host." },
      { role: "user", content: "Echo lantern." },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "call_1",
            type: "function",
            function: { name: ECHO.name, arguments: '{"text":"lantern"}' },
          },
        ],
      },
      { role: "tool", tool_call_id: "call_1", content: "echo: lantern" },
    ],
    tools: [{ type: "function", function: { ...ECHO } }],
  };
}

function refusal(run: () => void): RelayMindInputError {
  try {
    run();
  } catch (error) {
    expect(error).toBeInstanceOf(RelayMindInputError);
    return error as RelayMindInputError;
  }
  throw new Error("the guard accepted the input");
}

describe("the host context (before the host converts it)", () => {
  it("passes a text and tools context unchanged", () => {
    const context = textContext();
    const before = structuredClone(context);
    checkContext(context as never);
    expect(context).toEqual(before);
    expect(context.messages).toHaveLength(3);
    expect(context.tools).toHaveLength(1);
  });

  it("refuses an image part in a user message by its field", () => {
    const context = textContext();
    context.messages[0] = {
      role: "user",
      content: [
        { type: "text", text: "Look." },
        { type: "image", data: "aGVsbG8=", mimeType: "image/png" },
      ],
      timestamp: 1,
    } as never;
    const error = refusal(() => checkContext(context as never));
    expect(error.field).toBe("messages[0].content[1].type");
    expect(error.message).toContain("image");
    expect(error.message).toContain("text only");
    expect(error.message).toContain("never removes");
  });

  it("refuses an image part in a tool result by its field", () => {
    const context = textContext();
    (context.messages[2] as { content: unknown[] }).content.push({
      type: "image",
      data: "aGVsbG8=",
      mimeType: "image/png",
    });
    const error = refusal(() => checkContext(context as never));
    expect(error.field).toBe("messages[2].content[1].type");
  });
});

describe("the request options", () => {
  it("passes no response format and a text response format", () => {
    checkOptions(undefined);
    checkOptions({});
    checkOptions({ responseFormat: { type: "text" } });
    // A field set to null counts as omitted (spec 7.1), as in checkPayload.
    checkOptions({ responseFormat: null });
  });

  it("refuses a structured response format the switches would drop", () => {
    const error = refusal(() =>
      checkOptions({ responseFormat: { type: "json_schema", json_schema: { name: "x" } } }),
    );
    expect(error.field).toBe("response_format");
    expect(error.message).toContain("json_schema");
  });
});

describe("the outgoing payload (what the transport would send)", () => {
  it("passes a section 7.1 text and tools payload unchanged", () => {
    const context = textContext();
    const payload = textPayload();
    const before = structuredClone(payload);
    checkPayload(payload, context as never);
    expect(payload).toEqual(before);
  });

  it("refuses parallel_tool_calls true and quotes the gateway's code", () => {
    const error = refusal(() =>
      checkPayload({ ...textPayload(), parallel_tool_calls: true }, textContext() as never),
    );
    expect(error.field).toBe("parallel_tool_calls");
    expect(error.message).toContain("unsupported_parameter");
    expect(error.message).toContain("one function call per response");
  });

  it("accepts parallel_tool_calls false", () => {
    checkPayload({ ...textPayload(), parallel_tool_calls: false }, textContext() as never);
  });

  it("refuses a strict tool", () => {
    const payload = textPayload();
    (payload.tools[0]!.function as Record<string, unknown>).strict = true;
    const error = refusal(() => checkPayload(payload, textContext() as never));
    expect(error.field).toBe("tools[0].function.strict");
  });

  it("refuses n other than 1", () => {
    const error = refusal(() => checkPayload({ ...textPayload(), n: 2 }, textContext() as never));
    expect(error.field).toBe("n");
  });

  it("refuses a response format other than text", () => {
    const error = refusal(() =>
      checkPayload(
        { ...textPayload(), response_format: { type: "json_object" } },
        textContext() as never,
      ),
    );
    expect(error.field).toBe("response_format");
  });

  it("refuses a non-text content part", () => {
    const payload = textPayload();
    payload.messages[1] = {
      role: "user",
      content: [{ type: "image_url", image_url: { url: "data:," } }],
    } as never;
    const error = refusal(() => checkPayload(payload, textContext() as never));
    expect(error.field).toBe("messages[1].content[0].type");
  });

  it("refuses a payload that lost a host tool", () => {
    const payload = { ...textPayload(), tools: [] };
    const error = refusal(() => checkPayload(payload, textContext() as never));
    expect(error.field).toBe("tools");
    expect(error.message).toContain(ECHO.name);
    expect(error.message).toContain("never drops");
  });
});
