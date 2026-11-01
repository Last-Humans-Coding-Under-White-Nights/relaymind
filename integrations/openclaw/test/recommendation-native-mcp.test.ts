// RM08–RM12, RM16, RM39; AC35 (spec 0.65 section 7.6.2, section 14.5 native
// request row): the conditional direct MCP use path. Controlled host-shaped
// doubles only; a fixture route and registry are never qualification evidence.
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { RecommendationClientError } from "../src/recommendation-transport.js";
import { createFileUseLedger } from "../src/recommendation-native-ledger.js";
import { fixture } from "./recommendation-native-fixture.js";
import {
  canonicalSchemaHash,
  createNativeMcpHandler,
} from "../src/recommendation-native-mcp.js";
import { MAX_NATIVE_REQUEST_BYTES } from "../src/recommendation-native-authority.js";

const recommendationId = "4774d146-17bd-4ec9-8507-f528ccea51fa";
const contentHash = "c".repeat(64);
const inputSchema = {
  type: "object",
  properties: { text: { type: "string", maxLength: 64 } },
  required: ["text"],
  additionalProperties: false,
};
const schemaHash = canonicalSchemaHash(inputSchema)!;
const SCOPE = {
  route: "fixture.exact-use",
  transport: "stdio",
  toolScope: "independent-tool",
  maxApprovalWaitMs: 30000,
};

function ledgerPath(): string {
  return join(mkdtempSync(join(tmpdir(), "relaymind-ledger-")), "use.jsonl");
}
// Spec 0.65 7.6.2 condition 6: the native approval binds the same operation,
// target, full arguments, server/tool/schema and current authority.
const BOUND = [
  "operationId",
  "argumentsDigest",
  "serverId",
  "toolName",
  "exposedName",
  "schemaHash",
  "sessionKey",
];
function echo(request: any) {
  return Object.fromEntries(BOUND.map((key) => [key, request[key]]));
}
function setup(
  options: {
    qualified?: boolean;
    route?: boolean;
    ledger?: string | null;
  } = {},
) {
  const f = fixture();
  let now = 1;
  const record: unknown[] = [];
  const identity = {
    server_id: "alpha.one",
    tool_name: "append",
    exposed_name: "alpha_one__append",
    input_schema: inputSchema,
    schema_hash: schemaHash,
  };
  const view: any = {
    recommendation_id: recommendationId,
    task_id: "task",
    state: "chosen",
    choice_expires_at: "2030-01-01T00:00:00Z",
    selected_action: {
      recommendation_id: recommendationId,
      task_id: "task",
      scope: { user_id: f.binding.userId, workspace_id: f.binding.workspaceId },
      choice: {
        kind: "asset",
        asset_id: "mcp:alpha.one/append",
        content_hash: contentHash,
      },
    },
    displayed_candidates: [
      {
        asset: {
          asset_id: "mcp:alpha.one/append",
          kind: "mcp_tool",
          content_hash: contentHash,
          source_version: "1",
          stale: false,
          identity,
        },
      },
    ],
  };
  const native: any = {
    serverId: "alpha.one",
    toolName: "append",
    exposedName: "alpha_one__append",
    inputSchema: structuredClone(inputSchema),
    approvalDeadlineMs: 60000,
    current: true,
  };
  let decision: any = (request: any) => ({
    decision: "allow-once",
    approvalId: "approval-1",
    operationId: request.operationId,
    argumentsDigest: request.argumentsDigest,
  });
  let invoke: any = async () => ({
    ok: true,
    nativeRunId: "native-run-1",
    nativeCallId: "native-call-1",
  });
  const route: any = {
    route: SCOPE.route,
    transport: SCOPE.transport,
    toolScope: SCOPE.toolScope,
    bind: async (request: any) => {
      record.push([
        "bind",
        request.serverId,
        request.toolName,
        request.exposedName,
      ]);
      return {
        ...native,
        inputSchema: structuredClone(native.inputSchema),
        assertCurrent: () => {
          if (!native.current) throw Error("native_binding_retired");
        },
        release: () => record.push(["release"]),
      };
    },
    requestApproval: async (_binding: unknown, request: any, control: any) => {
      record.push(["approval", control.timeoutMs]);
      const value = await (typeof decision === "function"
        ? decision(request, control)
        : decision);
      // A well-behaved route echoes the binding unless a case overrides it.
      return value && typeof value === "object"
        ? { ...echo(request), ...value }
        : value;
    },
    invoke: async (_binding: unknown, request: any, control: any) => {
      record.push(["invoke", request.arguments, request.approvalId]);
      return invoke(request, control);
    },
  };
  const deps: any = {
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: f.dependencies.resolveWorkspace,
    monotonicNow: () => now,
    wallNow: () => Date.parse("2026-10-31T00:00:00Z"),
    qualification: (operation: string) =>
      options.qualified !== false && operation === "mcp_use"
        ? { adapterRevision: "a".repeat(64), mcp: SCOPE }
        : undefined,
    route: options.route === false ? undefined : route,
    recommendationClient: async () => ({
      get: async () => {
        record.push(["choice"]);
        return view;
      },
    }),
    actionClient: async () => ({
      listFeedback: async () => {
        record.push(["registration"]);
        return {
          schema_version: 1,
          records: [],
          feedback_revision: 0n,
          next_after_id: null,
        };
      },
    }),
    operationId: () => "operation-1",
    ledger:
      options.ledger === null
        ? undefined
        : createFileUseLedger(options.ledger ?? ledgerPath()),
  };
  f.options.params = {
    sessionKey: "native-key",
    expectedSessionId: "native-id",
    expectedLifecycleRevision: "native-life",
    recommendationId,
    assetId: "mcp:alpha.one/append",
    contentHash,
    arguments: { text: "hello" },
    idempotencyKey: "explicit-use-1",
  };
  const handler = createNativeMcpHandler(deps);
  return {
    ...f,
    view,
    native,
    route,
    deps,
    record,
    handler,
    run: () => handler(f.options),
    invokes: () => record.filter((item: any) => item[0] === "invoke").length,
    setDecision: (value: any) => {
      decision = value;
    },
    setInvoke: (value: any) => {
      invoke = value;
    },
    advance: (ms: number) => {
      now += ms;
    },
    reply: () => f.replies[f.replies.length - 1],
  };
}
function receipt(outcome: string, code: string, extra: object = {}) {
  return {
    operationId: "operation-1",
    recommendationId,
    assetId: "mcp:alpha.one/append",
    contentHash,
    outcome,
    code,
    nativeRunId: null,
    nativeCallId: null,
    ...extra,
  };
}

