// RM39; AC30: exact selection/read fixtures are distinct from native instruction-use qualification.
import { createHash } from "node:crypto";
import { expect, it } from "vitest";
import { fixture } from "./recommendation-native-fixture.js";
import { createNativeSkillHandler } from "../src/recommendation-native-skill.js";
const content = "# Native fixture\nFollow the exact selected instruction.\n";
const hash = createHash("sha256").update(content).digest("hex");
const skillId = "4774d146-17bd-4ec9-8507-f528ccea51fa",
  revision = "b".repeat(64);
function setup() {
  const f = fixture();
  f.options.params = {
    sessionKey: "native-key",
    expectedSessionId: "native-id",
    expectedLifecycleRevision: "native-life",
    recommendationId: skillId,
    assetId: "skill:fixture",
    contentHash: hash,
    skillId,
    revision,
  };
  const view: any = {
    recommendation_id: skillId,
    task_id: "task",
    state: "chosen",
    choice_expires_at: "2030-01-01T00:00:00Z",
    selected_action: {
      recommendation_id: skillId,
      task_id: "task",
      scope: { user_id: f.binding.userId, workspace_id: f.binding.workspaceId },
      choice: { kind: "asset", asset_id: "skill:fixture", content_hash: hash },
    },
    displayed_candidates: [
      {
        asset: {
          asset_id: "skill:fixture",
          kind: "skill",
          content_hash: hash,
          source_version: null,
          stale: false,
        },
      },
    ],
  };
  const calls: unknown[] = [];
  const dispatch: any = async (method: string, params: unknown) => {
    calls.push([method, params]);
    return method === "skills.library.activate"
      ? {
          ok: true,
          payload: {
            sessionKey: "native-key",
            sessionActivation: "next-turn",
            selections: [{ skillId, revision }],
          },
        }
      : { ok: true, payload: { entry: { skillId, revision }, content } };
  };
  const deps: any = {
    bindings: [f.binding],
    controlUiOrigin: "https://fixture.test",
    resolveWorkspace: () => f.binding.workspaceDir,
    monotonicNow: () => 1,
    wallNow: () => Date.parse("2026-10-31T00:00:00Z"),
    skills: [
      {
        assetId: "skill:fixture",
        contentHash: hash,
        sourceVersion: null,
        skillId,
        revision,
      },
    ],
    recommendationClient: async () => ({ get: async () => view }),
    actionClient: async () => ({
      listFeedback: async () => ({
        schema_version: 1,
        records: [],
        feedback_revision: 0n,
        next_after_id: null,
      }),
    }),
    dispatch,
  };
  return {
    ...f,
    view,
    calls,
    deps,
    handler: () => createNativeSkillHandler(deps),
  };
}
it("activates and reads only the committed chosen asset's exact native revision without claiming instruction use", async () => {
  const f = setup();
  await f.handler()(f.options);
  expect(f.calls).toEqual([
    [
      "skills.library.activate",
      { sessionKey: "native-key", action: "attach", skillId, revision },
    ],
    ["skills.library.read", { sessionKey: "native-key", skillId, revision }],
  ]);
  expect(f.replies[0]).toEqual([
    true,
    {
      skillId,
      revision,
      sessionActivation: "next-turn",
      instructions: "not_observed",
    },
    undefined,
  ]);
});
it.each([
  "not-chosen",
  "scope",
  "hash",
  "source",
  "native-revision",
  "expired",
  "retired",
])("blocks %s before any native activation", async (kind) => {
  const f = setup();
  if (kind === "not-chosen") f.view.state = "ready";
  if (kind === "scope") f.view.selected_action.scope.user_id = "foreign";
  if (kind === "hash") f.options.params.contentHash = "c".repeat(64);
  if (kind === "source")
    f.view.displayed_candidates[0].asset.source_version = "changed";
  if (kind === "native-revision") f.options.params.revision = "c".repeat(64);
  if (kind === "expired") f.view.choice_expires_at = "2020-01-01T00:00:00Z";
  if (kind === "retired") f.retire();
  await f.handler()(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});
it("retains partial activation when exact native read mismatches the original bytes", async () => {
  const f = setup();
  f.deps.dispatch = async (method: string) => {
    f.calls.push(method);
    return method === "skills.library.activate"
      ? {
          ok: true,
          payload: {
            sessionKey: "native-key",
            sessionActivation: "next-turn",
            selections: [{ skillId, revision }],
          },
        }
      : {
          ok: true,
          payload: { entry: { skillId, revision }, content: "different bytes" },
        };
  };
  await f.handler()(f.options);
  expect(f.calls).toHaveLength(2);
  expect(f.replies[0]?.[0]).toBe(false);
  expect(f.replies[0]?.[2]).toMatchObject({
    details: { selectionMayHaveChanged: true, skillId, revision },
  });
});

it("denies revoked gateway action registration before any native skill dispatch", async () => {
  const f = setup();
  f.deps.actionClient = async () => ({
    listFeedback: async () => {
      throw Error("user_action_required");
    },
  });
  await f.handler()(f.options);
  expect(f.calls).toHaveLength(0);
  expect(f.replies[0]?.[0]).toBe(false);
});
