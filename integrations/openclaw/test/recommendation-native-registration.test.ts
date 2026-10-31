// RM37/RM39; AC28/AC30: operator configuration never substitutes for qualification.
import { expect, it } from "vitest";
import { registerNativeRecommendations } from "../src/recommendation-native.js";
it("registers no production RPC from unbuilt sources or operator configuration alone", () => {
  const calls: unknown[] = [];
  const api: any = {
    runtime: { version: "2026.10.7" },
    registerGatewayMethod: (...args: unknown[]) => calls.push(args),
    logger: { warn: () => {} },
  };
  const configured: any = {
    baseUrl: "http://127.0.0.1:8080/v1",
    recommendations: {
      controlUiOrigin: "https://fixture.test",
      applicationTokenEnv: "FIXTURE_APP",
      bindings: [],
      skills: [],
    },
  };
  registerNativeRecommendations(api, configured);
  expect(calls).toHaveLength(0);
});
it("keeps the optional feature absent without changing provider registration", () => {
  const api: any = {
    registerGatewayMethod: () => {
      throw Error("unexpected registration");
    },
  };
  expect(() =>
    registerNativeRecommendations(api, {
      baseUrl: "http://127.0.0.1:8080/v1",
    } as any),
  ).not.toThrow();
});
