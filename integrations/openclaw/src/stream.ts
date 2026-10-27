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
//   because the gateway exposes one call per response;
// - a gateway failure (an HTTP error or a streamed error event) keeps its code
//   and gets one sanitized, actionable message (src/errors.ts, #138), in the
//   error event and the result alike;
// - every request carries an Idempotency-Key for its inference payload
//   (src/idempotency.ts), and the plugin makes one attempt unless maxRetries
//   allows a retry of a 429 (same key) or a 504 (a new key) before any answer
//   content; a failed attempt it retries is never shown to the host.
//
// The wrapper forwards every event in order and never changes emitted text: it
// only replaces the terminal "done" event of a response that breaks a rule with
// an "error" event. The host keeps its tools, its approvals, and its own retries;
// a host retry of the same conversation sends the key it last used, so the
// gateway replays a completed run instead of running it again, and a key whose
// run ended failed or cancelled is never sent again (section 7.4, KeyLedger).
import type { ProviderWrapStreamFnContext } from "openclaw/plugin-sdk/plugin-entry";

import { describeFailure, gatewayFailure, retryRule, type GatewayFailure } from "./errors.ts";
import { IDEMPOTENCY_HEADER, idempotencyKey, KeyLedger, turnId, type KeyFate } from "./idempotency.ts";
import { MAX_RETRIES } from "./config.ts";
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

/**
 * A gateway failure with the plugin's sanitized, actionable message (src/errors.ts);
 * any other message unchanged. The gateway's code stays the error code.
 */
export function withGatewayMessage(
  message: AssistantMessage,
  secrets: readonly (string | undefined)[] = [],
): AssistantMessage {
  const failure = gatewayFailure(message);
  if (failure === undefined) return message;
  return { ...message, errorMessage: describeFailure(failure, secrets) };
}

/** The final message under the plugin's rules: the answer rule, then the error message. */
function settle(message: AssistantMessage, secrets: readonly (string | undefined)[]) {
  return withGatewayMessage(checkAnswer(message), secrets);
}

/** A failed message for an attempt that produced none (refused, or the stream threw). */
function failedMessage(model: Model, reason: string): AssistantMessage {
  return {
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
    errorMessage: reason,
    timestamp: Date.now(),
  };
}

const isFailure = (message: AssistantMessage) =>
  message.stopReason === "error" || message.stopReason === "aborted";

/** The terminal event of a final message: "error" for a failure, "done" otherwise, with its own reason. */
function terminalEvent(message: AssistantMessage): AssistantMessageEvent {
  return (
    isFailure(message)
      ? { type: "error", reason: message.stopReason, error: message }
      : { type: "done", reason: message.stopReason, message }
  ) as AssistantMessageEvent;
}

/** The final message of a request the host aborted before the plugin sent it again. */
function abortedMessage(model: Model): AssistantMessage {
  return { ...failedMessage(model, "Request was aborted"), stopReason: "aborted" };
}

/**
 * What an attempt leaves its key (src/idempotency.ts KeyLedger). An answer keeps
 * it (a retry replays it); so does a refusal that created no run (a 429 stores
 * nothing) and a conflict (the key is another payload's, or its run is still
 * going). A run that ended failed or cancelled spends it (section 7.4: 409
 * run_terminal), and so does an abort or a transport failure, whose run may
 * exist and did not complete: a new key costs at most a new run.
 */
function keyFate(message: AssistantMessage, failure: GatewayFailure | undefined): KeyFate {
  if (!isFailure(message)) return "keep";
  if (failure === undefined) return "spent";
  if (failure.code === "idempotency_conflict") return "keep";
  if (failure.code === "run_terminal" || retryRule(failure.code) === "new_key") return "spent";
  return failure.runId ? "spent" : "keep";
}

/** A failed stream that sends nothing: the request was refused before it was built. */
function refused(model: Model, error: Error): AssistantMessageEventStreamLike {
  const message = failedMessage(model, error.message);
  return {
    async *[Symbol.asyncIterator](): AsyncIterator<AssistantMessageEvent> {
      yield { type: "error", reason: "error", error: message };
    },
    result: async () => message,
  };
}