it("is refused as unqualified with zero calls when the registry does not list mcp_use", async () => {
  for (const variant of [{ qualified: false }, { route: false }]) {
    const f = setup(variant);
    await f.run();
    expect(f.reply()).toEqual([
      false,
      undefined,
      { code: "INVALID_REQUEST", message: "unqualified" },
    ]);
    expect(f.record).toHaveLength(0);
  }
});

it("refuses a route whose declared scope differs from the reviewed entry", async () => {
  const f = setup();
  f.route.transport = "http";
  await f.run();
  expect(f.reply()?.[2]).toEqual({
    code: "INVALID_REQUEST",
    message: "unqualified",
  });
  expect(f.record).toHaveLength(0);
});

it("admits the exact chosen tool once with a bound approval when a fixture registry qualifies it", async () => {
  const f = setup();
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("completed", "ok", {
      nativeRunId: "native-run-1",
      nativeCallId: "native-call-1",
    }),
    undefined,
  ]);
  expect(f.record.filter((item: any) => item[0] === "bind")).toEqual([
    ["bind", "alpha.one", "append", "alpha_one__append"],
  ]);
  expect(f.record.filter((item: any) => item[0] === "invoke")).toEqual([
    ["invoke", { text: "hello" }, "approval-1"],
  ]);
  expect(f.record.at(-1)).toEqual(["release"]);
});

it.each([
  [
    "an extra field",
    (f: any) => {
      f.options.params.serverId = "alpha?one";
    },
  ],
  [
    "non-object arguments",
    (f: any) => {
      f.options.params.arguments = ["hello"];
    },
  ],
  [
    "a nonfinite number",
    (f: any) => {
      f.options.params.arguments = { text: Number.NaN };
    },
  ],
  [
    "a malformed content hash",
    (f: any) => {
      f.options.params.contentHash = "x";
    },
  ],
  [
    "a malformed idempotency key",
    (f: any) => {
      f.options.params.idempotencyKey = " padded ";
    },
  ],
  [
    "a request over the native row limit",
    (f: any) => {
      f.options.params.arguments = {
        text: "x".repeat(MAX_NATIVE_REQUEST_BYTES),
      };
    },
  ],
])("returns only the closed invalid_request for %s", async (_label, change) => {
  const f = setup();
  change(f);
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "invalid_request" },
  ]);
  expect(f.record).toHaveLength(0);
});

