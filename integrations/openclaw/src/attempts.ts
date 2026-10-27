// Plugin diagnostics: one record per inference attempt (RM12, RM22, RM25, RM33;
// spec section 7.5, #138).
//
// Each attempt the plugin sends is recorded with the gateway run it belongs to:
// X-RelayMind-Run-ID of an accepted response, else the run_id of the gateway's
// error body, else the run ID inside the chat response's ID (chatcmpl-<run>).
// An attempt with none, such as one aborted before the gateway sent headers or
// a 429 that created no run, is recorded as "unavailable", never invented. One
// OpenClaw turn (src/idempotency.ts turnId) can hold several records, one per
// attempt and one per tool-result request, so the turn-to-runs mapping is the
// set of records with the same turn. The native run view and the demo UI page
// of each run are linked (both need the application token; the links hold
// none). Usage is "unknown": the gateway's chat responses carry none, and the
// native trace is authoritative for model calls and per-model usage.
//
// Records go to the host logger and, when diagnosticsPath is set, to one JSON
// line each in that file. They never go into answer text, and they hold no
// trace events, no secrets, and no message content.
import { appendFileSync } from "node:fs";

import { gatewayFailure } from "./errors.ts";
import type { Logger } from "./redaction.ts";
import type { AttemptReport } from "./stream.ts";

export const ATTEMPT_LOG_PREFIX = "RelayMind inference attempt ";
const RUN_ID = /^run_[A-Za-z0-9_-]{1,120}$/;

export interface AttemptRecord {
  event: "relaymind.inference_attempt";
  turn: string;
  attempt: number;
  idempotencyKey: string;
  runId: string;
  runIdSource: "header" | "error_body" | "response_id" | null;
  outcome: "answer" | "tool_call" | "failed" | "aborted";
  status: number | null;
  code: string | null;
  reasonCode: string | null;
  replayed: boolean;
  retried: boolean;
  nativeRunUrl: string | null;
  uiUrl: string | null;
  usage: "unknown";
}

function header(headers: Record<string, string> | undefined, name: string): string | undefined {
  if (!headers) return undefined;
  for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() === name) return value;
  return undefined;
}

const runIdOf = (value: unknown) => (typeof value === "string" && RUN_ID.test(value) ? value : null);

/** The record of one attempt; baseUrl is the plugin's configured /v1 root. */
export function attemptRecord(report: AttemptReport, baseUrl: string): AttemptRecord {
  const raw = report.raw;
  const failure = gatewayFailure(raw);
  const fromHeader = runIdOf(header(report.response?.headers, "x-relaymind-run-id"));
  const fromBody = failure?.runId ? runIdOf(failure.runId) : null;
  const responseId = (raw as { responseId?: unknown }).responseId;
  const fromResponse =
    typeof responseId === "string" ? runIdOf(responseId.replace(/^chatcmpl-/, "")) : null;
  const runId = fromHeader ?? fromBody ?? fromResponse;
  const source = fromHeader ? "header" : fromBody ? "error_body" : fromResponse ? "response_id" : null;
  const origin = baseUrl.replace(/\/v1$/, "");
  let outcome: AttemptRecord["outcome"];
  if (raw.stopReason === "aborted") outcome = "aborted";
  else if (raw.stopReason === "error") outcome = "failed";
  else if (raw.stopReason === "toolUse") outcome = "tool_call";
  else outcome = "answer";
  return {
    event: "relaymind.inference_attempt",
    turn: report.turn,
    attempt: report.attempt,
    idempotencyKey: report.idempotencyKey,
    runId: runId ?? "unavailable",
    runIdSource: source,
    outcome,
    status: failure ? failure.status : (report.response?.status ?? null),
    code: failure?.code ?? null,
    reasonCode: failure?.reasonCode ?? null,
    replayed: header(report.response?.headers, "idempotent-replayed") === "true",
    retried: report.retried,
    nativeRunUrl: runId ? `${origin}/relay/v1/runs/${runId}` : null,
    uiUrl: runId ? `${origin}/ui/runs/${runId}` : null,
    usage: "unknown",
  };
}

export interface AttemptLogOptions {
  /** The plugin's redacting logger (src/redaction.ts). */
  logger: Logger;
  /** diagnosticsPath: one JSON line per attempt is appended here. */
  path?: string;
}

/** A writer of attempt records; a file it cannot write is one warning, never a failed request. */
export function createAttemptLog(options: AttemptLogOptions): (record: AttemptRecord) => void {
  let warned = false;
  return (record) => {
    const line = JSON.stringify(record);
    options.logger.info(`${ATTEMPT_LOG_PREFIX}${line}`);
    if (!options.path) return;
    try {
      appendFileSync(options.path, `${line}\n`, "utf8");
    } catch (error) {
      if (warned) return;
      warned = true;
      const why = (error as NodeJS.ErrnoException).code ?? "an error";
      options.logger.warn(
        `RelayMind could not write the diagnosticsPath file (${why}); attempt records still go to the log.`,
      );
    }
  };
}
