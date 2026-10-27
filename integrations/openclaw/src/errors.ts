// Gateway errors as the host's error classes (RM12, RM16, RM20, RM33; spec
// sections 7.5 and 11, #138).
//
// The pinned transport turns a gateway error into a failed assistant message:
// errorCode is the gateway's code, errorBody the JSON of the gateway's error
// object ({code, message, run_id, reason_code}), and errorMessage
// "<status> <message>" for an HTTP error or the bare message for a streamed
// error event. The plugin reads those fields and writes one sanitized,
// actionable message that starts with "RelayMind <code> (...)", so the class
// can be read back from the message alone. The classes follow section 7.5:
// context_limit_exceeded is a context overflow, needs_external and
// external_failed are failures of this provider, 429 overloaded is a temporary
// overload, 504 deadline_exceeded is a timeout, and 401 is an authentication
// failure with the reconnect instruction. A class is a truthful answer to the
// host's own failover hooks; whether the host then fails over is host policy,
// which the plugin neither triggers nor hides.
import { RECONNECT } from "./diagnostics.ts";
import { redact } from "./redaction.ts";

/** The host's failover reasons the plugin uses (the frozen FAILOVER_REASONS of the pinned SDK). */
export type RelayMindFailoverReason =
  | "auth"
  | "format"
  | "overloaded"
  | "server_error"
  | "timeout"
  | "context_overflow"
  | "empty_response"
  | "unclassified";

/** How a failed attempt may be retried under the bounded rule (section 7.5). */
export type RetryRule = "none" | "same_key" | "new_key";

export interface GatewayFailure {
  /** The gateway's error code (spec section 11). */
  code: string;
  /** The HTTP status; null for an error event inside a 200 stream. */
  status: number | null;
  /** The run the error belongs to; null when the gateway created none or the body was cut. */
  runId: string | null;
  reasonCode: string | null;
  gatewayMessage: string | null;
}

interface CodeRule {
  reason: RelayMindFailoverReason;
  retry: RetryRule;
  explain: string;
}

const PROVIDER_FAILURE = "This is a failure of the RelayMind provider, not an answer.";

const RULES: Record<string, CodeRule> = {
  context_limit_exceeded: {
    reason: "context_overflow",
    retry: "none",
    explain:
      "The conversation does not fit the gateway's external context limit (a context overflow). " +
      "RelayMind never trims the host's conversation or tools.",
  },
  invalid_request: {
    reason: "format",
    retry: "none",
    explain: "The gateway refused the request as outside its chat contract (spec section 7.1).",
  },
  unsupported_parameter: {
    reason: "format",
    retry: "none",
    explain: "The gateway refused a request field it does not support (spec section 7.1).",
  },
  invalid_execution_mode: {
    reason: "format",
    retry: "none",
    explain: "The gateway does not know the execution mode the plugin sent.",
  },
  unauthenticated: { reason: "auth", retry: "none", explain: RECONNECT },
  execution_mode_forbidden: {
    reason: "server_error",
    retry: "none",
    explain:
      "The gateway's policy does not permit the configured executionMode. Change the plugin's " +
      `executionMode or the gateway's policy. ${PROVIDER_FAILURE}`,
  },
  idempotency_conflict: {
    reason: "server_error",
    retry: "none",
    explain:
      "The gateway already holds this Idempotency-Key for a run that has not finished, for a " +
      "different request, or for a response it cannot replay; nothing new was started.",
  },
  run_terminal: {
    reason: "server_error",
    retry: "none",
    explain:
      "This identical request already ran under the same Idempotency-Key and that run ended " +
      "failed or cancelled; RelayMind does not start it again under that key.",
  },
  needs_external: {
    reason: "server_error",
    retry: "none",
    explain:
      "The request cannot complete under the effective execution mode without the external " +
      `model (for example executionMode local_only). ${PROVIDER_FAILURE}`,
  },
  overloaded: {
    reason: "overloaded",
    retry: "same_key",
    explain:
      "The gateway's execution slot is held by another run or an idle probe; the request was " +
      "not queued and nothing was created. It can be sent again when the slot is free.",
  },
  internal_error: {
    reason: "server_error",
    retry: "none",
    explain: `The gateway hit an internal error. ${PROVIDER_FAILURE}`,
  },
  workflow_unavailable: {
    reason: "server_error",
    retry: "none",
    explain: `The gateway's configured workflow is unavailable. ${PROVIDER_FAILURE}`,
  },
  external_failed: {
    reason: "server_error",
    retry: "none",
    explain: `The gateway's external model could not finish the run. ${PROVIDER_FAILURE}`,
  },
  deadline_exceeded: {
    reason: "timeout",
    retry: "new_key",
    explain: "A gateway time limit ran out before an answer (a timeout).",
  },
  predicate_unmet: {
    reason: "server_error",
    retry: "none",
    explain: `The answer did not meet its completion check; no answer was completed. ${PROVIDER_FAILURE}`,
  },
};

