// RM37/RM39; AC28/AC30. Pure handler; registration stays disabled until qualification.
import type { DirectActionInput } from "./recommendation-host.ts";
import type { CreateRecommendationV1 } from "./recommendation-contract.ts";
import {
  object,
  validateChoice,
  validateFeedback,
  validateCreate,
  encodeJson,
} from "./recommendation-contract.ts";
import { MAX_SQLITE_INTEGER } from "./recommendation-json.ts";
import type {
  RecommendationClient,
  UserActionClient,
  ReadOptions,
  WriteOptions,
} from "./recommendations.ts";
import { RecommendationClientError } from "./recommendations.ts";
import { renderRecommendation } from "./recommendation-render.ts";

import { prepareNativeAuthority } from "./recommendation-native-authority.ts";
import type {
  NativeActionOptions,
  NativeActionBinding,
  NativeScopeDependencies,
} from "./recommendation-native-authority.ts";
export type {
  NativeActionOptions,
  NativeActionBinding,
} from "./recommendation-native-authority.ts";
interface Dependencies extends NativeScopeDependencies {
  actionClient: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<UserActionClient>;
  recommendationClient?: (
    binding: NativeActionBinding,
    options: ReadOptions,
  ) => Promise<RecommendationClient>;
}
type Input =
  | DirectActionInput
  | { kind: "recommend"; request: CreateRecommendationV1 };
function decimal(value: unknown): bigint {
  if (typeof value !== "string" || !/^(0|[1-9][0-9]{0,18})$/.test(value))
    throw Error("invalid_input");
  const result = BigInt(value);
  if (result > MAX_SQLITE_INTEGER) throw Error("invalid_input");
  return result;
}
function input(value: unknown): Input {
  object(
    value,
    ["kind"],
    [
      "request",
      "recommendationId",
      "page",
      "feedbackId",
      "expectedFeedbackRevision",
    ],
  );
  switch (value.kind) {
    case "recommend":
      object(value, ["kind", "request"]);
      validateCreate(value.request);
      if (value.request.selection_scope !== "discovery")
        throw Error("runtime_unavailable");
      return { kind: "recommend", request: value.request };
    case "choose": {
      object(value, ["kind", "recommendationId", "request"]);
      object(value.request, ["schema_version", "expected_version", "choice"]);
      const request = {
        ...value.request,
        expected_version: decimal(value.request.expected_version),
      };
      validateChoice(request);
      return {
        kind: "choose",
        recommendationId: value.recommendationId,
        request,
      };
    }
    case "feedback": {
      object(value, ["kind", "recommendationId", "request"]);
      object(value.request, [
        "schema_version",
        "event_id",
        "expected_feedback_revision",
        "feedback",
      ]);
      const request = {
        ...value.request,
        expected_feedback_revision: decimal(
          value.request.expected_feedback_revision,
        ),
      };
      validateFeedback(request);
      return {
        kind: "feedback",
        recommendationId: value.recommendationId,
        request,
      };
    }
    case "listFeedback":
      object(value, ["kind", "page"]);
      object(value.page, ["limit"], ["afterId"]);
      return {
        kind: "listFeedback",
        page: {
          limit: value.page.limit,
          ...(value.page.afterId === undefined
            ? {}
            : { afterId: value.page.afterId }),
        },
      };
    case "deleteFeedback":
      object(value, ["kind", "feedbackId", "expectedFeedbackRevision"]);
      return {
        kind: "deleteFeedback",
        feedbackId: value.feedbackId,
        expectedFeedbackRevision: decimal(value.expectedFeedbackRevision),
      };
    case "resetFeedback":
      object(value, ["kind", "expectedFeedbackRevision"]);
      return {
        kind: "resetFeedback",
        expectedFeedbackRevision: decimal(value.expectedFeedbackRevision),
      };
    default:
      throw Error("invalid_input");
  }
}

export function createNativeActionHandler(
  dependencies: Dependencies,
): (options: NativeActionOptions) => Promise<void> {
  return async (options) => {
    let authority: ReturnType<typeof prepareNativeAuthority> | undefined;
    try {
      object(
        options.params,
        [
          "sessionKey",
          "expectedSessionId",
          "expectedLifecycleRevision",
          "input",
          "idempotencyKey",
        ],
        ["agentId"],
      );
      authority = prepareNativeAuthority(options, dependencies);
      const { binding, signal, deadlineMs, assertCurrent: current } = authority;
      const action = input(options.params.input);
      const key = options.params.idempotencyKey;
      if (
        typeof key !== "string" ||
        key !== key.trim() ||
        !/^[\x20-\x7e]{1,255}$/.test(key)
      )
        throw Error("invalid_key");
      const write: WriteOptions = {
        signal,
        deadlineMs,
        idempotencyKey: key,
        assertCurrent: current,
      };
      let label: "preference-only" | "feedback" | "privacy" = "privacy";
      let receipt: unknown;
      if (action.kind === "recommend") {
        if (!dependencies.recommendationClient)
          throw Error("recommendation_unavailable");
        const client = await dependencies.recommendationClient(binding, write);
        current();
        const view = await client.create(action.request, write);
        current();
        options.respond(
          true,
          {
            label: "recommendation",
            text: renderRecommendation(view),
            receipt_json: encodeJson(view),
          },
          undefined,
        );
        return;
      }
      const actions = await dependencies.actionClient(binding, write);
      current();
      switch (action.kind) {
        case "choose":
          label = "preference-only";
          receipt = await actions.choose(
            action.recommendationId,
            action.request,
            write,
          );
          break;
        case "feedback":
          label = "feedback";
          receipt = await actions.feedback(
            action.recommendationId,
            action.request,
            write,
          );
          break;
        case "listFeedback":
          receipt = await actions.listFeedback(action.page, write);
          break;
        case "deleteFeedback":
          receipt = await actions.deleteFeedback(
            action.feedbackId,
            action.expectedFeedbackRevision,
            write,
          );
          break;
        case "resetFeedback":
          receipt = await actions.resetFeedback(
            action.expectedFeedbackRevision,
            write,
          );
          break;
      }
      // A committed HTTP action is historical evidence. Retired host authority
      // cannot undo it, and this handler grants no subsequent native use.
      try {
        current();
      } catch {
        /* Preserve the committed receipt without renewed authority. */
      }
      options.respond(
        true,
        { label, receipt_json: encodeJson(receipt) },
        undefined,
      );
    } catch (error) {
      const code =
        error instanceof RecommendationClientError
          ? error.code
          : "native_action_unavailable";
      options.respond(false, undefined, {
        code: "INVALID_REQUEST",
        message: code,
      });
    } finally {
      authority?.close();
    }
  };
}
