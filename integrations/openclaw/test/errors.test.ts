// Gateway errors as the host's error classes (RM12, RM16, RM20, RM33; spec
// sections 7.5 and 11, #138).
//
// The pinned transport turns a gateway error into a failed assistant message:
// errorCode is the gateway's code, errorBody the JSON of its error object (with
// run_id and reason_code), and errorMessage "<status> <message>" for an HTTP
// error or the bare message for a streamed error event (recorded in
// test/stream.test.ts). The plugin reads those fields, writes one sanitized,
// actionable message, and classifies the failure truthfully for the host's
// failover hooks; it never decides the host's failover.
import { describe, expect, it } from "vitest";

import { RECONNECT } from "../src/diagnostics.ts";
import {
  describeFailure,
  failoverReason,
  gatewayFailure,
  isContextOverflow,
  retryRule,
} from "../src/errors.ts";

const TOKEN = "rm-app-token-4f9c2a7e1b3d5c6a8e0f2b4d6c8a0e1f";

function failedMessage(fields: {
  status?: number;
  code?: string;
  message?: string;
  runId?: string | null;
  reasonCode?: string;
}) {
  const body: Record<string, unknown> = {
    code: fields.code,
    message: fields.message ?? "gateway message",
    run_id: fields.runId ?? null,
  };
  if (fields.reasonCode) body.reason_code = fields.reasonCode;
  const text = fields.message ?? "gateway message";
  return {
    role: "assistant",
    content: [],
    stopReason: "error",
    errorMessage: fields.status === undefined ? text : `${fields.status} ${text}`,
    ...(fields.code ? { errorCode: fields.code } : {}),
    errorBody: JSON.stringify(body),
  } as never;
}

// Every gateway code with its status (spec section 11) and the host's class.
const TABLE: [string, number | undefined, string, string][] = [
  ["context_limit_exceeded", 400, "context_overflow", "none"],
  ["invalid_request", 400, "format", "none"],
  ["unsupported_parameter", 400, "format", "none"],
  ["invalid_execution_mode", 400, "format", "none"],
  ["unauthenticated", 401, "auth", "none"],
  ["execution_mode_forbidden", 403, "server_error", "none"],
  ["idempotency_conflict", 409, "server_error", "none"],
  ["run_terminal", 409, "server_error", "none"],
  ["needs_external", 422, "server_error", "none"],
  ["overloaded", 429, "overloaded", "same_key"],
  ["internal_error", 500, "server_error", "none"],
  ["workflow_unavailable", 503, "server_error", "none"],
  ["external_failed", 503, "server_error", "none"],
  ["deadline_exceeded", 504, "timeout", "new_key"],
  ["predicate_unmet", undefined, "server_error", "none"],
];

describe("reading a gateway failure from the transport's failed message", () => {
  it("reads the code, status, run ID, and reason code", () => {
    const failure = gatewayFailure(
      failedMessage({
        status: 422,
        code: "needs_external",
        runId: "run_1",
        reasonCode: "context_limit",
      }),
    );
    expect(failure).toEqual({
      code: "needs_external",
      status: 422,
      runId: "run_1",
      reasonCode: "context_limit",
      gatewayMessage: "gateway message",
    });
  });

  it("a streamed error event has no HTTP status", () => {
    const failure = gatewayFailure(failedMessage({ code: "external_failed", runId: "run_9" }));
    expect(failure?.status).toBeNull();
    expect(failure?.runId).toBe("run_9");
  });

  it("is undefined for a message that did not fail or carries no gateway code", () => {
    expect(gatewayFailure({ role: "assistant", content: [], stopReason: "stop" } as never)).toBeUndefined();
    expect(
      gatewayFailure({
        role: "assistant",
        content: [],
        stopReason: "error",
        errorMessage: "Connection error.",
      } as never),
    ).toBeUndefined();
    expect(
      gatewayFailure({
        role: "assistant",
        content: [],
        stopReason: "aborted",
        errorMessage: "Request was aborted",
      } as never),
    ).toBeUndefined();
  });

  it("a truncated or non-JSON error body still gives the code; the run ID is then null", () => {
    const failure = gatewayFailure({
      role: "assistant",
      content: [],
      stopReason: "error",
      errorMessage: "503 the external model could not finish the run",
      errorCode: "external_failed",
      errorBody: '{"code":"external_failed","message":"the ext',
    } as never);
    expect(failure).toMatchObject({ code: "external_failed", status: 503, runId: null });
  });
});

