// Generated from the published gateway schemas. Do not edit by hand.
export type JsonValue =
  | null
  | boolean
  | number
  | bigint
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };
export type RecommendationViewV1 = {
  schema_version: 1;
  recommendation_id: string;
  task_id: string;
  state: "computing" | "ready" | "failed" | "cancelled" | "expired" | "chosen";
  state_version: bigint;
  created_at: string;
  ready_at: null | string;
  choice_expires_at: null | string;
  retain_until: string;
  catalog_revision: bigint;
  host_snapshot_revision: null;
  decision:
    | null
    | "confident"
    | "choice_required"
    | "no_match"
    | "empty_catalog";
  displayed_candidates: Array<{
    candidate: RecommendationResultV1PropertiesFinalCandidatesItems;
    asset: AssetRecordV1;
  }>;
  result: null | RecommendationResultV1;
  selected_action: null | ActionEvidenceV1;
  feedback_revision: bigint;
  error: null | {
    code:
      | "unauthenticated"
      | "internal_error"
      | "invalid_request"
      | "user_action_required"
      | "recommendation_not_found"
      | "feedback_not_found"
      | "stale_version"
      | "idempotency_conflict"
      | "recommendation_in_progress"
      | "recommendation_expired"
      | "asset_changed"
      | "invalid_state"
      | "overloaded"
      | "recommendation_unavailable"
      | "deadline_exceeded";
    message: string;
    run_id: null;
    recommendation_id: null | string;
  };
};
export type RecommendationResultV1 = {
  schema_version: 1;
  decision: "confident" | "choice_required" | "no_match" | "empty_catalog";
  catalog_revision: bigint;
  host_snapshot_revision: null;
  attempts: Array<RecommendationResultV1Selectorattemptv1>;
  primary_attempt: number | null;
  base_candidates: Array<RecommendationResultV1Candidatev2>;
  base_decision: "confident" | "choice_required" | "no_match" | "empty_catalog";
  final_candidates: Array<RecommendationResultV1Candidatev2>;
  final_decision:
    | "confident"
    | "choice_required"
    | "no_match"
    | "empty_catalog";
  fallback_reason: string | null;
  adaptation: null | {
    identity: {
      name: string;
      version: string;
    };
    feedback_revision: bigint;
    changed: boolean;
  };
  admission: {
    outcome: "accepted";
    rechecked_catalog_revision: bigint;
    checked_at: string;
  };
  observed_ms: number;
  embeddings: Array<{
    model: {
      name: string;
      version: string;
    };
    artifact_hash: string;
    representation_revision: string;
    device: string;
    input_bytes: number;
    input_tokens: number | null;
    duration_ms: number;
    cost_observations: Array<{
      provenance: "observed" | "reported";
      value: RecommendationResultV1Costv1;
    }>;
    outcome: "succeeded" | "failed" | "cancelled" | "not_ready";
  }>;
  host_run_ids: Array<string>;
};
export type RecommendationEventV1 =
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type:
        | "accepted"
        | "selector.started"
        | "selector.finished"
        | "adaptation.applied"
        | "ready"
        | "failed"
        | "cancelled"
        | "chosen"
        | "expired"
        | "feedback.recorded"
        | "feedback.deleted";
      actor: "gateway" | "selector" | "adapter";
      erased: true;
      payload: null;
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "accepted";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        configured_primary: {
          name: string;
          version: string;
        };
        task_id: string;
        task_text: string;
        conversation: Array<{
          role: "system" | "developer" | "user" | "assistant";
          text: string;
        }>;
        scope: {
          user_id: string;
          workspace_id: string;
        };
        budgets: {
          total_ms: number;
          primary_ms: number;
          candidate_limit: number;
          display_limit: number;
          max_catalog_records: number;
          max_catalog_bytes: number;
          max_result_bytes: number;
          max_create_bytes: number;
          max_task_bytes: number;
          max_conversation_entries: number;
          max_conversation_bytes: number;
          choice_seconds: number;
          retention_days: number;
          fallback_enabled: boolean;
        };
        config_digest: string;
        catalog_revision: bigint;
        host_snapshot_revision: null;
        requested_kinds: Array<"skill" | "mcp_tool">;
        selection_scope: "discovery" | "runtime";
        language_tags: Array<string>;
        code_switched: boolean;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "selector.started";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        attempt: number;
        selector: {
          name: string;
          version: string;
        };
        mechanism: string;
        remaining_ms: number;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "selector.finished";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        attempt_record: RecommendationResultV1PropertiesAttemptsItems;
        embeddings: Array<RecommendationResultV1PropertiesEmbeddingsItems>;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "adaptation.applied";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        adaptation: RecommendationResultV1PropertiesAdaptationAnyof1;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "ready";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        state_version: bigint;
        result: RecommendationResultV1;
        ready_at: string;
        choice_expires_at: string;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "failed";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        state_version: bigint;
        code: RecommendationViewV1PropertiesErrorAnyof1PropertiesCode;
        reason:
          | "interrupted"
          | "deadline"
          | "unavailable"
          | "internal"
          | "corrupt_catalog";
        active_ms: number;
        attempts: Array<RecommendationResultV1PropertiesAttemptsItems>;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "cancelled";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload:
        | {
            state_version: bigint;
            origin: "gateway";
            reason: "disconnect" | "shutdown";
            action: null;
            active_ms: number;
            attempts: Array<RecommendationResultV1PropertiesAttemptsItems>;
          }
        | {
            state_version: bigint;
            origin: "direct_user";
            reason: "user_cancel";
            action: ActionEvidenceV1;
            active_ms: number;
            attempts: Array<RecommendationResultV1PropertiesAttemptsItems>;
          };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "chosen";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        state_version: bigint;
        action: ActionEvidenceV1;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "expired";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        state_version: bigint;
        choice_expires_at: string;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "feedback.recorded";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        feedback_id: string;
        feedback_revision: bigint;
      };
    }
  | {
      schema_version: 1;
      recommendation_id: string;
      seq: bigint;
      timestamp: string;
      elapsed_ms: number;
      event_type: "feedback.deleted";
      actor: "gateway" | "selector" | "adapter";
      erased: false;
      payload: {
        feedback_id_digest: string;
        feedback_revision: bigint;
      };
    };