it("accepts a request exactly at the native row limit boundary and refuses one byte over", async () => {
  const probe = setup();
  const base = Buffer.byteLength(
    JSON.stringify({
      ...probe.options.req,
      params: { ...probe.options.params, arguments: { text: "" } },
    }),
    "utf8",
  );
  const fits = setup();
  fits.native.inputSchema.properties.text.maxLength = MAX_NATIVE_REQUEST_BYTES;
  fits.view.displayed_candidates[0].asset.identity.input_schema =
    fits.native.inputSchema;
  fits.view.displayed_candidates[0].asset.identity.schema_hash =
    canonicalSchemaHash(fits.native.inputSchema);
  fits.options.params.arguments = {
    text: "x".repeat(MAX_NATIVE_REQUEST_BYTES - base),
  };
  await fits.run();
  expect(fits.reply()?.[1]).toMatchObject({ outcome: "completed" });
  const over = setup();
  over.options.params.arguments = {
    text: "x".repeat(MAX_NATIVE_REQUEST_BYTES - base + 1),
  };
  await over.run();
  expect(over.reply()?.[2]).toEqual({
    code: "INVALID_REQUEST",
    message: "invalid_request",
  });
});

it.each([
  [
    "a not-chosen recommendation",
    "choice_unavailable",
    (f: any) => {
      f.view.state = "ready";
    },
  ],
  [
    "an expired choice",
    "choice_unavailable",
    (f: any) => {
      f.view.choice_expires_at = "2020-01-01T00:00:00Z";
    },
  ],
  [
    "a foreign scope",
    "choice_unavailable",
    (f: any) => {
      f.view.selected_action.scope.user_id = "foreign";
    },
  ],
  [
    "a stale offered asset",
    "choice_unavailable",
    (f: any) => {
      f.view.displayed_candidates[0].asset.stale = true;
    },
  ],
  [
    "a skill asset",
    "choice_unavailable",
    (f: any) => {
      f.view.displayed_candidates[0].asset.kind = "skill";
    },
  ],
  [
    "a colliding server identity",
    "binding_changed",
    (f: any) => {
      f.native.serverId = "alpha?one";
    },
  ],
  [
    "a renamed exposed tool",
    "binding_changed",
    (f: any) => {
      f.native.exposedName = "alpha_one__append_2";
    },
  ],
  [
    "a backward-compatible schema change with still-valid arguments",
    "binding_changed",
    (f: any) => {
      f.native.inputSchema.properties.note = { type: "string" };
    },
  ],
  [
    "arguments outside the exact schema",
    "invalid_request",
    (f: any) => {
      f.options.params.arguments = { text: "hello", path: "/etc" };
    },
  ],
  [
    "arguments the schema refuses",
    "invalid_request",
    (f: any) => {
      f.options.params.arguments = { text: 7 };
    },
  ],
  [
    "a schema keyword the adapter cannot enforce",
    "unavailable",
    (f: any) => {
      f.native.inputSchema.properties.text.format = "uri";
      f.view.displayed_candidates[0].asset.identity.input_schema =
        f.native.inputSchema;
      f.view.displayed_candidates[0].asset.identity.schema_hash =
        canonicalSchemaHash(f.native.inputSchema);
    },
  ],
  [
    "revoked gateway registration",
    "unauthorized",
    (f: any) => {
      f.deps.actionClient = async () => ({
        listFeedback: async () => {
          throw new RecommendationClientError("user_action_required", 403);
        },
      });
    },
  ],
  [
    "approval denial",
    "approval_denied",
    (f: any) =>
      f.setDecision((r: any) => ({
        decision: "deny",
        approvalId: "approval-1",
        operationId: r.operationId,
        argumentsDigest: r.argumentsDigest,
      })),
  ],
  [
    "an approval bound to other arguments",
    "approval_denied",
    (f: any) =>
      f.setDecision((r: any) => ({
        decision: "allow-once",
        approvalId: "approval-1",
        operationId: r.operationId,
        argumentsDigest: "0".repeat(64),
      })),
  ],
  [
    "an approval bound to another operation",
    "approval_denied",
    (f: any) =>
      f.setDecision((r: any) => ({
        decision: "allow-once",
        approvalId: "approval-1",
        operationId: "other",
        argumentsDigest: r.argumentsDigest,
      })),
  ],
  [
    "an allow-always decision",
    "approval_denied",
    (f: any) =>
      f.setDecision((r: any) => ({
        decision: "allow-always",
        approvalId: "approval-1",
        operationId: r.operationId,
        argumentsDigest: r.argumentsDigest,
      })),
  ],
  [
    "server removal while approval is pending",
    "binding_changed",
    (f: any) =>
      f.setDecision((r: any) => {
        f.native.current = false;
        return {
          decision: "allow-once",
          approvalId: "approval-1",
          operationId: r.operationId,
          argumentsDigest: r.argumentsDigest,
        };
      }),
  ],
  [
    "choice erasure while approval is pending",
    "choice_unavailable",
    (f: any) =>
      f.setDecision((r: any) => {
        f.view.state = "expired";
        f.view.selected_action = null;
        return {
          decision: "allow-once",
          approvalId: "approval-1",
          operationId: r.operationId,
          argumentsDigest: r.argumentsDigest,
        };
      }),
  ],
  [
    "session reset while approval is pending",
    "unauthorized",
    (f: any) =>
      f.setDecision((r: any) => {
        f.retire();
        return {
          decision: "allow-once",
          approvalId: "approval-1",
          operationId: r.operationId,
          argumentsDigest: r.argumentsDigest,
        };
      }),
  ],
  [
    "policy change while approval is pending",
    "unauthorized",
    (f: any) =>
      f.setDecision((r: any) => {
        f.changeWorkspace();
        return {
          decision: "allow-once",
          approvalId: "approval-1",
          operationId: r.operationId,
          argumentsDigest: r.argumentsDigest,
        };
      }),
  ],
])("does not dispatch after %s", async (_label, code, change) => {
  const f = setup();
  change(f);
  await f.run();
  expect(f.reply()).toEqual([true, receipt("not_dispatched", code), undefined]);
  expect(f.invokes()).toBe(0);
});