/** A single-consumer event queue: the wrapper writes, the host reads. */
function channel<T>() {
  const items: T[] = [];
  let ended = false;
  let wake: (() => void) | undefined;
  const notify = () => {
    wake?.();
    wake = undefined;
  };
  return {
    push(...values: T[]) {
      items.push(...values);
      notify();
    },
    end() {
      ended = true;
      notify();
    },
    async *[Symbol.asyncIterator](): AsyncIterator<T> {
      for (;;) {
        if (items.length > 0) {
          yield items.shift()!;
          continue;
        }
        if (ended) return;
        await new Promise<void>((resolve) => (wake = resolve));
      }
    },
  };
}

/** What one attempt sent and got back, for diagnostics (src/attempts.ts). */
export interface AttemptReport {
  /** The OpenClaw turn of the request (src/idempotency.ts turnId). */
  turn: string;
  attempt: number;
  idempotencyKey: string;
  /** The HTTP status and headers of an accepted (2xx) response; undefined otherwise. */
  response?: { status: number; headers: Record<string, string> };
  /** The attempt's final message under the answer rule, with the transport's gateway fields. */
  raw: AssistantMessage;
  /** Whether the plugin retries after this attempt. */
  retried: boolean;
}

export interface RelayMindStreamOptions {
  /** The credential values seen in this process, removed from every message. */
  secrets?: () => readonly (string | undefined)[];
  /** Plugin retries after a 429 or a 504 before any content; 0 (the default) to 2. */
  maxRetries?: number;
  /** The wait before retry n is n times this; 500 ms by default. */
  retryDelayMs?: number;
  /** Called once per attempt, after its outcome is known. */
  onAttempt?: (report: AttemptReport) => void;
  /** The key each payload sends next; one per provider, so a host retry sees the earlier attempts. */
  keys?: KeyLedger;
}

const RETRY_DELAY_MS = 500;
/** The one event that shows no answer content; after any other, the attempt is the answer and is never retried. */
const CONTENT_FREE = "start";

/** The value of a header, matched without regard to case; undefined when it is absent. */
export function headerValue(headers: Record<string, string> | undefined, name: string): string | undefined {
  if (!headers) return undefined;
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() === wanted) return value;
  return undefined;
}

