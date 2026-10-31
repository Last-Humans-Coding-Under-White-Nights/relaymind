// RM39; AC30: exact native selection/read, never a claim of instruction load or task success.
import { createHash } from "node:crypto";
import type { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";
import { object, identifier } from "./recommendation-contract.ts";
import type {
  RecommendationClient,
  UserActionClient,
  ReadOptions,
} from "./recommendations.ts";
import { prepareNativeAuthority } from "./recommendation-native-authority.ts";
import type {
  NativeActionOptions,
  NativeActionBinding,
  NativeScopeDependencies,
} from "./recommendation-native-authority.ts";
import type { NativeSkillBinding } from "./recommendation-native-config.ts";
interface Dependencies extends NativeScopeDependencies {
  skills: readonly NativeSkillBinding[];
  recommendationClient: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<RecommendationClient>;
  actionClient: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<UserActionClient>;
  wallNow: () => number;
  dispatch?: typeof dispatchGatewayMethod;
}
export function createNativeSkillHandler(dependencies: Dependencies) {
  return async (options: NativeActionOptions): Promise<void> => {
    let authority: ReturnType<typeof prepareNativeAuthority> | undefined;
    let selectionMayHaveChanged = false;
    let selected: NativeSkillBinding | undefined;
    try {
      object(
        options.params,
        [
          "sessionKey",
          "expectedSessionId",
          "expectedLifecycleRevision",
          "recommendationId",
          "assetId",
          "contentHash",
          "skillId",
          "revision",
        ],
        ["agentId"],
      );
      authority = prepareNativeAuthority(options, dependencies);
      const { binding, signal, deadlineMs, assertCurrent: current } = authority;
      const { recommendationId, assetId, contentHash, skillId, revision } =
        options.params;
      identifier(recommendationId);
      identifier(skillId);
      if (
        typeof assetId !== "string" ||
        typeof contentHash !== "string" ||
        !/^[a-f0-9]{64}$/.test(contentHash) ||
        typeof revision !== "string" ||
        !/^[a-f0-9]{64}$/.test(revision)
      )
        throw Error();
      const read: ReadOptions = { signal, deadlineMs, assertCurrent: current };
      const actions = await dependencies.actionClient(binding, read);
      current();
      const registeredCurrent = async () => {
        // The existing authenticated read rechecks the current registration.
        // Its private records are discarded and never become model context.
        await actions.listFeedback({ limit: 1 }, read);
        current();
      };
      await registeredCurrent();
      const client = await dependencies.recommendationClient(binding, read);
      current();
      const view = await client.get(recommendationId, read);
      current();
      const action = view.selected_action;
      const expires = Date.parse(view.choice_expires_at ?? "");
      if (
        view.recommendation_id !== recommendationId ||
        view.state !== "chosen" ||
        !action ||
        action.recommendation_id !== recommendationId ||
        action.task_id !== view.task_id ||
        action.scope.user_id !== binding.userId ||
        action.scope.workspace_id !== binding.workspaceId ||
        action.choice.kind !== "asset" ||
        action.choice.asset_id !== assetId ||
        action.choice.content_hash !== contentHash ||
        !Number.isFinite(expires) ||
        dependencies.wallNow() >= expires
      )
        throw Error();
      const offered = view.displayed_candidates.find(
        (item) => item.asset.asset_id === assetId,
      )?.asset;
      if (
        !offered ||
        offered.kind !== "skill" ||
        offered.content_hash !== contentHash ||
        offered.stale
      )
        throw Error();
      selected = dependencies.skills.find(
        (item) =>
          item.assetId === assetId &&
          item.contentHash === contentHash &&
          item.sourceVersion === offered.source_version &&
          item.skillId === skillId &&
          item.revision === revision,
      );
      if (!selected) throw Error();
      // Spec 0.63: a choice that expires while preparation awaits dispatches
      // nothing further; after activation the partial-selection error stands.
      const unexpired = () => {
        if (dependencies.wallNow() >= expires) throw Error();
      };
      const dispatch =
        dependencies.dispatch ??
        (await import("openclaw/plugin-sdk/gateway-method-runtime"))
          .dispatchGatewayMethod;
      current();
      const sessionKey = options.sessionAccessAuthority!.target.sessionKey;
      const timeout = () =>
        Math.max(
          1,
          Math.min(3000, Math.floor(deadlineMs - dependencies.monotonicNow())),
        );
      current();
      // Even a lost native response may follow a committed next-turn selection.
      await registeredCurrent();
      unexpired();
      selectionMayHaveChanged = true;
      const activated = await dispatch(
        "skills.library.activate",
        { sessionKey, action: "attach", skillId, revision },
        { timeoutMs: timeout() },
      );
      current();
      if (!activated.ok) throw Error();
      const activation = activated.payload;
      object(activation, ["sessionKey", "selections", "sessionActivation"]);
      if (
        activation.sessionKey !== sessionKey ||
        activation.sessionActivation !== "next-turn" ||
        !Array.isArray(activation.selections) ||
        !activation.selections.some(
          (item) => item.skillId === skillId && item.revision === revision,
        )
      )
        throw Error();
      await registeredCurrent();
      unexpired();
      const result = await dispatch(
        "skills.library.read",
        { sessionKey, skillId, revision },
        { timeoutMs: timeout() },
      );
      current();
      if (!result.ok) throw Error();
      const loaded = result.payload;
      object(loaded, ["entry", "content"], ["revisions", "files"]);
      if (
        loaded.entry?.skillId !== skillId ||
        loaded.entry?.revision !== revision ||
        loaded.entry?.removed === true ||
        typeof loaded.content !== "string" ||
        Buffer.byteLength(loaded.content, "utf8") > 1048576 ||
        createHash("sha256").update(loaded.content, "utf8").digest("hex") !==
          contentHash
      )
        throw Error();
      current();
      await registeredCurrent();
      options.respond(
        true,
        {
          skillId,
          revision,
          sessionActivation: "next-turn",
          instructions: "not_observed",
        },
        undefined,
      );
    } catch {
      options.respond(false, undefined, {
        code: "INVALID_REQUEST",
        message: "native_skill_unavailable",
        ...(selectionMayHaveChanged && selected
          ? {
              details: {
                selectionMayHaveChanged: true,
                skillId: selected.skillId,
                revision: selected.revision,
              },
            }
          : {}),
      });
    } finally {
      authority?.close();
    }
  };
}