it("refuses an unauthenticated caller with only a closed code", async () => {
  const f = setup();
  f.options.client.internal.agentToolCaller = true;
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "unauthorized" },
  ]);
  expect(f.record).toHaveLength(0);
});

it("bounds the approval wait separately and times out with zero server calls", async () => {
  const f = setup();
  f.native.approvalDeadlineMs = 20;
  f.setDecision(() => new Promise(() => {}));
  await f.run();
  expect(f.record.find((item: any) => item[0] === "approval")).toEqual([
    "approval",
    20,
  ]);
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "timeout"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
  expect(f.record.at(-1)).toEqual(["release"]);
});

it("uses the smaller of the native approval deadline and the declared maximum wait", async () => {
  const f = setup();
  f.native.approvalDeadlineMs = 10 * SCOPE.maxApprovalWaitMs;
  await f.run();
  expect(f.record.find((item: any) => item[0] === "approval")).toEqual([
    "approval",
    SCOPE.maxApprovalWaitMs,
  ]);
});

it("excludes the approval wait from the 60-second preparation deadline", async () => {
  const f = setup();
  const bind = f.route.bind;
  f.route.bind = async (...args: any[]) => {
    f.advance(40000);
    return bind(...args);
  };
  f.setDecision((r: any) => {
    f.advance(25000);
    return {
      decision: "allow-once",
      approvalId: "approval-1",
      operationId: r.operationId,
      argumentsDigest: r.argumentsDigest,
    };
  });
  await f.run();
  expect(f.reply()?.[1]).toMatchObject({ outcome: "completed", code: "ok" });
});

it("treats an approval resolved after its bound as timeout with zero server calls", async () => {
  const f = setup();
  f.setDecision((r: any) => {
    f.advance(SCOPE.maxApprovalWaitMs + 1);
    return {
      decision: "allow-once",
      approvalId: "approval-1",
      operationId: r.operationId,
      argumentsDigest: r.argumentsDigest,
    };
  });
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "timeout"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
});

it("times out before dispatch when preparation exceeds 60 seconds", async () => {
  const f = setup();
  const bind = f.route.bind;
  f.route.bind = async (...args: any[]) => {
    f.advance(60001);
    return bind(...args);
  };
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "timeout"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
});

it("reports host failure and indeterminate outcomes honestly without retrying", async () => {
  const failed = setup();
  failed.setInvoke(async () => ({
    ok: false,
    nativeRunId: "native-run-2",
    nativeCallId: "native-call-2",
  }));
  await failed.run();
  expect(failed.reply()?.[1]).toEqual(
    receipt("failed", "host_failed", {
      nativeRunId: "native-run-2",
      nativeCallId: "native-call-2",
    }),
  );
  const lost = setup();
  lost.setInvoke(async () => {
    throw Error("socket hang up");
  });
  await lost.run();
  expect(lost.reply()?.[1]).toEqual(
    receipt("indeterminate", "transport_unknown"),
  );
  expect(lost.invokes()).toBe(1);
  const unobserved = setup();
  unobserved.setInvoke(async () => ({
    ok: true,
    nativeRunId: null,
    nativeCallId: null,
  }));
  await unobserved.run();
  expect(unobserved.reply()?.[1]).toEqual(
    receipt("indeterminate", "transport_unknown"),
  );
});

it("replays a historical receipt for the same key and refuses a changed request", async () => {
  const f = setup();
  await f.run();
  await f.run();
  expect(f.invokes()).toBe(1);
  expect(f.replies[1]).toEqual(f.replies[0]);
  f.options.params.arguments = { text: "changed" };
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "idempotency_conflict"),
    undefined,
  ]);
  expect(f.invokes()).toBe(1);
});

