// RM37/RM39; AC28/AC30: CONTROLLED host-shaped fixture over the real gateway, not person qualification.
import { writeFileSync } from "node:fs";
import { parsePluginConfig } from "../../dist/config.js";
import { nativeRecommendationDependencies } from "../../dist/recommendation-native.js";
import { createNativeActionHandler } from "../../dist/recommendation-native-actions.js";
import { decodeJson } from "../../dist/recommendation-json.js";
const exchanges = [];
const fetcher = globalThis.fetch;
globalThis.fetch = async (url, options) => {
  const result = await fetcher(url, options);
  exchanges.push({
    path: new URL(url).pathname,
    method: options.method,
    status: result.status,
  });
  return result;
};
const workspace = process.env.RELAYMIND_FIXTURE_WORKSPACE;
const config = parsePluginConfig({
  baseUrl: process.env.RELAYMIND_BASE_URL,
  recommendations: {
    controlUiOrigin: "https://fixture.test",
    applicationTokenEnv: "RELAYMIND_APP_TOKEN",
    bindings: [
      {
        profileId: "controlled-profile",
        authenticatedUserId: "fixture@example.test",
        agentId: "main",
        workspaceDir: workspace,
        userId: "u",
        workspaceId: "w",
        actionTokenEnv: "RELAYMIND_ACTION_TOKEN",
      },
    ],
  },
});
const hostConfig = {
  gateway: {
    roles: {
      default: "person",
      definitions: {
        person: {
          sessions: { others: "none" },
          agents: ["main"],
          scopes: ["operator.read", "operator.write"],
        },
      },
    },
  },
};
const api = {
  runtime: {
    version: "2026.10.7",
    agent: { resolveAgentWorkspaceDir: () => workspace },
  },
};
const handler = createNativeActionHandler(
  nativeRecommendationDependencies(api, config),
);
let current = true;
let serial = 0;
let last;
const options = {
  client: {
    authenticatedUserId: "fixture@example.test",
    preparedSessionProfile: { profileId: "controlled-profile" },
    presenceKey: "controlled-connection",
    internal: { authenticatedControlUi: true },
    browserOrigin: { origin: "https://fixture.test" },
    connect: {
      scopes: ["operator.read", "operator.write"],
      device: { id: "controlled-device" },
    },
    connectionSignal: new AbortController().signal,
  },
  sessionMutationAuthorization: {
    assertCurrent: () => {
      if (!current) throw Error("retired");
    },
  },
  sessionAccessAuthority: {
    target: {
      agentId: "main",
      sessionKey: "controlled-session",
      sessionId: "controlled-id",
      lifecycleRevision: "controlled-life",
    },
    assertCurrent: () => {
      if (!current) throw Error("retired");
    },
  },
  hasCurrentClientAuthority: () => current,
  context: { getRuntimeConfig: () => hostConfig },
  respond: (ok, payload, error) => {
    last = ok ? { ok, payload } : { ok, code: error.message };
  },
};
async function call(input, key) {
  serial++;
  last = undefined;
  await handler({
    ...options,
    req: { id: "controlled-request-" + serial },
    params: {
      sessionKey: "controlled-session",
      agentId: "main",
      expectedSessionId: "controlled-id",
      expectedLifecycleRevision: "controlled-life",
      input,
      idempotencyKey: key,
    },
  });
  return last;
}
function value(result) {
  if (!result.ok) throw Error(result.code);
  return decodeJson(result.payload.receipt_json);
}
try {
  const recommend = await call(
    {
      kind: "recommend",
      request: {
        schema_version: 1,
        task_text: "hello greeting",
        requested_kinds: ["skill"],
        selection_scope: "discovery",
      },
    },
    "create",
  );
  const view = value(recommend),
    asset = view.displayed_candidates[0].asset,
    id = view.recommendation_id;
  const choose = await call(
    {
      kind: "choose",
      recommendationId: id,
      request: {
        schema_version: 1,
        expected_version: String(view.state_version),
        choice: {
          kind: "asset",
          asset_id: asset.asset_id,
          content_hash: asset.content_hash,
        },
      },
    },
    "choose",
  );
  const choiceReplay = await call(
    {
      kind: "choose",
      recommendationId: id,
      request: {
        schema_version: 1,
        expected_version: String(view.state_version),
        choice: {
          kind: "asset",
          asset_id: asset.asset_id,
          content_hash: asset.content_hash,
        },
      },
    },
    "choose",
  );
  const feedback = await call(
    {
      kind: "feedback",
      recommendationId: id,
      request: {
        schema_version: 1,
        event_id: "controlled-correction",
        expected_feedback_revision: "0",
        feedback: {
          kind: "correction",
          asset_id: asset.asset_id,
          content_hash: asset.content_hash,
        },
      },
    },
    "correction",
  );
  const record = value(feedback);
  const inspect = await call(
    { kind: "listFeedback", page: { limit: 10 } },
    "inspect",
  );
  const deleted = await call(
    {
      kind: "deleteFeedback",
      feedbackId: record.feedback_id,
      expectedFeedbackRevision: String(record.feedback_revision),
    },
    "delete",
  );
  const reset = await call(
    {
      kind: "resetFeedback",
      expectedFeedbackRevision: String(value(deleted).feedback_revision),
    },
    "reset",
  );
  const before = exchanges.length;
  current = false;
  const retired = await call(
    {
      kind: "resetFeedback",
      expectedFeedbackRevision: String(value(reset).feedback_revision),
    },
    "retired",
  );
  const report = {
    fixture: "controlled-host-context-real-gateway",
    recommend: value(recommend),
    choose: value(choose),
    choiceReplay: value(choiceReplay),
    feedback: record,
    inspect: value(inspect),
    deleted: value(deleted),
    reset: value(reset),
    retired,
    retiredExchanges: exchanges.length - before,
    exchanges,
  };
  writeFileSync(
    process.argv[2],
    JSON.stringify(report, (_key, value) =>
      typeof value === "bigint" ? String(value) : value,
    ),
    { encoding: "utf8" },
  );
} catch {
  writeFileSync(
    process.argv[2],
    JSON.stringify({
      fixture: "controlled-host-context-real-gateway",
      failed: true,
      exchanges,
    }),
    { encoding: "utf8" },
  );
  process.exitCode = 1;
}
