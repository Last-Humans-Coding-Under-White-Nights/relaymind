// RM08–RM12, RM16, RM39; AC35 (spec 0.65 section 7.6.2, section 14.5 native
// request row): the conditional direct MCP use path. Controlled host-shaped
// doubles only; a fixture route and registry are never qualification evidence.
import { createHash } from "node:crypto";
import { expect, it } from "vitest";
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

function setup(options: { qualified?: boolean; route?: boolean } = {}) {
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
      return typeof decision === "function"
        ? decision(request, control)
        : decision;
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
          throw Error("user_action_required");
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
      throw Error("user_action_required");
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