it("fails closed as indeterminate when the same key is still unresolved", async () => {
  const f = setup();
  let release: () => void = () => {};
  f.setInvoke(
    () =>
      new Promise((resolve) => {
        release = () =>
          resolve({
            ok: true,
            nativeRunId: "native-run-1",
            nativeCallId: "native-call-1",
          });
      }),
  );
  const first = f.run();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await f.handler(f.options);
  expect(f.reply()).toEqual([
    true,
    receipt("indeterminate", "transport_unknown"),
    undefined,
  ]);
  release();
  await first;
  expect(f.invokes()).toBe(1);
});

it("rechecks current authorization before recovering a historical receipt", async () => {
  const f = setup();
  await f.run();
  f.retire();
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "unauthorized" },
  ]);
});

it("reports cancellation during dispatch as indeterminate", async () => {
  const f = setup();
  const abort = new AbortController();
  f.options.signal = abort.signal;
  f.setInvoke(async () => {
    abort.abort();
    throw Error("aborted");
  });
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("indeterminate", "cancelled"));
});

it("recomputes the RelayMind canonical schema hash rather than a JSON.stringify byte hash", () => {
  const reordered = {
    additionalProperties: false,
    required: ["text"],
    properties: { text: { maxLength: 64, type: "string" } },
    type: "object",
  };
  expect(canonicalSchemaHash(reordered)).toBe(schemaHash);
  expect(schemaHash).not.toBe(
    createHash("sha256").update(JSON.stringify(reordered)).digest("hex"),
  );
  // Python canonical_json: sorted keys, no spaces, non-ASCII kept as is.
  expect(canonicalSchemaHash({ b: 1, a: "é" })).toBe(
    createHash("sha256").update('{"a":"é","b":1}', "utf8").digest("hex"),
  );
  // Numbers whose text form may differ between implementations are refused.
  expect(canonicalSchemaHash({ maximum: 1e21 })).toBeUndefined();
  expect(canonicalSchemaHash({ multipleOf: 0.00001 })).toBeUndefined();
});

it("consumes an allow-once approval at most once (approval replay makes zero calls)", async () => {
  const f = setup();
  await f.run();
  f.options.params.idempotencyKey = "explicit-use-2";
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "approval_denied"),
    undefined,
  ]);
  expect(f.invokes()).toBe(1);
});

it("refuses historical recovery with only a closed code when registration was revoked", async () => {
  const f = setup();
  await f.run();
  f.deps.actionClient = async () => ({
    listFeedback: async () => {
      throw new RecommendationClientError("user_action_required", 403);
    },
  });
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "unauthorized" },
  ]);
  expect(f.invokes()).toBe(1);
});

/**
 * Spec 0.65 section 7.6.2 condition 1: "the same key may only recover its
 * historical receipt after current authorization checks, never dispatch
 * again. If dispatch history is lost or unresolved, fail closed with an
 * indeterminate outcome rather than replaying a possible mutation."
 * Condition 6: "Allow-once is consumed at most once." The key claim and the
 * consumed approval are durable before dispatch, so a restart (a new handler
 * over the same ledger file) cannot replay either.
 */
it("fails closed as indeterminate after a restart lost a dispatched key's outcome", async () => {
  const path = ledgerPath();
  const first = setup({ ledger: path });
  first.setInvoke(() => new Promise(() => {}));
  void first.run();
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(first.invokes()).toBe(1);
  // The process restarts: new handler, new ledger instance, same file.
  first.deps.ledger.close();
  const restarted = setup({ ledger: path });
  await restarted.run();
  expect(restarted.reply()).toEqual([
    true,
    receipt("indeterminate", "transport_unknown"),
    undefined,
  ]);
  expect(restarted.invokes()).toBe(0);
});

it("recovers a completed receipt after a restart without dispatching again", async () => {
  const path = ledgerPath();
  const first = setup({ ledger: path });
  await first.run();
  first.deps.ledger.close();
  const restarted = setup({ ledger: path });
  await restarted.run();
  expect(restarted.reply()).toEqual(first.reply());
  expect(restarted.invokes()).toBe(0);
});

it("keeps an allow-once approval consumed across a restart", async () => {
  const path = ledgerPath();
  const first = setup({ ledger: path });
  await first.run();
  first.deps.ledger.close();
  const restarted = setup({ ledger: path });
  restarted.options.params.idempotencyKey = "explicit-use-2";
  await restarted.run();
  expect(restarted.reply()).toEqual([
    true,
    receipt("not_dispatched", "approval_denied"),
    undefined,
  ]);
  expect(restarted.invokes()).toBe(0);
});

it("refuses without a durable use ledger before any call", async () => {
  const f = setup({ ledger: null });
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "unavailable" },
  ]);
  expect(f.record).toHaveLength(0);
});

