// RM39/AC35: candidate App discovery cannot invent a view or mutate a tool.
import {expect, test} from "vitest";
// @ts-expect-error test-only JavaScript observer
import {observeDispatch} from "./probe.mjs";

test("records authenticated inventory and an honest missing-view refusal", async () => {
  const calls: Array<{method: string; params: unknown}> = [];
  const events: Array<Record<string, unknown>> = [];
  const inventory = {ok: true, payload: {tools: [{name: "alpha-one__append"}]}};
  const dispatch = async (method: string, params: unknown) => {
    calls.push({method, params});
    return method === "tools.effective" ? inventory : {ok: false, error: {code: "INVALID_REQUEST", message: "viewId required"}};
  };
  const result = await observeDispatch(dispatch, (event: Record<string, unknown>) => events.push(event))(
    "tools.effective", {sessionKey: "s"}, {timeoutMs: 3000},
  );
  expect(result).toEqual(inventory);
  expect(calls).toEqual([
    {method: "tools.effective", params: {sessionKey: "s"}},
    {method: "mcp.app.listTools", params: {}},
  ]);
  expect(events).toHaveLength(2);
  expect(events).toEqual([
    {method: "tools.effective", status: "ok", tool_names: ["alpha-one__append"]},
    {method: "mcp.app.listTools", status: "refused", tool_names: []},
  ]);
});