export type ReadinessV1 =
  | {
      schema_version: 1;
      state: "ready";
      reason: "usable_catalog";
      catalog_revision: bigint | null;
      catalog_size: number;
      lexical_ready: boolean;
      semantic_coverage: {
        ready: number;
        total: number;
      };
      pending_jobs: number;
      failed_jobs: number;
    }
  | {
      schema_version: 1;
      state: "empty";
      reason: "empty_catalog";
      catalog_revision: bigint | null;
      catalog_size: number;
      lexical_ready: boolean;
      semantic_coverage: {
        ready: number;
        total: number;
      };
      pending_jobs: number;
      failed_jobs: number;
    }
  | {
      schema_version: 1;
      state: "unavailable";
      reason: "disabled" | "corrupt_catalog" | "no_lexical_generation";
      catalog_revision: bigint | null;
      catalog_size: number;
      lexical_ready: boolean;
      semantic_coverage: {
        ready: number;
        total: number;
      };
      pending_jobs: number;
      failed_jobs: number;
    };
export type AssetRecordV1 =
  | {
      schema_version: 1;
      asset_id: string;
      kind: "skill";
      source_id: string;
      source_locator: string;
      source_version: string | null;
      captured_at: string;
      content_hash: string;
      description: string;
      language_tags: Array<string>;
      dependencies: Array<string>;
      availability: "available" | "needs_setup" | "unavailable" | "unknown";
      stale: boolean;
      identity: {
        package_id: string;
        package_version: string | null;
      };
    }
  | {
      schema_version: 1;
      asset_id: string;
      kind: "mcp_tool";
      source_id: string;
      source_locator: string;
      source_version: string | null;
      captured_at: string;
      content_hash: string;
      description: string;
      language_tags: Array<string>;
      dependencies: Array<string>;
      availability: "available" | "needs_setup" | "unavailable" | "unknown";
      stale: boolean;
      identity: {
        server_id: string;
        tool_name: string;
        exposed_name: string;
        input_schema: Record<string, JsonValue>;
        schema_hash: string;
      };
    };