it("refuses before preparation when the ledger cannot bind the key", async () => {
  const f = setup();
  f.deps.ledger.claim = () => {
    throw Error("ledger_full");
  };
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "unavailable" },
  ]);
  expect(f.record.filter((item: any) => item[0] === "bind")).toHaveLength(0);
  expect(f.invokes()).toBe(0);
});

it("does not dispatch when the dispatch marker cannot be written", async () => {
  const f = setup();
  f.deps.ledger.markDispatched = () => {
    throw Error("ledger_unavailable");
  };
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "unavailable"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
});

/**
 * Second review. Spec 0.65 section 7.6.2 condition 1: "Reusing a key with a
 * different canonical request in the same registered scope/session
 * incarnation is idempotency_conflict; the same key may only recover its
 * historical receipt after current authorization checks, never dispatch
 * again." Version 0.65 has no pre-dispatch-refusal exception, so an admitted
 * key is bound to its request durably at admission and its pre-dispatch
 * refusal is its historical receipt.
 */
it("binds an admitted key: a pre-dispatch refusal is its historical receipt", async () => {
  const f = setup();
  f.setDecision((r: any) => ({
    decision: "deny",
    approvalId: "approval-1",
    operationId: r.operationId,
    argumentsDigest: r.argumentsDigest,
  }));
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("not_dispatched", "approval_denied"));
  // Same request, fresh allow-once decision: the historical receipt stands.
  f.setDecision((r: any) => ({
    decision: "allow-once",
    approvalId: "approval-2",
    operationId: r.operationId,
    argumentsDigest: r.argumentsDigest,
  }));
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("not_dispatched", "approval_denied"));
  // A different request under the same key is a conflict, never a dispatch.
  f.options.params.arguments = { text: "DIFFERENT mutation" };
  await f.run();
  expect(f.reply()?.[1]).toEqual(
    receipt("not_dispatched", "idempotency_conflict"),
  );
  expect(f.invokes()).toBe(0);
});

it("keeps a transient pre-dispatch refusal as the key's receipt", async () => {
  const f = setup();
  const working = f.deps.actionClient;
  let reads = 0;
  f.deps.actionClient = async () => ({
    listFeedback: async () => {
      if (++reads === 2)
        throw new RecommendationClientError("deadline_exceeded");
      return working().then((client: any) => client.listFeedback());
    },
  });
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("not_dispatched", "timeout"));
  f.deps.actionClient = working;
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("not_dispatched", "timeout"));
  expect(f.invokes()).toBe(0);
});

it("recovers a key bound before a restart that never reached dispatch as not dispatched", async () => {
  const path = ledgerPath();
  const first = setup({ ledger: path });
  first.setDecision(() => new Promise(() => {}));
  void first.run();
  await new Promise((resolve) => setTimeout(resolve, 10));
  first.deps.ledger.close();
  const restarted = setup({ ledger: path });
  await restarted.run();
  expect(restarted.reply()).toEqual([
    true,
    receipt("not_dispatched", "unavailable"),
    undefined,
  ]);
  expect(restarted.invokes()).toBe(0);
  expect(first.invokes()).toBe(0);
});

it.each(["serverId", "toolName", "exposedName", "schemaHash", "sessionKey"])(
  "refuses an approval whose %s is not the bound target",
  async (field) => {
    const f = setup();
    f.setDecision((r: any) => ({
      decision: "allow-once",
      approvalId: "approval-1",
      operationId: r.operationId,
      argumentsDigest: r.argumentsDigest,
      [field]: "other",
    }));
    await f.run();
    expect(f.reply()).toEqual([
      true,
      receipt("not_dispatched", "approval_denied"),
      undefined,
    ]);
    expect(f.invokes()).toBe(0);
  },
);

// The gateway's canonical_json keeps 0.0; a JSON value in this process
// cannot. Such a schema is unavailable, never a claimed binding change.
it("refuses an integral-float schema it cannot rehash as unavailable, not binding_changed", async () => {
  const floatSchema = {
    type: "object",
    properties: { n: { type: "number", minimum: 0 } },
    required: ["n"],
    additionalProperties: false,
  };
  // relaymind.canonical.canonical_digest of the same schema with minimum 0.0.
  const gatewayHash =
    "52226622f5528168997a52a9cf3ac9a39dbbfdbf9674d14b7e0126b1b1a9ffb9";
  expect(canonicalSchemaHash(floatSchema)).toBe(
    "ae0ab8ab7bee0ce17a8d1e470c1c82b645052f20de229189afcabd03a4ef148e",
  );
  const f = setup();
  f.native.inputSchema = structuredClone(floatSchema);
  f.view.displayed_candidates[0].asset.identity.input_schema = floatSchema;
  f.view.displayed_candidates[0].asset.identity.schema_hash = gatewayHash;
  f.options.params.arguments = { n: 1 };
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "unavailable"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
});

