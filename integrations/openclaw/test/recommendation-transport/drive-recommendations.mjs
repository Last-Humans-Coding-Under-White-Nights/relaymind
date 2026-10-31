// RM36/RM37/RM39; AC27/AC28/AC30 fixture driver. No production host registration.
import { writeFileSync } from "node:fs";
import {
  createRecommendationClient,
  createUserActionClient,
} from "../../dist/recommendations.js";
import { createRecommendationController } from "../../dist/recommendation-controller.js";
import { createFixtureActionSession } from "../../dist/recommendation-host.js";
const output = process.argv[2];
const exchanges = [];
const fetcher = async (url, init) => {
  const response = await fetch(url, init);
  exchanges.push({
    path: new URL(url).pathname,
    query: new URL(url).search,
    method: init.method,
    action: new Headers(init.headers).has("X-RelayMind-User-Action-Token"),
    status: response.status,
  });
  return response;
};
const config = {
  baseUrl: process.env.RELAYMIND_BASE_URL,
  applicationToken: async () => process.env.RELAYMIND_APP_TOKEN,
  fetch: fetcher,
  monotonicNow: () => performance.now(),
  maxResponseBytes: 1048576,
};
const client = createRecommendationClient(config);
const actions = createUserActionClient({
  ...config,
  actionToken: async () => process.env.RELAYMIND_ACTION_TOKEN,
});
const read = () => ({
  signal: new AbortController().signal,
  deadlineMs: performance.now() + 10000,
});
const write = (key) => ({ ...read(), idempotencyKey: key });
const request = {
  schema_version: 1,
  task_text: "hello greeting",
  requested_kinds: ["skill"],
  selection_scope: "discovery",
};
const capture = async (fn) => {
  try {
    return await fn();
  } catch (e) {
    return {
      code: e.code,
      status: e.status,
      recommendationId: e.recommendationId,
    };
  }
};
try {
  const ready = await client.readiness(read());
  const created = await client.create(request, write("create"));
  const id = created.recommendation_id;
  const replay = await client.create(request, write("create"));
  const get = await client.get(id, read());
  const events = await client.events(id, { afterSeq: 0n, limit: 100 }, read());
  const before = exchanges.length;
  const deny = await createRecommendationController({ client }).act(
    { kind: "resetFeedback", expectedFeedbackRevision: 0n },
    { actor: "user" },
    write("forged"),
  );
  const denyRequests = exchanges.length - before;
  const ordinaryAction = await capture(() =>
    createUserActionClient({
      ...config,
      actionToken: async () => "unregistered-fixture",
    }).choose(
      id,
      {
        schema_version: 1,
        expected_version: created.state_version,
        choice: { kind: "none" },
      },
      write("unregistered"),
    ),
  );
  const stale = await capture(() =>
    actions.choose(
      id,
      { schema_version: 1, expected_version: 0n, choice: { kind: "none" } },
      write("stale"),
    ),
  );
  const session = createFixtureActionSession(actions);
  const controller = createRecommendationController({
    client,
    hostBoundary: { authorizeDirectInput: async () => session },
  });
  const choice = {
    schema_version: 1,
    expected_version: created.state_version,
    choice: { kind: "none" },
  };
  const chosen = await controller.act(
    { kind: "choose", recommendationId: id, request: choice },
    { fixture: true },
    write("choice"),
  );
  const choiceReplay = await actions.choose(id, choice, write("choice"));
  const conflict = await capture(() =>
    actions.choose(
      id,
      { ...choice, choice: { kind: "cancel" } },
      write("choice"),
    ),
  );
  const fb = {
    schema_version: 1,
    event_id: "fixture-feedback",
    expected_feedback_revision: 0n,
    feedback: { kind: "usefulness", value: "useful" },
  };
  const feedback = await actions.feedback(id, fb, write("feedback"));
  const feedbackReplay = await actions.feedback(id, fb, write("feedback"));
  const page = await actions.listFeedback({ limit: 10 }, read());
  const deleted = await actions.deleteFeedback(
    feedback.feedback_id,
    feedback.feedback_revision,
    write("delete"),
  );
  const deleteReplay = await actions.deleteFeedback(
    feedback.feedback_id,
    feedback.feedback_revision,
    write("delete"),
  );
  const reset = await actions.resetFeedback(
    deleted.feedback_revision,
    write("reset"),
  );
  const current = await client.get(id, read());
  const finalEvents = await client.events(
    id,
    { afterSeq: 0n, limit: 100 },
    read(),
  );
  const runtime = await capture(() =>
    client.create({ ...request, selection_scope: "runtime" }, write("runtime")),
  );
  const target = created.result.final_candidates[0];
  const correction = await controller.act(
    {
      kind: "feedback",
      recommendationId: id,
      request: {
        schema_version: 1,
        event_id: "correction",
        expected_feedback_revision: 3n,
        feedback: {
          kind: "correction",
          asset_id: target.asset_id,
          content_hash: target.content_hash,
        },
      },
    },
    { fixture: true },
    write("correction"),
  );
  const rejection = await actions.feedback(
    id,
    {
      schema_version: 1,
      event_id: "rejection",
      expected_feedback_revision: 4n,
      feedback: {
        kind: "rejection",
        asset_id: target.asset_id,
        content_hash: target.content_hash,
      },
    },
    write("rejection"),
  );
  const firstPage = await controller.act(
    { kind: "listFeedback", page: { limit: 1 } },
    { fixture: true },
    write("inspect"),
  );
  const nextPage = await actions.listFeedback(
    { limit: 1, afterId: firstPage.receipt.next_after_id },
    read(),
  );
  const result = {
    correction,
    rejection,
    firstPage,
    nextPage,
    ready,
    created,
    replay,
    get,
    events,
    deny,
    denyRequests,
    ordinaryAction,
    stale,
    chosen,
    choiceReplay,
    conflict,
    feedback,
    feedbackReplay,
    page,
    deleted,
    deleteReplay,
    reset,
    current,
    finalEvents,
    runtime,
    exchanges,
  };
  writeFileSync(
    output,
    JSON.stringify(result, (_k, v) =>
      typeof v === "bigint" ? v.toString() : v,
    ),
  );
} catch (e) {
  writeFileSync(
    output,
    JSON.stringify({
      failure: {
        code: e.code,
        status: e.status,
        recommendationId: e.recommendationId,
      },
      exchanges,
    }),
  );
  process.exitCode = 1;
}