function wait(ms: number, signal: AbortSignal | undefined): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(true);
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve(false);
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * The host's stream function with the guard before, the answer rule and the
 * error mapping after, an Idempotency-Key on every request, and the bounded
 * retry rule of section 7.5 (src/idempotency.ts, #138).
 */
export function relaymindStreamFn(base: StreamFn, streamOptions: RelayMindStreamOptions = {}): StreamFn {
  const secrets = streamOptions.secrets ?? (() => []);
  const maxRetries = streamOptions.maxRetries ?? 0;
  if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > MAX_RETRIES) {
    throw new RangeError(`maxRetries must be an integer from 0 to ${MAX_RETRIES}`);
  }
  const delayMs = streamOptions.retryDelayMs ?? RETRY_DELAY_MS;
  const keys = streamOptions.keys ?? new KeyLedger();
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
    const known = () => [...secrets(), options?.apiKey];
    // A key the host set itself is the host's: kept as is, and never retried by the plugin.
    const hostKey = headerValue(options?.headers, IDEMPOTENCY_HEADER);
    const baseKey = hostKey ?? idempotencyKey({ model, context, options });
    const turn = turnId(context, (options as { sessionId?: string } | undefined)?.sessionId);
    const out = channel<AssistantMessageEvent>();
    let resolveFinal!: (message: AssistantMessage) => void;
    const final = new Promise<AssistantMessage>((resolve) => (resolveFinal = resolve));

    const attemptOnce = async (key: string, attempt: number) => {
      let response: AttemptReport["response"];
      const hostOnResponse = options?.onResponse;
      const headers = hostKey ? { ...options?.headers } : { ...options?.headers, [IDEMPOTENCY_HEADER]: key };
      const held: AssistantMessageEvent[] = [];
      let committed = false;
      let terminal: { event: AssistantMessageEvent; raw: AssistantMessage } | undefined;
      try {
        const inner = await base(model, context, {
          ...options,
          headers,
          onPayload,
          onResponse: async (r: { status: number; headers: Record<string, string> }, m: Model) => {
            response = { status: r.status, headers: r.headers };
            return hostOnResponse?.(r, m);
          },
        } as Options);
        for await (const event of inner) {
          if (event.type === "done" || event.type === "error") {
            terminal = { event, raw: event.type === "done" ? event.message : event.error };
            break;
          }
          if (!committed && event.type === CONTENT_FREE) {
            held.push(event);
            continue;
          }
          if (!committed) {
            committed = true;
            out.push(...held.splice(0));
          }
          out.push(event);
        }
        if (terminal === undefined) {
          const raw = await inner.result();
          terminal = { event: terminalEvent(raw), raw };
        }
      } catch (error) {
        const raw = failedMessage(model, error instanceof Error ? error.message : String(error));
        terminal = { event: { type: "error", reason: "error", error: raw }, raw };
      }
      return { response, held, committed, ...terminal };
    };

    const emit = (held: AssistantMessageEvent[], event: AssistantMessageEvent, raw: AssistantMessage) => {
      const settled = settle(raw, known());
      out.push(...held);
      if (event.type === "done" && settled === raw && !isFailure(raw)) out.push(event);
      else if (event.type === "error") out.push({ ...event, error: settled } as AssistantMessageEvent);
      else out.push(terminalEvent(settled));
      resolveFinal(settled);
    };

    // Diagnostics never fail a request.
    const report = (attempt: AttemptReport) => {
      try {
        streamOptions.onAttempt?.(attempt);
      } catch {
        // A broken diagnostics writer is not the answer's problem.
      }
    };

    const run = async () => {
      // A host key is sent as is; the plugin's own key follows the ledger (section 7.4).
      let { key, n } = hostKey ? { key: hostKey, n: 0 } : keys.next(baseKey);
      for (let attempt = 1; ; attempt++) {
        const outcome = await attemptOnce(key, attempt);
        // Under the answer rule: an empty answer is recorded as the failure the host sees.
        const checked = checkAnswer(outcome.raw);
        const failure = gatewayFailure(checked);
        if (!hostKey) keys.record(baseKey, n, keyFate(checked, failure));
        const rule = failure ? retryRule(failure.code) : "none";
        const retryable = !hostKey && !outcome.committed && rule !== "none" && attempt <= maxRetries;
        // wait() is false only when the host aborts: then the abort is the outcome, not the last failure.
        const retry = retryable && (await wait(delayMs * attempt, options?.signal ?? undefined));
        const aborted = retryable && !retry ? abortedMessage(model) : undefined;
        report({
          turn,
          attempt,
          idempotencyKey: key,
          ...(outcome.response ? { response: outcome.response } : {}),
          raw: aborted ?? checked,
          retried: retry,
        });
        if (retry) {
          // The ledger gives the same key after a 429 and <key>-retry<n+1> after a 504.
          ({ key, n } = keys.next(baseKey));
          continue;
        }
        if (aborted) emit(outcome.held, terminalEvent(aborted), aborted);
        else emit(outcome.held, outcome.event, outcome.raw);
        break;
      }
    };
    // Whatever happens, the host gets one terminal event and a result.
    void run()
      .catch((error: unknown) => {
        const raw = failedMessage(model, error instanceof Error ? error.message : String(error));
        emit([], { type: "error", reason: "error", error: raw }, raw);
      })
      .finally(() => out.end());

    return {
      [Symbol.asyncIterator]: () => out[Symbol.asyncIterator](),
      result: () => final,
    } as AssistantMessageEventStreamLike;
  };
}
