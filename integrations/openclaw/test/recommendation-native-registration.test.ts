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

// Spec 0.65 7.6.1/7.6.2: each RPC is registered only for its own operation;
// relaymind.mcp stays reserved without a reviewed mcp_use entry and exact route.
import { registerQualifiedNativeMethods } from "../src/recommendation-native.js";
const SCOPE = {
  route: "fixture.exact-use",
  transport: "stdio",
  toolScope: "independent-tool",
  maxApprovalWaitMs: 30000,
};
let opened = 0;
let closed = 0;
let hooks: Array<[string, () => void]> = [];
function registered(
  operations: string[],
  route?: object,
  ledger: boolean = true,
) {
  const names: string[] = [];
  hooks = [];
  const api: any = {
    registerGatewayMethod: (name: string) => names.push(name),
    logger: { warn: () => {} },
    on: (name: string, handler: () => void) => hooks.push([name, handler]),
  };
  const dependencies: any = () => ({ bindings: [], skills: [] });
  registerQualifiedNativeMethods(
    api,
    dependencies,
    (operation) =>
      operations.includes(operation)
        ? ({ mcp: operations.includes("mcp_use") ? SCOPE : undefined } as any)
        : undefined,
    {
      mcpRoute: route as any,
      openLedger: ledger
        ? () => {
            opened++;
            return { close: () => closed++ } as any;
          }
        : undefined,
    },
  );
  return names;
}
const route = {
  route: SCOPE.route,
  transport: SCOPE.transport,
  toolScope: SCOPE.toolScope,
};
it("registers relaymind.mcp only for a qualified mcp_use entry and its exact route", () => {
  expect(
    registered(["direct_actions", "exact_skill_selection"], route),
  ).toEqual(["relaymind.recommendations", "relaymind.skill"]);
  expect(registered(["direct_actions", "mcp_use"])).toEqual([
    "relaymind.recommendations",
  ]);
  expect(
    registered(["direct_actions", "mcp_use"], { ...route, transport: "http" }),
  ).toEqual(["relaymind.recommendations"]);
  expect(registered(["direct_actions", "mcp_use"], route)).toEqual([
    "relaymind.recommendations",
    "relaymind.mcp",
  ]);
  // Without durable dispatch history the RPC stays reserved.
  expect(registered(["direct_actions", "mcp_use"], route, false)).toEqual([
    "relaymind.recommendations",
  ]);
  // skill_loaded_use defines no RPC; the fence enforces it.
  expect(registered(["direct_actions", "skill_loaded_use"])).toEqual([
    "relaymind.recommendations",
  ]);
});

// Review 4435448004: each operation registers only when its own entry
// qualifies; an unqualified direct_actions entry does not hide the others.
it("registers each native method independently of direct_actions", () => {
  expect(registered(["mcp_use", "exact_skill_selection"], route)).toEqual([
    "relaymind.skill",
    "relaymind.mcp",
  ]);
  expect(registered(["mcp_use"], route)).toEqual(["relaymind.mcp"]);
  expect(registered([], route)).toEqual([]);
});

// Review 4435447999: the registration opens the ledger only for a registered
// relaymind.mcp and closes it when the gateway stops.
it("opens the ledger only for relaymind.mcp and closes it on gateway stop", () => {
  opened = closed = 0;
  registered(["direct_actions"], route);
  expect(opened).toBe(0);
  registered(["mcp_use"], route);
  expect(opened).toBe(1);
  expect(hooks.map(([name]) => name)).toEqual(["gateway_stop"]);
  hooks[0]![1]();
  expect(closed).toBe(1);
});
