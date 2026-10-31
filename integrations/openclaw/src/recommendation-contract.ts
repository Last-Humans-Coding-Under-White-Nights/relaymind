// Published recommendation protocol and consistency admission (RM36/RM37; AC27/AC28).
import {
  decodeJson,
  encodeJson,
  MAX_SQLITE_INTEGER,
} from "./recommendation-json.ts";
import {
  bytes,
  invalid,
  published,
  UUID,
  validateSchema,
  type Schema,
} from "./recommendation-schema.ts";
import type {
  RecommendationViewV1,
  RecommendationResultV1,
  ReadinessV1,
  RecommendationEventV1,
  FeedbackRecordV1,
  ActionEvidenceV1,
} from "./recommendation-types.ts";
export type * from "./recommendation-types.ts";
export type CreateRecommendationV1 = {
  schema_version: 1;
  task_text: string;
  requested_kinds: ("skill" | "mcp_tool")[];
  selection_scope: "discovery" | "runtime";
  conversation?: {
    role: "system" | "developer" | "user" | "assistant";
    text: string;
  }[];
  language_tags?: string[];
  code_switched?: boolean;
  host_snapshot_revision?: null;
};
export type ChoiceRequestV1 = {
  schema_version: 1;
  expected_version: bigint;
  choice: ActionEvidenceV1["choice"];
};
export type FeedbackRequestV1 = {
  schema_version: 1;
  event_id: string;
  expected_feedback_revision: bigint;
  feedback: FeedbackRecordV1["feedback"];
};
export type RecommendationEventsV1 = {
  schema_version: 1;
  recommendation_id: string;
  events: RecommendationEventV1[];
  next_after_seq: bigint;
  has_more: boolean;
};
export type FeedbackPageV1 = {
  schema_version: 1;
  records: FeedbackRecordV1[];
  feedback_revision: bigint;
  next_after_id: string | null;
};
export type FeedbackReceiptV1 = {
  schema_version: 1;
  feedback_id: string;
  feedback_revision: bigint;
};
export type DeleteFeedbackReceiptV1 = {
  schema_version: 1;
  feedback_revision: bigint;
  deleted: true;
};
export type ResetFeedbackReceiptV1 = {
  schema_version: 1;
  feedback_revision: bigint;
  removed_count: number;
};
export function object(
  value: unknown,
  required: string[],
  optional: string[] = [],
): asserts value is Record<string, any> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    invalid();
  if (
    required.some((k) => !Object.hasOwn(value, k)) ||
    Object.keys(value).some(
      (k) => !required.includes(k) && !optional.includes(k),
    )
  )
    invalid();
}
export function identifier(value: unknown): asserts value is string {
  if (typeof value !== "string" || !UUID.test(value)) invalid();
}
export function revision(value: unknown): asserts value is bigint {
  if (typeof value !== "bigint" || value < 0n || value > MAX_SQLITE_INTEGER)
    invalid();
}
export function pageLimit(value: number): void {
  if (!Number.isSafeInteger(value) || value < 1 || value > 1000) invalid();
}
function version(value: Record<string, unknown>): void {
  if (value.schema_version !== 1) invalid();
}
function ordered(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(ordered);
  if (typeof value === "object" && value !== null)
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((k) => [k, ordered((value as Record<string, unknown>)[k])]),
    );
  return value;
}
function same(a: unknown, b: unknown): boolean {
  return encodeJson(ordered(a)) === encodeJson(ordered(b));
}
function candidates(items: { asset_id: string }[]): void {
  if (new Set(items.map((c) => c.asset_id)).size !== items.length) invalid();
}
function result(
  value: RecommendationResultV1,
  id?: string,
  check: () => void = () => {},
): void {
  published("recommendation_result.v1.json", value, check);
  if (value.decision !== value.final_decision) invalid();
  for (const [items, decision] of [
    [value.base_candidates, value.base_decision],
    [value.final_candidates, value.final_decision],
  ] as const) {
    candidates(items);
    if (
      Boolean(items.length) !==
      ["confident", "choice_required"].includes(decision)
    )
      invalid();
  }
  if (value.attempts.some((a, i) => a.attempt !== i + 1)) invalid();
  if (value.decision === "empty_catalog") {
    if (
      value.attempts.length ||
      value.primary_attempt !== null ||
      value.base_decision !== "empty_catalog"
    )
      invalid();
  } else if (value.primary_attempt !== 0 || !value.attempts.length) invalid();
  if (
    value.adaptation === null &&
    (!same(value.base_candidates, value.final_candidates) ||
      value.base_decision !== value.final_decision)
  )
    invalid();
  for (const attempt of value.attempts)
    if (attempt.result) {
      candidates(attempt.result.candidates);
      if (id && attempt.result.recommendation_id !== id) invalid();
      if (attempt.result.catalog_revision !== value.catalog_revision) invalid();
    }
  validateSchema(value.admission.checked_at, {
    type: "string",
    format: "date-time",
  });
}
export function validateRecommendationView(
  value: unknown,
  check: () => void = () => {},
): RecommendationViewV1 {
  published("recommendation_view.v1.json", value, check);
  const view = value as RecommendationViewV1;
  const derived =
    ["ready", "expired", "chosen"].includes(view.state) ||
    (view.state === "cancelled" && view.ready_at !== null);
  if (derived) {
    if (
      view.result === null ||
      view.ready_at === null ||
      view.choice_expires_at === null
    )
      invalid();
    result(view.result, view.recommendation_id, check);
    if (
      view.decision !== view.result.decision ||
      view.catalog_revision !== view.result.catalog_revision
    )
      invalid();
    if (
      view.displayed_candidates.length !==
      Math.min(3, view.result.final_candidates.length)
    )
      invalid();
    for (const [i, entry] of view.displayed_candidates.entries()) {
      if (!same(entry.candidate, view.result.final_candidates[i])) invalid();
      for (const field of [
        "asset_id",
        "content_hash",
        "kind",
        "source_version",
      ] as const)
        if (entry.candidate[field] !== entry.asset[field]) invalid();
      for (const pointer of entry.candidate.evidence_refs) {
        let field: unknown = entry.asset;
        for (const part of pointer.split("/").slice(1)) {
          const key = part.replace(/~1/g, "/").replace(/~0/g, "~");
          if (
            typeof field !== "object" ||
            field === null ||
            !Object.hasOwn(field, key)
          )
            invalid();
          field = (field as Record<string, unknown>)[key];
        }
      }
      if (
        entry.candidate.availability !== undefined &&
        entry.candidate.availability !== entry.asset.availability
      )
        invalid();
    }
  } else if (
    [view.result, view.decision, view.ready_at, view.choice_expires_at].some(
      (v) => v !== null,
    ) ||
    view.displayed_candidates.length
  )
    invalid();
  if ((view.error !== null) !== (view.state === "failed")) invalid();
  if (
    view.error?.recommendation_id !== null &&
    view.error?.recommendation_id !== undefined &&
    view.error.recommendation_id !== view.recommendation_id
  )
    invalid();
  if (view.selected_action) {
    if (
      view.selected_action.recommendation_id !== view.recommendation_id ||
      view.selected_action.task_id !== view.task_id ||
      view.selected_action.state_version !== view.state_version ||
      !["chosen", "cancelled"].includes(view.state)
    )
      invalid();
    if (
      (view.selected_action.choice.kind === "cancel") !==
      (view.state === "cancelled")
    )
      invalid();
  }
  if (
    (view.state === "chosen" || (view.state === "cancelled" && derived)) &&
    !view.selected_action
  )
    invalid();
  return view;
}
export function parseProtocol(
  text: string,
  check: () => void = () => {},
): unknown {
  if (text.length > 33554432 || bytes(text, check) > 33554432) invalid();
  return decodeJson(text, check);
}
export function decodeRecommendationView(
  text: string,
  check: () => void = () => {},
): RecommendationViewV1 {
  return validateRecommendationView(parseProtocol(text, check), check);
}
export function decodeReadiness(
  text: string,
  check: () => void = () => {},
): ReadinessV1 {
  const value = parseProtocol(text, check) as ReadinessV1;
  published("recommendation_readiness.v1.json", value, check);
  if (value.semantic_coverage.ready > value.semantic_coverage.total) invalid();
  return value;
}
export function decodeEvents(
  text: string,
  id: string,
  after: bigint,
  limit: number,
  check: () => void = () => {},
): RecommendationEventsV1 {
  const page = parseProtocol(text, check);
  object(page, [
    "schema_version",
    "recommendation_id",
    "events",
    "next_after_seq",
    "has_more",
  ]);
  version(page);
  revision(page.next_after_seq);
  if (
    page.recommendation_id !== id ||
    !Array.isArray(page.events) ||
    page.events.length > limit ||
    typeof page.has_more !== "boolean"
  )
    invalid();
  let previous = after;
  for (const event of page.events) {
    published("recommendation_event.v1.json", event, check);
    if (event.recommendation_id !== id || event.seq <= previous) invalid();
    previous = event.seq;
    if (
      !event.erased &&
      (event.event_type === "chosen" || event.event_type === "cancelled") &&
      event.payload.action !== null
    ) {
      const action = event.payload.action;
      if (
        action.recommendation_id !== id ||
        action.state_version !== event.payload.state_version ||
        (action.choice.kind === "cancel") !== (event.event_type === "cancelled")
      )
        invalid();
    }
    if (!event.erased && event.event_type === "ready")
      result(event.payload.result, id, check);
  }
  if (
    page.next_after_seq !== previous ||
    (page.has_more && page.events.length !== limit)
  )
    invalid();
  return page as RecommendationEventsV1;
}
export function validateCreate(
  value: unknown,
): asserts value is CreateRecommendationV1 {
  object(
    value,
    ["schema_version", "task_text", "requested_kinds", "selection_scope"],
    [
      "conversation",
      "language_tags",
      "code_switched",
      "host_snapshot_revision",
    ],
  );
  version(value);
  if (
    typeof value.task_text !== "string" ||
    !value.task_text.trim() ||
    bytes(value.task_text) > 8192 ||
    !["discovery", "runtime"].includes(value.selection_scope) ||
    (value.host_snapshot_revision !== undefined &&
      value.host_snapshot_revision !== null) ||
    (value.code_switched !== undefined &&
      typeof value.code_switched !== "boolean")
  )
    invalid();
  validateSchema(value.requested_kinds, {
    type: "array",
    minItems: 1,
    maxItems: 2,
    uniqueItems: true,
    items: { enum: ["skill", "mcp_tool"] },
  });
  validateSchema(value.language_tags ?? [], {
    type: "array",
    maxItems: 32,
    uniqueItems: true,
    items: { type: "string", pattern: "^[A-Za-z0-9-]{1,64}$" },
  });
  if (value.conversation !== undefined) {
    if (!Array.isArray(value.conversation) || value.conversation.length > 32)
      invalid();
    let size = 0;
    for (const m of value.conversation) {
      object(m, ["role", "text"]);
      if (
        !["system", "developer", "user", "assistant"].includes(m.role) ||
        typeof m.text !== "string"
      )
        invalid();
      size += bytes(m.text);
    }
    if (size > 32768) invalid();
  }
  if (bytes(encodeJson(value)) > 524288) invalid();
}
export function validateChoice(
  value: unknown,
): asserts value is ChoiceRequestV1 {
  object(value, ["schema_version", "expected_version", "choice"]);
  version(value);
  revision(value.expected_version);
  validateSchema(value.choice, {
    oneOf: [
      {
        type: "object",
        additionalProperties: false,
        required: ["kind", "asset_id", "content_hash"],
        properties: {
          kind: { const: "asset" },
          asset_id: { type: "string", minLength: 1 },
          content_hash: { type: "string", pattern: "^[0-9a-f]{64}$" },
        },
      },
      ...["none", "cancel"].map((kind) => ({
        type: "object",
        additionalProperties: false,
        required: ["kind"],
        properties: { kind: { const: kind } },
      })),
    ],
  });
}
export function validateFeedback(
  value: unknown,
): asserts value is FeedbackRequestV1 {
  object(value, [
    "schema_version",
    "event_id",
    "expected_feedback_revision",
    "feedback",
  ]);
  version(value);
  revision(value.expected_feedback_revision);
  validateSchema(
    value.event_id,
    { type: "string", minLength: 1, maxLength: 256 },
    undefined,
    "event_id",
  );
  validateSchema(value.feedback, {
    $ref: "feedback_record.v1.json#/properties/feedback",
  });
}
export function decodeFeedback(
  text: string,
  kind: "create" | "list" | "delete" | "reset",
  check: () => void = () => {},
):
  | FeedbackReceiptV1
  | FeedbackPageV1
  | DeleteFeedbackReceiptV1
  | ResetFeedbackReceiptV1 {
  const value = parseProtocol(text, check);
  const extra = {
    create: ["feedback_id"],
    list: ["records", "next_after_id"],
    delete: ["deleted"],
    reset: ["removed_count"],
  }[kind];
  object(value, ["schema_version", "feedback_revision", ...extra]);
  version(value);
  revision(value.feedback_revision);
  if (kind === "create") identifier(value.feedback_id);
  if (kind === "delete" && value.deleted !== true) invalid();
  if (
    kind === "reset" &&
    (!Number.isSafeInteger(value.removed_count) || value.removed_count < 0)
  )
    invalid();
  if (kind === "list") {
    if (
      !Array.isArray(value.records) ||
      value.records.length > 1000 ||
      (value.next_after_id !== null &&
        (typeof value.next_after_id !== "string" ||
          !/^[A-Za-z0-9_-]{1,32}$/.test(value.next_after_id)))
    )
      invalid();
    const ids = new Set();
    for (const record of value.records) {
      published("feedback_record.v1.json", record, check);
      if (
        ids.has(record.feedback_id) ||
        record.feedback_revision > value.feedback_revision
      )
        invalid();
      ids.add(record.feedback_id);
    }
  }
  return value as
    | FeedbackReceiptV1
    | FeedbackPageV1
    | DeleteFeedbackReceiptV1
    | ResetFeedbackReceiptV1;
}
export { encodeJson };