function withPattern(f: any, pattern: string) {
  f.native.inputSchema.properties.text = { type: "string", pattern };
  f.view.displayed_candidates[0].asset.identity.input_schema =
    f.native.inputSchema;
  f.view.displayed_candidates[0].asset.identity.schema_hash =
    canonicalSchemaHash(f.native.inputSchema);
}
// Spec 0.65 7.6.2 condition 1: the 60-second deadline covers preparation, so a
// host schema pattern must not run unbounded synchronous backtracking.
it.each([
  "^(a+)+$",
  "^(a|aa)*$",
  "^a*a*b$",
  "a+b",
  "^(?=a)a$",
  "^(a)\\1$",
  "^\\p{L}+$",
  "^[a-z]{0,64}[a-z]{0,64}x$",
  "^" + "a".repeat(300) + "$",
])(
  "refuses the unprovably bounded pattern %s as unavailable",
  async (pattern) => {
    const f = setup();
    withPattern(f, pattern);
    f.options.params.arguments = { text: "a".repeat(30) + "b" };
    const started = performance.now();
    await f.run();
    expect(performance.now() - started).toBeLessThan(1000);
    expect(f.reply()).toEqual([
      true,
      receipt("not_dispatched", "unavailable"),
      undefined,
    ]);
    expect(f.invokes()).toBe(0);
  },
);

it.each([
  ["^[a-z0-9_-]+$", "hello_1", true],
  ["^\\d{4}-\\d{2}-\\d{2}$", "2026-11-01", true],
  ["^[a-z]+$", "Hello", false],
])("enforces the linear pattern %s", async (pattern, text, accepted) => {
  const f = setup();
  withPattern(f, pattern);
  f.options.params.arguments = { text };
  await f.run();
  expect(f.reply()?.[1]).toMatchObject(
    accepted
      ? { outcome: "completed" }
      : { outcome: "not_dispatched", code: "invalid_request" },
  );
});

it("refuses a pattern-bound argument longer than the bounded match length", async () => {
  const f = setup();
  withPattern(f, "^[a-z]+$");
  f.options.params.arguments = { text: "a".repeat(8193) };
  await f.run();
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "unavailable"),
    undefined,
  ]);
});

// Review 4435447985/4435447991 (RM16): a gateway transport failure is reported
// as unavailable or timeout from the closed list, never as an authority or
// choice refusal; only the gateway's own refusals map to those codes.
it.each([
  ["registration", "transport_error", null, "unavailable"],
  ["registration", "protocol_error", null, "unavailable"],
  ["registration", "overloaded", 503, "unavailable"],
  ["registration", "deadline_exceeded", null, "timeout"],
  ["registration", "user_action_required", 403, "unauthorized"],
  ["registration", "unauthenticated", 401, "unauthorized"],
  ["choice", "transport_error", null, "unavailable"],
  ["choice", "recommendation_unavailable", 503, "unavailable"],
  ["choice", "deadline_exceeded", null, "timeout"],
  ["choice", "recommendation_not_found", 404, "choice_unavailable"],
  ["choice", "recommendation_expired", 410, "choice_unavailable"],
])(
  "maps a %s gateway error %s (status %s) to %s",
  async (where, code, status, expected) => {
    const f = setup();
    const failure = new RecommendationClientError(
      code as string,
      status as any,
    );
    if (where === "registration")
      f.deps.actionClient = async () => ({
        listFeedback: async () => {
          throw failure;
        },
      });
    else
      f.deps.recommendationClient = async () => ({
        get: async () => {
          throw failure;
        },
      });
    await f.run();
    expect(f.reply()).toEqual([
      true,
      receipt("not_dispatched", expected as string),
      undefined,
    ]);
    expect(f.invokes()).toBe(0);
  },
);

it("maps a non-gateway failure while reading the choice to unavailable", async () => {
  const f = setup();
  f.deps.recommendationClient = async () => ({
    get: async () => {
      throw Error("socket hang up");
    },
  });
  await f.run();
  expect(f.reply()?.[1]).toEqual(receipt("not_dispatched", "unavailable"));
});

// Review 4435447955/4435447965: once the outcome is settled it is never
// settled again, so a failed response cannot corrupt the durable history.
it("settles a completed outcome once even when sending the response fails", async () => {
  const path = ledgerPath();
  const f = setup({ ledger: path });
  const respond = f.options.respond;
  let first = true;
  f.options.respond = (...args: unknown[]) => {
    if (first) {
      first = false;
      throw Error("connection closed");
    }
    return respond(...args);
  };
  await f.run().catch(() => {});
  expect(f.invokes()).toBe(1);
  // The completed outcome is never re-reported as indeterminate.
  expect(f.replies).toHaveLength(0);
  f.deps.ledger.close();
  const restarted = setup({ ledger: path });
  await restarted.run();
  expect(restarted.reply()).toEqual([
    true,
    receipt("completed", "ok", {
      nativeRunId: "native-run-1",
      nativeCallId: "native-call-1",
    }),
    undefined,
  ]);
  expect(restarted.invokes()).toBe(0);
});

