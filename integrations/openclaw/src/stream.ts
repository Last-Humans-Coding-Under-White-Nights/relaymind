// The plugin's wrapper around the host's stream function (RM19, RM20, RM33;
// spec sections 7.2, 7.3, and 7.5).
//
// The gateway's client-owned backend buffers and validates its generation, so
// a streamed response is the replay of a finished run: the role chunk, the
// content chunks or one tool-call chunk, the finish chunk, and [DONE]. A failure
// after the status line is one {"error": ...} data event without a finish chunk;
// under early headers, keep-alive comments come first and are never content.
// The pinned transport already makes a stream without a finish chunk, an error
// event, malformed JSON, and a malformed call a failure (recorded in
// test/stream.test.ts). The wrapper adds what the plugin owns:
//
// - before sending: the input guard (src/input-guard.ts) on the host context,
//   the request options, and the final payload, after every host payload hook
//   (the plugin is the innermost wrapper; the host's generic wrappers sit
//   outside it and patch the payload through options.onPayload, which runs
//   before checkPayload; see src/provider.ts);
// - after the response: an answer with no text and no call is a failure, never
//   an empty success, and more than one call in one response is a failure,
//   because the gateway exposes one call per response.
//
// The wrapper forwards every event in order and never changes emitted text: it
// only replaces the terminal "done" event of a response that breaks a rule with
// an "error" event. The host keeps its tools, its approvals, and its retries.
import type { ProviderWrapStreamFnContext } from "openclaw/plugin-sdk/plugin-entry";

import { checkContext, checkOptions, checkPayload } from "./input-guard.ts";

/** The host's stream function and the types it carries, from the public SDK. */
export type StreamFn = NonNullable<ProviderWrapStreamFnContext["streamFn"]>;
type Model = Parameters<StreamFn>[0];
export type Context = Parameters<StreamFn>[1];
type Options = Parameters<StreamFn>[2];
type AssistantMessageEventStreamLike = Awaited<ReturnType<StreamFn>>;
type AssistantMessage = Awaited<ReturnType<AssistantMessageEventStreamLike["result"]>>;
type AssistantMessageEvent =
  AssistantMessageEventStreamLike extends AsyncIterable<infer E> ? E : never;

const EMPTY_ANSWER =
  "RelayMind returned an empty answer: no text and no function call. This is a failure " +
  "of the response, not an answer.";
const MORE_THAN_ONE_CALL =
  "RelayMind returned more than one function call; the client-owned alias returns one " +
  "function call per response (spec 7.2), so the response is not used.";

function failed(message: AssistantMessage, reason: string): AssistantMessage {
  return { ...message, stopReason: "error", errorMessage: reason };
}

/** The final message, or a failure when it breaks the plugin's answer rule. */
export function checkAnswer(message: AssistantMessage): AssistantMessage {
  if (message.stopReason === "error" || message.stopReason === "aborted") return message;
  const calls = message.content.filter((block) => block.type === "toolCall").length;
  if (calls > 1) return failed(message, MORE_THAN_ONE_CALL);
  const text = message.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
  if (calls === 0 && text.trim() === "") return failed(message, EMPTY_ANSWER);
  return message;
}

function policed(inner: AssistantMessageEventStreamLike): AssistantMessageEventStreamLike {
  return {
    async *[Symbol.asyncIterator](): AsyncIterator<AssistantMessageEvent> {
      for await (const event of inner) {
        if (event.type === "done") {
          const checked = checkAnswer(event.message);
          if (checked !== event.message) {
            yield { type: "error", reason: "error", error: checked };
            continue;
          }
        }
        yield event;
      }
    },
    result: async () => checkAnswer(await inner.result()),
  };
}

/** A failed stream that sends nothing: the request was refused before it was built. */
function refused(model: Model, error: Error): AssistantMessageEventStreamLike {
  const message: AssistantMessage = {
    role: "assistant",
    content: [],
    api: model.api,
    provider: model.provider,
    model: model.id,
    usage: {
      input: 0,
      output: 0,
      cacheRead: 0,
      cacheWrite: 0,
      totalTokens: 0,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
    },
    stopReason: "error",
    errorMessage: error.message,
    timestamp: Date.now(),
  };
  return {
    async *[Symbol.asyncIterator](): AsyncIterator<AssistantMessageEvent> {
      yield { type: "error", reason: "error", error: message };
    },
    result: async () => message,
  };
}

/** The host's stream function with the guard before and the answer rule after. */
export function relaymindStreamFn(base: StreamFn): StreamFn {
  return async (model: Model, context: Context, options?: Options) => {
    try {
      checkContext(context);
      checkOptions(options);
    } catch (error) {
      return refused(model, error as Error);
    }
    const hostHook = options?.onPayload;
    const onPayload = async (payload: unknown, payloadModel: Model) => {
      const replaced = hostHook ? await hostHook(payload, payloadModel) : undefined;
      const next = replaced === undefined ? payload : replaced;
      // A throw here fails the request before it is sent (the transport's error path).
      checkPayload(next, context);
      return replaced;
    };
    return policed(await base(model, context, { ...options, onPayload }));
  };
}
