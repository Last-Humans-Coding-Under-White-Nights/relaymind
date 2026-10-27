// Unsupported input is refused by name, never removed (RM19, RM33; spec sections
// 7.1 and 7.5).
//
// The client-owned alias takes text, one function call per response, and the
// section 7.1 request subset. The pinned host adapts what a model cannot take
// instead of failing: an image part becomes the text "(image omitted: ...)", and
// a structured response format or a strict flag that the declared switches turn
// off is dropped. Each of those changes the question, so the plugin refuses the
// request before it is sent and names the field. The guard only reads: it never
// trims, summarizes, reorders, or drops a message or a tool. The gateway still
// rejects what section 7.1 rejects; the guard front-runs a few of those
// rejections with a clearer message.
import type { Context } from "./stream.ts";

export class RelayMindInputError extends Error {
  override name = "RelayMindInputError";
  /** The refused field, as a path into the host context or the request body. */
  readonly field: string;

  constructor(field: string, reason: string) {
    super(`RelayMind refused the request before sending it: ${field} ${reason}`);
    this.field = field;
  }
}

const NEVER_REMOVED =
  "the client-owned alias takes text only (spec 7.1), and the plugin never removes " +
  "input to make a request fit";

type Part = { type?: unknown };

/** Refuse a host context that the transport would change before sending. */
export function checkContext(context: Context): void {
  context.messages.forEach((message, m) => {
    if (message.role !== "user" && message.role !== "toolResult") return;
    const content = message.content as string | Part[];
    if (typeof content === "string") return;
    content.forEach((part, p) => {
      if (part.type !== "text") {
        throw new RelayMindInputError(
          `messages[${m}].content[${p}].type`,
          `is ${String(part.type)}; ${NEVER_REMOVED}`,
        );
      }
    });
  });
}

/** Refuse request options that the declared switches would drop silently. */
export function checkOptions(options: { responseFormat?: unknown } | undefined): void {
  const format = options?.responseFormat as { type?: unknown } | undefined;
  if (format !== undefined && format.type !== "text") {
    throw new RelayMindInputError(
      "response_format",
      `is ${String(format.type)}; the gateway answers 400 unsupported_parameter for a ` +
        "structured response format (spec 7.1), and the plugin does not drop it",
    );
  }
}

/**
 * Refuse an outgoing body that asks for behavior the backend does not have; it
 * runs on the final payload, after every host payload hook.
 */
export function checkPayload(payload: unknown, context: Context): void {
  if (!payload || typeof payload !== "object") return;
  const body = payload as Record<string, unknown>;
  if (body.parallel_tool_calls === true) {
    throw new RelayMindInputError(
      "parallel_tool_calls",
      "is true; the gateway answers 400 unsupported_parameter, because RelayMind returns " +
        "one function call per response (spec 7.2)",
    );
  }
  if (body.n !== undefined && body.n !== null && body.n !== 1) {
    throw new RelayMindInputError(
      "n",
      `is ${String(body.n)}; the gateway returns one choice (spec 7.1)`,
    );
  }
  const format = body.response_format as { type?: unknown } | undefined | null;
  if (format && format.type !== "text") {
    throw new RelayMindInputError(
      "response_format",
      `is ${String(format.type)}; the gateway answers 400 unsupported_parameter (spec 7.1)`,
    );
  }
  const tools = Array.isArray(body.tools) ? (body.tools as { function?: Record<string, unknown> }[]) : [];
  tools.forEach((tool, t) => {
    if (tool.function?.strict === true) {
      throw new RelayMindInputError(
        `tools[${t}].function.strict`,
        "is true; the gateway does not offer strict schemas (spec 7.1)",
      );
    }
  });
  const sent = new Set(tools.map((tool) => tool.function?.name));
  const lost = (context.tools ?? []).map((tool) => tool.name).filter((name) => !sent.has(name));
  if (lost.length > 0) {
    throw new RelayMindInputError(
      "tools",
      `would leave out ${lost.join(", ")}; the plugin never drops a host tool (spec 7.5)`,
    );
  }
  const messages = Array.isArray(body.messages) ? (body.messages as { content?: unknown }[]) : [];
  messages.forEach((message, m) => {
    if (!Array.isArray(message.content)) return;
    (message.content as Part[]).forEach((part, p) => {
      if (part.type !== "text") {
        throw new RelayMindInputError(
          `messages[${m}].content[${p}].type`,
          `is ${String(part.type)}; ${NEVER_REMOVED}`,
        );
      }
    });
  });
}