describe("the host's error classes (section 7.5)", () => {
  it.each(TABLE)("%s (HTTP %s) is %s", (code, status, reason) => {
    const message = describeFailure(
      gatewayFailure(failedMessage({ status, code, runId: "run_1" }))!,
      [],
    );
    expect(failoverReason({ errorMessage: message, code })).toBe(reason);
    // The class survives without the code: the host may pass only the message.
    expect(failoverReason({ errorMessage: message })).toBe(reason);
  });

  it("only context_limit_exceeded is a context overflow; every other gateway code is not", () => {
    for (const [code, status] of TABLE) {
      const message = describeFailure(gatewayFailure(failedMessage({ status, code }))!, []);
      expect(isContextOverflow({ errorMessage: message, code })).toBe(
        code === "context_limit_exceeded",
      );
    }
  });

  it("a local context limit under needs_external is not a context overflow", () => {
    // reason_code context_limit: the local model is too small, the external one was not allowed.
    const message = describeFailure(
      gatewayFailure(failedMessage({ status: 422, code: "needs_external", reasonCode: "context_limit" }))!,
      [],
    );
    expect(isContextOverflow({ errorMessage: message, code: "needs_external" })).toBe(false);
    expect(failoverReason({ errorMessage: message, code: "needs_external" })).toBe("server_error");
  });

  it("the plugin's own empty-answer failure is an empty response", () => {
    expect(
      failoverReason({ errorMessage: "RelayMind returned an empty answer: no text and no function call." }),
    ).toBe("empty_response");
  });

  it("leaves errors that are not RelayMind's to the host's own classification", () => {
    expect(failoverReason({ errorMessage: "Connection error." })).toBeUndefined();
    expect(isContextOverflow({ errorMessage: "Connection error." })).toBeUndefined();
    expect(failoverReason({ errorMessage: "maximum context length exceeded", code: "other" })).toBeUndefined();
  });
});

describe("sanitized, actionable messages", () => {
  it("names the code, the status, the run ID, and the reason code", () => {
    const message = describeFailure(
      gatewayFailure(
        failedMessage({ status: 422, code: "needs_external", runId: "run_7", reasonCode: "context_limit" }),
      )!,
      [],
    );
    expect(message).toMatch(/^RelayMind needs_external \(HTTP 422\): /);
    expect(message).toContain("run run_7");
    expect(message).toContain("reason context_limit");
    expect(message).toMatch(/executionMode/);
  });

  it("a streamed error event is named as one", () => {
    const message = describeFailure(gatewayFailure(failedMessage({ code: "external_failed" }))!, []);
    expect(message).toMatch(/^RelayMind external_failed \(stream error event\): /);
  });

  it("401 carries the exact reconnect instruction", () => {
    const message = describeFailure(
      gatewayFailure(failedMessage({ status: 401, code: "unauthenticated" }))!,
      [],
    );
    expect(message).toContain(RECONNECT);
  });

  it("429 says nothing was queued or created, and 504 names the time limit", () => {
    const overloaded = describeFailure(gatewayFailure(failedMessage({ status: 429, code: "overloaded" }))!, []);
    expect(overloaded).toMatch(/not queued/);
    const deadline = describeFailure(
      gatewayFailure(failedMessage({ status: 504, code: "deadline_exceeded", message: "the time limit external_seconds was exhausted before an answer" }))!,
      [],
    );
    expect(deadline).toContain("external_seconds");
  });

  it("removes a known secret and any bearer credential from the gateway's text", () => {
    const message = describeFailure(
      gatewayFailure(
        failedMessage({
          status: 400,
          code: "invalid_request",
          message: `bad value ${TOKEN} and Bearer abc.def`,
        }),
      )!,
      [TOKEN],
    );
    expect(message).not.toContain(TOKEN);
    expect(message).not.toContain("abc.def");
  });

  it("bounds the gateway's text and puts it on one line", () => {
    const long = `${"x".repeat(2000)}\nsecond line`;
    const message = describeFailure(
      gatewayFailure(failedMessage({ status: 400, code: "invalid_request", message: long }))!,
      [],
    );
    expect(message.length).toBeLessThan(1200);
    expect(message).not.toContain("\n");
  });

  it("an unknown code is still a named RelayMind failure", () => {
    const message = describeFailure(
      gatewayFailure(failedMessage({ status: 418, code: "teapot" }))!,
      [],
    );
    expect(message).toMatch(/^RelayMind teapot \(HTTP 418\): /);
    expect(failoverReason({ errorMessage: message, code: "teapot" })).toBe("unclassified");
  });
});

describe("what may be retried (section 7.5 bounded retry rule)", () => {
  it.each(TABLE)("%s: %s", (code, _status, _reason, rule) => {
    expect(retryRule(code)).toBe(rule);
  });
});
