// RM16/RM37/RM39; AC28/AC30 (spec 0.65 section 7.6.1): the RelayMind-managed
// loaded-use fence. It stays refused unless the reviewed registry separately
// lists skill_loaded_use for these exact adapter bytes. It never exports
// instruction content in a receipt and never claims task success.
import { createHash } from "node:crypto";
import type { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";
import type {
  RecommendationClient,
  UserActionClient,
  ReadOptions,
} from "./recommendations.ts";
import {
  classifyGatewayError,
  prepareNativeAuthority,
} from "./recommendation-native-authority.ts";
import type {
  NativeActionOptions,
  NativeActionBinding,
  NativeScopeDependencies,
} from "./recommendation-native-authority.ts";
import type { NativeSkillBinding } from "./recommendation-native-config.ts";
import type { QualificationResolver } from "./recommendation-native-registry.ts";
import type { NativeUseLedger } from "./recommendation-native-ledger.ts";

/** Fixed refusal codes, a subset of the section 7.6.2 receipt vocabulary. */
export type NativeUseCode =
  | "unqualified"
  | "unauthorized"
  | "choice_unavailable"
  | "binding_changed"
  | "unavailable";
export class NativeUseRefusal extends Error {
  readonly code: NativeUseCode;
  /** False for a transient failure that refuses this attempt only. */
  readonly blocks: boolean;
  constructor(code: NativeUseCode, blocks = true) {
    super(code);
    this.code = code;
    this.blocks = blocks;
    this.name = "NativeUseRefusal";
  }
}
/** One chosen immutable revision managed in one exact session incarnation. */
export interface ManagedSkillUse {
  readonly sessionKey: string;
  readonly sessionId: string;
  readonly lifecycleRevision: string;
  readonly recommendationId: string;
  readonly assetId: string;
  readonly contentHash: string;
  readonly skillId: string;
  readonly revision: string;
}
interface Dependencies extends NativeScopeDependencies {
  qualification: QualificationResolver;
  /** The current operator association, read anew at every fence. */
  skills: () => readonly NativeSkillBinding[];
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
  /** Durable conversation blocks; without it no managed use proceeds. */
  ledger?: NativeUseLedger;
}
const MAX_SKILL_BYTES = 1048576;
function refuse(code: NativeUseCode): never {
  throw new NativeUseRefusal(code);
}
/** A transient gateway/host failure: refuse this attempt, never block. */
function transient(): never {
  throw new NativeUseRefusal("unavailable", false);
}
function incarnation(use: {
  sessionKey: string;
  sessionId: string;
  lifecycleRevision: string;
}): string {
  return JSON.stringify([use.sessionKey, use.sessionId, use.lifecycleRevision]);
}

/**
 * Checks, before native loading, after each await and immediately before
 * instruction delivery: fresh native authority for the same person and session
 * incarnation, registration, the retained unexpired choice, the exact eligible
 * asset and operator association, a nonremoved library entry and the
 * immutable selected bytes. A failure blocks further managed use in that
 * session incarnation; already delivered text is not claimed to be removed.
 */
export function createSkillLoadedUseFence(dependencies: Dependencies) {
  // A restart must not lift a block, so blocks live in the durable ledger.
  // An unpersisted stop still holds in this process; a failed write also
  // leaves the ledger refusing every later use.
  const unpersisted = new Set<string>();
  const block = (use: ManagedSkillUse) => {
    try {
      dependencies.ledger?.block(incarnation(use));
    } catch {
      unpersisted.add(incarnation(use));
    }
  };
  const isBlocked = (use: ManagedSkillUse): boolean => {
    if (!dependencies.ledger) refuse("unavailable");
    if (unpersisted.has(incarnation(use))) return true;
    try {
      return dependencies.ledger.isBlocked(incarnation(use));
    } catch {
      refuse("unavailable");
    }
  };
  async function verify(
    options: NativeActionOptions,
    use: ManagedSkillUse,
  ): Promise<string> {
    // Unqualified: no gateway request, no native dispatch.
    if (!dependencies.qualification("skill_loaded_use")) refuse("unqualified");
    if (isBlocked(use)) refuse("binding_changed");
    let authority: ReturnType<typeof prepareNativeAuthority> | undefined;
    // Only a failure attributable to the use's own verified person and
    // session incarnation blocks the conversation durably; an unverified
    // caller must not be able to block someone else's conversation.
    let attributed = false;
    try {
      try {
        authority = prepareNativeAuthority(options, dependencies);
      } catch {
        refuse("unauthorized");
      }
      const live = authority;
      const { binding, signal } = live;
      const target = options.sessionAccessAuthority!.target;
      const cancelled = () =>
        options.signal?.aborted === true ||
        options.client?.connectionSignal?.aborted === true;
      const current = () => {
        try {
          live.assertCurrent();
        } catch {
          // The fence's own deadline or a host cancellation is transient.
          if (live.expired() || cancelled()) transient();
          refuse("unauthorized");
        }
      };
      // RM16: only the gateway's own refusals are fence failures.
      const gatewayFailed: (error: unknown, choice: boolean) => never = (
        error,
        choice,
      ) => {
        if (error instanceof NativeUseRefusal) throw error;
        const failure = classifyGatewayError(error);
        if (failure === "unauthorized") refuse("unauthorized");
        if (failure === "choice_unavailable" && choice)
          refuse("choice_unavailable");
        transient();
      };
      // An earlier RPC's JSON identity is not authority for this turn.
      if (
        target.sessionKey !== use.sessionKey ||
        target.sessionId !== use.sessionId ||
        target.lifecycleRevision !== use.lifecycleRevision
      )
        refuse("unauthorized");
      attributed = true;
      // The deadline is read live: an excluded wait may extend it.
      const read = (): ReadOptions => ({
        signal,
        deadlineMs: live.deadline(),
        assertCurrent: current,
      });
      // Registration, retained unexpired choice, eligible asset and the live
      // operator association: checked before the native read and again after
      // it, at the final boundary before content is released for delivery.
      const state = async () => {
        try {
          const actions = await dependencies.actionClient(binding, read());
          current();
          await actions.listFeedback({ limit: 1 }, read());
        } catch (error) {
          gatewayFailed(error, false);
        }
        current();
        let view: Awaited<ReturnType<RecommendationClient["get"]>>;
        try {
          const client = await dependencies.recommendationClient(
            binding,
            read(),
          );
          current();
          view = await client.get(use.recommendationId, read());
        } catch (error) {
          gatewayFailed(error, true);
        }
        current();
        const action = view.selected_action;
        const expires = Date.parse(view.choice_expires_at ?? "");
        const unexpired = () => {
          if (!Number.isFinite(expires) || dependencies.wallNow() >= expires)
            refuse("choice_unavailable");
        };
        if (
          view.recommendation_id !== use.recommendationId ||
          view.state !== "chosen" ||
          !action ||
          action.recommendation_id !== use.recommendationId ||
          action.task_id !== view.task_id ||
          action.scope.user_id !== binding.userId ||
          action.scope.workspace_id !== binding.workspaceId ||
          action.choice.kind !== "asset" ||
          action.choice.asset_id !== use.assetId ||
          action.choice.content_hash !== use.contentHash
        )
          refuse("choice_unavailable");
        unexpired();
        const offered = view.displayed_candidates.find(
          (item) => item.asset.asset_id === use.assetId,
        )?.asset;
        if (
          !offered ||
          offered.kind !== "skill" ||
          offered.content_hash !== use.contentHash ||
          offered.stale
        )
          refuse("choice_unavailable");
        const mapped = dependencies
          .skills()
          .filter(
            (item) =>
              item.assetId === use.assetId &&
              item.contentHash === use.contentHash &&
              item.sourceVersion === offered.source_version,
          );
        if (
          mapped.length !== 1 ||
          mapped[0]!.skillId !== use.skillId ||
          mapped[0]!.revision !== use.revision
        )
          refuse("binding_changed");
        return unexpired;
      };
      const unexpired = await state();
      const dispatch =
        dependencies.dispatch ??
        (await import("openclaw/plugin-sdk/gateway-method-runtime"))
          .dispatchGatewayMethod;
      current();
      unexpired();
      // The exact immutable revision, never the library head.
      let result: Awaited<ReturnType<typeof dispatchGatewayMethod>>;
      try {
        result = await dispatch(
          "skills.library.read",
          {
            sessionKey: use.sessionKey,
            skillId: use.skillId,
            revision: use.revision,
          },
          {
            timeoutMs: Math.max(
              1,
              Math.min(
                3000,
                Math.floor(live.deadline() - dependencies.monotonicNow()),
              ),
            ),
          },
        );
      } catch {
        transient();
      }
      current();
      unexpired();
      // RM16: the host's generic retryable failure (UNAVAILABLE) refuses this
      // attempt only; a library refusal (missing/foreign revision, removed
      // entry, changed session) or any other result blocks.
      if (!result.ok && (result as any).error?.code === "UNAVAILABLE")
        transient();
      const loaded = result.ok ? (result.payload as any) : undefined;
      if (!loaded) refuse("binding_changed");
      if (
        typeof loaded !== "object" ||
        loaded.entry?.skillId !== use.skillId ||
        loaded.entry?.revision !== use.revision ||
        loaded.entry?.removed === true ||
        typeof loaded.content !== "string" ||
        Buffer.byteLength(loaded.content, "utf8") > MAX_SKILL_BYTES ||
        createHash("sha256").update(loaded.content, "utf8").digest("hex") !==
          use.contentHash
      )
        refuse("binding_changed");
      // HTTP observations are not a transaction with the native read: record
      // the post-read state at this boundary, then release the bytes.
      (await state())();
      current();
      return loaded.content;
    } catch (error) {
      const refusal =
        error instanceof NativeUseRefusal
          ? error
          : new NativeUseRefusal("unavailable", false);
      if (attributed && refusal.blocks) block(use);
      throw refusal;
    } finally {
      authority?.close();
    }
  }
  return {
    /** Before native loading and after the exact read of the pinned revision. */
    async load(options: NativeActionOptions, use: ManagedSkillUse) {
      const content = await verify(options, use);
      return { skillId: use.skillId, revision: use.revision, content };
    },
    /**
     * Immediately before the next model request: the complete payload must be
     * the selected revision's SKILL.md bytes, without any normalization. A
     * refusal means the caller must not dispatch that model request.
     */
    async deliver(
      options: NativeActionOptions,
      use: ManagedSkillUse,
      payload: string,
    ): Promise<void> {
      const content = await verify(options, use);
      const delivered = Buffer.from(payload, "utf8");
      if (
        !delivered.equals(Buffer.from(content, "utf8")) ||
        createHash("sha256").update(delivered).digest("hex") !== use.contentHash
      ) {
        block(use);
        refuse("binding_changed");
      }
    },
    /** A blocked conversation needs a fresh session, authority and choice. */
    isBlocked,
  };
}
