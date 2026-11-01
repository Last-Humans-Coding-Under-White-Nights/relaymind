// RM37/RM39; AC28/AC30: controlled native-handler fixtures are not person qualification.
import { expect, it } from "vitest";
import type { UserActionClient } from "../src/recommendations.js";
import { createNativeActionHandler } from "../src/recommendation-native-actions.js";

import { fixture } from "./recommendation-native-fixture.js";

it("maps only the actual prepared person and native workspace to a registered action", async () => {
  const f = fixture();
  await f.handler(f.options);
  expect(f.calls).toHaveLength(1);
  expect(f.calls[0]).toMatchObject([
    0n,
    { idempotencyKey: "explicit-user-request" },
  ]);
  expect(f.replies[0]).toEqual([
    true,
    {
      label: "privacy",
      receipt_json:
        '{"schema_version":1,"feedback_revision":1,"removed_count":0}',
    },
    undefined,
  ]);
});

it.each([
  "application",
  "model",
  "profile",
  "account",
  "agent",
  "workspace",
  "session",
  "lifecycle",
  "authority",
  "extra",
])("refuses %s with zero action calls", async (kind) => {
  const f = fixture();
  if (kind === "application") delete f.options.client.preparedSessionProfile;
  if (kind === "model") f.options.client.internal = { agentToolCaller: {} };
  if (kind === "profile")
    f.options.client.preparedSessionProfile.profileId = "foreign";
  if (kind === "account")
    f.options.client.authenticatedUserId = "foreign@example.test";
  if (kind === "agent")
    f.options.sessionAccessAuthority.target.agentId = "foreign";
  if (kind === "workspace") f.changeWorkspace();
  if (kind === "session") f.options.params.expectedSessionId = "stale";
  if (kind === "lifecycle")
    f.options.params.expectedLifecycleRevision = "stale";
  if (kind === "authority") f.retire();
  if (kind === "extra") f.options.params.profileId = "invented";
  await f.handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

it("does not dispatch when custody changes while credentials are awaited", async () => {
  const f = fixture();
  const handler = createNativeActionHandler({
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => f.binding.workspaceDir,
    actionClient: async () => {
      f.retire();
      return {
        resetFeedback: async () => {
          f.calls.push("wrong");
        },
      } as unknown as UserActionClient;
    },
    monotonicNow: () => 1,
  });
  await handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

it.each(["-1", "01", "9223372036854775808", 1, true])(
  "rejects malformed lossless revision %s",
  async (value) => {
    const f = fixture();
    f.options.params.input.expectedFeedbackRevision = value;
    await f.handler(f.options);
    expect(f.calls).toHaveLength(0);
    expect(f.replies[0]?.[0]).toBe(false);
  },
);

it("rechecks native custody inside the real transport after credentials resolve and before fetch", async () => {
  const f = fixture();
  const { createUserActionClient } = await import("../src/recommendations.js");
  const handler = createNativeActionHandler({
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => f.binding.workspaceDir,
    monotonicNow: () => 1,
    actionClient: async () =>
      createUserActionClient({
        baseUrl: "http://127.0.0.1:8080/v1",
        monotonicNow: () => 1,
        maxResponseBytes: 4096,
        applicationToken: async () => {
          f.retire();
          return "fixture-application-token";
        },
        actionToken: async () => "fixture-action-token",
        fetch: async () => {
          f.calls.push("fetch");
          return new Response(
            '{"schema_version":1,"feedback_revision":1,"removed_count":0}',
            { status: 200, headers: { "content-type": "application/json" } },
          );
        },
      }),
  });
  await handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

it("returns the existing validated recommendation receipt losslessly for explicit native UI choice", async () => {
  const f = fixture();
  const { readFileSync } = await import("node:fs");
  const { decodeRecommendationView, encodeJson } = await import(
    "../src/recommendation-contract.js"
  );
  const view = decodeRecommendationView(
    readFileSync(
      new URL("./fixtures/recommendation-view.v1.json", import.meta.url),
      "utf8",
    ),
  );
  f.options.params.agentId = "main";
  f.options.params.input = {
    kind: "recommend",
    request: {
      schema_version: 1,
      task_text: "hello",
      requested_kinds: ["skill"],
      selection_scope: "discovery",
    },
  };
  const handler = createNativeActionHandler({
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => f.binding.workspaceDir,
    monotonicNow: () => 1,
    actionClient: async () => {
      throw Error("not an action");
    },
    recommendationClient: async () => ({ create: async () => view }) as any,
  });
  await handler(f.options);
  expect(f.replies[0]?.[0]).toBe(true);
  expect(f.replies[0]?.[1]).toMatchObject({
    label: "recommendation",
    receipt_json: encodeJson(view),
  });
});

it("refuses a role that broadens its agent set beyond the configured native mappings", async () => {
  const f = fixture();
  f.options.context.getRuntimeConfig = () => ({
    gateway: {
      roles: {
        default: "person",
        definitions: {
          person: {
            sessions: { others: "none" },
            scopes: ["operator.read", "operator.write"],
            agents: ["main", "foreign-agent"],
          },
        },
      },
    },
  });
  await f.handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

it("refuses a native context without its socket-bound lifetime signal", async () => {
  const f = fixture();
  f.options.client.connectionSignal = undefined;
  await f.handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

it("selects the mapping for the actual native workspace rather than an earlier workspace", async () => {
  const f = fixture();
  const second = {
    ...f.binding,
    workspaceDir: "/fixture/second-workspace",
    userId: "second-user",
  };
  const seen: unknown[] = [];
  const handler = createNativeActionHandler({
    bindings: [f.binding, second],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => second.workspaceDir,
    monotonicNow: () => 1,
    actionClient: async (binding) => {
      seen.push(binding);
      return {
        resetFeedback: async () => ({
          schema_version: 1,
          feedback_revision: 1n,
          removed_count: 0,
        }),
      } as any;
    },
  });
  await handler(f.options);
  expect(f.replies[0]?.[0]).toBe(true);
  expect(seen).toEqual([second]);
});

// Spec 0.63 clarification of 7.6.1: only isolation conditions are universal.
it.each([
  [
    "a read-only personal role beside the writer role",
    {
      person: {
        sessions: { others: "none" },
        scopes: ["operator.read", "operator.write"],
        agents: ["main"],
      },
      viewer: {
        sessions: { others: "none" },
        scopes: ["operator.read"],
        agents: ["main"],
      },
    },
  ],
  [
    "writer roles for disjoint registered agents",
    {
      person: {
        sessions: { others: "none" },
        scopes: ["operator.read", "operator.write"],
        agents: ["main"],
      },
      research: {
        sessions: { others: "none" },
        scopes: ["operator.read", "operator.write"],
        agents: ["research"],
      },
    },
  ],
])("accepts %s", async (_name, definitions) => {
  const f = fixture();
  const research = {
    ...f.binding,
    agentId: "research",
    workspaceDir: "/fixture/research",
    userId: "research-user",
    actionTokenEnv: "FIXTURE_RESEARCH",
  };
  const handler = createNativeActionHandler({
    ...f.dependencies,
    bindings: [f.binding, research],
  });
  f.options.context.getRuntimeConfig = () => ({
    gateway: { roles: { default: "person", definitions } },
  });
  await handler(f.options);
  expect(f.calls).toHaveLength(1);
  expect(f.replies[0]?.[0]).toBe(true);
});

it.each([
  ["another person's sessions", { sessions: { others: "all" } }],
  ["admin scope", { scopes: ["operator.read", "operator.admin"] }],
  ["an unregistered agent", { agents: ["main", "foreign-agent"] }],
])("still refuses any role allowing %s", async (_name, change) => {
  const f = fixture();
  f.options.context.getRuntimeConfig = () => ({
    gateway: {
      roles: {
        default: "person",
        definitions: {
          person: {
            sessions: { others: "none" },
            scopes: ["operator.read", "operator.write"],
            agents: ["main"],
          },
          viewer: {
            sessions: { others: "none" },
            scopes: ["operator.read"],
            agents: ["main"],
            ...change,
          },
        },
      },
    },
  });
  await f.handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});

// Spec 0.65 section 14.5: every section 7.6.1–7.6.2 native request is ≤512 KiB.
it("refuses a direct action request over the 512 KiB native row without any action", async () => {
  const f = fixture();
  f.options.params.idempotencyKey = "k";
  f.options.req = { id: "rpc-request", padding: "x".repeat(512 * 1024) };
  await f.handler(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});
