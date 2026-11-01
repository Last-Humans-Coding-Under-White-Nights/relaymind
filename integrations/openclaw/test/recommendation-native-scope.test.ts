// Spec 0.63 sections 7.6/7.6.1 (RM37/RM39; AC28/AC30): every gateway request a
// native binding makes carries that binding's scope, so a shared base URL or a
// credential registered for another scope fails closed at the gateway.
import { afterEach, expect, it, vi } from "vitest";
import { nativeRecommendationDependencies } from "../src/recommendation-native.ts";
import { parseNativeRecommendationConfig } from "../src/recommendation-native-config.ts";

const ID = "4774d146-17bd-4ec9-8507-f528ccea51fa";
const binding = (n: string) => ({
  profileId: "profile-" + n,
  authenticatedUserId: n + "@example.test",
  agentId: "main",
  workspaceDir: "/fixture/" + n,
  userId: "user-" + n,
  workspaceId: "workspace-" + n,
  actionTokenEnv: "FIXTURE_ACTION_" + n.toUpperCase(),
});
const mismatch = () =>
  new Response(
    JSON.stringify({
      error: {
        code: "scope_mismatch",
        message: "the expected scope is not the scope this gateway serves",
        run_id: null,
        recommendation_id: null,
      },
    }),
    { status: 403, headers: { "Content-Type": "application/json" } },
  );
const options = () => ({
  signal: new AbortController().signal,
  deadlineMs: performance.now() + 5000,
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function dependencies() {
  vi.stubEnv("FIXTURE_APPLICATION", "application-token-value");
  vi.stubEnv("FIXTURE_ACTION_A", "action-token-a");
  vi.stubEnv("FIXTURE_ACTION_B", "action-token-b");
  const recommendations = parseNativeRecommendationConfig({
    controlUiOrigin: "https://fixture.test",
    applicationTokenEnv: "FIXTURE_APPLICATION",
    bindings: [binding("a"), binding("b")],
  });
  const api: any = {
    runtime: { agent: { resolveAgentWorkspaceDir: () => "/x" } },
  };
  const deps = nativeRecommendationDependencies(api, {
    baseUrl: "http://127.0.0.1:8080/v1",
    recommendations,
  } as any);
  return { deps, bindings: recommendations.bindings };
}

it("binds recommendation and action clients to each binding's own scope", async () => {
  const seen: Headers[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: unknown, init: RequestInit) => {
      seen.push(new Headers(init.headers));
      return mismatch();
    }),
  );
  const { deps, bindings } = dependencies();
  for (const b of bindings) {
    const recommendations = await deps.recommendationClient(b);
    await expect(recommendations.get(ID, options())).rejects.toMatchObject({
      code: "scope_mismatch",
    });
    const actions = await deps.actionClient(b);
    await expect(
      actions.listFeedback({ limit: 1 }, options()),
    ).rejects.toMatchObject({
      code: "scope_mismatch",
    });
  }
  expect(
    seen.map((h) => [
      h.get("X-RelayMind-Expected-User"),
      h.get("X-RelayMind-Expected-Workspace"),
    ]),
  ).toEqual([
    ["user-a", "workspace-a"],
    ["user-a", "workspace-a"],
    ["user-b", "workspace-b"],
    ["user-b", "workspace-b"],
  ]);
});

// Review 4435447975 (spec 0.65 7.6.2): the excluded approval wait extends the
// deadline every reader sees, including the deadlineMs field.
import { prepareNativeAuthority } from "../src/recommendation-native-authority.js";
import { fixture as nativeFixture } from "./recommendation-native-fixture.js";
it("reports the resumed deadline through deadlineMs and deadline()", () => {
  const f = nativeFixture();
  let now = 100;
  const authority = prepareNativeAuthority(
    f.options,
    { ...f.dependencies, monotonicNow: () => now },
    60000,
  );
  try {
    expect(authority.deadlineMs).toBe(60100);
    authority.suspendDeadline();
    now += 25000;
    authority.resumeDeadline();
    expect(authority.deadline()).toBe(85100);
    expect(authority.deadlineMs).toBe(85100);
  } finally {
    authority.close();
  }
});
