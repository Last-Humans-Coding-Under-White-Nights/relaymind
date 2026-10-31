// RM36/AC27: a real fetch abort cancels the create request's owned computation.
import { writeFileSync } from "node:fs";
import { createRecommendationClient } from "../../dist/recommendations.js";
const ac = new AbortController();
let calls = 0;
const client = createRecommendationClient({
  baseUrl: process.env.RELAYMIND_BASE_URL,
  applicationToken: async () => process.env.RELAYMIND_APP_TOKEN,
  fetch: async (...args) => {
    calls++;
    return fetch(...args);
  },
  monotonicNow: () => performance.now(),
  maxResponseBytes: 1048576,
});
const timer = setTimeout(() => ac.abort(), 300);
try {
  await client.create(
    {
      schema_version: 1,
      task_text: "hello greeting",
      requested_kinds: ["skill"],
      selection_scope: "discovery",
    },
    {
      signal: ac.signal,
      deadlineMs: performance.now() + 5000,
      idempotencyKey: "abort-create",
    },
  );
  throw Error("unexpected_success");
} catch (e) {
  writeFileSync(process.argv[2], JSON.stringify({ code: e.code, calls }));
} finally {
  clearTimeout(timer);
}