export type ActionEvidenceV1 = {
  adapter_id: string;
  scope: {
    user_id: string;
    workspace_id: string;
  };
  recommendation_id: string;
  task_id: string;
  choice:
    | {
        kind: "asset";
        asset_id: string;
        content_hash: string;
      }
    | {
        kind: "none";
      }
    | {
        kind: "cancel";
      };
  catalog_revision: bigint;
  host_snapshot_revision: null;
  recorded_at: string;
  state_version: bigint;
};
export type FeedbackRecordV1 = {
  schema_version: 1;
  feedback_id: string;
  recommendation_id: string;
  task_id: string;
  scope: {
    user_id: string;
    workspace_id: string;
  };
  adapter_id: string;
  event_id: string;
  recorded_at: string;
  retain_until: string;
  feedback_revision: bigint;
  feedback:
    | {
        kind: "correction";
        asset_id: string;
        content_hash: string;
      }
    | {
        kind: "rejection";
        asset_id: string;
        content_hash: string;
      }
    | {
        kind: "usefulness";
        value: "useful" | "not_useful";
      };
  redacted_task_text: string;
  asset_content_hash: null | string;
  configured_primary: {
    name: string;
    version: string;
  };
  catalog_revision: bigint;
};
export type RecommendationResultV1PropertiesFinalCandidatesItems =
  RecommendationResultV1Candidatev2;
export type RecommendationResultV1Selectorattemptv1 = {
  attempt: number;
  selector: {
    name: string;
    version: string;
  };
  mechanism: string;
  outcome:
    | "ranked"
    | "no_match"
    | "not_ready"
    | "unavailable"
    | "stale"
    | "invalid"
    | "timeout"
    | "cancelled";
  reason: string | null;
  observed_ms: number;
  reported_ms: number | null;
  index_revision: string | null;
  cost_observations: Array<{
    provenance: "observed" | "reported";
    value: RecommendationResultV1Costv1;
  }>;
  result: RecommendationResultV1Selectionresultv2 | null;
};
export type RecommendationResultV1Candidatev2 = {
  asset_id: string;
  kind: "skill" | "mcp_tool";
  content_hash: string;
  source_version: string | null;
  score: number;
  score_kind: string;
  evidence_refs: Array<string>;
  availability?: "available" | "needs_setup" | "unavailable" | "unknown";
};
export type RecommendationResultV1Costv1 = {
  input_tokens: number | null;
  output_tokens: number | null;
  cached_tokens: number | null;
  reasoning_tokens: number | null;
  calls: number | null;
  cpu_ms: number | null;
  peak_bytes: number | null;
};
export type RecommendationResultV1PropertiesAttemptsItems =
  RecommendationResultV1Selectorattemptv1;
export type RecommendationResultV1PropertiesEmbeddingsItems = {
  model: {
    name: string;
    version: string;
  };
  artifact_hash: string;
  representation_revision: string;
  device: string;
  input_bytes: number;
  input_tokens: number | null;
  duration_ms: number;
  cost_observations: Array<{
    provenance: "observed" | "reported";
    value: RecommendationResultV1Costv1;
  }>;
  outcome: "succeeded" | "failed" | "cancelled" | "not_ready";
};
export type RecommendationResultV1PropertiesAdaptationAnyof1 = {
  identity: {
    name: string;
    version: string;
  };
  feedback_revision: bigint;
  changed: boolean;
};
export type RecommendationViewV1PropertiesErrorAnyof1PropertiesCode =
  | "unauthenticated"
  | "internal_error"
  | "invalid_request"
  | "user_action_required"
  | "recommendation_not_found"
  | "feedback_not_found"
  | "stale_version"
  | "idempotency_conflict"
  | "recommendation_in_progress"
  | "recommendation_expired"
  | "asset_changed"
  | "invalid_state"
  | "overloaded"
  | "recommendation_unavailable"
  | "deadline_exceeded";
export type RecommendationResultV1Selectionresultv2 = {
  schema_version: 2;
  recommendation_id: string;
  catalog_revision: bigint;
  host_snapshot_revision: null;
  selector: {
    name: string;
    version: string;
  };
  mechanism: string;
  outcome: "ranked" | "no_match" | "not_ready" | "unavailable";
  decision: "confident" | "choice_required" | "no_match" | null;
  candidates: Array<{
    asset_id: string;
    kind: "skill" | "mcp_tool";
    content_hash: string;
    source_version: string | null;
    score: number;
    score_kind: string;
    evidence_refs: Array<string>;
    availability?: "available" | "needs_setup" | "unavailable" | "unknown";
  }>;
  index_revision: string | null;
  elapsed_ms: number;
  usage_claims: null | {
    provenance: "reported";
    value: {
      input_tokens: number | null;
      output_tokens: number | null;
      cached_tokens: number | null;
      reasoning_tokens: number | null;
      calls: number | null;
      cpu_ms: number | null;
      peak_bytes: number | null;
    };
  };
};
