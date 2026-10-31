// Dormant recommendation/action clients (RM36/RM37/RM39; spec 7.6). No retries.
import {
  decodeRecommendationView,
  decodeReadiness,
  decodeEvents,
  decodeFeedback,
  identifier,
  revision,
  pageLimit,
  validateCreate,
  validateChoice,
  validateFeedback,
} from "./recommendation-contract.ts";
import type {
  CreateRecommendationV1,
  ChoiceRequestV1,
  FeedbackRequestV1,
  RecommendationViewV1,
  ReadinessV1,
  RecommendationEventsV1,
  FeedbackPageV1,
  FeedbackReceiptV1,
  DeleteFeedbackReceiptV1,
  ResetFeedbackReceiptV1,
} from "./recommendation-contract.ts";
import { transport, input } from "./recommendation-transport.ts";
import type {
  ClientConfig,
  ActionClientConfig,
  ReadOptions,
  WriteOptions,
} from "./recommendation-transport.ts";
export { RecommendationClientError } from "./recommendation-transport.ts";
export type { ClientConfig, ActionClientConfig, ReadOptions, WriteOptions };
export interface RecommendationClient {
  create(
    request: CreateRecommendationV1,
    options: WriteOptions,
  ): Promise<RecommendationViewV1>;
  get(id: string, options: ReadOptions): Promise<RecommendationViewV1>;
  readiness(options: ReadOptions): Promise<ReadinessV1>;
  events(
    id: string,
    page: { afterSeq: bigint; limit: number },
    options: ReadOptions,
  ): Promise<RecommendationEventsV1>;
}
export interface UserActionClient {
  choose(
    id: string,
    request: ChoiceRequestV1,
    options: WriteOptions,
  ): Promise<RecommendationViewV1>;
  feedback(
    id: string,
    request: FeedbackRequestV1,
    options: WriteOptions,
  ): Promise<FeedbackReceiptV1>;
  listFeedback(
    page: { afterId?: string; limit: number },
    options: ReadOptions,
  ): Promise<FeedbackPageV1>;
  deleteFeedback(
    id: string,
    expectedFeedbackRevision: bigint,
    options: WriteOptions,
  ): Promise<DeleteFeedbackReceiptV1>;
  resetFeedback(
    expectedFeedbackRevision: bigint,
    options: WriteOptions,
  ): Promise<ResetFeedbackReceiptV1>;
}
const base = "/relay/v1/recommendations";
export function createRecommendationClient(
  config: ClientConfig,
): RecommendationClient {
  const call = transport(config);
  return {
    async create(request, opts) {
      input(() => validateCreate(request));
      return call("POST", base, request, opts, decodeRecommendationView);
    },
    async get(id, opts) {
      input(() => identifier(id));
      return call("GET", base + "/" + id, undefined, opts, (text, check) => {
        const view = decodeRecommendationView(text, check);
        if (view.recommendation_id !== id) throw Error();
        return view;
      });
    },
    async readiness(opts) {
      return call("GET", base + "/readiness", undefined, opts, decodeReadiness);
    },
    async events(id, page, opts) {
      input(() => {
        identifier(id);
        revision(page.afterSeq);
        pageLimit(page.limit);
      });
      return call(
        "GET",
        base +
          "/" +
          id +
          "/events?after_seq=" +
          String(page.afterSeq) +
          "&limit=" +
          page.limit,
        undefined,
        opts,
        (text, check) =>
          decodeEvents(text, id, page.afterSeq, page.limit, check),
      );
    },
  };
}
export function createUserActionClient(
  config: ActionClientConfig,
): UserActionClient {
  const call = transport(config, config.actionToken);
  return {
    async choose(id, request, opts) {
      input(() => {
        identifier(id);
        validateChoice(request);
      });
      return call(
        "POST",
        base + "/" + id + "/choice",
        request,
        opts,
        (text, check) => {
          const view = decodeRecommendationView(text, check);
          if (view.recommendation_id !== id) throw Error();
          return view;
        },
      );
    },
    async feedback(id, request, opts) {
      input(() => {
        identifier(id);
        validateFeedback(request);
      });
      return call(
        "POST",
        base + "/" + id + "/feedback",
        request,
        opts,
        (t, c) => decodeFeedback(t, "create", c) as FeedbackReceiptV1,
      );
    },
    async listFeedback(page, opts) {
      input(() => {
        pageLimit(page.limit);
        if (
          page.afterId !== undefined &&
          (typeof page.afterId !== "string" ||
            !/^[A-Za-z0-9_-]{1,32}$/.test(page.afterId))
        )
          throw Error();
      });
      const query =
        "?limit=" +
        page.limit +
        (page.afterId === undefined
          ? ""
          : "&after_id=" + encodeURIComponent(page.afterId));
      return call(
        "GET",
        "/relay/v1/feedback" + query,
        undefined,
        opts,
        (t, c) => {
          const pageResult = decodeFeedback(t, "list", c) as FeedbackPageV1;
          if (pageResult.records.length > page.limit) throw Error();
          return pageResult;
        },
      );
    },
    async deleteFeedback(id, expectedFeedbackRevision, opts) {
      input(() => {
        identifier(id);
        revision(expectedFeedbackRevision);
      });
      return call(
        "DELETE",
        "/relay/v1/feedback/" +
          id +
          "?expected_feedback_revision=" +
          expectedFeedbackRevision,
        undefined,
        opts,
        (t, c) => decodeFeedback(t, "delete", c) as DeleteFeedbackReceiptV1,
      );
    },
    async resetFeedback(expectedFeedbackRevision, opts) {
      input(() => revision(expectedFeedbackRevision));
      return call(
        "POST",
        "/relay/v1/feedback/reset",
        {
          schema_version: 1,
          expected_feedback_revision: expectedFeedbackRevision,
        },
        opts,
        (t, c) => decodeFeedback(t, "reset", c) as ResetFeedbackReceiptV1,
      );
    },
  };
}