const UNKNOWN: CodeRule = {
  reason: "unclassified",
  retry: "none",
  explain: `RelayMind returned an error the plugin does not know. ${PROVIDER_FAILURE}`,
};

/** The prefix of every message the plugin writes for a gateway failure. */
const PREFIX = /^RelayMind ([a-z_]{1,64}) \((?:HTTP \d{3}|stream error event)\): /;
/** The plugin's own empty-answer failure (src/stream.ts). */
const EMPTY_ANSWER = /^RelayMind returned an empty answer/;
const MAX_GATEWAY_TEXT = 300;
const SAFE_CODE = /^[a-z_]{1,64}$/;
const SAFE_ID = /^[A-Za-z0-9_-]{1,128}$/;

interface FailedLike {
  stopReason?: string;
  errorMessage?: string;
  errorCode?: string;
  errorBody?: string;
}

function parseBody(body: string | undefined): Record<string, unknown> {
  if (!body) return {};
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const inner = (parsed as { error?: unknown }).error;
      if (inner && typeof inner === "object" && !Array.isArray(inner)) {
        return inner as Record<string, unknown>;
      }
      return parsed as Record<string, unknown>;
    }
  } catch {
    // A cut body (the transport keeps 500 characters) still has errorCode.
  }
  return {};
}

const stringOr = (value: unknown, pattern: RegExp) =>
  typeof value === "string" && pattern.test(value) ? value : null;

/** The gateway failure behind a failed message; undefined when it is not one. */
export function gatewayFailure(message: FailedLike): GatewayFailure | undefined {
  if (message.stopReason !== "error") return undefined;
  const body = parseBody(message.errorBody);
  const code = stringOr(message.errorCode, SAFE_CODE) ?? stringOr(body.code, SAFE_CODE);
  if (code === null) return undefined;
  const status = /^(\d{3})\b/.exec(message.errorMessage ?? "");
  return {
    code,
    status: status ? Number(status[1]) : null,
    runId: stringOr(body.run_id, SAFE_ID),
    reasonCode: stringOr(body.reason_code, SAFE_CODE),
    gatewayMessage: typeof body.message === "string" ? body.message : null,
  };
}

function oneLine(text: string, secrets: readonly (string | undefined)[]): string {
  const flat = redact(text, secrets).replace(/\s+/g, " ").trim();
  return flat.length > MAX_GATEWAY_TEXT ? `${flat.slice(0, MAX_GATEWAY_TEXT)}...` : flat;
}

/** The one message the host shows for a gateway failure: sanitized and actionable. */
export function describeFailure(
  failure: GatewayFailure,
  secrets: readonly (string | undefined)[],
): string {
  const rule = RULES[failure.code] ?? UNKNOWN;
  const where = failure.status === null ? "stream error event" : `HTTP ${failure.status}`;
  const parts = [`RelayMind ${failure.code} (${where}): ${rule.explain}`];
  if (failure.gatewayMessage) parts.push(`Gateway: "${oneLine(failure.gatewayMessage, secrets)}".`);
  const run = failure.runId ? `Gateway run ${failure.runId}` : "Gateway run unavailable";
  parts.push(failure.reasonCode ? `${run}, reason ${failure.reasonCode}.` : `${run}.`);
  return parts.join(" ");
}

/** The retry rule of a gateway code (section 7.5): only 429 and 504 may be retried. */
export function retryRule(code: string): RetryRule {
  return (RULES[code] ?? UNKNOWN).retry;
}

interface HookContext {
  errorMessage: string;
  code?: string;
  status?: number;
}

function codeOf(ctx: HookContext): string | undefined {
  const fromMessage = PREFIX.exec(ctx.errorMessage ?? "")?.[1];
  if (fromMessage) return fromMessage;
  return ctx.code !== undefined && ctx.code in RULES ? ctx.code : undefined;
}

/** classifyFailoverReason: the class of a RelayMind failure; undefined for any other error. */
export function failoverReason(ctx: HookContext): RelayMindFailoverReason | undefined {
  if (EMPTY_ANSWER.test(ctx.errorMessage ?? "")) return "empty_response";
  const code = codeOf(ctx);
  if (code === undefined) return undefined;
  return (RULES[code] ?? UNKNOWN).reason;
}

/** matchesContextOverflowError: true only for context_limit_exceeded; undefined for other errors. */
export function isContextOverflow(ctx: HookContext): boolean | undefined {
  const code = codeOf(ctx);
  if (code === undefined) return undefined;
  return code === "context_limit_exceeded";
}
