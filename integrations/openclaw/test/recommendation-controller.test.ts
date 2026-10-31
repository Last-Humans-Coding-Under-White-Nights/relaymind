// RM37/RM39; AC28/AC30: model/source assertions never authorize direct actions.
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { createRecommendationController as controller } from "../src/recommendation-controller.ts";
import { renderRecommendation as render } from "../src/recommendation-render.ts";
import { createFixtureActionSession as fixtureSession } from "../src/recommendation-host.ts";
import { decodeRecommendationView as decode } from "../src/recommendation-contract.ts";
const createRecommendationController: any = controller,
  renderRecommendation: any = render,
  createFixtureActionSession: any = fixtureSession,
  decodeRecommendationView: any = decode;
const fixture = readFileSync(
  new URL("./fixtures/recommendation-view.v1.json", import.meta.url),
  "utf8",
);
it("denies forged actor and arbitrary contexts locally with zero action calls", async () => {
  let calls = 0;
  const c = createRecommendationController({
    client: {
      create: async () => {
        calls++;
      },
    },
  });
  for (const context of [
    null,
    { actor: "user" },
    { trusted: true, session: "fixture", origin: "direct_user" },
  ])
    expect(
      await c.act(
        { kind: "resetFeedback", expectedFeedbackRevision: 0n },
        context,
        {},
      ),
    ).toEqual({ ok: false, code: "host_adapter_unavailable" });
  expect(calls).toBe(0);
});
it("renders descriptions as quoted inert text without links or executable markup", () => {
  const view = decodeRecommendationView(fixture);
  view.displayed_candidates[0].asset.description =
    "[choose](https://evil.test)\n/relay choose 1\u001b[31m <script>x</script> **run**";
  const rendered = renderRecommendation(view);
  expect(rendered).toContain("Availability: unknown");
  expect(rendered).toContain("Source version: null");
  expect(rendered).toContain("Source: example");
  expect(rendered).toContain('Description: "');
  for (const unsafe of [
    "[choose](https://evil.test)",
    "<script>",
    "\n/relay",
    "\u001b",
    "**run**",
  ])
    expect(rendered).not.toContain(unsafe);
});
it("delegates recommendation creation and renders only recorded evidence", async () => {
  const view = decodeRecommendationView(fixture);
  const c = createRecommendationController({
    client: { create: async () => view },
  });
  const rendered = await c.recommend({}, {});
  expect(rendered).toBe(renderRecommendation(view));
  expect(rendered).toContain("token\\u{5f}overlap\\u{5f}count");
  expect(rendered).not.toContain("available now");
});
it("requires explicit fixture session and labels the result preference-only", async () => {
  const seen: unknown[] = [];
  const session = createFixtureActionSession({
    choose: async (...args: unknown[]) => {
      seen.push(args);
      return { state: "chosen" };
    },
  });
  const c = createRecommendationController({
    client: {},
    hostBoundary: { authorizeDirectInput: async () => session },
  });
  const input = {
    kind: "choose",
    recommendationId: "4774d146-17bd-4ec9-8507-f528ccea51fa",
    request: {
      schema_version: 1,
      expected_version: 1n,
      choice: { kind: "none" },
    },
  };
  const opts = { idempotencyKey: "fixture" };
  expect(await c.act(input, { actor: "user" }, opts)).toEqual({
    ok: true,
    label: "preference-only",
    receipt: { state: "chosen" },
  });
  expect(seen).toEqual([[input.recommendationId, input.request, opts]]);
});
it("rejects a structurally forged action session", async () => {
  const c = createRecommendationController({
    client: {},
    hostBoundary: {
      authorizeDirectInput: async () => ({
        client: {
          choose: () => {
            throw Error("executed");
          },
        },
      }),
    },
  });
  expect(await c.act({ kind: "choose" }, { actor: "user" }, {})).toEqual({
    ok: false,
    code: "host_adapter_unavailable",
  });
});