/**
 * Review 4435447968. Spec 0.65 section 7.6.2 condition 1: "indeterminate
 * requires code=transport_unknown, timeout or cancelled after dispatch may
 * have begun; other codes are not_dispatched and prove no native dispatch
 * for this attempt", and "the same key may only recover its historical
 * receipt after current authorization checks, never dispatch again". A
 * same-key request arriving while the first attempt is still preparing waits
 * for that attempt and recovers its actual receipt; it never fabricates an
 * indeterminate outcome before dispatch.
 */
it("recovers the in-flight attempt's actual receipt for a concurrent same-key request", async () => {
  const f = setup();
  let decide: (value: unknown) => void = () => {};
  f.setDecision(
    (r: any) =>
      new Promise((resolve) => {
        decide = () =>
          resolve({
            decision: "deny",
            approvalId: "approval-1",
            operationId: r.operationId,
            argumentsDigest: r.argumentsDigest,
          });
      }),
  );
  const first = f.run();
  await new Promise((resolve) => setTimeout(resolve, 10));
  const second = f.handler(f.options);
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(f.replies).toHaveLength(0);
  decide(undefined);
  await Promise.all([first, second]);
  expect(f.replies).toEqual([
    [true, receipt("not_dispatched", "approval_denied"), undefined],
    [true, receipt("not_dispatched", "approval_denied"), undefined],
  ]);
  expect(f.invokes()).toBe(0);
});

it("refuses a concurrent same-key request with a different body as idempotency_conflict", async () => {
  const f = setup();
  f.setDecision(() => new Promise(() => {}));
  void f.run();
  await new Promise((resolve) => setTimeout(resolve, 10));
  f.options.params.arguments = { text: "changed" };
  await f.handler(f.options);
  expect(f.reply()).toEqual([
    true,
    receipt("not_dispatched", "idempotency_conflict"),
    undefined,
  ]);
  expect(f.invokes()).toBe(0);
});

// Spec 0.65 7.6.2 condition 1: a malformed request fails closed as
// invalid_request before identity resolution, never as unavailable or
// unauthorized.
it("refuses an explicit undefined property as invalid_request before any call", async () => {
  const f = setup();
  f.options.params.agentId = undefined;
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "invalid_request" },
  ]);
  expect(f.record).toHaveLength(0);
});

it("refuses an unpaired surrogate in a session field as invalid_request before any call", async () => {
  const f = setup();
  f.options.sessionAccessAuthority.target.sessionKey = "native-\ud800";
  f.options.params.sessionKey = "native-\ud800";
  await f.run();
  expect(f.reply()).toEqual([
    false,
    undefined,
    { code: "INVALID_REQUEST", message: "invalid_request" },
  ]);
  expect(f.record).toHaveLength(0);
});

it("refuses non-string session fields as invalid_request, not unauthorized", async () => {
  for (const [field, value] of [
    ["sessionKey", 7],
    ["expectedSessionId", null],
    ["expectedLifecycleRevision", 1],
    ["agentId", 3],
  ] as const) {
    const f = setup();
    f.options.params[field] = value;
    await f.run();
    expect(f.reply()).toEqual([
      false,
      undefined,
      { code: "INVALID_REQUEST", message: "invalid_request" },
    ]);
    expect(f.record).toHaveLength(0);
  }
});

it("checks registration once for a concurrent same-key request waiting on the in-flight attempt", async () => {
  const deny = (r: any) => ({
    decision: "deny",
    approvalId: "approval-1",
    operationId: r.operationId,
    argumentsDigest: r.argumentsDigest,
  });
  const registrations = (f: ReturnType<typeof setup>) =>
    f.record.filter((item: any) => item[0] === "registration").length;
  const single = setup();
  single.setDecision(deny);
  await single.run();
  const baseline = registrations(single);
  const f = setup();
  let decide: () => void = () => {};
  f.setDecision(
    (r: any) =>
      new Promise((resolve) => {
        decide = () => resolve(deny(r));
      }),
  );
  const first = f.run();
  await new Promise((resolve) => setTimeout(resolve, 10));
  const second = f.handler(f.options);
  await new Promise((resolve) => setTimeout(resolve, 10));
  decide();
  await Promise.all([first, second]);
  expect(f.replies).toHaveLength(2);
  expect(registrations(f)).toBe(baseline + 1);
});
