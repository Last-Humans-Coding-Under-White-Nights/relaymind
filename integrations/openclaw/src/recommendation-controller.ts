// Unregistered controller seam (RM37/RM39). There is deliberately no load/use method.
import {
  UnavailableHostBoundary,
  actionClientForSession,
} from "./recommendation-host.ts";
import type { HostBoundary, DirectActionInput } from "./recommendation-host.ts";
import { renderRecommendation } from "./recommendation-render.ts";
import type { CreateRecommendationV1 } from "./recommendation-contract.ts";
import type { RecommendationClient, WriteOptions } from "./recommendations.ts";
export interface RecommendationController {
  recommend(
    request: CreateRecommendationV1,
    options: WriteOptions,
  ): Promise<string>;
  act(
    input: DirectActionInput,
    context: unknown,
    options: WriteOptions,
  ): Promise<
    | { ok: false; code: "host_adapter_unavailable" }
    | {
        ok: true;
        label: "preference-only" | "feedback" | "privacy";
        receipt: unknown;
      }
  >;
}
export function createRecommendationController({
  client,
  hostBoundary = UnavailableHostBoundary,
}: {
  client: RecommendationClient;
  hostBoundary?: HostBoundary;
}): RecommendationController {
  return {
    async recommend(request, options) {
      return renderRecommendation(await client.create(request, options));
    },
    async act(input, context, options) {
      const session = await hostBoundary.authorizeDirectInput(context, input);
      const actions =
        session === null ? undefined : actionClientForSession(session);
      if (!actions) return { ok: false, code: "host_adapter_unavailable" };
      switch (input.kind) {
        case "choose":
          return {
            ok: true,
            label: "preference-only",
            receipt: await actions.choose(
              input.recommendationId,
              input.request,
              options,
            ),
          };
        case "feedback":
          return {
            ok: true,
            label: "feedback",
            receipt: await actions.feedback(
              input.recommendationId,
              input.request,
              options,
            ),
          };
        case "listFeedback":
          return {
            ok: true,
            label: "privacy",
            receipt: await actions.listFeedback(input.page, options),
          };
        case "deleteFeedback":
          return {
            ok: true,
            label: "privacy",
            receipt: await actions.deleteFeedback(
              input.feedbackId,
              input.expectedFeedbackRevision,
              options,
            ),
          };
        case "resetFeedback":
          return {
            ok: true,
            label: "privacy",
            receipt: await actions.resetFeedback(
              input.expectedFeedbackRevision,
              options,
            ),
          };
      }
    },
  };
}
