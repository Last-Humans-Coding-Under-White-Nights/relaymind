// Generated from the published Python schema; run scripts/sync-recommendation-contract.py.
export default {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "https://relaymind.local/schemas/feedback_record.v1.json",
  type: "object",
  additionalProperties: false,
  required: [
    "schema_version",
    "feedback_id",
    "recommendation_id",
    "task_id",
    "scope",
    "adapter_id",
    "event_id",
    "recorded_at",
    "retain_until",
    "feedback_revision",
    "feedback",
    "redacted_task_text",
    "asset_content_hash",
    "configured_primary",
    "catalog_revision",
  ],
  properties: {
    schema_version: {
      const: 1,
    },
    feedback_id: {
      type: "string",
      format: "uuid",
    },
    recommendation_id: {
      type: "string",
      format: "uuid",
    },
    task_id: {
      type: "string",
      pattern: "^[0-9a-f]{64}$",
    },
    scope: {
      type: "object",
      additionalProperties: false,
      required: ["user_id", "workspace_id"],
      properties: {
        user_id: {
          type: "string",
          minLength: 1,
          maxLength: 256,
        },
        workspace_id: {
          type: "string",
          minLength: 1,
          maxLength: 256,
        },
      },
    },
    adapter_id: {
      type: "string",
      minLength: 1,
      maxLength: 256,
    },
    event_id: {
      type: "string",
      minLength: 1,
      maxLength: 256,
    },
    recorded_at: {
      type: "string",
      format: "date-time",
    },
    retain_until: {
      type: "string",
      format: "date-time",
    },
    feedback_revision: {
      type: "integer",
      minimum: 1,
    },
    feedback: {
      oneOf: [
        {
          type: "object",
          additionalProperties: false,
          required: ["kind", "asset_id", "content_hash"],
          properties: {
            kind: {
              const: "correction",
            },
            asset_id: {
              type: "string",
              minLength: 1,
              maxLength: 256,
            },
            content_hash: {
              type: "string",
              pattern: "^[0-9a-f]{64}$",
            },
          },
        },
        {
          type: "object",
          additionalProperties: false,
          required: ["kind", "asset_id", "content_hash"],
          properties: {
            kind: {
              const: "rejection",
            },
            asset_id: {
              type: "string",
              minLength: 1,
              maxLength: 256,
            },
            content_hash: {
              type: "string",
              pattern: "^[0-9a-f]{64}$",
            },
          },
        },
        {
          type: "object",
          additionalProperties: false,
          required: ["kind", "value"],
          properties: {
            kind: {
              const: "usefulness",
            },
            value: {
              enum: ["useful", "not_useful"],
            },
          },
        },
      ],
    },
    redacted_task_text: {
      type: "string",
    },
    asset_content_hash: {
      anyOf: [
        {
          type: "null",
        },
        {
          type: "string",
          pattern: "^[0-9a-f]{64}$",
        },
      ],
    },
    configured_primary: {
      type: "object",
      additionalProperties: false,
      required: ["name", "version"],
      properties: {
        name: {
          type: "string",
          minLength: 1,
        },
        version: {
          type: "string",
          minLength: 1,
        },
      },
    },
    catalog_revision: {
      type: "integer",
      minimum: 0,
    },
  },
} as const;
